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
    def get_scene_guide(
        mode: ChatterMode,
        expression_hints: dict[str, str] | None = None,
        chat_stream: "ChatStream | None" = None,
    ) -> str:
        """根据模式返回 ``<scene_and_protocol>`` 段使用的场景与工具协议。

        Args:
            mode: 三态模式之一。
            expression_hints: 可选的 ``{intent/emotion: 描述}`` 字典，由
                :meth:`VTSPerformer.get_expression_hints` 提供。如果有值，
                会在 vtb / vtb_live 场景文案末尾追加一段"额外动作触发"提示，
                让模型知道选某些 intent / emotion 会触发哪些手部 / 道具表情。
                voice 模式下永远忽略此参数（voice 不接 VTS）。
            chat_stream: 当前聊天流。voice 模式下用来检查是否处于通话中——
                通话中会在场景文案末尾追加"开始时间 + 已持续时长"的状态片段，
                让模型在 prompt 里直接看到通话语境，不必依赖通话期间产生的
                history_messages 推断。
        """

        if mode == "voice":
            base = VOICE_SCENE_GUIDE
            if chat_stream is None:
                return base
            # 检查 voice_chatter 接管的"主动通话"语境（QQ 等私聊升级到通话）
            from .. import call_state as _cs

            active = _cs._active_call  # noqa: SLF001 — 同插件读模块级单例
            if active is None or active.caller_stream_id != (chat_stream.stream_id or ""):
                return base

            # 注入动态通话状态：开始时间、已持续时长、剩余超时时间。
            import datetime as _dt
            import time as _time

            started_human = _dt.datetime.fromtimestamp(active.started_at).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            now = _time.time()
            total_elapsed = max(0.0, now - active.started_at)
            elapsed_min = int(total_elapsed // 60)
            elapsed_sec = int(total_elapsed % 60)
            elapsed_str = (
                f"{elapsed_min} 分 {elapsed_sec} 秒"
                if elapsed_min > 0
                else f"{elapsed_sec} 秒"
            )
            # 静默基线：基于 last_activity_at 而不是 started_at——"对话活跃就续期"
            idle_for = max(0.0, now - active.last_activity_at)
            remaining = max(0.0, active.timeout_seconds - idle_for)
            remaining_str = f"{int(remaining // 60)} 分 {int(remaining % 60)} 秒"
            timeout_min = int(active.timeout_seconds // 60)
            timeout_str = f"{timeout_min} 分钟" if timeout_min > 0 else f"{int(active.timeout_seconds)} 秒"

            call_status_block = (
                "\n\n<call_status>\n"
                "**【你正在通话中】** 当前 stream 正处于一通本地语音通话——\n"
                f"- 通话开始于：{started_human}\n"
                f"- 已持续：{elapsed_str}\n"
                f"- 距自动挂断还剩：{remaining_str}\n"
                f"- 挂断规则：双方都安静超过 {timeout_str} 才会自动挂；"
                "只要持续在聊，通话不会因总时长被中断。\n"
                "- 通话从原 QQ 私聊升级而来：你的回复经 TTS 通过本机扬声器播放，"
                "对方的话来自麦克风 ASR（可能有错字），双方都看不到文字。\n"
                "- 任何时候认为该挂断，调 ``end_voice_call`` 并附一句自然告别即可，"
                "不必等用户开口提议。\n"
                "</call_status>"
            )
            return base + call_status_block

        base = VTB_LIVE_SCENE_GUIDE if mode == "vtb_live" else VTB_SCENE_GUIDE
        if not expression_hints:
            return base

        # 把 hints 拼成 markdown 列表追加到场景文案末尾。
        # key 大写归一，对应 intent 值；也兼容 emotion 主类型（happy / sad ...）。
        lines = []
        for key, desc in expression_hints.items():
            if not key or not desc:
                continue
            lines.append(f"- `{key.upper()}` → {desc}")
        if not lines:
            return base

        # 用 <expression_hints> 标签把额外说明圈起来，与 base 文案视觉分离。
        suffix = (
            "\n\n<expression_hints>\n"
            "**额外的手部 / 道具表情触发**：选下面的 intent / emotion 时，"
            "会同步激活对应的虚拟形象表情或道具动作。在合适的语境里使用，"
            "可以让虚拟形象的表演更有戏；不适合的场景就不要硬选。\n"
            + "\n".join(lines)
            + "\n</expression_hints>"
        )
        return base + suffix

    @staticmethod
    async def build_system_prompt(
        plugin_config: "SherpaOnnxVoiceChatterConfig | None",
        chat_stream: "ChatStream",
        mode: ChatterMode | None = None,
        expression_hints: dict[str, str] | None = None,
    ) -> str:
        """构建系统提示词。

        Args:
            plugin_config: 插件配置（可能为空）。
            chat_stream: 当前聊天流。
            mode: 显式指定模式；为空时根据 ``chat_stream.platform`` 自动判定。
            expression_hints: 可选的"intent / emotion → 动作描述"字典，由
                :meth:`VTSPerformer.get_expression_hints` 提供，用于在 vtb /
                vtb_live 场景文案末尾追加额外动作触发说明。
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
            .set(
                "scene_guide",
                VoiceChatterPromptBuilder.get_scene_guide(
                    actual_mode, expression_hints, chat_stream=chat_stream
                ),
            )
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
