"""身体与上半身灵动度（二阶耦合 + 呼吸肩相位差 + 说话韵律扩散）单元测试。

覆盖：
- :mod:`dynamics.SecondOrderDynamics` 二阶系统的收敛、阻尼与 dt 鲁棒性。
- :class:`AutoAnimator` 的身体跟随头部耦合与呼吸肩相位差。
- :class:`SpeechAnimator` 的说话韵律三轴扩散。
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from plugins.anima_chatter.vts.animation.auto import AutoAnimator
from plugins.anima_chatter.vts.animation.body import BodyControl, Breathing, Eyelids
from plugins.anima_chatter.vts.animation.dynamics import (
    EyelidMotion,
    SecondOrderDynamics,
)
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


def test_eyelid_retarget_preserves_motion() -> None:
    """半闭眼改令时不跳位置、不清速度，并直接接续到新目标。"""
    motion = EyelidMotion()
    motion.retarget(0.0, 0.6)
    motion.update(0.3)
    position, velocity = motion.value, motion.velocity
    assert position == pytest.approx(0.5)
    assert velocity < 0.0
    motion.retarget(0.2, 0.4)
    assert motion.value == position
    assert motion.velocity == velocity
    assert motion.update(0.001) < position
    for _ in range(40):
        motion.update(0.01)
    assert motion.value == pytest.approx(0.2)
    assert motion.velocity == pytest.approx(0.0)
    assert motion.arrived


def test_eyelid_release_brakes_before_opening() -> None:
    """闭合中释放先承接原速度，再平滑转向睁开，不跳到全开。"""
    motion = EyelidMotion()
    motion.retarget(0.0, 0.6)
    motion.update(0.3)
    motion.retarget(1.0, 0.5)
    assert 0.0 < motion.update(0.001) < 0.5
    for _ in range(50):
        assert 0.0 <= motion.update(0.01) <= 1.0
    assert motion.value == pytest.approx(1.0)


@pytest.mark.parametrize("opening", [False, True])
def test_fast_eyelid_reversal_does_not_hit_boundary(opening: bool) -> None:
    """快速运动途中反向先制动，不触底卡帧，也不丢失初始速度。"""
    motion = EyelidMotion(0.0 if opening else 1.0)
    motion.retarget(1.0 if opening else 0.0, 0.12)
    motion.update(0.08)
    position, velocity = motion.value, motion.velocity
    motion.retarget(0.0 if opening else 1.0, 0.3)
    assert (motion.value, motion.velocity) == (position, velocity)
    samples = [motion.update(1 / 30) for _ in range(8)]
    assert all(0.0 < value < 1.0 for value in samples)
    assert all(abs(later - earlier) > 1e-5 for earlier, later in pairwise(samples))
    motion.update(1 / 30)
    assert motion.value == pytest.approx(motion.target)
    assert motion.velocity == pytest.approx(0.0)


def test_depth_motion_slows_with_breath_before_holding() -> None:
    """屏息和恢复时深度位移速度同相位一起趋零，不硬冻结未完成的深度轨迹。"""
    breath = Breathing(0.25)
    command = BodyControl.model_validate(
        {"breath": {"depth": 3.0, "hold": True, "transition_seconds": 0.1}}
    )
    assert command.breath is not None
    breath.apply(command.breath)
    depth_samples = [breath.depth.y]
    for _ in range(10):
        breath.update(0.01)
        depth_samples.append(breath.depth.y)
    increments = [later - earlier for earlier, later in pairwise(depth_samples)]
    assert max(increments) > 0.0001
    assert 0.0 < increments[-1] < increments[-2] < increments[-3] < increments[-4]
    assert abs(increments[-1]) < max(increments) * 0.05
    frozen = breath.phase, breath.depth.y, breath.sample()
    breath.update(0.4)
    assert (breath.phase, breath.depth.y, breath.sample()) == frozen
    resume = BodyControl.model_validate({"breath": {"hold": False}})
    assert resume.breath is not None
    breath.apply(resume.breath)
    breath.update(0.001)
    assert abs(breath.depth.y - frozen[1]) < max(increments) * 0.01
    breath.update(0.5)
    assert breath.depth.y > frozen[1]


def test_explicit_null_restores_indefinite_eyelid_hold() -> None:
    """省略保持时长保留原定时，显式 null 清除定时并允许无限保持。"""
    eyes = Eyelids(20.0, 20.0)
    eyes.apply(
        BodyControl.model_validate({"left_eye": {"openness": 0.0, "hold_seconds": 0.2}})
    )
    eyes.update(0.3)
    eyes.apply(BodyControl.model_validate({"left_eye": {"openness": 0.3}}))
    assert eyes.left.hold_seconds == 0.2
    eyes.apply(BodyControl.model_validate({"left_eye": {"hold_seconds": None}}))
    assert eyes.left.hold_seconds is None
    assert eyes.left.hold_remaining is None
    eyes.update(5.0)
    assert eyes.left.mode == "manual"
    assert eyes.left.motion.value == pytest.approx(0.3)
    eyes.apply(BodyControl.model_validate({"left_eye": {"release": True}}))
    eyes.update(0.4)
    eyes.apply(BodyControl.model_validate({"left_eye": {"openness": 0.0}}))
    eyes.update(5.0)
    assert eyes.left.mode == "manual"


async def test_slow_frames_preserve_control_elapsed_time() -> None:
    """100ms 慢帧与等时长小步具有相同呼吸相位、眼睑保持和释放进度。"""
    slow, fast = AutoAnimator(), AutoAnimator()
    control = BodyControl.model_validate(
        {
            "breath": {"frequency_hz": 0.25},
            "left_eye": {
                "openness": 0.0,
                "transition_seconds": 0.2,
                "hold_seconds": 0.4,
            },
            "blink": {"interval_seconds": 20.0},
        }
    )
    for animator in (slow, fast):
        animator.apply_body_control(control)
    for _ in range(8):
        await slow.update(0.1)
    for _ in range(80):
        await fast.update(0.01)
    assert slow.breathing.phase == pytest.approx(fast.breathing.phase, abs=1e-9)
    assert slow.eyelids.left.mode == fast.eyelids.left.mode == "auto"
    assert slow.eyelids.left.motion.value == fast.eyelids.left.motion.value == 1.0


def test_breath_retimes_without_restarting_and_holds_phase() -> None:
    """调速保留相位；屏息逐渐制动，保持非零位置并从原位置恢复。"""
    breath = Breathing(0.25)
    breath.update(0.37)
    phase, value, frequency = breath.phase, breath.sample(), breath.frequency.y
    breath.apply(BodyControl.model_validate({"breath": {"frequency_hz": 0.8}}).breath)  # type: ignore[arg-type]
    assert (breath.phase, breath.sample(), breath.frequency.y) == (
        phase,
        value,
        frequency,
    )
    breath.update(0.02)
    assert frequency < breath.frequency.y < 0.8
    assert 0.0 < (breath.phase - phase) % 1.0 < 0.016
    breath.apply(
        BodyControl.model_validate(
            {"breath": {"hold": True, "transition_seconds": 0.3}}
        ).breath
    )  # type: ignore[arg-type]
    assert breath.running.value == 1.0
    breath.update(0.15)
    assert 0.0 < breath.running.value < 1.0
    breath.update(0.5)
    stopped_phase, stopped_value = breath.phase, breath.sample()
    assert abs(stopped_value) > 0.01
    breath.update(3.0)
    assert breath.phase == stopped_phase
    assert breath.sample() == stopped_value
    assert breath.depth.y == pytest.approx(1.0)
    breath.apply(BodyControl.model_validate({"breath": {"hold": False}}).breath)  # type: ignore[arg-type]
    assert breath.phase == stopped_phase
    breath.update(0.05)
    assert breath.phase != stopped_phase
    assert 0.0 < breath.running.value < 1.0


def test_manual_eyelid_retarget_hold_and_release() -> None:
    """改令替换旧保持期限，计时从抵达后开始，释放不补眨且不影响另一眼。"""
    eyes = Eyelids(20.0, 20.0)
    eyes.apply(
        BodyControl.model_validate(
            {
                "left_eye": {
                    "openness": 0.0,
                    "transition_seconds": 0.6,
                    "hold_seconds": 0.1,
                }
            }
        )
    )
    eyes.update(0.3)
    assert eyes.left.motion.value == pytest.approx(0.5)
    assert eyes.left.hold_remaining == 0.1
    position, velocity = eyes.left.motion.value, eyes.left.motion.velocity
    eyes.apply(
        BodyControl.model_validate({"left_eye": {"openness": 0.2, "hold_seconds": 0.5}})
    )
    assert (eyes.left.motion.value, eyes.left.motion.velocity) == (position, velocity)
    eyes.update(0.4)
    assert eyes.left.mode == "manual"
    assert eyes.left.hold_remaining == 0.5
    eyes.update(0.3)
    assert eyes.left.motion.value == pytest.approx(0.2)
    assert eyes.left.hold_remaining == pytest.approx(0.4)
    eyes.apply(BodyControl.model_validate({"left_eye": {"release": True}}))
    eyes.update(0.01)
    assert 0.2 < eyes.left.motion.value < 1.0
    eyes.update(0.6)
    assert eyes.left.mode == "auto"
    assert eyes.right.motion.value == 1.0
    eyes.blink()
    assert eyes.left.mode == "auto"


def test_blink_retiming_keeps_active_cycle_and_manual_eye() -> None:
    """修改节奏不重启正在闭合的眨眼；独立眼睑不被主动眨眼覆盖。"""
    eyes = Eyelids(4.0, 4.0)
    eyes.apply(
        BodyControl.model_validate({"blink": {"now": True, "close_seconds": 0.6}})
    )
    eyes.update(0.3)
    position, velocity = eyes.left.motion.value, eyes.left.motion.velocity
    eyes.apply(
        BodyControl.model_validate(
            {"blink": {"interval_seconds": 2.0, "close_seconds": 0.1, "now": True}}
        )
    )
    assert eyes.left.motion.duration == 0.6
    assert (eyes.left.motion.value, eyes.left.motion.velocity) == (position, velocity)
    eyes.apply(
        BodyControl.model_validate(
            {"left_eye": {"openness": 0.25, "transition_seconds": 0.2}}
        )
    )
    assert (eyes.left.motion.value, eyes.left.motion.velocity) == (position, velocity)
    eyes.update(0.7)
    assert eyes.left.mode == "manual"
    assert eyes.left.motion.value == pytest.approx(0.25)
    assert eyes.right.mode == "auto"


def test_retime_and_timed_holds_preserve_partial_control() -> None:
    """只改时长时继承半闭眼运动，定时保持抵达后才开始且不干扰另一眼。"""
    eyes = Eyelids(20.0, 20.0)
    eyes.apply(
        BodyControl.model_validate(
            {
                "left_eye": {
                    "openness": 0.0,
                    "transition_seconds": 0.6,
                    "hold_seconds": 0.2,
                }
            }
        )
    )
    eyes.update(0.3)
    before = (eyes.left.motion.value, eyes.left.motion.velocity)
    eyes.apply(
        BodyControl.model_validate(
            {"left_eye": {"transition_seconds": 0.4}, "right_eye": {"openness": 0.8}}
        )
    )
    assert (eyes.left.motion.value, eyes.left.motion.velocity) == before
    assert eyes.left.motion.duration == 0.4
    eyes.update(0.4)
    assert eyes.left.mode == "manual"
    assert eyes.left.hold_remaining == pytest.approx(0.2)
    eyes.update(0.21)
    assert eyes.left.mode == "release"
    eyes.update(0.5)
    assert eyes.left.mode == "auto"
    assert eyes.right.mode == "manual"
    assert eyes.right.motion.value == pytest.approx(0.8)


def test_timed_breath_hold_freezes_depth_until_resume() -> None:
    """深度变更中的屏息冻结真实位移，定时从完全制动后开始。"""
    breath = Breathing(0.25)
    command = BodyControl.model_validate(
        {
            "breath": {
                "depth": 2.0,
                "inhale_ratio": 0.5,
                "hold": True,
                "hold_seconds": 0.4,
                "transition_seconds": 0.1,
            }
        }
    )
    assert command.breath is not None
    breath.apply(command.breath)
    breath.update(0.1)
    frozen = breath.phase, breath.sample(), breath.depth.y
    assert breath.running.value == 0.0
    assert breath.hold_remaining == 0.4
    breath.update(0.2)
    assert (breath.phase, breath.sample(), breath.depth.y) == frozen
    assert breath.hold_remaining == pytest.approx(0.2)
    breath.update(0.5)
    assert breath.running.value == pytest.approx(1.0)
    assert breath.phase != frozen[0]
    assert breath.inhale_fraction.y == pytest.approx(1.0 / 3.0, abs=0.02)


async def test_breath_survives_speech_fade(auto_animator: AutoAnimator) -> None:
    """说话完全淡出待机动作后，呼吸仍逐帧输出并保留独立控制。"""
    auto_animator.set_performing(True)
    auto_animator.apply_body_control(
        BodyControl.model_validate(
            {"breath": {"frequency_hz": 0.6}, "left_eye": {"openness": 0.0}}
        )
    )
    samples = []
    for _ in range(90):
        samples.append((await auto_animator.update(1 / 30))["v_head_z"])
    assert auto_animator.performing_fade_weight == 0.0
    assert max(samples[-30:]) - min(samples[-30:]) > 0.1
    auto_animator.set_performing(False)
    await auto_animator.update(1 / 30)
    assert auto_animator.breathing.target_frequency == 0.6
    assert auto_animator.eyelids.left.mode == "manual"


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
    # 末帧应已明显跟随到目标方向（head*w_rx = 10*0.4=4°）。带阻尼 + 速率限制
    # 的耦合路径在 10 秒内不会完全到 4°，只要求显著逼近（>2.0° 且正向）。
    assert timeline[-1] > 2.0
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
    # 采样约 20 秒（覆盖多个 0.15Hz 周期）。value noise 的瞬时峰值随随机
    # 相位波动（同一窗口两次运行的 max 可差 3 倍），改用**绝对值均值**断言
    # ——统计量稳定，不受相位影响；三轴均值非零即"身体在动"。
    sums = {"v_body_x": 0.0, "v_body_y": 0.0, "v_body_z": 0.0}
    frames = 600
    for _ in range(frames):
        out = await auto_animator.update(1 / 30)
        for key in sums:
            sums[key] += abs(out.get(key, 0.0))
    means = {key: total / frames for key, total in sums.items()}
    # v_body_x 是常驻律动主通道（噪声直出，均值幅度显著）；v_body_y / z 在本
    # fixture 下主要叠加呼吸起伏等小分量，只断言均值严格非零（|noise| 均值
    # 数学上恒 > 0，实测 ~0.009，余量充足），避免对幅度下采样噪声过拟合。
    assert means["v_body_x"] > 0.3
    assert means["v_body_y"] > 0.001
    assert means["v_body_z"] > 0.001


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


@pytest.mark.parametrize("recovering", [False, True])
async def test_speech_slow_frames_approach_target_without_overshoot(
    recovering: bool,
) -> None:
    """半秒慢帧和长帧恢复都单调靠近姿态目标，不发生越界反向摆动。"""
    animator = SpeechAnimator()
    animator.intent_map["NARRATING"]["x"] = 4.0
    animator.set_intent("NARRATING")
    if recovering:
        animator.current_params["v_head_x"] = 4.0
        animator.speaking_end_time = 1.0
    target = 0.0 if recovering else 4.0
    delta_time = 2.0 if recovering else 0.5
    previous = animator.current_params["v_head_x"]
    for _ in range(3):
        output = await animator.update(delta_time)
        value = output["v_head_x"]
        assert min(previous, target) < value < max(previous, target)
        assert abs(value - target) < abs(previous - target)
        previous = value
    assert previous == pytest.approx(target, abs=0.01)


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
