"""音频包络计算与时间戳追踪。

把一段已经解码到 numpy 的 PCM 数据（float32, mono / stereo 都行）按固定步长
切成 RMS 序列，再用 :class:`EnvelopeTracker` 记录"播放开始时间戳"，让消费方
（:mod:`voice_chatter.vts.animation.speech`）按当前时刻 ``time.monotonic()``
查到对应位置的归一化包络值。

这是"伪流式"——音频本身在 ``sounddevice`` 那边一次性丢进去播放，我们靠
预计算 + 时间戳把"播放进度"还原出来，省得改 sd.play 的降级链路。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class EnvelopeFrame:
    """单帧包络数据。

    - ``rms``：归一化到 0~1 的实时音量包络（已经做过对数压缩 + EMA 平滑）。
    - ``velocity``：相邻两帧 rms 差的绝对值（也归一化），代表"语调突变量"，
      用来驱动身体的"节拍感"晃动。
    """

    rms: float = 0.0
    velocity: float = 0.0


def compute_envelope(
    data: np.ndarray,
    samplerate: int,
    *,
    hop_seconds: float = 1.0 / 30.0,
    ema_alpha: float = 0.3,
) -> list[float]:
    """把 PCM 数据切成包络序列（一般每 30Hz 一帧）。

    步骤：

    1. 单声道化：双声道直接 ``mean(axis=1)``。
    2. 按 hop_size 切成不重叠的窗口（足够覆盖 30Hz 帧率，简单可靠）。
    3. 每窗算 RMS（无对数压缩——TTS 输出动态范围本身就不大；如果以后想突出弱
       音节再考虑加 ``log1p``）。
    4. 单极 EMA 低通去抖（``alpha`` 越小越平滑）。
    5. 找到全段最大值做归一化，输出 0~1 浮点列表。

    Args:
        data: float32 或可被 cast 为 float32 的 PCM 数据。形状 ``(n,)`` 或 ``(n, ch)``。
        samplerate: 采样率（Hz）。
        hop_seconds: 每帧步长（秒）。默认 1/30 ≈ 33ms，与动画循环 30Hz 对齐。
        ema_alpha: EMA 平滑系数（0~1）。0.3 是经验值——足够去抖又不至于把"啊"
            这种短爆发音糊掉。

    Returns:
        归一化到 0~1 的包络浮点列表；空数据返回空列表。
    """

    if data is None or len(data) == 0:
        return []

    # 1) 多声道 → 单声道
    pcm = data
    if pcm.ndim == 2 and pcm.shape[1] > 1:
        pcm = pcm.mean(axis=1)
    pcm = np.ascontiguousarray(pcm, dtype=np.float32)

    # 2) 按 hop 切窗
    hop_size = max(1, int(samplerate * hop_seconds))
    n_frames = len(pcm) // hop_size
    if n_frames <= 0:
        return []

    # 3) 每窗 RMS（向量化）
    trimmed = pcm[: n_frames * hop_size].reshape(n_frames, hop_size)
    raw_rms = np.sqrt(np.mean(trimmed * trimmed, axis=1) + 1e-12)

    # 4) EMA 平滑
    smoothed = np.empty_like(raw_rms)
    prev = float(raw_rms[0])
    for i, val in enumerate(raw_rms):
        prev = ema_alpha * float(val) + (1.0 - ema_alpha) * prev
        smoothed[i] = prev

    # 5) 归一化（除以全段峰值）
    peak = float(smoothed.max()) or 1.0
    normalized = smoothed / peak

    return [float(v) for v in normalized]


class EnvelopeTracker:
    """以"播放开始时间戳 + envelope 序列"还原当前播放进度的包络值。

    生命周期：

    1. ``begin(envelope, hop_seconds)``：播放即将开始时调用，记录开始时间戳。
    2. ``current()``：消费方（如动画器）每帧查询，返回 :class:`EnvelopeFrame`。
       - 在 envelope 范围内：按 ``elapsed / hop`` 索引取值。
       - 范围外（音频已播完）：返回 0 frame，让消费方做"渐回"动画。
    3. ``end()``：播放结束时清空 envelope，让 ``current()`` 返回 0。

    所有方法都加了线程锁，因为 ``begin`` / ``end`` 在 asyncio 主循环跑，
    而 ``current()`` 可能在 30Hz 动画循环跑（同一 event loop 下其实不会并发，
    但 sounddevice executor 线程里如果有调用就会撞，所以加锁更安全）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._envelope: list[float] = []
        self._hop_seconds: float = 1.0 / 30.0
        self._start_time: Optional[float] = None
        self._prev_rms: float = 0.0

    def begin(self, envelope: list[float], hop_seconds: float = 1.0 / 30.0) -> None:
        """音频开始播放：写入 envelope 并打上开始时间戳。"""

        with self._lock:
            self._envelope = list(envelope) if envelope else []
            self._hop_seconds = max(1e-3, hop_seconds)
            self._start_time = time.monotonic() if self._envelope else None
            self._prev_rms = 0.0

    def end(self) -> None:
        """音频结束：标记 tracker 不再活跃，让 ``current()`` 返回 0。"""

        with self._lock:
            self._envelope = []
            self._start_time = None
            self._prev_rms = 0.0

    @property
    def is_active(self) -> bool:
        """是否处于播放中（有 envelope 且未越界）。"""

        with self._lock:
            return self._start_time is not None and bool(self._envelope)

    def current(self) -> EnvelopeFrame:
        """根据当前 monotonic 时间查表，返回这一帧的包络值。

        Returns:
            :class:`EnvelopeFrame`。如果未开始 / 已超出 envelope 长度，返回零值。
        """

        with self._lock:
            if self._start_time is None or not self._envelope:
                return EnvelopeFrame()

            elapsed = time.monotonic() - self._start_time
            idx = int(elapsed / self._hop_seconds)
            if idx < 0:
                return EnvelopeFrame()
            if idx >= len(self._envelope):
                # 音频已播完但 end() 还没被调用——返回 0，让动画自然渐回。
                return EnvelopeFrame()

            rms = self._envelope[idx]
            velocity = abs(rms - self._prev_rms)
            self._prev_rms = rms
            return EnvelopeFrame(rms=rms, velocity=velocity)


__all__ = ["EnvelopeFrame", "EnvelopeTracker", "compute_envelope"]
