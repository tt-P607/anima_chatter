"""anima_chatter 的 VTB 模式控制命令。

提供 ``/vtb on`` / ``/vtb off`` / ``/vtb status`` 三个子命令，让用户可以在
任意聊天流中显式接管或释放 anima_chatter（VTB 模式）。

接管原理：
- 调用 :func:`chat_api.unregister_active_chatter` 释放当前 stream 的活跃 chatter。
- 调用 :func:`chat_api.register_active_chatter` 显式绑定 anima_chatter 实例。
- 释放后下一轮自动绑定回 default_chatter（或其他评分更高的 chatter）。

注意：
- 该命令仅控制 *当前 stream*，不支持跨 stream 接管。
- ASR 实时通话流（platform=local_asr）会自动绑定 anima_chatter，无需用本命令。
"""

from __future__ import annotations

from src.app.plugin_system.api import chat_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

from . import call_state
from ._internal_compat import restart_stream_loop
from .constants import CHATTER_SIGNATURE as _CHATTER_SIGNATURE
from .voice_call_lifecycle import finalize_call


logger = get_logger("anima_chatter.commands")


class VTBCommand(BaseCommand):
    """``/vtb`` 命令组：在当前聊天流上手动启用/关闭 anima_chatter 的 VTB 模式。"""

    command_name: str = "vtb"
    command_description: str = (
        "VTube Studio 虚拟形象模式控制：on=接管当前聊天流；"
        "off=释放接管；status=查看当前接管状态。"
    )
    permission_level: PermissionLevel = PermissionLevel.OWNER

    async def _reply(self, text: str) -> None:
        """向当前聊天流发送一条命令回执文本。"""

        await send_text(text, stream_id=self.stream_id)

    async def _force_restart_loop(self) -> None:
        """重启当前流的循环，丢弃缓存的旧 chatter 生成器。

        StreamLoopManager 会缓存 ``chatter.execute()`` 返回的异步生成器到
        ``_chatter_genes`` 字典里。如果不重启循环，仅替换 chatter 实例无法
        让下一 tick 用上新 chatter；旧生成器仍会被 ``asend`` 推进，导致
        命令 "失效"。
        """

        try:
            await restart_stream_loop(self.stream_id)
        except Exception as exc:
            logger.warning(
                f"重启流循环失败 stream={self.stream_id}: {exc}",
                exc_info=True,
            )

    @cmd_route("on")
    async def handle_on(self) -> tuple[bool, str]:
        """在当前聊天流接管为 anima_chatter（VTB 模式）。"""

        chatter_cls = chat_api.get_chatter_class(_CHATTER_SIGNATURE)
        if chatter_cls is None:
            await self._reply("未找到 anima_chatter 组件，无法接管。请检查插件是否启用。")
            return False, "anima_chatter 未注册"

        existing = chat_api.get_chatter_by_stream(self.stream_id)
        if existing is not None and existing.__class__ is chatter_cls:
            await self._reply("当前聊天流已经处于 VTB 模式。")
            return True, "already active"

        instance = chatter_cls(stream_id=self.stream_id, plugin=self.plugin)
        chat_api.bind_chatter_for_stream(self.stream_id, instance)
        # 重启流循环：销毁旧 chatter 生成器，下一 tick 会用 anima_chatter 重建。
        await self._force_restart_loop()

        await self._reply(
            "✓ 已切换到 VTB 模式：本聊天流现在由 anima_chatter 接管，"
            "回复将经 TTS+VTube Studio 表演。"
        )
        logger.info(f"VTB 接管 stream={self.stream_id}")
        return True, "vtb on"

    @cmd_route("off")
    async def handle_off(self) -> tuple[bool, str]:
        """释放 anima_chatter 接管，恢复默认 chatter。"""

        existing = chat_api.get_chatter_by_stream(self.stream_id)
        if existing is None:
            await self._reply("当前聊天流没有活跃 chatter，无需释放。")
            return True, "noop"

        if existing.__class__.get_signature() != _CHATTER_SIGNATURE:
            await self._reply(
                "当前聊天流目前由其他 chatter 接管（"
                f"{existing.chatter_name}），未做更改。"
                "如需切换，请使用对应的接管命令。"
            )
            return True, "not vtb"

        chat_api.restore_stream_to_default(self.stream_id)
        # 重启流循环：销毁旧 anima_chatter 生成器，下一 tick 会按
        # ChatType / platform 自动绑回 default_chatter。
        await self._force_restart_loop()

        await self._reply(
            "✓ VTB 模式已关闭，下一轮将自动绑回默认聊天器。"
        )
        logger.info(f"VTB 释放 stream={self.stream_id}")
        return True, "vtb off"

    @cmd_route("status")
    async def handle_status(self) -> tuple[bool, str]:
        """查看当前聊天流的 chatter 接管情况。"""

        existing = chat_api.get_chatter_by_stream(self.stream_id)
        if existing is None:
            await self._reply(
                "当前聊天流暂未绑定 chatter（即使发消息也尚未启动循环）。"
            )
            return True, "no chatter"

        signature = existing.__class__.get_signature() or "<unknown>"
        is_vtb = signature == _CHATTER_SIGNATURE
        platform = ""
        try:
            if self._message is not None:
                platform = self._message.platform or ""
        except Exception:
            platform = ""

        lines = [
            f"当前 chatter：{existing.chatter_name} ({signature})",
            f"是否 VTB 接管：{'是' if is_vtb else '否'}",
            f"当前平台：{platform or '(未知)'}",
        ]
        await self._reply("\n".join(lines))
        return True, "status reported"


class VoiceCommand(BaseCommand):
    """``/voice`` 命令组：手动控制语音通话功能。

    主要作为兜底——正常情况下模型会调用 ``end_voice_call`` action 自然挂断。
    本命令在以下场景使用：

    - 模型卡住没有调用 end_voice_call（例如 LLM 异常 / 推理超时）
    - 用户想强制挂断
    - 状态被异常残留（极端情况下需要清理）

    可用子命令：

    - ``/voice off``：在当前 stream 强制挂断当前通话（OWNER 权限）
    - ``/voice status``：查看当前是否处于通话中
    """

    command_name: str = "voice"
    command_description: str = "语音通话兜底控制：off=强制挂断；status=查看通话状态。"
    permission_level: PermissionLevel = PermissionLevel.OWNER

    async def _reply(self, text: str) -> None:
        """向当前聊天流发送一条命令回执文本。"""

        await send_text(text, stream_id=self.stream_id)

    @cmd_route("off")
    async def handle_off(self) -> tuple[bool, str]:
        """在当前 stream 强制挂断通话。"""

        active = await call_state.get_active_call()
        if active is None:
            await self._reply("当前没有进行中的语音通话。")
            return True, "no active call"

        if active.caller_stream_id != self.stream_id:
            await self._reply(
                "当前 stream 不是通话发起方。请到通话发起的 stream 里执行 ``/voice off``，"
                f"或在那里挂断（通话发起方 stream={active.caller_stream_id}）。"
            )
            return False, "stream mismatch"

        # 走 _finalize_call 公共路径，保证副作用顺序与 EndVoiceCallAction 一致。
        platform = ""
        try:
            if self._message is not None:
                platform = self._message.platform or ""
        except Exception:
            platform = ""

        ok, msg = await finalize_call(
            stream_id=self.stream_id,
            platform=platform,
            farewell="通话已被手动挂断。",
            end_reason="user",
            plugin=self.plugin,
        )
        if ok:
            logger.info(f"用户手动挂断通话 stream={self.stream_id}")
            await self._reply("✓ 通话已挂断。")
            return True, "voice off"
        await self._reply(f"挂断失败：{msg}")
        return False, msg

    @cmd_route("status")
    async def handle_status(self) -> tuple[bool, str]:
        """查看当前是否有通话进行中。"""

        active = await call_state.get_active_call()
        if active is None:
            await self._reply("当前没有进行中的语音通话。")
            return True, "no call"

        remaining = await call_state.get_remaining_seconds()
        remaining_str = f"{remaining:.0f}s" if remaining is not None else "unknown"
        lines = [
            f"通话发起 stream：{active.caller_stream_id}",
            f"已持续：{int(__import__('time').time() - active.started_at)}s",
            f"剩余超时：{remaining_str}",
            f"通话期间消息数：{len(active.messages_in_call)}",
        ]
        await self._reply("\n".join(lines))
        return True, "status reported"


__all__ = ["VTBCommand", "VoiceCommand"]
