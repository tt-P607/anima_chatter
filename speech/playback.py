"""直播语音与歌曲的 FIFO、任务所有权和真实播放事件。

接收与物理播放分别串行，歌曲与 PCM 语音共享物理播放顺序。
Action 返回接收结果，后台任务在实际起播、结束或取消时释放轮次容量。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from src.app.plugin_system.api.log_api import get_logger

from .._internal_compat import create_background_task
from ..audio import estimate_tts_duration_by_chars
from ..runtime import pipeline_state
from ..runtime.heartbeat import feed_watchdog_during

if TYPE_CHECKING:
    from ..audio import AudioPlayer
    from ..vts import VTSPerformer
    from .markers import SpeechSegment


__all__ = [
    "PerformanceStyle",
    "dispatch_track_pipelined",
    "estimate_segments_duration",
    "play_track_blocking",
]


logger = get_logger("anima_chatter.speech.playback")

PlaybackTurn = tuple[asyncio.Future[None], asyncio.Future[None] | None]
_playback_tail: asyncio.Future[None] | None = None
_receive_tail: asyncio.Future[None] | None = None
_playback_tasks: set[asyncio.Task[Any]] = set()
_playback_cleanup_tasks: set[asyncio.Task[Any]] = set()
_playback_closed = False


def activate_playback() -> None:
    """允许加载后的直播播放并重置已结束的顺序门。"""

    global _playback_closed, _playback_tail, _receive_tail
    _playback_closed = False
    _playback_tail = None
    _receive_tail = None


def reserve_playback_turn() -> PlaybackTurn:
    """为说话或唱歌分配调用顺序，不提前打开音频或网络流。"""

    global _playback_tail
    if _playback_closed:
        raise RuntimeError("直播播放已关闭")
    previous = _playback_tail
    ticket = asyncio.get_running_loop().create_future()
    _playback_tail = ticket
    return ticket, previous


def _release_playback_turn(turn: PlaybackTurn) -> None:
    """释放当前顺序门，取消排队项时保留其前驱的顺序。"""

    ticket, previous = turn
    if ticket.done():
        return
    if previous is not None and not previous.done():
        previous.add_done_callback(lambda _: _release_playback_turn(turn))
    else:
        ticket.set_result(None)


def reserve_receive_turn() -> PlaybackTurn:
    """按提交顺序串行接收 TTS，接收结束后不等待设备排空。"""

    global _receive_tail
    if _playback_closed:
        raise RuntimeError("直播播放已关闭")
    previous = _receive_tail
    ticket = asyncio.get_running_loop().create_future()
    _receive_tail = ticket
    return ticket, previous


@contextlib.asynccontextmanager
async def playback_turn(turn: PlaybackTurn) -> AsyncIterator[None]:
    """等待此前播放项结束并在退出或取消时释放顺序门。"""

    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("直播播放必须在任务中执行")
    _playback_tasks.add(task)
    try:
        if turn[1] is not None:
            await asyncio.shield(turn[1])
        if _playback_closed:
            raise RuntimeError("直播播放已关闭")
        yield
    finally:
        _release_playback_turn(turn)
        _playback_tasks.discard(task)


def create_playback_task(
    factory: Callable[[], Awaitable[None]],
    *,
    name: str,
    turn: PlaybackTurn,
    cleanup: Callable[[], Awaitable[None]],
) -> asyncio.Task[Any]:
    """派发归本插件所有的播放任务并处理起跑前取消的顺序门。"""

    cleanup_started = False

    async def finalize() -> None:
        """每项只回收一次排播占位。"""

        nonlocal cleanup_started
        if not cleanup_started:
            cleanup_started = True
            await cleanup()

    async def run() -> None:
        """延迟创建播放协程并在退出时完成清理。"""

        try:
            await factory()
        finally:
            await finalize()

    coro = run()
    try:
        handle = create_background_task(coro, name=name, metadata={"kind": "playback"})
    except Exception:
        coro.close()
        _release_playback_turn(turn)
        raise
    task = handle.task
    _playback_tasks.add(task)

    def completed(finished: asyncio.Task[Any]) -> None:
        """回收播放任务及其顺序门。"""

        _playback_tasks.discard(finished)
        _release_playback_turn(turn)
        coro.close()
        if not cleanup_started:
            handle = create_background_task(
                finalize(), name=f"{name}.cleanup", metadata={"kind": "playback_cleanup"}
            )
            _playback_cleanup_tasks.add(handle.task)
            handle.task.add_done_callback(_playback_cleanup_tasks.discard)

    task.add_done_callback(completed)
    return task


async def close_playback() -> None:
    """取消并等待本插件的在途及排队播放，不关闭共享 Provider。"""

    global _playback_closed
    _playback_closed = True
    current = asyncio.current_task()
    tasks = [task for task in _playback_tasks if task is not current and not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    cleanup_tasks = list(_playback_cleanup_tasks)
    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks)


@dataclass(slots=True)
class PerformanceStyle:
    """一次播放的表演参数（emotion / intent）。

    Attributes:
        emotion: ``"类型:强度"`` 格式的情绪标记，驱动 SpeechAnimator 的情绪矩阵。
        intent: 顶层动作意图；片段自带 ``motion`` 时按片段覆盖。
        emotion_main: emotion 的主类型（不含强度），用于 expression 匹配兜底。
    """

    emotion: str
    intent: str
    emotion_main: str

    @classmethod
    def create(cls, emotion: str, intent: str) -> "PerformanceStyle":
        """从原始 emotion / intent 字符串构造。

        Args:
            emotion: ``"类型:强度"`` 或纯类型字符串。
            intent: 动作意图名。

        Returns:
            解析好主类型的表演参数。
        """

        main = (emotion or "neutral").split(":", 1)[0].strip().lower() or "neutral"
        return cls(emotion=emotion, intent=intent, emotion_main=main)


class TimelineRunner(Protocol):
    """歌曲播放期间的动作时间轴执行协议。

    由 ``sing_song`` 提供实现，让虚拟形象在歌曲不同段落切换动作。
    """

    async def run(self, performer: "VTSPerformer", stop_event: asyncio.Event) -> None:
        """在播放期间按时间轴触发动作切换。

        Args:
            performer: VTS 表演器。
            stop_event: 播放结束 / 异常时由调用方触发，收到后应立即退出。
        """
        ...


def estimate_segments_duration(segments: list["SpeechSegment"]) -> float:
    """按字数粗估分段语音的总播放时长（含段间静默）。

    仅用于容量背压，不作为起播时刻、等待期限或 Actor 放行比例。

    Args:
        segments: 待播放片段列表。

    Returns:
        估算总时长（秒）。
    """

    return sum(
        estimate_tts_duration_by_chars(segment.text) + max(0.0, segment.wait_before)
        for segment in segments
    )


# ── 整轨音频（唱歌） ───────────────────────────────────────


async def _play_track(
    *,
    audio_bytes: bytes,
    inst_bytes: bytes | None,
    audio_player: "AudioPlayer",
    performer: "VTSPerformer | None",
    timeline: TimelineRunner | None,
    song_name: str,
    on_started: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """播放一整轨音频（可选双轨 + 动作时间轴）。

    ``inst_bytes`` 非空时走双轨：人声进 VB-Cable 驱动口型、伴奏进独立设备，
    伴奏不带动口型。唱歌不调 ``start_speech_playback``——不需要触发 hotkey，
    让 VTS 的麦克风口型 + 时间轴自动同步即可。

    Args:
        audio_bytes: 人声 / 主音轨。
        inst_bytes: 伴奏轨；``None`` 表示单轨。
        audio_player: 本地音频播放器。
        performer: VTS 表演器；``None`` 时只播音频。
        timeline: 动作时间轴执行器；``None`` 表示全程保持初始姿态。
        song_name: 歌名，仅用于日志。
    """

    async def _emit(start_callback: Callable[[], Awaitable[None]]) -> None:
        """按是否有伴奏选择单轨 / 双轨播放。"""

        if inst_bytes:
            await audio_player.play_dual(
                audio_bytes, inst_bytes, on_started=start_callback
            )
        else:
            await audio_player.play_audio(audio_bytes, on_started=start_callback)

    if performer is None:
        if on_started is None:
            async def start_callback() -> None:
                return None

            await _emit(start_callback)
        else:
            await _emit(on_started)
        return

    stop_event = asyncio.Event()
    async with performer.speaking_session(emotion="happy:1", intent="NARRATING"):
        timeline_handle = None

        async def handle_started() -> None:
            nonlocal timeline_handle
            if timeline is not None:
                timeline_handle = create_background_task(
                    timeline.run(performer, stop_event),
                    name=f"anima_chatter.sing_timeline.{song_name[:20]}",
                    metadata={"kind": "sing_timeline"},
                )
            if on_started is not None:
                await on_started()

        try:
            await _emit(handle_started)
        finally:
            stop_event.set()
            if timeline_handle is not None:
                task = timeline_handle.task
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task


async def play_track_blocking(
    *,
    stream_id: str,
    audio_bytes: bytes,
    inst_bytes: bytes | None,
    audio_player: "AudioPlayer",
    performer: "VTSPerformer | None",
    timeline: TimelineRunner | None,
    pre_delay: float,
    song_name: str,
    on_started: Callable[[], Awaitable[None]] | None = None,
) -> tuple[bool, str]:
    """阻塞模式播放整轨音频：播完才返回。

    Args:
        stream_id: 所属聊天流。
        audio_bytes: 人声 / 主音轨。
        inst_bytes: 伴奏轨。
        audio_player: 本地音频播放器。
        performer: VTS 表演器。
        timeline: 动作时间轴执行器。
        pre_delay: 开场静默秒数（在表演锁之外等待，避免占用 VTS）。
        song_name: 歌名。

    Returns:
        ``(是否成功, 给模型的执行结果描述)``。
    """

    turn = reserve_playback_turn()
    async with feed_watchdog_during(stream_id), playback_turn(turn):
        if pre_delay > 0:
            logger.info(f"开场停顿 {pre_delay:.1f}s 后开始播放《{song_name}》")
            await asyncio.sleep(pre_delay)
        await _play_track(
            audio_bytes=audio_bytes,
            inst_bytes=inst_bytes,
            audio_player=audio_player,
            performer=performer,
            timeline=timeline,
            song_name=song_name,
            on_started=on_started,
        )

    logger.info(f"歌曲播放完成：《{song_name}》")
    return True, f"已播放歌曲：《{song_name}》"


async def dispatch_track_pipelined(
    *,
    stream_id: str,
    audio_bytes: bytes,
    inst_bytes: bytes | None,
    audio_player: "AudioPlayer",
    performer: "VTSPerformer | None",
    timeline: TimelineRunner | None,
    pre_delay: float,
    song_duration: float,
    song_name: str,
    on_started: Callable[[], Awaitable[None]] | None = None,
) -> tuple[bool, str]:
    """流水线模式播放整轨音频：reserve 占位后派发后台任务，立即返回。

    队列积压超限时拒排（背压反馈），起播 / 播完回写实际进度。

    Args:
        stream_id: 所属聊天流。
        audio_bytes: 人声 / 主音轨。
        inst_bytes: 伴奏轨。
        audio_player: 本地音频播放器。
        performer: VTS 表演器。
        timeline: 动作时间轴执行器。
        pre_delay: 开场静默秒数。
        song_duration: 歌曲实际时长（秒）。
        song_name: 歌名。

    Returns:
        ``(是否成功, 给模型的执行结果描述)``。
    """

    track_id = f"track_{uuid.uuid4().hex[:12]}"
    logger.info(
        f"进入流水线：《{song_name}》（歌曲 {song_duration:.1f}s），开始 reserve"
    )
    reserved = await pipeline_state.reserve(
        stream_id, song_duration, track_id=track_id, kind="song"
    )
    if reserved is None:
        return False, (
            "播放队列已满（积压超过上限），本轮歌曲未排入队列——"
            "请稍后再唱，等待当前队列消化"
        )
    _, finish_at = reserved
    turn = reserve_playback_turn()
    started = False

    async def finalize() -> None:
        """释放未起播占位或记录歌曲的实际结束时间。"""

        if started:
            await pipeline_state.report_actual(
                stream_id, track_id=track_id, finished_at=time.monotonic()
            )
        else:
            await pipeline_state.relinquish(
                stream_id, track_id=track_id, reserved_until=finish_at
            )

    async def _background() -> None:
        """按调用顺序等待起播、开场静默并播放整轨。"""

        nonlocal started
        async with playback_turn(turn):
            if pre_delay > 0:
                await asyncio.sleep(pre_delay)

            async def report_started() -> None:
                nonlocal started
                started = True
                await pipeline_state.report_actual(
                    stream_id, track_id=track_id, started_at=time.monotonic()
                )
                if on_started is not None:
                    await on_started()

            await _play_track(
                audio_bytes=audio_bytes,
                inst_bytes=inst_bytes,
                audio_player=audio_player,
                performer=performer,
                timeline=timeline,
                song_name=song_name,
                on_started=report_started,
            )
        logger.info(f"[bg_sing {stream_id[:8]}] 唱完了：《{song_name}》")

    create_playback_task(
        lambda: _guard_background(_background(), stream_id=stream_id, label="bg_sing"),
        name=f"anima_chatter.background_sing.{stream_id[:8]}",
        turn=turn,
        cleanup=finalize,
    )

    logger.info(
        f"已派发后台播放、Action 立即返回（《{song_name}》，预计 {song_duration:.1f}s）"
    )
    return True, (
        f"已派发歌曲《{song_name}》到后台播放队列"
        f"（歌曲时长 {song_duration:.1f}s，流水线已启用）"
    )


# ── 后台任务共享工具 ───────────────────────────────────────


async def _guard_background(
    coro: Coroutine[Any, Any, None],
    *,
    stream_id: str,
    label: str,
) -> None:
    """包裹后台播放协程，统一处理取消与异常。

    Action 已返回接收结果，后台错误记录日志。容量与顺序门由任务清理边界
    回收，不在本层重复处理，也不重播已输出的内容。

    Args:
        coro: 待执行的后台协程。
        stream_id: 所属聊天流，仅用于日志。
        label: 日志前缀标签（``bg_play`` / ``bg_sing``）。
    """

    try:
        await coro
    except asyncio.CancelledError:
        logger.info(f"[{label} {stream_id[:8]}] 后台播放被取消")
        raise
    except Exception as exc:
        logger.error(f"[{label} {stream_id[:8]}] 后台播放异常: {exc}", exc_info=True)  # noqa: G201
