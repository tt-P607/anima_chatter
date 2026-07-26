"""anima_chatter 的 Chatter 主类。

通过 ``default_chatter:service:chat_core`` 复用聊天会话控制流，自身只实现各个
adapter 协议方法：

- **PromptAdapter**：按模式选 prompt、给直播消息加来源平台前缀
- **UnreadAdapter**：拉未读消息，通话中同步入档；vtb_live 在此处过流水线门
- **UsableAdapter**：注入工具并屏蔽与本插件冲突的动作
- **SubAgentAdapter**：注意力过滤（详见 [`attention.py`](attention.py:1)）
- **PlainTextResponseAdapter**：模型不调工具直接吐文本时的兜底

外加一个独立的通话超时看门狗任务，与会话并行运行。
"""

from __future__ import annotations

import asyncio
import datetime
from typing import TYPE_CHECKING, Any, AsyncGenerator, cast

from src.app.plugin_system.api import stream_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import (
    BaseChatter,
    Failure,
    Stop,
    Success,
    Wait,
    WaitResumeEvent,
)
from src.app.plugin_system.types import (
    ChatStream,
    ChatType,
    LLMPayload,
    LLMRequest,
    LLMUsable,
    Message,
    ROLE,
    Text,
    ToolRegistry,
)

from .._internal_compat import cancel_background_task, create_background_task
from ..modes import ASSOCIATED_PLATFORMS, ChatterMode, VOICE_PLATFORM, resolve_mode
from ..prompts import (
    PLAIN_TEXT_REMINDER_VOICE,
    PLAIN_TEXT_REMINDER_VTB,
    AnimaChatterPromptBuilder,
)
from ..runtime import call_state, pipeline_state
from ..voice_call import finalize_call
from . import attention, request_factory
from .attention import SubAgentDecision
from .logging import SafeLoggerWrapper
from .session_bridge import (
    AnimaSessionAdapters,
    AnimaSessionOptions,
    ChatCoreServiceLike,
    PlainTextResponseHandling,
)

if TYPE_CHECKING:
    from ..config import AnimaChatterConfig
    from ..protocol import AnimaPlugin


logger = get_logger("anima_chatter")


__all__ = ["AnimaChatter"]


_CHAT_CORE_SERVICE = "default_chatter:service:chat_core"

# voice 模式下强制的 tick 间隔与缓冲策略——实时通话必须高频轮询且不缓冲。
_VOICE_TICK_INTERVAL = 0.1

# 通话超时检查间隔（秒）。
_TIMEOUT_CHECK_INTERVAL = 1.0

# 不注入给模型的工具名。前三个是 chat_core 的会话管理工具，最后一个是其它
# 唱歌插件的动作——anima 接管的流统一用自己的 sing_song 双轨翻唱，避免两个
# 唱歌动作并存让模型混淆、也避免走消息链路与本地播放冲突。
_BLOCKED_USABLE_NAMES = frozenset(
    {
        "action-send_text",
        "action-stop_conversation",
        "create_agent",
        "get_agent",
        "kill_agent",
        "action-sing_a_song",
    }
)


class AnimaChatter(BaseChatter):
    """语音通话、虚拟形象互动与直播弹幕的三模式对话控制器。"""

    name = "anima_chatter"
    description = (
        "语音通话与 VTube Studio 虚拟形象互动通用 Chatter。"
        "platform=local_asr 时为实时通话模式；其他平台需通过 /vtb on 显式接管。"
    )
    associated_platforms = ASSOCIATED_PLATFORMS
    chat_type = ChatType.ALL

    # 默认值；apply_stream_runtime_options 会按 platform 动态覆写。
    stream_tick_interval = _VOICE_TICK_INTERVAL
    allow_message_buffer = False

    _active_stream: ChatStream | None = None

    # ── 配置与模式 ──────────────────────────────────────

    @property
    def _plugin(self) -> "AnimaPlugin":
        """返回收窄后的插件实例。

        Returns:
            本插件实例。
        """

        from ..protocol import require_plugin

        return require_plugin(self.plugin)

    @property
    def _config(self) -> "AnimaChatterConfig | None":
        """返回插件配置；加载失败时为 ``None``。"""

        return self._plugin.config

    @staticmethod
    def _resolve_mode(chat_stream: ChatStream | None) -> ChatterMode:
        """判定当前运行模式。

        Args:
            chat_stream: 当前聊天流；``None`` 时按 vtb 处理。

        Returns:
            运行模式。
        """

        return "vtb" if chat_stream is None else resolve_mode(chat_stream)

    def apply_stream_runtime_options(self, chat_stream: ChatStream) -> None:
        """按 platform 动态决定 tick 间隔与消息缓冲策略。

        voice 模式固定 tick=0.1 且禁用缓冲（实时通话必须高频响应）；其余模式
        走 ``[plugin]`` 配置。

        Args:
            chat_stream: 当前聊天流。
        """

        config = self._config
        if chat_stream.platform == VOICE_PLATFORM:
            self.stream_tick_interval = _VOICE_TICK_INTERVAL
            self.allow_message_buffer = False
        elif config is not None:
            self.stream_tick_interval = config.plugin.tick_interval
            self.allow_message_buffer = config.plugin.allow_message_buffer
        super().apply_stream_runtime_options(chat_stream)

    # ── PromptAdapter 协议 ──────────────────────────────

    async def _build_system_prompt(self, chat_stream: ChatStream) -> str:
        """按模式构建系统提示词。

        Args:
            chat_stream: 当前聊天流。

        Returns:
            渲染好的 system prompt。
        """

        performer = self._plugin.get_active_performer()
        expression_hints = (
            performer.get_expression_hints() if performer is not None else {}
        )
        return await AnimaChatterPromptBuilder.build_system_prompt(
            self._config,
            chat_stream,
            mode=self._resolve_mode(chat_stream),
            expression_hints=expression_hints,
        )

    async def _build_user_prompt(
        self,
        chat_stream: ChatStream,
        history_text: str,
        unread_lines: str,
        extra: str = "",
        clean_mode: bool = False,
    ) -> str:
        """按模式构建用户提示词。

        Args:
            chat_stream: 当前聊天流。
            history_text: 已格式化的历史消息文本。
            unread_lines: 已格式化的未读消息文本。
            extra: 额外提醒文本。
            clean_mode: 会话协议要求的参数，anima 不使用。

        Returns:
            渲染好的 user prompt。
        """

        _ = clean_mode
        return await AnimaChatterPromptBuilder.build_user_prompt(
            chat_stream,
            history_text,
            unread_lines,
            extra,
            mode=self._resolve_mode(chat_stream),
        )

    def _build_enhanced_history_text(self, chat_stream: ChatStream) -> str:
        """构建历史消息文本（会话协议要求的方法名）。

        Args:
            chat_stream: 当前聊天流。

        Returns:
            拼接好的历史文本。
        """

        return AnimaChatterPromptBuilder.build_history_text(
            chat_stream, self.format_message_line
        )

    def format_message_line(
        self,
        msg: Message,
        time_format: str = "%H:%M",
    ) -> str:
        """给消息行带上"来源平台"前缀。

        直播场景下 dispatcher 会在 ``msg.extra`` 注入 ``source_platform``，此时
        在 platform_id 前插入 ``<source_platform>`` 标签，让模型一眼看出这条来
        自哪个平台。没有该字段时回退到基类格式。

        Args:
            msg: 待格式化的消息。
            time_format: 时间格式串。

        Returns:
            格式化后的单行文本。
        """

        line = super().format_message_line(msg, time_format=time_format)
        source = msg.extra.get("source_platform") if isinstance(msg.extra, dict) else None
        if not source:
            return line
        # 基类格式为 ``【时间】<角色> [id] 名称 [msg_id]：内容``，在 ``[`` 前插入。
        marker_idx = line.find("[")
        if marker_idx <= 0:
            return f"<{source}> {line}"
        return f"{line[:marker_idx]}<{source}>{line[marker_idx:]}"

    @staticmethod
    def _build_negative_behaviors_extra() -> str:
        """构建用户提示词末尾的行为约束提醒。

        Returns:
            提醒文本；未配置约束时为空串。
        """

        return AnimaChatterPromptBuilder.build_negative_behaviors_extra()

    # ── UnreadAdapter 协议 ──────────────────────────────

    @staticmethod
    def _upsert_pending_unread_payload(
        response: Any,
        formatted_text: str,
        unread_msgs: list[Message] | None = None,
        native_multimodal: bool = False,
        logger_override: Any = None,
    ) -> None:
        """把格式化好的未读消息块写成一条 USER payload。

        anima 不使用原生多模态（voice 通话纯音频；vtb 模式的图片描述由外层
        VLM 处理后随 unread_lines 一起进来），所以用最简单的文本包装即可。

        Args:
            response: 会话持有的 LLM 响应对象。
            formatted_text: 已格式化的未读文本。
            unread_msgs: 原始未读消息，anima 不使用。
            native_multimodal: 是否原生多模态，anima 不使用。
            logger_override: 日志覆写，anima 不使用。
        """

        _ = unread_msgs, native_multimodal, logger_override
        response.add_payload(LLMPayload(ROLE.USER, Text(formatted_text)))

    async def fetch_unreads(
        self,
        time_format: str = "%H:%M",
    ) -> tuple[str, list[Message]]:
        """拉取未读消息，通话进行中同步入档。

        **vtb_live 流水线门**：在真正拉未读**之前**先过门——这样门期间到达的
        弹幕都会被一并拉进来，让后续决策看到的是聚合后的完整一批，而不是入门
        时的快照。门通过后立即标记新一轮。

        ``wait_gate`` 在"累积不足"或"门已过"时立即返回，因此每 tick 都调用也
        不会被空转拖累。

        Args:
            time_format: 时间格式串。

        Returns:
            ``(格式化文本, 未读消息列表)``。
        """

        await self._pass_pipeline_gate()

        unread_lines, unread_msgs = await super().fetch_unreads(time_format=time_format)
        if unread_msgs and await call_state.is_call_active_for_stream(self.stream_id):
            await self._record_call_messages(unread_msgs)
        return unread_lines, unread_msgs

    async def _pass_pipeline_gate(self) -> None:
        """vtb_live 模式下等待流水线门并标记新一轮。"""

        try:
            chat_stream = await stream_api.activate_stream(self.stream_id)
        except (RuntimeError, ValueError):
            return
        if chat_stream is None or self._resolve_mode(chat_stream) != "vtb_live":
            return

        had_gate = await pipeline_state.is_gate_pending(self.stream_id)
        await pipeline_state.wait_gate(self.stream_id)
        await pipeline_state.reset_round(self.stream_id)
        if had_gate:
            logger.info("fetch_unreads 通过流水线门，准备聚合期间累积的所有弹幕")

    async def _record_call_messages(self, unread_msgs: list[Message]) -> None:
        """把通话期间的用户消息记入通话状态。

        ``messages_in_call`` 是 ``voice_call.ended`` 事件的关键 payload——订阅方
        据此把通话历史补回自己的对话链。ASR 识别与平台文字消息都经 unread 进入
        chatter，统一在这里挂钩最稳妥。

        Args:
            unread_msgs: 本轮未读消息。
        """

        for msg in unread_msgs:
            text = (
                msg.processed_plain_text
                or (str(msg.content) if msg.content is not None else "")
            ).strip()
            if not text:
                continue
            await call_state.record_user_message(
                self.stream_id, text, ts=self._message_timestamp(msg)
            )

    @staticmethod
    def _message_timestamp(msg: Message) -> float | None:
        """把消息时间统一转成 Unix 时间戳。

        ``msg.time`` 在框架里既可能是 ``datetime`` 也可能是数值。

        Args:
            msg: 待取时间的消息。

        Returns:
            Unix 时间戳；无法解析时返回 ``None``。
        """

        raw = msg.time
        if raw is None:
            return None
        if isinstance(raw, datetime.datetime):
            return raw.timestamp()
        return float(raw)

    # ── UsableAdapter 协议 ──────────────────────────────

    async def inject_usables(self, request: LLMRequest) -> ToolRegistry:
        """注入可用工具，屏蔽与本插件冲突的动作。

        ``say`` 与 ``say_and_perform`` 的互斥由各自的 ``go_activate`` 决定，
        此处不额外过滤。

        Args:
            request: 待注入的 LLM 请求。

        Returns:
            注册好的工具表。
        """

        usables: list[type[LLMUsable]] = await self.get_llm_usables()
        usables = await self.modify_llm_usables(usables)

        registry = ToolRegistry()
        for usable in usables:
            schema = usable.to_schema()
            name = str(schema.get("function", {}).get("name", ""))
            if name in _BLOCKED_USABLE_NAMES:
                continue
            registry.register(usable)

        tools = registry.get_all()
        if tools:
            request.add_payload(LLMPayload(ROLE.TOOL, cast(Any, tools)))
        return registry

    # ── SubAgentAdapter 协议 ────────────────────────────

    async def sub_agent(
        self,
        unreads_text: str,
        unread_msgs: list[Message],
        chat_stream: ChatStream,
    ) -> SubAgentDecision:
        """判断本轮是否需要回复。

        - voice 模式：直通响应，不调 LLM。
        - 私聊：直通响应（一对一对话不需要过滤）。
        - vtb / vtb_live：本地概率门 + sub_actor LLM 决策。

        Args:
            unreads_text: 格式化后的未读文本。
            unread_msgs: 未读消息列表。
            chat_stream: 当前聊天流。

        Returns:
            决策结果。
        """

        mode = self._resolve_mode(chat_stream)
        if mode == "voice":
            return {"should_respond": True, "reason": "voice 模式跳过过滤，直接响应"}

        if mode == "vtb_live":
            self._log_danmaku_preview(unread_msgs)

        if str(chat_stream.chat_type).lower() == "private":
            return {"should_respond": True, "reason": "私聊场景跳过过滤，直接响应"}

        config = self._config
        if config is None or not config.vtb_attention.enabled:
            return {"should_respond": True, "reason": "注意力过滤未启用，直接响应"}

        passed, reason = attention.passes_probability_gate(
            config.vtb_attention, unread_msgs, chat_stream
        )
        if passed:
            return {"should_respond": True, "reason": f"概率直通响应：{reason}"}

        template_name, fallback_prompt = attention.resolve_sub_agent_prompt_source(mode)
        try:
            request = self.create_request(
                "sub_actor", "anima_attention", with_reminder="sub_actor"
            )
        except (ValueError, KeyError):
            return {"should_respond": True, "reason": "sub_actor 配置不可用，默认响应"}

        return await attention.decide_should_respond(
            request=request,
            unreads_text=unreads_text,
            chat_stream=chat_stream,
            template_name=template_name,
            fallback_prompt=fallback_prompt,
        )

    @staticmethod
    def _log_danmaku_preview(unread_msgs: list[Message]) -> None:
        """打印聚合后的弹幕预览，便于观察流水线门的聚合效果。

        Args:
            unread_msgs: 本轮未读消息。
        """

        preview = "\n".join(
            f"  - {msg.sender_name or msg.sender_id}: "
            f"{(msg.processed_plain_text or str(msg.content) or '').strip()[:60]}"
            for msg in unread_msgs[:5]
        )
        more = f"\n  ... 共 {len(unread_msgs)} 条" if len(unread_msgs) > 5 else ""
        logger.info(
            f"sub_agent 收到 {len(unread_msgs)} 条聚合弹幕（流水线门已通过），"
            f"打包发给 LLM 决策：\n{preview}{more}"
        )

    # ── PlainTextResponseAdapter 协议 ───────────────────

    def handle_plain_text_response(
        self,
        *,
        message: str,
        retry_count: int,
        response: Any,
    ) -> PlainTextResponseHandling:
        """模型不调工具直接吐文本时的兜底策略。

        anima 在所有模式下都**必须**通过 say / say_and_perform 输出正文——纯文本
        既不会被播放也不会发到聊天界面。这里给模型一次按模式提醒重发的机会，
        超过重试次数就直接等待用户。

        Args:
            message: 模型输出的纯文本。
            retry_count: 当前已重试次数（首次为 0）。
            response: 会话持有的 LLM 响应对象。

        Returns:
            处理策略。
        """

        _ = message, response
        config = self._config
        retry_limit = 1 if config is None else config.plugin.plain_text_retry_limit

        chat_stream = self._active_stream
        platform = chat_stream.platform if chat_stream is not None else ""
        reminder = (
            PLAIN_TEXT_REMINDER_VOICE
            if platform == VOICE_PLATFORM
            else PLAIN_TEXT_REMINDER_VTB
        )

        if retry_count < max(0, retry_limit):
            return {"action": "retry", "reminder_text": reminder}
        return {"action": "wait", "reminder_text": ""}

    # ── LLM 请求构造 ────────────────────────────────────

    def create_request(
        self,
        task: str = "actor",
        request_name: str = "",
        with_reminder: str | None = None,
    ) -> LLMRequest:
        """构造 LLM 请求，支持自定义模型集。

        Args:
            task: 任务名。
            request_name: 请求名；空串时用 chatter 名。
            with_reminder: 要注入的 SystemReminder bucket。

        Returns:
            构造好的请求对象。
        """

        config = self._config
        if config is None:
            return super().create_request(task, request_name, with_reminder)

        return request_factory.create_request(
            section=config.plugin,
            stream_id=self.stream_id,
            task=task,
            request_name=request_name or self.name,
            with_reminder=with_reminder,
        )

    # ── 通话超时看门狗 ──────────────────────────────────

    async def _voice_call_timeout_watchdog(self) -> None:
        """轮询检查通话静默超时，到点时挂断。

        写成独立任务而非在 ``sub_agent`` 里检查——后者只在有未读时才被调用，
        双方都不说话时反而不会触发。挂断的副作用包括解除 chatter 接管与重启
        stream 循环，本 chatter 的生成器会在那之后自然退出，因此挂断一次后本
        任务即可结束。
        """

        while True:
            try:
                await asyncio.sleep(_TIMEOUT_CHECK_INTERVAL)
                if not await call_state.is_call_active_for_stream(self.stream_id):
                    continue
                remaining = await call_state.get_remaining_seconds()
                if remaining is None or remaining > 0:
                    continue

                logger.info(f"通话静默超时，自动挂断 stream={self.stream_id}")
                await finalize_call(
                    stream_id=self.stream_id,
                    farewell="（通话超时挂断）",
                    end_reason="timeout",
                    plugin=self._plugin,
                )
                return
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - 看门狗不能因单次异常退出
                logger.warning(f"通话超时检查异常（继续）: {exc}", exc_info=True)

    # ── 主循环 ──────────────────────────────────────────

    async def execute(
        self,
    ) -> AsyncGenerator[Wait | Success | Failure, WaitResumeEvent | None]:
        """通过 chat_core service 跑对话循环。

        Yields:
            会话产出的 ``Wait`` / ``Success`` / ``Failure``。
        """

        chat_stream = await stream_api.activate_stream(self.stream_id)
        if chat_stream is None:
            logger.error(f"无法激活聊天流: {self.stream_id}")
            yield Failure("无法激活聊天流")
            return

        self._active_stream = chat_stream
        self.apply_stream_runtime_options(chat_stream)

        service = get_service(_CHAT_CORE_SERVICE)
        if service is None:
            logger.error(
                f"未找到 {_CHAT_CORE_SERVICE} service。请确认 default_chatter 插件已启用。"
            )
            yield Failure("default_chatter chat_core service 不可用")
            return

        session = cast(ChatCoreServiceLike, service).create_session(
            stream_id=self.stream_id,
            options=self._build_session_options(),
            adapters=self._build_session_adapters(),
        )

        watchdog = create_background_task(
            self._voice_call_timeout_watchdog(),
            name=f"anima_chatter.voice_call_timeout.{self.stream_id[:8]}",
            metadata={"stream_id": self.stream_id, "kind": "voice_call_timeout"},
        )

        try:
            runner = session.execute()
            resume_event: WaitResumeEvent | None = None
            while True:
                try:
                    result = await runner.asend(resume_event)
                except StopAsyncIteration:
                    return
                # chat_core 可能 yield Stop（罕见——stop_conversation 已被屏蔽），
                # 按 Success 收尾。
                if isinstance(result, Stop):
                    yield Success("收到 Stop，按 Success 收尾")
                    return
                resume_event = yield result
        finally:
            self._active_stream = None
            cancel_background_task(watchdog)

    def _build_session_options(self) -> AnimaSessionOptions:
        """构造 chat_core 会话选项。

        Returns:
            会话选项。
        """

        config = self._config
        return AnimaSessionOptions(
            enable_action_suspend=(
                True if config is None else config.plugin.enable_action_suspend
            ),
        )

    def _build_session_adapters(self) -> AnimaSessionAdapters:
        """构造 chat_core 适配器集合——本 chatter 同时充当全部适配器。

        Returns:
            适配器集合。
        """

        return AnimaSessionAdapters(
            request_adapter=self,
            prompt_adapter=self,
            unread_adapter=self,
            usable_adapter=self,
            tool_execution_adapter=self,
            sub_agent_adapter=self,
            logger_adapter=SafeLoggerWrapper(logger),
            plain_text_adapter=self,
        )
