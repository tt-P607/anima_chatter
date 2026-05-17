"""voice_chatter 提示词构建器。

把"模式判定（modes.py）+ 场景文案（scenes.py）+ 模板字符串（templates.py）"
组装成最终送给 LLM 的 system / user prompt。

其它模块只需要 ``from .prompts import VoiceChatterPromptBuilder`` 即可。
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING

from src.core.config import get_core_config
from src.core.prompt import get_prompt_manager

from ..modes import ChatterMode, resolve_mode
from .scenes import VOICE_SCENE_GUIDE, VTB_LIVE_SCENE_GUIDE, VTB_SCENE_GUIDE

if TYPE_CHECKING:
    from src.core.models.message import Message
    from src.core.models.stream import ChatStream

    from ..config import SherpaOnnxVoiceChatterConfig


class VoiceChatterPromptBuilder:
    """voice_chatter 提示词构建器。"""

    @staticmethod
    def resolve_mode(chat_stream: "ChatStream") -> ChatterMode:
        """根据流的 platform 自动判定模式。

        实际逻辑在 :func:`plugins.voice_chatter.modes.resolve_mode`，本方法
        只做轻包装，方便外部沿用 ``VoiceChatterPromptBuilder.resolve_mode``
        这个调用习惯。
        """

        return resolve_mode(chat_stream)

    @staticmethod
    def build_action_suspend_guidance(
        plugin_config: "SherpaOnnxVoiceChatterConfig | None",
        mode: ChatterMode = "voice",
    ) -> str:
        """构建 Action-only 回合的提示词说明（按模式描述对应 action 名）。"""

        enabled = (
            True
            if plugin_config is None
            else bool(plugin_config.plugin.enable_action_suspend)
        )
        # voice 用 say；vtb / vtb_live 都用 say_and_perform。
        action_examples = (
            "say、pass_and_wait" if mode == "voice" else "say_and_perform、pass_and_wait"
        )
        if enabled:
            return (
                f'Action: 是你在互动过程中的"动作"，例如 {action_examples}。'
                '当你只接收到 Action 的返回信息时，只需要输出"__SUSPEND__"表示当前回合挂起，'
                "等待用户继续说话或等待新的恢复事件；"
            )
        return (
            f'Action: 是你在互动过程中的"动作"，例如 {action_examples}。'
            '当你只接收到 Action 的返回信息时，不要输出"__SUSPEND__"，'
            "而应把这些回执当作常规工具结果，继续决定下一步要调用的工具或动作。"
            "如果你调用的是 pass_and_wait，则会进入等待，而不是继续追加新的调用。"
            "通常在你说完后调用来暂时挂起会话。"
        )

    @staticmethod
    def get_scene_guide(mode: ChatterMode) -> str:
        """根据模式返回 ``<scene_and_protocol>`` 段使用的场景与工具协议。"""

        if mode == "vtb_live":
            return VTB_LIVE_SCENE_GUIDE
        if mode == "vtb":
            return VTB_SCENE_GUIDE
        return VOICE_SCENE_GUIDE

    @staticmethod
    async def build_system_prompt(
        plugin_config: "SherpaOnnxVoiceChatterConfig | None",
        chat_stream: "ChatStream",
        mode: ChatterMode | None = None,
    ) -> str:
        """构建系统提示词。

        Args:
            plugin_config: 插件配置（可能为空）。
            chat_stream: 当前聊天流。
            mode: 显式指定模式；为空时根据 ``chat_stream.platform`` 自动判定。
        """

        actual_mode: ChatterMode = mode or VoiceChatterPromptBuilder.resolve_mode(chat_stream)
        tmpl = get_prompt_manager().get_template("voice_chatter_system_prompt")
        if not tmpl:
            return ""
        return await (
            tmpl.set("nickname", chat_stream.bot_nickname)
            .set(
                "action_suspend_guidance",
                VoiceChatterPromptBuilder.build_action_suspend_guidance(
                    plugin_config, actual_mode
                ),
            )
            .set("sub_agent_collaboration_extra", "")
            .set("scene_guide", VoiceChatterPromptBuilder.get_scene_guide(actual_mode))
            .build()
        )

    @staticmethod
    async def build_user_prompt(
        chat_stream: "ChatStream",
        history_text: str,
        unread_lines: str,
        extra: str = "",
        mode: ChatterMode | None = None,
    ) -> str:
        """构建用户提示词，按模式选择对应模板。"""

        actual_mode: ChatterMode = mode or VoiceChatterPromptBuilder.resolve_mode(chat_stream)
        if actual_mode == "vtb_live":
            template_name = "voice_chatter_vtb_live_user_prompt"
        elif actual_mode == "vtb":
            template_name = "voice_chatter_vtb_user_prompt"
        else:
            template_name = "voice_chatter_user_prompt"

        tmpl = get_prompt_manager().get_template(template_name)
        assert tmpl, f"缺少模板 {template_name}"
        return await (
            tmpl.set("stream_name", chat_stream.stream_name or chat_stream.stream_id)
            .set("current_time", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            .set("platform", chat_stream.platform)
            .set("history", history_text)
            .set("unreads", unread_lines)
            .set("extra", extra)
            .set("stream_id", chat_stream.stream_id or "")
            .build()
        )

    @staticmethod
    def build_history_text(
        chat_stream: "ChatStream",
        formatter: "Callable[[Message], str]",
    ) -> str:
        """构建历史消息文本。"""

        return "\n".join(formatter(msg) for msg in chat_stream.context.history_messages)

    @staticmethod
    def build_negative_behaviors_extra() -> str:
        """构建负面行为提醒。"""

        negative_behaviors = get_core_config().personality.negative_behaviors
        if not negative_behaviors:
            return ""
        return "行为提醒：请严格遵守以下约束：\n" + "\n".join(negative_behaviors)


__all__ = ["VoiceChatterPromptBuilder"]
