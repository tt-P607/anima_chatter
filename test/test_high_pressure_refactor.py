"""高压稳定性重构机制的单元测试。

覆盖：pipeline 背压拒排、实际进度回写（单一时钟真源）、唤醒必达确认循环与
表演锁收窄后的会话参数隔离。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest

from plugins.anima_chatter.config import PipeliningSection
from plugins.anima_chatter.runtime import pipeline_state

# ── pipeline 背压拒排 + 进度回写 ─────────────────────────────


_PipeliningFixture = PipeliningSection


@pytest.fixture
def pipelining_section() -> _PipeliningFixture:
    """构造真实 PipeliningSection 实例（字段与配置类同一真源）。"""

    return PipeliningSection(max_backlog_seconds=10.0)


class TestBackpressureReject:
    """max_backlog_seconds 背压拒排。"""

    async def test_reserve_rejects_when_backlog_exceeds_limit(
        self, pipelining_section: _PipeliningFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline_state.configure(pipelining_section)
        monkeypatch.setattr(pipeline_state, "_states", {})
        # 排 8s（含本段 <=10s，可接受）。
        first = await pipeline_state.reserve("stream", 8.0, track_id="t1")
        assert first is not None
        # 队列已积压 8s，再排 5s（含本段 13s > 10s）应被拒。
        second = await pipeline_state.reserve("stream", 5.0, track_id="t2")
        assert second is None

    async def test_song_reserves_song_duration_without_pre_delay(
        self,
        pipelining_section: _PipeliningFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """歌曲 reserve 使用实际歌曲时长和 song 类型，不包含开场静默。"""

        from plugins.anima_chatter.speech.playback import (
            activate_playback,
            close_playback,
            dispatch_track_pipelined,
        )

        pipeline_state.configure(pipelining_section)
        reservation_calls: list[tuple[float, str]] = []
        original_reserve = pipeline_state.reserve

        async def record_reservation(
            stream_id: str,
            duration: float,
            *,
            track_id: str = "",
            kind: str = "speech",
        ) -> tuple[float, float] | None:
            reservation_calls.append((duration, kind))
            return await original_reserve(
                stream_id, duration, track_id=track_id, kind=kind
            )

        monkeypatch.setattr(pipeline_state, "reserve", record_reservation)
        started = False

        async def on_started() -> None:
            nonlocal started
            started = True

        activate_playback()
        try:
            result = await dispatch_track_pipelined(
                stream_id="stream-song-pre-delay",
                audio_bytes=b"song",
                inst_bytes=None,
                audio_player=object(),
                performer=None,
                timeline=None,
                pre_delay=60.0,
                song_duration=8.0,
                song_name="song",
                on_started=on_started,
            )
        finally:
            await close_playback()
            activate_playback()

        assert result[0] is True
        assert reservation_calls == [(8.0, "song")]
        assert not started

    async def test_reserve_rejects_segment_that_would_exceed_limit(
        self, pipelining_section: _PipeliningFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """背压含本段自身时长：积压 9s + 本段 5s > 10s 应拒。"""
        pipeline_state.configure(pipelining_section)
        monkeypatch.setattr(pipeline_state, "_states", {})
        ok = await pipeline_state.reserve("stream", 9.0, track_id="t1")
        assert ok is not None
        oversized = await pipeline_state.reserve("stream", 5.0, track_id="t2")
        assert oversized is None

    async def test_reserve_accepts_fitting_segment(
        self, pipelining_section: _PipeliningFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """积压 5s + 本段 5s <= 10s 应接受。"""
        pipeline_state.configure(pipelining_section)
        monkeypatch.setattr(pipeline_state, "_states", {})
        ok = await pipeline_state.reserve("stream", 5.0, track_id="t1")
        assert ok is not None
        ok2 = await pipeline_state.reserve("stream", 5.0, track_id="t2")
        assert ok2 is not None

    async def test_reserve_accepts_within_limit(
        self, pipelining_section: _PipeliningFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline_state.configure(pipelining_section)
        monkeypatch.setattr(pipeline_state, "_states", {})
        ok = await pipeline_state.reserve("stream", 8.0, track_id="t1")
        assert ok is not None

    async def test_report_actual_tracks_events_on_reserved_item(
        self, pipelining_section: _PipeliningFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """实际起播保持容量占用，结束事件只释放对应 reservation。"""
        pipeline_state.configure(pipelining_section)
        monkeypatch.setattr(pipeline_state, "_states", {})
        reserved = await pipeline_state.reserve("stream", 5.0, track_id="t1")
        assert reserved is not None
        await pipeline_state.report_actual(
            "stream", track_id="t1", started_at=time.monotonic()
        )
        state = pipeline_state._states["stream"]
        assert state.reservations["t1"].started_at is not None
        assert await pipeline_state.is_gate_pending("stream") is False
        await pipeline_state.report_actual(
            "stream", track_id="t1", finished_at=time.monotonic()
        )
        assert "t1" not in state.reservations


# ── 唤醒必达 ─────────────────────────────────────────────────


class TestWakeupConfirm:
    """唤醒注入的确认重试循环。"""

    async def test_wake_and_confirm_retries_until_ack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """注入首次"成功"但确认失败时，重复注入直到 stream 脱离 Wait。"""
        from plugins.anima_chatter.runtime import pipeline_state as ps

        calls = {"wake": 0, "snapshots": 0}

        def fake_wake(stream_id: str, *, only_if_new_unreads: bool = True) -> bool:
            calls["wake"] += 1
            return True

        def fake_snapshot(stream_id: str) -> tuple[bool, float | None]:
            calls["snapshots"] += 1
            # 每轮“注入前基线 + 确认”各一次快照；前两轮仍在同一旧 Wait
            # （计数未推进），第三轮已脱离。
            if calls["snapshots"] < 5:
                return True, 5
            return False, None

        monkeypatch.setattr(ps, "wake_stream_from_wait", fake_wake)
        monkeypatch.setattr(ps, "wait_state_snapshot", fake_snapshot)
        monkeypatch.setattr(ps, "_WAKEUP_CONFIRM_INTERVAL", 0.01)

        await ps._wake_and_confirm("stream")
        # 唤醒被注入了 3 次（前两次确认失败）。
        assert calls["wake"] == 3

    async def test_wake_and_confirm_no_retry_on_new_round_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """仍在 Wait 但未读计数已推进（新一轮 Wait）时，视为生效不再重试。"""
        from plugins.anima_chatter.runtime import pipeline_state as ps

        calls = {"wake": 0}

        def fake_wake(stream_id: str, *, only_if_new_unreads: bool = True) -> bool:
            calls["wake"] += 1
            return True

        snapshot_state = {"baseline_done": False}

        def fake_snapshot(stream_id: str) -> tuple[bool, float | None]:
            if not snapshot_state["baseline_done"]:
                snapshot_state["baseline_done"] = True
                return True, 5  # 注入前基线：计数 5。
            return True, 7  # 确认时：新一轮 Wait，计数推进到 7。

        monkeypatch.setattr(ps, "wake_stream_from_wait", fake_wake)
        monkeypatch.setattr(ps, "wait_state_snapshot", fake_snapshot)
        monkeypatch.setattr(ps, "_WAKEUP_CONFIRM_INTERVAL", 0.01)

        await ps._wake_and_confirm("stream")
        assert calls["wake"] == 1

    async def test_wake_and_confirm_retries_when_unread_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """仍在同一旧 Wait（计数未推进）时必须重试，不能伪装确认。"""
        from plugins.anima_chatter.runtime import pipeline_state as ps

        calls = {"wake": 0}

        def fake_wake(stream_id: str, *, only_if_new_unreads: bool = True) -> bool:
            calls["wake"] += 1
            return True

        def fake_snapshot(stream_id: str) -> tuple[bool, float | None]:
            return True, 5  # 始终同一旧 Wait，计数永不推进。

        monkeypatch.setattr(ps, "wake_stream_from_wait", fake_wake)
        monkeypatch.setattr(ps, "wait_state_snapshot", fake_snapshot)
        monkeypatch.setattr(ps, "_WAKEUP_CONFIRM_ATTEMPTS", 3, raising=True)
        monkeypatch.setattr(ps, "_WAKEUP_CONFIRM_INTERVAL", 0.01)

        await ps._wake_and_confirm("stream")
        assert calls["wake"] == 3

    async def test_wake_and_confirm_no_unreads_no_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无新弹幕时注入直接返回，不进入确认循环。"""
        from plugins.anima_chatter.runtime import pipeline_state as ps

        def fake_wake(stream_id: str, *, only_if_new_unreads: bool = True) -> bool:
            return False

        snap_calls = {"count": 0}

        def fake_snapshot(stream_id: str) -> tuple[bool, float | None]:
            snap_calls["count"] += 1
            return False, None

        monkeypatch.setattr(ps, "wake_stream_from_wait", fake_wake)
        monkeypatch.setattr(ps, "wait_state_snapshot", fake_snapshot)
        await ps._wake_and_confirm("stream")
        # 注入前基线快照恰好一次，之后直接返回。
        assert snap_calls["count"] == 1


# ── 表演锁收窄后的会话隔离 ───────────────────────────────────


class TestSessionIsolation:
    """contextvars 会话参数在并发 Task 间隔离。"""

    async def test_concurrent_sessions_keep_own_params(self) -> None:
        from plugins.anima_chatter.vts.performer import (
            _SESSION_PARAMS,
            _SESSION_STARTED,
        )

        async def session(value: str) -> tuple[str, bool]:
            token_p = _SESSION_PARAMS.set(("happy", 1, value))
            token_s = _SESSION_STARTED.set(False)
            try:
                await asyncio.sleep(0.02)
                _SESSION_STARTED.set(True)
                await asyncio.sleep(0.02)
                params = _SESSION_PARAMS.get()
                assert params is not None
                return params[2], _SESSION_STARTED.get()
            finally:
                _SESSION_PARAMS.reset(token_p)
                _SESSION_STARTED.reset(token_s)

        results = await asyncio.gather(session("A"), session("B"), session("C"))
        assert results == [("A", True), ("B", True), ("C", True)]


# ── 会话收尾的活跃守卫 ───────────────────────────────


class TestSessionTeardownGuard:
    """交错播放下 A 会话退出不能关掉 B 会话的 speaking 状态。"""

    async def test_early_session_exit_keeps_count(self) -> None:
        from plugins.anima_chatter.vts.performer import VTSPerformer

        performer = VTSPerformer.__new__(VTSPerformer)
        performer._perform_lock = asyncio.Lock()
        performer._active_session_count = 0
        performer.speech_animator = None
        performer.auto_animator = None
        performer._expression_map = {}
        performer._active_expressions = set()

        async with performer.speaking_session(emotion="happy:1", intent="IDLE"):
            async with performer.speaking_session(emotion="sad:1", intent="NARRATING"):
                # 内层会话进行中，外层尚未退出。
                assert performer._active_session_count == 2
            # 外层已退出，内层仍在会话中——活跃计数不为 0，收尾不触发。
            assert performer._active_session_count == 1

        # 全部退出后才归零。
        assert performer._active_session_count == 0

    async def test_relinquish_returns_reserved_backlog(
        self, pipelining_section: _PipeliningFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """起播前取消只释放被取消项，不清除同轮其余 reservation。"""
        pipeline_state.configure(pipelining_section)
        monkeypatch.setattr(pipeline_state, "_states", {})
        reserved = await pipeline_state.reserve("stream", 8.0, track_id="t1")
        assert reserved is not None
        await pipeline_state.reserve("stream", 1.0, track_id="keep")
        await pipeline_state.relinquish(
            "stream", track_id="t1", reserved_until=reserved[1]
        )
        state = pipeline_state._states["stream"]
        assert set(state.reservations) == {"keep"}
        again = await pipeline_state.reserve("stream", 8.0, track_id="t2")
        assert again is not None


# ── 拒排反馈文案 ─────────────────────────────────────────────


class TestBackpressureFeedback:
    """超出容量的 reservation 返回拒排信号。"""

    async def test_reserve_returns_backpressure_signal(
        self, pipelining_section: _PipeliningFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline_state.configure(pipelining_section)
        monkeypatch.setattr(pipeline_state, "_states", {})
        await pipeline_state.reserve("stream", 8.0, track_id="t0")

        assert await pipeline_state.reserve("stream", 5.0, track_id="t1") is None


@pytest.fixture(autouse=True)
def _clean_pipeline_state() -> Iterator[None]:
    """每个测试前后清空 pipeline 全局状态。"""
    yield
    pipeline_state._states.clear()
