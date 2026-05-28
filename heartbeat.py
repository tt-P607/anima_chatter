"""anima_chatter 的 watchdog 心跳辅助模块。

say / say_and_perform 这类 action 在 chatter generator 内 ``await`` 长达
几十秒的 TTS 合成 + 音频播放期间，generator 没机会 yield，框架的 stream
loop 也就没机会更新心跳，watchdog 会判超过 ``stream_warning_threshold``
（默认 40 秒）打 WARNING，超过 ``stream_restart_threshold``（默认 60 秒）
直接强制重启 stream，把当前 chatter 任务杀掉。

:func:`feed_watchdog_during` 在播放期间起一个轻量后台任务每 ``interval``
秒主动调 ``WatchDog.feed_dog`` 喂一次，让 watchdog 安静。

只在播放器内部 await 期间使用——一旦 action.execute 返回，generator 自然
会 yield，框架自己会重新打心跳，无需再喂。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from src.kernel.concurrency import get_watchdog
from src.kernel.logger import get_logger

_logger = get_logger("anima_chatter.heartbeat")


@contextlib.asynccontextmanager
async def feed_watchdog_during(
    stream_id: str,
    *,
    interval: float = 5.0,
) -> AsyncIterator[None]:
    """在 ``with`` 块内每 ``interval`` 秒主动喂一次 watchdog。

    用法::

        async with feed_watchdog_during(self.chat_stream.stream_id):
            await long_running_audio_playback(...)

    Args:
        stream_id: 当前聊天流 ID，对应 watchdog 注册时的 stream_id。
        interval: 喂狗间隔秒数，默认 5 秒。需小于
            ``stream_warning_threshold``（默认 40 秒）。
    """

    if interval <= 0:
        # 关闭喂狗（用于测试 / 调试）
        yield
        return

    stop_event = asyncio.Event()
    watchdog = get_watchdog()

    async def _feeder() -> None:
        """后台喂狗循环：每 ``interval`` 秒喂一次，直到 stop_event 触发。"""

        try:
            while not stop_event.is_set():
                try:
                    watchdog.feed_dog(stream_id)
                except Exception as exc:  # noqa: BLE001
                    _logger.debug(f"feed_dog 失败 stream={stream_id[:8]}: {exc}")
                # 用 wait_for 实现"sleep 但可被立即唤醒"
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass

    feeder_task = asyncio.create_task(
        _feeder(), name=f"anima_chatter_watchdog_feeder_{stream_id[:8]}"
    )

    try:
        yield
    finally:
        stop_event.set()
        if not feeder_task.done():
            feeder_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await feeder_task


__all__ = ["feed_watchdog_during"]
