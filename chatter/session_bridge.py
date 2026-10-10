"""与 NDFC（``neo_default_chatter:service:chat_core``）之间的协议桥接。

按**字段协议**与 NDFC 的 chat_core Service 对齐，不 import 其它插件的实现类型
——插件之间的依赖只能通过公开签名 / Service / 协议边界建立。

NDFC 的 ``create_session(stream_id, plugin=None)`` 不接受 adapters/options，
会话行为自包含；anima 的差异化逻辑通过订阅 ``neo_default_chatter:*`` 事件
（见 :mod:`.ndfc_handlers`）实现。本模块只保留：会话执行协议（:class:`SessionLike`）、
Service 最小公开形状（:class:`ChatCoreServiceLike`），以及两个为测试 / 文档保留的
选项与适配器 dataclass（anima 不再把它们传给 chat_core，仅作类型说明）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict


__all__ = [
    "AnimaSessionAdapters",
    "AnimaSessionOptions",
    "ChatCoreServiceLike",
    "PlainTextResponseHandling",
    "SessionLike",
]


class PlainTextResponseHandling(TypedDict):
    """模型不调工具直接吐纯文本时的处理策略。

    Attributes:
        action: ``"retry"`` 提醒后重试 / ``"wait"`` 直接等待用户 / ``"stop"`` 终止。
        reminder_text: ``action == "retry"`` 时注入给模型的提醒文本。
    """

    action: Literal["retry", "wait", "stop"]
    reminder_text: str


class SessionLike(Protocol):
    """可复用聊天会话的最小执行协议。"""

    def execute(self) -> Any:
        """返回会话异步生成器。"""
        ...


class ChatCoreServiceLike(Protocol):
    """NDFC chat_core Service 的最小公开形状。"""

    def create_session(
        self,
        *,
        stream_id: str,
        plugin: Any | None = None,
    ) -> SessionLike:
        """创建聊天会话。

        Args:
            stream_id: 会话所属聊天流。
            plugin: 可选插件实例；为 None 时回退到 Service 所属插件。

        Returns:
            可执行的会话对象。
        """
        ...


@dataclass(slots=True)
class AnimaSessionAdapters:
    """anima_chatter 各 adapter 能力的占位集合（类型说明用）。

    anima 不再把这些 adapter 传给 chat_core；差异化逻辑改由 :mod:`.ndfc_handlers`
    转发到 :class:`AnimaChatter` 的对应方法。本类型保留字段，供单元测试校验
    字段完整性。
    """

    request_adapter: Any
    prompt_adapter: Any
    unread_adapter: Any
    usable_adapter: Any
    tool_execution_adapter: Any
    sub_agent_adapter: Any
    logger_adapter: Any
    plain_text_adapter: Any | None = None
    stream_event_observer: Any | None = None


@dataclass(slots=True)
class AnimaSessionOptions:
    """anima_chatter 会话选项（类型说明用）。

    默认值已按 anima 的场景调整——关掉了对话冷却、子代理协作、原生多模态与
    stop 直接唤醒等不适用的特性。anima 不再把这些选项传给 chat_core，保留
    字段供单元测试校验。
    """

    actor_task_name: str = "actor"
    """主动作模型的任务名。"""

    sub_actor_task_name: str = "sub_actor"
    """决策模型的任务名。"""

    enable_cooldown: bool = False
    """是否启用对话冷却。anima 没有这个概念，固定关闭。"""

    enable_action_suspend: bool = True
    """纯 Action 回合是否挂起等待用户。"""

    enable_programmatic_controller: bool = True
    """是否启用程序化控制器（概率门由 anima 自己的 sub_agent 处理）。"""

    enable_sub_agent_collaboration: bool = False
    """是否启用子代理协作。"""

    enable_stop_direct_message_wake: bool = False
    """是否允许 stop 后被直接消息唤醒。"""

    stop_direct_message_wake_probability: float = 0.0
    """上述唤醒的概率。"""

    native_multimodal: bool = False
    """是否使用原生多模态输入。anima 的图片描述由外层 VLM 处理。"""

    theme_guide: dict[str, str] = field(default_factory=dict)
    """主题引导文案映射。"""

    negative_behavior_reinforcement: bool = False
    """是否由 chat_core 注入负面行为提醒。anima 自己在 user prompt 末尾注入。"""

    enable_llm_stream: bool = False
    """是否启用 LLM 流式输出。"""

    filter_mode: str = "sub_only"
    """未读过滤模式。"""

    enable_sub_agent_context: bool = False
    """是否给决策模型附加上下文。"""

    sub_agent_context_history_limit: int = 0
    """决策上下文的历史条数上限。"""

    sub_agent_decision_history_limit: int = 3
    """决策历史的保留条数。"""
