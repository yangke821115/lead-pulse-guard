"""
Lead Pulse Guard - Meta Webhook Gateway

Receives Facebook Page webhook events, verifies the
X-Hub-Signature-256 HMAC sent by Meta, tags each event with the Facebook
Page it originated from, and decides whether the comment is a candidate
for an automated reply.

Security model
--------------
- POST /webhook : REQUIRES a valid X-Hub-Signature-256 HMAC.
  Requests whose signature does not match are rejected with HTTP 403 and
  their body is deliberately NOT logged.
- The HMAC is computed over the RAW request body bytes, never over a
  re-serialised (json.dumps) version of the parsed payload. Re-encoding
  would change whitespace / key order and the digest would never match.
- Comparison uses hmac.compare_digest() (timing safe); plain == is not used.
- The App Secret is read at startup from a read-only mounted file
  (default /run/secrets/meta_app_secret), with an environment-variable
  fallback (META_APP_SECRET).
- If no App Secret is available the process REFUSES TO START. It must never
  fall back to accepting unverified events.

Routing and eligibility model
-----------------------------
- The webhook subscription is APP-level, so every Page that subscribes to
  this app delivers events to this single endpoint. Recognising the Page is
  therefore the only way to tell them apart.
- PAGE_REGISTRY maps a Page ID to a display name and a knowledge-base slug.
- The registry is TAGGING METADATA. Nothing is dispatched to any knowledge
  base and no reply is ever sent by this service.

Eligibility rules
-----------------
Two independent loops have to be stopped, and neither check replaces the
other:

1. Machine-to-machine loop. A reply sent by one of our own Pages produces
   another webhook event. Replying to it would reply to ourselves forever.
   Stopped structurally: if the commenter is one of our own Pages, the event
   is never a reply candidate. This check alone kills the automatic loop.

2. Human politeness loop. A person answers a reply with a bare courtesy
   such as "thanks" or "ok", the bot answers again, and so on. The identity
   check cannot catch this, because every message is a genuine new comment
   from a human. Stopped by the closing-phrase filter below.

The closing-phrase filter is deliberately biased so that its failures are
harmless. Skipping a real question is the damaging error, so:

  - A question always wins. If the message carries a question mark or an
    interrogative word, it is a reply candidate regardless of anything else.
  - Matching is whole-message, not substring. "thanks, but what is the
    dosage?" contains a courtesy word but is obviously a real question.
  - The filter strips known courtesy tokens and filler particles and only
    skips when nothing meaningful is left.

The worst case of this bias is replying when a reply was not needed, which
is a cosmetic annoyance rather than a lost enquiry.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import unicodedata

from fastapi import FastAPI, HTTPException, Request, Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app = FastAPI()

VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "")
SIGNATURE_HEADER = "X-Hub-Signature-256"
SECRET_FILE = os.environ.get("META_APP_SECRET_FILE", "/run/secrets/meta_app_secret")

# Event spool: the single hand-off point to the AI worker. A local file, never
# a network call -- this service keeps zero outbound capability and holds no
# credential for any downstream system.
SPOOL_FILE = os.environ.get("EVENT_SPOOL_FILE", "/spool/events.jsonl")
SPOOL_MAX_BYTES = int(os.environ.get("EVENT_SPOOL_MAX_BYTES", str(32 * 1024 * 1024)))
SPOOL_SCHEMA = "leadpulse.event.comment/v1"
LEAD_SPOOL_SCHEMA = "leadpulse.event.lead/v1"

# ---------------------------------------------------------------------------
# Page registry
# ---------------------------------------------------------------------------
# Maps Facebook Page ID -> display name and knowledge-base slug.
#
# ROUTING METADATA ONLY. Nothing is written to any knowledge base at this
# stage, and no reply is ever sent.
#
# Kept out of the source so no customer identifiers live in the repository.
# Point PAGES_CONFIG_FILE at a JSON object shaped like pages.example.json.
# If that file is absent, the PAGES env var (comma-separated IDs) is used as
# a fallback and each Page gets a generic name.
# ---------------------------------------------------------------------------
PAGES_CONFIG_FILE = os.environ.get(
    "PAGES_CONFIG_FILE", "/etc/leadpulse/pages.json"
)


def _load_page_registry() -> dict:
    """Build Page ID -> {name, kb}. Returns {} instead of raising."""
    if os.path.isfile(PAGES_CONFIG_FILE):
        with open(PAGES_CONFIG_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("%s must contain a JSON object" % PAGES_CONFIG_FILE)
        registry = {}
        for page_id, meta in data.items():
            if not isinstance(meta, dict):
                raise ValueError("entry for %s must be an object" % page_id)
            registry[str(page_id)] = {
                "name": str(meta.get("name") or "page-%s" % str(page_id)[-4:]),
                "kb": str(meta.get("kb") or "default"),
            }
        return registry

    raw = os.environ.get("PAGES", "").strip()
    if raw:
        registry = {}
        for index, page_id in enumerate(
            pid.strip() for pid in raw.split(",") if pid.strip()
        ):
            registry[page_id] = {"name": "page-%d" % index, "kb": "default"}
        return registry

    return {}


PAGE_REGISTRY = _load_page_registry()

if not PAGE_REGISTRY:
    logging.warning(
        "Page registry is EMPTY (no %s and no PAGES env var). "
        "Every event will be recorded as UNMAPPED and nothing will be routed.",
        PAGES_CONFIG_FILE,
    )


# ---------------------------------------------------------------------------
# Reply-eligibility tuning
# ---------------------------------------------------------------------------
# Courtesy closers. After normalisation these are stripped from the message;
# if nothing meaningful remains the message is treated as an acknowledgement
# and is NOT answered.
CLOSING_TOKENS = (
    # Chinese - multi-character first is not required, tokens are sorted by
    # length at import time so the longest match always wins.
    "\u597d\u7684", "\u597d\u7684\u597d\u7684", "\u5f88\u597d", "\u633a\u597d",
    "\u4e0d\u9519", "\u6536\u5230", "\u77e5\u9053\u4e86", "\u660e\u767d\u4e86",
    "\u4e86\u89e3\u4e86", "\u61c2\u4e86", "\u6ca1\u95ee\u9898", "\u6ca1\u4e8b",
    "\u5ba2\u6c14", "\u8f9b\u82e6\u4e86", "\u8c22\u8c22", "\u591a\u8c22",
    "\u611f\u8c22", "\u8c22\u4e86", "\u5475\u5475", "\u54c8\u54c8", "\u55ef\u55ef",
    "\u54e6\u54e6", "\u77e5\u9053", "\u660e\u767d", "\u4e86\u89e3", "\u6536\u5230",
    "\u597d", "\u884c", "\u53ef\u4ee5", "\u55ef", "\u54e6", "\u662f\u7684",
    "\u597d\u7684", "\u6b63\u597d",
    # English (spaces are removed by normalisation, so "thank you" -> thankyou)
    "thankyou", "thanks", "thank", "thx", "ty", "okay", "okok", "ok",
    "gotit", "isee", "understood", "noted", "alright", "sure", "cool",
    "nice", "great", "good", "kk", "k",
)

# Particles that carry no request on their own. Stripped alongside the
# closers so that "ok thanks teacher" collapses to nothing.
FILLER_TOKENS = (
    "\u8001\u5e08", "\u5927\u5bb6", "\u8001\u677f", "\u4eb2", "\u5404\u4f4d",
    "\u6211\u4eec", "\u4f60\u4eec", "\u4f60", "\u60a8", "\u6211",
    "\u7684", "\u4e86", "\u5566", "\u5427", "\u554a", "\u5440", "\u561b",
)

# Question markers. Checked against the ORIGINAL message, because
# normalisation removes the punctuation that gives them away. A hit here
# forces the message to be treated as a reply candidate.
_ZH_QUESTION_MARKERS = (
    "\uff1f", "?", "\u5417", "\u5462", "\u4ec0\u4e48", "\u600e\u4e48",
    "\u600e\u6837", "\u591a\u5c11", "\u4ef7\u683c", "\u62a5\u4ef7",
    "\u662f\u5426", "\u80fd\u4e0d\u80fd", "\u6709\u6ca1\u6709",
    "\u662f\u4e0d\u662f", "\u54ea", "\u51e0",
)
_EN_QUESTION_RE = re.compile(
    r"\b(how|what|why|when|where|which|who|price|cost|quote|quotation"
    r"|spec|specs|specification|delivery|warranty|moq)\b",
    re.IGNORECASE,
)

# Maximum number of times we will answer inside a single comment thread.
MAX_REPLY_DEPTH = 3

# How long a comment id is remembered for duplicate suppression.
DEDUP_TTL_SECONDS = 3600

_TOKEN_ORDER = sorted(
    set(CLOSING_TOKENS) | set(FILLER_TOKENS), key=len, reverse=True
)

# Characters that survive category filtering but still carry no meaning.
_IGNORED_CHARS = {"\u200d", "\ufe0f", "\ufe0e", "\u20e3"}


def _load_app_secret() -> bytes:
    """Load the Meta App Secret from a file first, then the environment."""
    try:
        with open(SECRET_FILE, "rb") as fh:
            secret = fh.read().strip()
        if secret:
            logging.info("App Secret loaded from file %s", SECRET_FILE)
            return secret
        logging.error("FATAL: App Secret file %s is empty.", SECRET_FILE)
        sys.exit(1)
    except FileNotFoundError:
        logging.warning(
            "App Secret file %s not found; falling back to environment.", SECRET_FILE
        )
    except OSError as exc:
        logging.error("FATAL: cannot read App Secret file %s: %s", SECRET_FILE, exc)
        sys.exit(1)

    env_secret = os.environ.get("META_APP_SECRET", "").strip()
    if env_secret:
        logging.info("App Secret loaded from the META_APP_SECRET environment variable.")
        return env_secret.encode()

    logging.error(
        "FATAL: no Meta App Secret available (checked %s and META_APP_SECRET). "
        "Refusing to start: unverified webhook events must never be accepted.",
        SECRET_FILE,
    )
    sys.exit(1)


APP_SECRET = _load_app_secret()


def _signature_is_valid(raw_body: bytes, header_value: str) -> bool:
    """Constant-time check of the X-Hub-Signature-256 header."""
    if not header_value:
        return False
    algo, sep, provided = header_value.partition("=")
    if not sep or algo.strip().lower() != "sha256":
        return False
    provided = provided.strip().lower()
    expected = hmac.new(APP_SECRET, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


# ---------------------------------------------------------------------------
# Message classification
# ---------------------------------------------------------------------------
def _normalise(text: str) -> str:
    """Lower-case and drop punctuation, spaces, symbols and emoji.

    Emoji land in Unicode category So, so the symbol filter removes them
    without needing an explicit emoji table.
    """
    out = []
    for char in text.lower():
        if char in _IGNORED_CHARS:
            continue
        category = unicodedata.category(char)
        if category[0] in ("P", "Z", "S", "C"):
            continue
        out.append(char)
    return "".join(out)


def _is_question(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in _ZH_QUESTION_MARKERS):
        return True
    return bool(_EN_QUESTION_RE.search(lowered))


def _classify_message(message):
    """Return (kind, remainder) for a comment message.

    kind is one of:
      question  - carries a question; always answerable
      closing   - pure courtesy / acknowledgement; not answerable
      content   - carries something to respond to
      no_text   - no message field at all (sticker or attachment only)
    """
    if not isinstance(message, str):
        return "no_text", ""

    if _is_question(message):
        return "question", ""

    remainder = _normalise(message)
    for token in _TOKEN_ORDER:
        if token and token in remainder:
            remainder = remainder.replace(token, "")
            # Tokens are length-sorted, but removing one can expose another
            # shorter match, so keep sweeping until the string is stable.
            changed = True
            while changed:
                changed = False
                for again in _TOKEN_ORDER:
                    if again and again in remainder:
                        remainder = remainder.replace(again, "")
                        changed = True

    if remainder:
        return "content", remainder
    return "closing", ""


# ---------------------------------------------------------------------------
# Duplicate suppression
# ---------------------------------------------------------------------------
_seen_events = {}


def _is_duplicate(key: str) -> bool:
    """Return True if this event key was already handled recently.

    In-memory only: the table is lost if the container restarts, which is
    acceptable at the current volume and keeps the service stateless on disk.
    """
    now = time.monotonic()
    for stale in [k for k, expiry in _seen_events.items() if expiry <= now]:
        del _seen_events[stale]

    if key in _seen_events:
        return True
    _seen_events[key] = now + DEDUP_TTL_SECONDS
    return False


# ---------------------------------------------------------------------------
# Thread depth tracking
# ---------------------------------------------------------------------------
_comment_root = {}
_thread_reply_count = {}


def _resolve_root(post_id, comment_id, parent_id):
    """Best-effort thread root for a comment.

    A top-level comment has parent_id == post_id and is its own root.
    Otherwise the root is inherited from the comment being replied to. If
    that parent was never observed (for example it predates this process),
    the parent id itself is used as the root, which keeps distinct threads
    distinct instead of collapsing them together.
    """
    if comment_id is None:
        return None
    if post_id is None or parent_id is None or parent_id == post_id:
        return comment_id
    return _comment_root.get(parent_id, parent_id)


def _record_self_reply(root):
    if root is None:
        return
    _thread_reply_count[root] = _thread_reply_count.get(root, 0) + 1


def _depth_exceeded(root) -> int:
    return _thread_reply_count.get(root, 0)


# ---------------------------------------------------------------------------
# Event evaluation
# ---------------------------------------------------------------------------
def _entry_context(entry: dict) -> str:
    """Compact, non-sensitive summary of an entry.

    The message body is deliberately NOT repeated here, because the full
    payload is already recorded by the META WEBHOOK EVENT line. This summary
    only carries shape and identifiers, so it stays short and greppable.
    """
    parts = []
    changes = entry.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            value = value if isinstance(value, dict) else {}
            parts.append(
                "field=%s item=%s verb=%s comment_id=%s has_message=%s"
                % (
                    change.get("field"),
                    value.get("item"),
                    value.get("verb"),
                    value.get("comment_id"),
                    isinstance(value.get("message"), str),
                )
            )
    return " ".join(parts) if parts else "field=<none>"


def _decide(entry: dict, value: dict, page_name: str) -> str:
    """Return a compact decision string for one change."""
    item = value.get("item")
    verb = value.get("verb")
    comment_id = value.get("comment_id")
    parent_id = value.get("parent_id")
    post_id = value.get("post_id")

    sender = value.get("from")
    sender_id = sender.get("id") if isinstance(sender, dict) else None

    if item != "comment" or verb != "add":
        return "reply=no reason=not_a_new_comment"

    if comment_id is None:
        return "reply=no reason=missing_comment_id"

    root = _resolve_root(post_id, comment_id, parent_id)

    # Rule 1: our own Pages never receive an automated reply. This is the
    # structural guard against replying to ourselves.
    if isinstance(sender_id, str) and sender_id in PAGE_REGISTRY:
        if comment_id:
            _comment_root[comment_id] = root
        _record_self_reply(root)
        sender_name = PAGE_REGISTRY[sender_id]["name"]
        return "reply=no reason=self_page sender=%s" % sender_name

    # Remember the mapping so later replies can find this thread.
    if comment_id:
        _comment_root[comment_id] = root

    # Rule 2: duplicate suppression, keyed on comment id + verb.
    if _is_duplicate("%s:%s" % (comment_id, verb)):
        return "reply=no reason=duplicate"

    # Rule 3: thread depth cap, a backstop for the courtesy loop.
    seen = _depth_exceeded(root)
    if seen >= MAX_REPLY_DEPTH:
        return "reply=no reason=depth_limit depth=%d max=%d" % (seen, MAX_REPLY_DEPTH)

    # Rule 4: content filter. A question always wins.
    kind, _ = _classify_message(value.get("message"))
    if kind == "closing":
        return "reply=no reason=closing_phrase"
    if kind == "question":
        return "reply=yes reason=question"
    if kind == "no_text":
        return "reply=yes reason=no_text"
    return "reply=yes reason=content"


def _emit_spool(entry: dict, value: dict, page_id, meta, decision: str) -> None:
    """Append one normalised event to the spool file.

    This is the only hand-off this service makes to anything downstream. It is
    a local file append: no network, no client library, no downstream
    credential. The AI worker reads the file -- it never calls this service,
    and this service never calls it.

    A spool failure must never break webhook processing, so everything here is
    best-effort and swallowed into a warning.
    """
    try:
        fields = {}
        for token in decision.split():
            if "=" in token:
                key, _, val = token.partition("=")
                fields[key] = val

        sender = value.get("from")
        sender = sender if isinstance(sender, dict) else {}
        post = value.get("post")
        post = post if isinstance(post, dict) else {}

        record = {
            "schema": SPOOL_SCHEMA,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "page": {
                "id": page_id,
                "name": meta["name"] if meta else None,
                "kb": meta["kb"] if meta else None,
                "mapped": meta is not None,
            },
            "comment": {
                "id": value.get("comment_id"),
                "parent_id": value.get("parent_id"),
                "post_id": value.get("post_id"),
                "message": value.get("message"),
                "created_time": value.get("created_time"),
                "permalink_url": value.get("permalink_url"),
                "item": value.get("item"),
                "verb": value.get("verb"),
            },
            "author": {"id": sender.get("id"), "name": sender.get("name")},
            "post": {
                "id": post.get("id"),
                "status_type": post.get("status_type"),
                "permalink_url": post.get("permalink_url"),
                "is_published": post.get("is_published"),
            },
            "decision": {
                "reply": fields.get("reply") == "yes",
                "reason": fields.get("reason"),
                "detail": decision,
            },
        }

        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"

        directory = os.path.dirname(SPOOL_FILE)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(SPOOL_FILE) and os.path.getsize(SPOOL_FILE) >= SPOOL_MAX_BYTES:
            os.replace(SPOOL_FILE, SPOOL_FILE + ".1")
        with open(SPOOL_FILE, "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception as exc:  # noqa: BLE001 - spool trouble must not break the webhook
        logging.warning("SPOOL write failed (%s): %s", type(exc).__name__, exc)


def _emit_lead_spool(entry: dict, value: dict, page_id, meta, page_name: str) -> None:
    """Append one normalised lead (Instant Form) event to the same spool file.

    Same best-effort contract as _emit_spool: a spool failure must never break
    webhook processing.

    Deliberately does NOT fetch the lead itself. Resolving field_data requires
    a Page Access Token, and this service is designed to hold no credentials
    and make no outbound calls. Downstream reads the spool and does the lookup.
    """
    try:
        def pick(key):
            raw = value.get(key)
            return raw if isinstance(raw, (str, int, float)) else None

        record = {
            "schema": LEAD_SPOOL_SCHEMA,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "page": {
                "id": page_id,
                "name": meta["name"] if meta else None,
                "kb": meta["kb"] if meta else None,
                "mapped": meta is not None,
            },
            "lead": {
                "leadgen_id": pick("leadgen_id"),
                "form_id": pick("form_id"),
                "created_time": pick("created_time"),
                # 广告实体 ID：第三方投放时本主页无权读取，Meta 会静默省略。
                # 这里如实记录「有就是有、没有就是 None」，由下游写成「无法获取」。
                "ad_id": pick("ad_id"),
                "adgroup_id": pick("adgroup_id"),
                "page_id": pick("page_id"),
            },
            "entry_time": entry.get("time"),
        }

        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"

        directory = os.path.dirname(SPOOL_FILE)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(SPOOL_FILE) and os.path.getsize(SPOOL_FILE) >= SPOOL_MAX_BYTES:
            os.replace(SPOOL_FILE, SPOOL_FILE + ".1")
        with open(SPOOL_FILE, "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception as exc:  # noqa: BLE001
        logging.warning("LEAD SPOOL write failed (%s): %s", type(exc).__name__, exc)


def _tag_entries(body) -> None:
    """Emit one routing line and one decision line per entry."""
    entries = body.get("entry") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        logging.warning(
            "PAGE_ROUTE malformed payload: entry missing or not a list; "
            "no routing tag emitted."
        )
        return

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            logging.warning(
                "PAGE_ROUTE malformed entry[%d]: not an object; skipped.", index
            )
            continue

        page_id = entry.get("id")
        meta = PAGE_REGISTRY.get(page_id) if isinstance(page_id, str) else None

        if meta is None:
            logging.warning(
                "PAGE_ROUTE UNMAPPED page_id=%s name=<unmapped> kb=<none> "
                "index=%d %s -- accepted and recorded but not routed",
                page_id,
                index,
                _entry_context(entry),
            )
        else:
            logging.info(
                "PAGE_ROUTE page_id=%s name=%s kb=%s index=%d %s",
                page_id,
                meta["name"],
                meta["kb"],
                index,
                _entry_context(entry),
            )

        page_name = meta["name"] if meta else "<unmapped>"
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            if change.get("field") == "leadgen":
                # 线索（Instant Form）走独立分支：它不是评论，
                # 不该进回复资格判定，也永远不应该被自动回复。
                logging.info(
                    "LEAD_ROUTE page_id=%s name=%s leadgen_id=%s form_id=%s",
                    page_id, page_name, value.get("leadgen_id"), value.get("form_id"))
                _emit_lead_spool(entry, value, page_id, meta, page_name)
                continue
            decision = _decide(entry, value, page_name)
            logging.info(
                "DECISION page_id=%s name=%s comment_id=%s %s",
                page_id,
                page_name,
                value.get("comment_id"),
                decision,
            )
            _emit_spool(entry, value, page_id, meta, decision)


@app.get("/health")
async def health():
    return {"status": "ok", "pages_configured": len(PAGE_REGISTRY)}


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params

    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    logging.info(
        "Webhook verification request: mode=%s token_match=%s",
        mode,
        token == VERIFY_TOKEN,
    )

    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return Response(content=challenge, media_type="text/plain")

    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request):
    raw_body = await request.body()

    if not _signature_is_valid(raw_body, request.headers.get(SIGNATURE_HEADER, "")):
        logging.warning(
            "Webhook signature verification FAILED; request rejected "
            "(body intentionally not logged). bytes=%d",
            len(raw_body),
        )
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        body = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        logging.warning("Webhook body was not valid JSON (signature was valid); rejected.")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logging.info("META WEBHOOK EVENT: %s", body)
    _tag_entries(body)

    return {"status": "EVENT_RECEIVED"}
