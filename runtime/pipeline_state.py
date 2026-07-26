"""anima_chatter vtb_live 音频流水线状态机。

仅在 ``vtb_live`` 模式下生效，目的是让"Action 提前返回 + 后台音频持续播放"
不打架——按 stream 维护两个时间戳：

- ``audio_finish_at``：物理音频队列里最后一段音频**预计播完**的时刻；下一次
  ``reserve`` 的新音频从这里之后开始排队。
- ``round_start_at``：**本轮 LLM 调用**第一次 ``reserve`` 的起点；据此算出
  "流水线门"时刻，LLM 到点才被放行去准备下一轮。

关键语义：

1. **轮内**多个 Action 的 reserve 紧接排队，不加 silence_gap（同一轮连贯输出）。
2. **跨轮**第一个 reserve 加 silence_gap（可带随机抖动），避免接得太急。
3. 本轮累积时长低于 ``min_duration_seconds`` 时不启用流水线，门时刻返回 0。
4. 跨轮判定由调用方显式调 :func:`reset_round` 标记。

Action 内的协议::

    start_at, finish_at = await reserve(stream_id, duration)
    # 派发后台 task：等到 start_at → 播放 → 直到 finish_at
    # Action 立即返回，**不**等待 finish_at

LLM 入口的协议::

    await wait_gate(stream_id)   # 阻塞直到门时刻
    await reset_round(stream_id) # 通过门 → 标记新一轮

用模块级状态而非 Service 的理由同 :mod:`.call_state`。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger

from .._internal_compat import (
    BackgroundTaskHandle,
    cancel_background_task,
    create_background_task,
    wake_stream_from_wait,
)
from .heartbeat import feed_watchdog_during

if TYPE_CHECKING:
    from ..config import PipeliningSection


__all__ = [
    "clear",
    "clear_all",
    "configure",
    "is_gate_pending",
    "reserve",
    "reset_round",
    "wait_gate",
]


logger = get_logger("anima_chatter.pipeline")


@dataclass(slots=True)
class _StreamState:
    """单条 stream 的流水线状态。"""

    audio_finish_at: float = 0.0
    """物理音频队列中最后一段音频预计播完的 ``time.monotonic()`` 时刻。"""

    round_start_at: float = 0.0
    """本轮 LLM 调用第一次 reserve 的起点（用于计算门时刻）。"""

    round_accumulated: float = 0.0
    """本轮累积音频时长（秒，不含 silence_gap）。:func:`reset_round` 时清零。"""

    is_round_first: bool = True
    """下一次 reserve 是否为本轮第一次。控制 silence_gap 是否加入起点。"""

    last_silence_gap_used: float = 0.0
    """最近一次跨轮 reserve 实际使用的 silence_gap（含抖动），仅供日志。"""

    wakeup_handle: BackgroundTaskHandle | None = None
    """当前轮的门到点唤醒任务句柄；每次 reserve 重新调度。"""


_section: "PipeliningSection | None" = None
_states: dict[str, _StreamState] = {}
_lock = asyncio.Lock()


def configure(section: "PipeliningSection") -> None:
    """注入 ``[pipelining]`` 配置段。插件 ``on_plugin_loaded`` 时调用一次。

    重复调用以最后一次为准；不清空已有状态——配置改了状态依然有效。

    Args:
        section: 插件配置的 ``pipelining`` 段（唯一真源，本模块不复制字段）。
    """

    global _section
    _section = section
    logger.info(
        f"流水线配置已应用: enabled={section.enabled}, "
        f"trigger_percent={section.trigger_percent:.2f}, "
        f"silence_gap={section.silence_gap_seconds:.1f}s, "
        f"min_duration={section.min_duration_seconds:.1f}s"
    )


def _get_state_unlocked(stream_id: str) -> _StreamState:
    """**不加锁**地获取 / 创建 stream 状态。仅供已持锁的内部函数使用。

    Args:
        stream_id: 目标聊天流 ID。

    Returns:
        该 stream 的状态对象。
    """

    state = _states.get(stream_id)
    if state is None:
        state = _StreamState()
        _states[stream_id] = state
    return state


def _gate_at_unlocked(state: _StreamState) -> float:
    """**不加锁**地计算流水线门时刻。

    门时刻取 ``max(trigger_percent 时刻, 结束前 min_remaining_seconds 时刻)``
    ——两者取**较晚者**，尽可能多吞吐弹幕：

    - 短回复（30s, trigger=60%）：trigger 给 +18s，"结束前 25s" 给 +5s → 取 +18s
    - 长歌曲（180s, trigger=60%）：trigger 给 +108s，"结束前 25s" 给 +155s → 取 +155s

    Args:
        state: 目标 stream 的状态。

    Returns:
        ``time.monotonic()`` 口径的门时刻；返回 ``0.0`` 表示本轮不启用流水线。
    """

    if _section is None or not _section.enabled:
        return 0.0
    if state.round_accumulated < _section.min_duration_seconds:
        return 0.0

    trigger_gate = (
        state.round_start_at + state.round_accumulated * _section.trigger_percent
    )
    min_remaining = _section.min_remaining_seconds
    if min_remaining <= 0:
        return trigger_gate
    # finish_at 即 round_start_at + round_accumulated（轮内没有插入间隔）。
    late_gate = state.round_start_at + state.round_accumulated - min_remaining
    return max(trigger_gate, late_gate)


async def is_gate_pending(stream_id: str) -> bool:
    """返回指定流是否存在尚未通过的有效流水线门。

    Args:
        stream_id: 目标聊天流 ID。

    Returns:
        存在未到点的门时返回 ``True``。
    """

    async with _lock:
        state = _states.get(stream_id)
        if state is None:
            return False
        gate = _gate_at_unlocked(state)
    return gate > time.monotonic()


def _cancel_wakeup_unlocked(state: _StreamState) -> None:
    """取消状态关联的唤醒任务并清空句柄。

    Args:
        state: 目标 stream 的状态。
    """

    handle = state.wakeup_handle
    state.wakeup_handle = None
    cancel_background_task(handle)


def _schedule_wakeup_unlocked(stream_id: str, state: _StreamState) -> None:
    """**不加锁**地（重新）调度"门到点唤醒 stream loop"的后台任务。

    每次 reserve 后调用：先取消之前调度的任务（累积时长变化会推迟门时刻），
    再按当前门时刻重新起一个。门时刻为 0（未启用 / 累积不足）时不起任务。

    必须在已持有 :data:`_lock` 的上下文中调用。

    Args:
        stream_id: 目标聊天流 ID。
        state: 该 stream 的状态。
    """

    _cancel_wakeup_unlocked(state)

    gate = _gate_at_unlocked(state)
    if gate <= 0:
        return

    now = time.monotonic()
    wait_seconds = max(0.0, gate - now)

    async def _wakeup() -> None:
        """到点后按需唤醒 stream loop。"""

        try:
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            # only_if_new_unreads=True：仅在确实有新弹幕时唤醒，避免 LLM
            # 没事找事主动发起对话。
            if wake_stream_from_wait(stream_id, only_if_new_unreads=True):
                logger.info(
                    f"[pipeline {stream_id[:8]}] 流水线门到点（{wait_seconds:.2f}s 后）"
                    "+ 检测到新累积弹幕 → 主动唤醒 stream loop 处理"
                )
            else:
                logger.info(
                    f"[pipeline {stream_id[:8]}] 流水线门到点（{wait_seconds:.2f}s 后）"
                    "但无新弹幕累积，继续保持 Wait"
                )
        except asyncio.CancelledError:
            pass

    state.wakeup_handle = create_background_task(
        _wakeup(),
        name=f"anima_chatter.pipeline_wakeup.{stream_id[:8]}",
        metadata={"stream_id": stream_id, "kind": "pipeline_wakeup"},
    )


def _resolve_round_base_unlocked(state: _StreamState, now: float) -> float:
    """计算本次 reserve 的起播时刻。

    跨轮第一次：等上一轮播完再加 silence_gap（含随机抖动）；轮内后续：紧接
    队列尾，不加间隔。

    Args:
        state: 目标 stream 的状态。
        now: 当前 ``time.monotonic()``。

    Returns:
        本段音频应该开始播放的时刻。
    """

    if not state.is_round_first:
        return max(now, state.audio_finish_at)

    if state.audio_finish_at > now and _section is not None:
        gap = _section.silence_gap_seconds
        jitter = _section.silence_gap_jitter
        if jitter > 0:
            gap = max(0.0, gap + random.uniform(-jitter, jitter))
        state.last_silence_gap_used = gap
        base = state.audio_finish_at + gap
    else:
        state.last_silence_gap_used = 0.0
        base = now

    state.round_start_at = base
    state.is_round_first = False
    return base


async def reserve(stream_id: str, duration: float) -> tuple[float, float]:
    """预约一段音频在播放队列中的位置。

    Args:
        stream_id: 目标聊天流 ID。
        duration: 该段音频的物理时长（秒），**不含**任何 silence_gap。

    Returns:
        ``(start_at, finish_at)`` —— ``time.monotonic()`` 口径的起播与结束时刻。
        调用方应立即派发后台 task（睡到 ``start_at`` → 播放），Action 自身**不**
        等待 ``finish_at``。
    """

    duration = max(0.0, float(duration))
    if duration == 0.0:
        now = time.monotonic()
        return now, now

    async with _lock:
        state = _get_state_unlocked(stream_id)
        now = time.monotonic()

        start_at = _resolve_round_base_unlocked(state, now)
        finish_at = start_at + duration
        state.audio_finish_at = finish_at
        state.round_accumulated += duration

        is_round_first_log = state.round_accumulated == duration
        round_marker = "新一轮第一段" if is_round_first_log else "同轮追加"
        gap_info = (
            f", silence_gap={state.last_silence_gap_used:.2f}s"
            if is_round_first_log and state.last_silence_gap_used > 0
            else ""
        )
        logger.info(
            f"[pipeline {stream_id[:8]}] reserve {round_marker}: "
            f"duration={duration:.2f}s, start_in={start_at - now:.2f}s, "
            f"finish_in={finish_at - now:.2f}s, "
            f"round_acc={state.round_accumulated:.2f}s{gap_info}"
        )

        # 累计时长变化会推迟门时刻，需要重新调度唤醒任务。
        _schedule_wakeup_unlocked(stream_id, state)

        return start_at, finish_at


async def wait_gate(stream_id: str) -> None:
    """阻塞直到流水线门时刻到达。

    在即将发起新一轮 LLM 调用前调用，让 LLM 等到累积音频播放到
    ``trigger_percent`` 之后再执行。以下情况立即返回：流水线未启用、本轮累积
    时长不足 ``min_duration_seconds``、门时刻已过。

    本函数**不**调用 :func:`reset_round`——由上层在通过门后自行决定是否标记
    新一轮，避免在 timer 唤醒等"非真正消费"路径误清状态。

    Args:
        stream_id: 目标聊天流 ID。

    Raises:
        asyncio.CancelledError: 流被取消时透传，不阻塞清理路径。
    """

    async with _lock:
        state = _get_state_unlocked(stream_id)
        gate = _gate_at_unlocked(state)
        accumulated = state.round_accumulated

    if gate <= 0:
        return

    now = time.monotonic()
    if gate <= now:
        logger.info(
            f"[pipeline {stream_id[:8]}] 流水线门已过（直通）: "
            f"gate-now={gate - now:.2f}s"
        )
        return

    wait_seconds = gate - now
    trigger_percent = _section.trigger_percent if _section is not None else 0.0
    logger.info(
        f"[pipeline {stream_id[:8]}] LLM 等流水线门：阻塞 {wait_seconds:.2f}s "
        f"(累计 {trigger_percent * 100:.0f}% 触发点；本轮总时长 ≈ {accumulated:.1f}s)"
    )

    # 门可能阻塞数十秒，期间 chatter 主循环没机会 yield 心跳——必须主动喂
    # watchdog，避免触发框架的 stream 重启阈值。
    try:
        async with feed_watchdog_during(stream_id, interval=5.0):
            await asyncio.sleep(wait_seconds)
        logger.info(f"[pipeline {stream_id[:8]}] 流水线门通过，LLM 开始新一轮调用")
    except asyncio.CancelledError:
        logger.info(f"[pipeline {stream_id[:8]}] wait_gate 被取消")
        raise


async def reset_round(stream_id: str) -> None:
    """标记下一次 reserve 是新一轮的开始。

    通常在通过 :func:`wait_gate` 之后立即调用——让下一次 reserve 计算起点时把
    silence_gap 加进去。清理本轮累积，但**不**清 ``audio_finish_at``（队列里
    还有未播完的音频，新轮 reserve 必须基于它排队）。

    Args:
        stream_id: 目标聊天流 ID。
    """

    async with _lock:
        state = _get_state_unlocked(stream_id)
        had_round = state.round_accumulated > 0
        state.is_round_first = True
        state.round_accumulated = 0.0
        state.round_start_at = 0.0
        remaining = max(0.0, state.audio_finish_at - time.monotonic())
        # 本轮的门已经用完，取消它的唤醒任务。
        _cancel_wakeup_unlocked(state)

    if had_round:
        logger.info(
            f"[pipeline {stream_id[:8]}] reset_round：本轮累积清零，"
            f"队列剩余 {remaining:.1f}s 音频未播完"
        )


async def clear(stream_id: str) -> None:
    """彻底清空指定 stream 的流水线状态。

    用于异常恢复 / chatter 卸载场景。清空后下一次 reserve 视同全新开始。

    Args:
        stream_id: 目标聊天流 ID。
    """

    async with _lock:
        state = _states.pop(stream_id, None)
        if state is not None:
            _cancel_wakeup_unlocked(state)
    if state is not None:
        logger.info(f"[pipeline {stream_id[:8]}] 状态已彻底清空")


async def clear_all() -> None:
    """取消所有唤醒任务并清空全部流的流水线状态。插件卸载时调用。"""

    async with _lock:
        states = list(_states.values())
        _states.clear()
        for state in states:
            _cancel_wakeup_unlocked(state)
    if states:
        logger.info(f"流水线状态已全部清空，共 {len(states)} 条流")
