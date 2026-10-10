"""直播会话编排、弹幕注意力和 NDFC 事件适配。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, ClassVar, cast

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
    ROLE,
    ChatStream,
    ChatType,
    LLMPayload,
    LLMRequest,
    LLMUsable,
    Message,
    Text,
    ToolRegistry,
)

from ..prompts import (
    PLAIN_TEXT_REMINDER_VTB,
    SPEECH_RULES_TEMPLATE_NAME,
    AnimaChatterPromptBuilder,
)
from ..runtime import pipeline_state
from . import attention, request_factory
from .attention import SubAgentDecision
from .session_bridge import ChatCoreServiceLike, PlainTextResponseHandling

if TYPE_CHECKING:
    from ..config import AnimaChatterConfig
    from ..protocol import AnimaPlugin


logger = get_logger("anima_chatter")


__all__ = ["AnimaChatter"]


_CHAT_CORE_SERVICE = "neo_default_chatter:service:chat_core"

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
        "action-say",
        "action-start_voice_call",
        "action-end_voice_call",
    }
)


class AnimaChatter(BaseChatter):
    """直播弹幕互动与虚拟形象表演的会话控制器。"""

    name = "anima_chatter"
    description = "直播弹幕互动、VTube Studio 表演与唱歌 Chatter。"
    associated_platforms: ClassVar[list[str]] = ["live"]
    speech_rules_template_name: ClassVar[str] = SPEECH_RULES_TEMPLATE_NAME
    chat_type = ChatType.ALL

    stream_tick_interval = 1.0
    allow_message_buffer = True

    # 工具注入缓存：(usable 签名指纹, registry, tools)。类级共享——同一轮
    # 高压弹幕下多个实例注入的工具集一致；集合变化时指纹失配自动重建。
    _usables_cache: tuple[tuple[str, ...], ToolRegistry, list] | None = None

    def __init__(self, stream_id: str, plugin: Any) -> None:
        """初始化 chatter 实例状态。

        Args:
            stream_id: 绑定的聊天流 ID。
            plugin: 所属插件实例。
        """

        super().__init__(stream_id, plugin)
        # 实例属性（旧实现为类注解 + 实例赋值，restart 竞态下可能互覆）。
        self._active_stream: ChatStream | None = None

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

    def apply_stream_runtime_options(self, chat_stream: ChatStream) -> None:
        """应用直播配置的 tick 间隔与消息缓冲策略。

        Args:
            chat_stream: 当前聊天流。
        """

        config = self._config
        if config is not None:
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
        source = (
            msg.extra.get("source_platform") if isinstance(msg.extra, dict) else None
        )
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
        """在容量门通过后读取未读弹幕快照。

        等待不会推进回复轮；轮次只在 NDFC 确认进入 ``MODEL_TURN`` 时 claim。

        Args:
            time_format: 时间格式串。

        Returns:
            ``(格式化文本, 未读消息列表)``。
        """

        await pipeline_state.wait_gate(self.stream_id)
        return await super().fetch_unreads(time_format=time_format)

    async def prepare_response_round(self) -> None:
        """在真实 Actor 回复轮开始前等待容量并标记新一轮。"""

        try:
            chat_stream = await stream_api.activate_stream(self.stream_id)
        except (RuntimeError, ValueError):
            return
        if chat_stream is None or chat_stream.platform != "live":
            return

        had_gate = await pipeline_state.is_gate_pending(self.stream_id)
        await pipeline_state.wait_gate(self.stream_id)
        await pipeline_state.reset_round(self.stream_id)
        if had_gate:
            logger.info("fetch_unreads 通过流水线门，准备聚合期间累积的所有弹幕")

    # ── UsableAdapter 协议 ──────────────────────────────

    async def inject_usables(self, request: LLMRequest) -> ToolRegistry:
        """注入可用工具，屏蔽与本插件冲突的动作。

        schema 按"usable 集合指纹"缓存：集合未变时直接复用上次的
        ``(registry, tools)``，避免高压下每轮 LLM 调用都全量重建 schema 与
        ToolRegistry（schema 是纯静态 classmethod 产物，重建是纯浪费）。
        集合变化（组件启用 / 禁用 / 其他插件注册变动）时指纹失配自动重建。

        Args:
            request: 待注入的 LLM 请求。

        Returns:
            注册好的工具表。
        """

        usables: list[type[LLMUsable]] = await self.get_llm_usables()
        usables = await self.modify_llm_usables(usables)

        fingerprint = tuple(
            sorted(
                usable.get_signature() or usable.__name__  # type: ignore[attr-defined]
                for usable in usables
            )
        )
        cached = type(self)._usables_cache
        if cached is not None and cached[0] == fingerprint:
            registry = cached[1]
            tools = cached[2]
        else:
            from ..actions import AnimaPassAndWaitAction

            registry = ToolRegistry()
            for usable in usables:
                schema = usable.to_schema()
                name = str(schema.get("function", {}).get("name", ""))
                if name in _BLOCKED_USABLE_NAMES:
                    continue
                if (
                    name in {"pass_and_wait", "action-pass_and_wait"}
                    and usable is not AnimaPassAndWaitAction
                ):
                    continue
                registry.register(usable)
            tools = registry.get_all()
            type(self)._usables_cache = (fingerprint, registry, tools)

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

        使用本地概率门和 sub_actor LLM 决策筛选直播弹幕。

        Args:
            unreads_text: 格式化后的未读文本。
            unread_msgs: 未读消息列表。
            chat_stream: 当前聊天流。

        Returns:
            决策结果。
        """

        self._log_danmaku_preview(unread_msgs)

        config = self._config
        if config is None or not config.vtb_attention.enabled:
            return {"should_respond": True, "reason": "注意力过滤未启用，直接响应"}

        passed, reason = attention.passes_probability_gate(
            config.vtb_attention, unread_msgs, chat_stream
        )
        if passed:
            return {"should_respond": True, "reason": f"概率直通响应：{reason}"}

        template_name, fallback_prompt = attention.resolve_sub_agent_prompt_source()
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

        if retry_count < max(0, retry_limit):
            return {"action": "retry", "reminder_text": PLAIN_TEXT_REMINDER_VTB}
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

        if chat_stream.platform != "live":
            yield Failure("anima_chatter 仅处理直播聊天流")
            return

        self._active_stream = chat_stream
        self.apply_stream_runtime_options(chat_stream)

        service = get_service(_CHAT_CORE_SERVICE)
        if service is None:
            logger.error(
                f"未找到 {_CHAT_CORE_SERVICE} service。请确认 neo_default_chatter 插件已启用。"
            )
            yield Failure("neo_default_chatter chat_core service 不可用")
            return

        # NDFC 会话自包含：不接受 adapters/options，通过 neo_default_chatter:*
        # 事件 seam 定制。传入 anima 插件实例供会话读取基础配置（anima 的
        # 差异化逻辑由 ndfc_handlers 转发，会话自身的可替换函数全部被替换）。
        session = cast(ChatCoreServiceLike, service).create_session(
            stream_id=self.stream_id,
            plugin=self.plugin,
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
