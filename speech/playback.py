"""音频播放的**唯一**实现——阻塞模式与 vtb_live 流水线模式共用。

三个 Action（``say`` / ``say_and_perform`` / ``sing_song``）此前各自实现了一套
"合成 → reserve → 派发后台 / 阻塞播放"的流程，逻辑高度重叠且行为已出现漂移。
本模块把它们收敛为两个入口：

- :func:`play_segments_blocking` —— 在 chatter generator 内播完才返回（voice /
  vtb 模式，以及流水线退化路径）。
- :func:`dispatch_segments_pipelined` —— reserve 占位后派发后台任务，Action
  立即返回（vtb_live 流水线模式）。

歌曲播放走 :func:`play_track_blocking` / :func:`dispatch_track_pipelined`，与
分段语音共享同一套 reserve / 后台派发 / watchdog 逻辑。

**物理串行保证**：无论走哪条路径，音频都由 ``AudioPlayer`` 的播放锁 +
``VTSPerformer`` 的表演锁双重串行，绝不会重叠，只会排队。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from src.app.plugin_system.api.log_api import get_logger

from .._internal_compat import create_background_task
from ..audio import estimate_tts_duration_by_chars
from ..runtime import pipeline_state
from ..runtime.heartbeat import feed_watchdog_during

if TYPE_CHECKING:
    from ..audio import AudioPlayer
    from ..config import PipeliningSection
    from ..vts import VTSPerformer
    from .backend import TTSArtifact
    from .markers import SpeechSegment


__all__ = [
    "PerformanceStyle",
    "dispatch_segments_pipelined",
    "dispatch_track_pipelined",
    "estimate_segments_duration",
    "play_segments_blocking",
    "play_track_blocking",
    "should_use_pipeline",
]


logger = get_logger("anima_chatter.speech.playback")


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

    用于 reserve 之前的占位——真实时长要等合成完才知道，但流水线需要在合成前
    就占好时间轴位置以保证多个 Action 的先后顺序。估算只影响门的软触发时刻，
    物理播放顺序由播放锁保证。

    Args:
        segments: 待播放片段列表。

    Returns:
        估算总时长（秒）。
    """

    return sum(
        estimate_tts_duration_by_chars(segment.text) + max(0.0, segment.wait_before)
        for segment in segments
    )


def should_use_pipeline(
    *,
    is_live_mode: bool,
    section: "PipeliningSection",
    estimated_duration: float,
) -> bool:
    """判断本次播放是否走流水线模式。

    Args:
        is_live_mode: 当前是否为 ``vtb_live`` 模式。
        section: 插件配置的 ``pipelining`` 段。
        estimated_duration: 本次播放的估算总时长（秒）。

    Returns:
        三个条件（直播模式 / 配置启用 / 时长达标）同时满足时返回 ``True``。
    """

    if not is_live_mode or not section.enabled:
        return False
    if estimated_duration < section.min_duration_seconds:
        logger.info(
            f"流水线退化：估算总时长 {estimated_duration:.2f}s < "
            f"min_duration {section.min_duration_seconds:.2f}s，本次走阻塞模式"
        )
        return False
    return True


# ── 分段语音 ───────────────────────────────────────────────


async def _consume_segments(
    *,
    tasks: list["asyncio.Task[TTSArtifact]"],
    segments: list["SpeechSegment"],
    performer: "VTSPerformer | None",
    audio_player: "AudioPlayer | None",
    style: PerformanceStyle,
) -> int:
    """按索引顺序消费合成结果并播放（流式：第一段好就播）。

    行内 motion 标记处理：每段播放前按 ``segment.motion`` 切 intent + expression；
    为 ``None`` 时用顶层 intent。没有 performer（VTS 未连）时退回纯音频播放。

    Args:
        tasks: 与 ``segments`` 同序的合成任务列表。
        segments: 片段列表。
        performer: VTS 表演器；``None`` 表示只播音频不驱动形象。
        audio_player: 本地音频播放器；``performer`` 为 ``None`` 时必须提供。
        style: 顶层表演参数。

    Returns:
        实际播放成功的片段数。
    """

    played = 0
    for index, task in enumerate(tasks):
        artifact = await task
        segment = segments[index]

        audio = artifact.audio
        if not artifact.is_playable or audio is None:
            logger.error(f"跳过不可播放片段 {index}: {segment.text[:30]}...")
            continue

        if segment.wait_before >= 0.1:
            await asyncio.sleep(segment.wait_before)

        segment_intent = segment.motion or style.intent
        if performer is not None:
            # 关键时序：首段音频"已经合成完准备播放"时才真正触发 VTS 动作链。
            # start_speech_playback 幂等，后续段调用是 no-op。
            await performer.start_speech_playback()
            await performer.switch_segment_intent(
                segment_intent,
                emotion_main_for_expression=style.emotion_main,
            )
            await performer.play(audio)
        elif audio_player is not None:
            await audio_player.play_audio(audio)
        else:
            logger.error("performer 与 audio_player 均不可用，无法播放")
            continue

        played += 1
        logger.info(
            f"已播放段 {index}: {segment.text[:20]}... "
            f"emotion={style.emotion} intent={segment_intent}"
        )
    return played


async def _run_segments(
    *,
    tasks: list["asyncio.Task[TTSArtifact]"],
    segments: list["SpeechSegment"],
    performer: "VTSPerformer | None",
    audio_player: "AudioPlayer | None",
    style: PerformanceStyle,
) -> int:
    """在 ``speaking_session``（如有 performer）内消费并播放全部片段。

    Args:
        tasks: 合成任务列表。
        segments: 片段列表。
        performer: VTS 表演器；``None`` 时跳过 session。
        audio_player: 本地音频播放器。
        style: 顶层表演参数。

    Returns:
        实际播放成功的片段数。
    """

    if performer is None:
        return await _consume_segments(
            tasks=tasks,
            segments=segments,
            performer=None,
            audio_player=audio_player,
            style=style,
        )

    async with performer.speaking_session(emotion=style.emotion, intent=style.intent):
        return await _consume_segments(
            tasks=tasks,
            segments=segments,
            performer=performer,
            audio_player=audio_player,
            style=style,
        )


async def play_segments_blocking(
    *,
    stream_id: str,
    tasks: list["asyncio.Task[TTSArtifact]"],
    segments: list["SpeechSegment"],
    performer: "VTSPerformer | None",
    audio_player: "AudioPlayer | None",
    style: PerformanceStyle,
) -> tuple[bool, str]:
    """阻塞模式播放分段语音：播完才返回。

    整段播放期间 chatter generator 不会 yield，需要主动喂 watchdog 避免触发框架
    的 stream 重启阈值。

    Args:
        stream_id: 所属聊天流。
        tasks: 合成任务列表。
        segments: 片段列表。
        performer: VTS 表演器。
        audio_player: 本地音频播放器。
        style: 顶层表演参数。

    Returns:
        ``(是否成功, 给模型的执行结果描述)``。
    """

    async with feed_watchdog_during(stream_id):
        played = await _run_segments(
            tasks=tasks,
            segments=segments,
            performer=performer,
            audio_player=audio_player,
            style=style,
        )
    return True, f"已播放 {played}/{len(segments)} 段"


async def dispatch_segments_pipelined(
    *,
    stream_id: str,
    tasks: list["asyncio.Task[TTSArtifact]"],
    segments: list["SpeechSegment"],
    performer: "VTSPerformer | None",
    audio_player: "AudioPlayer | None",
    style: PerformanceStyle,
    estimated_duration: float,
) -> tuple[bool, str]:
    """流水线模式播放分段语音：reserve 占位后派发后台任务，立即返回。

    Args:
        stream_id: 所属聊天流。
        tasks: 合成任务列表（已在并发合成中）。
        segments: 片段列表。
        performer: VTS 表演器。
        audio_player: 本地音频播放器。
        style: 顶层表演参数。
        estimated_duration: 估算总时长（秒），用于 reserve 占位。

    Returns:
        ``(True, 给模型的执行结果描述)``。
    """

    logger.info(
        f"进入流水线：{len(segments)} 段，估算总时长 {estimated_duration:.2f}s，开始 reserve"
    )
    start_at, finish_at = await pipeline_state.reserve(stream_id, estimated_duration)

    async def _background() -> None:
        """后台流式播放：等到 start_at → 按段顺序等合成结果并播放。"""

        await _wait_until(start_at, stream_id=stream_id, finish_at=finish_at)
        played = await _run_segments(
            tasks=tasks,
            segments=segments,
            performer=performer,
            audio_player=audio_player,
            style=style,
        )
        logger.info(f"[bg_play {stream_id[:8]}] 后台流式播放完成（{played} 段）")

    create_background_task(
        _guard_background(_background(), stream_id=stream_id, label="bg_play"),
        name=f"anima_chatter.background_play.{stream_id[:8]}",
        metadata={"stream_id": stream_id, "kind": "background_play"},
    )

    logger.info(
        f"已派发后台流式播放、Action 立即返回"
        f"（{len(segments)} 段，估算 {estimated_duration:.2f}s）"
    )
    return True, (
        f"已派发 {len(segments)} 段到后台流式播放队列"
        f"（估算总时长 {estimated_duration:.2f}s，流水线已启用）"
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

    async def _emit() -> None:
        """按是否有伴奏选择单轨 / 双轨播放。"""

        if inst_bytes:
            await audio_player.play_dual(audio_bytes, inst_bytes)
        else:
            await audio_player.play_audio(audio_bytes)

    if performer is None:
        await _emit()
        return

    stop_event = asyncio.Event()
    async with performer.speaking_session(emotion="happy:1", intent="NARRATING"):
        timeline_handle = None
        if timeline is not None:
            timeline_handle = create_background_task(
                timeline.run(performer, stop_event),
                name=f"anima_chatter.sing_timeline.{song_name[:20]}",
                metadata={"kind": "sing_timeline"},
            )
        try:
            await _emit()
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

    if pre_delay > 0:
        logger.info(f"开场停顿 {pre_delay:.1f}s 后开始播放《{song_name}》")
        await asyncio.sleep(pre_delay)

    async with feed_watchdog_during(stream_id):
        await _play_track(
            audio_bytes=audio_bytes,
            inst_bytes=inst_bytes,
            audio_player=audio_player,
            performer=performer,
            timeline=timeline,
            song_name=song_name,
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
) -> tuple[bool, str]:
    """流水线模式播放整轨音频：reserve 占位后派发后台任务，立即返回。

    ``pre_delay`` 也算进 reserve 总时长——后台任务才会真正等待这段静默。

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
        ``(True, 给模型的执行结果描述)``。
    """

    total_duration = pre_delay + song_duration
    logger.info(
        f"进入流水线：《{song_name}》总 {total_duration:.1f}s "
        f"（{pre_delay:.1f}s 静默 + {song_duration:.1f}s 歌曲），开始 reserve"
    )
    start_at, finish_at = await pipeline_state.reserve(stream_id, total_duration)

    async def _background() -> None:
        """后台播放：等到 start_at → 开场静默 → 播放整轨。"""

        await _wait_until(start_at, stream_id=stream_id, finish_at=finish_at)
        if pre_delay > 0:
            logger.info(
                f"[bg_sing {stream_id[:8]}] 开场停顿 {pre_delay:.1f}s 后开唱《{song_name}》"
            )
            await asyncio.sleep(pre_delay)
        logger.info(f"[bg_sing {stream_id[:8]}] 开唱：《{song_name}》")
        await _play_track(
            audio_bytes=audio_bytes,
            inst_bytes=inst_bytes,
            audio_player=audio_player,
            performer=performer,
            timeline=timeline,
            song_name=song_name,
        )
        logger.info(f"[bg_sing {stream_id[:8]}] 唱完了：《{song_name}》")

    create_background_task(
        _guard_background(_background(), stream_id=stream_id, label="bg_sing"),
        name=f"anima_chatter.background_sing.{stream_id[:8]}",
        metadata={"stream_id": stream_id, "kind": "background_sing"},
    )

    logger.info(
        f"已派发后台播放、Action 立即返回（《{song_name}》，预计 {total_duration:.1f}s）"
    )
    return True, (
        f"已派发歌曲《{song_name}》到后台播放队列"
        f"（总时长 {total_duration:.1f}s，流水线已启用）"
    )


# ── 后台任务共享工具 ───────────────────────────────────────


async def _wait_until(start_at: float, *, stream_id: str, finish_at: float) -> None:
    """睡到 reserve 给出的起播时刻。

    Args:
        start_at: ``time.monotonic()`` 口径的起播时刻。
        stream_id: 所属聊天流，仅用于日志。
        finish_at: 预计结束时刻，仅用于日志。
    """

    now = time.monotonic()
    wait = start_at - now
    if wait <= 0:
        logger.info(f"[{stream_id[:8]}] 队列已空，立即开始播放")
        return
    logger.info(
        f"[{stream_id[:8]}] 排队中：等待 {wait:.2f}s 到起播时刻"
        f"（预计 {finish_at - now:.2f}s 后结束）"
    )
    await asyncio.sleep(wait)


async def _guard_background(
    coro: Coroutine[Any, Any, None],
    *,
    stream_id: str,
    label: str,
) -> None:
    """包裹后台播放协程，统一处理取消与异常。

    流水线模式下 Action 已经返回 Success，把错误反馈给 LLM 的成本远高于记日志，
    因此这里只记录不抛出。

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
    except Exception as exc:  # noqa: BLE001 - 后台任务不能让异常逃逸到事件循环
        logger.error(f"[{label} {stream_id[:8]}] 后台播放异常: {exc}", exc_info=True)
