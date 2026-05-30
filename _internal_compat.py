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
]
