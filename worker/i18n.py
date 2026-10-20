"""Card and UI labels.

The repository ships English by default. Deployments that need Chinese operator
cards can set AIW_CARD_LANG=zh without touching the code.
"""
import os

CARD_LANG = (os.environ.get("AIW_CARD_LANG", "en").strip().lower() or "en")
if CARD_LANG not in ("en", "zh"):
    CARD_LANG = "en"


LABELS = {
    "en": {
        # common
        "unknown_page": "Unknown Page",
        "unknown_user": "a user",
        "no_text": "(no text)",
        "no_value": "(none)",
        "tbd": "To be confirmed",
        # source line
        "source": "**Source:** ",
        "source_unknown": "**Source:** unknown",
        "post_type": "Post type: ",
        "open_post": "[Open post](%s)",
        "post_id": "Post ID: `%s`",
        "comment_time": "Commented at: %s (Beijing time)",
        "type_photo": "Photo post",
        "type_video": "Video/Reel",
        "type_text": "Text post",
        "type_shared": "Shared post",
        # review card
        "review_title": "🔔 %s · New comment to review",
        "commenter": "**Commenter:** %s",
        "review_body": "**[User comment]**\n> %s\n\n**[AI analysis]**\nIntent: %s　Stage: %s　Suggested action: %s\n\n**[Suggested reply]** (edit below, then send)%s",
        "reply_label": "Reply text",
        "send": "Send to Facebook",
        "ignore": "Ignore this one",
        "flag_invented": "⚠️ Self-check: may have invented specifics",
        "flag_unanswered": "⚠️ Self-check: did not answer the question directly",
        "flag_outside_kb": "⚠️ Self-check: may go beyond the KB",
        # done card
        "done_title": "%s · %s",
        "original_comment": "**Original comment:**\n> %s",
        # alert card
        "alert_title": "⚠️ %s · AI generation failed, manual handling needed",
        "user_comment": "**User comment:**\n> %s",
        "failure_detail": "**Failure detail:**\n```\n%s\n```",
        "alert_body": "This comment **did not produce any reply suggestion**. Please decide manually. The original event is still in the spool and can be replayed after the fix.",
        # notice card
        "notice_title": "💬 %s · No reply needed",
        "content": "**Content:**\n> %s",
        "notice_closing": "This is a conversational closing (e.g. \"thanks\", \"got it\", \"OK\") — **no reply needed**.\n\nReplying would start a \"thank you\" loop, so the system stays silent.\nThis card is informational only; no action is required.",
        # lead card
        "lead_title": "🔔 %s · New lead",
        "lead_customer": "**👤 Customer:** %s　**🏢 Company:** %s\n**🌏 Country/Region:** %s　**📅 Submitted:** %s",
        "lead_contact": "**📮 Contact:** %s",
        "lead_ai": "**🤖 AI analysis**\n",
        "lead_summary": "**Summary:** %s\n**Priority:** %s\n**Evidence:** %s　**Missing:** %s",
        "lead_reply": "**💬 Suggested reply (Chinese)**\n> %s",
        "lead_copy": "📨 Send English reply to my DM",
        "lead_open": "📊 Open Bitable record",
        "lead_id": "**Lead ID:** `%s`",
        "lead_stored": "The lead **is already in the Bitable**, but AI analysis was not generated. Please check it manually.",
        "lead_not_stored": "This lead **was not written to the Bitable**. The original event is still in the spool and can be replayed after the fix — nothing is lost.",
        "lead_alert_title": "⚠️ %s · Lead processing failed, manual handling needed",
        # toasts / status
        "toast_sent": "Sent to Facebook",
        "toast_ignored": "Ignored",
        "toast_dry_run": "Simulated send (dry-run)",
        "toast_sending": "Sending…",
        "toast_error": "Something went wrong, please retry",
        "toast_handled": "Already handled",
        "toast_not_allowed": "You are not allowed to review",
        "toast_sending_dm": "Sending the reply text to your DM…",
        "empty_reply": "The reply text is empty",
        "toast_lead_expired": "The original reply text for this lead has expired. Check it in the Bitable",
        "toast_unknown": "Unknown operation",
        # done results
        "result_dry_run": "**Simulated send** (dry-run; nothing was published to Facebook)",
        "result_sent": "**Published to Facebook** ✅　New comment ID: `%s`",
        "result_content": "%s\n\n**Content posted:**\n> %s",
        "result_failed": "**Publishing failed** ❌\n\n```\n%s\n```\n\n**Original draft (not posted):**\n> %s",
        "result_ignored": "**Ignored** — nothing was published.",
        "done_sent": "✅ Handled (sent) · %s",
        "done_ignored": "✅ Handled (ignored) · %s",
    },
    "zh": {
        "unknown_page": "未知主页",
        "unknown_user": "某位用户",
        "no_text": "（无文字）",
        "no_value": "（无）",
        "tbd": "待确认",
        "source": "**来源：** ",
        "source_unknown": "**来源：** 未知",
        "post_type": "帖子类型：",
        "open_post": "[打开原帖](%s)",
        "post_id": "帖子 ID：`%s`",
        "comment_time": "留言时间：%s（北京时间）",
        "type_photo": "图文帖",
        "type_video": "视频帖/Reel",
        "type_text": "文字帖",
        "type_shared": "转发帖",
        "review_title": "🔔 %s · 新评论待审核",
        "commenter": "**评论者：** %s",
        "review_body": "**【用户评论】**\n> %s\n\n**【AI分析】**\n意图：%s　客户阶段：%s　建议动作：%s\n\n**【AI回复建议】**（可直接修改后点发送）%s",
        "reply_label": "回复正文",
        "send": "发送到 Facebook",
        "ignore": "忽略这条",
        "flag_invented": "⚠️ 自检：可能编造了具体信息",
        "flag_unanswered": "⚠️ 自检：未直接回答问题",
        "flag_outside_kb": "⚠️ 自检：可能超出知识库范围",
        "done_title": "%s · %s",
        "original_comment": "**原评论：**\n> %s",
        "alert_title": "⚠️ %s · AI 生成失败，需人工处理",
        "user_comment": "**用户评论：**\n> %s",
        "failure_detail": "**失败详情：**\n```\n%s\n```",
        "alert_body": "这条留言**没有生成任何回复建议**，请人工决定如何处理。原始事件仍在 spool 里，修好后可重放。",
        "notice_title": "💬 %s · 无需回复的留言",
        "content": "**内容：**\n> %s",
        "notice_closing": "这条属于对话收尾语（如「谢谢」「好的」「收到」）—— **无需回复**。\n\n回复它只会形成「你谢我、我谢你」的来回刷屏，所以系统主动不回。\n本卡片仅作告知，不需要任何操作。",
        "lead_title": "🔔 %s · 新询盘线索",
        "lead_customer": "**👤 客户：** %s　**🏢 公司：** %s\n**🌏 国家/地区：** %s　**📅 提交：** %s",
        "lead_contact": "**📮 联系方式：** %s",
        "lead_ai": "**🤖 AI 分析**\n",
        "lead_summary": "**摘要：** %s\n**优先级：** %s\n**依据：** %s　**缺失：** %s",
        "lead_reply": "**💬 建议回复（中文）**\n> %s",
        "lead_copy": "📨 发送英文回复到我的私聊",
        "lead_open": "📊 打开多维表格记录",
        "lead_id": "**线索 ID：** `%s`",
        "lead_stored": "线索**已经存入多维表格**，只是没生成 AI 分析，请直接到表里人工查看。",
        "lead_not_stored": "这条线索**没有写入多维表格**。原始事件仍在 spool 里，修好后可重放，不会丢。",
        "lead_alert_title": "⚠️ %s · 询盘处理失败，需人工处理",
        "toast_sent": "已发送",
        "toast_ignored": "已忽略",
        "toast_dry_run": "已模拟发送",
        "toast_sending": "正在发送…",
        "toast_error": "处理出错，请重试",
        "toast_handled": "这条已经处理过了",
        "toast_not_allowed": "你没有审核权限",
        "toast_sending_dm": "正在把回复原文发到你的私聊…",
        "empty_reply": "回复正文是空的",
        "toast_lead_expired": "这条线索的回复原文已过期，请到多维表格查看",
        "toast_unknown": "未知操作",
        "result_dry_run": "**已模拟发送**（当前为 dry-run，未真正发布到 Facebook）",
        "result_sent": "**已发送到 Facebook** ✅　新评论 ID：`%s`",
        "result_content": "%s\n\n**发出的内容：**\n> %s",
        "result_failed": "**发布失败** ❌\n\n```\n%s\n```\n\n**原草稿（未发出）：**\n> %s",
        "result_ignored": "**已忽略** —— 未发布任何回复。",
        "done_sent": "✅ 已处理（发送）· %s",
        "done_ignored": "✅ 已处理（忽略）· %s",
    },
}

_L = LABELS.get(CARD_LANG, LABELS["en"])


def T(key):
    return _L.get(key, LABELS["en"].get(key, key))


_STAGE = {
    "en": {"hot": "Hot lead", "warm": "Warm lead", "cold": "Cold lead", "not_target": "Not a target"},
    "zh": {"hot": "热线索", "warm": "温线索", "cold": "冷线索", "not_target": "非目标"},
}
_INTENT = {
    "en": {"quote": "Quote", "technical": "Technical", "after_sales": "After-sales",
           "partnership": "Partnership", "spam": "Spam", "other": "Other"},
    "zh": {"quote": "询价", "technical": "技术咨询", "after_sales": "售后",
           "partnership": "合作", "spam": "垃圾信息", "other": "其他"},
}
_ACTION = {
    "en": {"reply": "Reply", "forward_to_sales": "Forward to sales", "ignore": "Ignore"},
    "zh": {"reply": "回复", "forward_to_sales": "转销售", "ignore": "忽略"},
}
_PRIORITY = {
    "en": {"high": "High", "medium": "Medium", "low": "Low"},
    "zh": {"high": "高", "medium": "中", "low": "低"},
}


# Older records may still store the previous Chinese enum values.
# Accept both forms on read; never rewrite data in place.

_ALIASES = {
    "intent": {"询价": "quote", "技术咨询": "technical", "售后": "after_sales",
               "合作": "partnership", "垃圾信息": "spam", "其他": "other"},
    "stage": {"冷线索": "cold", "温线索": "warm", "热线索": "hot", "非目标": "not_target"},
    "action": {"回复": "reply", "转销售": "forward_to_sales", "忽略": "ignore"},
    "priority": {"高": "high", "中": "medium", "低": "low"},
}


def code(kind, value):
    """Normalise a stored enum value to its language-neutral code."""
    return _ALIASES.get(kind, {}).get(value, value)


def label(kind, code_value):
    table = {"stage": _STAGE, "intent": _INTENT, "action": _ACTION, "priority": _PRIORITY}[kind]
    lang = CARD_LANG if CARD_LANG in table else "en"
    normalised = code(kind, code_value)
    return table[lang].get(normalised, normalised)


def priority_code(value):
    """Lead analysis still emits 高/中/低; normalise either form to a code."""
    return code("priority", value)
