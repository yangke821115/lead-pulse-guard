"""最小化飞书长连接客户端。

## 为什么不直接用 lark_oapi

`lark_oapi/__init__.py` 第 2 行是 `from .api import *`，会把飞书**所有 REST
端点**的封装一次性导入。实测代价：10,623 个模块、96 MB 磁盘、**约 142 MB
常驻内存**。而本项目一个 REST 端点都不用 —— 发消息、更新卡片全部是自己用
urllib 写的。付这个代价纯属浪费。

## 做法：两层垫片，都在 import lark_oapi 之前装好

1. **空的顶层包**替换 `lark_oapi`，让真正的 `__init__.py` 永不执行。
   它带着真实目录的 `__path__`，所以 `lark_oapi.ws` / `.event` / `.core`
   照常能加载。

2. **自己的最小 EventDispatcherHandler** 替换 `event.dispatcher_handler`。
   `ws.Client` 对这个对象只有一个要求 —— 提供 `_do_without_validation(payload)`。
   真正的 dispatcher_handler 有 2617 行、import 了 24 个 api processor 包，
   而我们只需要其中 `card.action.trigger` 这一条分发路径。

## 边界（明确写出来）

- 支持 `p2.card.action.trigger` 与 `p2.im.message.receive_v1`。其余事件忽略。
- 依赖 SDK 内部结构。SDK 升级后必须重新验证；`install()` 装不上时直接抛错，
  **不会静默降级**成用不了的状态。
- 不提供 `lark_oapi.Client`（REST 客户端）—— 我们用不到，也不该用。
"""
import importlib.machinery, importlib.util, json, sys, types

_installed = False


class EventDispatcherHandler:
    """只做一件事：把 card.action.trigger 事件分发给回调。"""

    def __init__(self, callback, message_callback=None):
        self._callback = callback
        self._message_callback = message_callback

    def _do_without_validation(self, payload):
        from lark_oapi.core.json import JSON
        from lark_oapi.event.context import EventContext
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTrigger,
        )

        text = payload.decode("utf-8")
        context = JSON.unmarshal(text, EventContext)

        if getattr(context, "schema", None):
            context.schema = "p2"
            context.type = context.header.event_type
            context.token = context.header.token
        elif getattr(context, "uuid", None):
            context.schema = "p1"
            context.type = context.event.get("type")

        kind = "%s.%s" % (context.schema, context.type)
        if kind == "p2.card.action.trigger":
            return self._callback(JSON.unmarshal(text, P2CardActionTrigger))
        if kind == "p2.im.message.receive_v1":
            if self._message_callback is None:
                return None
            return self._message_callback(json.loads(text))
        return None

    @staticmethod
    def builder(encrypt_key="", verification_token=""):
        return _Builder()


class _Builder:
    def __init__(self):
        self._callback = None
        self._message_callback = None

    def register_p2_card_action_trigger(self, fn):
        self._callback = fn
        return self

    def register_p2_im_message_receive_v1(self, fn):
        self._message_callback = fn
        return self

    def build(self):
        if self._callback is None:
            raise RuntimeError("没有注册任何回调")
        return EventDispatcherHandler(self._callback, self._message_callback)


def install():
    """装垫片。必须在任何 lark_oapi 导入之前调用。"""
    global _installed
    if _installed:
        return
    if "lark_oapi" in sys.modules:
        raise RuntimeError(
            "lark_oapi 已经被导入过了 —— 垫片必须抢在任何 lark_oapi 导入之前装"
        )

    spec = importlib.util.find_spec("lark_oapi")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("环境里找不到 lark_oapi 包，无法安装垫片")
    root = list(spec.submodule_search_locations)[0]

    package = types.ModuleType("lark_oapi")
    package.__path__ = [root]
    package.__package__ = "lark_oapi"
    fake_spec = importlib.machinery.ModuleSpec("lark_oapi", loader=None, is_package=True)
    fake_spec.submodule_search_locations = [root]
    package.__spec__ = fake_spec
    sys.modules["lark_oapi"] = package

    stub = types.ModuleType("lark_oapi.event.dispatcher_handler")
    stub.EventDispatcherHandler = EventDispatcherHandler
    stub.__all__ = ["EventDispatcherHandler"]
    sys.modules["lark_oapi.event.dispatcher_handler"] = stub

    _installed = True


def make_client(app_id, app_secret, on_card_action, on_message=None):
    """构造长连接客户端（会阻塞在 .start()）。"""
    install()
    import lark_oapi.ws as ws_client
    from lark_oapi.core.enum import LogLevel

    builder = EventDispatcherHandler.builder("", "").register_p2_card_action_trigger(on_card_action)
    if on_message is not None:
        builder = builder.register_p2_im_message_receive_v1(on_message)
    handler = builder.build()
    return ws_client.Client(app_id, app_secret, event_handler=handler,
                            log_level=LogLevel.INFO)
