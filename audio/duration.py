"""音频时长读取工具。

供 Action 层计算 TTS / 歌曲时长，进而向
[`runtime/pipeline_state.py`](../runtime/pipeline_state.py:1) 申请播放时段。

三个入口：

- :func:`read_duration_from_bytes` —— 从内存音频 bytes 读时长（``soundfile.info``
  仅读 header，不解码，O(1) 开销）。
- :func:`read_duration_from_path` —— 从磁盘文件读时长。
- :func:`estimate_tts_duration_by_chars` —— 合成前按字数粗估，用于 reserve 占位。

前两者失败时返回 ``None``，调用方通常退化为按字数估算。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import soundfile as sf  # type: ignore[import-untyped]

from src.app.plugin_system.api.log_api import get_logger


__all__ = [
    "estimate_tts_duration_by_chars",
    "read_duration_from_bytes",
    "read_duration_from_path",
]


logger = get_logger("anima_chatter.audio.duration")


# 中文 TTS 平均语速（字/秒）。经验值，覆盖大部分 GSV / qwen-tts 风格。
_AVG_CHARS_PER_SECOND = 4.5


def _duration_from_info(info: Any) -> float | None:
    """从 ``soundfile`` 的 info 对象算出时长。

    Args:
        info: ``soundfile.info()`` 的返回值。

    Returns:
        时长秒数；帧数或采样率无效时返回 ``None``。
    """

    frames = info.frames
    samplerate = info.samplerate
    if frames <= 0 or samplerate <= 0:
        return None
    return float(frames) / float(samplerate)


def read_duration_from_bytes(audio: bytes) -> float | None:
    """从音频 bytes 读取时长（秒）。

    支持 WAV / FLAC / OGG；MP3 取决于 libsndfile 是否启用了 mp3 解码。

    Args:
        audio: 音频文件的原始 bytes（如 TTS 合成结果）。

    Returns:
        时长秒数；读取失败时返回 ``None``（记调试日志）。
    """

    if not audio:
        return None
    try:
        with io.BytesIO(audio) as buffer:
            return _duration_from_info(sf.info(buffer))
    except (RuntimeError, ValueError) as exc:
        logger.debug(f"读取音频 bytes 时长失败: {exc}")
        return None


def read_duration_from_path(path: Path) -> float | None:
    """从磁盘文件读取音频时长（秒）。

    Args:
        path: 文件绝对路径。

    Returns:
        时长秒数；读取失败时返回 ``None``（记调试日志）。
    """

    try:
        return _duration_from_info(sf.info(str(path)))
    except (OSError, RuntimeError, ValueError) as exc:
        logger.debug(f"读取文件时长失败 path={path.name}: {exc}")
        return None


def estimate_tts_duration_by_chars(
    text: str,
    *,
    chars_per_second: float = _AVG_CHARS_PER_SECOND,
) -> float:
    """基于字符数估算 TTS 时长（秒）。

    粗估用，服务于 reserve 之前"还没合成就要占时间轴"的场景。**不要**用作最终
    时长——合成完后应该用 :func:`read_duration_from_bytes` 修正。

    Args:
        text: 待合成文本。
        chars_per_second: 平均语速（字/秒）。

    Returns:
        估算时长（秒）；文本为空时返回 ``0.0``。
    """

    stripped = (text or "").strip()
    if not stripped:
        return 0.0
    return len(stripped) / max(0.1, chars_per_second)
