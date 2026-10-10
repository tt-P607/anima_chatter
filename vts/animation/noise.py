"""一维 value noise（平滑随机），替代 sin 叠加做有机微动。

sin 波是完美周期，人眼能预判出"规律"，显得机械。value noise 是"平滑的随机
数"——随机但相邻时刻连续过渡，没有可察觉的周期，更接近真人无意识微动。

实现：把时间轴切成整数格点，每个格点用哈希得到 -1~1 的伪随机值，格点之间用
smoothstep 插值。多个倍频（octave）叠加得到更自然的层次。无外部依赖。
"""

from __future__ import annotations

import math


def _hash01(n: int, seed: int) -> float:
    """把整数格点哈希成 0~1 的伪随机值（确定性，同输入同输出）。"""

    x = (n * 374761393 + seed * 668265263) & 0xFFFFFFFF
    x = (x ^ (x >> 13)) * 1274126177 & 0xFFFFFFFF
    x = x ^ (x >> 16)
    return (x & 0xFFFFFFFF) / 0xFFFFFFFF


def _smoothstep(t: float) -> float:
    """3t²-2t³ 平滑插值，两端导数为 0，过渡无棱角。"""

    return t * t * (3.0 - 2.0 * t)


def value_noise(t: float, *, seed: int = 0) -> float:
    """单倍频 value noise，返回 -1~1 的平滑随机值。

    Args:
        t: 时间坐标（建议已乘频率系数，整数间隔约对应一次起伏）。
        seed: 随机种子，不同 seed 得到互相独立的噪声曲线。
    """

    i = math.floor(t)
    frac = t - i
    a = _hash01(i, seed)
    b = _hash01(i + 1, seed)
    return (a + (b - a) * _smoothstep(frac)) * 2.0 - 1.0


def fbm(t: float, *, seed: int = 0, octaves: int = 2) -> float:
    """多倍频叠加（fractal brownian motion），层次更丰富，仍返回 -1~1。

    每个倍频频率翻倍、幅度减半，叠加后归一化。octaves=2 已足够自然。
    """

    total = 0.0
    amplitude = 1.0
    frequency = 1.0
    norm = 0.0
    for octave in range(max(1, octaves)):
        total += value_noise(t * frequency, seed=seed + octave * 101) * amplitude
        norm += amplitude
        amplitude *= 0.5
        frequency *= 2.0
    return total / norm if norm > 0 else 0.0


__all__ = ["fbm", "value_noise"]
