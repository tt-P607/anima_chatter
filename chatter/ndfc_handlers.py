"""anima_chatter 对 NDFC（neo_default_chatter）事件 seam 的转发处理器。

anima_chatter 通过 ``neo_default_chatter:service:chat_core`` 复用 NDFC 的主会话
逻辑。NDFC 会话自包含，通过 ``neo_default_chatter:*`` 事件暴露可替换函数
（seam）。本模块为 anima 接管的 stream 提供一组转发处理器：订阅 NDFC 事件，
从 ``chat_api.get_chatter_by_stream(stream_id)`` 取回当前绑定的
:class:`AnimaChatter` 实例，把事件转发给它已实现的 adapter 方法
（prompt 构建 / 注意力过滤 / 工具注入 / 请求构造 / 未读拉取 / 消息格式化）。

设计要点：

- 所有 handler ``weight`` 均为 ``100``，高于 NDFC 内置 handler（``0`` / ``1`` / ``2``），
  保证先于默认实现执行；用 ``STOP`` 短路默认 handler 实现"替换"语义。
- 只处理 anima 接管的 stream：从 ``params["stream_id"]`` 反查 chatter，若当前
  绑定实例不是 :class:`AnimaChatter` 则返回 ``PASS`` 放行 NDFC 默认行为。
- 各 handler 复用 :class:`AnimaChatter` 上已有的 adapter 方法，不改动其内部逻辑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import chat_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import LLMPayload, ROLE, Text
from src.kernel.event import EventDecision

if TYPE_CHECKING:
    from ..chatter import AnimaChatter

logger = get_logger("anima_chatter.ndfc_handlers")

# NDFC 事件名（字符串字面量，避免对插件内部枚举的源码级 import）。
_EVT_PREPROCESS = "neo_default_chatter:preprocess"
_EVT_INJECT_UNREAD_PAYLOAD = "neo_default_chatter:inject_unread_payload"
_EVT_INJECT_USABLES = "neo_default_chatter:inject_usables"
_EVT_CREATE_REQUEST = "neo_default_chatter:create_request"
_EVT_FETCH_UNREADS = "neo_default_chatter:fetch_unreads"
_EVT_FORMAT_UNREAD_LINE = "neo_default_chatter:format_unread_line"
_EVT_BUILD_HISTORY_TEXT = "neo_default_chatter:build_history_text"

# 所有 anima 转发 handler 的统一权重：高于 NDFC 内置 handler，先执行并以
# STOP 短路默认实现。
_HANDLER_WEIGHT = 100

# 挂在 chat_stream.context 上的"flush 前历史文本"暂存属性。
# :preprocess 事件在 flush 之前发布，params["history_text"] 是干净的
# 历史快照；把它暂存起来，供 :inject_unread_payload 阶段构造 anima
# user prompt 时读取（此时 NDFC 已完成 flush，历史已含本轮未读）。
_HISTORY_TEXT_ATTR = "_anima_chatter_preflush_history_text"


def _get_anima_chatter(stream_id: str) -> "AnimaChatter | None":
    """按 stream 反查当前绑定的 AnimaChatter 实例。

    Args:
        stream_id: 聊天流 ID。

    Returns:
        绑定在该流上的 :class:`AnimaChatter` 实例；不是 anima 接管时返回 ``None``。
    """
    try:
        chatter = chat_api.get_chatter_by_stream(stream_id)
    except (RuntimeError, ValueError):
        return None
    if chatter is None:
        return None
    from ..chatter import AnimaChatter

    return chatter if isinstance(chatter, AnimaChatter) else None


class AnimaPreprocessHandler(BaseEventHandler):
    """转发 ``:preprocess`` 事件——注意力过滤（概率门 + sub_actor 决策）。

    复用 :meth:`AnimaChatter.sub_agent`，把决策结果映射为 NDFC 的
    ``proceed`` / ``reason`` / ``force_stop_minutes``。未读消息文本由 anima
    自己的 ``format_message_line`` 逐条格式化。
    """

    name = "anima_preprocess"
    description = "转发 neo_default_chatter:preprocess 到 AnimaChatter.sub_agent"
    weight = _HANDLER_WEIGHT
    init_subscribe = [_EVT_PREPROCESS]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """执行注意力过滤，把决策写入预填的决策字段。"""
        stream_id = str(params.get("stream_id") or "")
        chatter = _get_anima_chatter(stream_id)
        if chatter is None:
            return EventDecision.PASS, params

        unreads = params.get("unreads") or []
        chat_stream = params.get("chat_stream")
        if not unreads or chat_stream is None:
            return EventDecision.PASS, params

        # 把 flush 前的 history_text 暂存到 chat_stream.context，供后续
        # :inject_unread_payload 阶段构造 anima user prompt 时读取
        # （flush 之后 history 会包含本轮未读，需用 flush 前的快照）。
        history_text = params.get("history_text") or ""
        context = getattr(chat_stream, "context", None)
        if context is not None and history_text:
            setattr(context, _HISTORY_TEXT_ATTR, history_text)

        # 用 anima 的消息格式化逻辑逐条拼 unread_lines，供 sub_actor 决策模型使用。
        unread_lines = "\n".join(
            chatter.format_message_line(msg) for msg in unreads
        )
        try:
            decision = await chatter.sub_agent(unread_lines, list(unreads), chat_stream)
        except Exception as exc:  # noqa: BLE001 - 决策失败不应拦截消息
            logger.warning(f"anima 注意力决策失败，按放行处理 stream={stream_id}: {exc}")
            params["proceed"] = True
            params["reason"] = "anima 注意力决策异常，默认放行"
            return EventDecision.STOP, params

        if decision.get("should_respond"):
            params["proceed"] = True
            params["reason"] = decision.get("reason") or "anima 判定放行"
        else:
            params["proceed"] = False
            params["reason"] = decision.get("reason") or "anima 判定不回复"
        return EventDecision.STOP, params


class AnimaInjectUnreadPayloadHandler(BaseEventHandler):
    """转发 ``:inject_unread_payload``——注入 anima 三模式 USER prompt。

    复用 :meth:`AnimaChatter._build_system_prompt` / :meth:`AnimaChatter._build_user_prompt`
    / :meth:`AnimaChatter._build_negative_behaviors_extra`，直接改写共享的
    ``response``：把 NDFC 默认塞入的 SYSTEM payload 替换为 anima 的模板，
    并用自己的 USER prompt 注入，保证三模式场景文案、VTS 动作表、通话状态
    全部生效。
    """

    name = "anima_inject_unread_payload"
    description = "转发 neo_default_chatter:inject_unread_payload 到 AnimaChatter prompt 构建"
    weight = _HANDLER_WEIGHT
    init_subscribe = [_EVT_INJECT_UNREAD_PAYLOAD]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """把 anima 的 system / user prompt 注入共享 response。"""
        stream_id = str(params.get("stream_id") or "")
        chatter = _get_anima_chatter(stream_id)
        if chatter is None:
            return EventDecision.PASS, params

        response = params.get("response")
        if response is None:
            return EventDecision.PASS, params

        formatted_text = params.get("formatted_text") or ""
        unread_msgs = params.get("unread_msgs") or []
        chat_stream = getattr(chatter, "_active_stream", None)

        try:
            # 1) 用 anima 的 system prompt 替换 NDFC 默认塞入的 SYSTEM payload。
            if chat_stream is not None:
                system_text = await chatter._build_system_prompt(chat_stream)
                if system_text:
                    _replace_system_payload(response, system_text)

            # 2) 用 anima 的 user prompt 注入：历史文本取 :preprocess 阶段暂存的
            #    flush 前快照，未读文本用 anima 的 format_message_line 现算。
            extra = chatter._build_negative_behaviors_extra()
            if chat_stream is not None:
                context = getattr(chat_stream, "context", None)
                # 只消费本次 :preprocess 暂存的干净历史；用完即清，避免 resume
                # 场景（不经过 preprocess）读到上一轮的旧快照。
                history_text = ""
                if context is not None:
                    history_text = getattr(context, _HISTORY_TEXT_ATTR, "")
                    setattr(context, _HISTORY_TEXT_ATTR, "")
                if not history_text:
                    history_text = chatter._build_enhanced_history_text(chat_stream)
                unread_lines = "\n".join(
                    chatter.format_message_line(msg) for msg in unread_msgs
                )
                user_text = await chatter._build_user_prompt(
                    chat_stream,
                    history_text=history_text,
                    unread_lines=unread_lines,
                    extra=extra,
                )
            else:
                user_text = formatted_text
            response.add_payload(LLMPayload(ROLE.USER, Text(user_text)))
            params["skip"] = True
            return EventDecision.STOP, params
        except Exception as exc:  # noqa: BLE001 - 注入失败放行默认
            logger.warning(f"anima prompt 注入失败，放行默认 stream={stream_id}: {exc}")
            return EventDecision.PASS, params


class AnimaInjectUsablesHandler(BaseEventHandler):
    """转发 ``:inject_usables``——注入工具并屏蔽与 anima 冲突的动作。

    复用 :meth:`AnimaChatter.inject_usables`（含 ``_BLOCKED_USABLE_NAMES`` 过滤）。
    """

    name = "anima_inject_usables"
    description = "转发 neo_default_chatter:inject_usables 到 AnimaChatter.inject_usables"
    weight = _HANDLER_WEIGHT
    init_subscribe = [_EVT_INJECT_USABLES]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """执行工具注入，把 ToolRegistry 填入 payload。"""
        stream_id = str(params.get("stream_id") or "")
        chatter = _get_anima_chatter(stream_id)
        if chatter is None:
            return EventDecision.PASS, params

        request = params.get("request")
        if request is None:
            return EventDecision.PASS, params

        try:
            registry = await chatter.inject_usables(request)
            params["tool_registry"] = registry
            return EventDecision.STOP, params
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"anima 工具注入失败，放行默认 stream={stream_id}: {exc}")
            return EventDecision.PASS, params


class AnimaCreateRequestHandler(BaseEventHandler):
    """转发 ``:create_request``——自定义模型集 + SystemReminder bucket。

    复用 :meth:`AnimaChatter.create_request`（基于 anima 的 ``[plugin]`` 配置）。
    """

    name = "anima_create_request"
    description = "转发 neo_default_chatter:create_request 到 AnimaChatter.create_request"
    weight = _HANDLER_WEIGHT
    init_subscribe = [_EVT_CREATE_REQUEST]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """构造 LLM 请求，把 LLMRequest 填入 payload。"""
        stream_id = str(params.get("stream_id") or "")
        chatter = _get_anima_chatter(stream_id)
        if chatter is None:
            return EventDecision.PASS, params

        try:
            request = chatter.create_request(
                params.get("task_name") or "actor",
                params.get("request_name") or "",
                params.get("with_reminder"),
            )
            params["request"] = request
            return EventDecision.STOP, params
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"anima 请求构造失败，放行默认 stream={stream_id}: {exc}")
            return EventDecision.PASS, params


class AnimaFetchUnreadsHandler(BaseEventHandler):
    """转发 ``:fetch_unreads``——拉未读 + 通话入档 + vtb_live 流水线门。

    复用 :meth:`AnimaChatter.fetch_unreads`。
    """

    name = "anima_fetch_unreads"
    description = "转发 neo_default_chatter:fetch_unreads 到 AnimaChatter.fetch_unreads"
    weight = _HANDLER_WEIGHT
    init_subscribe = [_EVT_FETCH_UNREADS]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """拉取未读消息，把 messages 填入 payload。"""
        stream_id = str(params.get("stream_id") or "")
        chatter = _get_anima_chatter(stream_id)
        if chatter is None:
            return EventDecision.PASS, params

        try:
            _, messages = await chatter.fetch_unreads()
            params["messages"] = messages
            return EventDecision.STOP, params
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"anima 未读拉取失败，放行默认 stream={stream_id}: {exc}")
            return EventDecision.PASS, params


class AnimaFormatUnreadLineHandler(BaseEventHandler):
    """转发 ``:format_unread_line``——直播来源平台前缀。

    复用 :meth:`AnimaChatter.format_message_line`。
    """

    name = "anima_format_unread_line"
    description = "转发 neo_default_chatter:format_unread_line 到 AnimaChatter.format_message_line"
    weight = _HANDLER_WEIGHT
    init_subscribe = [_EVT_FORMAT_UNREAD_LINE]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """格式化单条未读消息。"""
        stream_id = str(params.get("stream_id") or "")
        chatter = _get_anima_chatter(stream_id)
        if chatter is None:
            return EventDecision.PASS, params

        message = params.get("message")
        if message is None:
            return EventDecision.PASS, params

        try:
            params["formatted_line"] = chatter.format_message_line(
                message, params.get("time_format") or "%H:%M"
            )
            return EventDecision.STOP, params
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"anima 消息格式化失败，放行默认 stream={stream_id}: {exc}")
            return EventDecision.PASS, params


class AnimaBuildHistoryTextHandler(BaseEventHandler):
    """转发 ``:build_history_text``——用 anima 的历史格式化逻辑。

    复用 :meth:`AnimaChatter._build_enhanced_history_text`。
    """

    name = "anima_build_history_text"
    description = (
        "转发 neo_default_chatter:build_history_text 到 "
        "AnimaChatter._build_enhanced_history_text"
    )
    weight = _HANDLER_WEIGHT
    init_subscribe = [_EVT_BUILD_HISTORY_TEXT]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """构建历史文本，把按行拆分的 list 填入 payload。"""
        stream_id = str(params.get("stream_id") or "")
        chatter = _get_anima_chatter(stream_id)
        if chatter is None:
            return EventDecision.PASS, params

        chat_stream = params.get("chat_stream")
        if chat_stream is None:
            return EventDecision.PASS, params

        try:
            text = chatter._build_enhanced_history_text(chat_stream)
            params["lines"] = text.split("\n") if text else []
            return EventDecision.STOP, params
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"anima 历史构建失败，放行默认 stream={stream_id}: {exc}")
            return EventDecision.PASS, params


# ── 工具函数 ──────────────────────────────────────────────


def _replace_system_payload(response: Any, system_text: str) -> None:
    """把 response 中第一条 SYSTEM payload 的内容替换为 anima 的 system prompt。

    NDFC 会话在 ``_execute_with_stream`` 阶段 0.3 会先注入一条 ``ROLE.SYSTEM``
    的默认 prompt（``session.py:385-388``）。anima 需要用自己的模板覆盖它——
    直接改 ``payloads[0].content``（保持 position 不变，避免破坏后续 context
    合并逻辑）。

    Args:
        response: LLMRequest / LLMResponse（均有 ``payloads`` 字段）。
        system_text: 要写入的 system prompt 文本。
    """
    payloads = getattr(response, "payloads", None)
    if not payloads:
        return
    for payload in payloads:
        if getattr(payload, "role", None) == ROLE.SYSTEM:
            payload.content = [Text(system_text)]  # type: ignore[attr-defined]
            return


__all__ = [
    "AnimaBuildHistoryTextHandler",
    "AnimaCreateRequestHandler",
    "AnimaFetchUnreadsHandler",
    "AnimaFormatUnreadLineHandler",
    "AnimaInjectUnreadPayloadHandler",
    "AnimaInjectUsablesHandler",
    "AnimaPreprocessHandler",
]
