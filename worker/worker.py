"""AI Worker 主逻辑。

链路：spool 事件 → DeepSeek 生成建议 → 飞书卡片 → 人工点击 → Graph API 回帖
"""
import json, os, queue, threading, time

from . import chat, config, feishu, graph, kbase, leadstore, leads, llm, spool

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
        log("跳过 comment=%s：主页 %s 无令牌" % (comment_id, page_id))
        return
    note = ("这条属于对话收尾语（如「谢谢」「好的」「收到」）—— **无需回复**。\n\n"
            "回复它只会形成「你谢我、我谢你」的来回刷屏，所以系统主动不回。\n"
            "本卡片仅作告知，不需要任何操作。")
    try:
        feishu.send_card(feishu.build_notice_card(event, note))
        log("推送无需回复提示卡 comment=%s（closing_phrase）" % comment_id)
    except Exception as exc:
        log("提示卡推送失败 comment=%s: %r" % (comment_id, exc))


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
            log("线索处理异常: %r" % (exc,))
        return

    comment = event.get("comment") or {}
    decision = event.get("decision") or {}
    comment_id = comment.get("id")

    if not comment_id:
        log("跳过：事件缺少 comment_id")
        return
    if _already_seen(comment_id):
        log("跳过 comment=%s（本轮已处理过）" % comment_id)
        return
    if not decision.get("reply"):
        reason = decision.get("reason")
        if reason == "closing_phrase":
            _push_notice_card(event, comment_id)
        else:
            log("跳过 comment=%s（%s）" % (comment_id, reason))
        return

    page_id = (event.get("page") or {}).get("id")
    if not graph.has_token(page_id):
        log("跳过 comment=%s：主页 %s 没有配置访问令牌，推了也发不出去" % (comment_id, page_id))
        return

    log("处理 comment=%s 来自 %s" % (comment_id, (event.get("author") or {}).get("name")))
    kb = kbase.load(_kb_slug(event))
    block = kbase.as_prompt_block(kb)
    if kb["facts_empty"]:
        log("  知识库「已确认事实」为空 —— 模型将不会陈述任何产品信息")

    t0 = time.time()
    try:
        suggestion, usage = llm.generate(event, block)
    except Exception as exc:
        log("  ❌ 生成失败，重试链全部用尽: %r" % (exc,))
        try:
            _tgt, _why = feishu.chat_for((event.get("page") or {}).get("id"), None)
            feishu.send_card(feishu.build_alert_card(event, exc), to=_tgt)
            log("  ⚠️ 已推送「生成失败」告警卡片 → 群 %s（依据：%s）" % (_tgt[-8:], _why))
        except Exception as exc2:
            log("  ❌ 告警卡片也发送失败: %r" % (exc2,))
        return
    log("  ✅ 生成完成 %.1fs tokens=%s via=%s model=%s%s intent=%s action=%s domain=%s" % (
        time.time() - t0, usage.get("total_tokens", "?"),
        usage.get("via", "?"), usage.get("model", "?"),
        "（已降级兜底）" if usage.get("fell_back") else "",
        suggestion.get("intent"), suggestion.get("action"), suggestion.get("domain")))

    if suggestion.get("action") == "忽略":
        log("  模型判定为忽略，不推送卡片")
        return

    card = feishu.build_review_card(event, suggestion)
    target, why = feishu.chat_for((event.get("page") or {}).get("id"),
                                  suggestion.get("domain"))
    try:
        message_id = feishu.send_card(card, to=target)
    except Exception as exc:
        log("  ❌ 推送卡片失败: %r" % (exc,))
        return
    log("  📨 已推送审核卡片 message_id=%s → 群 %s（依据：%s，domain=%s）"
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
    log("spool 监听启动: %s" % tailer.path)
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
            log("spool 循环异常（连续第 %d 次）: %r" % (consecutive, exc))
            if consecutive >= config.MAX_CONSECUTIVE_FAILURES:
                log("FATAL: spool 循环连续失败 %d 次，进程退出以便 systemd 重启"
                    % consecutive)
                os._exit(1)

        # 定期清理过期的线索取件记录。放在主循环而非写盘时，
        # 是为了「长期没有新线索」时也能清掉，不依赖新流量触发。
        if time.time() - last_sweep >= _SWEEP_INTERVAL:
            try:
                removed = pending_sweep()
                if removed:
                    log("🧹 清理 %d 条过期线索取件记录（保留 %d 天）"
                        % (removed, config.PENDING_LEAD_TTL_DAYS))
            except Exception as exc:
                log("  [WARN] 取件记录清理失败（不影响主流程）: %r" % (exc,))
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
            log("  → 人工确认发送，长度=%d 字符，dry_run=%s"
                % (len(text), bool(detail.get("dry_run"))))
            if detail.get("dry_run"):
                headline = "**已模拟发送**（当前为 dry-run，未真正发布到 Facebook）"
                tone = "orange"
            else:
                headline = "**已发送到 Facebook** ✅　新评论 ID：`%s`" % detail.get("comment_id")
                tone = "green"
            body = "%s\n\n**发出的内容：**\n> %s" % (headline, text.replace("\n", " "))
        else:
            log("  ❌ 发布失败: %s" % detail)
            body = ("**发布失败** ❌\n\n```\n%s\n```\n\n**原草稿（未发出）：**\n> %s"
                    % (str(detail)[:300], text.replace("\n", " ")))
            tone = "__TONE_FAIL__"
        done = feishu.build_done_card(record["event"], record["suggestion"], body, tone=tone)
        if message_id:
            done["header"]["title"]["content"] = "✅ 已处理（发送）· %s" % (
                (record["event"].get("page") or {}).get("name") or "未知主页")
            _finish_card(message_id, done, "发送", to=chat_id)
    except Exception as exc:
        log("  ❌ 后台发帖异常: %r" % (exc,))


def _finish_card(message_id, card, label, to=None):
    """收走带按钮的卡片，另发一张无按钮的「已处理」卡。

    为什么不用原地更新（PATCH）：实测 API 返回 code=0 成功，但部分飞书客户端
    仍显示旧卡片的按钮，用户会重复点击。撤回 + 重发在客户端行为上确定可靠。
    """
    if message_id:
        rok, rout = feishu.recall_message(message_id)
        log("  撤回原卡片（%s）: %s" % (label, "OK" if rok else
                                        "失败 code=%s msg=%s" % (rout.get("code"), rout.get("msg"))))
    else:
        log("  WARN 未取到 open_message_id，无法撤回原卡片")
    try:
        new_id = feishu.send_card(card, to=to)
        log("  已发送「已处理」卡片（%s）message_id=%s" % (label, new_id))
    except Exception as exc:
        log("  ❌ 发送「已处理」卡片失败（%s）: %r" % (label, exc))


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

        log("卡片点击 operator=%s value=%s" % (open_id, value))

        allowed = feishu.operator_allowlist()
        if open_id not in allowed:
            log("  ⛔ 拒绝：%s 不在审核白名单内" % open_id)
            return toast("error", "你没有审核权限")

        kind = value.get("action")

        # 「复制建议回复」不属于待审核队列（它不是待发布内容），
        # 所以必须在取 record 之前分流。
        if kind == "lead_copy":
            rec = pending_get("lead:%s" % value.get("leadgen_id"))
            if not rec:
                return toast("warning", "这条线索的回复原文已过期，请到多维表格里查看")
            threading.Thread(target=_send_lead_copy, args=(open_id, rec),
                             daemon=True).start()
            return toast("info", "正在把回复原文发到你的私聊…")

        comment_id = value.get("comment_id")
        record = pending_get(comment_id)
        if not record:
            return toast("warning", "这条已经处理过了")

        if kind == "ignore":
            pending_drop(comment_id)
            log("  → 人工选择「忽略」，未发布")
            done = feishu.build_done_card(record["event"], record["suggestion"],
                                          "**已忽略** —— 未发布任何回复。", tone="grey")
            done["header"]["title"]["content"] = "✅ 已处理（忽略）· %s" % (
                (record["event"].get("page") or {}).get("name") or "未知主页")
            _finish_card(message_id, done, "忽略", to=record.get("chat_id"))
            return toast("success", "已忽略")

        if kind == "send":
            text = (form.get("edited_reply") or "").strip()
            if not text:
                log("  ⛔ 表单里没有正文（form_value 为空），未发布")
                return toast("error", "回复正文是空的")
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
            return toast("info", "正在发送…")

        return toast("info", "未知操作")
    except Exception as exc:
        log("卡片回调异常: %r" % (exc,))
        return toast("error", "处理出错，请重试")


def _send_lead_copy(open_id, record):
    """把线索的回复原文私聊发给点击者，方便长按复制。

    为什么不用原生复制按钮：飞书卡片不接受 copy 行为
    （实测 code 200621 unknown behavior type）。
    """
    text = (record.get("reply_en") or record.get("reply_zh") or "").strip()
    if not text:
        log("  [WARN] 线索复制：没有可发送的回复原文")
        return
    body = ("【询盘回复原文 - 可直接复制】\n\n%s\n\n-- %s"
            % (text, record.get("page_name") or "Meta 询盘"))
    try:
        out = feishu.send_text_to(open_id, body)
        if out.get("code") == 0:
            log("  [OK] 已把询盘回复原文私聊发给 %s" % open_id[-8:])
            return
        log("  [WARN] 私聊失败 code=%s msg=%s，改为发到群里"
            % (out.get("code"), out.get("msg")))
    except Exception as exc:
        log("  [WARN] 私聊异常 %r，改为发到群里" % (exc,))
    try:
        feishu.send_text(body, to=record.get("chat_id"))
    except Exception as exc:
        log("  [FAIL] 发到群里也失败: %r" % (exc,))


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
_seen_msg_ids = set()
_seen_msg_lock = threading.Lock()

HELP_TEXT = """可用命令：
/记住 <内容>   —— 写入长期记忆（跨会话保留，最多 50 条）
/记忆          —— 查看已记住的内容
/忘记 <序号>   —— 删除某一条
/clear         —— 清空本会话上下文（不动长期记忆）
/help          —— 显示这条

其他内容直接发给 AI 助手：
模型走 FreeLLMAPI 免费池（auto 路由），失败时兜底 DeepSeek 官方。

注意：长期记忆会随每次对话一并发给该模型，请不要放密码、密钥、客户联系方式。"""


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
            log("私信重复投递，已忽略 event_id=%s" % event_id)
            return None

        if chat_type != "p2p":
            log("私信事件非单聊（chat_type=%s），忽略" % chat_type)
            return None

        if open_id not in feishu.operator_allowlist():
            log("拒绝：私信来自白名单外 %s" % open_id)
            return None

        if msg_type != "text":
            feishu.send_text_to(open_id, "目前只支持纯文本消息。")
            return None

        text = (text or "").strip()
        if not text:
            return None
        log("收到私信 open_id=%s 正文=%s" % (open_id[-8:], text[:120]))

        if _handle_command(open_id, text):
            return None

        _chat_queue.put((open_id, text))
    except Exception as exc:
        log("私信入口异常: %r" % (exc,))
    return None


def _handle_command(open_id, text):
    """返回 True 表示已作为命令处理，不要再丢给模型。"""
    low = text.lower()

    if low in ("/clear", "/reset") or text == "/重置":
        chat.clear_history(open_id)
        feishu.send_text_to(open_id, "已清空本会话上下文（长期记忆保留）。")
        log("  已清空 %s 的上下文" % open_id[-8:])
        return True

    if low in ("/help", "/?", "/帮助"):
        feishu.send_text_to(open_id, HELP_TEXT)
        return True

    if low in ("/memory", "/mem", "/记忆"):
        items = chat.memory_list(open_id)
        if not items:
            feishu.send_text_to(open_id, "长期记忆还是空的。\n用 /记住 <内容> 添加。")
        else:
            lines = ["长期记忆（%d/%d 条）：" % (len(items), chat.MEM_MAX)]
            for i, item in enumerate(items, 1):
                lines.append("%d. %s" % (i, item.get("text", "")))
            lines.append("")
            lines.append("删除：/忘记 <序号>")
            feishu.send_text_to(open_id, "\n".join(lines))
        return True

    for prefix in ("/remember", "/记住"):
        if low.startswith(prefix) or text.startswith(prefix):
            body = text[len(prefix):].strip()
            if not body:
                feishu.send_text_to(
                    open_id, "用法：/记住 <内容>\n例如：/记住 我在华东做机电设备贸易")
                return True
            ok, count, err = chat.memory_add(open_id, body)
            if not ok:
                feishu.send_text_to(open_id, "没记住：%s" % err)
            else:
                msg = "已记住（第 %d/%d 条）：\n%s" % (count, chat.MEM_MAX, body[:120])
                if count == 1:
                    msg += ("\n\n⚠️ 提醒：这条会随每次对话发给第三方模型（免费池），"
                            "请不要放密码、密钥、客户联系方式。")
                feishu.send_text_to(open_id, msg)
                log("  写入记忆 %s 第 %d 条" % (open_id[-8:], count))
            return True

    for prefix in ("/forget", "/忘记"):
        if low.startswith(prefix) or text.startswith(prefix):
            body = text[len(prefix):].strip()
            if not body.isdigit():
                feishu.send_text_to(open_id, "用法：/忘记 <序号>（序号见 /记忆）")
                return True
            ok, detail = chat.memory_del(open_id, int(body))
            feishu.send_text_to(open_id, detail)
            if ok:
                log("  删除记忆 %s 第 %s 条" % (open_id[-8:], body))
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
            log("聊天处理异常: %r" % (exc,))
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
        log("  [FAIL] 生成失败: %r" % (exc,))
        feishu.send_text_to(open_id, "生成失败：\n%s" % str(exc)[:280])
        return
    history.append({"role": "assistant", "content": answer})
    chat.save_history(open_id, history)
    body = answer if len(answer) <= 4000 else answer[:4000] + "\n...(过长已截断)"
    out = feishu.send_text_to(open_id, body)
    ok = (out.get("code") == 0)
    log("  [%s] 已回复 %s | %.1fs via=%s model=%s tokens=%s%s"
        % ("OK" if ok else "WARN", open_id[-8:], time.time() - t0,
           meta.get("via"), meta.get("model"), meta.get("total_tokens"),
           "（已降级兜底）" if meta.get("fell_back") else ""))
    if not ok:
        log("  [WARN] 飞书发送失败 code=%s msg=%s" % (out.get("code"), out.get("msg")))


def build_lark_client():
    return larklite.make_client(config.read_secret("feishu-app-id"),
                                config.read_secret("feishu-app-secret"),
                                on_card_action,
                                on_message=on_message)


def self_check():
    kb = kbase.load(None)
    print("  POST_ENABLED   : %s" % config.POST_ENABLED)
    print("  模型           : %s" % config.MODEL)
    print("  spool          : %s" % config.SPOOL_FILE)
    print("  KB 已确认事实  : %d 条%s" % (len(kb["facts"]), "（空 —— 模型不会陈述任何产品信息）" if kb["facts_empty"] else ""))
    print("  KB 禁止陈述    : %d 条" % len(kb["forbid"]))
    print("  审核白名单     : %s" % ", ".join(sorted(feishu.operator_allowlist())) or "（空）")
    print("  推送目标 chat  : %s" % feishu.chat_id())
    print("  lark SDK       : %s" % ("已加载（精简垫片）" if LARK_OK else "不可用: %r" % _LARK_ERROR))
    ids = _token_page_ids()
    if not ids:
        print("  主页令牌复核    : 跳过（%s 下无令牌）" % config.GRAPH_SECRETS)
    for pid in ids:
        ok, me = graph.verify_identity(pid)
        print("  %s 令牌身份复核: %s（/me 返回 %s / %s）" % (
            pid, "✅ 是主页身份" if ok else "❌ 不符",
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
    log("Lead Pulse Guard worker 启动")
    log("  模型=%s  POST_ENABLED=%s" % (config.MODEL, config.POST_ENABLED))
    kb = kbase.load(None)
    log("  知识库：已确认事实 %d 条%s" % (
        len(kb["facts"]), "（空）" if kb["facts_empty"] else ""))
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
