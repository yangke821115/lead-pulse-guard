"""spool 监听：带 offset + inode 检测，能正确处理轮转与截断。"""
import json, os

from . import config


class SpoolTailer:
    def __init__(self, path=None, offset_file=None):
        self.path = path or config.SPOOL_FILE
        self.offset_file = offset_file or config.OFFSET_FILE
        self.inode = None
        self.offset = 0
        self._first_run = not os.path.exists(self.offset_file)
        self._load()

    def _load(self):
        if os.path.exists(self.offset_file):
            try:
                with open(self.offset_file, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
                self.inode = state.get("inode")
                self.offset = int(state.get("offset", 0))
            except (ValueError, OSError):
                self.inode, self.offset = None, 0

    def _save(self):
        tmp = self.offset_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"inode": self.inode, "offset": self.offset}, handle)
        os.replace(tmp, self.offset_file)

    def read_new(self):
        """产出尚未处理的事件（dict）。首次运行从文件末尾开始，不重放历史。"""
        if not os.path.exists(self.path):
            return []
        stat = os.stat(self.path)
        if self.inode != stat.st_ino:
            # 新文件（首次、或轮转后新建）
            self.inode = stat.st_ino
            self.offset = 0 if self._first_run else 0
        elif stat.st_size < self.offset:
            # 文件被截断
            self.offset = 0

        if self._first_run:
            # 首次启动：跳到末尾，只处理之后的新事件
            self.offset = stat.st_size
            self.inode = stat.st_ino
            self._first_run = False
            self._save()
            return []

        events = []
        with open(self.path, "r", encoding="utf-8") as handle:
            handle.seek(self.offset)
            # 注意：必须用 readline()，不能用 `for line in handle`。
            # for 迭代会走内部预读缓冲，Python 会因此禁用 tell()，
            # 抛 OSError('telling position disabled by next() call')。
            while True:
                line = handle.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    break  # 半行（下游正在写），等下次
                self.offset = handle.tell()
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        self._save()
        return events
