"""直播语音标记、PCM 合成协调和说话与唱歌的有序播放。

直播动作使用 ``streaming`` 直调 TTS service，``playback`` 管理 FIFO 与资源清理。
"""

from __future__ import annotations

from .backend import (
    PCMStream,
    TTSRequest,
    TTSService,
    get_tts_service,
)
from .markers import (
    SpeechSegment,
    parse_speech_segments,
    split_complete_sentences,
    strip_markers,
)
from .playback import (
    PerformanceStyle,
    dispatch_track_pipelined,
    estimate_segments_duration,
    play_track_blocking,
)
from .synthesis import build_segment_markers

__all__ = [
    "PCMStream",
    "PerformanceStyle",
    "SpeechSegment",
    "TTSRequest",
    "TTSService",
    "build_segment_markers",
    "dispatch_track_pipelined",
    "estimate_segments_duration",
    "get_tts_service",
    "parse_speech_segments",
    "play_track_blocking",
    "split_complete_sentences",
    "strip_markers",
]
