"""AI Worker 主逻辑。

链路：spool 事件 → DeepSeek 生成建议 → 飞书卡片 → 人工点击 → Graph API 回帖
"""
import json, os, queue, threading, time

from . import chat, config, feishu, graph, i18n, kbase, leadstore, leads, llm, spool

from . import larklite

# 垫片必须抢在任何 lark_oapi 导入之前装好。
# 不这么做的话，lark_oapi/__init__.py 的 `from .api import *` 会拉进
# 10623 个模块（约 142MB 常驻内存），而本项目一个 REST 端点都不用。
_LARK_ERROR = None
try:
    larklite.install()
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse, CallBackToast)
    LARK_OK = True
except Exception as _exc:  # 装不上就明确失败，不静默降级
    LARK_OK = False
    _LARK_ERROR = _exc


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------- 待办存储 ----------

_pending_lock = threading.Lock()

_SWEEP_INTERVAL = 3600        # 过期取件记录的清理间隔（秒）


def _load_pending():
    if not os.path.exists(config.PENDING_FILE):
        return {}
    try:
        with open(config.PENDING_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (ValueError, OSError):
        return {}


def _save_pending(data):
    tmp = config.PENDING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, config.PENDING_FILE)


def pending_put(comment_id, record):
    with _pending_lock:
        data = _load_pending()
        data[comment_id] = record
        _save_pending(data)


def pending_get(comment_id):
    with _pending_lock:
        return _load_pending().get(comment_id)


def pending_drop(comment_id):
    with _pending_lock:
        data = _load_pending()
        data.pop(comment_id, None)
        _save_pending(data)


def _prune_expired_leads(data):
    """删除过期的「线索取件记录」，返回删除条数。

    只动 kind == "lead" 的记录。comment 记录是待办队列，等人工点
    「发送 / 忽略」才清除，绝不按时间淘汰 —— 否则审核卡片上的按钮
    会突然报「已过期」，让人以为漏了单。

    created_at 缺失或非数字的一律视为过期：这是可过期的缓存，
    正文已落在飞书多维表格，清掉只影响「复制建议回复」按钮。
    """
    ttl = config.PENDING_LEAD_TTL_DAYS * 86400
    now = time.time()
    doomed = []
    for key, rec in data.items():
        if not isinstance(rec, dict) or rec.get("kind") != "lead":
            continue
        created = rec.get("created_at")
        if not isinstance(created, (int, float)) or now - created > ttl:
            doomed.append(key)
    for key in doomed:
        data.pop(key, None)
    return len(doomed)


def pending_sweep():
    """按 TTL 清理线索取件记录。返回清理条数（0 表示无变化、不写盘）。"""
    with _pending_lock:
        data = _load_pending()
        count = _prune_expired_leads(data)
        if count:
            _save_pending(data)
        return count


# ---------- 事件处理 ----------

_seen = {}
_seen_lock = threading.Lock()


def _already_seen(comment_id):
    now = time.time()
    with _seen_lock:
        for key in [k for k, t in _seen.items() if now - t > config.DEDUP_TTL]:
            _seen.pop(key, None)
        if comment_id in _seen:
            return True
        _seen[comment_id] = now
        return False


def _push_notice_card(event, comment_id):
    """收尾语之类的留言：推一张灰色告知卡，不调 LLM、不带按钮。

    为什么值得推：完全静默会让用户以为「没人理我」，
    而这类留言本身是有信息量的（有人在互动、在客气）。
    """
    page_id = (event.get("page") or {}).get("id")
    if not graph.has_token(page_id):
        log("Skipping comment=%s: homepage %s has no token" % (comment_id, page_id))
        return
    note = ("This is a conversation-closing remark (e.g. \"thanks\", \"ok\", \"got it\") — **no reply needed**.\n\n"
            "Replying would only create a back-and-forth of \"you thank me, I thank you\", so the system deliberately does not reply.\n"
            "This card is for informational purposes only; no action is required.")
    try:
        feishu.send_card(feishu.build_notice_card(event, note))
        log("Pushing no-reply-needed prompt card comment=%s (closing_phrase)" % comment_id)
    except Exception as exc:
        log("Failed to push prompt card comment=%s: %r" % (comment_id, exc))


def _kb_slug(event):
    """取该事件所属主页的知识库标识（gateway 已打标）。"""
    return ((event or {}).get("page") or {}).get("kb")


def process_event(event):
    # 线索（Instant Form）事件走独立支路。两条链路共用一个 spool，
    # 靠 schema 区分；下面的评论逻辑一行不改。
    if (event.get("schema") or "") == "leadpulse.event.lead/v1":
        try:
            leads.process(event)
        except Exception as exc:
            log("Lead processing error: %r" % (exc,))
        return

    comment = event.get("comment") or {}
    decision = event.get("decision") or {}
    comment_id = comment.get("id")

    if not comment_id:
        log("Skipping: event missing comment_id")
        return
    if _already_seen(comment_id):
        log("Skipping comment=%s (already processed this round)" % comment_id)
        return
    if not decision.get("reply"):
        reason = decision.get("reason")
        if reason == "closing_phrase":
            _push_notice_card(event, comment_id)
        else:
            log("Skipping comment=%s (%s)" % (comment_id, reason))
        return

    page_id = (event.get("page") or {}).get("id")
    if not graph.has_token(page_id):
        log("Skipping comment=%s: homepage %s has no access token configured, so it can't be sent anyway" % (comment_id, page_id))
        return

    log("Processing comment=%s from %s" % (comment_id, (event.get("author") or {}).get("name")))
    kb = kbase.load(_kb_slug(event))
    block = kbase.as_prompt_block(kb)
    if kb["facts_empty"]:
        log("  Knowledge base \"confirmed facts\" is empty — the model will not state any product information")

    t0 = time.time()
    try:
        suggestion, usage = llm.generate(event, block)
    except Exception as exc:
        log("  ❌ Generation failed, retry chain exhausted: %r" % (exc,))
        try:
            _tgt, _why = feishu.chat_for((event.get("page") or {}).get("id"), None)
            feishu.send_card(feishu.build_alert_card(event, exc), to=_tgt)
            log("  ⚠️ Pushed \"generation failed\" alert card → group %s (reason: %s)" % (_tgt[-8:], _why))
        except Exception as exc2:
            log("  ❌ Failed to send alert card as well: %r" % (exc2,))
        return
    log("  ✅ Generation complete %.1fs tokens=%s via=%s model=%s%s intent=%s action=%s domain=%s" % (
        time.time() - t0, usage.get("total_tokens", "?"),
        usage.get("via", "?"), usage.get("model", "?"),
        "(degraded fallback)" if usage.get("fell_back") else "",
        suggestion.get("intent"), suggestion.get("action"), suggestion.get("domain")))

    if i18n.code("action", suggestion.get("action")) == "ignore":
        log("  Model decided to ignore, not pushing card")
        return

    card = feishu.build_review_card(event, suggestion)
    target, why = feishu.chat_for((event.get("page") or {}).get("id"),
                                  suggestion.get("domain"))
    try:
        message_id = feishu.send_card(card, to=target)
    except Exception as exc:
        log("  ❌ Failed to push card: %r" % (exc,))
        return
    log("  📨 Review card pushed message_id=%s → group %s (basis: %s, domain=%s)"
        % (message_id, target[-8:], why, suggestion.get("domain")))

    pending_put(comment_id, {
        "message_id": message_id,
        "chat_id": target,
        "page_id": (event.get("page") or {}).get("id"),
        "page_name": (event.get("page") or {}).get("name"),
        "event": event,
        "suggestion": suggestion,
        "created_at": time.time(),
    })


def tail_loop(stop_event):
    tailer = spool.SpoolTailer()
    log("spool listener started: %s" % tailer.path)
    consecutive = 0
    last_sweep = 0.0          # 0 → 启动后第一轮就清一次
    while not stop_event.is_set():
        try:
            for event in tailer.read_new():
                process_event(event)
            consecutive = 0
        except Exception as exc:
            # 一个被 try/except 包住的无限循环，如果每轮都失败，系统会呈现
            # 「健康但无功能」—— 服务是 active，实际什么都没消费。
            # 所以连续失败到阈值就主动退出，让 systemd 重启。
            consecutive += 1
            log("spool loop exception (consecutive #%d): %r" % (consecutive, exc))
            if consecutive >= config.MAX_CONSECUTIVE_FAILURES:
                log("FATAL: spool loop failed %d consecutive times, exiting process so systemd can restart it"
                    % consecutive)
                os._exit(1)

        # 定期清理过期的线索取件记录。放在主循环而非写盘时，
        # 是为了「长期没有新线索」时也能清掉，不依赖新流量触发。
        if time.time() - last_sweep >= _SWEEP_INTERVAL:
            try:
                removed = pending_sweep()
                if removed:
                    log("🧹 Cleaned up %d expired lead pickup records (retention %d days)"
                        % (removed, config.PENDING_LEAD_TTL_DAYS))
            except Exception as exc:
                log("  [WARN] Failed to clean up pickup records (does not affect main flow): %r" % (exc,))
            last_sweep = time.time()

        stop_event.wait(config.POLL_SECONDS)


# ---------- 飞书回调 ----------

def _do_send(record, comment_id, page_id, text, message_id, chat_id=None):
    """在后台线程里真实发帖，完成后原地更新卡片。

    与回调应答分离，避免飞书回调超时（超时会让用户看到假错误并可能重复点击）。
    """
    try:
        ok, detail = graph.post_reply(page_id, comment_id, text)
        if ok:
            log("  → Manually confirmed send, length=%d chars, dry_run=%s"
                % (len(text), bool(detail.get("dry_run"))))
            if detail.get("dry_run"):
                headline = i18n.T("result_dry_run")
                tone = "orange"
            else:
                headline = i18n.T("result_sent") % detail.get("comment_id")
                tone = "green"
            body = i18n.T("result_content") % (headline, text.replace("\n", " "))
        else:
            log("  ❌ Publish failed: %s" % detail)
            body = i18n.T("result_failed") % (str(detail)[:300], text.replace("\n", " "))
            tone = "__TONE_FAIL__"
        done = feishu.build_done_card(record["event"], record["suggestion"], body, tone=tone)
        if message_id:
            done["header"]["title"]["content"] = i18n.T("done_sent") % (
                (record["event"].get("page") or {}).get("name") or i18n.T("unknown_page"))
            _finish_card(message_id, done, "Send", to=chat_id)
    except Exception as exc:
        log("  ❌ Background post error: %r" % (exc,))


def _finish_card(message_id, card, label, to=None):
    """收走带按钮的卡片，另发一张无按钮的「已处理」卡。

    为什么不用原地更新（PATCH）：实测 API 返回 code=0 成功，但部分飞书客户端
    仍显示旧卡片的按钮，用户会重复点击。撤回 + 重发在客户端行为上确定可靠。
    """
    if message_id:
        rok, rout = feishu.recall_message(message_id)
        log("  Recall original card (%s): %s" % (label, "OK" if rok else
                                        "Failed code=%s msg=%s" % (rout.get("code"), rout.get("msg"))))
    else:
        log("  WARN open_message_id not obtained, cannot recall original card")
    try:
        new_id = feishu.send_card(card, to=to)
        log("  Sent \"Processed\" card (%s) message_id=%s" % (label, new_id))
    except Exception as exc:
        log("  ❌ Failed to send \"Processed\" card (%s): %r" % (label, exc))


def on_card_action(data):
    """必须返回应答对象，否则客户端报 code:200671。"""
    resp = P2CardActionTriggerResponse()

    def toast(kind, text):
        t = CallBackToast()
        t.type = kind
        t.content = text
        resp.toast = t
        return resp

    try:
        event = data.event
        operator = getattr(event, "operator", None)
        open_id = getattr(operator, "open_id", None)
        action = getattr(event, "action", None)
        value = (getattr(action, "value", None) or {})
        form = (getattr(action, "form_value", None) or {})
        context = getattr(event, "context", None)
        message_id = getattr(context, "open_message_id", None)

        log("Card click operator=%s value=%s" % (open_id, value))

        allowed = feishu.operator_allowlist()
        if open_id not in allowed:
            log("  ⛔ Rejected: %s is not in the review whitelist" % open_id)
            return toast("error", i18n.T("toast_not_allowed"))

        kind = value.get("action")

        # 「复制建议回复」不属于待审核队列（它不是待发布内容），
        # 所以必须在取 record 之前分流。
        if kind == "lead_copy":
            rec = pending_get("lead:%s" % value.get("leadgen_id"))
            if not rec:
                return toast("warning", i18n.T("toast_lead_expired"))
            threading.Thread(target=_send_lead_copy, args=(open_id, rec),
                             daemon=True).start()
            return toast("info", i18n.T("toast_sending_dm"))

        comment_id = value.get("comment_id")
        record = pending_get(comment_id)
        if not record:
            return toast("warning", i18n.T("toast_handled"))

        if kind == "ignore":
            pending_drop(comment_id)
            log("  → Manually selected \"Ignore\", not published")
            done = feishu.build_done_card(record["event"], record["suggestion"],
                                          i18n.T("result_ignored"), tone="grey")
            done["header"]["title"]["content"] = i18n.T("done_ignored") % (
                (record["event"].get("page") or {}).get("name") or i18n.T("unknown_page"))
            _finish_card(message_id, done, "Ignore", to=record.get("chat_id"))
            return toast("success", i18n.T("toast_ignored"))

        if kind == "send":
            text = (form.get("edited_reply") or "").strip()
            if not text:
                log("  ⛔ No body in the form (form_value is empty), not published")
                return toast("error", i18n.T("empty_reply"))
            page_id = record["page_id"]

            # 【关键】先摘除队列记录，再发帖。
            # 飞书回调超时时用户会重复点击，摘除记录能让第二次点击拿到
            # 「这条已经处理过了」，从机制上杜绝重复发布。
            pending_drop(comment_id)

            # 【关键】立刻应答。飞书要求约 3 秒内返回应答对象，
            # 而 graph.post_reply() 本身就要 2–4 秒 —— 同步执行必然偶发超时，
            # 客户端会弹「目标服务器回调没有反应」，让用户误以为失败。
            threading.Thread(
                target=_do_send,
                args=(record, comment_id, page_id, text, message_id, record.get("chat_id")),
                daemon=True).start()
            return toast("info", i18n.T("toast_sending"))

        return toast("info", i18n.T("toast_unknown"))
    except Exception as exc:
        log("Card callback error: %r" % (exc,))
        return toast("error", i18n.T("toast_error"))


def _send_lead_copy(open_id, record):
    """把线索的回复原文私聊发给点击者，方便长按复制。

    为什么不用原生复制按钮：飞书卡片不接受 copy 行为
    （实测 code 200621 unknown behavior type）。
    """
    text = (record.get("reply_en") or record.get("reply_zh") or "").strip()
    if not text:
        log("  [WARN] Lead copy: no reply text to send")
        return
    body = ("[Inquiry reply text - ready to copy]\n\n%s\n\n-- %s"
            % (text, record.get("page_name") or "Meta inquiry"))
    try:
        out = feishu.send_text_to(open_id, body)
        if out.get("code") == 0:
            log("  [OK] Sent the inquiry reply text to %s via DM" % open_id[-8:])
            return
        log("  [WARN] DM failed code=%s msg=%s, falling back to group"
            % (out.get("code"), out.get("msg")))
    except Exception as exc:
        log("  [WARN] DM exception %r, falling back to group" % (exc,))
    try:
        feishu.send_text(body, to=record.get("chat_id"))
    except Exception as exc:
        log("  [FAIL] Sending to group also failed: %r" % (exc,))


# ---------- 飞书私信 ----------

_seen_msg_ids = set()
_seen_msg_lock = threading.Lock()


def _msg_first_seen(event_id):
    """飞书会重投事件；按 event_id 去重，防止同一条消息回两遍。"""
    if not event_id:
        return True
    with _seen_msg_lock:
        if event_id in _seen_msg_ids:
            return False
        _seen_msg_ids.add(event_id)
        if len(_seen_msg_ids) > 500:
            _seen_msg_ids.clear()
        return True


# ---------- 飞书私信：自由对话 ----------

_chat_queue = queue.Queue()

HELP_TEXT = """Available commands:
/remember <content>   -- write to long-term memory (persists across sessions, max 50 entries)
/memory          -- view remembered content
/forget <index>   -- delete an entry
/clear         -- clear this session's context (long-term memory untouched)
/help          -- show this message

Anything else goes straight to the AI assistant:
The model uses the FreeLLMAPI free pool (auto routing), with DeepSeek official as fallback on failure.

Note: long-term memory is sent to the model with every conversation, so don't put passwords, keys, or customer contact info in it."""


def _msg_first_seen(event_id):
    """飞书会重投事件；按 event_id 去重，防止同一条消息回两遍。"""
    if not event_id:
        return True
    with _seen_msg_lock:
        if event_id in _seen_msg_ids:
            return False
        _seen_msg_ids.add(event_id)
        if len(_seen_msg_ids) > 500:
            _seen_msg_ids.clear()
        return True


def on_message(payload):
    """飞书私信入口。

    **必须立刻返回** —— 这里跑在长连接线程里，任何阻塞都会拖住 WS 心跳。
    真正的生成交给 chat_loop 后台线程。
    """
    try:
        event = payload.get("event") or {}
        header = payload.get("header") or {}
        event_id = header.get("event_id")
        sender = event.get("sender") or {}
        open_id = ((sender.get("sender_id") or {}).get("open_id")) or ""
        msg = event.get("message") or {}
        chat_type = msg.get("chat_type")
        msg_type = msg.get("message_type")
        raw = msg.get("content") or ""
        try:
            text = (json.loads(raw) or {}).get("text", "")
        except Exception:
            text = raw

        if not _msg_first_seen(event_id):
            log("Duplicate DM delivery, ignored event_id=%s" % event_id)
            return None

        if chat_type != "p2p":
            log("DM event is not a one-on-one chat (chat_type=%s), ignoring" % chat_type)
            return None

        if open_id not in feishu.operator_allowlist():
            log("Rejected: DM from outside whitelist %s" % open_id)
            return None

        if msg_type != "text":
            feishu.send_text_to(open_id, "Only plain text messages are supported for now.")
            return None

        text = (text or "").strip()
        if not text:
            return None
        log("Received DM open_id=%s body=%s" % (open_id[-8:], text[:120]))

        if _handle_command(open_id, text):
            return None

        _chat_queue.put((open_id, text))
    except Exception as exc:
        log("DM entry point error: %r" % (exc,))
    return None


def _handle_command(open_id, text):
    """返回 True 表示已作为命令处理，不要再丢给模型。"""
    low = text.lower()

    if low in ("/clear", "/reset"):
        chat.clear_history(open_id)
        feishu.send_text_to(open_id, "Cleared this session's context (long-term memory retained).")
        log("  Cleared context for %s" % open_id[-8:])
        return True

    if low in ("/help", "/?"):
        feishu.send_text_to(open_id, HELP_TEXT)
        return True

    if low in ("/memory", "/mem"):
        items = chat.memory_list(open_id)
        if not items:
            feishu.send_text_to(open_id, "Long-term memory is still empty.\nUse /remember <content> to add.")
        else:
            lines = ["Long-term memory (%d/%d entries):" % (len(items), chat.MEM_MAX)]
            for i, item in enumerate(items, 1):
                lines.append("%d. %s" % (i, item.get("text", "")))
            lines.append("")
            lines.append("Delete: /forget <index>")
            feishu.send_text_to(open_id, "\n".join(lines))
        return True

    for prefix in ("/remember",):
        if low.startswith(prefix) or text.startswith(prefix):
            body = text[len(prefix):].strip()
            if not body:
                feishu.send_text_to(
                    open_id, "Usage: /remember <content>\nExample: /remember I trade electromechanical equipment in East China")
                return True
            ok, count, err = chat.memory_add(open_id, body)
            if not ok:
                feishu.send_text_to(open_id, "Not remembered: %s" % err)
            else:
                msg = "Remembered (entry %d/%d):\n%s" % (count, chat.MEM_MAX, body[:120])
                if count == 1:
                    msg += ("\n\n⚠️ Note: this will be sent to third-party models (free pool) with every conversation,"
                            "so please don't include passwords, keys, or customer contact details.")
                feishu.send_text_to(open_id, msg)
                log("  Wrote memory %s entry %d" % (open_id[-8:], count))
            return True

    for prefix in ("/forget",):
        if low.startswith(prefix) or text.startswith(prefix):
            body = text[len(prefix):].strip()
            if not body.isdigit():
                feishu.send_text_to(open_id, "Usage: /forget <index> (see /memory for indexes)")
                return True
            ok, detail = chat.memory_del(open_id, int(body))
            feishu.send_text_to(open_id, detail)
            if ok:
                log("  Deleted memory %s entry %s" % (open_id[-8:], body))
            return True

    return False


def chat_loop():
    """后台消费私信队列。串行处理，保证同一用户的消息顺序不乱。"""
    while True:
        try:
            open_id, text = _chat_queue.get()
        except Exception:
            continue
        try:
            _do_chat(open_id, text)
        except Exception as exc:
            log("Chat processing exception: %r" % (exc,))
        finally:
            _chat_queue.task_done()


def _do_chat(open_id, text):
    t0 = time.time()
    history = chat.load_history(open_id)
    history.append({"role": "user", "content": text})
    messages = [chat.system_message(open_id)] + history
    try:
        answer, meta = chat.reply(messages)
    except Exception as exc:
        chat.save_history(open_id, history)
        log("  [FAIL] Generation failed: %r" % (exc,))
        feishu.send_text_to(open_id, "Generation failed:\n%s" % str(exc)[:280])
        return
    history.append({"role": "assistant", "content": answer})
    chat.save_history(open_id, history)
    body = answer if len(answer) <= 4000 else answer[:4000] + "\n...(truncated, too long)"
    out = feishu.send_text_to(open_id, body)
    ok = (out.get("code") == 0)
    log("  [%s] Replied %s | %.1fs via=%s model=%s tokens=%s%s"
        % ("OK" if ok else "WARN", open_id[-8:], time.time() - t0,
           meta.get("via"), meta.get("model"), meta.get("total_tokens"),
           "(degraded fallback)" if meta.get("fell_back") else ""))
    if not ok:
        log("  [WARN] Feishu send failed code=%s msg=%s" % (out.get("code"), out.get("msg")))


def build_lark_client():
    return larklite.make_client(config.read_secret("feishu-app-id"),
                                config.read_secret("feishu-app-secret"),
                                on_card_action,
                                on_message=on_message)


def self_check():
    kb = kbase.load(None)
    print("  POST_ENABLED   : %s" % config.POST_ENABLED)
    print("  Model          : %s" % config.MODEL)
    print("  spool          : %s" % config.SPOOL_FILE)
    print("  KB confirmed facts  : %d items%s" % (len(kb["facts"]), "(empty — the model will not state any product information)" if kb["facts_empty"] else ""))
    print("  KB forbidden statements    : %d items" % len(kb["forbid"]))
    print("  Review whitelist     : %s" % ", ".join(sorted(feishu.operator_allowlist())) or "(empty)")
    print("  Push target chat  : %s" % feishu.chat_id())
    print("  lark SDK       : %s" % ("Loaded (minimal shim)" if LARK_OK else "Unavailable: %r" % _LARK_ERROR))
    ids = _token_page_ids()
    if not ids:
        print("  Homepage token recheck    : skipped (no token under %s)" % config.GRAPH_SECRETS)
    for pid in ids:
        ok, me = graph.verify_identity(pid)
        print("  %s token identity recheck: %s (/me returned %s / %s)" % (
            pid, "✅ Is homepage identity" if ok else "❌ Mismatch",
            me.get("id"), me.get("name")))


def _token_page_ids():
    """列出已配置令牌的主页 ID，用于自检。"""
    import glob, os
    pattern = os.path.join(config.GRAPH_SECRETS, "page-token-*")
    return sorted(os.path.basename(p)[len("page-token-"):] for p in glob.glob(pattern))


def main():
    import sys
    if "--check" in sys.argv:
        self_check()
        return
    log("=" * 60)
    log("Lead Pulse Guard worker starting")
    log("  model=%s  POST_ENABLED=%s" % (config.MODEL, config.POST_ENABLED))
    kb = kbase.load(None)
    log("  Knowledge base: %d confirmed facts%s" % (
        len(kb["facts"]), "(empty)" if kb["facts_empty"] else ""))
    log("=" * 60)
    stop = threading.Event()
    t = threading.Thread(target=tail_loop, args=(stop,), daemon=True)
    t.start()
    threading.Thread(target=chat_loop, daemon=True).start()
    client = build_lark_client()
    try:
        client.start()  # 阻塞
    finally:
        stop.set()
