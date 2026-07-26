"""vtb_live 流水线状态机的单元测试。

覆盖轮内 / 跨轮排队语义、门时刻计算（trigger_percent 与 min_remaining 取较晚
者）、最低时长门槛与状态清理。

所有用例都禁用后台唤醒任务调度——那条路径依赖框架的 stream loop，不属于本模块
的单元测试范围。
"""

from __future__ import annotations

import time

import pytest

from plugins.anima_chatter.config import PipeliningSection
from plugins.anima_chatter.runtime import pipeline_state


_STREAM = "live:room:1001"


@pytest.fixture(autouse=True)
def _disable_wakeup(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁用门到点唤醒任务，避免单元测试依赖框架 stream loop。"""

    monkeypatch.setattr(
        pipeline_state, "_schedule_wakeup_unlocked", lambda *_args: None
    )


def _configure(**overrides: float | bool) -> None:
    """按给定覆盖项注入流水线配置。

    Args:
        **overrides: 要覆盖的配置字段。
    """

    defaults: dict[str, float | bool] = {
        "enabled": True,
        "trigger_percent": 0.6,
        "silence_gap_seconds": 0.0,
        "silence_gap_jitter": 0.0,
        "min_duration_seconds": 1.0,
        "min_remaining_seconds": 0.0,
    }
    defaults.update(overrides)
    pipeline_state.configure(PipeliningSection(**defaults))  # type: ignore[arg-type]


async def test_reserve_returns_requested_duration() -> None:
    """预约返回的区间长度应等于请求时长。"""

    _configure()

    start_at, finish_at = await pipeline_state.reserve(_STREAM, 10.0)

    assert finish_at - start_at == pytest.approx(10.0)


async def test_zero_duration_reserve_is_noop() -> None:
    """零时长预约应返回同一时刻且不影响累积。"""

    _configure()

    start_at, finish_at = await pipeline_state.reserve(_STREAM, 0.0)

    assert start_at == finish_at
    assert await pipeline_state.is_gate_pending(_STREAM) is False


async def test_same_round_reservations_are_contiguous() -> None:
    """轮内多次预约应紧接排队，不插入间隔。"""

    _configure(silence_gap_seconds=5.0)

    _, first_finish = await pipeline_state.reserve(_STREAM, 10.0)
    second_start, _ = await pipeline_state.reserve(_STREAM, 5.0)

    assert second_start == pytest.approx(first_finish)


async def test_cross_round_reservation_adds_silence_gap() -> None:
    """跨轮的第一次预约应在上一轮结束后加静默间隔。"""

    _configure(silence_gap_seconds=7.0)

    _, first_finish = await pipeline_state.reserve(_STREAM, 30.0)
    await pipeline_state.reset_round(_STREAM)
    second_start, _ = await pipeline_state.reserve(_STREAM, 10.0)

    assert second_start == pytest.approx(first_finish + 7.0)


async def test_cross_round_gap_skipped_when_queue_empty() -> None:
    """队列已空时跨轮不加间隔——没有需要错开的音频。"""

    _configure(silence_gap_seconds=7.0)

    await pipeline_state.reset_round(_STREAM)
    start_at, _ = await pipeline_state.reserve(_STREAM, 10.0)

    assert start_at == pytest.approx(time.monotonic(), abs=0.5)


async def test_gate_pending_after_sufficient_accumulation() -> None:
    """累积时长达标后应存在待通过的门。"""

    _configure(min_duration_seconds=1.0, trigger_percent=0.5)

    await pipeline_state.reserve(_STREAM, 10.0)

    assert await pipeline_state.is_gate_pending(_STREAM) is True


async def test_gate_absent_below_min_duration() -> None:
    """累积时长不足 ``min_duration_seconds`` 时不启用流水线。"""

    _configure(min_duration_seconds=30.0)

    await pipeline_state.reserve(_STREAM, 10.0)

    assert await pipeline_state.is_gate_pending(_STREAM) is False


async def test_gate_absent_when_disabled() -> None:
    """流水线关闭时永远没有门。"""

    _configure(enabled=False)

    await pipeline_state.reserve(_STREAM, 60.0)

    assert await pipeline_state.is_gate_pending(_STREAM) is False


async def test_wait_gate_returns_immediately_when_disabled() -> None:
    """流水线关闭时 ``wait_gate`` 应立即返回。"""

    _configure(enabled=False)
    await pipeline_state.reserve(_STREAM, 60.0)

    started = time.monotonic()
    await pipeline_state.wait_gate(_STREAM)

    assert time.monotonic() - started < 0.5


async def test_min_remaining_pushes_gate_later_for_long_audio() -> None:
    """长音频下"结束前 N 秒"应比 trigger_percent 更晚，取较晚者。

    180 秒音频、trigger=60% → 比例门在 +108s；min_remaining=25 → 结束前门在
    +155s。应取 +155s。
    """

    _configure(trigger_percent=0.6, min_remaining_seconds=25.0)

    start_at, finish_at = await pipeline_state.reserve(_STREAM, 180.0)
    state = pipeline_state._states[_STREAM]  # noqa: SLF001 - 白盒校验门时刻
    gate = pipeline_state._gate_at_unlocked(state)  # noqa: SLF001

    assert gate == pytest.approx(finish_at - 25.0)
    assert gate > start_at + 180.0 * 0.6


async def test_trigger_percent_wins_for_short_audio() -> None:
    """短音频下比例门更晚，应取比例门。

    30 秒音频、trigger=60% → 比例门在 +18s；min_remaining=25 → 结束前门在 +5s。
    应取 +18s。
    """

    _configure(trigger_percent=0.6, min_remaining_seconds=25.0)

    start_at, _ = await pipeline_state.reserve(_STREAM, 30.0)
    state = pipeline_state._states[_STREAM]  # noqa: SLF001
    gate = pipeline_state._gate_at_unlocked(state)  # noqa: SLF001

    assert gate == pytest.approx(start_at + 18.0)


async def test_reset_round_clears_gate_but_keeps_queue() -> None:
    """重置轮次应清掉门，但保留队列尾时刻供下一轮排队。"""

    _configure()

    _, finish_at = await pipeline_state.reserve(_STREAM, 10.0)
    await pipeline_state.reset_round(_STREAM)

    assert await pipeline_state.is_gate_pending(_STREAM) is False
    state = pipeline_state._states[_STREAM]  # noqa: SLF001
    assert state.audio_finish_at == pytest.approx(finish_at)
    assert state.round_accumulated == 0.0


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
