"""anima_chatter 通话状态中枢。

维护 **同时只允许一个** 进行中的语音通话状态：

- 谁发起的（``caller_stream_id``）
- 起讫时间（``started_at`` / 静默超时）
- 通话前接管该 stream 的 chatter 签名（供订阅方判断"这是不是我负责的 stream"）
- 通话期间收发的消息（用于通话结束后桥接历史）

为什么用模块级单例而不是 Service：本插件内多个模块需要读写状态，而
``ServiceManager.get_service()`` 每次新建实例，存状态会出问题；模块级单例 +
``asyncio.Lock`` 是框架内多处验证过的稳妥模式。

并发安全：所有公共协程都先拿 :data:`_lock`；唯一的同步读接口
:func:`snapshot_active_call_unlocked` 依赖 CPython 单变量读的原子性。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Literal, TypedDict


__all__ = [
    "ActiveCall",
    "CallEndReason",
    "CallMessage",
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "clear_active_call",
    "end_call",
    "get_active_call",
    "get_remaining_seconds",
    "is_call_active_for_stream",
    "record_assistant_message",
    "record_system_note",
    "record_user_message",
    "set_active_call",
    "snapshot_active_call_unlocked",
]


DEFAULT_CALL_TIMEOUT_SECONDS: float = 300.0
"""默认**静默**超时（秒）。超过这么久双方都没动静就自动挂断。"""


CallEndReason = Literal["model", "user", "timeout", "manual", "plugin_unload"]
"""通话结束原因。用于事件 payload 与结束标注文案。"""


class CallMessage(TypedDict):
    """通话期间记录的单条消息。

    字段与订阅方（如 kokoro_flow_chatter 的通话历史 handler）的链式条目对齐，
    可直接转换写回对话历史。

    Attributes:
        role: ``"user"`` / ``"assistant"`` / ``"system"``。
        text: 消息正文。
        ts: Unix 时间戳。
    """

    role: str
    text: str
    ts: float


@dataclass(slots=True)
class ActiveCall:
    """单次进行中通话的全部状态。"""

    caller_stream_id: str
    """通话发起方的 stream_id。"""

    started_at: float
    """通话开始的 Unix 时间戳。"""

    last_activity_at: float = 0.0
    """最近一次活跃事件的 Unix 时间戳（任意一方发声 / 发字 / 系统标注都算）。

    超时基于"上次活跃后经过的时间"，**不是**"从通话开始经过的时间"——只要双方
    在持续聊天，通话不会因总时长被强制挂断。
    """

    timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS
    """**静默**超时时长（秒）。"""

    previous_chatter_signature: str = ""
    """通话开始前接管该 stream 的 chatter 组件签名；没有则为空串。"""

    messages_in_call: list[CallMessage] = field(default_factory=list)
    """通话期间产生的所有消息，按发生顺序排列。"""


# ── 模块级单例 ─────────────────────────────────────
_active_call: ActiveCall | None = None
_lock = asyncio.Lock()

# user 消息去重的时间窗（秒）。同 text 且时间戳差在此窗内视为重复。
_DEDUP_WINDOW_SECONDS = 0.5


async def get_active_call() -> ActiveCall | None:
    """返回当前进行中的通话。

    Returns:
        ``ActiveCall`` 实例；没有通话时返回 ``None``。
    """

    async with _lock:
        return _active_call


def snapshot_active_call_unlocked() -> ActiveCall | None:
    """**不加锁**地读取当前 ``ActiveCall`` 的快照。

    供 :func:`..modes.resolve_mode` 与 prompt 构建这类**同步路径**使用——它们
    不能 ``await``，但需要看到通话状态来决定模式 / prompt。

    在 CPython 下读模块级单变量是原子操作，与写路径（必须拿 :data:`_lock`）
    不会出现"读到半个实例"的中间态，最多读到刚被替换前的旧值；"判定 + 立即用"
    的场景能容忍一拍延迟。

    Returns:
        当前 ``ActiveCall`` 引用；没有通话返回 ``None``。**不要**修改返回值。
    """

    return _active_call


async def is_call_active_for_stream(stream_id: str) -> bool:
    """判定某个 stream 是否正处于通话中。

    Args:
        stream_id: 待判定的聊天流 ID。

    Returns:
        该 stream 是通话发起方时返回 ``True``。
    """

    async with _lock:
        return _active_call is not None and _active_call.caller_stream_id == stream_id


async def set_active_call(
    caller_stream_id: str,
    *,
    previous_chatter_signature: str = "",
    timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
) -> ActiveCall:
    """开始一次通话。

    Args:
        caller_stream_id: 通话发起方的 stream_id。
        previous_chatter_signature: 通话前接管该 stream 的 chatter 签名。
        timeout_seconds: 静默超时时长（秒），下限 1 秒。

    Returns:
        新建的 ``ActiveCall``。

    Raises:
        RuntimeError: 已有通话进行中（同时只允许一个）。调用方应先调
            :func:`get_active_call` 判断，不要靠异常做控制流。
    """

    global _active_call
    async with _lock:
        if _active_call is not None:
            raise RuntimeError(
                f"已有进行中的通话 stream={_active_call.caller_stream_id}，"
                f"无法同时开启第二个"
            )
        now = time.time()
        _active_call = ActiveCall(
            caller_stream_id=caller_stream_id,
            started_at=now,
            last_activity_at=now,
            timeout_seconds=max(1.0, float(timeout_seconds)),
            previous_chatter_signature=previous_chatter_signature,
        )
        return _active_call


async def end_call(reason: CallEndReason) -> ActiveCall | None:
    """结束当前通话并返回被清掉的 ``ActiveCall`` 快照。

    本函数**只**清状态，不做副作用——不发事件、不动 chatter、不停 ASR。那些
    由 :func:`..voice_call.lifecycle.finalize_call` 统一编排。

    Args:
        reason: 结束原因；本模块不持有它，由调用方放进事件 payload。

    Returns:
        通话快照（含 ``messages_in_call``）；没有活跃通话时返回 ``None``（幂等）。
    """

    _ = reason
    global _active_call
    async with _lock:
        snapshot = _active_call
        _active_call = None
        return snapshot


async def clear_active_call() -> None:
    """无条件清状态，不返回快照。

    与 :func:`end_call` 的区别：调用方不需要通话期间的消息。仅在启动通话失败
    回滚、异常恢复等场景使用。
    """

    global _active_call
    async with _lock:
        _active_call = None


async def get_remaining_seconds() -> float | None:
    """返回当前通话剩余**静默**秒数。

    基于 ``last_activity_at`` 计算："如果接下来 X 秒都没人说话，就会被挂断"。
    任何活跃事件都会通过 ``record_*`` 刷新 ``last_activity_at``，实质上续期。

    Returns:
        剩余秒数（可为负，表示已超时）；没有通话时返回 ``None``。
    """

    async with _lock:
        if _active_call is None:
            return None
        idle_for = time.time() - _active_call.last_activity_at
        return _active_call.timeout_seconds - idle_for


def _is_duplicate_user_message(
    call: ActiveCall,
    text: str,
    ts: float | None,
    normalized_ts: float,
) -> bool:
    """判断是否与已记录的 user 消息重复。

    同一批 unread 在 chatter 主循环重试 / 续轮时可能被多次返回，若不去重会让
    通话稿出现重复条目。按 ``text`` + ``ts`` 组合判定；``ts`` 缺失时退化为仅按
    ``text`` 判定。

    Args:
        call: 当前通话状态。
        text: 待记录的消息正文。
        ts: 调用方给的原始时间戳（可能为 ``None``）。
        normalized_ts: 归一化后的时间戳。

    Returns:
        是重复消息时返回 ``True``。
    """

    for existing in call.messages_in_call:
        if existing["role"] != "user" or existing["text"] != text:
            continue
        if ts is None:
            return True
        if abs(existing["ts"] - normalized_ts) < _DEDUP_WINDOW_SECONDS:
            return True
    return False


async def record_user_message(
    stream_id: str,
    text: str,
    *,
    ts: float | None = None,
) -> None:
    """通话期间记录一条用户消息（来自 ASR 或平台文字）。

    ``stream_id`` 与当前通话不匹配时静默丢弃（防御调用方传错）。重复消息只刷新
    活跃时间不重复记录，详见 :func:`_is_duplicate_user_message`。

    Args:
        stream_id: 消息所属聊天流。
        text: 消息正文。
        ts: 消息的 Unix 时间戳；``None`` 时取当前时间。
    """

    async with _lock:
        if _active_call is None or _active_call.caller_stream_id != stream_id:
            return
        now = time.time()
        normalized_ts = float(ts) if ts is not None else now

        if _is_duplicate_user_message(_active_call, text, ts, normalized_ts):
            _active_call.last_activity_at = now
            return

        _active_call.messages_in_call.append(
            CallMessage(role="user", text=text, ts=normalized_ts)
        )
        _active_call.last_activity_at = now


async def record_assistant_message(stream_id: str, text: str) -> None:
    """通话期间记录一条 bot 回复。

    bot 主动发声也算活跃，会刷新 ``last_activity_at``，避免单方独白被误判超时。

    Args:
        stream_id: 消息所属聊天流。
        text: 回复正文。
    """

    async with _lock:
        if _active_call is None or _active_call.caller_stream_id != stream_id:
            return
        now = time.time()
        _active_call.messages_in_call.append(
            CallMessage(role="assistant", text=text, ts=now)
        )
        _active_call.last_activity_at = now


async def record_system_note(stream_id: str, text: str) -> None:
    """通话期间记录一条系统标注（如"通话开始 / 结束"等元事件）。

    与 user / assistant 消息的区别是 ``role == "system"``，下游会把它渲染成
    状态描述而非对话发言。系统标注同样刷新活跃时间——通话起讫这种关键节点
    不应被当成"静默"。

    Args:
        stream_id: 消息所属聊天流。
        text: 标注正文。
    """

    async with _lock:
        if _active_call is None or _active_call.caller_stream_id != stream_id:
            return
        now = time.time()
        _active_call.messages_in_call.append(
            CallMessage(role="system", text=text, ts=now)
        )
        _active_call.last_activity_at = now
