#!/usr/bin/env python3
"""Lead Pulse Guard worker 入口。

包结构约定：本文件所在的目录就是一个名为 worker 的包
（部署到 /opt/leadpulse/worker，或镜像里的 /app/worker）。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
os.environ.setdefault("LPW_BASE", _HERE)

from worker.worker import main  # noqa: E402

main()
