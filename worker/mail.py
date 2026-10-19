"""邮箱监控：只读轮询 IMAP，把新客户来信推成飞书通知卡。

设计取舍（每条都是踩过坑的结果）：
- 只读：一律用 BODY.PEEK，不标已读、不移动、不删除，绝不改变邮箱状态
- 首次运行以当前最大 UID 为基线，不倒灌历史邮件（否则一上线就被几千封淹没）
- **同时读 INBOX 与「已发送」**：只看来信会得出「没人处理」的错误结论 ——
  「客户是否已被回复」的权威数据源是 Sent，不是 INBOX
- 去重双保险：UID 水位 + Message-ID 集合，跨重启持久化
- **投递确认**：每封的处理结果都写状态文件，避免「以为推了其实没推」
- 分类不确定一律判为「非垃圾」：漏掉真客户的代价远大于多推一张卡片
- 连续失败要吵：单账号超阈值推告警卡；全部账号长期失败则退出让 systemd 重启
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

from . import config, feishu, llm

log = logging.getLogger("mail")


def _state_dir():
    raw = (os.environ.get("AIW_MAIL_STATE")
           or os.environ.get("STATE_DIRECTORY")
           or os.path.join(config.STATE_DIR, "mail"))
    return raw.split(":")[0]


STATE_DIR = _state_dir()
STATE_FILE = os.path.join(STATE_DIR, "state.json")
FILTERED_LOG = os.path.join(STATE_DIR, "filtered.jsonl")

IMAP_HOST = os.environ.get("AIW_MAIL_IMAP_HOST", "imap.exmail.qq.com")
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


# ---------------- 状态 ----------------

def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("状态文件损坏，按空状态处理（会重做基线，不会重复推送）：%s", exc)
        return {}


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


GLOBAL_SEEN_KEY = "_global_seen"


def _global_seen(state):
    """跨账号去重表：同一封信若同时送到多个监控邮箱，只推一次卡。"""
    return set(state.get(GLOBAL_SEEN_KEY) or [])


def _global_seen_add(state, key):
    g = _global_seen(state)
    g.add(key)
    if len(g) > GLOBAL_SEEN_KEEP:
        g = set(list(g)[-GLOBAL_SEEN_KEEP:])
    state[GLOBAL_SEEN_KEY] = sorted(g)


def _push_rec(bucket, key, rec, limit):
    """往有界列表里追加一条记录（投递确认 / 已发送历史都靠它）。"""
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
            raise RuntimeError("密码文件为空: %s" % acct["password_file"])
        out.append({
            "address": acct["address"],
            "password": password,
            "page_id": str(acct.get("page_id") or ""),
            "label": acct.get("label") or acct["address"],
        })
    return out


# ---------------- 邮件解析 ----------------

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
    re.compile(r"^\s*-{2,}\s*(原始邮件|Original Message)\s*-{2,}", re.I),
    re.compile(r"^\s*(发件人|From)\s*[:：]"),
    re.compile(r"^\s*_{10,}\s*$"),
)


def _trim_quotes(text):
    """砍掉回复引用，只留新内容 —— 否则噪音会淹没模型和卡片。"""
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
        return "(时间未知)"
    if dt is None:
        return "(时间未知)"
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


# ---------------- 内部邮件拦截 ----------------

# 自有域名：发件人属于这些域名时，视为「我们自己发出的邮件」，不推卡片。
# 刻意不从账号表推导 —— 防止将来新增外部代管账号时判定范围被静默放大。
# 配置方式：AIW_MAIL_OWN_DOMAINS="example.com,example.org"
OWN_DOMAINS = tuple(d.strip().lower()
                    for d in os.environ.get("AIW_MAIL_OWN_DOMAINS", "").split(",")
                    if d.strip())


def _domain_of(addr):
    if not addr or "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1].strip().lower()


def is_internal_mail(mail):
    """内部邮件 = 发件人属于自有域名，即「这封信是我们自己发出的」。

    2026-10-13 修订：原规则要求发件人与收件人「同属」自有域名，
    结果我们发给客户的邮件（To 是客户、我们只在 Cc / Bcc）被判成
    外部来信，推了卡片到飞书群。改为只看发件人 —— 我方销售邮箱
    不会收到自己同事的询盘。

    这是确定性规则，不交给 LLM 猜 —— 内部邮件不该消耗模型调用，
    也不该有「模型这次没判对」的风险。
    """
    frm = _domain_of(mail.get("from_addr"))
    if frm in OWN_DOMAINS:
        return True, "内部邮件（我方发出：%s）" % (mail.get("from_addr") or "")
    return False, ""


# ---------------- 分诊 ----------------

CLASSIFY_SYSTEM = """你是外贸公司的邮件分诊助手。收到一封发到公司销售邮箱的邮件，请判断：

1) is_spam：是否为「非客户来信」。属于垃圾的只有这几类：
   B2B 推销与冷邮件（lead generation、代做 SEO、建站、代运营、卖邮箱列表）、
   钓鱼与诈骗、系统通知、广告、订阅退订通知。
   ⚠️ 真实客户的询价、技术咨询、售后、抱怨投诉、合作洽谈、代理申请，
   一律【不算】垃圾。判断不确定时取 false —— 漏掉真客户远比多推一条通知严重。

2) domain：内容主题，只能取以下值之一：
@@DOMAIN_GUIDE@@

3) intent：只能取 "询价" / "技术咨询" / "售后" / "合作" / "其他"
4) urgency：只能取 "高"（有明确采购需求或时限）/ "中" / "低"
5) summary：一句话中文摘要，不超过 60 字，只描述来信内容本身，不得编造。

即使判定为垃圾邮件，domain/intent/summary 也要照实填写。

只输出 JSON，不要任何解释或 markdown 代码块：
{"is_spam": false, "domain": "@@DOMAIN_ONE@@", "intent": "询价", "urgency": "高", "summary": "..."}"""


def _classify_system():
    """把主题枚举与说明填进提示词。

    与 llm.py 用同一套占位符约定：主题清单来自 AIW_DOMAIN_ENUM /
    AIW_DOMAIN_GUIDE，因此换业务只需要改环境变量，不用改代码。
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
                raise RuntimeError("输出被 max_tokens 截断")
            if not content.strip():
                raise RuntimeError("返回空内容")
            text = content.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            return json.loads(text)
        except Exception as exc:
            errors.append("%s: %s" % (model, exc))
    raise RuntimeError("全部模型失败：" + " | ".join(errors))


def classify(mail):
    user = "发件人：%s <%s>\n主题：%s\n正文：\n%s" % (
        mail["from_name"] or "(无名)", mail["from_addr"],
        mail["subject"] or "(无主题)", mail["body"] or "(无正文)")
    out = _llm_json(CLASSIFY_SYSTEM, user)
    out.setdefault("is_spam", False)          # 缺字段时偏向「不是垃圾」
    out.setdefault("domain", "其他")
    out.setdefault("intent", "其他")
    out.setdefault("urgency", "中")
    out.setdefault("summary", "")
    return out


# ---------------- 路由 ----------------

def chat_for_domain(domain, page_id):
    """内容主题优先于收件地址 —— 询盘可能发到另一个业务线的邮箱。"""
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


# ---------------- 卡片 ----------------

_PALETTE = ("blue", "turquoise", "purple", "orange", "green", "wathet")
_DOMAINS = [d.strip() for d in config.DOMAIN_ENUM.split(",") if d.strip()]
_DOMAIN_COLOR = {d: _PALETTE[i % len(_PALETTE)] for i, d in enumerate(_DOMAINS)}


def sent_history(st, addr):
    """在该账号的「已发送」记录里找有没有回过这个地址。"""
    addr = (addr or "").lower()
    if not addr or not st:
        return []
    return [r for r in (st.get("sent") or [])
            if addr in (r.get("to") or "").lower()]


def build_mail_card(acct, mail, triage, st=None, test=False):
    domain = triage.get("domain") or "其他"
    title = "📧 新客户来信 · %s" % domain
    if test:
        title = "🧪 测试 · " + title
    lines = ["**收件箱：** %s" % acct["label"]]
    lines.append("**发件人：** %s" % (mail["from_name"] or "(无名)"))
    lines.append("**邮箱：** `%s`" % mail["from_addr"])
    lines.append("**时间：** %s（北京时间）" % mail["local_time"])
    if mail["subject"]:
        lines.append("**主题：** %s" % mail["subject"])
    body = (mail["body"] or "（无正文）")[:CARD_BODY_CHARS]
    lines.append("**正文：**\n> %s" % body.replace("\n", "\n> "))
    lines.append("---")
    lines.append("**AI 分诊：** 主题=%s　意图=%s　紧急度=%s" % (
        domain, triage.get("intent") or "其他", triage.get("urgency") or "中"))
    if triage.get("summary"):
        lines.append(triage["summary"])
    hist = sent_history(st, mail["from_addr"])
    if hist:
        lines.append("**往来记录：** 已发送里查到此地址 **%d** 次，最近一次 %s"
                     % (len(hist), hist[-1].get("ts") or "?"))
    else:
        lines.append("**往来记录：** 已发送里查不到对此地址的回复（疑似首次）")
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title},
                   "template": _DOMAIN_COLOR.get(domain, "grey")},
        "body": {"elements": [feishu._md(x) for x in lines]},
    }


def _log_filtered(acct, mail, triage, reason):
    """被过滤的邮件必须留痕 —— 分类错了要能查回来，不能凭空消失。"""
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
           "account": acct["address"], "label": acct["label"],
           "from": "%s <%s>" % (mail["from_name"], mail["from_addr"]),
           "subject": mail["subject"], "reason": reason,
           "triage": triage, "preview": mail["body"][:200]}
    try:
        with open(FILTERED_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("写过滤日志失败：%s", exc)


# ---------------- IMAP ----------------

def _connect(acct):
    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx, timeout=30)
    M.login(acct["address"], acct["password"])
    return M


def _find_sent_folder(M):
    """名字带空格的文件夹在 imaplib 里必须加引号，否则 BAD EXAMINE parameters!"""
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
            log.info("[%s] 收件箱首次运行，基线 UID=%s，不回推历史邮件", addr, top)
            return 0

        floor = 0 if opts.get("backfill") else int(st.get("last_uid") or 0)
        pending = [u for u in uids if u > floor]
        if len(pending) > MAX_FETCH:
            log.warning("[%s] 待处理 %d 封，本轮只取最早 %d 封（防雪崩）",
                        addr, len(pending), MAX_FETCH)
            pending = pending[:MAX_FETCH]

        seen = set(st.get("seen") or [])
        gseen = _global_seen(state)
        retry = st.setdefault("retry", {})
        for uid in pending:
            raw = _fetch_raw(M, uid)
            if not raw:
                log.warning("[%s] UID %s 取不到内容，跳过", addr, uid)
                st["last_uid"] = uid
                _save_state(state)
                continue

            mail = _parse(raw, uid)
            key = mail["message_id"] or ("uid:%s@%s" % (uid, addr))
            if key in seen or key in gseen:
                where = "本账号" if key in seen else "其他账号"
                log.info("[%s] UID %s 已在%s处理过（%s），跳过",
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
                log.info("[%s] UID %s %s，只记日志不推卡片：%s",
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
                log.info("[%s] UID %s 判定为推销/垃圾，只记日志不推卡片：%s",
                         addr, uid, (mail["subject"] or mail["from_addr"])[:60])
            else:
                to, how = chat_for_domain(triage.get("domain"), acct["page_id"])
                card = build_mail_card(acct, mail, triage, st)
                if opts.get("dry_run"):
                    log.info("[%s] DRY-RUN 本应推送：%s", addr,
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
                        log.info("[%s] 已推送来信：%s ｜ %s ｜ 群=%s(%s) ｜ message_id=%s",
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
                        log.error("[%s] UID %s 推送失败（第 %d 次）：%s",
                                  addr, uid, attempts, exc)
                        if give_up:
                            log.error("[%s] UID %s 连续 %d 次失败，放弃重试。"
                                      "该来信已记录在 processed，需人工补看：%s ｜ %s",
                                      addr, uid, attempts, mail["from_addr"],
                                      mail["subject"])
                        else:
                            done = False   # 不推进水位，下轮重试

            if done:
                seen.add(key)
                _global_seen_add(state, key)
                st["last_uid"] = uid
                st["seen"] = list(seen)[-SEEN_KEEP:]
                retry.pop(str(uid), None)
                _save_state(state)
            else:
                _save_state(state)
                break          # 停在失败这一封，避免后面的推进把水位带过去
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return new_count


def _poll_sent(acct, state, opts):
    """读「已发送」，记录我们回过谁 —— 只看 INBOX 会误判「没人处理」。"""
    addr = acct["address"]
    st = state.setdefault(addr, {})
    M = _connect(acct)
    recorded = 0
    try:
        folder = _find_sent_folder(M)
        if not folder:
            log.warning("[%s] 找不到已发送文件夹，跳过", addr)
            return 0
        typ, data = M.select('"%s"' % folder, readonly=True)
        if typ != "OK":
            log.warning("[%s] 打开已发送失败：%s", addr, data)
            return 0
        uids = _uids(M)
        top = max(uids) if uids else 0
        if "sent_uid" not in st:
            pending = uids[-MAX_FETCH:]
            log.info("[%s] 已发送首次运行：回读最近 %d 封建立往来记录", addr, len(pending))
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
            log.info("[%s] 已发送记录更新 %d 封（累计 %d 条）",
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
        # 已发送读失败不该拖垮来信推送 —— 记下来但不算轮询失败
        log.error("[%s] 已发送读取失败（不影响来信推送）：%s", acct["address"], exc)
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
            log.error("[%s] 轮询失败（连续 %d 次）：%s", addr, rec["fails"], exc)
            now = time.time()
            if rec["fails"] >= ALERT_AFTER and now - rec.get("alert_at", 0) > ALERT_COOLDOWN:
                rec["alert_at"] = now
                try:
                    feishu.send_card(_alert_card(acct, exc, rec["fails"]))
                    log.info("[%s] 已推送监控告警卡", addr)
                except Exception as inner:
                    log.error("[%s] 告警卡也发不出去：%s", addr, inner)
    # 全部账号都长期失败 -> 服务已无意义，退出让 systemd 重启
    if accounts and all(counters.get(a["address"], {}).get("fails", 0) >= EXIT_AFTER
                        for a in accounts):
        log.error("全部账号连续失败 ≥ %d 次，退出让 systemd 重启", EXIT_AFTER)
        sys.exit(1)


def _alert_card(acct, exc, fails):
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text",
                             "content": "🔴 邮箱监控异常 · %s" % acct["label"]},
                   "template": "red"},
        "body": {"elements": [
            feishu._md("**收件箱：** `%s`" % acct["address"]),
            feishu._md("**连续失败：** %d 次" % fails),
            feishu._md("**错误：**\n> %s" % str(exc)[:400]),
            feishu._md("新的客户来信可能收不到。常见原因："
                       "密码被改、后台关闭了 IMAP、账号被限制登录。"),
        ]},
    }


# ---------------- 入口 ----------------

def _sample():
    return {
        "uid": 0, "message_id": "<sample@local>",
        "subject": "Inquiry: pricing and lead time",
        "from_name": "测试发件人（非真实客户）",
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
            "已发送记录数": len(st.get("sent") or []),
            "处理统计": by,
            "最近 3 条": proc[-3:],
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
    log.info("邮箱监控启动：%d 个账号（%s），轮询间隔 %d 秒，状态目录 %s",
             len(accounts), "、".join(a["label"] for a in accounts),
             POLL_SECONDS, STATE_DIR)

    if "--test-card" in argv:
        acct = accounts[0]
        demo_domain = _DOMAINS[0] if _DOMAINS else "other"
        triage = {"domain": demo_domain, "intent": "询价", "urgency": "高",
                  "summary": "客户询问价格与交期（本条为测试）"}
        to, how = chat_for_domain(demo_domain, acct["page_id"])
        mid = feishu.send_card(
            build_mail_card(acct, _sample(), triage, None, test=True), to=to)
        log.info("测试卡已发送：群=%s(%s) message_id=%s", to[:12], how, mid)
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
            log.error("本轮异常：%s", exc)
        if opts["once"]:
            break
        time.sleep(max(5.0, POLL_SECONDS - (time.time() - begin)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
