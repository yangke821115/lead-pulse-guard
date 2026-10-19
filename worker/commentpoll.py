#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Facebook 评论兜底轮询。

背景：webhook 投递已被证实会丢事件（同一天进 5 条、只到 2 条）。评论链路
目前没有观察到丢失，但同类风险存在，加一层兜底。

做法：枚举主页近期帖子 -> 取每帖评论 -> 挑出「网关从没见过的」那些，
把它们**当成一条 Meta 投递**重新 POST 给网关（带合法的
X-Hub-Signature-256）。这样验签、自身评论过滤、去重、线程深度、收尾语
识别全部由网关负责 —— 本脚本不复制任何一条业务规则。

幂等：网关的 spool 本身就是账本，凡是它处理过的事件都在里面。
轮询前先读 spool（含轮转的 .1），已出现过的 comment_id 一律跳过。

用法：commentpoll.py [--dry-run]
退出码：0 正常；1 两个主页都读取失败
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

VER = "v21.0"
TOKEN_DIR = config.GRAPH_SECRETS
APP_SECRET_FILE = os.environ.get(
    "AIW_APP_SECRET_FILE", os.path.join(config.SECRETS, "app-secret"))
SPOOL = config.SPOOL_FILE
HOOK = "http://127.0.0.1:9000/webhook"

WINDOW = 7 * 86400      # 只兜底最近 7 天的评论：更老的属历史遗留，不该现在补推
POST_LIMIT = 25         # 每个主页看最近多少条帖子
DRY = "--dry-run" in sys.argv

PAGES = [(pid, name) for pid, name, _kb in config.pages()]


def log(msg):
    print(msg, flush=True)


def to_epoch(v):
    """评论接口返回的 created_time 是 ISO 字符串，webhook 里是 unix 整数 —— 两种都收。"""
    if isinstance(v, (int, float)):
        return int(v)
    if not v:
        return 0
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp())
        except Exception:
            continue
    return 0


def graph(path, token, **params):
    url = "https://graph.facebook.com/%s%s?%s" % (
        VER, path, urllib.parse.urlencode(params))
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def seen_ids():
    """网关处理过的 comment_id 全集（spool 即账本）。"""
    out = set()
    for path in (SPOOL + ".1", SPOOL):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                cid = ((ev.get("comment") or {}).get("id"))
                if cid:
                    out.add(str(cid))
    return out


def post_to_gateway(secret, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), body,
                               hashlib.sha256).hexdigest()
    req = urllib.request.Request(HOOK, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Hub-Signature-256", sig)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        return "ERR:%r" % (exc,)


def main():
    secret = open(APP_SECRET_FILE).read().strip()
    known = seen_ids()
    cutoff = int(time.time()) - WINDOW
    failed, scanned, injected = 0, 0, 0

    for page_id, page_name in PAGES:
        try:
            token = open(os.path.join(
                TOKEN_DIR, "page-token-%s" % page_id)).read().strip()
        except Exception as exc:
            log("[WARN] %s 读不到令牌：%r" % (page_name, exc))
            failed += 1
            continue

        try:
            feed = graph("/%s/posts" % page_id, token,
                         fields=("id,created_time,comments.limit(100)"
                                 "{id,from,message,created_time,parent{id},permalink_url}"),
                         limit=str(POST_LIMIT))
        except Exception as exc:
            log("[WARN] %s 帖子列表读取失败：%r" % (page_name, exc))
            failed += 1
            continue

        for post in feed.get("data") or []:
            # 评论由帖列表的嵌套展开内联返回（comments.limit(100){...}），
            # 不再逐个帖子单独请求 —— 每轮调用数从 1+N 降到 1。
            page = post.get("comments") or {}
            if not page.get("data"):
                continue

            for c in page.get("data") or []:
                cid = str(c.get("id"))
                scanned += 1
                if cid in known:
                    continue
                sender = c.get("from") or {}
                if str(sender.get("id")) == page_id:      # 我们自己发的
                    continue
                created = to_epoch(c.get("created_time"))
                if created and created < cutoff:
                    continue
                payload = {"object": "page", "entry": [{
                    "id": page_id, "time": created, "changes": [{
                        "field": "feed", "value": {
                            "item": "comment", "verb": "add",
                            "comment_id": cid,
                            "parent_id": (c.get("parent") or {}).get("id") or post["id"],
                            "post_id": post["id"],
                            "message": c.get("message") or "",
                            "created_time": created,
                            "permalink_url": c.get("permalink_url"),
                            "from": {"id": str(sender.get("id")),
                                     "name": sender.get("name")},
                            "post": {"id": post["id"], "status_type": "",
                                     "permalink_url": "", "is_published": True},
                        }}]}]}
                if DRY:
                    log("  缺 %s（%s，%s）" % (cid, page_name,
                                              (c.get("message") or "")[:40]))
                    injected += 1
                    continue
                status = post_to_gateway(secret, payload)
                if status == 200:
                    injected += 1
                    log("补投 %s（%s，%s）-> 网关 %s" % (cid, page_name,
                          (c.get("message") or "")[:40], status))
                else:
                    log("[ERROR] 补投 %s 失败：网关返回 %s" % (cid, status))

    if DRY:
        log("Facebook 侧扫描 %d 条评论，其中网关没见过的 %d 条" % (scanned, injected))
    elif injected:
        log("本轮补投 %d 条（共扫描 %d 条）" % (injected, scanned))
    return 1 if failed >= len(PAGES) else 0


if __name__ == "__main__":
    sys.exit(main())
