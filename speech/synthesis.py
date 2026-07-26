"""TTS 分段并发合成——``say`` 与 ``say_and_perform`` 的共享实现。

把"一段文本 → 若干 :class:`SpeechSegment` → 并发合成 → 按序消费"这条链路收
敛到一处。合成任务通过框架 task_manager 立即并发起跑，播放阶段按索引顺序
``await``，因此前一段播放期间后续段仍在合成，首句延迟只取决于第一段。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger

from .._internal_compat import create_background_task
from .backend import TTSArtifact, TTSBackend, TTSRequest, retry_empty_audio

if TYPE_CHECKING:
    from ..config import TTSSection
    from .markers import SpeechSegment


__all__ = ["build_segment_markers", "synthesize_segments"]


logger = get_logger("anima_chatter.speech.synthesis")


def build_segment_markers(
    segment: "SpeechSegment",
    tts_params: dict[str, Any],
) -> dict[str, Any]:
    """合并片段自带 markers 与本次调用的 TTS 参数。

    片段自带的行内标记优先级更高——``[emotion:...]`` 这类标记应该能覆盖 Action
    顶层参数给的默认值。

    Args:
        segment: 待合成片段。
        tts_params: Action 收到的动态 TTS 参数（style / language / speed 等）。

    Returns:
        送给 provider 的完整 markers 字典。
    """

    markers = dict(segment.markers)
    for key, value in tts_params.items():
        markers.setdefault(key, value)
    return markers


async def _synthesize_one(
    *,
    backend: TTSBackend,
    stream_id: str,
    segment: "SpeechSegment",
    tts_params: dict[str, Any],
    empty_audio_retry_count: int,
    semaphore: asyncio.Semaphore,
    index: int,
) -> TTSArtifact:
    """合成单个片段（受并发信号量约束）。

    Args:
        backend: TTS 后端。
        stream_id: 所属聊天流。
        segment: 待合成片段。
        tts_params: 动态 TTS 参数。
        empty_audio_retry_count: 空音频重试次数。
        semaphore: 并发信号量。
        index: 片段序号，仅用于日志。

    Returns:
        合成产物；失败时 ``error`` 非空，调用方按 :attr:`TTSArtifact.is_playable`
        判断是否可播。
    """

    request = TTSRequest(
        stream_id=stream_id,
        text=segment.text,
        emotion=segment.emotion,
        markers=build_segment_markers(segment, tts_params),
    )
    async with semaphore:
        artifact = await backend.synthesize(request)
        if not artifact.is_playable and empty_audio_retry_count > 0:
            artifact = await retry_empty_audio(
                backend=backend,
                request=request,
                artifact=artifact,
                retry_count=empty_audio_retry_count,
            )
    if not artifact.is_playable:
        logger.warning(f"片段 {index} 合成结果不可播放: {segment.text[:30]}...")
    return artifact


def synthesize_segments(
    *,
    backend: TTSBackend,
    stream_id: str,
    segments: list["SpeechSegment"],
    tts_params: dict[str, Any],
    section: "TTSSection",
) -> list[asyncio.Task[TTSArtifact]]:
    """并发合成全部片段，返回与 ``segments`` 同序的任务列表。

    任务立即通过 task_manager 起跑；调用方按索引顺序 ``await``，实现"边播边合成"。

    Args:
        backend: TTS 后端。
        stream_id: 所属聊天流。
        segments: 待合成片段列表。
        tts_params: 动态 TTS 参数。
        section: 插件配置的 ``tts`` 段（提供并发数与重试次数）。

    Returns:
        任务列表；下标与 ``segments`` 一一对应。
    """

    semaphore = asyncio.Semaphore(section.max_parallel_segments)
    tasks: list[asyncio.Task[TTSArtifact]] = []
    for index, segment in enumerate(segments):
        handle = create_background_task(
            _synthesize_one(
                backend=backend,
                stream_id=stream_id,
                segment=segment,
                tts_params=tts_params,
                empty_audio_retry_count=section.empty_audio_retry_count,
                semaphore=semaphore,
                index=index,
            ),
            name=f"anima_chatter.tts_segment.{stream_id[:8]}.{index}",
            metadata={"stream_id": stream_id, "kind": "tts_segment"},
        )
        tasks.append(handle.task)
    return tasks
