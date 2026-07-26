"""anima_chatter 的控制命令。

- ``/vtb on|off|status``：在当前聊天流手动接管 / 释放 VTB 模式。
- ``/voice off|status``：语音通话的兜底控制（正常情况下模型会自己挂断）。

接管原理：调 ``chat_api.bind_chatter_for_stream`` 绑定本插件的 chatter，再重启
流循环让缓存的旧 chatter 生成器作废——只换实例不重启循环的话，下一 tick 仍会
推进旧生成器，命令看起来"没生效"。释放时调 ``restore_stream_to_default``，下
一轮会按评分自动绑回默认 chatter。
"""

from __future__ import annotations

import time

from src.app.plugin_system.api import chat_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

from ._internal_compat import restart_stream_loop
from .constants import CHATTER_SIGNATURE
from .protocol import require_plugin
from .runtime import call_state
from .voice_call import finalize_call


logger = get_logger("anima_chatter.commands")


__all__ = ["VTBCommand", "VoiceCommand"]


class _ReplyMixin:
    """给命令类提供统一的回执发送与流循环重启。"""

    async def _reply(self, text: str) -> None:
        """向当前聊天流发送一条命令回执。

        Args:
            text: 回执正文。
        """

        await send_text(text, stream_id=self.stream_id)  # type: ignore[attr-defined]

    async def _restart_loop(self) -> None:
        """重启当前流的循环，丢弃缓存的旧 chatter 生成器。"""

        stream_id = self.stream_id  # type: ignore[attr-defined]
        try:
            await restart_stream_loop(stream_id)
        except Exception as exc:  # noqa: BLE001 - 重启失败不应让命令崩掉
            logger.warning(f"重启流循环失败 stream={stream_id}: {exc}", exc_info=True)


class VTBCommand(_ReplyMixin, BaseCommand):
    """``/vtb`` 命令组：手动启用 / 关闭当前聊天流的 VTB 模式。"""

    name: str = "vtb"
    description: str = (
        "VTube Studio 虚拟形象模式控制：on=接管当前聊天流；"
        "off=释放接管；status=查看当前接管状态。"
    )
    permission_level: PermissionLevel = PermissionLevel.OWNER

    @cmd_route("on")
    async def handle_on(self) -> tuple[bool, str]:
        """接管当前聊天流为 VTB 模式。

        Returns:
            ``(是否成功, 结果描述)``。
        """

        chatter_cls = chat_api.get_chatter_class(CHATTER_SIGNATURE)
        if chatter_cls is None:
            await self._reply("未找到 anima_chatter 组件，无法接管。请检查插件是否启用。")
            return False, "anima_chatter 未注册"

        existing = chat_api.get_chatter_by_stream(self.stream_id)
        if existing is not None and existing.__class__ is chatter_cls:
            await self._reply("当前聊天流已经处于 VTB 模式。")
            return True, "already active"

        chat_api.bind_chatter_for_stream(
            self.stream_id, chatter_cls(stream_id=self.stream_id, plugin=self.plugin)
        )
        await self._restart_loop()

        await self._reply(
            "已切换到 VTB 模式：本聊天流现在由 anima_chatter 接管，"
            "回复将经 TTS + VTube Studio 表演。"
        )
        logger.info(f"VTB 接管 stream={self.stream_id}")
        return True, "vtb on"

    @cmd_route("off")
    async def handle_off(self) -> tuple[bool, str]:
        """释放 VTB 接管，恢复默认 chatter。

        Returns:
            ``(是否成功, 结果描述)``。
        """

        existing = chat_api.get_chatter_by_stream(self.stream_id)
        if existing is None:
            await self._reply("当前聊天流没有活跃 chatter，无需释放。")
            return True, "noop"

        if existing.get_signature() != CHATTER_SIGNATURE:
            await self._reply(
                f"当前聊天流由其他 chatter 接管（{existing.name}），未做更改。"
                "如需切换，请使用对应的接管命令。"
            )
            return True, "not vtb"

        chat_api.restore_stream_to_default(self.stream_id)
        await self._restart_loop()

        await self._reply("VTB 模式已关闭，下一轮将自动绑回默认聊天器。")
        logger.info(f"VTB 释放 stream={self.stream_id}")
        return True, "vtb off"

    @cmd_route("status")
    async def handle_status(self) -> tuple[bool, str]:
        """查看当前聊天流的 chatter 接管情况。

        Returns:
            ``(是否成功, 结果描述)``。
        """

        existing = chat_api.get_chatter_by_stream(self.stream_id)
        if existing is None:
            await self._reply("当前聊天流暂未绑定 chatter（发消息后才会启动循环）。")
            return True, "no chatter"

        signature = existing.get_signature() or "<unknown>"
        platform = self._message.platform if self._message is not None else ""
        await self._reply(
            "\n".join(
                [
                    f"当前 chatter：{existing.name} ({signature})",
                    f"是否 VTB 接管：{'是' if signature == CHATTER_SIGNATURE else '否'}",
                    f"当前平台：{platform or '(未知)'}",
                ]
            )
        )
        return True, "status reported"


class VoiceCommand(_ReplyMixin, BaseCommand):
    """``/voice`` 命令组：语音通话的兜底控制。

    正常情况下模型会调用 ``end_voice_call`` 自然挂断。本命令用于模型卡住
    （LLM 异常 / 推理超时）、用户想强制挂断，或状态异常残留的场景。
    """

    name: str = "voice"
    description: str = "语音通话兜底控制：off=强制挂断；status=查看通话状态。"
    permission_level: PermissionLevel = PermissionLevel.OWNER

    @cmd_route("off")
    async def handle_off(self) -> tuple[bool, str]:
        """在当前流强制挂断通话。

        Returns:
            ``(是否成功, 结果描述)``。
        """

        active = await call_state.get_active_call()
        if active is None:
            await self._reply("当前没有进行中的语音通话。")
            return True, "no active call"

        if active.caller_stream_id != self.stream_id:
            await self._reply(
                "当前 stream 不是通话发起方。请到通话发起的 stream 里执行 /voice off"
                f"（通话发起方 stream={active.caller_stream_id}）。"
            )
            return False, "stream mismatch"

        # 走公共路径，保证副作用顺序与 end_voice_call action 一致。
        ok, msg = await finalize_call(
            stream_id=self.stream_id,
            farewell="通话已被手动挂断。",
            end_reason="manual",
            plugin=require_plugin(self.plugin),
        )
        if ok:
            logger.info(f"用户手动挂断通话 stream={self.stream_id}")
            await self._reply("通话已挂断。")
            return True, "voice off"

        await self._reply(f"挂断失败：{msg}")
        return False, msg

    @cmd_route("status")
    async def handle_status(self) -> tuple[bool, str]:
        """查看当前是否有通话进行中。

        Returns:
            ``(是否成功, 结果描述)``。
        """

        active = await call_state.get_active_call()
        if active is None:
            await self._reply("当前没有进行中的语音通话。")
            return True, "no call"

        remaining = await call_state.get_remaining_seconds()
        remaining_str = f"{remaining:.0f}s" if remaining is not None else "unknown"
        await self._reply(
            "\n".join(
                [
                    f"通话发起 stream：{active.caller_stream_id}",
                    f"已持续：{int(time.time() - active.started_at)}s",
                    f"剩余静默超时：{remaining_str}",
                    f"通话期间消息数：{len(active.messages_in_call)}",
                ]
            )
        )
        return True, "status reported"
