"""``VTＳConnection`` 独立事件循环线程的行为测试。

验证方案 A 的核心：VTS 的动画 / 发送 / 心跳循环跑在**独立线程的私有事件循环**
里，主程序阻塞不影响 worker 循环推进；对外桥接接口（connect / close /
trigger_hotkey / set_expression）兼容。
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.anima_chatter.audio import AudioPlayer
from plugins.anima_chatter.config import AnimaChatterConfig
from plugins.anima_chatter.vts.animation.auto import AutoAnimator
from plugins.anima_chatter.vts.animation.base import BaseAnimator
from plugins.anima_chatter.vts.animation.body import BodyControl
from plugins.anima_chatter.vts.animation.speech import SpeechAnimator
from plugins.anima_chatter.vts.connection import VTSConnection
from plugins.anima_chatter.vts.performer import VTSPerformer


@pytest.fixture
def conn() -> "VTSConnection":
    """构造一个不真正连 VTS 的连接实例。"""
    return VTSConnection(host="127.0.0.1", port=8001)


def test_worker_loop_self_heals_after_close(conn: "VTSConnection") -> None:
    """验证循环在 close 回收后可再次启动（自愈）。"""
    loop = conn._ensure_worker_loop()
    assert loop is not None
    assert not loop.is_closed()
    first_loop = conn._worker_loop

    # 回收后再次获取应得到新的可用循环（线程重启）。
    conn._shutdown_worker()
    loop2 = conn._ensure_worker_loop()
    assert loop2 is not None
    assert not loop2.is_closed()
    assert loop2 is not first_loop
    conn._shutdown_worker()


def test_run_coro_crosses_thread(conn: "VTSConnection") -> None:
    """验证 _run_coro 能把协程投递到 worker 循环并拿到结果。"""
    result = conn._run_coro(_add_async(2, 3), timeout=5.0)
    assert result == 5
    conn._shutdown_worker()


def test_worker_pushes_while_main_blocked(conn: "VTSConnection") -> None:
    """方案 A 核心：主线程阻塞时，worker 循环仍按计划推进。

    在主线程 sleep 阻塞 0.5s 期间，worker 循环里用一个定时器累加计数；
    验收 worker 循环未被主线程阻塞拖住，计数仍在推进。
    """
    loop = conn._ensure_worker_loop()
    counter: dict[str, int] = {"n": 0}

    def _tick() -> None:
        counter["n"] += 1

    # 在 worker 循环里每 0.02s 调度一次 tick，持续 0.6s。
    stop_event = threading.Event()

    async def _schedule() -> None:
        while not stop_event.is_set():
            loop.call_later(0.02, _tick)
            await asyncio.sleep(0.02)

    fut = asyncio.run_coroutine_threadsafe(_schedule(), loop)

    # 主线程阻塞 0.5s（模拟主程序卡顿）。
    time.sleep(0.5)
    stop_event.set()
    fut.result(timeout=2.0)

    # 0.5s / 0.02s ≈ 25 次；允许调度延迟留足余量，应远大于"完全被阻塞"的 0~1 次。
    assert counter["n"] >= 10, f"worker 循环被主线程阻塞拖住: n={counter['n']}"
    conn._shutdown_worker()


def test_trigger_hotkey_returns_false_when_not_connected(
    conn: "VTSConnection",
) -> None:
    """未连接时 trigger_hotkey / set_expression 应安全返回 False。"""

    async def _probe() -> None:
        assert await conn.trigger_hotkey("some_hotkey") is False
        assert await conn.set_expression("expr.exp3.json", True) is False

    asyncio.run(_probe())
    conn._shutdown_worker()


async def _add_async(a: int, b: int) -> int:
    """一个简单的可跨线程投递的协程，用于验证桥接。"""
    return a + b


async def test_body_control_uses_worker_without_waiting_for_speech(
    conn: VTSConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """语音播放锁被占用时仍在 VTS 工作线程接受独立控制。"""
    animator = AutoAnimator()
    conn.animators = [animator]
    conn._ensure_worker_loop()
    conn.is_connected = True
    performer = VTSPerformer(
        plugin_config=AnimaChatterConfig(), audio_player=cast(AudioPlayer, object())
    )
    performer.connection = conn
    performer.auto_animator = animator
    performer.speech_animator = SpeechAnimator()
    performer._initialized = True
    applied_in: list[int] = []
    original_apply = animator.apply_body_control

    def record_apply(control: BodyControl) -> None:
        """记录真实动画状态变更所属线程。"""
        applied_in.append(threading.get_ident())
        original_apply(control)

    monkeypatch.setattr(animator, "apply_body_control", record_apply)
    control = BodyControl.model_validate({"left_eye": {"openness": 0.0}})
    try:
        async with performer._perform_lock:
            assert await asyncio.wait_for(performer.apply_body_control(control), 1.0)
        assert conn._worker_thread is not None
        assert applied_in == [conn._worker_thread.ident]
        assert applied_in[0] != threading.get_ident()
        async with performer.speaking_session():
            await performer.start_speech_playback()
            assert animator.is_performing
        assert not animator.is_performing
        assert animator.eyelids.left.mode == "manual"
    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_eye", "new_eye", "expected_mode", "target"),
    [
        ({"release": True}, {"openness": 0.25}, "manual", 0.25),
        ({"openness": 0.1}, {"release": True}, "release", 1.0),
        (
            {"openness": 0.1, "hold_seconds": 1.0},
            {"openness": 0.25, "hold_seconds": None},
            "manual",
            0.25,
        ),
        (
            {"openness": 0.1, "hold_seconds": 0.1, "transition_seconds": 2.0},
            {"openness": 0.25, "transition_seconds": 0.2},
            "manual",
            0.25,
        ),
    ],
)
async def test_worker_filters_stale_body_fields_independently(
    conn: VTSConnection,
    old_eye: dict[str, object],
    new_eye: dict[str, object],
    expected_mode: str,
    target: float,
) -> None:
    """较旧字段不覆盖新值，未冲突字段仍在真实 worker 应用。"""
    animator = AutoAnimator()
    conn.animators = [animator]
    conn._ensure_worker_loop()
    conn.is_connected = True
    performer = VTSPerformer(
        plugin_config=AnimaChatterConfig(), audio_player=cast(AudioPlayer, object())
    )
    performer.connection = conn
    performer.auto_animator = animator
    performer.speech_animator = SpeechAnimator()
    performer._initialized = True
    try:
        stale_revision = performer.reserve_body_control()
        assert await performer.apply_body_control(
            BodyControl.model_validate(
                {"breath": {"frequency_hz": 0.5}, "left_eye": new_eye}
            )
        )
        stale_control = BodyControl.model_validate(
            {
                "breath": {"frequency_hz": 0.2},
                "left_eye": old_eye,
                "right_eye": {"openness": 0.35},
            }
        )
        assert await performer.apply_body_control(
            stale_control, revision=stale_revision
        )
        assert animator.breathing.target_frequency == pytest.approx(0.5)
        assert animator.eyelids.left.mode == expected_mode
        assert animator.eyelids.left.motion.target == target
        assert animator.eyelids.left.hold_seconds is None
        assert animator.eyelids.right.mode == "manual"
        assert animator.eyelids.right.motion.target == pytest.approx(0.35)
        if "transition_seconds" in new_eye:
            assert animator.eyelids.left.motion.duration == new_eye["transition_seconds"]
        if expected_mode == "manual":
            await asyncio.wrap_future(conn._submit(animator.update(1.5)))
            assert animator.eyelids.left.mode == "manual"
            assert animator.eyelids.left.motion.value == pytest.approx(target)
    finally:
        await conn.close()


async def test_animation_loop_preserves_long_frame_time(
    conn: VTSConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接层将 200ms 实际间隔原样交付动画器，不先截成 100ms。"""
    from plugins.anima_chatter.vts import connection

    clock = iter([0.0, 0.2])
    loop = SimpleNamespace(time=lambda: next(clock))
    monkeypatch.setattr(connection.asyncio, "get_event_loop", lambda: loop)
    monkeypatch.setattr(connection.asyncio, "sleep", AsyncMock())
    conn.is_connected = True
    conn.vts = object()

    async def stop_after_frame(delta: float) -> dict[str, float]:
        """记录一个完整帧间隔后结束离线循环。"""
        assert delta == pytest.approx(0.2)
        conn._worker_stop.set()
        return {}

    animate = AsyncMock(side_effect=stop_after_frame)
    monkeypatch.setattr(conn, "_animate_frame", animate)
    await conn._animation_loop()
    animate.assert_awaited_once()


@pytest.mark.parametrize("auto_first", [True, False])
async def test_eye_openness_is_not_added_to_other_animations(
    conn: VTSConnection,
    auto_first: bool,
) -> None:
    """其他动画的眼部偏移不能相加或覆盖绝对开度，头部仍正常叠加。"""
    animator = AutoAnimator()
    animator.apply_body_control(
        BodyControl.model_validate(
            {"left_eye": {"openness": 0.25, "transition_seconds": 0.02}}
        )
    )
    other = cast(
        BaseAnimator,
        MagicMock(
            spec=BaseAnimator,
            is_active=True,
            update=AsyncMock(return_value={"v_eye_left": 0.9, "v_head_x": 2.0}),
        ),
    )
    conn.animators = [animator, other] if auto_first else [other, animator]
    output = await conn._animate_frame(0.03)
    assert output["v_eye_left"] == pytest.approx(0.25)
    assert "v_head_x" in output


__all__: list[str] = []
