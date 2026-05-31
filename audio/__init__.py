"""anima_chatter 的本地音频播放子包。"""

from .duration import (
    estimate_tts_duration_by_chars,
    read_duration_from_bytes,
    read_duration_from_path,
)
from .envelope import EnvelopeFrame, EnvelopeTracker, compute_envelope
from .loudness import normalize_audio_array, normalize_audio_bytes
from .player import AudioPlayer

__all__ = [
    "AudioPlayer",
    "EnvelopeFrame",
    "EnvelopeTracker",
    "compute_envelope",
    "estimate_tts_duration_by_chars",
    "normalize_audio_array",
    "normalize_audio_bytes",
    "read_duration_from_bytes",
    "read_duration_from_path",
]
