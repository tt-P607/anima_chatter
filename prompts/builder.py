"""直播场景、人格和部署指令的提示词构建器。"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING

from src.app.plugin_system.api import adapter_api, prompt_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import ChatStream, Message

from .._internal_compat import get_personality, get_prompt_manager
from .scenes import build_vtb_live_scene_guide
from .templates import LIVE_USER_PROMPT_PROFILE

if TYPE_CHECKING:
    from ..config import AnimaChatterConfig


logger = get_logger("anima_chatter.prompts")


SPEECH_RULES_TEMPLATE_NAME: str = "tts_voice_plugin-neo.speech_rules"

__all__ = ["SPEECH_RULES_TEMPLATE_NAME", "AnimaChatterPromptBuilder"]


def _is_live_adapter_enabled(adapter: object) -> bool:
    """判断一个直播 adapter 是否真正启用（而非仅被登记）。

    直播 adapter 即使配置里 ``enabled = false`` 也会留在 adapter_manager 里，
    但不会建立长连、不会投递弹幕。检测活跃源时必须排除这种挂名实例，否则只开
    一个平台也会被误判成多平台同播，注入无关的限流提示词。

    Args:
        adapter: 待判断的 adapter 实例。

    Returns:
        adapter 处于启用状态时返回 ``True``；探测不到时保守视为启用。
    """

    probe = getattr(adapter, "_is_plugin_enabled", None)
    if callable(probe):
        return bool(probe())

    plugin = getattr(adapter, "plugin", None)
    config = getattr(plugin, "config", None)
    plugin_section = getattr(config, "plugin", None)
    return bool(getattr(plugin_section, "enabled", True))


def _detect_active_live_sources() -> frozenset[str]:
    """检测当前有哪些直播 adapter 在跑，返回它们的来源平台集合。

    约定：直播 adapter 必须在类上声明 ``source_platform`` 类属性，与 envelope
    的 ``additional_config.source_platform`` 一致。任何带该属性且已启用的
    adapter 都会被识别为直播来源——未来加新平台只要遵循该约定，无需改本插件。

    Returns:
        活跃直播来源集合；检测失败时返回空集合。
    """

    try:
        adapters = adapter_api.get_all_adapters().values()
    except RuntimeError:
        return frozenset()

    return frozenset(
        str(source)
        for adapter in adapters
        if (source := getattr(adapter, "source_platform", ""))
        and _is_live_adapter_enabled(adapter)
    )


class AnimaChatterPromptBuilder:
    """anima_chatter 的提示词构建器。"""

    @staticmethod
    def build_action_suspend_guidance(
        plugin_config: "AnimaChatterConfig | None",
    ) -> str:
        """构建 Action-only 回合的行为说明。

        Args:
            plugin_config: 插件配置；``None`` 时按默认（启用挂起）处理。

        Returns:
            说明文本。
        """

        enabled = (
            True
            if plugin_config is None
            else plugin_config.plugin.enable_action_suspend
        )
        examples = "say_and_perform、pass_and_wait"

        if enabled:
            return (
                f'Action: 是你在互动过程中的"动作"，例如 {examples}。'
                '当你只接收到 Action 的返回信息时，只需要输出"__SUSPEND__"表示当前回合挂起，'
                "等待用户继续说话或等待新的恢复事件；"
            )
        return (
            f'Action: 是你在互动过程中的"动作"，例如 {examples}。'
            '当你只接收到 Action 的返回信息时，不要输出"__SUSPEND__"，'
            "而应把这些回执当作常规工具结果，继续决定下一步要调用的工具或动作。"
            "如果你调用的是 pass_and_wait，则会进入等待，而不是继续追加新的调用。"
            "通常在你说完后调用来暂时挂起会话。"
        )

    @staticmethod
    def get_scene_guide(
        expression_hints: dict[str, str] | None = None,
    ) -> str:
        """构建直播场景与表演工具协议文案。

        Args:
            expression_hints: ``{intent/emotion: 描述}`` 映射，由 VTS 表演器提供。
                有值时在场景末尾追加预设动作触发表。

        Returns:
            场景文案。
        """

        base = build_vtb_live_scene_guide(_detect_active_live_sources())
        hints_block = AnimaChatterPromptBuilder._build_expression_hints_block(
            expression_hints
        )
        return base + hints_block

    @staticmethod
    def _build_expression_hints_block(hints: dict[str, str] | None) -> str:
        """把预设动作提示渲染成场景文案的追加块。

        Args:
            hints: ``{intent/emotion: 描述}`` 映射。

        Returns:
            追加块文本；无有效提示时返回空串。
        """

        if not hints:
            return ""

        lines = [
            f"- `{key.upper()}` → {desc}" for key, desc in hints.items() if key and desc
        ]
        if not lines:
            return ""

        return (
            "\n\n<expression_hints>\n"
            "**虚拟形象的「招牌」动作触发表**：下面这些 intent / emotion 已经在 "
            "Live2D 模型上预设好了**手部、道具、表情**的特殊动作，是这个虚拟形象"
            "最有辨识度的几个表演动作。\n"
            "\n"
            "选择策略——**敢用、积极用，但要自然**：\n"
            "- 一段对话里**鼓励**多种动作随语境交替，让虚拟形象看起来鲜活有戏，"
            "而不是一直一个姿势。\n"
            "- 每条回复只选一个最贴合当下情绪的；找不到非常贴切的就用 NARRATING 兜底。\n"
            "- 行内 ``[motion:NAME]`` 标记是高级用法——一段话里语义明显切换时用它，"
            "普通对话保持单一 intent 就够。\n"
            "\n"
            "**预设动作清单**：\n" + "\n".join(lines) + "\n</expression_hints>"
        )

    @staticmethod
    async def build_system_prompt(
        plugin_config: "AnimaChatterConfig | None",
        chat_stream: ChatStream,
        expression_hints: dict[str, str] | None = None,
    ) -> str:
        """构建系统提示词。

        Args:
            plugin_config: 插件配置。
            chat_stream: 当前聊天流。
            expression_hints: 预设动作提示映射。

        Returns:
            渲染好的 system prompt；模板未注册时返回空串。
        """

        template = get_prompt_manager().get_template("anima_chatter_system_prompt")
        if template is None:
            logger.error("system prompt 模板未注册")
            return ""

        speech_template = prompt_api.get_template(SPEECH_RULES_TEMPLATE_NAME)
        if speech_template is None:
            raise RuntimeError(f"公共语音模板未注册：{SPEECH_RULES_TEMPLATE_NAME}")
        speech_rules = await speech_template.build(strict=True)

        # ``<personality>`` 里的 ``{nickname}`` 是「角色人设名」，取全局人设配置，
        # 而非 ``chat_stream.bot_nickname``（那是平台账号显示名，职责不同）。
        return await (
            template.set("nickname", get_personality().nickname)
            .set("speech_rules", speech_rules)
            .set("stream_id", chat_stream.stream_id)
            .set(
                "action_suspend_guidance",
                AnimaChatterPromptBuilder.build_action_suspend_guidance(
                    plugin_config
                ),
            )
            .set("sub_agent_collaboration_extra", "")
            .set(
                "scene_guide",
                AnimaChatterPromptBuilder.get_scene_guide(
                    expression_hints
                ),
            )
            .set(
                "custom_instructions_block",
                AnimaChatterPromptBuilder.build_custom_instructions_block(
                    plugin_config
                ),
            )
            .build()
        )

    @staticmethod
    def build_custom_instructions_block(
        plugin_config: "AnimaChatterConfig | None",
    ) -> str:
        """构建对直播生效的部署级自定义指令块。

        配置缺失、提示词为空或部署指令未启用时不注入。

        Args:
            plugin_config: 插件配置。

        Returns:
            指令块文本；不注入时返回空串。
        """

        if plugin_config is None:
            return ""

        custom_text = plugin_config.plugin.custom_prompt.strip()
        if not custom_text:
            return ""

        if not plugin_config.plugin.custom_prompt_enabled:
            return ""

        return (
            "<custom_instructions>\n"
            "# 部署级自定义指令\n"
            "下面是本机部署者额外提供的指令；它们补充（不覆盖）上面的人设与场景说明。\n"
            "如果这里的要求与前面的安全准则冲突，则**仍然以前面的安全准则为准**。\n"
            "\n"
            f"{custom_text}\n"
            "</custom_instructions>"
        )

    @staticmethod
    async def build_user_prompt(
        chat_stream: ChatStream,
        history_text: str,
        unread_lines: str,
        extra: str = "",
    ) -> str:
        """构建直播弹幕用户提示词。

        Args:
            chat_stream: 当前聊天流。
            history_text: 已格式化的历史消息文本。
            unread_lines: 已格式化的未读消息文本。
            extra: 额外提醒文本。

        Returns:
            渲染好的 user prompt。

        Raises:
            RuntimeError: 直播模板未注册。
        """

        profile = LIVE_USER_PROMPT_PROFILE

        template = get_prompt_manager().get_template(profile["template_name"])
        if template is None:
            raise RuntimeError(f"user prompt 模板未注册: {profile['template_name']}")

        return await (
            template.set(
                "stream_name", chat_stream.stream_name or chat_stream.stream_id
            )
            .set("current_time", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            .set("platform", chat_stream.platform)
            .set("history", history_text)
            .set("unreads", unread_lines)
            .set("extra", extra)
            .set("stream_id", chat_stream.stream_id or "")
            .set("mode_header", profile["mode_header"])
            .set("section_tail", profile["section_tail"])
            .build()
        )

    @staticmethod
    def build_history_text(
        chat_stream: ChatStream,
        formatter: Callable[[Message], str],
    ) -> str:
        """把历史消息拼成文本。

        Args:
            chat_stream: 当前聊天流。
            formatter: 单条消息的格式化函数。

        Returns:
            拼接好的历史文本。
        """

        return "\n".join(
            formatter(message) for message in chat_stream.context.history_messages
        )

    @staticmethod
    def build_negative_behaviors_extra() -> str:
        """构建用户提示词末尾的行为约束提醒。

        只在 user prompt 末尾注入一次（近因效应对模型注意力更友好），system
        prompt 不再重复注入。

        Returns:
            提醒文本；未配置约束时返回空串。
        """

        behaviors = get_personality().negative_behaviors
        if not behaviors:
            return ""
        return "行为提醒：请严格遵守以下约束：\n" + "\n".join(behaviors)
