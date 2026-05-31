"""anima_chatter 与框架内部模块的兼容层。

本模块**唯一目的**是把所有"必须 import 框架内部模块（即 ``src.kernel`` /
``src.core`` 而非 ``src.app.plugin_system.api``）"的语句集中到一个文件里，
让违规面尽可能小、未来公开 API 补齐时只需要改这一处。

按规范，插件应仅通过 [`src.app.plugin_system.api.*`](../../../src/app/plugin_system/api/) 访问框架；
但 anima_chatter 当前用的几个能力（重启 stream loop / 喂 watchdog /
派 background task / 反查 plugin 实例 / 注入 history Message）暂未公开。
等公开 API 上线后，把这里的实现替换为对应公开调用即可。

每个函数在 docstring 顶部都用 TODO 标注了对应缺失的公开 API。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.models.message import Message


# ── 重启 stream 循环（vtb on/off, voice_call 切换） ─────────
async def restart_stream_loop(stream_id: str) -> None:
    """重启目标 stream 的 chatter 循环，让缓存的旧 chatter 生成器作废。

    TODO(framework): 等 ``chat_api.restart_stream_loop`` 公开后切回去。

    StreamLoopManager 会把 ``chatter.execute()`` 返回的异步生成器缓存到
    ``_chatter_genes``。直接换 chatter 实例**不会**让下一 tick 用上新
    chatter——旧生成器还在被 ``asend`` 推进。重启循环就是清那一份缓存。
    """

    from src.core.transport.distribution.stream_loop_manager import (
        get_stream_loop_manager,
    )

    await get_stream_loop_manager().restart_stream_loop(stream_id)


# ── 喂 watchdog（heartbeat 后台任务） ─────────────────────
def feed_watchdog(stream_id: str) -> None:
    """同步喂一次 stream 的 watchdog。

    TODO(framework): 等 ``chat_api.feed_watchdog(stream_id)`` 公开后切回去。

    [`heartbeat.py`](heartbeat.py:1) 的后台喂狗循环用此函数，避免长时间 await
    导致框架的 stream_warning_threshold / stream_restart_threshold 误触发。
    """

    from src.kernel.concurrency import get_watchdog

    get_watchdog().feed_dog(stream_id)


# ── task_manager（VTS 后台动画/参数发送循环） ─────────────
def create_background_task(coro: Any, *, name: str) -> Any:
    """通过框架 task_manager 派发一个后台任务，并返回其 handle。

    TODO(framework): 等 ``task_api.create_task`` 公开后切回去。

    VTS 长连接的 ``_heartbeat_loop`` / ``_animation_loop`` /
    ``_param_sender_loop`` 需要一个不被自动 GC 的后台任务派发器。
    用框架自带的 task_manager 而不是 ``asyncio.create_task`` 是规范要求。
    """

    from src.kernel.concurrency import get_task_manager

    return get_task_manager().create_task(coro, name=name)


# ── plugin 反查（sing_song.to_schema 拿 song_library） ────
def get_anima_chatter_plugin() -> Any | None:
    """反查 anima_chatter 插件实例。

    TODO(framework): 等 ``plugin_api.get_plugin_by_name`` 公开后切回去。

    [`actions/sing_song.py`](actions/sing_song.py:1) 的 ``to_schema`` 是
    ``classmethod``，没有 ``self.plugin`` 上下文，必须从 plugin_manager
    反查。失败时返回 ``None``，调用方需自行容错。
    """

    try:
        from src.core.managers import get_plugin_manager

        return get_plugin_manager().get_plugin("anima_chatter")
    except Exception:  # noqa: BLE001
        return None


# ── 主动唤醒 stream loop 的 Wait 状态 ─────────────────────
def wake_stream_from_wait(stream_id: str, *, only_if_new_unreads: bool = True) -> bool:
    """让 stream loop 解除当前的 ``Wait()`` 状态并立即推进 chatter。

    TODO(framework): 等 ``chat_api.wake_stream`` 公开后切回去。

    背景：dfc Session 在 ``yield Wait(None)`` 时框架会记录
    ``unread_count_at_yield``。后续只有 ``unread_count_now > unread_count_at_yield``
    才解除 wait。但流水线模式下：
    - Action 立即返回 → yield Wait(None) 时 unread 已经包含未处理消息
    - 没有新弹幕进来 → 永不解除

    本函数主动写入 ``_pending_wait_resume_events``，让
    [`stream_loop_manager._wait_state_check`](src/core/transport/distribution/stream_loop_manager.py:530)
    在下次 tick 立刻判定 "已有 pending event"，pop wait_state 并解除。

    用 ``source="message"`` 注入——这样 dfc Session 收到 resume_event 时**不**
    会走"主动构造 reminder_text 进 MODEL_TURN"那条路径（那条只对
    ``timer / sub_agent`` source 生效），而是回到正常的 unread 检查流程：
    没有新弹幕就直接 ``yield Wait()`` 继续等。

    Args:
        stream_id: 目标 stream。
        only_if_new_unreads: 仅当 stream 有新于 wait 时刻的未读消息时才唤醒。
            ``True`` 防止唤醒后没有新弹幕导致 LLM 主动发起对话；``False`` 强
            制唤醒。默认 ``True``。

    Returns:
        bool: ``True`` 表示成功注入；``False`` 表示当前没有 wait 状态可解除
            或没有新未读消息（此时本函数为 no-op）。
    """

    try:
        from src.core.components.base import WaitResumeEvent
        from src.core.transport.distribution.stream_loop_manager import (
            get_stream_loop_manager,
        )

        slm = get_stream_loop_manager()
        # 没有 wait_state 就别注入——避免把"正常推进中"的 chatter 干扰。
        wait_state = slm._wait_states.get(stream_id)  # noqa: SLF001
        if wait_state is None:
            return False
        # 已经有 pending event 就别叠加
        if stream_id in slm._pending_wait_resume_events:  # noqa: SLF001
            return False

        # only_if_new_unreads：检查 wait 时刻 unread_count 是否被新增
        if only_if_new_unreads:
            _, _, unread_count_at_yield = wait_state
            try:
                from src.core.managers.stream_manager import get_stream_manager

                ctx = get_stream_manager()._streams.get(stream_id)  # noqa: SLF001
                if ctx is None:
                    return False
                # ctx 是 ChatStream；context.unread_messages 是当前快照
                unread_count_now = len(ctx.context.unread_messages)
                if unread_count_now <= unread_count_at_yield:
                    return False
            except Exception:  # noqa: BLE001
                return False

        slm._pending_wait_resume_events[stream_id] = WaitResumeEvent(  # noqa: SLF001
            source="message",
            wait_time=None,
            unread_count=0,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# ── 构造 NOTICE / TEXT 类 Message 注入到历史 ──────────────
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
    """构造一条 ``NOTICE`` 类型的 Message，用于注入历史边界。

    TODO(framework): 等 ``message_api.create_notice_message`` 公开后切回去。

    [`actions/voice_call.py`](actions/voice_call.py:1) 在通话开始 / 结束时会
    向 history_messages 注入这种"系统标注"消息——给 DFC 等无状态 chatter
    在切回时看到通话边界，不必修改 DFC 源码。
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


__all__ = [
    "build_notice_message",
    "create_background_task",
    "feed_watchdog",
    "get_anima_chatter_plugin",
    "restart_stream_loop",
    "wake_stream_from_wait",
]
