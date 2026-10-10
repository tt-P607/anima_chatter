"""anima_chatter 语音回复轮次的音频容量状态机。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger

from .._internal_compat import (
    BackgroundTaskHandle,
    cancel_background_task,
    create_background_task,
    wait_state_snapshot,
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
    "relinquish",
    "report_actual",
    "reserve",
    "reset_round",
    "seal_empty_round",
    "wait_gate",
]


logger = get_logger("anima_chatter.pipeline")


@dataclass(slots=True)
class _StreamState:
    """单条 stream 的流水线状态。"""

    wakeup_handle: BackgroundTaskHandle | None = None
    """当前容量门唤醒任务句柄。"""

    round_id: int = 0
    current_round_id: int = 0
    next_round_id: int | None = None
    current_round_started: bool = False
    target_round_id: int = 0
    target_round_sealed: bool = False
    change_event: asyncio.Event = field(default_factory=asyncio.Event)
    reservations: dict[str, _Reservation] = field(default_factory=dict)
    next_track_id: int = 0


@dataclass(slots=True)
class _Reservation:
    """单条音频在回复轮中的容量占用与实际播放事件。"""

    round_id: int
    duration: float
    kind: str
    started_at: float | None = None
    finished_at: float | None = None


_section: PipeliningSection | None = None
_states: dict[str, _StreamState] = {}
_lock = asyncio.Lock()

# 唤醒必达：注入唤醒后确认 stream 已脱离 Wait 的重试参数。
_WAKEUP_CONFIRM_INTERVAL = 0.5
_WAKEUP_CONFIRM_ATTEMPTS = 10


def configure(section: PipeliningSection) -> None:
    """注入 ``[pipelining]`` 配置段。插件 ``on_plugin_loaded`` 时调用一次。

    重复调用以最后一次为准；不清空已有状态——配置改了状态依然有效。

    Args:
        section: 插件配置的 ``pipelining`` 段（唯一真源，本模块不复制字段）。
    """

    global _section
    _section = section
    logger.info(
        f"流水线配置已应用: song_prepare_lead="
        f"{section.song_prepare_lead_seconds:.1f}s, "
        f"max_backlog={_max_backlog_unlocked():.0f}s"
    )


def _max_backlog_unlocked() -> float:
    """读取积压上限配置。

    Returns:
        最大允许积压秒数；未 configure 时不限制积压。
    """

    if _section is None:
        return float("inf")
    return float(_section.max_backlog_seconds)


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


def _cancel_wakeup_unlocked(state: _StreamState) -> None:
    """取消状态关联的唤醒任务并清空句柄。

    Args:
        state: 目标 stream 的状态。
    """

    handle = state.wakeup_handle
    state.wakeup_handle = None
    cancel_background_task(handle)


async def _wake_and_confirm(stream_id: str) -> None:
    """注入唤醒并轮询确认 stream 已脱离 Wait；失败重复注入（唤醒必达）。

    only_if_new_unreads=True：仅在确实有新弹幕时唤醒，避免 LLM 没事找事
    主动发起对话。若注入时 stream 恰好不在 Wait 状态（上一轮 LLM 还没返回），
    事件会被框架丢弃——确认时以**注入前的未读计数基线**为对照：仍在 Wait
    且未读计数未推进，才能判定注入真被消费（或已进入新一轮 Wait）；仅
    "仍处于 Wait" 不够——旧 Wait + 未消费的注入事件也表现为 waiting=True。

    Args:
        stream_id: 目标聊天流 ID。
    """

    baseline: float | None = None
    confirm_error_logged = False
    for attempt in range(1, _WAKEUP_CONFIRM_ATTEMPTS + 1):
        try:
            _, baseline = wait_state_snapshot(stream_id)
            woke = wake_stream_from_wait(stream_id, only_if_new_unreads=True)
            if not woke:
                # 无新弹幕或无 wait 状态可解除：无事可做。
                logger.info(
                    f"[pipeline {stream_id[:8]}] 流水线门到点，"
                    "但无新弹幕累积，继续保持 Wait"
                )
                return
        except RuntimeError as exc:
            # 兼容层结构失效：不可伪装成成功，也不能多重试——每轮都会同样
            # 失败。记 ERROR 并终止本轮确认（S1 hack 失效应有可见性）。
            logger.error(
                f"[pipeline {stream_id[:8]}] 唤醒确认失败：{exc}（兼容层失效，"
                "本轮放弃确认循环；请检查框架 stream loop 结构变更）"
            )
            return

        logger.info(
            f"[pipeline {stream_id[:8]}] 流水线门到点，已注入唤醒"
            f"（第 {attempt} 次），等待 stream loop 响应"
        )
        # 确认：等待一小段时间后检查 Wait 状态是否已被消费。
        await asyncio.sleep(_WAKEUP_CONFIRM_INTERVAL)
        try:
            waiting, unread_at_yield = wait_state_snapshot(stream_id)
        except RuntimeError as exc:
            if not confirm_error_logged:
                logger.error(
                    f"[pipeline {stream_id[:8]}] 唤醒后确认快照不可用: {exc}"
                    "（注入已发出，跳过后续确认）"
                )
                confirm_error_logged = True
            return
        if not waiting:
            logger.info(
                f"[pipeline {stream_id[:8]}] 唤醒已确认：stream loop 已脱离 Wait"
            )
            return
        # 仍在 Wait——两种情况：注入被消费后进入新一轮 Wait（未读计数推进），
        # 或注入被丢弃（旧 Wait + 计数未推进）。与注入前基线比较才能区分。
        if unread_at_yield is not None and unread_at_yield != baseline:
            logger.info(
                f"[pipeline {stream_id[:8]}] stream 处于新一轮 Wait"
                "（未读计数已推进），唤醒视为已生效"
            )
            return

    logger.error(
        f"[pipeline {stream_id[:8]}] 唤醒注入 {_WAKEUP_CONFIRM_ATTEMPTS} 次"
        "均未确认生效——stream loop 可能假死，请检查日志中上一轮 LLM 调用状态"
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
            state.change_event.set()
            _cancel_wakeup_unlocked(state)
    if state is not None:
        logger.info(f"[pipeline {stream_id[:8]}] 状态已彻底清空")


async def clear_all() -> None:
    """取消所有唤醒任务并清空全部流的流水线状态。插件卸载时调用。"""

    async with _lock:
        states = list(_states.values())
        _states.clear()
        for state in states:
            state.change_event.set()
            _cancel_wakeup_unlocked(state)
    if states:
        logger.info(f"流水线状态已全部清空，共 {len(states)} 条流")


async def is_gate_pending(stream_id: str) -> bool:
    """判断是否尚不能准备唯一的下一回复轮。"""

    async with _lock:
        state = _states.get(stream_id)
        return state is not None and not _gate_open_unlocked(state, time.monotonic())


def _active_round_unlocked(
    state: _StreamState,
) -> list[_Reservation]:
    """返回当前轮尚未结束的项目。"""

    return [
        item
        for item in state.reservations.values()
        if item.round_id == state.current_round_id and item.finished_at is None
    ]


def _gate_open_unlocked(state: _StreamState, now: float) -> bool:
    """按实际起播和当前轮存量判断下一轮容量。"""

    if state.next_round_id is not None:
        return False
    active = _active_round_unlocked(state)
    if not active:
        return True
    lead = _section.song_prepare_lead_seconds if _section is not None else 25.0
    for item in active:
        if item.kind == "song" and (
            item.started_at is None
            or item.started_at + item.duration - lead > now
        ):
            return False
    return any(item.started_at is not None for item in active) or state.current_round_started


def _schedule_wakeup_unlocked(stream_id: str, state: _StreamState) -> None:
    """状态变化时循环检查容量并恢复有新未读的 stream。"""

    state.change_event.set()
    handle = state.wakeup_handle
    task = getattr(handle, "task", None) if handle is not None else None
    if task is not None and not task.done():
        return

    async def _wakeup() -> None:
        """等待状态开放或歌曲进入尾段。"""

        while True:
            async with _lock:
                current = _states.get(stream_id)
                if current is None or _gate_open_unlocked(current, time.monotonic()):
                    break
                current.change_event.clear()
                change_event = current.change_event
                lead = _section.song_prepare_lead_seconds if _section else 25.0
                deadlines = [
                    item.started_at + item.duration - lead
                    for item in _active_round_unlocked(current)
                    if item.kind == "song" and item.started_at is not None
                ]
                remaining = (
                    max(0.05, min(deadlines) - time.monotonic())
                    if deadlines
                    else 0.5
                )
            async with feed_watchdog_during(stream_id, interval=5.0):
                try:
                    await asyncio.wait_for(change_event.wait(), timeout=remaining)
                except TimeoutError:
                    pass
        await _wake_and_confirm(stream_id)

    state.wakeup_handle = create_background_task(
        _wakeup(),
        name=f"anima_chatter.pipeline_wakeup.{stream_id[:8]}",
        metadata={"stream_id": stream_id, "kind": "pipeline_wakeup"},
    )


async def reserve(
    stream_id: str,
    duration: float,
    *,
    track_id: str = "",
    kind: str = "speech",
) -> tuple[float, float] | None:
    """登记单项音频容量并返回仅供估算的时刻区间。"""

    duration = max(0.0, float(duration))
    if duration == 0.0:
        now = time.monotonic()
        return now, now
    async with _lock:
        state = _get_state_unlocked(stream_id)
        now = time.monotonic()
        if state.target_round_id == 0:
            if state.current_round_id == 0:
                state.round_id += 1
                state.current_round_id = state.round_id
            state.target_round_id = state.current_round_id
        round_id = state.target_round_id
        claim_next_round = (
            round_id != state.current_round_id and state.next_round_id is None
        )
        if claim_next_round and not _gate_open_unlocked(state, now):
            return None
        backlog = sum(
            item.duration
            for item in state.reservations.values()
            if item.finished_at is None
            and (
                item.round_id != state.current_round_id
                or item.kind != "song"
            )
        )
        added_backlog = (
            duration
            if round_id != state.current_round_id or kind != "song"
            else 0.0
        )
        if (
            _section is not None
            and backlog + added_backlog > _section.max_backlog_seconds
        ):
            return None
        state.next_track_id += 1
        track_id = track_id or f"{stream_id}:{state.next_track_id}"
        if track_id in state.reservations:
            return None
        if claim_next_round:
            state.next_round_id = round_id
        state.reservations[track_id] = _Reservation(round_id, duration, kind)
        _schedule_wakeup_unlocked(stream_id, state)
        return now, now + duration


async def report_actual(
    stream_id: str,
    *,
    track_id: str,
    started_at: float | None = None,
    finished_at: float | None = None,
) -> None:
    """更新该音频项实际起播或结束时间，单项结束不影响同轮其他项。"""

    async with _lock:
        state = _states.get(stream_id)
        item = state.reservations.get(track_id) if state is not None else None
        if item is None:
            return
        reservation = item
        if started_at is not None:
            reservation.started_at = started_at
            if reservation.round_id == state.current_round_id:
                state.current_round_started = True
            if reservation.round_id == state.next_round_id:
                state.current_round_id = reservation.round_id
                state.next_round_id = None
                state.current_round_started = True
        if finished_at is not None:
            reservation.finished_at = finished_at
        if reservation.finished_at is not None:
            state.reservations.pop(track_id, None)
            if (
                reservation.round_id == state.current_round_id
                and not _active_round_unlocked(state)
                and state.next_round_id is not None
            ):
                state.current_round_id = state.next_round_id
                state.next_round_id = None
                state.current_round_started = False
        _schedule_wakeup_unlocked(stream_id, state)


async def relinquish(
    stream_id: str,
    *,
    track_id: str,
    reserved_until: float | None = None,
) -> None:
    """只释放指定的失败或取消项目。"""

    del reserved_until
    async with _lock:
        state = _states.get(stream_id)
        if state is None:
            return
        item = state.reservations.pop(track_id, None)
        if item is not None and item.round_id == state.next_round_id and not any(
            reservation.round_id == state.next_round_id
            for reservation in state.reservations.values()
        ):
            state.next_round_id = None
        if (
            item is not None
            and item.round_id == state.current_round_id
            and not _active_round_unlocked(state)
            and state.next_round_id is not None
        ):
            state.current_round_id = state.next_round_id
            state.next_round_id = None
            state.current_round_started = False
        if item is not None:
            _schedule_wakeup_unlocked(stream_id, state)


async def wait_gate(stream_id: str) -> None:
    """等待容量条件变化；循环重查且不绕过长歌曲门。"""

    async with feed_watchdog_during(stream_id, interval=5.0):
        while True:
            async with _lock:
                state = _states.get(stream_id)
                if state is None or _gate_open_unlocked(state, time.monotonic()):
                    return
                state.change_event.clear()
                change_event = state.change_event
                lead = _section.song_prepare_lead_seconds if _section else 25.0
                deadlines = [
                    item.started_at + item.duration - lead
                    for item in _active_round_unlocked(state)
                    if item.kind == "song" and item.started_at is not None
                ]
                timeout = (
                    max(0.05, min(deadlines) - time.monotonic())
                    if deadlines
                    else None
                )
            try:
                await asyncio.wait_for(change_event.wait(), timeout=timeout)
            except TimeoutError:
                continue


async def reset_round(stream_id: str) -> None:
    """标记一次真实 Actor 生成轮次；FOLLOW_UP 不应调用。"""

    async with _lock:
        state = _get_state_unlocked(stream_id)
        if state.target_round_sealed:
            state.round_id += 1
            state.target_round_id = state.round_id
            state.target_round_sealed = False
            _cancel_wakeup_unlocked(state)
            return
        if state.next_round_id is not None:
            state.current_round_id = state.next_round_id
            state.next_round_id = None
            state.target_round_id = state.current_round_id
            state.current_round_started = False
            _cancel_wakeup_unlocked(state)
            return
        if state.current_round_id == 0:
            state.round_id += 1
            state.current_round_id = state.round_id
            state.target_round_id = state.round_id
        elif state.target_round_id == state.current_round_id and state.current_round_started:
            state.round_id += 1
            state.target_round_id = state.round_id
        _cancel_wakeup_unlocked(state)


async def seal_empty_round(stream_id: str) -> None:
    """封存未登记任何音频的 Actor 轮，供 NDFC 回到 WAIT_USER 时调用。"""

    async with _lock:
        state = _states.get(stream_id)
        if state is None:
            return
        if any(
            item.round_id == state.target_round_id
            for item in state.reservations.values()
        ):
            return
        if (
            state.target_round_id == state.current_round_id
            and state.current_round_started
        ):
            return
        state.target_round_sealed = True
        state.change_event.set()
