"""线索去重与状态（SQLite 单文件）。

为什么不用内存或 JSON：
- Meta 会重投 leadgen 事件，去重必须跨重启；
- 单条线索可能因为模型/网络失败而需要人工重放，内存态重启即丢。
单文件、无端口、无新进程；放在 StateDirectory（已 700）下。
"""
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn = None

_COLUMNS = ("leadgen_id", "page_id", "form_id", "created_time", "received_at",
            "status", "record_id", "analysis", "error", "updated_at")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  leadgen_id   TEXT PRIMARY KEY,
  page_id      TEXT,
  form_id      TEXT,
  created_time TEXT,
  received_at  TEXT,
  status       TEXT,
  record_id    TEXT,
  analysis     TEXT,
  error        TEXT,
  updated_at   TEXT
);
"""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _db():
    global _conn
    if _conn is None:
        directory = os.path.dirname(config.LEAD_DB)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # timeout 就是 busy timeout：默认 5 秒太短，任何瞬时写锁都会
        # 直接抛 "database is locked"。给到 30 秒，并把 WAL 打开，
        # 让读写可以并发。
        # isolation_level=None = 自动提交模式。
        # 必须这么写：默认模式下 sqlite3 会隐式 BEGIN，只要有「没走到 commit」
        # 的路径就会留下一个悬空写事务，把整个库锁死（实测踩到：
        # 另一个进程连 INSERT 都写不进去，连 root 也写不进去，
        # 但读操作正常 —— 这是写锁被长期占住的典型症状）。
        # 本模块每次操作都是单条语句，自动提交最省心。
        _conn = sqlite3.connect(config.LEAD_DB, check_same_thread=False,
                                timeout=30.0, isolation_level=None)
        cursor = _conn.execute("PRAGMA journal_mode=WAL")
        cursor.fetchall()
        cursor.close()
        _conn.executescript(_SCHEMA)
    return _conn


def claim(leadgen_id, page_id, form_id, created_time):
    """原子占位。返回 True 表示「这条之前没见过，归我了」。"""
    with _lock:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO leads (leadgen_id,page_id,form_id,created_time,"
                "received_at,status,updated_at) VALUES (?,?,?,?,?,?,?)",
                (leadgen_id, page_id, form_id, created_time, _now(), "处理中", _now()))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def mark(leadgen_id, **fields):
    bad = [k for k in fields if k not in _COLUMNS]
    if bad:
        raise ValueError("leadstore.mark 收到未知列: %s" % bad)
    if not fields:
        return
    assignments = ", ".join("%s=?" % k for k in fields)
    with _lock:
        conn = _db()
        conn.execute("UPDATE leads SET %s, updated_at=? WHERE leadgen_id=?" % assignments,
                     list(fields.values()) + [_now(), leadgen_id])
        conn.commit()


def get(leadgen_id):
    with _lock:
        conn = _db()
        row = conn.execute("SELECT * FROM leads WHERE leadgen_id=?",
                           (leadgen_id,)).fetchone()
        return dict(zip(_COLUMNS, row)) if row else None


def counts():
    with _lock:
        return dict(_db().execute(
            "SELECT status, COUNT(*) FROM leads GROUP BY status").fetchall())
