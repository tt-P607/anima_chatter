"""提示词构建器。

把"模式判定 + 场景文案 + 模板字符串"组装成最终送给 LLM 的 system / user prompt。
其它模块统一 ``from ..prompts import AnimaChatterPromptBuilder`` 即可。
"""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from src.app.plugin_system.api import adapter_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import ChatStream, Message

from .._internal_compat import get_personality, get_prompt_manager
from ..modes import ChatterMode, resolve_mode
from ..runtime import call_state
from .scenes import (
    VOICE_SCENE_GUIDE,
    VTB_SCENE_GUIDE,
    build_vtb_live_scene_guide,
)
from .templates import MODE_PROMPT_PROFILES

if TYPE_CHECKING:
    from ..config import AnimaChatterConfig


logger = get_logger("anima_chatter.prompts")


__all__ = ["AnimaChatterPromptBuilder"]


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


def _format_duration(seconds: float) -> str:
    """把秒数格式化为"N 分 M 秒"。

    Args:
        seconds: 时长秒数。

    Returns:
        中文时长描述；不足 1 分钟时只显示秒。
    """

    minutes, secs = divmod(int(max(0.0, seconds)), 60)
    return f"{minutes} 分 {secs} 秒" if minutes > 0 else f"{secs} 秒"


class AnimaChatterPromptBuilder:
    """anima_chatter 的提示词构建器。"""

    @staticmethod
    def resolve_mode(chat_stream: ChatStream) -> ChatterMode:
        """判定运行模式。

        Args:
            chat_stream: 当前聊天流。

        Returns:
            运行模式。
        """

        return resolve_mode(chat_stream)

    @staticmethod
    def build_action_suspend_guidance(
        plugin_config: "AnimaChatterConfig | None",
        mode: ChatterMode,
    ) -> str:
        """构建 Action-only 回合的行为说明。

        Args:
            plugin_config: 插件配置；``None`` 时按默认（启用挂起）处理。
            mode: 当前运行模式，决定文案里举例用哪个 say 动作。

        Returns:
            说明文本。
        """

        enabled = (
            True if plugin_config is None else plugin_config.plugin.enable_action_suspend
        )
        examples = "say、pass_and_wait" if mode == "voice" else "say_and_perform、pass_and_wait"

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
        mode: ChatterMode,
        expression_hints: dict[str, str] | None = None,
        chat_stream: ChatStream | None = None,
    ) -> str:
        """按模式返回场景与工具协议文案。

        Args:
            mode: 运行模式。
            expression_hints: ``{intent/emotion: 描述}`` 映射，由 VTS 表演器提供。
                有值时会在 vtb 系场景文案末尾追加"预设动作触发表"，让模型知道
                选某些 intent 会触发哪些手部 / 道具表情。voice 模式忽略此参数。
            chat_stream: 当前聊天流。voice 模式下用来检查是否处于通话中——通话中
                会追加"开始时间 + 已持续时长"的状态片段，让模型直接看到通话语境。

        Returns:
            场景文案。
        """

        if mode == "voice":
            return AnimaChatterPromptBuilder._build_voice_scene(chat_stream)

        base = (
            build_vtb_live_scene_guide(_detect_active_live_sources())
            if mode == "vtb_live"
            else VTB_SCENE_GUIDE
        )
        hints_block = AnimaChatterPromptBuilder._build_expression_hints_block(
            expression_hints
        )
        return base + hints_block

    @staticmethod
    def _build_voice_scene(chat_stream: ChatStream | None) -> str:
        """构建 voice 场景文案，通话中追加动态状态块。

        Args:
            chat_stream: 当前聊天流；``None`` 时只返回基础文案。

        Returns:
            场景文案。
        """

        if chat_stream is None:
            return VOICE_SCENE_GUIDE

        active = call_state.snapshot_active_call_unlocked()
        if active is None or active.caller_stream_id != (chat_stream.stream_id or ""):
            return VOICE_SCENE_GUIDE

        now = time.time()
        started_human = datetime.datetime.fromtimestamp(active.started_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        elapsed = _format_duration(now - active.started_at)
        idle_for = max(0.0, now - active.last_activity_at)
        remaining = _format_duration(max(0.0, active.timeout_seconds - idle_for))
        timeout_str = _format_duration(active.timeout_seconds)

        return VOICE_SCENE_GUIDE + (
            "\n\n<call_status>\n"
            "**【你正在通话中】** 当前 stream 正处于一通本地语音通话——\n"
            f"- 通话开始于：{started_human}\n"
            f"- 已持续：{elapsed}\n"
            f"- 距自动挂断还剩：{remaining}\n"
            f"- 挂断规则：双方都安静超过 {timeout_str} 才会自动挂；"
            "只要持续在聊，通话不会因总时长被中断。\n"
            "- 通话从原私聊升级而来：你的回复经 TTS 通过本机扬声器播放，"
            "对方的话来自麦克风 ASR（可能有错字），双方都看不到文字。\n"
            "- 任何时候认为该挂断，调 ``end_voice_call`` 并附一句自然告别即可，"
            "不必等用户开口提议。\n"
            "</call_status>"
        )

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
        mode: ChatterMode | None = None,
        expression_hints: dict[str, str] | None = None,
    ) -> str:
        """构建系统提示词。

        Args:
            plugin_config: 插件配置。
            chat_stream: 当前聊天流。
            mode: 显式指定模式；``None`` 时自动判定。
            expression_hints: 预设动作提示映射。

        Returns:
            渲染好的 system prompt；模板未注册时返回空串。
        """

        actual_mode = mode or resolve_mode(chat_stream)
        template = get_prompt_manager().get_template("anima_chatter_system_prompt")
        if template is None:
            logger.error("system prompt 模板未注册")
            return ""

        # ``<personality>`` 里的 ``{nickname}`` 是「角色人设名」，取全局人设配置，
        # 而非 ``chat_stream.bot_nickname``（那是平台账号显示名，职责不同）。
        return await (
            template.set("nickname", get_personality().nickname)
            .set(
                "action_suspend_guidance",
                AnimaChatterPromptBuilder.build_action_suspend_guidance(
                    plugin_config, actual_mode
                ),
            )
            .set("sub_agent_collaboration_extra", "")
            .set(
                "scene_guide",
                AnimaChatterPromptBuilder.get_scene_guide(
                    actual_mode, expression_hints, chat_stream=chat_stream
                ),
            )
            .set(
                "custom_instructions_block",
                AnimaChatterPromptBuilder.build_custom_instructions_block(
                    plugin_config, actual_mode
                ),
            )
            .build()
        )

    @staticmethod
    def build_custom_instructions_block(
        plugin_config: "AnimaChatterConfig | None",
        mode: ChatterMode,
    ) -> str:
        """按当前模式构建部署级自定义指令块。

        以下任一条件成立时不注入：配置缺失、``custom_prompt`` 为空、
        ``custom_prompt_modes`` 为空列表、当前模式不在该列表内。

        Args:
            plugin_config: 插件配置。
            mode: 当前运行模式。

        Returns:
            指令块文本；不注入时返回空串。
        """

        if plugin_config is None:
            return ""

        custom_text = plugin_config.plugin.custom_prompt.strip()
        if not custom_text:
            return ""

        allowed = {
            str(item).strip().lower()
            for item in plugin_config.plugin.custom_prompt_modes
            if item
        }
        if mode not in allowed:
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
        mode: ChatterMode | None = None,
    ) -> str:
        """构建用户提示词，按模式选择对应模板。

        Args:
            chat_stream: 当前聊天流。
            history_text: 已格式化的历史消息文本。
            unread_lines: 已格式化的未读消息文本。
            extra: 额外提醒文本。
            mode: 显式指定模式；``None`` 时自动判定。

        Returns:
            渲染好的 user prompt。

        Raises:
            RuntimeError: 对应模式的模板未注册（装配期错误）。
        """

        actual_mode = mode or resolve_mode(chat_stream)
        profile = MODE_PROMPT_PROFILES[actual_mode]

        template = get_prompt_manager().get_template(profile["template_name"])
        if template is None:
            raise RuntimeError(f"user prompt 模板未注册: {profile['template_name']}")

        return await (
            template.set("stream_name", chat_stream.stream_name or chat_stream.stream_id)
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
