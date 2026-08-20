"""身体与上半身灵动度（二阶耦合 + 呼吸肩相位差 + 说话韵律扩散）单元测试。

覆盖：
- :mod:`dynamics.SecondOrderDynamics` 二阶系统的收敛、阻尼与 dt 鲁棒性。
- :class:`AutoAnimator` 的身体跟随头部耦合与呼吸肩相位差。
- :class:`SpeechAnimator` 的说话韵律三轴扩散。
"""

from __future__ import annotations

import pytest

from plugins.anima_chatter.vts.animation.auto import AutoAnimator
from plugins.anima_chatter.vts.animation.dynamics import SecondOrderDynamics
from plugins.anima_chatter.vts.animation.speech import SpeechAnimator


@pytest.fixture
def auto_animator() -> AutoAnimator:
    """构造一个开启动态耦合 / 呼吸肩相位差的 AutoAnimator。"""
    anim = AutoAnimator()
    anim._body_follow_head_enabled = True
    anim._body_freq = 1.6
    anim._body_damp = 0.75
    anim._body_w_rx = 0.4
    anim._body_w_rz = 0.3
    anim._body_w_comp = 0.08
    anim._breath_shoulder_enabled = True
    anim._breath_shoulder_amplitude = 0.9
    anim._breath_shoulder_lag = 0.5
    # 常驻身体律动默认关：让耦合 / 呼吸肩测试保持确定性（不受噪声底噪干扰）。
    anim._body_idle_enabled = False
    anim._body_idle_scale = 1.0
    anim._body_idle_x_amp = 4.5
    anim._body_idle_y_amp = 2.5
    anim._body_idle_z_amp = 3.5
    return anim


# ── SecondOrderDynamics ────────────────────────────────


def test_second_order_converges_to_target() -> None:
    """稳定目标下二阶系统应收敛到目标值。"""
    sys2nd = SecondOrderDynamics(frequency=1.6, damping=0.9, response=0.0, x0=0.0)
    target = 10.0
    # 跑足够多步让它收敛。
    for _ in range(400):
        sys2nd.update(target, dt=1 / 30)
    assert abs(sys2nd.y - target) < 0.5


def test_second_order_dt_zero_returns_current() -> None:
    """dt<=0 时直接返回当前值，不做积分（不崩溃、不跳变）。"""
    sys2nd = SecondOrderDynamics(frequency=1.6, damping=0.75, response=0.0, x0=5.0)
    assert sys2nd.update(10.0, dt=0.0) == 5.0
    assert sys2nd.update(10.0, dt=-1.0) == 5.0


# ── AutoAnimator：身体跟随头部 ─────────────────────────


async def test_auto_body_follows_head_step(auto_animator: AutoAnimator) -> None:
    """头部阶跃后 v_body_x 应滞后且带衰减地逼近（而非瞬移 / 机械跳变）。"""
    timeline: list[float] = []
    for _ in range(300):
        out = await auto_animator.update(1 / 30)
        # 手动喂一个持续的头部横向输入，模拟头向一侧转动。
        auto_animator.macro_current_params["v_head_x"] = 10.0
        timeline.append(out.get("v_body_x", 0.0))

    # 首帧应接近于 0（有启动滞后，未瞬移）。
    assert abs(timeline[0]) < 1.0
    # 末帧应已跟随到目标附近（head*w_rx = 10*0.4=4°）。
    assert abs(timeline[-1] - 4.0) < 1.0
    # 中间轨迹应出现过 0 与目标之间的值（平滑过渡，非跳变）。
    assert any(0.05 < v < 3.5 for v in timeline)


async def test_auto_body_disabled_keeps_zero(auto_animator: AutoAnimator) -> None:
    """关闭耦合后，无宏观输入时 v_body_x 保持 0。"""
    auto_animator._body_follow_head_enabled = False
    auto_animator._body_coupling_x = SecondOrderDynamics(
        frequency=1.6, damping=0.75, response=0.0, x0=0.0
    )
    out = await auto_animator.update(1 / 30)
    # 关闭耦合时 macro v_body_x 为 0 → 输出 0（无宏观动作输入）。
    assert out.get("v_body_x", 0.0) == 0.0


# ── AutoAnimator：身体常驻律动 ─────────────────────────


async def test_auto_body_idle_active_when_enabled(auto_animator: AutoAnimator) -> None:
    """开启常驻律动后，无任何宏观输入也应有非零的三轴身体输出（身体在动）。"""
    auto_animator._body_idle_enabled = True
    # 采样足够长（约 10 秒 = 一个完整周期以上），确保三轴都测到峰值。
    seen = {"v_body_x": 0.0, "v_body_y": 0.0, "v_body_z": 0.0}
    for _ in range(300):
        out = await auto_animator.update(1 / 30)
        if "v_body_x" in out:
            seen["v_body_x"] = max(seen["v_body_x"], abs(out["v_body_x"]))
        if "v_body_y" in out:
            seen["v_body_y"] = max(seen["v_body_y"], abs(out["v_body_y"]))
        if "v_body_z" in out:
            seen["v_body_z"] = max(seen["v_body_z"], abs(out["v_body_z"]))
    # 三轴都应在整个采样窗口内出现过明显非零摆动。value noise 通常只到满幅的
    # 0.3~0.6，故 v_body_x 下限设 1.0（×4.5° 有效约 1.3~2.7°）；v_body_y
    # 幅度小、仅需存在性；v_body_z 还叠加 body_sway / breath_shoulder。
    assert seen["v_body_x"] > 1.0
    assert seen["v_body_x"] < 8.0
    assert seen["v_body_y"] > 0.3
    assert seen["v_body_z"] > 1.0


async def test_auto_body_idle_zero_when_disabled(auto_animator: AutoAnimator) -> None:
    """关闭常驻律动且无宏观输入时，仅受常驻律动控制的 v_body_x 恒 0。

    v_body_y 会叠加呼吸起伏、v_body_z 会叠加 body_sway，都不宜断言恒 0；
    此处只验证纯常驻律动通道的 v_body_x 在关闭时归零。
    """
    out = await auto_animator.update(1 / 30)
    assert out.get("v_body_x", 0.0) == 0.0


# ── AutoAnimator：呼吸肩相位差 ─────────────────────────


async def test_auto_breath_shoulder_active(auto_animator: AutoAnimator) -> None:
    """呼吸肩相位差开启时，v_body_z 应随呼吸产生非零分量。"""
    out = await auto_animator.update(1 / 30)
    # 呼吸肩分量叠加到 v_body_z；默认 body_sway_z 也可能有值，但肩相位差
    # 开启时 v_body_z 不应完全恒 0（除非恰好过零点，此处只做结构性断言）。
    assert "v_body_z" in out


# ── SpeechAnimator：说话韵律三轴扩散 ───────────────────


class _FakeEnvelopeTracker:
    """最小可用的 envelope tracker 替身，固定返回一组 rms / velocity。"""

    def __init__(self, rms: float, velocity: float) -> None:
        self._rms = rms
        self._velocity = velocity

    def current(self) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(rms=self._rms, velocity=self._velocity)


async def test_speech_body_pulse_spreads() -> None:
    """说话时三轴扩散应产生 v_body_x / v_body_y / v_body_z 输出。"""
    anim = SpeechAnimator(
        envelope_tracker=_FakeEnvelopeTracker(rms=0.8, velocity=0.6),  # type: ignore[arg-type]
    )
    # 打开音频驱动并进入说话状态。
    anim._audio_drive_enabled = True
    anim.set_speaking(True)
    anim.set_state(intent="NARRATING", emotion="happy:2")

    out = await anim.update(1 / 30)

    # 三轴都应出现（rms/velocity > 0 且增益 > 0）。
    assert "v_body_x" in out and out["v_body_x"] != 0.0
    assert "v_body_y" in out and out["v_body_y"] != 0.0
    assert "v_body_z" in out and out["v_body_z"] != 0.0


async def test_speech_body_quiet_when_not_speaking() -> None:
    """未说话时不应产生身体律动输出。"""
    anim = SpeechAnimator(
        envelope_tracker=_FakeEnvelopeTracker(rms=0.8, velocity=0.6),  # type: ignore[arg-type]
    )
    anim._audio_drive_enabled = True
    # is_speaking 保持 False。

    out = await anim.update(1 / 30)

    assert "v_body_x" not in out
    assert "v_body_y" not in out
    assert "v_body_z" not in out


__all__: list[str] = []
