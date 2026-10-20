"""飞书侧：发卡片、更新卡片、拿 tenant_access_token。"""
import datetime, json, os, time, urllib.error, urllib.request

from . import config, i18n

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


# ---------- Card construction ----------

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
    kind = {"added_photos": i18n.T("type_photo"),
            "added_video": i18n.T("type_video"),
            "created": i18n.T("type_text"),
            "shared_story": i18n.T("type_shared")}.get(st)
    if kind:
        bits.append(i18n.T("post_type") + kind)
    url = post.get("permalink_url") or comment.get("permalink_url")
    if url:
        bits.append(i18n.T("open_post") % url)
    elif post.get("id"):
        bits.append(i18n.T("post_id") % post.get("id"))
    ts = comment.get("created_time")
    if ts:
        try:
            dt = datetime.datetime.fromtimestamp(
                int(ts), datetime.timezone(datetime.timedelta(hours=8)))
            bits.append(i18n.T("comment_time") % dt.strftime("%Y-%m-%d %H:%M"))
        except Exception:
            pass
    return i18n.T("source") + "　".join(bits) if bits else i18n.T("source_unknown")


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
    if check.get("invented_specifics") is True:
        flags.append(i18n.T("flag_invented"))
    if check.get("answers_question") is False:
        flags.append(i18n.T("flag_unanswered"))
    if check.get("cites_only_kb") is False:
        flags.append(i18n.T("flag_outside_kb"))
    flag_line = ("\n" + "\n".join(flags)) if flags else ""

    header_color = {"hot": "red", "warm": "orange",
                    "cold": "blue", "not_target": "grey"}.get(
        i18n.code("stage", suggestion.get("stage")), "blue")

    body_text = i18n.T("review_body") % (
        (comment.get("message") or i18n.T("no_text")).replace("\n", " "),
        i18n.label("intent", suggestion.get("intent")),
        i18n.label("stage", suggestion.get("stage")),
        i18n.label("action", suggestion.get("action")),
        flag_line)

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": i18n.T("review_title") % (page.get("name") or i18n.T("unknown_page"))},
            "template": header_color,
        },
        "body": {"elements": [
            _md(i18n.T("commenter") % (author.get("name") or i18n.T("unknown_user"))),
            _md(_source_line(event)),
            _md(body_text),
            {"tag": "form", "name": "reply_form", "elements": [
                {"tag": "input", "name": "edited_reply", "input_type": "multiline_text",
                 "rows": 4, "default_value": _clip(suggestion.get("reply") or "", CARD_TEXT_LIMIT),
                 "label": {"tag": "plain_text", "content": i18n.T("reply_label")}},
                {"tag": "button", "name": "send_btn", "type": "primary",
                 "action_type": "form_submit",
                 "text": {"tag": "plain_text", "content": i18n.T("send")},
                 "value": {"action": "send", "comment_id": comment.get("id")}},
            ]},
            {"tag": "button", "name": "ignore_btn", "type": "default",
             "text": {"tag": "plain_text", "content": i18n.T("ignore")},
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
            "title": {"tag": "plain_text", "content": i18n.T("done_title") % (
                page.get("name") or i18n.T("unknown_page"), result_text.split("\n")[0][:40])},
            "template": tone,
        },
        "body": {"elements": [
            _md(i18n.T("commenter") % (author.get("name") or i18n.T("unknown_user"))),
            _md(_source_line(event)),
            _md(i18n.T("original_comment") % (
                (comment.get("message") or i18n.T("no_text")).replace("\n", " "))),
            _md(result_text),
        ]},
    }


def build_alert_card(event, detail):
    """重试链全部失败时的告警卡片。无按钮：需人工处理。"""
    page = event.get("page") or {}
    author = event.get("author") or {}
    comment = event.get("comment") or {}
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": i18n.T("alert_title") % (page.get("name") or i18n.T("unknown_page"))},
            "template": "red",
        },
        "body": {"elements": [
            _md(i18n.T("commenter") % (author.get("name") or i18n.T("unknown_user"))),
            _md(_source_line(event)),
            _md(i18n.T("user_comment") % (
                (comment.get("message") or i18n.T("no_text")).replace("\n", " "))),
            _md(i18n.T("failure_detail") % str(detail)[:700]),
            _md(i18n.T("alert_body")),
        ]},
    }


def build_notice_card(event, note, tone="grey"):
    """信息卡：不需要操作，无按钮。"""
    page = event.get("page") or {}
    author = event.get("author") or {}
    comment = event.get("comment") or {}
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": i18n.T("notice_title") % (page.get("name") or i18n.T("unknown_page"))},
            "template": tone,
        },
        "body": {"elements": [
            _md(i18n.T("commenter") % (author.get("name") or i18n.T("unknown_user"))),
            _md(i18n.T("content") % (
                (comment.get("message") or i18n.T("no_text")).replace("\n", " "))),
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
    """向指定用户发一条私信纯文本。

    用途：「复制建议回复」按钮 —— Lark 卡片**不接受**原生复制行为
    （实测 code 200621 unknown behavior type），所以改为把原文以私信发给点击者，
    手机上长按即可复制。
    """
    return _request("/im/v1/messages?receive_id_type=open_id", {
        "receive_id": open_id, "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }, token=tenant_token())


def _priority_badge(code):
    icon = {"high": "🔴", "medium": "🟠", "low": "🔵"}.get(code, "🟠")
    return icon + " " + i18n.label("priority", code)


def build_lead_card(page_name, display, analysis, record_url):
    """线索卡片。无状态变更按钮：销售在多维表格里跟进。"""
    priority = i18n.priority_code((analysis.get("priority") or "medium").strip())
    badge = _priority_badge(priority)
    template = {"high": "red", "medium": "orange", "low": "blue"}.get(priority, "orange")

    head = i18n.T("lead_customer") % (
        display.get("name"), display.get("company"),
        display.get("country"), display.get("submitted"))
    head += "\n" + i18n.T("lead_contact") % display.get("contacts")

    _reason = (analysis.get("reason") or i18n.T("tbd")).replace("\n", " ").strip()
    _missing = (analysis.get("missing") or i18n.T("tbd")).replace("\n", " ").strip()
    if len(_reason) > 28:
        _reason = _reason[:28] + "…"
    if len(_missing) > 10:
        _missing = _missing[:10] + "…"
    ai = i18n.T("lead_summary") % (
        analysis.get("summary") or i18n.T("tbd"), badge, _reason, _missing)

    reply_zh = (analysis.get("suggested_reply") or i18n.T("no_value")).strip()

    elements = [
        _md(head),
        {"tag": "hr"},
        _md(i18n.T("lead_ai") + ai),
        _md(i18n.T("lead_reply") % reply_zh.replace("\n", "\n> ")),
    ]
    elements.append({"tag": "button", "name": "copy_btn", "type": "primary",
                     "text": {"tag": "plain_text", "content": i18n.T("lead_copy")},
                     "value": {"action": "lead_copy",
                               "leadgen_id": display.get("leadgen_id")}})
    if record_url:
        elements.append({"tag": "button", "name": "open_btn", "type": "default",
                         "text": {"tag": "plain_text", "content": i18n.T("lead_open")},
                         "behaviors": [{"type": "open_url", "default_url": record_url}]})

    return {"schema": "2.0", "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": i18n.T("lead_title") % page_name},
                       "template": template},
            "body": {"elements": elements}}


def build_lead_alert_card(page_name, leadgen_id, record_id, detail):
    """线索处理失败告警。无按钮：需人工处理。"""
    elements = [
        _md(i18n.T("lead_id") % leadgen_id),
        _md(i18n.T("failure_detail") % str(detail)[:700]),
    ]
    elements.append(_md(i18n.T("lead_stored") if record_id else i18n.T("lead_not_stored")))
    return {"schema": "2.0", "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": i18n.T("lead_alert_title") % page_name},
                       "template": "red"},
            "body": {"elements": elements}}
