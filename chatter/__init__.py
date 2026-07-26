"""anima_chatter 的 Chatter 实现子包。

- :mod:`.core` — :class:`AnimaChatter` 主类，实现 chat_core 要求的各 adapter 协议。
- :mod:`.attention` — vtb / vtb_live 的"是否回复"注意力过滤。
- :mod:`.request_factory` — LLM 请求构造（自定义模型集 + SystemReminder bucket）。
- :mod:`.session_bridge` — 与 chat_core 之间的结构化协议（不 import 对方源码）。
- :mod:`.logging` — 传给 chat_core 的日志包装器。
"""

from __future__ import annotations

from .attention import SubAgentDecision, mark_reply_success
from .core import AnimaChatter
from .session_bridge import (
    AnimaSessionAdapters,
    AnimaSessionOptions,
    ChatCoreServiceLike,
    PlainTextResponseHandling,
)


__all__ = [
    "AnimaChatter",
    "AnimaSessionAdapters",
    "AnimaSessionOptions",
    "ChatCoreServiceLike",
    "PlainTextResponseHandling",
    "SubAgentDecision",
    "mark_reply_success",
]
