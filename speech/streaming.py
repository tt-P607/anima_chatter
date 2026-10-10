"""通过已注册 Provider 增量合成直播语音并按调用顺序播放。"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger

from ..runtime import pipeline_state
from ..runtime.heartbeat import feed_watchdog_during
from .backend import TTSRequest, TTSService
from .playback import (
    PerformanceStyle,
    _guard_background,
    _release_playback_turn,
    create_playback_task,
    playback_turn,
    reserve_playback_turn,
    reserve_receive_turn,
)
from .synthesis import build_segment_markers

if TYPE_CHECKING:
    from ..audio import AudioPlayer
    from ..vts import VTSPerformer
    from .markers import SpeechSegment

logger = get_logger("anima_chatter.speech.streaming")

_PCM_BLOCK_BYTES = 9600


class _BufferedSegment:
    """当前回复的 PCM 缓存，错误和 EOF 按音频字节的顺序交付。"""

    def __init__(self) -> None:
        """允许接收端缓存当前片段的全部音频。"""

        self.queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()

    async def chunks(self) -> AsyncIterator[bytes]:
        """消费接收任务产出的块，保留部分输出后的错误。"""

        while True:
            block = await self.queue.get()
            if block is None:
                return
            if isinstance(block, Exception):
                raise block
            yield block


async def play_streaming_segments(
    *,
    stream_id: str,
    provider: TTSService,
    segments: list[SpeechSegment],
    tts_params: dict[str, Any],
    performer: VTSPerformer | None,
    audio_player: AudioPlayer,
    style: PerformanceStyle,
    estimated_duration: float,
    on_segment_started: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[bool, str]:
    """提前合成停顿分隔的片段，通过同一输出流连续播放。"""

    track_id = f"pcm_{uuid.uuid4().hex[:12]}"
    reserved = await pipeline_state.reserve(
        stream_id, estimated_duration, track_id=track_id
    )
    if reserved is None:
        return False, "播放队列已满，本轮语音未排入队列；请等待当前队列消化"
    turn = reserve_playback_turn()
    receive_turn = reserve_receive_turn()
    started = False
    submitted_at = time.monotonic()
    receiver: asyncio.Task[Any] | None = None
    buffers = [_BufferedSegment() for _ in segments]

    async def finalize() -> None:
        """停止接收并释放本项的轮次容量与接收顺序。"""

        if receiver is not None:
            if not receiver.done():
                receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        _release_playback_turn(receive_turn)
        if started:
            await pipeline_state.report_actual(
                stream_id, track_id=track_id, finished_at=time.monotonic()
            )
        else:
            await pipeline_state.relinquish(stream_id, track_id=track_id)

    async def receive() -> None:
        """按顺序尽快接收全部音频，EOF 后立即请求下一段。"""

        async with playback_turn(receive_turn):
            for index, (segment, buffer) in enumerate(
                zip(segments, buffers, strict=True), start=1
            ):
                if not segment.text:
                    continue
                request = TTSRequest(
                    stream_id=stream_id,
                    text=segment.text,
                    emotion=segment.emotion,
                    markers=build_segment_markers(segment, tts_params),
                )
                try:
                    async with provider.open_pcm_stream(request) as stream:
                        if (
                            stream.sample_rate != 48000
                            or stream.channels != 1
                            or stream.sample_format != "s16le"
                        ):
                            raise ValueError("Provider PCM 必须是 48000Hz 单声道 s16le")
                        async for chunk in stream.chunks:
                            if not isinstance(chunk, bytes):
                                raise TypeError("PCM chunks must be bytes")
                            for offset in range(0, len(chunk), _PCM_BLOCK_BYTES):
                                buffer.queue.put_nowait(
                                    chunk[offset : offset + _PCM_BLOCK_BYTES]
                                )
                    logger.info(
                        f"PCM 接收结束：item={track_id}, segment={index}, "
                        f"elapsed={time.monotonic() - submitted_at:.3f}s"
                    )
                except Exception as error:
                    buffer.queue.put_nowait(error)
                    raise
                buffer.queue.put_nowait(None)

    async def continuous_pcm() -> AsyncIterator[bytes]:
        """连接原序音频与定长静音，保留每个请求的采样帧边界。"""

        for segment, buffer in zip(segments, buffers, strict=True):
            silence_frames = round(segment.wait_before * 48000)
            while silence_frames > 0:
                frames = min(silence_frames, _PCM_BLOCK_BYTES // 2)
                yield b"\x00\x00" * frames
                silence_frames -= frames
            if not segment.text:
                continue
            total_bytes = 0
            async for chunk in buffer.chunks():
                total_bytes += len(chunk)
                yield chunk
            if not total_bytes:
                raise RuntimeError("TTS 未输出可播放的 PCM")
            if total_bytes % 2:
                raise ValueError("PCM stream ended with a partial sample frame")

    async def background() -> None:
        """消费缓冲并在当前任务的表演作用域内驱动起播。"""

        nonlocal receiver
        receiver = create_playback_task(
            receive,
            name=f"anima_chatter.pcm_receive.{track_id}",
            turn=receive_turn,
            cleanup=release_receive,
        )
        async with feed_watchdog_during(stream_id), playback_turn(turn):
            session = (
                performer.speaking_session(emotion=style.emotion, intent=style.intent)
                if performer is not None
                else contextlib.nullcontext()
            )
            async with session:
                async def on_started() -> None:
                    """首次朗读 PCM 写入后同步整条回复的反馈。"""

                    nonlocal started
                    started = True
                    await pipeline_state.report_actual(
                        stream_id, track_id=track_id,
                        started_at=time.monotonic(),
                    )
                    logger.info(
                        f"PCM 实际起播：item={track_id}, "
                        f"elapsed={time.monotonic() - submitted_at:.3f}s"
                    )
                    if performer is not None:
                        await performer.start_speech_playback()
                        await performer.switch_segment_intent(
                            style.intent,
                            emotion_main_for_expression=style.emotion_main,
                        )
                    if on_segment_started is not None:
                        await on_segment_started(0)

                leading_frames = round(segments[0].wait_before * 48000)
                frames = await audio_player.play_pcm_stream(
                    continuous_pcm(), sample_rate=48000,
                    channels=1, sample_format="s16le", on_started=on_started,
                    start_after_frames=leading_frames,
                )
                if frames <= leading_frames:
                    raise RuntimeError("TTS 未输出可播放的 PCM")
        played = sum(bool(segment.text) for segment in segments)
        logger.info(
            f"PCM 播放完成：item={track_id}, segments={played}, "
            f"elapsed={time.monotonic() - submitted_at:.3f}s"
        )

    async def release_receive() -> None:
        """释放接收票据，不持有声卡播放票据。"""

        _release_playback_turn(receive_turn)

    create_playback_task(
        lambda: _guard_background(background(), stream_id=stream_id, label="pcm_play"),
        name=f"anima_chatter.pcm_play.{track_id}",
        turn=turn,
        cleanup=finalize,
    )
    logger.info(f"PCM 已接收：item={track_id}, segments={len(segments)}")
    return True, f"已接收 {len(segments)} 段语音，按队列流式播放"