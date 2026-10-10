"""挂起等待动作。

``pass_and_wait`` 在三种模式下行为一致——告诉 chatter 本轮动作完成后挂起，等
用户继续说话或等待固定秒数后恢复。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseAction


class AnimaPassAndWaitAction(BaseAction):
    """登记等待点：等用户继续说话，或等指定秒数后主动恢复。"""

    name = "pass_and_wait"
    associated_types = ["text"]
    description = (
        "为当前实时通话 / 虚拟形象会话登记等待点。说完话后调用它等待用户继续说话；"
        "seconds 为空时等待新输入，传入秒数时到时主动恢复。"
        "本动作只影响 chatter 是否挂起，不影响已经派发的 TTS / VTS 表演。"
    )
    chatter_allow = ["anima_chatter"]

    async def execute(
        self,
        seconds: Annotated[float | None, "等待秒数；为空则等待新的用户输入"] = None,
    ) -> tuple[bool, str]:
        """登记等待状态。

        Args:
            seconds: 等待秒数；``None`` 表示等待新的用户输入。

        Returns:
            ``(True, 结果描述)``。
        """

        if seconds is None:
            return True, "已登记等待新的用户输入"
        return True, f"已登记等待 {seconds} 秒后继续会话"


__all__ = ["AnimaPassAndWaitAction"]
