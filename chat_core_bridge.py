"""Anima Chatter 与可复用聊天核心之间的结构化协议桥接。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol, TypedDict


class SubAgentDecision(TypedDict):
    """注意力决策结果。"""

    should_respond: bool
    reason: str


class PlainTextResponseHandling(TypedDict):
    """模型直接输出文本时的处理策略。"""

    action: Literal["retry", "wait", "stop"]
    reminder_text: str


class SessionLike(Protocol):
    """可复用聊天会话的最小执行协议。"""

    def execute(self) -> Any:
        """返回会话异步生成器。"""
        ...


class ChatCoreServiceLike(Protocol):
    """Default Chatter chat_core Service 的最小公开形状。"""

    def create_session(
        self,
        *,
        stream_id: str,
        options: AnimaSessionOptions,
        adapters: AnimaSessionAdapters,
    ) -> SessionLike:
        """创建聊天会话。"""
        ...


@dataclass(slots=True)
class AnimaSessionAdapters:
    """传递给 chat_core 的运行时适配器集合。

    该结构按字段协议与 chat_core 对齐，不导入其他插件的实现类型。
    """

    request_adapter: Any
    prompt_adapter: Any
    unread_adapter: Any
    usable_adapter: Any
    tool_execution_adapter: Any
    sub_agent_adapter: Any
    logger_adapter: Any
    plain_text_adapter: Any | None = None
    stream_event_observer: Callable[..., Awaitable[None]] | None = None


@dataclass(slots=True)
class AnimaSessionOptions:
    """Anima 运行 chat_core 时使用的会话选项。"""

    actor_task_name: str = "actor"
    sub_actor_task_name: str = "sub_actor"
    enable_cooldown: bool = False
    enable_action_suspend: bool = True
    enable_programmatic_controller: bool = True
    enable_sub_agent_collaboration: bool = False
    enable_stop_direct_message_wake: bool = False
    stop_direct_message_wake_probability: float = 0.0
    native_multimodal: bool = False
    theme_guide: dict[str, str] = field(default_factory=dict)
    negative_behavior_reinforcement: bool = False
    enable_llm_stream: bool = False
    filter_mode: str = "sub_only"
    enable_sub_agent_context: bool = False
    sub_agent_context_history_limit: int = 0
    sub_agent_decision_history_limit: int = 3


__all__ = [
    "AnimaSessionAdapters",
    "AnimaSessionOptions",
    "ChatCoreServiceLike",
    "PlainTextResponseHandling",
    "SubAgentDecision",
]
