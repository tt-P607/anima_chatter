"""anima_chatter 提示词构建器。

把"模式判定（modes.py）+ 场景文案（scenes.py）+ 模板字符串（templates.py）"
组装成最终送给 LLM 的 system / user prompt。

其它模块只需要 ``from .prompts import AnimaChatterPromptBuilder`` 即可。
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING

from src.core.config import get_core_config
from src.core.prompt import get_prompt_manager

from ..modes import ChatterMode, resolve_mode
from .scenes import (
    VOICE_SCENE_GUIDE,
    VTB_SCENE_GUIDE,
    build_vtb_live_scene_guide,
)


def _is_live_adapter_enabled(adapter: object) -> bool:
    """判断一个直播 adapter 是否真正启用（而非仅被登记）。

    直播 adapter 即使 ``[plugin].enabled = false`` 也会留在 adapter_manager
    里，但不会建立长连、不会投递弹幕。检测活跃源时必须排除这种挂名实例，
    否则只开一个平台也会被误判成多平台同播。

    优先调 adapter 的 ``_is_plugin_enabled()``；没有则回退读
    ``plugin.config.plugin.enabled``；都拿不到时保守视为启用。
    """

    probe = getattr(adapter, "_is_plugin_enabled", None)
    if callable(probe):
        try:
            return bool(probe())
        except Exception:
            return True
    plugin = getattr(adapter, "plugin", None)
    config = getattr(plugin, "config", None)
    plugin_section = getattr(config, "plugin", None)
    return bool(getattr(plugin_section, "enabled", True))


def _detect_active_live_sources() -> frozenset[str]:
    """检测当前有哪些直播 adapter 在跑，返回它们的 ``source_platform`` 集合。

    通过 ``adapter_api`` 拿活跃 adapter 实例列表，过滤出有 ``source_platform``
    类属性、且 ``[plugin].enabled`` 为真的实例（约定：直播 adapter 必须在类上
    声明 ``source_platform``，与 envelope 的 ``additional_config.source_platform``
    一致）。

    设计要点：
    - 不直接 import 任何具体直播 adapter 模块，**保持 anima_chatter 与各
      直播 adapter 之间零硬依赖**。
    - 任何带 ``source_platform`` 类属性的 adapter 都会被识别为"直播来源"，
      未来加 Twitch / YouTube 适配器只要遵循该约定即可，无需改 anima_chatter。
    - **必须排除 enabled=false 的挂名 adapter**：它们仍登记在 adapter_manager
      里、``source_platform`` 类属性也在，但实际不投递弹幕。不排除会导致只开
      一个平台时被误判成多平台同播，错误注入多平台限流提示词。
    """

    try:
        from src.app.plugin_system.api import adapter_api
    except Exception:
        return frozenset()

    sources: set[str] = set()
    try:
        for adapter in adapter_api.get_all_adapters().values():
            source = getattr(adapter, "source_platform", "")
            if source and _is_live_adapter_enabled(adapter):
                sources.add(str(source))
    except Exception:
        return frozenset()
    return frozenset(sources)

if TYPE_CHECKING:
    from src.core.models.message import Message
    from src.core.models.stream import ChatStream

    from ..config import AnimaChatterConfig


class AnimaChatterPromptBuilder:
    """anima_chatter 提示词构建器。"""

    @staticmethod
    def resolve_mode(chat_stream: "ChatStream") -> ChatterMode:
        """根据流的 platform 自动判定模式。

        实际逻辑在 :func:`plugins.anima_chatter.modes.resolve_mode`，本方法
        只做轻包装，方便外部沿用 ``AnimaChatterPromptBuilder.resolve_mode``
        这个调用习惯。
        """

        return resolve_mode(chat_stream)

    @staticmethod
    def build_action_suspend_guidance(
        plugin_config: "AnimaChatterConfig | None",
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
            # 延迟局部导入，避免在模块初始化（Import Time）引发循环导入
            from .. import call_state as _cs

            active = _cs.snapshot_active_call_unlocked()
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

        if mode == "vtb_live":
            # 按当前实际启用的直播 adapter 动态渲染——单平台 / 多平台 /
            # 未来加新平台都能给到精准的 prompt，不会让用户看到无关分支。
            base = build_vtb_live_scene_guide(_detect_active_live_sources())
        else:
            base = VTB_SCENE_GUIDE
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
        # 文案目标：让模型**积极使用**这些预设动作，但保持自然灵活
        # ——根据 Live2D 动作本身的特点在适合的语境下触发，避免机械硬套。
        suffix = (
            "\n\n<expression_hints>\n"
            "**虚拟形象的「招牌」动作触发表**：下面这些 intent / emotion 已经在 "
            "Live2D 模型上预设好了**手部、道具、表情**的特殊动作，是这个虚拟形象"
            "最有辨识度的几个表演动作。\n"
            "\n"
            "选择策略——**敢用、积极用，但要自然**：\n"
            "- 一段对话里**鼓励**多种动作随语境交替，让虚拟形象看起来鲜活有戏，而不是一直一个姿势。\n"
            "- 每条回复只选一个最贴合当下情绪的；找不到非常贴切的就用 NARRATING（默认叙述）兜底。\n"
            "- 行内 ``[motion:NAME]`` 标记是高级用法——一段话里语义明显切换时用它，普通对话保持单一 intent 就够。\n"
            "\n"
            "**预设动作清单**：\n"
            + "\n".join(lines)
            + "\n</expression_hints>"
        )
        return base + suffix

    @staticmethod
    async def build_system_prompt(
        plugin_config: "AnimaChatterConfig | None",
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

        actual_mode: ChatterMode = mode or AnimaChatterPromptBuilder.resolve_mode(chat_stream)
        tmpl = get_prompt_manager().get_template("anima_chatter_system_prompt")
        if not tmpl:
            return ""
        # 注：``<personality>`` 块的 ``{nickname}`` 是「角色人设名」，应取全局
        # ``personality.nickname``（如"爱莉希雅"），而非 ``chat_stream.bot_nickname``
        # ——后者是「平台账号显示名」（如抖音 adapter 默认的"抖音主播"），二者职责
        # 不同。这里与 sub_agent prompt（plugin.py 注册时用的就是 personality.nickname）
        # 保持一致。
        personality_nickname = get_core_config().personality.nickname
        return await (
            tmpl.set("nickname", personality_nickname)
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
        """按当前模式构建用户自定义提示词块。

        从 ``plugin.custom_prompt`` 读取部署级 prompt 补丁；当且仅当
        当前 ``mode`` 出现在 ``plugin.custom_prompt_modes`` 列表里时才注入。
        渲染成 ``<custom_instructions>`` 标签块拼到 system prompt 末尾。

        优先级与失效条件：
        - ``custom_prompt`` 留空 → 不注入。
        - ``custom_prompt_modes`` 为 ``[]`` → 完全禁用，不注入。
        - 当前 mode 不在 ``custom_prompt_modes`` 里 → 不注入。
        - 上述都不触发 → 注入；最终 prompt 末尾出现 ``<custom_instructions>`` 块。
        """

        if plugin_config is None:
            return ""

        try:
            custom_text = str(plugin_config.plugin.custom_prompt or "").strip()
        except Exception:
            return ""
        if not custom_text:
            return ""

        try:
            allowed_modes = list(plugin_config.plugin.custom_prompt_modes or [])
        except Exception:
            return ""
        if not allowed_modes:
            return ""
        # 容忍空白 / 大小写不规范
        normalized = {str(m).strip().lower() for m in allowed_modes if m}
        if mode not in normalized:
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
        chat_stream: "ChatStream",
        history_text: str,
        unread_lines: str,
        extra: str = "",
        mode: ChatterMode | None = None,
    ) -> str:
        """构建用户提示词，按模式选择对应模板。"""

        from .templates import MODE_PROMPT_PROFILES

        actual_mode: ChatterMode = mode or AnimaChatterPromptBuilder.resolve_mode(chat_stream)
        profile = MODE_PROMPT_PROFILES.get(actual_mode) or MODE_PROMPT_PROFILES["voice"]
        template_name = profile["template_name"]

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
            .set("mode_header", profile["mode_header"])
            .set("section_tail", profile["section_tail"])
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
        """构建用户提示词末尾的负面行为提醒（"近因效应"复述）。

        历史上 system prompt 与 user prompt 各注入一份，模型会在一次请求里
        看到两份相同的 negative_behaviors 文本——浪费 token 不说，还容易让
        模型觉得"这就是模板冗余"，反而降低提醒效果。

        现在策略：**只**在 user prompt 末尾注入一份（"近因效应"对模型注意力
        最友好），system prompt 模板里同名占位符已经移除。
        """

        negative_behaviors = get_core_config().personality.negative_behaviors
        if not negative_behaviors:
            return ""
        return "行为提醒：请严格遵守以下约束：\n" + "\n".join(negative_behaviors)


__all__ = ["AnimaChatterPromptBuilder"]
