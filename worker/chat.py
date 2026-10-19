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

SYSTEM_PROMPT = """你是用户通过飞书私信对话的 AI 助手。

- 默认用中文回复；用户用其他语言提问时跟随用户的语言。
- 直接回答问题。不要客套开场，不要复述用户的话，不要问「还有什么可以帮您」。
- 不确定就说不确定，绝不编造事实、数字、链接或引文。
- 回答尽量简洁。用户明确要求展开时才写长。
- 输出纯文本。飞书的文本消息不渲染 Markdown，所以用简单符号和换行排版，
  不要用 **加粗**、# 标题 这类语法。
"""

_HIST_MAX = 24          # 每个用户保留的最大消息条数（user + assistant 合计）
_hist_lock = threading.Lock()

MEM_MAX = 50            # 每个用户最多多少条长期记忆
_MEM_CHARS = 300        # 单条记忆最大长度
_mem_lock = threading.Lock()


def system_message(open_id=None):
    body = SYSTEM_PROMPT + "\n\n当前时间：%s（北京时间）" % time.strftime("%Y-%m-%d %H:%M %A")
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
        return False, 0, "内容为空"
    if len(text) > _MEM_CHARS:
        text = text[:_MEM_CHARS]
    with _mem_lock:
        data = _read_mem()
        items = data.get(open_id) or []
        if len(items) >= MEM_MAX:
            return False, len(items), "已达上限 %d 条，请先用 /忘记 <序号> 删掉旧的" % MEM_MAX
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
            return False, "长期记忆还是空的。"
        if index < 1 or index > len(items):
            return False, "序号超出范围（当前 %d 条）。" % len(items)
        removed = items.pop(index - 1).get("text", "")
        data[open_id] = items
        _write_mem(data)
        return True, "已删除第 %d 条：\n%s" % (index, removed[:80])


def _memory_block(open_id):
    items = memory_list(open_id)
    if not items:
        return ""
    lines = ["\n\n# 长期记忆（用户此前让你记住的）\n\n",
             "回答时可以自然地使用这些信息；不要主动背诵整份清单。\n\n"]
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
        raise RuntimeError("空内容 finish_reason=%r reasoning_tokens=%s usage=%r"
                           % (finish, reasoning, usage))
    if finish == "length":
        content += "\n\n（⚠️ 达到输出上限，回答被截断）"
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
    raise RuntimeError("重试链全部 %d 次失败：%s" % (len(plan), attempts))
