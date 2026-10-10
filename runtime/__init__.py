"""直播排播、已唱记录和长操作心跳的插件内状态。"""

from __future__ import annotations

from . import pipeline_state, sung_history
from .heartbeat import feed_watchdog_during


__all__ = [
    "feed_watchdog_during",
    "pipeline_state",
    "sung_history",
]
