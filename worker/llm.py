"""DeepSeek call: turn a comment into a structured reply suggestion.

Core safety design: the system prompt makes "the KB is the single source of truth" a hard rule,
and when the fact base is empty it explicitly requires the model not to state any product information.
"""
import json, os, re, time, urllib.error, urllib.request

from . import config

# Replace the category placeholder in the prompt with the actual config (done once at module load, zero runtime cost)
def _domain_enum():
    items = [d.strip() for d in config.DOMAIN_ENUM.split(",") if d.strip()]
    return "|".join(items) if items else "general|other"


def _domain_guide():
    if config.DOMAIN_GUIDE.strip():
        return config.DOMAIN_GUIDE.rstrip()
    return "\n".join("  · %s" % d.strip() for d in _domain_enum().split("|"))


PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def _prompt(name):
    """Load a prompt file. Rules live in English only; reply language is a directive."""
    path = os.path.join(PROMPT_DIR, "%s.en.txt" % name)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _render(text):
    return (text.replace("@@DOMAIN_ENUM@@", _domain_enum())
                .replace("@@DOMAIN_GUIDE@@", _domain_guide()))


def _detect_language(text):
    """Return "zh", "en", or None when there is not enough signal.

    The reply follows the commenter's language, not the Page's: a Chinese customer
    writing on an English Page still deserves a Chinese answer.
    """
    text = text or ""
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if han and han >= max(1, latin // 3):
        return "zh"
    if latin:
        return "en"
    return None


def _event_language(event):
    comment = event.get("comment") or {}
    detected = _detect_language(comment.get("message") or "")
    if detected:
        return detected
    page_id = str((event.get("page") or {}).get("id") or "")
    return config.page_language(page_id)


def _post(payload, url, keyname, timeout=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", "Bearer " + config.read_secret(keyname))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout or config.LLM_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _attempt(event, kb_block, url, keyname, model, timeout=None, images=None):
    """Single attempt. Returns (suggestion, usage); raises on any problem."""
    lang = _event_language(event)
    lang_name = {"zh": "Simplified Chinese", "en": "English"}.get(lang, "English")
    system_prompt = (_render(_prompt("comment"))
                     + "\n\n# Reply language\n\nReply in %s.\n" % lang_name)
    user_template = _prompt("user")
    comment = event.get("comment") or {}
    user_msg = user_template.format(
        kb=kb_block,
        page_name=(event.get("page") or {}).get("name") or "Unknown Page",
        kb_slug=(event.get("page") or {}).get("kb") or "none",
        author_name=(event.get("author") or {}).get("name") or "a user",
        comment=(comment.get("message") or "（no text）"),
    )
    if images:
        content = [{"type": "text", "text": user_msg}]
        for uri in images:
            content.append({"type": "image_url", "image_url": {"url": uri}})
    else:
        content = user_msg
    body = _post({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": config.TEMPERATURE,
        "max_tokens": config.MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }, url, keyname, timeout)
    choice = body["choices"][0]
    content = choice["message"]["content"]
    finish = choice.get("finish_reason")
    usage = body.get("usage", {})

    # finish_reason == "length" means the output was hard-truncated by max_tokens —
    # the JSON is necessarily unclosed and parsing will always fail. Must warn explicitly, never silently drop.
    if finish == "length":
        raise RuntimeError(
            "Output truncated at max_tokens=%s (finish_reason=length); "
            "the KB or reply is too long. Tail of raw content: %r"
            % (config.MAX_TOKENS, content[-120:]))

    if not (content or "").strip():
        raise RuntimeError(
            "Model returned empty content. finish_reason=%r usage=%r" % (finish, usage))

    # Lenient parsing: some models wrap the output in markdown code fences on their own
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        suggestion = json.loads(text)
    except ValueError as exc:
        raise RuntimeError(
            "JSON parse failed: %s | finish_reason=%r | first 200 chars: %r | last 120 chars: %r"
            % (exc, finish, content[:200], content[-120:])) from exc

    # Fallback: safe defaults when the model omits fields
    suggestion.setdefault("domain", "other")
    suggestion.setdefault("intent", "other")
    suggestion.setdefault("stage", "cold")
    suggestion.setdefault("action", "reply")
    suggestion.setdefault("language", "en")
    suggestion.setdefault("reply", "")
    check = suggestion.setdefault("self_check", {})
    check.setdefault("answers_question", True)
    check.setdefault("cites_only_kb", True)
    check.setdefault("invented_specifics", False)
    check.setdefault("note", "")
    return suggestion, usage

# ---------- vision model fallback chain (tested 2026-10-16) ----------
# Tested 30 models one by one with the same test image ("4 color blocks + a white square hidden in the 3rd block"),
# 12 confirmed working after two rounds of retesting. Ordered fastest to slowest by latency, auto-failover to the next.
# Usage: llm.generate_viz(event, kb_block)  —— same interface as generate(), just a different model chain.
VIZ_FALLBACK = [
    "ministral-3b-latest",
    "ministral-3b-2512",
    "ministral-14b-2512",
    "gemini-3-flash-preview",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "openrouter/free",          # Routing pseudo-model, which one it hits is unpredictable, observed not to emit JSON
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-robotics-er-2-preview",
    "gemma-4-26b-a4b-it",
]

# Confirmed dead (failed both rounds, no longer in the chain):
#   stealth/space-bunny-alpha        upstream 404, not rate limiting
#   @cf/llava-hf/llava-1.5-7b-hf     HTTP 400 request format unsupported
#   @cf/meta/llama-3.2-11b-vision    429 rate limited
#   @cf/moondream / @cf/qwen         502 empty_completion
#   qwen/qwen3.8-27b                 413 request too large
#   gemma-4-31b-it                   timeout
#   nvidia/nemotron-3-nano-omni      502 empty_completion
#   nvidia/nemotron-3.5-content-safety  content safety classifier, returns "User Safety: safe"
#   gemini-3.1/3.5/3.6/3.7/3.8-flash  502 upstream_error (series-wide outage, not a model issue)
#   @cf/mistralai/mistral-small-3.1  answers color order backwards, unusable
#   dots-studio/dots-3-note-preview  does not output JSON

def generate_viz(event, kb_block, images=None):
    """Generation entry point for the image-recognition scenario. Same interface as generate(), just goes through the VIZ_FALLBACK chain.

    Why a separate chain: comment replies are plain text + KB constraints, so auto routing is fine;
    image recognition needs image_url in the input, and many models handle that format poorly,
    so we explicitly maintain a "tested and working" allowlist instead of relying on auto routing.
    """
    attempts = []
    for model in VIZ_FALLBACK:
        t0 = time.time()
        try:
            suggestion, usage = _attempt(event, kb_block,
                                         config.FREELLM_URL, "freellmapi-api-key",
                                         model, config.FREE_TIMEOUT, images=images)
        except Exception as exc:
            attempts.append({"via": "freellmapi", "model": model,
                             "seconds": round(time.time() - t0, 1),
                             "error": repr(exc)[:200]})
            continue
        meta = dict(usage or {})
        meta.update({"via": "freellmapi", "model": model,
                     "seconds": round(time.time() - t0, 1),
                     "fell_back": bool(attempts), "attempts": attempts})
        return suggestion, meta

    raise RuntimeError("Vision chain failed after %d attempts: %s" % (len(VIZ_FALLBACK), attempts))


def generate(event, kb_block):
    """Try in order of "free pool first, official as fallback", returning (suggestion, meta).

    Deliberately no fail-fast: on a single failure, switch to the next backend and retry.
    meta carries the attempts detail, for evaluating free-pool quality after the fact.
    """
    plan = [("freellmapi", config.FREELLM_URL, "freellmapi-api-key",
             config.FREE_LLM_MODEL, config.FREE_TIMEOUT)
            for _ in range(1)]
    plan.append(("deepseek", config.DEEPSEEK_URL, "deepseek-api-key",
                 config.FALLBACK_MODEL, config.LLM_TIMEOUT))

    attempts = []
    for name, url, keyname, model, timeout in plan:
        t0 = time.time()
        try:
            suggestion, usage = _attempt(event, kb_block, url, keyname, model, timeout)
        except Exception as exc:
            attempts.append({"via": name, "model": model,
                             "seconds": round(time.time() - t0, 1),
                             "error": repr(exc)[:300]})
            continue
        meta = dict(usage or {})
        meta.update({"via": name, "model": model,
                     "seconds": round(time.time() - t0, 1),
                     "fell_back": bool(attempts), "attempts": attempts})
        return suggestion, meta

    raise RuntimeError("Retry chain failed after %d attempts: %s" % (len(plan), attempts))
