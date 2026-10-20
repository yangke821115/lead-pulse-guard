"""飞书私信 → LLM 自由对话 + 本窗口私有的长期记忆。

与 llm.py 的分工：
  · llm.py   = 「Meta 评论 → 结构化回复建议」：固定 system prompt、强制 JSON、
               知识库约束、五条调性规则
  · chat.py  = 私信自由对话：纯文本输出，模型按自己的默认风格答，无知识库约束

记忆设计（方案 C）：与 Kai 的 kai-mem **物理隔离**。这里只存用户自己
用 /记住 明确写下的条目，随每次对话注入 system prompt。
代价是这些内容会发给免费池的第三方模型 —— 所以明确警示不要放密钥。
"""
import json, os, threading, time, urllib.request

from . import config

SYSTEM_PROMPT = """You are an AI assistant chatting with the user through Lark direct messages.

- Reply in the user's language; default to English when it is unclear.
- Answer the question directly. No pleasantries, no restating the user's message, no "anything else I can help with".
- If you are unsure, say so. Never invent facts, numbers, links, or quotes.
- Keep answers concise. Write more only when the user explicitly asks.
- Output plain text. Lark text messages do not render Markdown, so use simple symbols and line breaks,
  not **bold** or # headings.
"""

_HIST_MAX = 24          # 每个用户保留的最大消息条数（user + assistant 合计）
_hist_lock = threading.Lock()

MEM_MAX = 50            # 每个用户最多多少条长期记忆
_MEM_CHARS = 300        # 单条记忆最大长度
_mem_lock = threading.Lock()


def system_message(open_id=None):
    body = SYSTEM_PROMPT + "\n\nCurrent time: %s (Beijing time)" % time.strftime("%Y-%m-%d %H:%M %A")
    if open_id:
        block = _memory_block(open_id)
        if block:
            body += block
    return {"role": "system", "content": body}


# ---------- 会话历史 ----------

def _read_all():
    path = config.CHAT_HISTORY_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_all(data):
    path = config.CHAT_HISTORY_FILE
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, path)


def load_history(open_id):
    return list(_read_all().get(open_id) or [])


def save_history(open_id, messages):
    with _hist_lock:
        data = _read_all()
        data[open_id] = list(messages)[-_HIST_MAX:]
        _write_all(data)


def clear_history(open_id):
    with _hist_lock:
        data = _read_all()
        data.pop(open_id, None)
        _write_all(data)


# ---------- 长期记忆（本窗口私有） ----------

def _read_mem():
    path = config.CHAT_MEMORY_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_mem(data):
    path = config.CHAT_MEMORY_FILE
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, path)


def memory_list(open_id):
    return list(_read_mem().get(open_id) or [])


def memory_add(open_id, text):
    """返回 (ok, count, err)。"""
    text = " ".join((text or "").split())
    if not text:
        return False, 0, "Content is empty"
    if len(text) > _MEM_CHARS:
        text = text[:_MEM_CHARS]
    with _mem_lock:
        data = _read_mem()
        items = data.get(open_id) or []
        if len(items) >= MEM_MAX:
            return False, len(items), "Reached the limit of %d entries; use /forget <index> to remove old ones" % MEM_MAX
        items.append({"text": text, "at": time.strftime("%Y-%m-%d %H:%M")})
        data[open_id] = items
        _write_mem(data)
        return True, len(items), ""


def memory_del(open_id, index):
    """返回 (ok, 提示文字)。"""
    with _mem_lock:
        data = _read_mem()
        items = data.get(open_id) or []
        if not items:
            return False, "Long-term memory is empty."
        if index < 1 or index > len(items):
            return False, "Index out of range (currently %d entries)." % len(items)
        removed = items.pop(index - 1).get("text", "")
        data[open_id] = items
        _write_mem(data)
        return True, "Deleted entry %d:\n%s" % (index, removed[:80])


def _memory_block(open_id):
    items = memory_list(open_id)
    if not items:
        return ""
    lines = ["\n\n# Long-term memory (things the user asked you to remember)\n\n",
             "Use this naturally when answering; do not recite the whole list.\n\n"]
    for i, item in enumerate(items, 1):
        lines.append("%d. %s\n" % (i, item.get("text", "")))
    return "".join(lines)


# ---------- 调用 ----------

def _post(payload, url, keyname, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", "Bearer " + config.read_secret(keyname))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _attempt(messages, url, keyname, model, timeout, extra=None):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": config.CHAT_TEMPERATURE,
        "max_tokens": config.CHAT_MAX_TOKENS,
    }
    if extra:
        payload.update(extra)
    body = _post(payload, url, keyname, timeout)
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = (msg.get("content") or "").strip()
    finish = choice.get("finish_reason")
    usage = body.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")

    # 推理模型把预算吃光时正文会是空的，且 HTTP 仍是 200 —— 必须显式判失败
    if not content:
        raise RuntimeError("Empty content: finish_reason=%r reasoning_tokens=%s usage=%r"
                           % (finish, reasoning, usage))
    if finish == "length":
        content += "\n\n(⚠️ output limit reached; the answer was truncated)"
    return content, usage


def reply(messages):
    """按「免费池优先、官方兜底」尝试，返回 (text, meta)。"""
    plan = [("freellmapi", config.FREELLM_URL, "freellmapi-api-key",
             config.CHAT_MODEL, config.CHAT_TIMEOUT)
            for _ in range(max(1, config.CHAT_ATTEMPTS))]
    plan.append(("deepseek", config.DEEPSEEK_URL, "deepseek-api-key",
                 config.FALLBACK_MODEL, config.LLM_TIMEOUT))

    attempts = []
    for name, url, keyname, model, timeout in plan:
        t0 = time.time()
        # deepseek-flash 是推理模型：不关推理，思维链会吃光 max_tokens，
        # 正文返回空字符串且不报错（实测约 1/5 概率）。免费池不传这个参数。
        extra = {"reasoning_effort": "none"} if name == "deepseek" else None
        try:
            text, usage = _attempt(messages, url, keyname, model, timeout, extra)
        except Exception as exc:
            attempts.append({"via": name, "model": model,
                             "seconds": round(time.time() - t0, 1),
                             "error": repr(exc)[:240]})
            continue
        meta = dict(usage or {})
        meta.update({"via": name, "model": model,
                     "seconds": round(time.time() - t0, 1),
                     "fell_back": bool(attempts), "attempts": attempts})
        return text, meta
    raise RuntimeError("Retry chain failed after %d attempts: %s" % (len(plan), attempts))
