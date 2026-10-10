"""长阻塞期间主动喂 watchdog 的上下文管理器。

``say`` / ``say_and_perform`` / ``sing_song`` 这类 action 在 chatter generator
内 ``await`` 长达几十秒的 TTS 合成 + 音频播放期间，generator 没机会 yield，
框架的 stream loop 也就没机会更新心跳，watchdog 会先打 WARNING，进而强制重启
stream 把当前 chatter 任务杀掉。

:func:`feed_watchdog_during` 在这段期间起一个轻量后台任务，每 ``interval`` 秒
主动喂一次狗。只在播放器内部 ``await`` 期间使用——一旦 action 返回，generator
自然会 yield，框架自己会重新打心跳。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from src.app.plugin_system.api.log_api import get_logger

from .._internal_compat import (
    cancel_background_task,
    create_background_task,
    feed_watchdog,
)


logger = get_logger("anima_chatter.heartbeat")


__all__ = ["feed_watchdog_during"]


@contextlib.asynccontextmanager
async def feed_watchdog_during(
    stream_id: str,
    *,
    interval: float = 5.0,
) -> AsyncIterator[None]:
    """在 ``async with`` 块内每 ``interval`` 秒主动喂一次 watchdog。

    用法::

        async with feed_watchdog_during(stream_id):
            await long_running_audio_playback(...)

    Args:
        stream_id: 当前聊天流 ID，对应 watchdog 注册时的 stream_id。
        interval: 喂狗间隔秒数，默认 5 秒；须小于框架的
            ``stream_warning_threshold``。传 ``<= 0`` 关闭喂狗（测试用）。

    Yields:
        ``None``——本上下文管理器不产出值，只负责喂狗生命周期。
    """

    if interval <= 0:
        yield
        return

    stop_event = asyncio.Event()

    async def _feeder() -> None:
        """后台喂狗循环：每 ``interval`` 秒喂一次，直到 ``stop_event`` 触发。"""

        try:
            while not stop_event.is_set():
                feed_watchdog(stream_id)
                # 用 wait_for 实现"sleep 但可被立即唤醒"。
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass

    handle = create_background_task(
        _feeder(),
        name=f"anima_chatter.watchdog_feeder.{stream_id[:8]}",
        metadata={"stream_id": stream_id, "kind": "watchdog_feeder"},
    )

    try:
        yield
    finally:
        stop_event.set()
        task = handle.task
        if task is not None and not task.done():
            cancel_background_task(handle)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
