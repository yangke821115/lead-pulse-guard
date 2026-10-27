"""Email monitoring: read-only IMAP polling that pushes new customer emails as Lark notification cards.

Design tradeoffs (each one the result of a hard-learned lesson):
- Read-only: always use BODY.PEEK, never mark read, move, or delete — never change mailbox state
- First run baselines on the current max UID, no backfilling of history (otherwise thousands of emails flood in on launch)
- **Read both INBOX and Sent**: looking only at incoming mail leads to the wrong conclusion of "nobody handled it" —
  the authoritative source for "has the customer been replied to" is Sent, not INBOX
- Dedupe double insurance: UID watermark + Message-ID set, persisted across restarts
- **Delivery confirmation**: write each email's result to a state file, avoiding "thought it was pushed but it wasn't"
- When classification is uncertain, always judge as "not spam": missing a real customer costs far more than an extra card
- Failures must be loud: push an alert card when a single account exceeds the threshold; exit to let systemd restart when all accounts fail long-term
"""
import email
import email.utils
import html
import imaplib
import json
import logging
import os
import re
import ssl
import sys
import time
from email.header import decode_header

from . import config, feishu, i18n, llm

log = logging.getLogger("mail")


def _state_dir():
    raw = (os.environ.get("AIW_MAIL_STATE")
           or os.environ.get("STATE_DIRECTORY")
           or os.path.join(config.STATE_DIR, "mail"))
    return raw.split(":")[0]


STATE_DIR = _state_dir()
STATE_FILE = os.path.join(STATE_DIR, "state.json")
FILTERED_LOG = os.path.join(STATE_DIR, "filtered.jsonl")

IMAP_HOST = os.environ.get("AIW_MAIL_IMAP_HOST", "")
IMAP_PORT = int(os.environ.get("AIW_MAIL_IMAP_PORT", "993"))
ACCOUNTS_FILE = os.environ.get(
    "AIW_MAIL_ACCOUNTS", os.path.join(config.SECRETS, "mail-accounts.json"))
POLL_SECONDS = int(os.environ.get("AIW_MAIL_POLL", "180"))
MAX_FETCH = int(os.environ.get("AIW_MAIL_MAX_FETCH", "20"))
BODY_CHARS = int(os.environ.get("AIW_MAIL_BODY_CHARS", "2500"))
CARD_BODY_CHARS = int(os.environ.get("AIW_MAIL_CARD_BODY", "700"))
SEEN_KEEP = 500
GLOBAL_SEEN_KEEP = 2000
PROCESSED_KEEP = 200
SENT_KEEP = 300
ALERT_AFTER = int(os.environ.get("AIW_MAIL_ALERT_AFTER", "10"))
EXIT_AFTER = int(os.environ.get("AIW_MAIL_EXIT_AFTER", "20"))
MAX_SEND_RETRY = int(os.environ.get("AIW_MAIL_SEND_RETRY", "3"))
ALERT_COOLDOWN = 6 * 3600


# ---------------- State ----------------

def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("State file corrupted, treating as empty state (baseline will be redone, no duplicate pushes): %s", exc)
        return {}


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


GLOBAL_SEEN_KEY = "_global_seen"


def _global_seen(state):
    """Cross-account dedupe table: if the same email arrives at multiple monitored mailboxes, push only one card."""
    return set(state.get(GLOBAL_SEEN_KEY) or [])


def _global_seen_add(state, key):
    g = _global_seen(state)
    g.add(key)
    if len(g) > GLOBAL_SEEN_KEEP:
        g = set(list(g)[-GLOBAL_SEEN_KEEP:])
    state[GLOBAL_SEEN_KEY] = sorted(g)


def _push_rec(bucket, key, rec, limit):
    """Append a record to a bounded list (used for both delivery confirmation and sent history)."""
    lst = bucket.setdefault(key, [])
    lst.append(rec)
    if len(lst) > limit:
        del lst[:-limit]


def load_accounts():
    with open(ACCOUNTS_FILE, encoding="utf-8") as handle:
        cfg = json.load(handle)
    out = []
    for acct in cfg.get("accounts") or []:
        password = config.read_secret(acct["password_file"])
        if not password:
            raise RuntimeError("Password file is empty: %s" % acct["password_file"])
        out.append({
            "address": acct["address"],
            "password": password,
            "page_id": str(acct.get("page_id") or ""),
            "label": acct.get("label") or acct["address"],
        })
    # ── 有账号配置但没设 IMAP 主机 → 显式失败 ──────────────────────
    # 以前默认值写死成某家服务商，等于「没配置就静默连别人家的服务器」。
    # 现在宁可启动失败，也不猜。
    if out and not IMAP_HOST.strip():
        raise RuntimeError(
            "AIW_MAIL_IMAP_HOST is empty but %d mail account(s) are configured in %s. "
            "Set it to your provider's IMAP host (e.g. imap.gmail.com), "
            "or remove that file to disable mail monitoring."
            % (len(out), ACCOUNTS_FILE))
    return out


# ---------------- Email parsing ----------------

def _decode(value):
    if not value:
        return ""
    parts = []
    for chunk, enc in decode_header(value):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _strip_html(text):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", html.unescape(text))


_QUOTE_MARKERS = (
    re.compile(r"^\s*On .{5,80}\bwrote:", re.I),
    # Bilingual: match both English and Chinese mail-client headers.
    re.compile(r"^\s*-{2,}\s*(原始邮件|Original Message)\s*-{2,}", re.I),
    re.compile(r"^\s*(发件人|From)\s*[:：]"),
    re.compile(r"^\s*_{10,}\s*$"),
)


def _trim_quotes(text):
    """Strip quoted reply content, keep only the new content — otherwise the noise drowns the model and the card."""
    kept = []
    for line in (text or "").splitlines():
        if any(p.search(line) for p in _QUOTE_MARKERS):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    out = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _body_of(msg):
    plain = None
    htmls = []
    if msg.is_multipart():
        for part in msg.walk():
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = part.get_content_type()
            raw = part.get_payload(decode=True)
            if not raw:
                continue
            charset = part.get_content_charset() or "utf-8"
            if ctype == "text/plain" and plain is None:
                plain = raw.decode(charset, errors="replace")
            elif ctype == "text/html":
                htmls.append(raw.decode(charset, errors="replace"))
    else:
        raw = msg.get_payload(decode=True)
        if raw:
            charset = msg.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                htmls.append(text)
            else:
                plain = text
    text = plain if plain is not None else _strip_html("\n".join(htmls))
    return _trim_quotes(text)


def _local_time(value):
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except Exception:
        return "(time unknown)"
    if dt is None:
        return "(time unknown)"
    import datetime as _dt
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(
            _dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)[:16]


def _parse(raw, uid):
    msg = email.message_from_bytes(raw)
    pair = email.utils.parseaddr(_decode(msg.get("From")))
    from_name, from_addr = (pair[0], pair[1]) if pair[1] else ("", "")
    to_addrs = [a.lower() for _, a in
                email.utils.getaddresses([_decode(v) for v in
                 msg.get_all("To") or []]) if a]
    return {
        "uid": uid,
        "message_id": (msg.get("Message-ID") or "").strip(),
        "subject": _decode(msg.get("Subject")).strip(),
        "from_name": from_name.strip(),
        "from_addr": from_addr.strip().lower(),
        "to_addrs": to_addrs,
        "local_time": _local_time(msg.get("Date")),
        "body": _body_of(msg)[:BODY_CHARS],
    }


# ---------------- Internal mail interception ----------------

# Own domains: when the sender belongs to these domains, treat it as "mail we sent ourselves" and don't push a card.
# Deliberately not derived from the account table — prevents the scope from silently widening if externally managed accounts are added later.
# Configuration: AIW_MAIL_OWN_DOMAINS="example.com,example.org"
OWN_DOMAINS = tuple(d.strip().lower()
                    for d in os.environ.get("AIW_MAIL_OWN_DOMAINS", "").split(",")
                    if d.strip())


def _domain_of(addr):
    if not addr or "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1].strip().lower()


def is_internal_mail(mail):
    """Internal mail = sender belongs to an own domain, i.e. "this message was sent by us".

    2026-10-13 revision: the original rule required sender and recipient to both
    belong to an own domain, so mail we sent to customers (To is the customer, we're
    only on Cc / Bcc) was classified as external incoming mail and pushed a card to
    the Lark chat. Changed to look only at the sender — our sales mailbox
    won't receive leads from its own colleagues.

    This is a deterministic rule, not left to the LLM to guess — internal mail shouldn't
    consume model calls, nor carry the risk of "the model got it wrong this time".
    """
    frm = _domain_of(mail.get("from_addr"))
    if frm in OWN_DOMAINS:
        return True, "Internal mail (sent by us: %s)" % (mail.get("from_addr") or "")
    return False, ""


# ---------------- Triage ----------------

CLASSIFY_SYSTEM = """You are the email triage assistant for an export company. A message arrives in a sales mailbox. Decide:

1) is_spam: is this a non-customer message? Only these count as spam:
   B2B cold outreach (lead generation, SEO, website building, agency services, email lists),
   phishing and scams, system notifications, ads, subscription/unsubscribe notices.
   ⚠️ Real customer inquiries, technical questions, after-sales, complaints, partnership requests,
   and distributor applications are NOT spam. When unsure, return false — missing a real customer
   is far worse than one extra notification.

2) domain: topic; must be one of:
@@DOMAIN_GUIDE@@

3) intent: one of "quote" / "technical" / "after_sales" / "partnership" / "other"
4) urgency: one of "high" (clear purchase intent or deadline) / "medium" / "low"
5) summary: one-sentence summary in English, under 60 words, describing only the message itself; do not invent.

Even if judged spam, still fill domain/intent/summary truthfully.

Return JSON only — no explanation, no markdown fences:
{"is_spam": false, "domain": "@@DOMAIN_ONE@@", "intent": "quote", "urgency": "high", "summary": "..."}"""


def _classify_system():
    """Fill the topic enum and guide into the prompt.

    Uses the same placeholder convention as llm.py: the topic list comes from
    AIW_DOMAIN_ENUM / AIW_DOMAIN_GUIDE, so switching business only requires
    changing environment variables, not code.
    """
    domains = [d.strip() for d in config.DOMAIN_ENUM.split(",") if d.strip()]
    guide = config.DOMAIN_GUIDE.strip() or "\n".join(
        '   "%s"' % d for d in domains)
    return (CLASSIFY_SYSTEM
            .replace("@@DOMAIN_GUIDE@@", guide)
            .replace("@@DOMAIN_ONE@@", domains[0] if domains else "other"))


def _llm_json(system, user, max_tokens=700, temperature=0.2):
    plan = []
    for _ in range(config.FREE_ATTEMPTS):
        plan.append((config.FREE_LLM_MODEL, config.FREELLM_URL,
                     "freellmapi-api-key", config.FREE_TIMEOUT))
    plan.append((config.FALLBACK_MODEL, config.DEEPSEEK_URL,
                 "deepseek-api-key", config.LLM_TIMEOUT))
    errors = []
    for model, url, keyname, timeout in plan:
        try:
            body = llm._post({
                "model": model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }, url, keyname, timeout)
            choice = body["choices"][0]
            content = choice["message"]["content"] or ""
            if choice.get("finish_reason") == "length":
                raise RuntimeError("Output truncated by max_tokens")
            if not content.strip():
                raise RuntimeError("Returned empty content")
            text = content.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            return json.loads(text)
        except Exception as exc:
            errors.append("%s: %s" % (model, exc))
    raise RuntimeError("All models failed:" + " | ".join(errors))


def classify(mail):
    user = "From: %s <%s>\nSubject: %s\nBody:\n%s" % (
        mail["from_name"] or "(no name)", mail["from_addr"],
        mail["subject"] or "(no subject)", mail["body"] or "(no body)")
    out = _llm_json(CLASSIFY_SYSTEM, user)
    out.setdefault("is_spam", False)          # When fields are missing, lean toward "not spam"
    out.setdefault("domain", "other")
    out.setdefault("intent", "other")
    out.setdefault("urgency", "medium")
    out.setdefault("summary", "")
    return out


# ---------------- Routing ----------------

def chat_for_domain(domain, page_id):
    """Content topic takes precedence over recipient address — a lead may be sent to another business line's mailbox."""
    cfg = feishu.load_routing()
    routes = cfg.get("routes") or {}
    pid = str(page_id or "")
    own = routes.get(pid) or {}
    if domain and domain in own:
        return own[domain], "own-domain"
    if domain:
        hits = {r[domain] for r in routes.values()
                if isinstance(r, dict) and domain in r}
        if len(hits) == 1:
            return hits.pop(), "global-domain"
    if "*" in own:
        return own["*"], "page-wildcard"
    d = (cfg.get("defaults") or {}).get(pid)
    if d:
        return d, "page-default"
    return feishu.chat_id(), "single-chat-fallback"


# ---------------- Card ----------------

_PALETTE = ("blue", "turquoise", "purple", "orange", "green", "wathet")
_DOMAINS = [d.strip() for d in config.DOMAIN_ENUM.split(",") if d.strip()]
_DOMAIN_COLOR = {d: _PALETTE[i % len(_PALETTE)] for i, d in enumerate(_DOMAINS)}


def sent_history(st, addr):
    """Look in this account's "Sent" records for whether we've replied to this address."""
    addr = (addr or "").lower()
    if not addr or not st:
        return []
    return [r for r in (st.get("sent") or [])
            if addr in (r.get("to") or "").lower()]


def build_mail_card(acct, mail, triage, st=None, test=False):
    domain = triage.get("domain") or "other"
    title = "📧 New customer email · %s" % domain
    if test:
        title = "🧪 Test · " + title
    lines = ["**Mailbox:** %s" % acct["label"]]
    lines.append("**From:** %s" % (mail["from_name"] or "(no name)"))
    lines.append("**Email:** `%s`" % mail["from_addr"])
    lines.append("**Time:** %s (Beijing time)" % mail["local_time"])
    if mail["subject"]:
        lines.append("**Subject:** %s" % mail["subject"])
    body = (mail["body"] or "(no body)")[:CARD_BODY_CHARS]
    lines.append("**Body:**\n> %s" % body.replace("\n", "\n> "))
    lines.append("---")
    lines.append("**AI triage:** topic=%s　intent=%s　urgency=%s" % (
        domain, i18n.label("intent", triage.get("intent")), i18n.label("priority", triage.get("urgency"))))
    if triage.get("summary"):
        lines.append(triage["summary"])
    hist = sent_history(st, mail["from_addr"])
    if hist:
        lines.append("**History:** found this address **%d** times in Sent, last %s"
                     % (len(hist), hist[-1].get("ts") or "?"))
    else:
        lines.append("**History:** no reply to this address in Sent (likely first contact)")
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title},
                   "template": _DOMAIN_COLOR.get(domain, "grey")},
        "body": {"elements": [feishu._md(x) for x in lines]},
    }


def _log_filtered(acct, mail, triage, reason):
    """Filtered mail must leave a trace — misclassifications need to be recoverable, not vanish into thin air."""
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
           "account": acct["address"], "label": acct["label"],
           "from": "%s <%s>" % (mail["from_name"], mail["from_addr"]),
           "subject": mail["subject"], "reason": reason,
           "triage": triage, "preview": mail["body"][:200]}
    try:
        with open(FILTERED_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("Failed to write filter log: %s", exc)


# ---------------- IMAP ----------------

def _connect(acct):
    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx, timeout=30)
    M.login(acct["address"], acct["password"])
    return M


def _find_sent_folder(M):
    """Folders with spaces in the name must be quoted in imaplib, otherwise BAD EXAMINE parameters!"""
    typ, boxes = M.list()
    for box in boxes or []:
        raw = box.decode(errors="replace")
        m = re.search(r'"([^"]*)"\s*$', raw)
        name = m.group(1) if m else raw.split()[-1]
        if re.search(r"sent|已发送", name, re.I):
            return name
    return None


def _fetch_raw(M, uid):
    typ, d = M.uid("FETCH", str(uid), "(BODY.PEEK[])")
    for item in d or []:
        if isinstance(item, tuple) and len(item) > 1:
            return item[1]
    return None


def _uids(M):
    typ, data = M.uid("SEARCH", None, "ALL")
    return [int(x) for x in (data[0].split() if data and data[0] else [])]


def _poll_inbox(acct, state, opts):
    addr = acct["address"]
    st = state.setdefault(addr, {})
    M = _connect(acct)
    new_count = 0
    try:
        M.select("INBOX", readonly=True)
        uids = _uids(M)
        top = max(uids) if uids else 0

        if "last_uid" not in st and not opts.get("backfill"):
            st["last_uid"] = top
            st.setdefault("seen", [])
            _save_state(state)
            log.info("[%s] Inbox first run, baseline UID=%s, not pushing historical emails", addr, top)
            return 0

        floor = 0 if opts.get("backfill") else int(st.get("last_uid") or 0)
        pending = [u for u in uids if u > floor]
        if len(pending) > MAX_FETCH:
            log.warning("[%s] %d pending, this round only takes the earliest %d (avalanche prevention)",
                        addr, len(pending), MAX_FETCH)
            pending = pending[:MAX_FETCH]

        seen = set(st.get("seen") or [])
        gseen = _global_seen(state)
        retry = st.setdefault("retry", {})
        for uid in pending:
            raw = _fetch_raw(M, uid)
            if not raw:
                log.warning("[%s] UID %s content unavailable, skipping", addr, uid)
                st["last_uid"] = uid
                _save_state(state)
                continue

            mail = _parse(raw, uid)
            key = mail["message_id"] or ("uid:%s@%s" % (uid, addr))
            if key in seen or key in gseen:
                where = "this account" if key in seen else "other accounts"
                log.info("[%s] UID %s already processed in %s (%s), skipping",
                         addr, uid, where, key[:40])
                st["last_uid"] = uid
                _save_state(state)
                continue

            internal, why = is_internal_mail(mail)
            if internal:
                _log_filtered(acct, mail, {"is_spam": False,
                                           "filter": "internal"}, why)
                _push_rec(st, "processed", {
                    "uid": uid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": mail["from_addr"], "subject": mail["subject"],
                    "outcome": "filtered", "reason": "internal"}, PROCESSED_KEEP)
                log.info("[%s] UID %s %s, logging only without pushing card: %s",
                         addr, uid, why, (mail["subject"] or "")[:60])
                seen.add(key)
                _global_seen_add(state, key)
                st["last_uid"] = uid
                st["seen"] = list(seen)[-SEEN_KEEP:]
                retry.pop(str(uid), None)
                _save_state(state)
                continue

            triage = classify(mail)
            done = True
            if triage.get("is_spam"):
                _log_filtered(acct, mail, triage, "is_spam")
                _push_rec(st, "processed", {
                    "uid": uid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": mail["from_addr"], "subject": mail["subject"],
                    "outcome": "filtered", "reason": "is_spam",
                    "triage": triage}, PROCESSED_KEEP)
                log.info("[%s] UID %s classified as promotion/spam, logging only without pushing card: %s",
                         addr, uid, (mail["subject"] or mail["from_addr"])[:60])
            else:
                to, how = chat_for_domain(triage.get("domain"), acct["page_id"])
                card = build_mail_card(acct, mail, triage, st)
                if opts.get("dry_run"):
                    log.info("[%s] DRY-RUN would have pushed: %s", addr,
                             json.dumps(card, ensure_ascii=False)[:400])
                    _push_rec(st, "processed", {
                        "uid": uid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "from": mail["from_addr"], "subject": mail["subject"],
                        "outcome": "dry_run", "chat": to,
                        "triage": triage}, PROCESSED_KEEP)
                else:
                    try:
                        mid = feishu.send_card(card, to=to)
                        _push_rec(st, "processed", {
                            "uid": uid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "from": mail["from_addr"], "subject": mail["subject"],
                            "outcome": "delivered", "chat": to, "how": how,
                            "message_id": mid, "triage": triage}, PROCESSED_KEEP)
                        log.info("[%s] Pushed incoming mail: %s | %s | group=%s(%s) | message_id=%s",
                                 addr, mail["from_addr"],
                                 triage.get("summary") or mail["subject"],
                                 to[:12], how, mid)
                        new_count += 1
                    except Exception as exc:
                        attempts = int(retry.get(str(uid), 0)) + 1
                        retry[str(uid)] = attempts
                        give_up = attempts >= MAX_SEND_RETRY
                        _push_rec(st, "processed", {
                            "uid": uid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "from": mail["from_addr"], "subject": mail["subject"],
                            "outcome": "abandoned" if give_up else "send_failed",
                            "attempts": attempts, "chat": to,
                            "error": str(exc)[:200], "triage": triage},
                            PROCESSED_KEEP)
                        log.error("[%s] UID %s push failed (attempt %d): %s",
                                  addr, uid, attempts, exc)
                        if give_up:
                            log.error("[%s] UID %s failed %d times in a row, giving up retries."
                                      "This incoming mail was recorded in processed and needs manual review: %s | %s",
                                      addr, uid, attempts, mail["from_addr"],
                                      mail["subject"])
                        else:
                            done = False   # Don't advance the watermark; retry next round

            if done:
                seen.add(key)
                _global_seen_add(state, key)
                st["last_uid"] = uid
                st["seen"] = list(seen)[-SEEN_KEEP:]
                retry.pop(str(uid), None)
                _save_state(state)
            else:
                _save_state(state)
                break          # Stop at the failed message so later progress doesn't move the watermark past it
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return new_count


def _poll_sent(acct, state, opts):
    """Read "Sent" to record who we've replied to — looking only at INBOX would wrongly flag "nobody handled it"."""
    addr = acct["address"]
    st = state.setdefault(addr, {})
    M = _connect(acct)
    recorded = 0
    try:
        folder = _find_sent_folder(M)
        if not folder:
            log.warning("[%s] Sent folder not found, skipping", addr)
            return 0
        typ, data = M.select('"%s"' % folder, readonly=True)
        if typ != "OK":
            log.warning("[%s] Failed to open Sent folder: %s", addr, data)
            return 0
        uids = _uids(M)
        top = max(uids) if uids else 0
        if "sent_uid" not in st:
            pending = uids[-MAX_FETCH:]
            log.info("[%s] Sent first run: reading back the last %d messages to build correspondence history", addr, len(pending))
        else:
            pending = [u for u in uids if u > int(st.get("sent_uid") or 0)][:MAX_FETCH]
        for uid in pending:
            raw = _fetch_raw(M, uid)
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            _push_rec(st, "sent", {
                "uid": uid,
                "ts": _local_time(msg.get("Date")),
                "to": re.sub(r"\s+", " ", _decode(msg.get("To"))).strip()[:200],
                "cc": re.sub(r"\s+", " ", _decode(msg.get("Cc"))).strip()[:200],
                "subject": _decode(msg.get("Subject")).strip()[:120],
                "in_reply_to": (msg.get("In-Reply-To") or "").strip()[:120],
            }, SENT_KEEP)
            recorded += 1
        st["sent_uid"] = top
        _save_state(state)
        if recorded:
            log.info("[%s] Sent record update %d emails (total %d)",
                     addr, recorded, len(st.get("sent") or []))
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return recorded


def _poll_account(acct, state, opts):
    new_count = _poll_inbox(acct, state, opts)
    try:
        _poll_sent(acct, state, opts)
    except Exception as exc:
        # A failed Sent read shouldn't take down incoming-mail push — log it but don't count it as a polling failure
        log.error("[%s] Failed to read sent records (does not affect incoming mail push): %s", acct["address"], exc)
    return new_count


def poll_all(accounts, state, opts, counters):
    for acct in accounts:
        addr = acct["address"]
        try:
            _poll_account(acct, state, opts)
            counters[addr] = {"fails": 0,
                              "alert_at": counters.get(addr, {}).get("alert_at", 0)}
        except Exception as exc:
            rec = counters.setdefault(addr, {"fails": 0, "alert_at": 0})
            rec["fails"] = rec.get("fails", 0) + 1
            log.error("[%s] Polling failed (%d consecutive times): %s", addr, rec["fails"], exc)
            now = time.time()
            if rec["fails"] >= ALERT_AFTER and now - rec.get("alert_at", 0) > ALERT_COOLDOWN:
                rec["alert_at"] = now
                try:
                    feishu.send_card(_alert_card(acct, exc, rec["fails"]))
                    log.info("[%s] Pushed monitoring alert card", addr)
                except Exception as inner:
                    log.error("[%s] Alert card also failed to send: %s", addr, inner)
    # All accounts failing long-term -> the service is meaningless; exit and let systemd restart it
    if accounts and all(counters.get(a["address"], {}).get("fails", 0) >= EXIT_AFTER
                        for a in accounts):
        log.error("All accounts failed consecutively ≥ %d times, exiting to let systemd restart", EXIT_AFTER)
        sys.exit(1)


def _alert_card(acct, exc, fails):
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text",
                             "content": "🔴 Mailbox monitor error · %s" % acct["label"]},
                   "template": "red"},
        "body": {"elements": [
            feishu._md("**Mailbox:** `%s`" % acct["address"]),
            feishu._md("**Consecutive failures:** %d" % fails),
            feishu._md("**Error:**\n> %s" % str(exc)[:400]),
            feishu._md("New customer email may be missed. Common causes: "
                       "password changed, IMAP disabled, account restricted."),
        ]},
    }


# ---------------- Entry point ----------------

def _sample():
    return {
        "uid": 0, "message_id": "<sample@local>",
        "subject": "Inquiry: pricing and lead time",
        "from_name": "Test sender (not a real customer)",
        "from_addr": "sample@example.com", "local_time": "2026-09-22 18:00",
        "body": "Hello,\n\nWe are looking for the product listed on your website. "
                "Could you please quote your price and delivery time?\n\n"
                "Best regards",
    }


def _summary(state):
    out = []
    for addr, st in sorted(state.items()):
        if addr == GLOBAL_SEEN_KEY:
            continue
        proc = st.get("processed") or []
        by = {}
        for r in proc:
            by[r.get("outcome")] = by.get(r.get("outcome"), 0) + 1
        out.append({
            "account": addr,
            "inbox_last_uid": st.get("last_uid"),
            "sent_last_uid": st.get("sent_uid"),
            "sent_records": len(st.get("sent") or []),
            "processed": by,
            "last_3": proc[-3:],
        })
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    opts = {
        "once": "--once" in argv,
        "dry_run": "--dry-run" in argv,
        "backfill": "--backfill" in argv,
    }
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout)

    accounts = load_accounts()
    log.info("Mail monitoring started: %d accounts (%s), polling interval %d seconds, state directory %s",
             len(accounts), "、".join(a["label"] for a in accounts),
             POLL_SECONDS, STATE_DIR)

    if "--test-card" in argv:
        acct = accounts[0]
        demo_domain = _DOMAINS[0] if _DOMAINS else "other"
        triage = {"domain": demo_domain, "intent": "quote", "urgency": "high",
                  "summary": "Customer asks about price and lead time (test card)"}
        to, how = chat_for_domain(demo_domain, acct["page_id"])
        mid = feishu.send_card(
            build_mail_card(acct, _sample(), triage, None, test=True), to=to)
        log.info("Test card sent: group=%s(%s) message_id=%s", to[:12], how, mid)
        return 0

    if "--status" in argv:
        print(json.dumps(_summary(_load_state()), ensure_ascii=False, indent=2))
        return 0

    state = _load_state()
    counters = {}
    while True:
        begin = time.time()
        try:
            poll_all(accounts, state, opts, counters)
        except SystemExit:
            raise
        except Exception as exc:
            log.error("Exception in this round: %s", exc)
        if opts["once"]:
            break
        time.sleep(max(5.0, POLL_SECONDS - (time.time() - begin)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
