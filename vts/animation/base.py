"""动画器抽象基类。

动画器每帧返回 ``param_id -> value`` 字典；上层 connection 负责合并多个
动画器的输出并以稳定频率注入到 VTube Studio。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseAnimator(ABC):
    """动画模块基类。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化动画器，可选传入配置字典（保留以兼容旧接口）。"""

        self.config: dict[str, Any] = config or {}
        self.is_active: bool = True
        self.current_params: dict[str, float] = {}

    @abstractmethod
    async def update(self, delta_time: float) -> dict[str, float]:
        """每帧调用：返回需要注入到 VTS 的参数字典。

        Args:
            delta_time: 距离上一帧的时间间隔（秒）。

        Returns:
            ``{ "ParamID": value }``。
        """

    def set_active(self, active: bool) -> None:
        """切换激活状态；非激活时 connection 可跳过 update。"""

        self.is_active = active


__all__ = ["BaseAnimator"]
