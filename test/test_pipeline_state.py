"""直播语音回复轮次容量状态机的单元测试。

覆盖首音门、下一回复轮容量、歌曲尾段、单项取消和状态清理。

所有用例都禁用后台唤醒任务调度——那条路径依赖框架的 stream loop，不属于本模块
的单元测试范围。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.anima_chatter.config import PipeliningSection
from plugins.anima_chatter.runtime import pipeline_state

_STREAM = "live:room:1001"


@pytest.fixture(autouse=True)
def _disable_wakeup(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str, Any], None]:
    """禁用门到点唤醒任务，避免单元测试依赖框架 stream loop。"""

    original = pipeline_state._schedule_wakeup_unlocked
    monkeypatch.setattr(
        pipeline_state, "_schedule_wakeup_unlocked", lambda *_args: None
    )
    return original


def _configure(**overrides: float) -> None:
    """注入轮次容量测试配置。"""

    pipeline_state.configure(
        PipeliningSection(
            song_prepare_lead_seconds=float(
                overrides.get("song_prepare_lead_seconds", 25.0)
            ),
            max_backlog_seconds=float(overrides.get("max_backlog_seconds", 86400.0)),
        )
    )


async def test_reserve_returns_requested_duration() -> None:
    """预约返回的区间长度应等于请求时长。"""

    _configure()

    start_at, finish_at = await pipeline_state.reserve(_STREAM, 10.0)

    assert finish_at - start_at == pytest.approx(10.0)


async def test_zero_duration_reservation_does_not_hold_capacity() -> None:
    """零时长项不等待起播回调，也不占住当前轮。"""

    _configure()
    start_at, finish_at = await pipeline_state.reserve(_STREAM, 0.0)

    assert start_at == finish_at
    assert _STREAM not in pipeline_state._states


async def test_short_reply_opens_next_round_only_after_actual_start() -> None:
    """短回复尚未起播时不放行，首音输出后允许准备下一轮。"""

    _configure()
    await pipeline_state.reserve(_STREAM, 1.0, track_id="short-reply")

    assert await pipeline_state.is_gate_pending(_STREAM) is True
    await pipeline_state.report_actual(
        _STREAM, track_id="short-reply", started_at=time.monotonic()
    )
    assert await pipeline_state.is_gate_pending(_STREAM) is False


async def test_song_reservation_keeps_gate_closed_after_speech_starts() -> None:
    """同轮 speech 起播后，未起播的歌曲仍阻止下一轮越过容量门。"""

    _configure()
    await pipeline_state.reserve(_STREAM, 2.0, track_id="speech", kind="speech")
    await pipeline_state.report_actual(
        _STREAM, track_id="speech", started_at=time.monotonic()
    )
    await pipeline_state.reserve(_STREAM, 120.0, track_id="song", kind="song")

    assert await pipeline_state.is_gate_pending(_STREAM) is True


async def test_one_next_round_can_contain_multiple_actions() -> None:
    """同一回复轮的多个 Action 共用一轮容量，后续轮只允许一代。"""

    _configure()
    await pipeline_state.reset_round(_STREAM)
    await pipeline_state.reserve(_STREAM, 3.0, track_id="first")
    await pipeline_state.reserve(_STREAM, 2.0, track_id="second")
    assert await pipeline_state.is_gate_pending(_STREAM) is True

    await pipeline_state.report_actual(
        _STREAM, track_id="first", started_at=time.monotonic()
    )
    assert await pipeline_state.is_gate_pending(_STREAM) is False
    await pipeline_state.reset_round(_STREAM)
    await pipeline_state.reserve(_STREAM, 4.0, track_id="next-a")
    await pipeline_state.reserve(_STREAM, 1.0, track_id="next-b")
    state = pipeline_state._states[_STREAM]
    assert state.reservations["next-a"].round_id == state.reservations["next-b"].round_id
    assert len({item.round_id for item in state.reservations.values()}) == 2
    assert await pipeline_state.is_gate_pending(_STREAM) is True
    await pipeline_state.report_actual(
        _STREAM, track_id="next-a", started_at=time.monotonic()
    )
    assert await pipeline_state.is_gate_pending(_STREAM) is False


async def test_song_gate_opens_only_in_configured_tail_window() -> None:
    """歌曲起播后直到配置尾段才开放下一轮。"""

    _configure(song_prepare_lead_seconds=25.0)
    await pipeline_state.reset_round(_STREAM)
    await pipeline_state.reserve(_STREAM, 180.0, track_id="song", kind="song")
    started = time.monotonic()
    await pipeline_state.report_actual(_STREAM, track_id="song", started_at=started)
    assert await pipeline_state.is_gate_pending(_STREAM) is True

    state = pipeline_state._states[_STREAM]
    state.reservations["song"].started_at = started - 180.0 + 24.0
    assert await pipeline_state.is_gate_pending(_STREAM) is False


async def test_song_tail_gate_opens_at_exact_preparation_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """180 秒歌曲在第 155 秒放行，不早于配置的结束前 25 秒。"""

    clock = 1000.0
    monkeypatch.setattr(
        pipeline_state, "time", SimpleNamespace(monotonic=lambda: clock)
    )
    _configure(song_prepare_lead_seconds=25.0)
    await pipeline_state.reset_round(_STREAM)
    await pipeline_state.reserve(_STREAM, 180.0, track_id="song", kind="song")
    await pipeline_state.report_actual(_STREAM, track_id="song", started_at=clock)

    clock = 1154.999
    assert await pipeline_state.is_gate_pending(_STREAM) is True
    clock = 1155.0
    assert await pipeline_state.is_gate_pending(_STREAM) is False
    await asyncio.wait_for(pipeline_state.wait_gate(_STREAM), timeout=1)
    await pipeline_state.reset_round(_STREAM)
    assert await pipeline_state.reserve(_STREAM, 8.0, track_id="next") is not None

    state = pipeline_state._states[_STREAM]
    assert state.reservations["song"].finished_at is None
    assert state.next_round_id is not None
    assert await pipeline_state.is_gate_pending(_STREAM) is True


async def test_relinquish_releases_only_cancelled_item() -> None:
    """中项取消不释放同轮的其他容量。"""

    _configure()
    await pipeline_state.reset_round(_STREAM)
    await pipeline_state.reserve(_STREAM, 5.0, track_id="keep")
    await pipeline_state.reserve(_STREAM, 5.0, track_id="cancel")
    await pipeline_state.relinquish(_STREAM, track_id="cancel")

    state = pipeline_state._states[_STREAM]
    assert set(state.reservations) == {"keep"}
    assert await pipeline_state.is_gate_pending(_STREAM) is True


async def test_next_round_backlog_is_bounded_but_current_song_is_not() -> None:
    """歌曲自身可长于默认积压值，下一轮排队仍受容量限制。"""

    _configure(max_backlog_seconds=10.0)
    await pipeline_state.reset_round(_STREAM)
    await pipeline_state.reserve(_STREAM, 120.0, track_id="song", kind="song")
    await pipeline_state.report_actual(
        _STREAM, track_id="song", started_at=time.monotonic()
    )
    state = pipeline_state._states[_STREAM]
    item = state.reservations["song"]
    item.started_at = time.monotonic() - 100.0

    await pipeline_state.reset_round(_STREAM)
    assert await pipeline_state.reserve(_STREAM, 11.0, track_id="too-long") is None
    assert state.next_round_id is None
    assert await pipeline_state.reserve(_STREAM, 10.0, track_id="allowed") is not None


async def test_clear_removes_single_stream() -> None:
    """清理单条流不应影响其他流。"""

    _configure()

    await pipeline_state.reserve(_STREAM, 10.0)
    await pipeline_state.reserve("live:room:2002", 10.0)
    await pipeline_state.clear(_STREAM)

    assert _STREAM not in pipeline_state._states  # noqa: SLF001
    assert "live:room:2002" in pipeline_state._states  # noqa: SLF001


async def test_clear_all_removes_every_stream() -> None:
    """全量清理应清空所有流的状态。"""

    _configure()

    await pipeline_state.reserve(_STREAM, 10.0)
    await pipeline_state.reserve("live:room:2002", 10.0)
    await pipeline_state.clear_all()

    assert pipeline_state._states == {}  # noqa: SLF001


async def test_clear_wakes_waiting_gate() -> None:
    """清理状态会释放阻塞在容量门上的等待者。"""

    _configure()
    await pipeline_state.reserve(_STREAM, 120.0, track_id="song", kind="song")
    waiter = asyncio.create_task(pipeline_state.wait_gate(_STREAM))

    await pipeline_state.clear(_STREAM)
    await waiter


async def test_wait_gate_propagates_cancellation() -> None:
    """调用方取消容量门等待时，取消异常正常向上传播。"""

    _configure()
    await pipeline_state.reserve(_STREAM, 120.0, track_id="song", kind="song")
    waiter = asyncio.create_task(pipeline_state.wait_gate(_STREAM))
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter


async def test_actual_start_notifies_waiting_capacity_gate(
    monkeypatch: pytest.MonkeyPatch,
    _disable_wakeup: Callable[[str, Any], None],
) -> None:
    """真实状态通知能释放已在等待容量门的协程，不依赖轮询。"""

    def close_wakeup(coro: Coroutine[Any, Any, Any], **kwargs: Any) -> None:
        """仅隔离对框架的后台唤醒，保留实际 change_event 通知。"""

        coro.close()

    monkeypatch.setattr(pipeline_state, "_schedule_wakeup_unlocked", _disable_wakeup)
    monkeypatch.setattr(pipeline_state, "create_background_task", close_wakeup)
    _configure()
    await pipeline_state.reserve(_STREAM, 1.0, track_id="pending-start")
    waiter = asyncio.create_task(pipeline_state.wait_gate(_STREAM))
    await asyncio.sleep(0)
    assert not waiter.done()
    try:
        await pipeline_state.report_actual(
            _STREAM, track_id="pending-start", started_at=time.monotonic()
        )
        await asyncio.wait_for(waiter, timeout=1)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_claiming_next_round_does_not_open_a_third_round() -> None:
    """重复确认同一 Actor 轮不会再推进一个容量代次。"""

    _configure()
    await pipeline_state.reserve(_STREAM, 2.0, track_id="first")
    await pipeline_state.report_actual(
        _STREAM, track_id="first", started_at=time.monotonic()
    )
    await pipeline_state.reset_round(_STREAM)
    state = pipeline_state._states[_STREAM]
    claimed_round = state.target_round_id

    await pipeline_state.reset_round(_STREAM)

    assert state.target_round_id == claimed_round


async def test_empty_round_seal_does_not_allocate_an_extra_generation() -> None:
    """seal 释放无音频轮，下一次 claim 只推进一个容量代次。"""

    _configure()
    await pipeline_state.reserve(_STREAM, 1.0, track_id="first")
    await pipeline_state.report_actual(
        _STREAM, track_id="first", started_at=time.monotonic()
    )
    await pipeline_state.reset_round(_STREAM)
    state = pipeline_state._states[_STREAM]
    actor_round = state.target_round_id

    await pipeline_state.seal_empty_round(_STREAM)
    await pipeline_state.reset_round(_STREAM)
    next_round = state.target_round_id
    await pipeline_state.reset_round(_STREAM)

    assert next_round == actor_round + 1
    assert state.target_round_id == next_round


async def test_relinquish_last_next_round_reservation_releases_claim() -> None:
    """取消下一轮最后一项 reservation 时释放该轮 claim。"""

    _configure()
    await pipeline_state.reserve(_STREAM, 120.0, track_id="song", kind="song")
    await pipeline_state.report_actual(
        _STREAM,
        track_id="song",
        started_at=time.monotonic() - 100.0,
    )
    await pipeline_state.reset_round(_STREAM)
    await pipeline_state.reserve(_STREAM, 5.0, track_id="next")
    state = pipeline_state._states[_STREAM]

    await pipeline_state.relinquish(_STREAM, track_id="next")

    assert state.next_round_id is None
    assert await pipeline_state.is_gate_pending(_STREAM) is False
