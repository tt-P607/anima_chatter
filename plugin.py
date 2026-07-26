"""Anima 三模式聊天器与插件生命周期实现。

该模块装配 voice、vtb 和 vtb_live 三种运行模式，并通过
``default_chatter:service:chat_core`` 复用聊天会话控制流。模式专属逻辑包括
提示词、注意力决策、工具可见性、语音通话超时以及 VTube Studio 资源管理。
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any, AsyncGenerator, cast

import json_repair

from src.app.plugin_system.api import stream_api
from src.app.plugin_system.api.llm_api import get_model_set_by_name, get_model_set_by_task
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import (
    BaseChatter,
    BasePlugin,
    Failure,
    Success,
    Wait,
    WaitResumeEvent,
    register_plugin,
)
from src.app.plugin_system.types import (
    ChatStream,
    ChatType,
    LLMPayload,
    LLMUsable,
    Message,
    ROLE,
    Text,
    ToolRegistry,
)
from src.core.config import get_core_config
from src.core.prompt import STREAM_BUCKET_PREFIX
from src.core.prompt import get_prompt_manager
from src.core.utils.context_compression import default_chat_context_compression_handler
from src.kernel.concurrency import get_task_manager
from src.kernel.llm import LLMContextManager, LLMRequest, ReminderSourceSpec

from .chat_core_bridge import (
    AnimaSessionAdapters,
    AnimaSessionOptions,
    ChatCoreServiceLike,
    PlainTextResponseHandling,
    SubAgentDecision,
)

from . import pipeline_state
from .actions import (
    EndVoiceCallAction,
    SayAction,
    SayAndPerformAction,
    SingSongAction,
    StartVoiceCallAction,
    AnimaPassAndWaitAction,
)
from .audio import AudioPlayer
from .commands import VoiceCommand, VTBCommand
from .config import AnimaChatterConfig
from .constants import CHATTER_SIGNATURE
from .modes import ChatterMode
from .prompts import (
    MODE_PROMPT_PROFILES,
    PLAIN_TEXT_REMINDER_VTB,
    PLAIN_TEXT_REMINDER_VOICE,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    AnimaChatterPromptBuilder,
)
from .prompts.sub_agent import SUB_AGENT_PROMPT_LIVE, SUB_AGENT_PROMPT_VTB
from .vts import VTSPerformer


logger = get_logger("anima_chatter")


class _SafeLoggerWrapper:
    """转义决策面板中的 Rich 标记，同时透传普通日志方法。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def info(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.info(*args, **kwargs)

    def debug(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.debug(*args, **kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.warning(*args, **kwargs)

    def error(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.error(*args, **kwargs)

    def print_panel(
        self,
        message: str,
        title: str | None = None,
        border_style: str | None = None,
    ) -> None:
        """转义面板内容，避免模型输出被 Rich 当作未闭合标记解析。"""

        try:
            from rich.markup import escape

            safe = escape(message) if isinstance(message, str) else message
        except Exception:
            safe = message
        try:
            self._inner.print_panel(safe, title=title, border_style=border_style)
        except Exception as exc:  # noqa: BLE001
            self._inner.info(f"[panel-fallback] {title or ''}\n{safe}")
            self._inner.debug(f"print_panel 转义后仍失败: {exc}")

_CHATTER_SIGNATURE = CHATTER_SIGNATURE
_CHAT_CORE_SERVICE_SIGNATURE = "default_chatter:service:chat_core"


class AnimaChatter(BaseChatter):
    """提供语音通话、虚拟形象互动与直播弹幕对话控制。"""

    name = "anima_chatter"
    description = (
        "语音通话与 VTube Studio 虚拟形象互动通用 Chatter。"
        "platform=local_asr 时为实时通话模式；其他平台需通过 /vtb on 显式接管。"
    )
    associated_platforms = ["local_asr", "live"]
    chat_type = ChatType.ALL
    dependencies = ["asr_adapter_anima:adapter:asr_adapter_anima"]

    # 默认值；apply_stream_runtime_options 会按 platform 动态覆写。
    stream_tick_interval = 0.1
    allow_message_buffer = False

    def _get_plugin_config(self) -> AnimaChatterConfig | None:
        """返回插件配置。"""

        config = getattr(self.plugin, "config", None)
        return config if isinstance(config, AnimaChatterConfig) else None

    def _resolve_mode(self, chat_stream: ChatStream | None = None) -> ChatterMode:
        """根据当前流的 platform 决定运行模式。"""

        if chat_stream is None:
            return "vtb"
        return AnimaChatterPromptBuilder.resolve_mode(chat_stream)

    def apply_stream_runtime_options(self, chat_stream: Any) -> None:
        """根据 platform 动态决定 tick 间隔与消息缓冲策略。

        - voice 模式（local_asr）：固定 tick=0.1，禁用 buffer，沿用现状。
        - vtb 模式（其他平台）：使用 plugin section 中的配置（默认 1.0 / True）。
        """

        plugin_config = self._get_plugin_config()
        platform = getattr(chat_stream, "platform", "") or ""
        if platform == "local_asr":
            self.stream_tick_interval = 0.1
            self.allow_message_buffer = False
        elif plugin_config is not None:
            self.stream_tick_interval = float(plugin_config.plugin.tick_interval)
            self.allow_message_buffer = bool(plugin_config.plugin.allow_message_buffer)
        super().apply_stream_runtime_options(chat_stream)

    # ── PromptAdapter 协议方法 ──────────────────────────────

    async def _build_system_prompt(self, chat_stream: ChatStream) -> str:
        """根据 platform 自动选择 voice / vtb 场景的系统提示词。"""

        # 仅在 plugin 上下文里能拿到当前活动表演器（VTS）；
        # 没有时返回空 dict，builder 会跳过额外注入逻辑。
        expression_hints: dict[str, str] = {}
        get_active = getattr(self.plugin, "get_active_performer", None)
        performer: Any = get_active() if callable(get_active) else None
        if performer is not None and hasattr(performer, "get_expression_hints"):
            try:
                expression_hints = performer.get_expression_hints()
            except Exception:
                expression_hints = {}

        return await AnimaChatterPromptBuilder.build_system_prompt(
            self._get_plugin_config(),
            chat_stream,
            mode=self._resolve_mode(chat_stream),
            expression_hints=expression_hints,
        )

    def _build_history_text(self, chat_stream: ChatStream) -> str:
        """构建历史消息文本（保留旧名给单测 / 历史调用点）。"""

        return AnimaChatterPromptBuilder.build_history_text(
            chat_stream, self.format_message_line
        )

    def format_message_line(
        self,
        msg: Message,
        time_format: str = "%H:%M",
    ) -> str:
        """重写基类版本，给消息行带上"来源平台"前缀。

        当 ``msg.extra["source_platform"]`` 存在时（直播场景下由 dispatcher
        注入，例如 ``bilibili_live`` / ``douyin_live``），在 platform_id 前面
        加 ``<source_platform>`` 标签，让模型一眼能看出这条来自哪个平台：

        ``【03:02】<成员> <bilibili_live>[open_id...] 昵称 [msg]: 内容``
        ``【03:02】<成员> <douyin_live>[sec_uid...] 昵称 [msg]: 内容``
        ``【03:02】<成员> [3905802962] 昵称 [msg]: 内容``  （非直播场景，无前缀）

        没有 ``source_platform`` 字段时回退到基类格式，行为不变。
        """

        line = super().format_message_line(msg, time_format=time_format)
        try:
            source = msg.extra.get("source_platform") if isinstance(msg.extra, dict) else None
        except Exception:
            source = None
        if not source:
            return line
        # 在第一段角色之后、platform_id 之前插入 ``<source_platform>``。
        # 基类格式是：``【时间】<角色> [id] 名称 [msg_id]：内容``
        # 我们在 ``[id]`` 前插入。如果没有 ``[`` 直接前置。
        marker_idx = line.find("[")
        if marker_idx <= 0:
            return f"<{source}> {line}"
        return f"{line[:marker_idx]}<{source}>{line[marker_idx:]}"

    def _build_enhanced_history_text(self, chat_stream: ChatStream) -> str:
        """dfc Session 期望的 PromptAdapter 协议方法（同步）。

        和 :meth:`_build_history_text` 同义；保留两个名字仅是兼容历史调用点。
        """

        return self._build_history_text(chat_stream)

    async def _build_user_prompt(
        self,
        chat_stream: ChatStream,
        history_text: str,
        unread_lines: str,
        extra: str = "",
        clean_mode: bool = False,
    ) -> str:
        """构建用户提示词（按模式选择不同模板）。

        **vtb_live 流水线门**：在构造提示词前，如果配置启用了流水线，会先
        阻塞到累积音频时长达到 ``trigger_percent`` 时刻才返回——sub_agent 钩子
        在 timer 唤醒等路径不会被调到，这里是兜底入口。通过门后立即调
        :func:`pipeline_state.reset_round` 标记新一轮——下次 reserve 会在
        队列尾加 silence_gap，避免新一轮音频接得太急。

        Args:
            clean_mode: 是否生成清洁模式 prompt（去掉某些注入内容）。
                anima_chatter 目前不使用此模式，参数仅为与 default_chatter
                Session 协议兼容而保留。
        """

        # 流水线门已经移到 fetch_unreads——这里不再 wait_gate，避免重复阻塞。
        # _build_user_prompt 本身只构造提示词，不再插入流水线逻辑。
        _ = clean_mode  # anima_chatter 不使用 clean_mode
        mode = self._resolve_mode(chat_stream)
        return await AnimaChatterPromptBuilder.build_user_prompt(
            chat_stream,
            history_text,
            unread_lines,
            extra,
            mode=mode,
        )

    @staticmethod
    def _build_negative_behaviors_extra() -> str:
        """构建行为提醒。"""

        return AnimaChatterPromptBuilder.build_negative_behaviors_extra()

    # ── UnreadAdapter 协议方法 ──────────────────────────────

    @staticmethod
    def _upsert_pending_unread_payload(
        response: Any,
        formatted_text: str,
        unread_msgs: list[Message] | None = None,
        native_multimodal: bool = False,
        logger_override: Any = None,
    ) -> None:
        """把格式化好的未读消息块写成一条 USER payload。

        anima 不接 native_multimodal（voice 通话纯音频；vtb 模式输出在虚拟
        形象上，输入端的 image 也由用户的 vlm 在外层处理），所以这里用最
        简版的 ``Text`` 包装即可——VLM 描述文本会跟着 unread_lines 一起进来。
        """

        _ = unread_msgs, native_multimodal, logger_override  # 兼容签名
        response.add_payload(LLMPayload(ROLE.USER, Text(formatted_text)))

    async def fetch_unreads(
        self,
        time_format: str = "%H:%M",
    ) -> tuple[str, list[Message]]:
        """获取未读消息，并在通话进行中把每条消息记录到 ``call_state``。

        通话期间 ``messages_in_call`` 是 ``voice_call.ended`` 事件的关键
        payload——kfc 等 chatter 收到事件后会基于它把通话历史补回 chain。
        ASR 识别 / QQ 文字消息都会经过 unread_messages 进入 chatter，所以
        统一在这里挂钩最稳妥。

        **vtb_live 流水线门**：在真正拉 unread 前先 wait_gate——这样门期间到
        达的所有弹幕都会被一并拉进来，让 sub_agent / _build_user_prompt 看
        到的是聚合后的完整一批，而不是入门时的快照。
        门通过后立即 reset_round，标记下一次 reserve 是新一轮。

        wait_gate 在「累积 < min_duration」或「门已过」时立即返回，所以非
        阻塞路径上的 fetch_unreads（每个 tick 都会调一次）不会被空转拖累。
        """

        from . import call_state as _cs

        # ── vtb_live 流水线门（关键聚合点） ──
        # 必须放在 super().fetch_unreads() 之前——这样阻塞期间到达的新弹幕
        # 会进入 stream context.unread_messages，门通过后由 super 一次性
        # 全部拉出来给 dfc Session。
        try:
            chat_stream = await stream_api.activate_stream(self.stream_id)
        except (RuntimeError, ValueError):
            chat_stream = None
        if chat_stream is not None and self._resolve_mode(chat_stream) == "vtb_live":
            from . import pipeline_state as _ps

            had_gate = await _ps.is_gate_pending(self.stream_id)
            await _ps.wait_gate(self.stream_id)
            await _ps.reset_round(self.stream_id)
            if had_gate:
                logger.info(
                    "📨 fetch_unreads 通过流水线门，准备聚合期间累积的所有弹幕"
                )

        unread_lines, unread_msgs = await super().fetch_unreads(time_format=time_format)
        if not unread_msgs:
            return unread_lines, unread_msgs

        if await _cs.is_call_active_for_stream(self.stream_id):
            for msg in unread_msgs:
                text = (
                    msg.processed_plain_text
                    or (str(msg.content) if msg.content is not None else "")
                ).strip()
                if not text:
                    continue
                # msg.time 在框架里既可能是 datetime 也可能是 float；统一转成
                # Unix 时间戳。datetime 对象走 .timestamp()，数值类型直接 float()。
                raw_time = getattr(msg, "time", None)
                if raw_time is None:
                    ts: float | None = None
                elif hasattr(raw_time, "timestamp"):
                    try:
                        ts = float(raw_time.timestamp())
                    except Exception:
                        ts = None
                else:
                    try:
                        ts = float(raw_time)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        ts = None
                await _cs.record_user_message(self.stream_id, text, ts=ts)
        return unread_lines, unread_msgs

    # ── UsableAdapter 协议方法 ──────────────────────────────

    async def inject_usables(self, request: Any) -> ToolRegistry:
        """注入 Chatter 可用工具，排除 stop/send_text/sub-agent 管理工具。

        say / say_and_perform 的互斥可见由各自 ``go_activate`` 决定，
        此处不再单独过滤。
        """

        usables: list[type[LLMUsable]] = await self.get_llm_usables()
        usables = await self.modify_llm_usables(usables)
        blocked_names = {
            "action-send_text",
            "action-stop_conversation",
            "create_agent",
            "get_agent",
            "kill_agent",
            # 屏蔽 singing_plugin 的 sing_a_song：anima 接管的流（voice/vtb/
            # vtb_live）统一用自己的 sing_song 双轨翻唱，避免两个唱歌动作并存
            # 让模型混淆、也避免 singing_plugin 走 QQ 语音链路与直播本地播放冲突。
            "action-sing_a_song",
        }

        registry = ToolRegistry()
        for usable in usables:
            schema = usable.to_schema()
            name = str(schema.get("function", {}).get("name", ""))
            if name in blocked_names:
                continue
            registry.register(usable)

        if registry.get_all():
            request.add_payload(LLMPayload(ROLE.TOOL, registry.get_all()))  # type: ignore[arg-type]
        return registry

    # ── SubAgentAdapter 协议方法 ────────────────────────────

    async def sub_agent(
        self,
        unreads_text: str,
        unread_msgs: list[Message],
        chat_stream: ChatStream,
    ) -> SubAgentDecision:
        """vtb / vtb_live 模式下的"是否回复"决策。

        - voice 模式（local_asr 或通话进行中）：直通响应，不调 LLM。
        - 私聊：直通响应（与 dfc 一致；一对一对话不需要过滤）。
        - vtb / vtb_live：通过本地概率门 + dfc decision_agent 决策。

        **vtb_live 流水线门**：在 vtb_live 模式下，无论原决策结果如何，都先
        阻塞到累积音频播放进度达到 ``trigger_percent`` 时刻——避免 LLM 在上
        一轮还在播放时就抢着说话。"决定响应"的请求通过门后调
        :func:`pipeline_state.reset_round` 标记新一轮开始，让下一次 reserve
        把 silence_gap 加到队列起点。

        注意力过滤使用本插件的结构化决策请求，保持与聊天核心相同的响应格式。
        """

        mode = self._resolve_mode(chat_stream)
        if mode == "voice":
            return {
                "should_respond": True,
                "reason": "voice 模式跳过过滤，直接响应",
            }

        # ── vtb_live 弹幕摘要日志 ─────────────────────
        # 流水线门已经在 fetch_unreads 处统一处理；这里只负责打印聚合后的弹幕
        # 预览，让用户清晰看到本次会被打包给 LLM 的完整内容（流水线门可能聚
        # 合了 N 条）。
        if mode == "vtb_live":
            preview_lines = [
                f"  - {m.sender_name or m.sender_id}: {(m.processed_plain_text or str(m.content) or '').strip()[:60]}"
                for m in unread_msgs[:5]
            ]
            preview_text = "\n".join(preview_lines)
            more_hint = (
                f"\n  ... 共 {len(unread_msgs)} 条" if len(unread_msgs) > 5 else ""
            )
            logger.info(
                f"🚀 sub_agent 收到 {len(unread_msgs)} 条聚合弹幕（流水线门已通过），"
                f"打包发给 LLM 决策：\n{preview_text}{more_hint}"
            )

        if str(chat_stream.chat_type).lower() == "private":
            return {
                "should_respond": True,
                "reason": "私聊场景跳过 sub-agent，直接响应",
            }

        # ── 概率门（沿用 dfc 同款权重，避免行为漂移） ──
        plugin_config = self._get_plugin_config()
        attention_section = (
            getattr(plugin_config, "vtb_attention", None) if plugin_config else None
        )
        enabled = (
            True if attention_section is None else bool(attention_section.enabled)
        )
        enable_programmatic = (
            True
            if attention_section is None
            else bool(attention_section.enable_programmatic_controller)
        )

        if not enabled:
            return {
                "should_respond": True,
                "reason": "vtb 注意力过滤已禁用，直接响应",
            }

        # 概率门（与 dfc 完全同款的硬编码权重，避免插件之间漂移）。
        if enable_programmatic:
            probability, reason_text = self._compute_bypass_probability(
                unread_msgs, chat_stream
            )
            if random.random() < probability:
                return {
                    "should_respond": True,
                    "reason": f"概率直通响应：{reason_text}",
                }

        # ── sub_actor LLM 决策 ──
        if mode == "vtb_live":
            template_name = "anima_chatter_sub_agent_prompt_vtb_live"
            fallback_prompt = SUB_AGENT_PROMPT_LIVE
        else:
            template_name = "anima_chatter_sub_agent_prompt_vtb"
            fallback_prompt = SUB_AGENT_PROMPT_VTB
        return await self._decide_should_respond(
            unreads_text=unreads_text,
            chat_stream=chat_stream,
            template_name=template_name,
            fallback_prompt=fallback_prompt,
        )

    # 概率门权重（与 dfc 完全一致，硬编码，不通过配置暴露——避免漂移）。
    _BASE_BYPASS_PROBABILITY = 0.1
    _NAME_MENTION_BONUS = 0.7
    _ALIAS_MENTION_BONUS = 0.4
    _UNREAD_MESSAGE_BONUS = 0.05
    _NEXT_TICK_REPLY_BONUS = 0.5
    _NEXT_TICK_BONUS_ATTR = "_anima_chatter_next_tick_bonus"

    @staticmethod
    def _message_text_for_match(msg: Message) -> str:
        """从消息提取用于关键词匹配的文本。"""

        if isinstance(msg.processed_plain_text, str) and msg.processed_plain_text:
            return msg.processed_plain_text
        if isinstance(msg.content, str):
            return msg.content
        return str(msg.content)

    @classmethod
    def _messages_contain_any(
        cls,
        unread_msgs: list[Message],
        names: list[str],
    ) -> bool:
        """判断未读消息中是否包含任意名字 / 别名。"""

        normalized = [name.strip().lower() for name in names if name.strip()]
        if not normalized:
            return False
        for msg in unread_msgs:
            lowered = cls._message_text_for_match(msg).lower()
            if any(name in lowered for name in normalized):
                return True
        return False

    @staticmethod
    def _identity_names(chat_stream: ChatStream) -> tuple[str, list[str]]:
        """获取 bot 的主名字和别名列表。"""

        fallback_nickname = (
            chat_stream.bot_nickname.strip()
            if isinstance(chat_stream.bot_nickname, str)
            else ""
        )
        try:
            personality = get_core_config().personality
        except RuntimeError:
            return fallback_nickname, []

        nickname = (
            personality.nickname.strip()
            if isinstance(personality.nickname, str) and personality.nickname.strip()
            else fallback_nickname
        )
        alias_names = [
            alias.strip()
            for alias in personality.alias_names
            if isinstance(alias, str) and alias.strip()
        ]
        return nickname, alias_names

    def _compute_bypass_probability(
        self,
        unread_msgs: list[Message],
        chat_stream: ChatStream,
    ) -> tuple[float, str]:
        """计算"无 LLM 直通"放行概率（与 dfc 完全一致的硬编码权重）。"""

        nickname, alias_names = self._identity_names(chat_stream)

        probability = self._BASE_BYPASS_PROBABILITY
        reasons = [f"基础概率 {self._BASE_BYPASS_PROBABILITY:.2f}"]

        if nickname and self._messages_contain_any(unread_msgs, [nickname]):
            probability += self._NAME_MENTION_BONUS
            reasons.append(f"命中名字 +{self._NAME_MENTION_BONUS:.2f}")

        if self._messages_contain_any(unread_msgs, alias_names):
            probability += self._ALIAS_MENTION_BONUS
            reasons.append(f"命中别名 +{self._ALIAS_MENTION_BONUS:.2f}")

        unread_bonus = len(unread_msgs) * self._UNREAD_MESSAGE_BONUS
        if unread_bonus > 0:
            probability += unread_bonus
            reasons.append(f"{len(unread_msgs)} 条未读 +{unread_bonus:.2f}")

        # 上一回合刚回复 → 下一 tick 加成（由 mark_reply_success 写入）
        next_tick = float(getattr(chat_stream.context, self._NEXT_TICK_BONUS_ATTR, 0.0))
        setattr(chat_stream.context, self._NEXT_TICK_BONUS_ATTR, 0.0)
        if next_tick > 0:
            probability += next_tick
            reasons.append(f"上一回合刚回复 +{next_tick:.2f}")

        capped = min(probability, 1.0)
        if capped != probability:
            reasons.append("封顶 1.00")

        return capped, "，".join(reasons)

    @classmethod
    def mark_reply_success(cls, chat_stream: ChatStream) -> None:
        """供 SayAndPerformAction 在成功发出回复后调用。

        让下一次 tick 的概率门 +0.5——模型刚说完话之后用户继续讲话时更
        容易顺势继续。与 dfc 行为完全一致。
        """

        current = float(getattr(chat_stream.context, cls._NEXT_TICK_BONUS_ATTR, 0.0))
        setattr(
            chat_stream.context,
            cls._NEXT_TICK_BONUS_ATTR,
            max(current, cls._NEXT_TICK_REPLY_BONUS),
        )

    async def _decide_should_respond(
        self,
        *,
        unreads_text: str,
        chat_stream: ChatStream,
        template_name: str,
        fallback_prompt: str,
    ) -> SubAgentDecision:
        """调用 sub_actor 模型判断当前批次消息是否需要回复。"""

        try:
            request = self.create_request(
                "sub_actor",
                "anima_attention",
                with_reminder="sub_actor",
            )
        except (ValueError, KeyError):
            return {"should_respond": True, "reason": "sub_actor 配置不可用，默认响应"}

        personality = get_core_config().personality
        bot_id = chat_stream.bot_id or ""
        bot_id_section = f"它的平台标识是 {bot_id}。\n" if bot_id else ""
        template = get_prompt_manager().get_template(template_name)
        if template is not None:
            sub_prompt = await (
                template.set("nickname", personality.nickname)
                .set("bot_id", bot_id)
                .set("bot_id_section", bot_id_section)
                .set("personality_core_section", personality.personality_core)
                .set("personality_side_section", personality.personality_side)
                .build()
            )
        else:
            sub_prompt = fallback_prompt.format(
                nickname=personality.nickname,
                bot_id=bot_id,
                bot_id_section=bot_id_section,
                personality_core_section=personality.personality_core,
                personality_side_section=personality.personality_side,
            )

        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(sub_prompt)))
        request.add_payload(
            LLMPayload(ROLE.USER, Text(f"【新收到待判定消息】\n{unreads_text}"))
        )
        try:
            response = await request.send(stream=False)
            await response
            content = response.message.strip()
            if not content:
                return {"should_respond": True, "reason": "模型未返回判断内容"}
            result = json_repair.loads(content)
            if isinstance(result, dict):
                return {
                    "should_respond": bool(result.get("should_respond", True)),
                    "reason": str(result.get("reason") or "未提供理由"),
                }
            logger.warning(f"注意力决策未返回 JSON 对象: {content[:200]}")
        except Exception as error:  # noqa: BLE001
            logger.error(f"注意力决策执行失败: {error}", exc_info=True)
        return {"should_respond": True, "reason": "决策失败，默认响应"}

    # ── PlainTextResponseAdapter 协议方法 ──────────────────

    def handle_plain_text_response(
        self,
        *,
        message: str,
        retry_count: int,
        response: Any,
    ) -> PlainTextResponseHandling:
        """模型不调工具直接吐文本时的兜底策略。

        anima 在 voice / vtb 模式下都**必须**通过 say / say_and_perform 输出
        正文——纯文本不会被播放也不会发到聊天界面。这里通过 dfc Session 提供
        的 :class:`PlainTextResponseAdapter` 钩子，给模型一次按 mode 提醒重
        发的机会；超过重试次数就直接 wait。

        Args:
            message: LLM 输出的纯文本（已被剥成 message 字段）。
            retry_count: 当前已重试次数（首次为 0）。
            response: dfc Session 持有的 LLMResponse，用于必要时 add_payload。
        """

        _ = response, message  # 占位：需要时可读
        plugin_config = self._get_plugin_config()
        retry_limit = (
            1 if plugin_config is None else int(plugin_config.plugin.plain_text_retry_limit)
        )

        platform = ""
        chat_stream = self._active_stream
        if chat_stream is not None:
            platform = chat_stream.platform or ""

        if platform == "local_asr":
            reminder = PLAIN_TEXT_REMINDER_VOICE
        else:
            reminder = PLAIN_TEXT_REMINDER_VTB

        if retry_count < max(0, retry_limit):
            return {"action": "retry", "reminder_text": reminder}
        return {"action": "wait", "reminder_text": ""}

    # ── 通话超时检查（独立 task，与 Session 并行） ─────────

    async def _voice_call_timeout_watchdog(self) -> None:
        """每秒检查一次通话超时，触发时调 ``finalize_call`` 挂断。

        dfc Session 没有 ``pre_tick_hook``——把超时检查写在 sub_agent 里的话
        sub_agent 本身就有调用频率约束（仅在有未读时跑）。改用独立 task 在
        Session 旁边持续 poll，超时时主动调 finalize_call，副作用包括：解除
        chatter 接管 + 重启 stream loop —— 我们的 ``execute()`` 生成器在那
        之后会自然退出（loop 重启会丢弃旧生成器）。
        """

        from . import call_state as _cs
        from .voice_call_lifecycle import finalize_call

        while True:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                return
            try:
                if not await _cs.is_call_active_for_stream(self.stream_id):
                    continue
                remaining = await _cs.get_remaining_seconds()
                if remaining is None or remaining > 0:
                    continue
                logger.info(f"voice_call 超时挂断 stream={self.stream_id}")
                try:
                    await finalize_call(
                        stream_id=self.stream_id,
                        platform="",  # finalize_call 已不再需要 platform
                        farewell="（通话超时挂断）",
                        end_reason="timeout",
                        plugin=self.plugin,
                    )
                except Exception as exc:
                    logger.warning(f"通话超时挂断流程异常: {exc}", exc_info=True)
                # 一次挂断之后这个 watchdog 任务就该退出——loop 会被重启，
                # 当前 chatter generator 也会被丢弃。下次再开通话时
                # execute() 会重新启动新的 watchdog。
                return
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"通话超时 watchdog 异常（继续）: {exc}")

    # ── LLM Request ──────────────────────────────────────────

    _active_stream: ChatStream | None = None

    def create_request(
        self,
        task: str = "actor",
        request_name: str = "",
        with_reminder: str | None = None,
    ) -> Any:
        """重写以支持自定义回复模型（类似 kokoro_flow_chatter）。"""

        config = self._get_plugin_config()
        model_set = None

        if config and config.plugin.models:
            # 使用 models 列表
            parts = []
            for model_name in config.plugin.models:
                m_set = get_model_set_by_name(
                    model_name,
                    temperature=config.plugin.temperature,
                    max_tokens=config.plugin.max_tokens,
                )
                if m_set:
                    parts.extend(m_set)
            if parts:
                model_set = parts

        if not model_set:
            # fallback 到 model_task，如果都没配则使用传进来的 task（默认 actor）
            task_name = config.plugin.model_task if config else task
            model_set = get_model_set_by_task(task_name)

        reminder_sources = None
        if with_reminder is not None:
            # 与 BaseChatter.create_request 对齐：同时注册全局 bucket 和
            # stream:{stream_id}:{bucket} 的流私有 bucket。
            bucket = with_reminder
            reminder_sources = [
                ReminderSourceSpec(
                    bucket=bucket,
                    wrap_with_system_tag=True,
                )
            ]
            if self.stream_id:
                reminder_sources.append(
                    ReminderSourceSpec(
                        bucket=f"{STREAM_BUCKET_PREFIX}{self.stream_id}:{bucket}",
                        wrap_with_system_tag=True,
                    )
                )

        context_manager = LLMContextManager(
            context_compression_handler=default_chat_context_compression_handler,
            reminder_sources=reminder_sources,
        )

        return LLMRequest(
            model_set=model_set,
            request_name=request_name or self.name,
            meta_data={"stream_id": self.stream_id},
            context_manager=context_manager,
        )

    # ── execute() ───────────────────────────────────────────

    async def execute(
        self,
    ) -> AsyncGenerator[Wait | Success | Failure, WaitResumeEvent | None]:
        """通过 dfc 的 chat_core service 跑对话循环。

        步骤：
        1. 通过 ``service_api.get_service`` 拿 dfc 的 chat_core service；
        2. 用 ``DefaultChatterSessionAdapters`` 把 anima 自身作为各 adapter 注入；
        3. 用 ``DefaultChatterSessionOptions`` 关掉 anima 不需要的特性
           （cooldown / 子代理协作 / native_multimodal / stop direct wake）；
        4. 同时用 ``task_manager`` 起一个独立的通话超时 watchdog，与 Session
           并行跑。
        """

        from src.app.plugin_system.api.service_api import get_service
        chat_stream = await stream_api.activate_stream(self.stream_id)
        if chat_stream is None:
            logger.error(f"无法激活聊天流: {self.stream_id}")
            yield Failure("无法激活聊天流")
            return

        self._active_stream = chat_stream
        self.apply_stream_runtime_options(chat_stream)

        service = get_service(_CHAT_CORE_SERVICE_SIGNATURE)
        if service is None:
            logger.error(
                f"未找到 {_CHAT_CORE_SERVICE_SIGNATURE} service。请确认 default_chatter "
                "插件已启用（manifest.json 已声明依赖，但运行期还需要其插件可用）。"
            )
            yield Failure("default_chatter chat_core service 不可用")
            return

        plugin_config = self._get_plugin_config()
        # anima 自定义 options：关掉 cooldown / 子代理协作 / native multimodal /
        # stop direct wake；这些 dfc 默认值对 anima 不合适。
        options = AnimaSessionOptions(
            actor_task_name="actor",
            sub_actor_task_name="sub_actor",
            enable_cooldown=False,  # anima 没有"对话冷却"概念
            enable_action_suspend=(
                True
                if plugin_config is None
                else bool(plugin_config.plugin.enable_action_suspend)
            ),
            enable_programmatic_controller=True,  # 概率门由 anima.sub_agent 处理
            enable_sub_agent_collaboration=False,
            enable_stop_direct_message_wake=False,
            stop_direct_message_wake_probability=0.0,
            native_multimodal=False,
            theme_guide={},
            negative_behavior_reinforcement=False,  # anima 已经在 user prompt 末尾注入
            enable_llm_stream=False,
        )

        adapters = AnimaSessionAdapters(
            request_adapter=self,
            prompt_adapter=self,
            unread_adapter=self,
            usable_adapter=self,
            tool_execution_adapter=self,
            sub_agent_adapter=self,
            logger_adapter=_SafeLoggerWrapper(logger),
            plain_text_adapter=self,
        )

        chat_core = cast(ChatCoreServiceLike, service)
        session = chat_core.create_session(
            stream_id=self.stream_id,
            options=options,
            adapters=adapters,
        )

        watchdog_task_info: Any | None = None
        try:
            watchdog_task_info = get_task_manager().create_task(
                self._voice_call_timeout_watchdog(),
                name=f"anima_chatter.voice_call_timeout_watchdog.{self.stream_id[:8]}",
                daemon=True,
                metadata={"plugin": "anima_chatter", "stream_id": self.stream_id},
            )

            runner = session.execute()
            resume_event: WaitResumeEvent | None = None
            while True:
                try:
                    result = await runner.asend(resume_event)
                except StopAsyncIteration:
                    return
                # dfc Session 可能 yield Stop（罕见——anima 一般不会触发，
                # 因为 stop_conversation 已在 inject_usables 里屏蔽），
                # 强行把 Stop 当成 Success 收尾。
                if result.__class__.__name__ == "Stop":
                    yield Success("anima_chatter 收到 Stop，按 Success 收尾")
                    return
                resume_event = yield result
        finally:
            self._active_stream = None
            if watchdog_task_info is not None:
                get_task_manager().cancel_task(watchdog_task_info.task_id)


@register_plugin
class AnimaChatterPlugin(BasePlugin):
    """装配 Anima 聊天器、语音动作、命令和本地表演资源。"""

    plugin_name = "anima_chatter"
    plugin_description = "实时语音通话、VTube Studio 表演与直播弹幕互动聊天器"
    configs = [AnimaChatterConfig]
    dependent_components = ["asr_adapter_anima:adapter:asr_adapter_anima"]

    # vtb 模式运行时资源；on_plugin_loaded 中按配置初始化。
    audio_player: AudioPlayer | None = None
    vts_performer: VTSPerformer | None = None
    # 直播清唱歌库；扫描 plugins/anima_chatter/songs/ 目录下的清唱文件。
    song_library: Any = None
    # TTS Provider 能力元数据缓存；on_plugin_loaded 时从 tts_http_server /status 查询。
    # 为 None 表示未获取到（TTS 服务未启动 / 老 provider 不支持），Action 回退到默认描述。
    tts_capabilities: Any = None

    async def on_plugin_loaded(self) -> None:
        """注册提示词并初始化流水线、音频、VTS、歌库和 TTS 能力。"""

        self._register_prompts()

        config = self.config if isinstance(self.config, AnimaChatterConfig) else None
        if config is None:
            logger.warning("插件配置加载异常，VTB 模式将无法播放音频或驱动 VTS。")
            return

        self._init_pipeline(config)
        await self._init_audio_resources(config)
        if config.plugin.enable_singing:
            self._init_song_library()
        else:
            logger.info("唱歌能力已通过 config.plugin.enable_singing=false 关闭，跳过歌库初始化")
            self.song_library = None

        # 查询 TTS Provider 能力元数据，供 SayAction / SayAndPerformAction 的
        # to_schema() 动态注入参数说明。查询失败时 tts_capabilities 保持 None，
        # Action 回退到 Annotated 里的默认描述。
        self._init_tts_capabilities(config)

    def _register_prompts(self) -> None:
        """注册 anima_chatter 在 prompt manager 上的全部模板。

        - system prompt：1 个，三模式共用。
        - user prompt：3 个（voice / vtb / vtb_live），来自 :data:`MODE_PROMPT_PROFILES`
          数据驱动循环——避免三段几乎重复的 ``get_or_create`` 调用。
        - sub-agent prompt：2 个（vtb / vtb_live），分别用群聊与直播话术。
        """

        from src.core.prompt import min_len, optional, wrap

        personality = get_core_config().personality
        prompt_manager = get_prompt_manager()

        # ── system prompt ─────────────────────────────
        # 注：``negative_behaviors`` 占位符已从模板移除（避免与 user prompt 末尾
        # 重复注入），此处也不再传入对应 policy。
        prompt_manager.get_or_create(
            name="anima_chatter_system_prompt",
            template=SYSTEM_PROMPT,
            policies={
                "nickname": optional(personality.nickname),
                "alias_names": optional("、".join(personality.alias_names)),
                "personality_core": optional(personality.personality_core),
                "personality_side": optional(personality.personality_side),
                "identity": optional(personality.identity),
                "reply_style": optional(personality.reply_style),
                "background_story": optional(personality.background_story)
                .then(min_len(10))
                .then(wrap("# 背景故事\n", "\n")),
                "safety_guidelines": optional("\n".join(personality.safety_guidelines)),
                "scene_guide": optional(""),
            },
        )

        # ── user prompt（三模式数据驱动） ──────────────
        for mode_name, profile in MODE_PROMPT_PROFILES.items():
            _ = mode_name  # mode 信息已编码在 profile 中，这里只用 value
            prompt_manager.get_or_create(
                name=profile["template_name"],
                template=USER_PROMPT_TEMPLATE,
                policies={
                    "stream_name": optional(profile["stream_name_default"]),
                    "current_time": optional("未知时间"),
                    "platform": optional(""),
                    "history": optional("")
                    .then(min_len(2))
                    .then(wrap(profile["history_wrap_prefix"], "\n")),
                    "unreads": optional("")
                    .then(min_len(2))
                    .then(wrap(profile["unreads_wrap_prefix"], "\n")),
                    "extra": optional("").then(min_len(2)).then(wrap("# 额外提醒\n", "\n")),
                    "mode_header": optional(profile["mode_header"]),
                    "section_tail": optional(profile["section_tail"]),
                },
            )

        # ── sub-agent prompt（vtb / vtb_live） ─────────
        sub_agent_policies = {
            "nickname": optional(personality.nickname),
            "bot_id": optional(""),
            "bot_id_section": optional(""),
            "personality_core_section": optional(personality.personality_core)
            .then(wrap("它的核心人格是：", "\n")),
            "personality_side_section": optional(personality.personality_side)
            .then(wrap("它的人格侧面是：", "\n")),
        }
        for name, template in (
            ("anima_chatter_sub_agent_prompt_vtb", SUB_AGENT_PROMPT_VTB),
            ("anima_chatter_sub_agent_prompt_vtb_live", SUB_AGENT_PROMPT_LIVE),
        ):
            prompt_manager.get_or_create(
                name=name,
                template=template,
                policies=sub_agent_policies,
            )

    def _init_pipeline(self, config: AnimaChatterConfig) -> None:
        """把 ``[pipelining]`` section 注入流水线状态机。

        本配置仅在 ``vtb_live`` 模式下生效——通过 :meth:`AnimaChatter.sub_agent`
        和 :meth:`AnimaChatter._build_user_prompt` 入口的 mode 判断把控；
        其他模式（voice / vtb）即便 enabled=true 也不会启用流水线。
        """

        section = config.pipelining
        pipeline_state.configure(
            pipeline_state.PipelineSettings(
                enabled=bool(section.enabled),
                trigger_percent=float(section.trigger_percent),
                silence_gap_seconds=float(section.silence_gap_seconds),
                silence_gap_jitter=float(getattr(section, "silence_gap_jitter", 0.0)),
                min_duration_seconds=float(section.min_duration_seconds),
                min_remaining_seconds=float(getattr(section, "min_remaining_seconds", 25.0)),
            )
        )

    def _init_tts_capabilities(self, config: AnimaChatterConfig) -> None:
        """通过进程内 service API 获取 TTS Provider 的能力元数据并缓存。

        不再走 HTTP 回环查询 ``/status``——本方法在 ``on_plugin_loaded`` 中被
        调用时，HTTP 服务器尚未绑定端口，回环查询必然 ConnectionRefused。

        改为通过 :func:`get_service` 拿 ``TTSProviderRegistryService``，
        再调 ``get_provider().get_capabilities()`` 直接拿到原始
        :class:`TTSCapabilities` 对象——零序列化损失，无需反序列化。

        时序容忍：若调用时 provider 尚未注册（tts_voice_plugin-neo 还没
        加载），``tts_capabilities`` 保持 ``None``，Action 的
        :meth:`_get_tts_capabilities` 会在 ``to_schema`` 首次调用时懒加载兜底。

        Args:
            config: 插件配置（保留参数兼容，当前未使用——endpoint 不再需要）。
        """

        try:
            from src.app.plugin_system.api.service_api import get_service

            registry = get_service("tts_http_server:service:tts_provider_registry")
        except Exception as exc:
            logger.debug(f"获取 TTSProviderRegistryService 失败: {exc}")
            return

        if registry is None:
            logger.debug("tts_http_server registry service 未注册，跳过 capabilities 查询")
            return

        provider_name = ""
        try:
            # getattr 绕过类型检查：get_service 返回 BaseService，
            # 但运行时实际是 TTSProviderRegistryService 子类
            get_provider_fn = getattr(registry, "get_provider", None)
            if not callable(get_provider_fn):
                logger.debug("registry 无 get_provider 方法")
                return
            provider: Any = get_provider_fn()
            if provider is None:
                logger.debug("无 TTS Provider 注册，capabilities 将在 to_schema 时懒加载")
                return
            provider_name = str(getattr(provider, "provider_name", "") or "")
            get_caps_fn = getattr(provider, "get_capabilities", None)
            caps: Any = get_caps_fn() if callable(get_caps_fn) else None
        except Exception as exc:
            logger.debug(f"调用 TTS Provider get_capabilities 失败: {exc}")
            return

        if caps is None:
            logger.info(
                f"TTS Provider '{provider_name}' 未提供 capabilities，"
                "Action schema 将使用默认参数描述"
            )
            return

        self.tts_capabilities = caps
        style_marker = "  - '"
        style_count_str = ""
        if caps.style_guide:
            style_count_str = f", styles={caps.style_guide.description.count(style_marker)}"
        logger.info(
            f"已从 TTS Provider '{provider_name}' 获取能力元数据"
            f"{style_count_str}，Action schema 将动态注入参数说明"
        )

    async def _init_audio_resources(self, config: AnimaChatterConfig) -> None:
        """初始化本地 AudioPlayer 与 VTube Studio 表演器。

        音频输出设备配置取 ``config.vts`` 段；若 ``config.vts.enabled`` 为 True
        则实例化 :class:`VTSPerformer`，否则仅初始化 AudioPlayer。
        """

        # 0 表示关闭响度归一化；否则把目标 dBFS 传给 AudioPlayer。
        output_device = config.vts.audio_output_device
        inst_output_device = config.vts.inst_output_device

        loudness_target = float(config.audio_drive.loudness_target_dbfs)
        loudness_arg: float | None = None if loudness_target == 0.0 else loudness_target
        self.audio_player = AudioPlayer(
            output_device=output_device,
            loudness_target_dbfs=loudness_arg,
            inst_output_device=inst_output_device,
        )
        if loudness_arg is None:
            logger.info("响度归一化已关闭（按原音量播放所有音频）")
        else:
            logger.info(
                f"响度归一化已启用：目标 {loudness_arg:.1f} dBFS"
                "（TTS 说话 / 唱歌 / 其它播放统一拉齐）"
            )

        if not config.vts.enabled:
            logger.info("配置中 vts.enabled=false，跳过 VTS 初始化（vtb 模式仅播音频）。")
            return

        self.vts_performer = VTSPerformer(
            plugin_config=config,
            audio_player=self.audio_player,
        )
        ok = await self.vts_performer.initialize()
        if ok:
            logger.info(
                "VTSPerformer 已就绪，vtb 模式回复将驱动 VTube Studio 嘴型/动作。"
            )
        else:
            logger.warning(
                "VTSPerformer 初始化未连接 VTS，vtb 模式将仅播放 TTS 音频。"
            )

    def get_active_performer(self) -> VTSPerformer | None:
        """返回当前激活的 VTSPerformer 实例。

        Action 层统一通过本方法获取表演器；未初始化时返回 None。
        """

        return self.vts_performer

    def _init_song_library(self) -> None:
        """初始化直播清唱歌库（``data/anima_chatter/songs/`` 目录）。"""

        from .song_library import SongLibrary

        plugin_dir = Path(__file__).resolve().parent
        try:
            self.song_library = SongLibrary(plugin_dir=plugin_dir, songs_rel_path="")
            song_count = len(self.song_library.get_song_names())
            if song_count > 0:
                logger.info(
                    f"清唱歌库已加载 {song_count} 首：{self.song_library.songs_dir}"
                )
            else:
                logger.info(
                    f"清唱歌库为空（路径：{self.song_library.songs_dir}），"
                    "把清唱文件放进去后无需重启即可被 sing_song action 识别。"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"清唱歌库初始化失败: {exc}")
            self.song_library = None

    async def on_plugin_unloaded(self) -> None:
        """卸载时终止通话并释放流水线、VTS 与音频资源。"""

        from . import call_state

        active_call = await call_state.get_active_call()
        if active_call is not None:
            from .voice_call_lifecycle import finalize_call

            await finalize_call(
                stream_id=active_call.caller_stream_id,
                platform="",
                farewell="服务正在停止，本次通话已结束。",
                end_reason="plugin_unload",
                plugin=self,
            )
        await pipeline_state.clear_all()

        if self.vts_performer is not None:
            try:
                await self.vts_performer.shutdown()
            except Exception as exc:
                logger.warning(f"关闭 VTSPerformer 失败: {exc}")
        self.vts_performer = None
        self.audio_player = None
        self.song_library = None
        self.tts_capabilities = None

    def get_components(self) -> list[type]:
        """根据插件总开关和唱歌开关返回组件。"""

        config = self.config if isinstance(self.config, AnimaChatterConfig) else None
        if config is not None and not config.plugin.enabled:
            return []

        components: list[type] = [
            AnimaChatter,
            SayAction,
            SayAndPerformAction,
            AnimaPassAndWaitAction,
            StartVoiceCallAction,
            EndVoiceCallAction,
            VTBCommand,
            VoiceCommand,
        ]

        if config is None or config.plugin.enable_singing:
            components.insert(3, SingSongAction)

        return components


__all__ = [
    "EndVoiceCallAction",
    "SayAction",
    "SayAndPerformAction",
    "SingSongAction",
    "AnimaChatter",
    "AnimaChatterPlugin",
    "StartVoiceCallAction",
    "VTBCommand",
    "VoiceCommand",
    "AnimaPassAndWaitAction",
]
