"""设置虚拟形象的持续身体控制参数。"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ValidationError

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

from ..protocol import require_plugin
from ..vts.animation.body import BodyControl, inject_body_schema

logger = get_logger("anima_chatter.action.control_body")


class ControlBodyAction(BaseAction):
    """直接设置虚拟形象的呼吸、眨眼或眼睑控制。"""

    name = "control_body"
    associated_types: ClassVar[list[str]] = ["voice", "text"]
    description = (
        "设置虚拟形象的呼吸、眨眼或左右眼睑参数。控制持续生效，"
        "直到再次设置或通过 release 释放对应眼睑。"
    )
    chatter_allow: ClassVar[list[str]] = ["anima_chatter"]

    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        """返回包含严格 body 控制参数的工具 schema。"""

        return inject_body_schema(super().to_schema())

    async def go_activate(self) -> bool:
        """仅向直播流开放身体控制。"""

        return self.chat_stream.platform == "live"

    async def execute(self, body: dict[str, Any]) -> tuple[bool, str]:
        """校验并立即应用身体控制，不获取语音播放锁。

        Args:
            body: BodyControl 命令字段。

        Returns:
            ``(是否成功, 结果描述)``。
        """

        try:
            control = BodyControl.model_validate(body)
        except ValidationError as error:
            return False, f"body 参数无效：{error.errors()[0]['msg']}"

        plugin = require_plugin(self.plugin)
        performer = plugin.get_active_performer()
        if performer is None or not performer.is_ready:
            return False, "VTube Studio 表演器未就绪，未应用身体控制"

        try:
            revision = performer.reserve_body_control()
            applied = await performer.apply_body_control(control, revision=revision)
        except Exception as error:  # noqa: BLE001 - performer 错误需转换为 Action 反馈
            logger.error(f"应用身体控制失败：{type(error).__name__}")
            return False, "身体控制应用失败"
        if not applied:
            logger.warning("表演器拒绝应用身体控制")
            return False, "表演器拒绝应用身体控制"
        return True, "身体控制已应用"


__all__ = ["ControlBodyAction"]
