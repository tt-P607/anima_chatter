"""anima_chatter 跨模块共享的运行时状态。

三个模块级单例 + 一个上下文管理器，均为"插件内多处读写、生命周期与插件一致"
的状态，不适合做成 Service（``ServiceManager.get_service()`` 每次新建实例，
存状态会出错）：

- :mod:`.call_state` — 当前进行中的语音通话（同时只允许一个）。
- :mod:`.pipeline_state` — vtb_live 音频流水线的时间轴排队状态（按 stream 隔离）。
- :mod:`.sung_history` — 本次运行已唱过的歌名，用于选歌去重。
- :mod:`.heartbeat` — 长阻塞期间主动喂 watchdog 的上下文管理器。
"""

from __future__ import annotations

from . import call_state, pipeline_state, sung_history
from .heartbeat import feed_watchdog_during


__all__ = [
    "call_state",
    "feed_watchdog_during",
    "pipeline_state",
    "sung_history",
]
