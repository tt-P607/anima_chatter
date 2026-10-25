"""呼吸、眨眼和独立眼睑的目标参数与逐帧状态。"""

from __future__ import annotations

import math
import random
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .dynamics import EyelidMotion, SecondOrderDynamics


class _ControlPatch(BaseModel):
    """只接受已声明的有限数值，省略字段表示保留原控制。"""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_change(self) -> Self:
        """拒绝没有任何目标或节奏的空指令。"""
        if not self.model_dump(exclude_none=True):
            raise ValueError("至少指定一个控制字段")
        return self


class BreathControl(_ControlPatch):
    """呼吸节奏与相位暂停控制。"""

    frequency_hz: float | None = Field(
        None, ge=0.05, le=2.0, description="呼吸频率，Hz；渐变到目标，不重置周期"
    )
    depth: float | None = Field(
        None,
        ge=0.0,
        le=3.0,
        description="相对配置呼吸幅度的倍率；屏息请用 hold，不要用深度归零",
    )
    inhale_ratio: float | None = Field(
        None, ge=0.2, le=5.0, description="吸气时长/呼气时长，例如 0.5 表示吸一呼二"
    )
    hold: bool | None = Field(
        None, description="true 减速后屏息；false 从当前位置恢复呼吸"
    )
    hold_seconds: float | None = Field(
        None,
        ge=0.0,
        le=60.0,
        description="完全停住后的屏息秒数；省略则保持，需配合 hold=true",
    )
    transition_seconds: float | None = Field(
        None, ge=0.05, le=10.0, description="屏息减速或恢复的过渡秒数，初值 0.5"
    )

    @model_validator(mode="after")
    def validate_hold(self) -> Self:
        """定时屏息必须明确要求暂停。"""
        if self.hold_seconds is not None and self.hold is not True:
            raise ValueError("hold_seconds 需要 hold=true")
        return self


class BlinkControl(_ControlPatch):
    """下一次眨眼的节奏，不修改正在进行的眨眼。"""

    interval_seconds: float | None = Field(
        None, ge=0.2, le=60.0, description="两次眨眼间隔秒数，保留当前等待进度"
    )
    close_seconds: float | None = Field(
        None, ge=0.02, le=5.0, description="闭合时长，秒"
    )
    hold_seconds: float | None = Field(
        None, ge=0.0, le=10.0, description="完全闭合后停留秒数"
    )
    open_seconds: float | None = Field(
        None, ge=0.02, le=5.0, description="睁开时长，秒"
    )
    now: bool | None = Field(
        None, description="主动眨一次；不打断当前眨眼，不覆盖独立眼睑控制，也不排队补眨"
    )


class EyelidControl(_ControlPatch):
    """单侧眼睑的绝对开度、到达后保持与平滑释放。"""

    openness: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="绝对开度：0 闭眼，1 全开；中途改令继承位置和速度",
    )
    transition_seconds: float | None = Field(
        None, ge=0.02, le=10.0, description="到达新目标或释放的秒数，初值 0.3"
    )
    hold_seconds: float | None = Field(
        None,
        ge=0.0,
        le=120.0,
        description="到达目标后保持秒数，计满自然释放；省略保留原设置，显式 null 清除定时并持续保持",
    )
    release: bool | None = Field(
        None,
        description="true 从当前位置自然恢复自动睁眼；与 openness/hold_seconds 互斥",
    )

    @model_validator(mode="after")
    def require_change(self) -> Self:
        """显式空保持时长表示清除定时，其余空补丁仍拒绝。"""
        if "hold_seconds" in self.model_fields_set:
            return self
        return super().require_change()

    @model_validator(mode="after")
    def validate_release(self) -> Self:
        """释放和设定保持目标不能同时出现。"""
        if self.release and (
            self.openness is not None or "hold_seconds" in self.model_fields_set
        ):
            raise ValueError("release 不能与 openness 或 hold_seconds 同时指定")
        return self


class BodyControl(_ControlPatch):
    """同一套可供独立 Action 和语音起播使用的身体控制补丁。"""

    breath: BreathControl | None = None
    blink: BlinkControl | None = None
    left_eye: EyelidControl | None = None
    right_eye: EyelidControl | None = None


def inject_body_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """向 Action 参数注入与运行时校验一致的身体控制 JSON Schema。"""
    body_schema = BodyControl.model_json_schema()
    parameters = schema["function"]["parameters"]
    parameters.setdefault("$defs", {}).update(body_schema.pop("$defs", {}))
    parameters["properties"]["body"] = body_schema
    return schema


class Breathing:
    """以累计相位驱动呼吸，相位和深度共用平滑制动的运动时钟。"""

    def __init__(self, frequency: float) -> None:
        """使用配置中的基础频率，初始位于吸气中点。"""
        self.phase = 0.25
        self.frequency = SecondOrderDynamics(1.0, 1.0, 0.0, frequency)
        self.depth = SecondOrderDynamics(1.0, 1.0, 0.0, 1.0)
        self.inhale_fraction = SecondOrderDynamics(1.0, 1.0, 0.0, 0.5)
        self.target_frequency = frequency
        self.target_depth = 1.0
        self.target_inhale_fraction = 0.5
        self.running = EyelidMotion()
        self.transition_seconds = 0.5
        self.hold_remaining: float | None = None

    def apply(self, control: BreathControl) -> None:
        """仅替换指定的呼吸目标，不重建振荡器或归零幅度。"""
        if control.frequency_hz is not None:
            self.target_frequency = control.frequency_hz
        if control.depth is not None:
            self.target_depth = control.depth
        if control.inhale_ratio is not None:
            self.target_inhale_fraction = control.inhale_ratio / (
                1.0 + control.inhale_ratio
            )
        if control.transition_seconds is not None:
            self.transition_seconds = control.transition_seconds
            if control.hold is None and not self.running.arrived:
                self.running.retarget(self.running.target, self.transition_seconds)
        if control.hold is not None:
            target = 0.0 if control.hold else 1.0
            if target != self.running.target:
                self.running.retarget(target, self.transition_seconds)
            self.hold_remaining = control.hold_seconds if control.hold else None

    def update(self, delta_time: float) -> None:
        """用小步积分保持调速连续；完全制动后才开始屏息计时。"""
        remaining = max(0.0, delta_time)
        while remaining > 1e-9:
            step = min(0.01, remaining)
            remaining -= step
            frequency = self.frequency.update(self.target_frequency, step)
            fraction = self.inhale_fraction.update(self.target_inhale_fraction, step)
            stopped = self.running.arrived and self.running.value == 0.0
            if stopped and self.hold_remaining is not None:
                self.hold_remaining -= step
                if self.hold_remaining <= 0.0:
                    self.running.retarget(1.0, self.transition_seconds)
                    self.hold_remaining = None
            old_speed = self.running.value
            speed = self.running.update(step)
            motion_delta = (old_speed + speed) * step / 2.0
            self.depth.update(self.target_depth, motion_delta)
            half_duration = fraction if self.phase < 0.5 else 1.0 - fraction
            self.phase = (
                self.phase
                + frequency * (old_speed + speed) * step / (4.0 * half_duration)
            ) % 1.0

    def sample(self, lag: float = 0.0) -> float:
        """返回含深度倍率的呼吸位移，可复用到头部、身体和肩部。"""
        return -math.cos(self.phase * math.tau - lag) * self.depth.y


class _Eyelid:
    """单侧眼睑的运动、保持和自动控制归属。"""

    def __init__(self) -> None:
        """以全开状态等待眨眼或独立控制。"""
        self.motion = EyelidMotion()
        self.mode = "auto"
        self.transition_seconds = 0.3
        self.hold_seconds: float | None = None
        self.hold_remaining: float | None = None
        self.cooldown = 0.0
        self._blink_hold = 0.0
        self._blink_open = 0.35

    @property
    def blinking(self) -> bool:
        """是否正在执行本侧的闭合、停留或睁开。"""
        return self.mode.startswith("blink_")

    def apply(self, control: EyelidControl) -> None:
        """修改本侧目标并继承运动状态；旧目标不保留异步收尾。"""
        if control.transition_seconds is not None:
            self.transition_seconds = control.transition_seconds
        if control.release:
            self.mode = "release"
            self.hold_remaining = None
            self.motion.retarget(1.0, self.transition_seconds)
            return
        if "hold_seconds" in control.model_fields_set:
            self.hold_seconds = control.hold_seconds
            self.hold_remaining = control.hold_seconds
        if control.openness is not None:
            self.mode = "manual"
            self.hold_remaining = self.hold_seconds
            self.motion.retarget(control.openness, self.transition_seconds)
        elif (
            control.transition_seconds is not None
            and self.mode in {"manual", "release"}
            and not self.motion.arrived
        ):
            self.motion.retarget(self.motion.target, self.transition_seconds)

    def blink(self, close: float, hold: float, opening: float) -> None:
        """仅在自动状态起眨，受控或运动中的眼睑不积压补眨。"""
        if self.mode != "auto" or self.cooldown > 0.0:
            return
        self.mode = "blink_close"
        self._blink_hold = hold
        self._blink_open = opening
        self.motion.retarget(0.0, close)

    def update(self, delta_time: float, interval: float) -> None:
        """逐段消耗帧时间，保持时间严格从到达目标之后开始。"""
        self.cooldown = max(0.0, self.cooldown - delta_time)
        remaining = max(0.0, delta_time)
        while remaining > 1e-9 and self.mode != "auto":
            if not self.motion.arrived:
                step = min(remaining, self.motion.duration - self.motion.elapsed)
                self.motion.update(step)
                remaining -= step
                if not self.motion.arrived:
                    return
            if self.mode == "blink_close":
                self.mode = "blink_hold"
                self.hold_remaining = self._blink_hold
            elif self.mode == "release":
                self.mode = "auto"
                self.cooldown = interval
                return
            elif self.mode == "blink_open":
                self.mode = "auto"
                return
            if self.hold_remaining is None:
                return
            consumed = min(remaining, self.hold_remaining)
            self.hold_remaining -= consumed
            remaining -= consumed
            if self.hold_remaining > 1e-9:
                return
            if self.mode == "blink_hold":
                self.mode = "blink_open"
                self.hold_remaining = None
                self.motion.retarget(1.0, self._blink_open)
            elif self.mode == "manual":
                self.mode = "release"
                self.hold_remaining = None
                self.motion.retarget(1.0, self.transition_seconds)


class Eyelids:
    """共享眨眼节奏、独立绝对开度的左右眼睑。"""

    def __init__(self, min_interval: float, max_interval: float) -> None:
        """以配置的随机间隔和默认闭合、停留、睁开时长初始化。"""
        self.left = _Eyelid()
        self.right = _Eyelid()
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.interval = random.uniform(min_interval, max_interval)
        self.elapsed = 0.0
        self.close_seconds = 0.12
        self.hold_seconds = 0.05
        self.open_seconds = 0.35

    def apply(self, control: BodyControl) -> None:
        """按侧应用指定开度，再修改下一次眨眼节奏或主动起眨。"""
        if control.left_eye is not None:
            self.left.apply(control.left_eye)
        if control.right_eye is not None:
            self.right.apply(control.right_eye)
        blink = control.blink
        if blink is None:
            return
        if blink.interval_seconds is not None:
            self.min_interval = self.max_interval = self.interval = (
                blink.interval_seconds
            )
        if blink.close_seconds is not None:
            self.close_seconds = blink.close_seconds
        if blink.hold_seconds is not None:
            self.hold_seconds = blink.hold_seconds
        if blink.open_seconds is not None:
            self.open_seconds = blink.open_seconds
        if blink.now:
            self.blink()

    def blink(self, custom_durations: dict[str, float] | None = None) -> None:
        """触发一次眨眼，已有独立控制优先，未完成的眨眼不重启。"""
        durations = custom_durations or {}
        for eyelid in (self.left, self.right):
            eyelid.blink(
                durations.get("close", self.close_seconds),
                durations.get("stay", self.hold_seconds),
                durations.get("open", self.open_seconds),
            )

    def update(self, delta_time: float) -> tuple[float, float]:
        """推进左右眼睑并生成绝对开度，不与其他动画求和。"""
        was_blinking = self.left.blinking or self.right.blinking
        self.left.update(delta_time, self.interval)
        self.right.update(delta_time, self.interval)
        if was_blinking:
            if not (self.left.blinking or self.right.blinking):
                self.elapsed = 0.0
                self.interval = random.uniform(self.min_interval, self.max_interval)
        else:
            self.elapsed += delta_time
            if self.elapsed >= self.interval:
                self.elapsed = 0.0
                self.interval = random.uniform(self.min_interval, self.max_interval)
                self.blink()
        return self.left.motion.value, self.right.motion.value


__all__ = ["BodyControl", "Breathing", "Eyelids", "inject_body_schema"]
