"""voice_chatter VTB 模式的子代理消息过滤。

复刻自 :mod:`plugins.default_chatter.decision_agent` + DefaultChatter 主类
里的 sub-agent 概率门逻辑，专门用于 vtb 模式（``platform != "local_asr"``）。

.. warning::

   **本模块与 default_chatter 是手动同步关系**，没有共享代码。

   如果你修改了 dfc 的 ``decision_agent`` 中：

   - 概率门权重（``_BASE_BYPASS_PROBABILITY`` / ``_NAME_MENTION_BONUS`` 等）
   - sub_actor LLM 调用流程
   - "上一回合刚回复 → 下一 tick 加成" 这条心理机制

   请**同步**回这个文件；否则 voice_chatter（vtb / vtb_live 模式）的
   注意力过滤行为会与 dfc 漂移。

两层过滤：

1. **概率门（本地 / 不调用 LLM）**：基础概率 + @名字 / 别名 / 未读条数加成。
   命中即直通响应，最大化降低 LLM 负担。
2. **决策 LLM（``sub_actor`` task）**：未命中概率门时调用一个轻量模型，
   输出 JSON ``{"should_respond": bool, "reason": str}``。

模块按需 ``await voice_chatter_sub_agent.should_respond(...)``，输入为
未读消息 + chat_stream，返回 :class:`SubAgentDecision`。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING, TypedDict

import json_repair

from src.core.config import get_core_config
from src.core.models.message import Message
from src.core.models.stream import ChatStream
from src.core.prompt import get_prompt_manager
from src.kernel.llm import LLMPayload, ROLE, Text
from src.kernel.llm.token_counter import count_text_tokens
from src.kernel.logger import Logger

if TYPE_CHECKING:
    from src.kernel.llm import LLMRequest


# ── 概率门权重（与 dfc 完全一致，硬编码不暴露） ─────────────────
_BASE_BYPASS_PROBABILITY = 0.1
_NAME_MENTION_BONUS = 0.7
_ALIAS_MENTION_BONUS = 0.4
_UNREAD_MESSAGE_BONUS = 0.05
_NEXT_TICK_REPLY_BONUS = 0.5

# 写到 stream context 上的字段名，记录"上次回复后下一 tick 加成"。
_NEXT_TICK_BONUS_ATTR = "_voice_chatter_next_tick_bonus"


@dataclass
class SubAgentConfig:
    """sub-agent 行为开关；与 dfc 同名设置对应。

    - ``enabled``：总开关，关闭后整个过滤器不生效（每条未读都直通响应）。
    - ``enable_programmatic_controller``：是否启用本地概率门；关闭后跳过概率
      直通逻辑，所有判定走 sub_actor LLM。
    """

    enabled: bool = True
    enable_programmatic_controller: bool = True


_DEFAULT_SUB_AGENT_CONFIG = SubAgentConfig()


# ── fallback prompt（与 dfc 类似，但措辞更贴近 VTB 直播场景） ──
_FALLBACK_SUB_AGENT_PROMPT = """你是一个聊天意图识别助手。
你的任务是分析新收到的聊天消息，结合历史上下文，判断当前正在做 VTB 直播
互动的虚拟形象主播是否有必要进行响应。

# 关于主机器人
主机器人的名字是 {nickname}。
{bot_id_section}{personality_core_section}{personality_side_section}
# 判定准则（VTB 直播场景）
你应该在以下情况判定为 "需要回复" (should_respond = true)：
1. 明确提及：消息中明确提到了机器人的名字或代称；或@了机器人（QQ 号 = {bot_id}）。
2. 话题相关：消息内容与正在进行的 VTB 互动话题高度相关，需要主播参与/回应。
3. 情感互动：消息表达问候、告别、称赞、抱怨、提问等需要回应的情绪。
4. 直接邀请：观众请求主播做某个动作、唱歌、表演等。

你应该在以下情况判定为 "不需要回复" (should_respond = false)：
1. 话题无关：是其他观众之间的闲聊，主播不是话题参与者。
2. 艾特他人：消息艾特了其他人（QQ 号不是 {bot_id}）。
3. 话未说完：明显是连续消息中的中间部分，可以等后续。
4. 机器博弈：检测到是其他 Bot 自动回复或刷屏。
5. 纯粹表情/弹幕：只有单个表情/无意义符号。

# 输出格式
请务必返回 JSON：
```json
{{
    "reason": "简短的判定理由",
    "should_respond": true/false
}}
```
"""


VOICE_CHATTER_SUB_AGENT_PROMPT_TEMPLATE = _FALLBACK_SUB_AGENT_PROMPT


class SubAgentDecision(TypedDict):
    """子代理是否响应的判定结果。"""

    reason: str
    should_respond: bool


# ── 工具函数 ─────────────────────────────────────────────────


def _set_next_tick_bonus(chat_stream: ChatStream, bonus: float) -> None:
    """为下一 tick 的概率门写入加成（用于"上一回合刚回复完，下一条更可能继续"）。"""

    current = float(getattr(chat_stream.context, _NEXT_TICK_BONUS_ATTR, 0.0))
    setattr(
        chat_stream.context,
        _NEXT_TICK_BONUS_ATTR,
        max(current, float(bonus)),
    )


def _consume_next_tick_bonus(chat_stream: ChatStream) -> float:
    """读取并清空下一 tick 加成。"""

    bonus = float(getattr(chat_stream.context, _NEXT_TICK_BONUS_ATTR, 0.0))
    setattr(chat_stream.context, _NEXT_TICK_BONUS_ATTR, 0.0)
    return bonus


def mark_reply_success(chat_stream: ChatStream) -> None:
    """供 SayAndPerformAction 在成功发出回复后调用。

    会让下一次 tick 的概率门多 +0.5，这样模型刚说完话之后用户继续讲话时
    更容易顺势继续。与 dfc 行为完全一致。
    """

    _set_next_tick_bonus(chat_stream, _NEXT_TICK_REPLY_BONUS)


def _message_text(message: Message) -> str:
    """提取消息文本（用于关键词命中判定）。"""

    if isinstance(message.processed_plain_text, str) and message.processed_plain_text:
        return message.processed_plain_text
    if isinstance(message.content, str):
        return message.content
    return str(message.content)


def _messages_contain_any(unread_msgs: list[Message], names: list[str]) -> bool:
    """判断任意未读消息是否包含给定名字/别名（大小写不敏感）。"""

    normalized = [name.strip().lower() for name in names if name.strip()]
    if not normalized:
        return False
    for msg in unread_msgs:
        lowered = _message_text(msg).lower()
        if any(name in lowered for name in normalized):
            return True
    return False


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


def compute_bypass_probability(
    unread_msgs: list[Message],
    chat_stream: ChatStream,
) -> tuple[float, str]:
    """计算"无 LLM 直通"的放行概率（带说明文本）。"""

    nickname, alias_names = _identity_names(chat_stream)

    probability = _BASE_BYPASS_PROBABILITY
    reasons = [f"基础概率 {_BASE_BYPASS_PROBABILITY:.2f}"]

    if nickname and _messages_contain_any(unread_msgs, [nickname]):
        probability += _NAME_MENTION_BONUS
        reasons.append(f"命中名字 +{_NAME_MENTION_BONUS:.2f}")

    if _messages_contain_any(unread_msgs, alias_names):
        probability += _ALIAS_MENTION_BONUS
        reasons.append(f"命中别名 +{_ALIAS_MENTION_BONUS:.2f}")

    unread_bonus = len(unread_msgs) * _UNREAD_MESSAGE_BONUS
    if unread_bonus > 0:
        probability += unread_bonus
        reasons.append(f"{len(unread_msgs)} 条未读 +{unread_bonus:.2f}")

    next_tick_bonus = _consume_next_tick_bonus(chat_stream)
    if next_tick_bonus > 0:
        probability += next_tick_bonus
        reasons.append(f"上一回合刚回复 +{next_tick_bonus:.2f}")

    capped = min(probability, 1.0)
    if capped != probability:
        reasons.append("封顶 1.00")

    return capped, "，".join(reasons)


# ── token 预算控制（避免长未读把 sub_actor 上下文撑爆） ─────


def _safe_count_tokens(text: str, model_identifier: str) -> int:
    """安全调用 token 计数（失败返回 0）。"""

    try:
        return count_text_tokens(text, model_identifier=model_identifier)
    except Exception:
        return 0


def _trim_text_suffix_by_budget(text: str, model_identifier: str, budget: int) -> str:
    """保留文本末尾内容，使总 token 数 ≤ budget；逐行回退或二分切。"""

    if budget <= 0 or not text:
        return ""

    total = _safe_count_tokens(text, model_identifier)
    if total <= budget:
        return text

    # 1) 行级回退（保留最后几行）
    lines = text.splitlines()
    kept_reversed: list[str] = []
    used = 0
    for line in reversed(lines):
        line_tokens = _safe_count_tokens(line, model_identifier)
        if kept_reversed and used + line_tokens > budget:
            break
        kept_reversed.append(line)
        used += line_tokens
    candidate = "\n".join(reversed(kept_reversed)).strip()
    if candidate and _safe_count_tokens(candidate, model_identifier) <= budget:
        return candidate

    # 2) 二分查找最大可保留的尾部子串
    left, right = 0, len(text)
    best = text[-512:]
    while left <= right:
        mid = (left + right) // 2
        suffix = text[mid:]
        token_count = _safe_count_tokens(suffix, model_identifier)
        if token_count == 0 or token_count > budget:
            left = mid + 1
            continue
        best = suffix
        right = mid - 1
    return best.strip()


def _fit_unreads_to_budget(request: "LLMRequest", unreads_text: str) -> str:
    """根据 sub_actor 的 model_set 限制未读消息长度。"""

    model_set = getattr(request, "model_set", None)
    if not isinstance(model_set, list) or not model_set:
        return unreads_text

    first_model = model_set[0]
    if not isinstance(first_model, dict):
        return unreads_text

    model_identifier = first_model.get("model_identifier")
    if not isinstance(model_identifier, str) or not model_identifier:
        return unreads_text

    max_context = first_model.get("max_context")
    if isinstance(max_context, int) and max_context > 0:
        budget = min(max(1024, max_context // 4), 8000)
    else:
        budget = 6000

    return _trim_text_suffix_by_budget(unreads_text, model_identifier, budget)


# ── 决策 LLM 调用 ─────────────────────────────────────────────


async def _decide_via_llm(
    *,
    chatter: Any,
    logger: Logger,
    unreads_text: str,
    chat_stream: ChatStream,
) -> SubAgentDecision:
    """调用 sub_actor 模型做正式决策。失败时降级为允许响应。"""

    try:
        request = chatter.create_request(
            "sub_actor",
            "voice_chatter_sub_agent",
            with_reminder="sub_actor",
        )
    except (ValueError, KeyError):
        return {"should_respond": True, "reason": "未找到 sub_actor 配置，默认响应"}

    nickname = get_core_config().personality.nickname
    bot_id = chat_stream.bot_id or ""
    bot_id_section = f"它的 QQ 号是 {bot_id}。\n" if bot_id else ""

    tmpl = get_prompt_manager().get_template("voice_chatter_sub_agent_prompt")
    if tmpl:
        sub_prompt = (
            await tmpl
            .set("nickname", nickname)
            .set("bot_id", bot_id)
            .set("bot_id_section", bot_id_section)
            .build()
        )
    else:
        sub_prompt = _FALLBACK_SUB_AGENT_PROMPT.format(
            nickname=nickname,
            bot_id=bot_id,
            bot_id_section=bot_id_section,
            personality_core_section="",
            personality_side_section="",
        )

    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(sub_prompt)))

    fitted = _fit_unreads_to_budget(request, unreads_text)
    if len(fitted) < len(unreads_text):
        logger.info(
            f"sub-agent 输入已截断: {len(unreads_text)} -> {len(fitted)} 字符"
        )
    request.add_payload(
        LLMPayload(ROLE.USER, Text(f"【新收到待判定消息】\n{fitted}"))
    )

    try:
        response = await request.send(stream=False)
        await response

        content = response.message
        if not content or not content.strip():
            logger.warning("sub-agent 返回空内容，默认响应")
            return {"should_respond": True, "reason": "模型未返回判断内容"}

        try:
            result = json_repair.loads(content)
            if isinstance(result, dict):
                return {
                    "should_respond": bool(result.get("should_respond", True)),
                    "reason": str(result.get("reason", "未提供理由")),
                }
        except Exception as exc:
            logger.debug(f"sub-agent JSON 解析失败: {exc} | 内容: {content[:300]}")

        logger.warning(f"sub-agent 输出不是合法 JSON，默认响应: {content[:200]}...")
        return {"should_respond": True, "reason": "JSON 解析失败，默认响应"}
    except Exception as exc:
        logger.error(f"sub-agent 决策异常: {exc}", exc_info=True)
        return {"should_respond": True, "reason": f"执行异常: {exc}"}


# ── 对外主入口 ───────────────────────────────────────────────


async def should_respond(
    *,
    chatter: Any,
    logger: Logger,
    unreads_text: str,
    unread_msgs: list[Message],
    chat_stream: ChatStream,
    config: SubAgentConfig | None = None,
) -> SubAgentDecision:
    """vtb 模式下决定是否对未读消息做出响应。

    Args:
        chatter: 当前 chatter 实例（需要支持 ``create_request``）。
        logger: 上下文 logger。
        unreads_text: 已格式化为人类可读的未读消息块。
        unread_msgs: 原始未读消息列表（用于关键词命中）。
        chat_stream: 当前流。
        config: 可选 :class:`SubAgentConfig`，由插件在启动时注入；缺省时
            使用模块默认行为（与 dfc 完全一致）。

    Returns:
        ``SubAgentDecision`` 字典。
    """

    cfg = config or _DEFAULT_SUB_AGENT_CONFIG

    # 0) 整体过滤器关闭：每条都直通响应。
    if not cfg.enabled:
        return {"reason": "sub-agent 已禁用，直接响应", "should_respond": True}

    # 1) 私聊场景：直接放行（与 dfc 一致；一对一对话不需要过滤）。
    if str(chat_stream.chat_type).lower() == "private":
        return {"reason": "私聊场景跳过 sub-agent，直接响应", "should_respond": True}

    # 2) 概率门（仅当 enable_programmatic_controller 开启时启用，与 dfc 一致）。
    if cfg.enable_programmatic_controller:
        bypass_probability, bypass_reason = compute_bypass_probability(unread_msgs, chat_stream)
        if random.random() < bypass_probability:
            return {
                "reason": f"概率直通响应：{bypass_reason}",
                "should_respond": True,
            }

    # 3) 走 sub_actor LLM 判定
    return await _decide_via_llm(
        chatter=chatter,
        logger=logger,
        unreads_text=unreads_text,
        chat_stream=chat_stream,
    )


__all__ = [
    "VOICE_CHATTER_SUB_AGENT_PROMPT_TEMPLATE",
    "SubAgentConfig",
    "SubAgentDecision",
    "compute_bypass_probability",
    "mark_reply_success",
    "should_respond",
]
