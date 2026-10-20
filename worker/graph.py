"""Facebook Graph API 回帖。

安全约束（写死在代码里，不依赖配置）：
1. 只能用【主页身份】发帖 —— 用 Page Access Token，绝不用个人令牌。
2. 发送前先复核令牌身份：GET /me 必须返回目标主页 ID，否则拒绝发送。
3. dry-run 时完全不发出任何网络请求。
"""
import json, os, urllib.error, urllib.parse, urllib.request

from . import config

GRAPH = "https://graph.facebook.com/v21.0"


def _get(path, token):
    req = urllib.request.Request(GRAPH + path)
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8") or "{}")


def _post(path, params, token):
    req = urllib.request.Request(GRAPH + path,
                                 data=urllib.parse.urlencode(params).encode("utf-8"),
                                 method="POST")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8") or "{}")


def has_token(page_id):
    """该主页是否已配置访问令牌。没有令牌的主页不能生成待审核卡片 ——
    否则人工点了「发送」才发现发不出去。"""
    return os.path.exists(config.page_token_file(page_id))


def verify_identity(page_id):
    """确认令牌确实是这个主页的，而不是个人账号。"""
    if not has_token(page_id):
        return False, {"error": "No token file for this page", "page_id": page_id}
    token = config.read_secret("page-token-%s" % page_id, config.GRAPH_SECRETS)
    me = _get("/me?fields=id,name", token)
    return me.get("id") == page_id, me


def post_reply(page_id, comment_id, message):
    """以主页身份回复某条评论。返回 (ok, detail)。"""
    if not config.POST_ENABLED:
        return True, {"dry_run": True, "message": "POST_ENABLED=false, not actually published"}

    if not has_token(page_id):
        return False, {"error": "No access token for this page, refusing to send", "page_id": page_id}

    token = config.read_secret("page-token-%s" % page_id, config.GRAPH_SECRETS)

    ok, me = verify_identity(page_id)
    if not ok:
        return False, {"error": "Token identity does not match the target page, sending refused",
                       "token_identity": me.get("id"), "expected": page_id}

    out = _post("/%s/comments" % comment_id, {"message": message}, token)
    if "id" in out:
        return True, {"comment_id": out["id"]}
    return False, out
