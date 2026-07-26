"""anima_chatter 语音合成与播放子包。

按职责分为四层：

- :mod:`.markers` — 模型输出中 ``[wait]`` / ``[emotion]`` / ``[motion]`` 标记的
  解析与句子切分，产出 :class:`SpeechSegment` 列表。
- :mod:`.backend` — TTS HTTP 客户端，把片段文本合成为音频 bytes。
- :mod:`.synthesis` — 分段并发合成调度，供 ``say`` / ``say_and_perform`` 共用。
- :mod:`.playback` — **唯一**的播放实现，同时覆盖阻塞模式与 vtb_live 流水线
  模式，供三个 Action 共用。

Action 层只负责解析自己的参数、准备音频来源，播放逻辑一律走本子包。
"""

from __future__ import annotations

from .backend import (
    TTSArtifact,
    TTSBackend,
    TTSRequest,
    build_tts_backend,
    retry_empty_audio,
)
from .markers import (
    SpeechSegment,
    parse_speech_segments,
    split_complete_sentences,
    strip_markers,
)
from .playback import (
    PerformanceStyle,
    dispatch_segments_pipelined,
    dispatch_track_pipelined,
    estimate_segments_duration,
    play_segments_blocking,
    play_track_blocking,
    should_use_pipeline,
)
from .synthesis import build_segment_markers, synthesize_segments


__all__ = [
    "PerformanceStyle",
    "SpeechSegment",
    "TTSArtifact",
    "TTSBackend",
    "TTSRequest",
    "build_segment_markers",
    "build_tts_backend",
    "dispatch_segments_pipelined",
    "dispatch_track_pipelined",
    "estimate_segments_duration",
    "parse_speech_segments",
    "play_segments_blocking",
    "play_track_blocking",
    "retry_empty_audio",
    "should_use_pipeline",
    "split_complete_sentences",
    "strip_markers",
    "synthesize_segments",
]
