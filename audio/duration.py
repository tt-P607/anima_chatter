"""音频时长读取工具。

供 :mod:`plugins.anima_chatter.actions` 计算 TTS / 歌曲时长，进而向
:mod:`plugins.anima_chatter.pipeline_state` 申请播放时段（reserve）。

两个入口：

- :func:`read_duration_from_bytes` —— 从内存里的 wav/mp3/flac bytes 读取
  时长（用 ``soundfile.info`` 仅读 header，不解码）。TTS 合成结果走这条。
- :func:`read_duration_from_path` —— 从磁盘文件路径读取时长。``sing_song``
  的歌库走这条（其实 SongInfo 已经预读过了，本函数仅作兜底）。

失败时返回 ``None``，调用方自行 fallback——通常是退化为基于文本字数估算。
"""

from __future__ import annotations

import io
from pathlib import Path

import soundfile as sf  # type: ignore

from src.app.plugin_system.api.log_api import get_logger


__all__ = [
    "estimate_tts_duration_by_chars",
    "read_duration_from_bytes",
    "read_duration_from_path",
]


logger = get_logger("anima_chatter.audio.duration")


# 中文 TTS 平均语速（字/秒）。经验值，覆盖大部分 GSV/qwen-tts 风格。
# 用于 reserve 之前的"粗估"——真正合成完后会用 read_duration_from_bytes 修正。
_AVG_CHARS_PER_SECOND = 4.5


def read_duration_from_bytes(audio: bytes) -> float | None:
    """从音频 bytes 读取时长（秒）。

    通过 ``soundfile.info()`` 仅读 header，不解码完整波形（O(1) 开销）。
    支持 WAV / FLAC / OGG；MP3 取决于 libsndfile 是否支持。

    Args:
        audio: 音频文件的原始 bytes（如 TTS 合成结果）。

    Returns:
        时长秒数；失败时返回 ``None``（调试日志）。
    """

    if not audio:
        return None
    try:
        with io.BytesIO(audio) as buf:
            info = sf.info(buf)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"读取音频 bytes 时长失败: {exc}")
        return None
    frames = getattr(info, "frames", 0) or 0
    samplerate = getattr(info, "samplerate", 0) or 0
    if frames <= 0 or samplerate <= 0:
        return None
    return float(frames) / float(samplerate)


def read_duration_from_path(path: Path) -> float | None:
    """从磁盘文件读取音频时长（秒）。

    Args:
        path: 文件绝对路径。

    Returns:
        时长秒数；失败返回 ``None``。
    """

    try:
        info = sf.info(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"读取文件时长失败 path={path.name}: {exc}")
        return None
    frames = getattr(info, "frames", 0) or 0
    samplerate = getattr(info, "samplerate", 0) or 0
    if frames <= 0 or samplerate <= 0:
        return None
    return float(frames) / float(samplerate)


def estimate_tts_duration_by_chars(text: str, *, chars_per_second: float | None = None) -> float:
    """基于字符数估算 TTS 时长（秒）。

    粗估用，用于 reserve 之前还没合成时的占位。**不要**用作最终时长——
    合成完后应该用 :func:`read_duration_from_bytes` 修正。

    Args:
        text: 待合成文本。
        chars_per_second: 平均语速（字/秒）。默认 4.5（中文 GSV/qwen-tts 经验）。

    Returns:
        估算秒数（≥0.5）。空文本返回 0.5（最小占位）。
    """

    speed = float(chars_per_second or _AVG_CHARS_PER_SECOND)
    if speed <= 0:
        speed = _AVG_CHARS_PER_SECOND
    chars = len((text or "").strip())
    if chars == 0:
        return 0.5
    return max(0.5, chars / speed)
