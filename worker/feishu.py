"""飞书侧：发卡片、更新卡片、拿 tenant_access_token。"""
import json, os, time, urllib.error, urllib.request

from . import config

BASE = "https://open.feishu.cn/open-apis"
_token_cache = {"value": None, "expire_at": 0}


def _request(path, payload, method="POST", token=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8") or "{}")


def tenant_token():
    now = time.time()
    if _token_cache["value"] and _token_cache["expire_at"] > now + 60:
        return _token_cache["value"]
    out = _request("/auth/v3/tenant_access_token/internal", {
        "app_id": config.read_secret("feishu-app-id"),
        "app_secret": config.read_secret("feishu-app-secret"),
    })
    if out.get("code") != 0:
        raise RuntimeError("tenant_access_token 失败: %s" % out)
    _token_cache["value"] = out["tenant_access_token"]
    _token_cache["expire_at"] = now + int(out.get("expire", 7200))
    return _token_cache["value"]


ROUTING_FILE = os.path.join(config.SECRETS, "chat-routing.json")
_route_cache = {"mtime": None, "cfg": None}


def load_routing():
    """读路由表。文件改动即时生效（按 mtime 缓存）。"""
    try:
        mt = os.path.getmtime(ROUTING_FILE)
    except OSError:
        return {"routes": {}, "defaults": {}}
    if _route_cache["mtime"] != mt:
        try:
            with open(ROUTING_FILE, encoding="utf-8") as f:
                _route_cache["cfg"] = json.load(f)
            _route_cache["mtime"] = mt
        except Exception:
            return {"routes": {}, "defaults": {}}
    return _route_cache["cfg"] or {"routes": {}, "defaults": {}}


def chat_for(page_id, domain):
    """按 主页 + 产品域 决定卡片发到哪个群。

    回退顺序：精确 domain → 主页 "*" → defaults[page_id] → 单群配置。
    故意设置多条回退：路由表坏掉时卡片必须仍有去处，不能凭空消失。
    """
    cfg = load_routing()
    pid = str(page_id or "")
    routes = (cfg.get("routes") or {}).get(pid) or {}
    if domain and domain in routes:
        return routes[domain], "domain=%s" % domain
    if "*" in routes:
        return routes["*"], "page-wildcard"
    d = (cfg.get("defaults") or {}).get(pid)
    if d:
        return d, "page-default"
    return chat_id(), "single-chat-fallback"


def chat_id():
    return config.read_secret("feishu-chat-id")


def operator_allowlist():
    raw = config.read_secret("operator-allowlist")
    return {x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()}


# ---------- 卡片构造 ----------

def _md(text):
    return {"tag": "markdown", "content": text}


# 飞书 input.default_value 的实测上限：1000 字符可靠通过，1200 字符被拒
# （2026-09-21 实测；边界在 1000–1200 之间）。留足余量取 1000。
CARD_TEXT_LIMIT = 1000


def _clip(text, limit):
    """飞书 input.default_value 有长度上限，超了整张卡片会创建失败（code 230099）。

    ⚠️ 这是**最后一道防线，正常永不触发** —— 提示词已要求控制在 400 字符内，
    此处阈值 500 是留给冗余的。一旦触发，说明模型没遵守长度约束，
    必须**高声记日志**，否则会静默地把草稿砍尾，用户点发送就发出半句话。
    """
    if len(text) <= limit:
        return text
    import logging
    logging.getLogger(__name__).warning(
        "回复超长被截断：%d → %d 字符。提示词要求 ≤400 字符，模型未遵守。"
        "被截断的草稿若直接发送，客户会看到断句。原文头 80 字：%r",
        len(text), limit, text[:80])
    return text[:limit - 24].rstrip() + " …（⚠️草稿过长已截断，请勿直接发送）"


def _source_line(event):
    """把「用户留言来自哪个帖子、什么时间」压成一行，便于审核时判断上下文。"""
    post = (event.get("post") or {})
    comment = (event.get("comment") or {})
    bits = []
    st = post.get("status_type")
    kind = {"added_photos": "图文帖", "added_video": "视频帖/Reel",
            "created": "文字帖", "shared_story": "转发帖"}.get(st)
    if kind:
        bits.append("帖子类型：" + kind)
    url = post.get("permalink_url") or comment.get("permalink_url")
    if url:
        bits.append("[打开原帖](%s)" % url)
    elif post.get("id"):
        bits.append("帖子 ID：`%s`" % post.get("id"))
    ts = comment.get("created_time")
    if ts:
        try:
            dt = datetime.datetime.fromtimestamp(
                int(ts), datetime.timezone(datetime.timedelta(hours=8)))
            bits.append("留言时间：%s（北京时间）" % dt.strftime("%Y-%m-%d %H:%M"))
        except Exception:
            pass
    return "**来源：** " + "　".join(bits) if bits else "**来源：** 未知"


def recall_message(message_id):
    """撤回自己发出的消息。用于「已处理」后把带按钮的卡片收走。"""
    out = _request("/im/v1/messages/%s" % message_id, {}, method="DELETE",
                   token=tenant_token())
    return out.get("code") == 0, out


def build_review_card(event, suggestion):
    """人工审核卡片：用户评论 / AI分析 / AI回复建议（可在输入框里改后发送）。"""
    page = event.get("page") or {}
    author = event.get("author") or {}
    comment = event.get("comment") or {}
    check = suggestion.get("self_check") or {}

    flags = []
    if check.get("invented_specifics"):
        flags.append("⚠️ 自检：可能编造了具体信息")
    if not check.get("answers_question"):
        flags.append("⚠️ 自检：未直接回答问题")
    if not check.get("cites_only_kb"):
        flags.append("⚠️ 自检：可能超出知识库范围")
    flag_line = ("\n" + "\n".join(flags)) if flags else ""

    header_color = {"热线索": "red", "温线索": "orange",
                    "冷线索": "blue", "非目标": "grey"}.get(suggestion.get("stage"), "blue")

    body_text = "**【用户评论】**\n> %s\n\n**【AI分析】**\n意图：%s　客户阶段：%s　建议动作：%s\n\n**【AI回复建议】**（可直接修改后点发送）%s" % (
        (comment.get("message") or "（无文字）").replace("\n", " "),
        suggestion.get("intent"), suggestion.get("stage"), suggestion.get("action"), flag_line)

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔔 %s · 新评论待审核" % (page.get("name") or "未知主页")},
            "template": header_color,
        },
        "body": {"elements": [
            _md("**评论者：** %s" % (author.get("name") or "某位用户")),
            _md(_source_line(event)),
            _md(body_text),
            {"tag": "form", "name": "reply_form", "elements": [
                {"tag": "input", "name": "edited_reply", "input_type": "multiline_text",
                 "rows": 4, "default_value": _clip(suggestion.get("reply") or "", CARD_TEXT_LIMIT),
                 "label": {"tag": "plain_text", "content": "回复正文"}},
                {"tag": "button", "name": "send_btn", "type": "primary",
                 "action_type": "form_submit",
                 "text": {"tag": "plain_text", "content": "发送到 Facebook"},
                 "value": {"action": "send", "comment_id": comment.get("id")}},
            ]},
            {"tag": "button", "name": "ignore_btn", "type": "default",
             "text": {"tag": "plain_text", "content": "忽略这条"},
             "value": {"action": "ignore", "comment_id": comment.get("id")}},
        ]},
    }


def build_done_card(event, suggestion, result_text, tone="green"):
    page = event.get("page") or {}
    author = event.get("author") or {}
    comment = event.get("comment") or {}
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "%s · %s" % (
                page.get("name") or "未知主页", result_text.split("\n")[0][:40])},
            "template": tone,
        },
        "body": {"elements": [
            _md("**评论者：** %s" % (author.get("name") or "某位用户")),
            _md(_source_line(event)),
            _md("**原评论：**\n> %s" % ((comment.get("message") or "（无文字）").replace("\n", " "))),
            _md(result_text),
        ]},
    }



def build_alert_card(event, detail):
    """重试链全部失败时的告警卡。无按钮：这不是「待审核」，是「待人工处理」。

    存在意义：以前生成失败只是记日志、事件被丢弃，客户留言无声消失。
    """
    page = event.get("page") or {}
    author = event.get("author") or {}
    comment = event.get("comment") or {}
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": "⚠️ %s · AI 生成失败，需人工处理" % (page.get("name") or "未知主页")},
            "template": "red",
        },
        "body": {"elements": [
            _md("**评论者：** %s" % (author.get("name") or "某位用户")),
            _md(_source_line(event)),
            _md("**用户评论：**\n> %s" % ((comment.get("message") or "（无文字）").replace("\n", " "))),
            _md("**失败详情：**\n```\n%s\n```" % str(detail)[:700]),
            _md("这条留言**没有生成任何回复建议**，请人工决定如何处理。"
                "原始事件仍在 spool 里，修好后可重放。"),
        ]},
    }


def build_notice_card(event, note, tone="grey"):
    """纯告知卡片：不需要任何操作，也没有按钮。

    用于被判定为「无需回复」但用户可能想知道的留言（如收尾语「谢谢」）。
    刻意不调用 LLM —— 回复「谢谢」不值得花 token 去生成「不客气」。
    """
    page = event.get("page") or {}
    author = event.get("author") or {}
    comment = event.get("comment") or {}
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": "💬 %s · 无需回复的留言" % (page.get("name") or "未知主页")},
            "template": tone,
        },
        "body": {"elements": [
            _md("**评论者：** %s" % (author.get("name") or "某位用户")),
            _md("**内容：**\n> %s" % ((comment.get("message") or "（无文字）").replace("\n", " "))),
            _md(note),
        ]},
    }


def send_card(card, to=None):
    out = _request("/im/v1/messages?receive_id_type=chat_id", {
        "receive_id": to or chat_id(),
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }, token=tenant_token())
    if out.get("code") != 0:
        raise RuntimeError("发卡片失败: %s" % out)
    return out["data"]["message_id"]


def update_card(message_id, card):
    out = _request("/im/v1/messages/%s" % message_id,
                   {"content": json.dumps(card, ensure_ascii=False)},
                   method="PATCH", token=tenant_token())
    return out.get("code") == 0, out


def send_text(text, to=None):
    out = _request("/im/v1/messages?receive_id_type=chat_id", {
        "receive_id": to or chat_id(), "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }, token=tenant_token())
    return out


def send_text_to(open_id, text):
    """给指定用户发私聊文本。

    用途：「复制建议回复」按钮 —— 飞书卡片**不接受**原生 copy 行为
    （实测 code 200621 unknown behavior type），改把原文私聊发给点击者，
    手机上长按即可复制。
    """
    return _request("/im/v1/messages?receive_id_type=open_id", {
        "receive_id": open_id, "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }, token=tenant_token())


LEAD_TEMPLATE = {"高": "red", "中": "orange", "低": "blue"}
LEAD_BADGE = {"高": "🔴 高", "中": "🟠 中", "低": "🔵 低"}


def build_lead_card(page_name, display, analysis, record_url):
    """询盘线索卡片。

    刻意不含任何「会改状态」的按钮：线索不是待发布内容，
    不需要「发送/忽略」这种审批动作，销售看表格跟进即可。
    """
    priority = (analysis.get("priority") or "中").strip()
    badge = LEAD_BADGE.get(priority, "🟠 中")
    template = LEAD_TEMPLATE.get(priority, "orange")

    # 产品需求不上卡片：多数填「其他/待确认」，且 AI 分析里已重新解读过。
    # 原始值仍写多维表格（产品需求字段），不受影响。
    head = ("**👤 客户：** %s　**🏢 公司：** %s\n"
            "**🌏 国家/地区：** %s　**📅 提交：** %s") % (
        display.get("name"), display.get("company"),
        display.get("country"), display.get("submitted"))
    head += "\n**📮 联系方式：** %s" % display.get("contacts")

    # 卡片空间有限：依据/缺失各截一段，完整内容看多维表格。
    _reason = (analysis.get("reason") or "待确认").replace("\n", " ").strip()
    _missing = (analysis.get("missing") or "待确认").replace("\n", " ").strip()
    if len(_reason) > 28:
        _reason = _reason[:28] + "…"
    if len(_missing) > 10:
        _missing = _missing[:10] + "…"
    ai = ("**摘要：** %s\n**优先级：** %s\n**依据：** %s　**缺失：** %s"
          % (analysis.get("summary") or "待确认", badge, _reason, _missing))

    reply_zh = (analysis.get("suggested_reply") or "（无）").strip()
    reply_en = (analysis.get("suggested_reply_en") or "").strip()

    elements = [
        _md(head),
        {"tag": "hr"},
        _md("**🤖 AI 分析**\n" + ai),
        _md("**💬 建议回复（中文）**\n> %s" % reply_zh.replace("\n", "\n> ")),
    ]
    elements.append({"tag": "button", "name": "copy_btn", "type": "primary",
                     "text": {"tag": "plain_text", "content": "📨 发送英文回复到我的私聊"},
                     "value": {"action": "lead_copy",
                               "leadgen_id": display.get("leadgen_id")}})
    if record_url:
        elements.append({"tag": "button", "name": "open_btn", "type": "default",
                         "text": {"tag": "plain_text", "content": "📊 打开多维表格记录"},
                         "behaviors": [{"type": "open_url", "default_url": record_url}]})

    return {"schema": "2.0", "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": "🔔 %s · 新询盘线索" % page_name},
                       "template": template},
            "body": {"elements": elements}}


def build_lead_alert_card(page_name, leadgen_id, record_id, detail):
    """线索处理失败告警。无按钮：这是「待人工处理」，不是「待审批」。"""
    elements = [
        _md("**线索 ID：** `%s`" % leadgen_id),
        _md("**失败详情：**\n```\n%s\n```" % str(detail)[:700]),
    ]
    if record_id:
        elements.append(_md("线索**已经存入多维表格**，只是没生成 AI 分析，"
                            "请直接到表里人工查看。"))
    else:
        elements.append(_md("这条线索**没有写入多维表格**。原始事件仍在 spool 里，"
                            "修好后可重放，不会丢。"))
    return {"schema": "2.0", "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": "⚠️ %s · 询盘处理失败，需人工处理" % page_name},
                       "template": "red"},
            "body": {"elements": elements}}
