"""anima_chatter 通话状态中枢。

维护 **同时只允许一个** 进行中的语音通话状态：

- 谁发起的（caller_stream_id）
- 起讫时间（started_at / 5min 超时）
- 之前接管这个 stream 的 chatter signature（用于 kfc 等通过事件
  hook 判断"这是不是我负责的 stream"）
- 通话期间收发的消息（用于通话结束后桥接历史）

为什么用模块级单例而不是 Service：
- 简单——本插件内 4~5 个文件需要读写状态，写成 Service 反而增加抽象层。
- Service 不是单例（``ServiceManager.get_service()`` 每次新建实例），
  存状态会出问题；用模块级单例 + 锁是 Neo-MoFox 内多次出现过的稳妥模式
  （asr_adapter / event_manager 的 ``_redirect_target`` / ``_PROVIDERS``
  都走这一套）。

并发安全：
- 所有公共方法都先拿 ``_lock``。
- 5 分钟超时由 :func:`get_remaining_seconds` 暴露给 runner 主循环检查
  （runner.py 在每次 tick 调一下）；超时时调 ``end_call("timeout")``。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


__all__ = [
    "ActiveCall",
    "CallEndReason",
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
"""默认通话超时（秒）。runner 主循环会按这个值检查 ``ActiveCall.started_at``。"""


CallEndReason = str
"""通话结束原因。约定：``"model"`` / ``"user"`` / ``"timeout"`` / ``"error"``。"""


@dataclass(slots=True)
class ActiveCall:
    """单次进行中通话的全部状态。"""

    caller_stream_id: str
    """通话发起方的 stream_id（QQ 私聊流 ID）。"""

    started_at: float
    """通话开始的 Unix 时间戳。"""

    last_activity_at: float = 0.0
    """最近一次活跃事件的 Unix 时间戳（任意一方发声 / 发字 / 系统标注都算）。

    超时基于"上次活跃后经过的时间"，**不是**"从通话开始经过的时间"——只要双方
    在持续聊天，通话不会因总时长被强制挂断；只有真正"安静下来 N 秒"才会挂。
    构造后由 :func:`set_active_call` 初始化为 ``started_at``。
    """

    timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS
    """**静默**超时时长（秒）。runner 检查 ``time.time() - last_activity_at >= timeout_seconds``
    时挂断——意为"双方安静这么久就当断了"。"""

    previous_chatter_signature: str = ""
    """通话开始前接管该 stream 的 chatter 组件签名。

    用于通话结束事件 payload，让 kfc 等 chatter 判断"这是不是我负责的 stream"。
    若开始前没有活跃 chatter（如默认 default_chatter 评分绑定），保持空串。
    """

    messages_in_call: list[Any] = field(default_factory=list)
    """通话期间产生的所有消息（user + assistant 顺序）。

    每条 entry 形如：
    ``{"role": "user"|"assistant", "text": str, "ts": float}``
    （故意与 kfc 的 ChainEntry 字段对齐，方便 voice_call_history_handler
    直接转 ChainEntry 写回 chain_payloads。）
    """


# ── 模块级单例 ─────────────────────────────────────
_active_call: ActiveCall | None = None
_lock = asyncio.Lock()


async def get_active_call() -> ActiveCall | None:
    """返回当前进行中的通话；没有则返回 None。"""

    async with _lock:
        return _active_call


def snapshot_active_call_unlocked() -> ActiveCall | None:
    """**不加锁**地读取当前 ActiveCall 的快照。

    供 :mod:`.modes` / :mod:`.prompts.builder` 这种**同步路径**使用——
    那两条调用链不能 ``await``，但又需要看到通话状态来决定模式 / prompt。

    在 CPython 下读模块级单变量是原子操作，与写路径（必须拿 :data:`_lock`）
    不会出现"读到半个 ActiveCall 实例"的中间态——最多读到刚被替换前的旧值，
    "判定 + 立即用"的场景能容忍一拍延迟。

    Returns:
        当前 ``ActiveCall`` 引用；没有通话返回 ``None``。绝**不**修改返回值，
        如要修改请走加锁的 :func:`set_active_call` / :func:`record_*_message`。
    """

    return _active_call


async def is_call_active_for_stream(stream_id: str) -> bool:
    """快速判定某个 stream 是否正处于通话中。

    anima_chatter.modes.resolve_mode 在优先级 1 判定时会调它——
    通话中的 stream 强制走 voice 模式，无视 platform。
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

    互斥：如果已有通话进行中，抛 :class:`RuntimeError`——同时只允许一个通话。
    调用方（``start_voice_call`` action）负责在调用前先调 :func:`get_active_call`
    判一下，给用户一个清晰的拒绝理由（不要靠异常做控制流）。
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
            last_activity_at=now,  # 起始时刻就是首次活跃
            timeout_seconds=max(1.0, float(timeout_seconds)),
            previous_chatter_signature=previous_chatter_signature,
        )
        return _active_call


async def end_call(reason: CallEndReason) -> ActiveCall | None:
    """结束当前通话并返回被清掉的 ``ActiveCall`` 快照。

    返回值：
    - 通话存在 → 返回快照（含 messages_in_call），调用方应基于此构造
      ``voice_call.ended`` 事件 payload。
    - 没有活跃通话 → 返回 None（幂等）。

    本函数**只**清状态，不做副作用——不发事件、不动 chatter、不停 ASR。
    那些都由 ``end_voice_call`` action 或 runner 超时处理路径串起来调。
    """

    _ = reason  # reason 由调用方放进事件 payload，本模块不持有
    global _active_call
    async with _lock:
        snapshot = _active_call
        _active_call = None
        return snapshot


async def clear_active_call() -> None:
    """无条件清状态（用于 ``/voice off`` 兜底命令）。

    与 :func:`end_call` 的区别：本函数不返回快照，调用方不需要拿到通话期间
    的消息。仅在异常恢复 / 用户强制兜底时使用。
    """

    global _active_call
    async with _lock:
        _active_call = None


async def get_remaining_seconds() -> float | None:
    """返回当前通话剩余**静默**秒数；没有通话返回 None。

    与"从通话开始算"的总时长不同——这里基于 ``last_activity_at`` 计算
    剩余时间："如果接下来 X 秒都没人说话，就会被挂断"。任何活跃事件
    （user / assistant 消息、系统标注）都会通过 :func:`record_*_message`
    刷新 ``last_activity_at``，实质上"续期"通话。

    runner 主循环每 tick 调一次：``<= 0`` 即视为超时。
    """

    async with _lock:
        if _active_call is None:
            return None
        idle_for = time.time() - _active_call.last_activity_at
        return _active_call.timeout_seconds - idle_for


async def record_user_message(stream_id: str, text: str, *, ts: float | None = None) -> None:
    """通话期间记录一条用户消息（来自 ASR 或 QQ 文字）。

    若 ``stream_id`` 不匹配当前通话 → 静默丢弃（防御调用方传错 stream）。
    刷新 ``last_activity_at``——只要有用户输入，超时计时器重置。

    **幂等去重**：anima_chatter 在 ``fetch_unreads`` 钩子里把通话期间的
    用户消息塞进 ``messages_in_call``，但同一批 unread 在 chatter 主循环
    重试 / 续轮时可能被 ``fetch_unreads`` 多次返回。为避免通话稿出现
    重复条目（"【第 1 轮 / 用户】咪 / 【第 2 轮 / 用户】咪"），按 ``ts``
    + ``text`` 的组合去重：相同 ts 与 text 的 user 消息只记录一次。
    没有 ``ts`` 的消息退化为按 ``text`` 去重，仍能避免视觉上的重复。
    """

    async with _lock:
        if _active_call is None or _active_call.caller_stream_id != stream_id:
            return
        now = time.time()
        normalized_ts = float(ts) if ts is not None else now

        # 幂等去重：扫描已有 user 消息中是否已存在 (text, ts) 完全相同的条目
        for existing in _active_call.messages_in_call:
            if existing.get("role") != "user":
                continue
            if existing.get("text") != text:
                continue
            existing_ts = existing.get("ts")
            # 同 text + 同 ts（精确到秒级）视为重复；ts 不存在时仅按 text 去重
            if ts is None or (
                isinstance(existing_ts, (int, float))
                and abs(float(existing_ts) - normalized_ts) < 0.5
            ):
                _active_call.last_activity_at = now
                return

        _active_call.messages_in_call.append(
            {"role": "user", "text": text, "ts": normalized_ts}
        )
        _active_call.last_activity_at = now


async def record_assistant_message(stream_id: str, text: str) -> None:
    """通话期间记录一条 bot 回复（say action 调用后由 runner 触发）。

    刷新 ``last_activity_at``——bot 主动发声也算活跃，避免单方独白被误判超时。
    """

    async with _lock:
        if _active_call is None or _active_call.caller_stream_id != stream_id:
            return
        now = time.time()
        _active_call.messages_in_call.append(
            {"role": "assistant", "text": text, "ts": now}
        )
        _active_call.last_activity_at = now


async def record_system_note(stream_id: str, text: str) -> None:
    """通话期间记录一条系统标注（如"通话开始"、"通话结束"等元事件）。

    与 user/assistant 消息的区别：``role == "system"``，下游 handler 会
    特殊处理——例如 kfc 的 voice_call_history_handler 会把它转换成"括号内
    的状态描述"插入 chain_payloads，而不是当成对话发言。

    系统标注**也刷新** ``last_activity_at``——通话开始 / 结束这种关键节点
    显然不应该当成"静默"。
    """

    async with _lock:
        if _active_call is None or _active_call.caller_stream_id != stream_id:
            return
        now = time.time()
        _active_call.messages_in_call.append(
            {"role": "system", "text": text, "ts": now}
        )
        _active_call.last_activity_at = now
