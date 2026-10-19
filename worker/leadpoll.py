#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lead Ads 线索兜底轮询。

为什么需要它：Meta 的 leadgen webhook 不是可靠的投递保证。实测过一天进
5 条提交、只有 2 条产生 webhook 事件，其余 3 条在 Meta 侧存在、在本地完全
不可见 —— 没有任何报错。

做法：直接读表单接口（/{page}/leadgen_forms -> /{form}/leads），把库里还没
有的线索交给 leads.process —— 与 webhook 完全同一条流水线
（写表格 -> AI 分析 -> 推卡片）。

幂等：leadstore.claim() 是原子占位。轮询先拿到、webhook 后到，后者会被挡掉；
反之亦然。所以两条路可以并存，不会重复推卡片。

用法：
    leadpoll.py             正常轮询（默认静默，只有补录时才输出）
    leadpoll.py --dry-run   只列出 Facebook 侧有哪些、缺哪几条，不处理
退出码：0 正常；1 所有主页都读取失败（供 systemd 记录失败）
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from . import config, leads, leadstore  # noqa: E402

VER = "v21.0"
TOKEN_DIR = config.GRAPH_SECRETS
DRY = "--dry-run" in sys.argv

PAGES = config.pages()


def log(msg):
    print(msg, flush=True)


def g(path, params, token):
    """Graph GET，返回 (ok, body)。"""
    url = "https://graph.facebook.com/%s%s" % (
        VER, path + (("?" + urllib.parse.urlencode(params)) if params else ""))
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return False, json.loads(exc.read().decode())
        except Exception:
            return False, {"error": {"message": "HTTP %s" % exc.code}}
    except Exception as exc:
        return False, {"error": {"message": repr(exc)}}


def token_for(page_id):
    with open(os.path.join(TOKEN_DIR, "page-token-%s" % page_id)) as fh:
        return fh.read().strip()


def leads_of_form(page_token, form_id):
    """翻页取一个表单下的全部线索，返回 [(leadgen_id, created_time)]；失败返回 None。"""
    out, path = [], "/%s/leads" % form_id
    params = {"fields": "id,created_time", "limit": "100"}
    for _ in range(50):
        ok, body = g(path, params, page_token)
        if not ok:
            log("[WARN] 表单 %s 线索读取失败：%s"
                % (form_id, str(body.get("error", {}).get("message"))[:80]))
            return None
        for row in body.get("data") or []:
            out.append((str(row.get("id")), row.get("created_time")))
        nxt = (body.get("paging") or {}).get("next")
        if not nxt:
            break
        parsed = urllib.parse.urlparse(nxt)
        path = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))
    return out


def main():
    # 空清单不是「没事可做」，而是「配置没生效」。静默退出会让服务显示成功的
    # 同时一条线索都不查 —— 那正是这个项目想消灭的那种失败。
    if not PAGES:
        log("[ERROR] 没有可轮询的主页：pages.json 缺失，且 %s 下没有 page-token-*"
            % TOKEN_DIR)
        return 1

    failed, seen, fresh = 0, 0, []

    for page_id, page_name, kb in PAGES:
        try:
            page_token = token_for(page_id)
        except Exception as exc:
            log("[WARN] %s 读不到访问令牌：%r" % (page_name, exc))
            failed += 1
            continue

        ok, body = g("/%s/leadgen_forms" % page_id,
                     {"fields": "id,name,leads_count", "limit": "100"}, page_token)
        if not ok:
            log("[WARN] %s 表单列表读取失败：%s"
                % (page_name, str(body.get("error", {}).get("message"))[:80]))
            failed += 1
            continue

        for form in body.get("data") or []:
            form_id = form.get("id")
            rows = leads_of_form(page_token, form_id)
            if rows is None:
                failed += 1
                continue
            for leadgen_id, created_time in rows:
                seen += 1
                if leadstore.get(leadgen_id):
                    continue
                fresh.append((page_id, page_name, kb, form_id, leadgen_id, created_time))

    if DRY:
        log("Facebook 侧共 %d 条线索，其中库里没有的 %d 条" % (seen, len(fresh)))
        for _, page_name, _, form_id, leadgen_id, _ in fresh:
            log("  缺 %s（%s / 表单 %s）" % (leadgen_id, page_name, form_id))
        return 0

    for page_id, page_name, kb, form_id, leadgen_id, created_time in fresh:
        log("补录 %s（%s）" % (leadgen_id, page_name))
        try:
            leads.process({
                "schema": "leadpulse.event.lead/v1",
                "received_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "page": {"id": page_id, "name": page_name, "kb": kb, "mapped": True},
                "lead": {"leadgen_id": leadgen_id, "form_id": form_id,
                         "created_time": created_time, "ad_id": None,
                         "adgroup_id": None, "page_id": page_id},
                "entry_time": 0, "backfill": True,
            })
        except Exception as exc:
            log("[ERROR] 处理 %s 失败：%r" % (leadgen_id, exc))

    if fresh:
        log("本轮补录 %d 条（共扫描 %d 条）" % (len(fresh), seen))
    # 全部主页都失败才返回非 0，避免偶发网络抖动把 timer 打成 failed
    return 1 if failed >= len(PAGES) else 0


if __name__ == "__main__":
    sys.exit(main())
