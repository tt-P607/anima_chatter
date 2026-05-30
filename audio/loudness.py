"""anima_chatter 音频响度归一化工具。

主要用于直播清唱：用户的翻唱歌曲音量参差不齐，直接播放会让直播间观众
听到一会儿响一会儿轻，体验差。本模块提供轻量 RMS 归一化 + 峰值软限幅，
**不引入 ffmpeg / pyloudnorm 等额外依赖**——只用 numpy + soundfile，
和 anima_chatter 现有依赖一致。

为什么不用 EBU R128 / LUFS？
- 标准 LUFS 测量需要 K-weighting 滤波器（IIR 双二阶串联），实现复杂；
- 直播场景观众感知差异主要来自"整体响度落差"，RMS 归一化已经能解决 90%
  问题，再要更准可以接 ``pyloudnorm`` 包但需要单独装；
- 这套实现的开销是 O(N)，5 分钟 44.1kHz 立体声大概 100ms 内跑完。

用法::

    raw = Path("song.mp3").read_bytes()
    normalized = normalize_audio_bytes(raw, target_dbfs=-20.0)
    await audio_player.play_audio(normalized)
"""

from __future__ import annotations

import io
import math
from typing import Final

import numpy as np
import soundfile as sf  # type: ignore

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("anima_chatter.audio.loudness")


# 归一化后允许的峰值上限（防 clipping）。0.95 是常用余量，留 5% 给数字采样
# 边界误差，避免出现尖锐的削波声。
_PEAK_CEILING: Final[float] = 0.95

# 计算 RMS 时丢弃幅度低于此阈值的样本——纯静音不计入；
# 0.001 ≈ -60 dBFS，刚好能过滤掉清唱前后的静默尾音，又不会把弱拍当成静音。
_SILENCE_FLOOR: Final[float] = 0.001


def _dbfs_to_linear(dbfs: float) -> float:
    """``-20 dBFS`` → 0.1 这种线性幅度转换。"""

    return float(10.0 ** (dbfs / 20.0))


def _linear_to_dbfs(linear: float) -> float:
    """``0.1`` → -20 dBFS，反向用于日志显示。"""

    if linear <= 1e-12:
        return -math.inf
    return 20.0 * math.log10(linear)


def _compute_rms(data: np.ndarray) -> float:
    """计算样本的均方根（RMS），跳过低于静音阈值的部分。

    如果整段都是静音返回 0；调用方应该跳过归一化避免除零。
    """

    if data.size == 0:
        return 0.0
    flat = data.flatten().astype(np.float64)
    abs_flat = np.abs(flat)
    mask = abs_flat > _SILENCE_FLOOR
    valid = flat[mask]
    if valid.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(valid * valid)))


def normalize_audio_array(
    data: np.ndarray,
    *,
    target_dbfs: float = -20.0,
    peak_ceiling: float = _PEAK_CEILING,
) -> np.ndarray:
    """在 numpy 数组层面做 RMS 归一化 + 峰值软限幅。

    步骤：
    1. 计算当前 RMS（跳过静音样本）。
    2. 算到目标 RMS 所需的增益系数。
    3. 应用增益。
    4. 检查峰值，超 ``peak_ceiling`` 则按比例缩回。

    Args:
        data: ``float32`` / ``float64`` numpy 数组，单声道一维或多声道二维。
        target_dbfs: 目标 RMS 响度（dBFS），默认 -20，常见直播 / 流媒体值。
            想更响可以调到 -16，更柔和到 -24。
        peak_ceiling: 峰值上限，默认 0.95。归一化后峰值若超此值，整体再缩到此值。

    Returns:
        归一化后的 ``float32`` 数组，shape 与输入一致。
    """

    if data.size == 0:
        return data.astype(np.float32, copy=False)

    rms = _compute_rms(data)
    if rms <= _SILENCE_FLOOR:
        # 整段静音，直接原样返回，避免除零放大噪声地板
        return data.astype(np.float32, copy=False)

    target_linear = _dbfs_to_linear(target_dbfs)
    gain = target_linear / rms

    # 应用增益
    normalized = (data.astype(np.float32, copy=False) * gain).astype(np.float32)

    # 峰值软限幅：超 ceiling 整体缩回
    peak = float(np.max(np.abs(normalized)))
    if peak > peak_ceiling:
        scale = peak_ceiling / peak
        normalized = (normalized * scale).astype(np.float32)
        logger.debug(
            f"loudness: peak={peak:.3f} > ceiling={peak_ceiling}, "
            f"applied scale={scale:.3f}"
        )

    final_rms = _compute_rms(normalized)
    logger.debug(
        f"loudness: src_rms={_linear_to_dbfs(rms):.1f}dBFS → "
        f"out_rms={_linear_to_dbfs(final_rms):.1f}dBFS "
        f"(target={target_dbfs:.1f}, gain×={gain:.3f})"
    )
    return normalized


def normalize_audio_bytes(
    audio_bytes: bytes,
    *,
    target_dbfs: float = -20.0,
    peak_ceiling: float = _PEAK_CEILING,
    output_format: str = "WAV",
    output_subtype: str = "PCM_16",
) -> bytes:
    """读 → 归一化 → 重编码为 WAV bytes。

    输入支持任何 ``soundfile`` 能解码的格式（mp3 / flac / wav / ogg 等）。
    输出固定为 WAV PCM_16，因为：
    - WAV 解码无依赖、所有播放设备都吃；
    - PCM_16 是 16-bit 整数，比 float32 体积小一半，传输 / 缓存更友好；
    - audio_player 用 ``soundfile.read`` 解码，对 WAV 处理最快。

    Args:
        audio_bytes: 原始音频文件字节（mp3 / wav / flac 等）。
        target_dbfs: 目标 RMS 响度（dBFS），默认 -20。
        peak_ceiling: 峰值上限，默认 0.95。
        output_format: 输出格式，默认 ``"WAV"``。
        output_subtype: 输出 sub-type，默认 ``"PCM_16"``。

    Returns:
        归一化后重新编码的 WAV bytes；输入解码失败时返回原 bytes。
    """

    if not audio_bytes:
        return audio_bytes

    try:
        with io.BytesIO(audio_bytes) as buf:
            data, samplerate = sf.read(buf, dtype="float32")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"loudness: 解码失败，跳过归一化（直接返回原 bytes）: {exc}")
        return audio_bytes

    normalized = normalize_audio_array(
        data,
        target_dbfs=target_dbfs,
        peak_ceiling=peak_ceiling,
    )

    try:
        out_buf = io.BytesIO()
        sf.write(
            out_buf,
            normalized,
            samplerate,
            format=output_format,
            subtype=output_subtype,
        )
        return out_buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"loudness: 重编码失败，返回原 bytes: {exc}")
        return audio_bytes


__all__ = [
    "normalize_audio_array",
    "normalize_audio_bytes",
]
