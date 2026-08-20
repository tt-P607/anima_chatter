"""二阶弹簧阻尼系统（Second-Order Dynamics）。

用于身体跟随头部等"带惯性、可回弹"的平滑耦合：以固有频率（响应速度）、阻尼比
（是否过冲回弹）和初始加速度响应为参数，对一个目标值做持续的隐式欧拉积分，
输出一条具备"启动滞后 → 平滑跟进 → 微小回弹"力学质感的轨迹。

相比指数趋近的一阶平滑（只能逼近、无惯性/回弹），它能更真实地模拟真实人体
"骨骼带肌肉、带质量"的从动感，是消除假人感的关键工具。仅依赖标准库 ``math``。
"""

from __future__ import annotations

import math


class SecondOrderDynamics:
    """通用二阶动力学平滑器。

    典型用例：把头部角度加权后作为目标值 ``x`` 逐帧送入 :meth:`update`，
    返回值即为身体角度（带惯性滞后与可能的轻微回弹）。

    参数经验取值（参考图形学 / 游戏标准二阶动态系统）：

    - ``frequency``：固有频率（Hz），控制响应速度，建议 1.0 ~ 2.0。
    - ``damping``：阻尼比，1.0 为临界阻尼（无过冲），0.7 ~ 0.85 有轻微回弹。
    - ``response``：初始加速度响应系数，建议 0.0 ~ 2.0。
    """

    def __init__(
        self,
        frequency: float,
        damping: float,
        response: float,
        x0: float,
    ) -> None:
        """初始化二阶系统参数与初始状态。

        Args:
            frequency: 固有频率（Hz），越大响应越快。
            damping: 阻尼比；<1.0 会产生轻微过冲回弹。
            response: 初始加速度响应系数。
            x0: 初始位置（通常取动画器初值 0.0）。
        """
        self.k1 = damping / (math.pi * frequency)
        self.k2 = 1.0 / ((2 * math.pi * frequency) ** 2)
        self.k3 = response * damping / (2 * math.pi * frequency)
        self.xp: float = x0
        self.y: float = x0
        self.yd: float = 0.0

    def update(self, x: float, dt: float) -> float:
        """按帧推进一步，返回平滑后的当前位置。

        Args:
            x: 这一帧的目标值。
            dt: 距上一帧的间隔（秒）；<=0 时直接返回当前值，不做积分
                （避免除零与时间回退造成的跳变）。

        Returns:
            平滑后的位置（度 / 值）。
        """
        if dt <= 0.0:
            return self.y

        xd = (x - self.xp) / dt
        self.xp = x

        # 隐式欧拉法迭代位置与速度。
        self.y = self.y + dt * self.yd
        self.yd = self.yd + dt * (x + self.k3 * xd - self.y - self.k1 * self.yd) / self.k2
        return self.y


__all__ = ["SecondOrderDynamics"]
