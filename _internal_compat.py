"""anima_chatter 与框架内部模块的**唯一**兼容层。

本模块是插件内**允许** ``import src.core.* / src.kernel.*`` 的唯一位置。
其余任何模块只能通过 [`src.app.plugin_system.api`](../../src/app/plugin_system/api/)
/ [`src.app.plugin_system.base`](../../src/app/plugin_system/base.py) /
[`src.app.plugin_system.types`](../../src/app/plugin_system/types.py) 访问框架，
或者调用本模块导出的函数。

按规范，插件应仅通过公开 API 访问框架；但 anima_chatter 用到的下列能力当前
尚未公开，只能在这里做集中收敛，等公开 API 补齐后替换实现即可：

============================== ===============================================
能力                            当前实现依赖
============================== ===============================================
重启 stream loop                ``src.core.transport.distribution``
主动唤醒 Wait 状态              ``src.core.transport.distribution``（私有属性）
喂 watchdog                     ``src.kernel.concurrency.get_watchdog``
派发后台任务                    ``src.kernel.concurrency.get_task_manager``
构造 NOTICE Message             ``src.core.models.message``
读取全局人设配置                ``src.core.config.get_core_config``
prompt 渲染策略 / bucket 前缀   ``src.core.prompt``
LLM 上下文压缩默认 handler      ``src.core.utils.context_compression``
============================== ===============================================

风险提示：:func:`wake_stream_from_wait` 访问了 ``StreamLoopManager`` /
``StreamManager`` 的私有属性，是本插件对框架耦合最深的一处。失败时会记录
WARNING 日志而**不是**静默返回，确保框架重构导致的失效可被及时发现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from src.core.models.message import Message


logger = get_logger("anima_chatter.compat")


__all__ = [
    "BackgroundTaskHandle",
    "build_notice_message",
    "cancel_background_task",
    "create_background_task",
    "default_context_compression_handler",
    "feed_watchdog",
    "get_anima_chatter_plugin",
    "get_personality",
    "get_prompt_manager",
    "prompt_min_len",
    "prompt_optional",
    "prompt_wrap",
    "restart_stream_loop",
    "stream_reminder_bucket",
    "wake_stream_from_wait",
]


# ── 后台任务 ───────────────────────────────────────────────

BackgroundTaskHandle = Any
"""``task_manager.create_task`` 返回的任务句柄（含 ``task`` 与 ``task_id``）。"""


def create_background_task(
    coro: "Coroutine[Any, Any, Any]",
    *,
    name: str,
    metadata: dict[str, Any] | None = None,
) -> BackgroundTaskHandle:
    """通过框架 task_manager 派发守护任务并返回任务句柄。

    当前框架公开 API 尚未提供 ``task_api.create_task``。

    Args:
        coro: 待执行的协程。
        name: 任务名（出现在 task_manager 的诊断信息里）。
        metadata: 附加元数据；``plugin`` 字段会被自动填成 ``anima_chatter``。

    Returns:
        任务句柄；可传给 :func:`cancel_background_task` 取消。
    """

    from src.kernel.concurrency import get_task_manager

    return get_task_manager().create_task(
        coro,
        name=name,
        daemon=True,
        metadata={"plugin": "anima_chatter", **(metadata or {})},
    )


def cancel_background_task(handle: BackgroundTaskHandle | None) -> None:
    """取消 :func:`create_background_task` 派发的任务；``None`` 时为 no-op。

    Args:
        handle: 任务句柄。
    """

    if handle is None:
        return

    from src.kernel.concurrency import get_task_manager

    get_task_manager().cancel_task(handle.task_id)


# ── watchdog ──────────────────────────────────────────────


def feed_watchdog(stream_id: str) -> None:
    """同步喂一次 stream 的 watchdog。

    当前框架公开 API 尚未提供 ``chat_api.feed_watchdog(stream_id)``。
    [`runtime/heartbeat.py`](runtime/heartbeat.py:1) 的后台喂狗循环用此函数，
    避免长时间 ``await`` 触发 stream 重启阈值。

    Args:
        stream_id: 目标聊天流 ID。
    """

    from src.kernel.concurrency import get_watchdog

    get_watchdog().feed_dog(stream_id)


# ── stream loop 控制 ───────────────────────────────────────


async def restart_stream_loop(stream_id: str) -> None:
    """重启目标 stream 的 chatter 循环，让缓存的旧 chatter 生成器作废。

    当前框架公开 API 尚未提供 ``chat_api.restart_stream_loop``。

    ``StreamLoopManager`` 会缓存 ``chatter.execute()`` 返回的异步生成器。直接
    换 chatter 实例**不会**让下一 tick 用上新 chatter——旧生成器仍在被 ``asend``
    推进。重启循环即清掉这份缓存。

    Args:
        stream_id: 目标聊天流 ID。
    """

    from src.core.transport.distribution.stream_loop_manager import (
        get_stream_loop_manager,
    )

    await get_stream_loop_manager().restart_stream_loop(stream_id)


def wake_stream_from_wait(stream_id: str, *, only_if_new_unreads: bool = True) -> bool:
    """让 stream loop 解除当前的 ``Wait()`` 状态并立即推进 chatter。

    当前框架公开 API 尚未提供 ``chat_api.wake_stream``。

    背景：chat_core Session 在 ``yield Wait(None)`` 时框架会记录
    ``unread_count_at_yield``，之后只有 ``unread_count_now >
    unread_count_at_yield`` 才解除 wait。但 vtb_live 流水线模式下 Action 提前
    返回，``yield Wait(None)`` 时 unread 已经包含未处理消息，没有新弹幕就永不
    解除。本函数主动写入 pending resume event 打破该僵局。

    用 ``source="message"`` 注入——这样 Session 收到 resume_event 时**不**会走
    "主动构造 reminder_text 进 MODEL_TURN" 那条路径（只对 ``timer`` /
    ``sub_agent`` 生效），而是回到正常的 unread 检查流程。

    Args:
        stream_id: 目标 stream。
        only_if_new_unreads: 仅当 stream 有新于 wait 时刻的未读消息时才唤醒。
            ``True`` 防止唤醒后没有新弹幕导致 LLM 主动发起对话。

    Returns:
        ``True`` 表示成功注入；``False`` 表示当前无 wait 状态可解除、无新未读，
        或框架内部结构已变更（后者会记 WARNING）。
    """

    try:
        from src.core.components.base import WaitResumeEvent
        from src.core.managers.stream_manager import get_stream_manager
        from src.core.transport.distribution.stream_loop_manager import (
            get_stream_loop_manager,
        )

        loop_manager = get_stream_loop_manager()
        # 没有 wait_state 就别注入——避免干扰"正常推进中"的 chatter。
        wait_state = loop_manager._wait_states.get(stream_id)  # noqa: SLF001
        if wait_state is None:
            return False
        # 已有 pending event 就别叠加。
        if stream_id in loop_manager._pending_wait_resume_events:  # noqa: SLF001
            return False

        if only_if_new_unreads:
            _, _, unread_count_at_yield = wait_state
            chat_stream = get_stream_manager()._streams.get(stream_id)  # noqa: SLF001
            if chat_stream is None:
                return False
            if len(chat_stream.context.unread_messages) <= unread_count_at_yield:
                return False

        loop_manager._pending_wait_resume_events[stream_id] = WaitResumeEvent(  # noqa: SLF001
            source="message",
            wait_time=None,
            unread_count=0,
        )
        return True
    except (AttributeError, ImportError, KeyError, TypeError) as exc:
        # 框架内部结构变更会走到这里。必须让它可见——静默失败的表现是
        # "直播时 bot 突然不再响应弹幕"，排查成本极高。
        logger.warning(
            f"唤醒 stream 失败（框架内部结构可能已变更）stream={stream_id}: {exc}",
            exc_info=True,
        )
        return False


# ── Message 构造 ───────────────────────────────────────────


def build_notice_message(
    *,
    message_id: str,
    content: str,
    platform: str,
    stream_id: str,
    sender_id: str = "system",
    sender_name: str = "系统通知",
    time: float | None = None,
) -> "Message":
    """构造一条 ``NOTICE`` 类型的 Message，用于向历史注入语境边界。

    当前框架公开 API 尚未提供 ``message_api.create_notice_message``。
    [`voice_call/lifecycle.py`](voice_call/lifecycle.py:1) 在通话开始 / 结束时
    用它写入"系统标注"，让切回来的其他 chatter 能看到通话边界。

    Args:
        message_id: 消息 ID。
        content: 标注正文。
        platform: 平台标识。
        stream_id: 所属聊天流。
        sender_id: 发送方 ID，默认 ``"system"``。
        sender_name: 发送方显示名，默认 ``"系统通知"``。
        time: Unix 时间戳；``None`` 时由框架填充。

    Returns:
        构造好的 ``NOTICE`` 类型 Message。
    """

    from src.core.models.message import Message, MessageType

    return Message(
        message_id=message_id,
        content=content,
        processed_plain_text=content,
        message_type=MessageType.NOTICE,
        platform=platform,
        stream_id=stream_id,
        sender_id=sender_id,
        sender_name=sender_name,
        time=time,
    )


# ── 全局配置 ───────────────────────────────────────────────


def get_personality() -> Any:
    """返回全局人设配置（``core.toml`` 的 ``[personality]`` 段）。

    当前框架公开 API 中 ``config_api`` 只负责插件自身配置，读取全局人设仍需
    走 ``src.core.config.get_core_config()``。

    Returns:
        ``PersonalitySection`` 实例（含 nickname / alias_names / identity 等）。
    """

    from src.core.config import get_core_config

    return get_core_config().personality


def default_context_compression_handler() -> Any:
    """返回框架默认的 LLM 上下文压缩 handler。

    ``LLMContextManager`` 需要它才能在上下文超长时自动压缩历史。当前该 handler
    未通过 ``llm_api`` 暴露。

    Returns:
        可直接传给 ``LLMContextManager(context_compression_handler=...)`` 的对象。
    """

    from src.core.utils.context_compression import (
        default_chat_context_compression_handler,
    )

    return default_chat_context_compression_handler


# ── prompt 渲染策略 ────────────────────────────────────────
# ``prompt_api`` 暴露了模板注册 / 检索，但没有暴露渲染策略工厂
# （optional / min_len / wrap）与流私有 bucket 前缀，这里补齐。


def get_prompt_manager() -> Any:
    """返回全局 PromptManager。

    ``prompt_api.get_or_create`` 只能建模板，注册时还需要拿 manager 做别的
    操作，统一从这里取。

    Returns:
        ``PromptManager`` 单例。
    """

    from src.core.prompt import get_prompt_manager as _get

    return _get()


def prompt_optional(default: str) -> Any:
    """构造"缺省值"渲染策略。

    Args:
        default: 占位符为空时使用的兜底文本。

    Returns:
        ``RenderPolicy`` 实例。
    """

    from src.core.prompt import optional

    return optional(default)


def prompt_min_len(length: int) -> Any:
    """构造"最短长度"渲染策略（不足则整段丢弃）。

    Args:
        length: 最短字符数。

    Returns:
        ``RenderPolicy`` 实例。
    """

    from src.core.prompt import min_len

    return min_len(length)


def prompt_wrap(prefix: str, suffix: str) -> Any:
    """构造"前后包裹"渲染策略。

    Args:
        prefix: 前缀文本。
        suffix: 后缀文本。

    Returns:
        ``RenderPolicy`` 实例。
    """

    from src.core.prompt import wrap

    return wrap(prefix, suffix)


def stream_reminder_bucket(stream_id: str, bucket: str) -> str:
    """拼出流私有的 SystemReminder bucket 名。

    与 ``BaseChatter.create_request`` 的行为对齐：除全局 bucket 外，还要注册
    ``stream:{stream_id}:{bucket}`` 这个流私有 bucket。

    Args:
        stream_id: 聊天流 ID。
        bucket: 基础 bucket 名（如 ``"actor"``）。

    Returns:
        流私有 bucket 全名。
    """

    from src.core.prompt import STREAM_BUCKET_PREFIX

    return f"{STREAM_BUCKET_PREFIX}{stream_id}:{bucket}"


# ── 插件实例反查 ───────────────────────────────────────────


def get_anima_chatter_plugin() -> Any | None:
    """通过公开插件 API 反查 anima_chatter 插件实例。

    ``to_schema()`` 是 classmethod，拿不到 ``self.plugin``，只能反查。

    Returns:
        ``AnimaChatterPlugin`` 实例；未加载时返回 ``None``。
    """

    from src.app.plugin_system.api import plugin_api

    try:
        return plugin_api.get_plugin("anima_chatter")
    except (RuntimeError, ValueError):
        return None
