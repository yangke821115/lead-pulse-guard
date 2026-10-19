"""飞书多维表格：把一条线索写成一行记录。

边界（有意收窄）：
- 只做「新增记录 / 更新记录」，不建表、不删表、不改表结构。
  表结构在部署时一次性建立，运行时不再触碰。
- 写失败必须抛异常，绝不静默 —— 丢掉一条线索比报一次错严重得多。
- 列名与表头中文一一对应；改表头就要同步改这里，否则会静默写空。
"""
import json

from . import config, feishu


def _api(path, payload=None, method="POST"):
    return feishu._request(path, payload, method=method, token=feishu.tenant_token())


def _records_path(suffix=""):
    return "/bitable/v1/apps/%s/tables/%s/records%s" % (
        config.BITABLE_APP_TOKEN, config.BITABLE_TABLE_ID, suffix)


def create_record(fields):
    out = _api(_records_path(), {"fields": fields})
    if out.get("code") != 0:
        raise RuntimeError("写多维表格失败: %s"
                           % json.dumps(out, ensure_ascii=False)[:300])
    return (out.get("data") or {}).get("record", {}).get("record_id")


def update_record(record_id, fields):
    out = _api(_records_path("/%s" % record_id), {"fields": fields}, method="PUT")
    return out.get("code") == 0, out


def record_url(record_id):
    if not record_id:
        return ""
    return ("%s/base/%s?table=%s&view=%s&record=%s"
            % (config.BITABLE_HOST, config.BITABLE_APP_TOKEN,
               config.BITABLE_TABLE_ID, config.BITABLE_VIEW_ID, record_id))
