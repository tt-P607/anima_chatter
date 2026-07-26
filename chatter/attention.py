"""vtb / vtb_live 模式的"是否回复"注意力过滤。

两级过滤：

1. **概率门**（本地、无 LLM）：基础概率 + 命中名字 / 别名 / 未读条数 / 上一回合
   刚回复等加成，命中就直接放行。权重与 default_chatter 完全一致的硬编码，避免
   插件之间行为漂移。
2. **sub_actor LLM 决策**：概率门没放行时，用小模型判断这批消息值不值得回。

voice 模式与私聊场景直通响应，不做过滤。
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any, TypedDict

import json_repair

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import ChatStream, LLMPayload, Message, ROLE, Text

from .._internal_compat import get_personality, get_prompt_manager
from ..modes import ChatterMode
from ..prompts.sub_agent import SUB_AGENT_PROMPT_LIVE, SUB_AGENT_PROMPT_VTB

if TYPE_CHECKING:
    from ..config import VTBAttentionSection


logger = get_logger("anima_chatter.attention")


__all__ = [
    "SubAgentDecision",
    "compute_bypass_probability",
    "decide_should_respond",
    "mark_reply_success",
    "passes_probability_gate",
    "resolve_sub_agent_prompt_source",
]


class SubAgentDecision(TypedDict):
    """注意力决策结果。

    Attributes:
        should_respond: 本轮是否应该回复。
        reason: 决策理由，用于日志与调试。
    """

    should_respond: bool
    reason: str


# ── 概率门权重（与 default_chatter 一致的硬编码，不通过配置暴露） ──
_BASE_BYPASS_PROBABILITY = 0.1
_NAME_MENTION_BONUS = 0.7
_ALIAS_MENTION_BONUS = 0.4
_UNREAD_MESSAGE_BONUS = 0.05
_NEXT_TICK_REPLY_BONUS = 0.5

# 加成值挂在 stream context 上的属性名。
_NEXT_TICK_BONUS_ATTR = "_anima_chatter_next_tick_bonus"


def mark_reply_success(chat_stream: ChatStream) -> None:
    """标记本轮已成功回复，给下一 tick 的概率门加成。

    模型刚说完话之后用户继续讲话时，更容易顺势继续对话。与 default_chatter
    的行为一致。

    Args:
        chat_stream: 当前聊天流。
    """

    current = float(getattr(chat_stream.context, _NEXT_TICK_BONUS_ATTR, 0.0))
    setattr(
        chat_stream.context,
        _NEXT_TICK_BONUS_ATTR,
        max(current, _NEXT_TICK_REPLY_BONUS),
    )


def _consume_next_tick_bonus(chat_stream: ChatStream) -> float:
    """读取并清空"上一回合刚回复"的加成。

    Args:
        chat_stream: 当前聊天流。

    Returns:
        加成值；没有加成时为 ``0.0``。
    """

    bonus = float(getattr(chat_stream.context, _NEXT_TICK_BONUS_ATTR, 0.0))
    setattr(chat_stream.context, _NEXT_TICK_BONUS_ATTR, 0.0)
    return bonus


def _message_text(message: Message) -> str:
    """取出消息中用于关键词匹配的文本。

    Args:
        message: 待提取的消息。

    Returns:
        消息正文；没有处理后文本时回退到原始 content。
    """

    if message.processed_plain_text:
        return message.processed_plain_text
    if isinstance(message.content, str):
        return message.content
    return str(message.content)


def _messages_contain_any(messages: list[Message], names: list[str]) -> bool:
    """判断消息里是否出现了任意一个名字。

    Args:
        messages: 待检查的消息列表。
        names: 名字 / 别名列表。

    Returns:
        命中任意一个时返回 ``True``。
    """

    normalized = [name.strip().lower() for name in names if name.strip()]
    if not normalized:
        return False
    return any(
        any(name in _message_text(message).lower() for name in normalized)
        for message in messages
    )


def _identity_names(chat_stream: ChatStream) -> tuple[str, list[str]]:
    """获取 bot 的主名字与别名列表。

    Args:
        chat_stream: 当前聊天流，用于取平台侧昵称作为兜底。

    Returns:
        ``(主名字, 别名列表)``。
    """

    fallback = (chat_stream.bot_nickname or "").strip()
    try:
        personality = get_personality()
    except RuntimeError:
        return fallback, []

    nickname = (personality.nickname or "").strip() or fallback
    aliases = [alias.strip() for alias in personality.alias_names if alias.strip()]
    return nickname, aliases


def compute_bypass_probability(
    unread_msgs: list[Message],
    chat_stream: ChatStream,
) -> tuple[float, str]:
    """计算"无 LLM 直通"的放行概率。

    Args:
        unread_msgs: 本轮未读消息。
        chat_stream: 当前聊天流。

    Returns:
        ``(概率, 理由描述)``；概率上限 1.0。
    """

    nickname, aliases = _identity_names(chat_stream)

    probability = _BASE_BYPASS_PROBABILITY
    reasons = [f"基础概率 {_BASE_BYPASS_PROBABILITY:.2f}"]

    if nickname and _messages_contain_any(unread_msgs, [nickname]):
        probability += _NAME_MENTION_BONUS
        reasons.append(f"命中名字 +{_NAME_MENTION_BONUS:.2f}")

    if _messages_contain_any(unread_msgs, aliases):
        probability += _ALIAS_MENTION_BONUS
        reasons.append(f"命中别名 +{_ALIAS_MENTION_BONUS:.2f}")

    unread_bonus = len(unread_msgs) * _UNREAD_MESSAGE_BONUS
    if unread_bonus > 0:
        probability += unread_bonus
        reasons.append(f"{len(unread_msgs)} 条未读 +{unread_bonus:.2f}")

    next_tick = _consume_next_tick_bonus(chat_stream)
    if next_tick > 0:
        probability += next_tick
        reasons.append(f"上一回合刚回复 +{next_tick:.2f}")

    capped = min(probability, 1.0)
    if capped != probability:
        reasons.append("封顶 1.00")
    return capped, "，".join(reasons)


def resolve_sub_agent_prompt_source(mode: ChatterMode) -> tuple[str, str]:
    """按模式选择 sub-agent 决策 prompt。

    Args:
        mode: 当前运行模式。

    Returns:
        ``(模板名, 模板内容兜底)``——模板未注册时用后者格式化。
    """

    if mode == "vtb_live":
        return "anima_chatter_sub_agent_prompt_vtb_live", SUB_AGENT_PROMPT_LIVE
    return "anima_chatter_sub_agent_prompt_vtb", SUB_AGENT_PROMPT_VTB


async def _render_sub_agent_prompt(
    template_name: str,
    fallback: str,
    chat_stream: ChatStream,
) -> str:
    """渲染 sub-agent 决策 prompt。

    Args:
        template_name: prompt manager 中的模板名。
        fallback: 模板未注册时的兜底模板串。
        chat_stream: 当前聊天流，用于取 bot 平台标识。

    Returns:
        渲染好的 prompt 文本。
    """

    personality = get_personality()
    bot_id = chat_stream.bot_id or ""
    bot_id_section = f"它的平台标识是 {bot_id}。\n" if bot_id else ""

    template = get_prompt_manager().get_template(template_name)
    if template is None:
        return fallback.format(
            nickname=personality.nickname,
            bot_id=bot_id,
            bot_id_section=bot_id_section,
            personality_core_section=personality.personality_core,
            personality_side_section=personality.personality_side,
        )

    return await (
        template.set("nickname", personality.nickname)
        .set("bot_id", bot_id)
        .set("bot_id_section", bot_id_section)
        .set("personality_core_section", personality.personality_core)
        .set("personality_side_section", personality.personality_side)
        .build()
    )


async def decide_should_respond(
    *,
    request: Any,
    unreads_text: str,
    chat_stream: ChatStream,
    template_name: str,
    fallback_prompt: str,
) -> SubAgentDecision:
    """调用 sub_actor 模型判断本批消息是否需要回复。

    Args:
        request: 已构造好的 LLM 请求对象（由 chatter 的 request factory 提供）。
        unreads_text: 格式化后的未读消息文本。
        chat_stream: 当前聊天流。
        template_name: 决策 prompt 的模板名。
        fallback_prompt: 模板未注册时的兜底模板串。

    Returns:
        决策结果；任何异常都降级为"响应"，避免因决策失败让 bot 完全哑火。
    """

    sub_prompt = await _render_sub_agent_prompt(
        template_name, fallback_prompt, chat_stream
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
    except Exception as error:  # noqa: BLE001 - LLM 调用异常类型不受控
        logger.error(f"注意力决策执行失败: {error}", exc_info=True)
    return {"should_respond": True, "reason": "决策失败，默认响应"}


def passes_probability_gate(
    section: "VTBAttentionSection",
    unread_msgs: list[Message],
    chat_stream: ChatStream,
) -> tuple[bool, str]:
    """执行本地概率门。

    Args:
        section: 注意力过滤配置段。
        unread_msgs: 本轮未读消息。
        chat_stream: 当前聊天流。

    Returns:
        ``(是否直通放行, 理由描述)``。控制器关闭时始终返回 ``(False, ...)``，
        交给 LLM 决策。
    """

    if not section.enable_programmatic_controller:
        return False, "程序化控制器已关闭"
    probability, reason = compute_bypass_probability(unread_msgs, chat_stream)
    return random.random() < probability, reason
