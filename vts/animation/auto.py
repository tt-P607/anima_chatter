"""生命感自动化动画器：自动眨眼/呼吸/眼神漫游/宏观动作。

完整移植自旧版 ``soul_chatter_plugin`` 的 ``blink_animator.AutoAnimator``，
仅做以下调整：

- ``logger`` 改为 ``src.kernel.logger.get_logger("voice_chatter.vts.auto_animator")``。
- 移除 ``from src.common.logger import get_logger`` 的旧导入路径。

行为完全保持不变：
- Ease-in-out 曲线眨眼 + 随机间隔。
- 模拟生物呼吸（headZ）。
- 眼神微颤 / 慢速漫游 / 随机扫视。
- 被动慢速摆动 + 宏观动作库（重心斜、扫视、好奇歪头、深呼吸等）。
- 安全平滑层（阻尼追赶 + 速率限制）防止瞬移。
"""

from __future__ import annotations

import math
import random
import time
from typing import Any

from src.kernel.logger import get_logger

from .base import BaseAnimator


logger = get_logger("voice_chatter.vts.auto_animator")


class AutoAnimator(BaseAnimator):
    """生命感自动化模块（整合版）。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化所有子状态机：眨眼/呼吸/眼神/宏观动作/安全平滑。"""

        super().__init__(config)
        self.start_time: float = time.time()
        self.is_performing: bool = False

        # ── 眨眼状态 ────────────────────────────────────
        self.blink_state: int = 0  # 0 睁, 1 闭中, 2 闭停, 3 开中
        self.blink_timer: float = 0.0
        self.next_blink_time: float = random.uniform(2.0, 6.0)

        self.base_close_duration: float = 0.12
        self.base_stay_duration: float = 0.05
        self.base_open_duration: float = 0.35

        self.current_close_duration: float = self.base_close_duration
        self.current_stay_duration: float = self.base_stay_duration
        self.current_open_duration: float = self.base_open_duration

        self.current_eye_open: float = 1.0

        # ── 呼吸 ──────────────────────────────────────
        self.breath_freq: float = 0.25
        self.breath_amplitude: float = 0.7

        # ── 眼神漫游 ──────────────────────────────────
        self.eye_x: float = 0.0
        self.eye_y: float = 0.0
        self.eye_wander_x: float = 0.0
        self.eye_wander_y: float = 0.0
        self.next_saccade_time: float = 0.0

        # ── 身体随机晃动相位 ──────────────────────────
        self.sway_offsets: list[float] = [random.uniform(0, 100) for _ in range(4)]

        # ── 被动慢速摆动 ──────────────────────────────
        self.next_passive_sway_time: float = time.time() + random.uniform(20.0, 40.0)
        self.passive_sway_timer: float = 0.0
        self.passive_sway_active: bool = False
        self.passive_sway_val: float = 0.0
        self.passive_sway_duration: float = 4.0
        self.passive_sway_cycles: int = 1

        # ── 宏观动作（idle 偶尔触发的大动作） ─────────
        self.macro_state: str = "IDLE"  # IDLE / MOVING / HOLDING / RETURNING
        self.macro_timer: float = 0.0
        self.next_macro_trigger_time: float = time.time() + random.uniform(20.0, 40.0)

        self.macro_history: list[str] = []
        self.macro_demo_queue: list[dict[str, Any]] = []
        self.macro_target_params: dict[str, float] = {}
        self.macro_start_params: dict[str, float] = {}
        self.macro_sequence: list[dict[str, Any]] = []
        self.macro_current_params: dict[str, float] = {
            "v_head_x": 0.0,
            "v_head_y": 0.0,
            "v_head_z": 0.0,
            "v_eye_x": 0.0,
            "v_eye_y": 0.0,
            "v_eye_l": 0.0,
            "v_eye_r": 0.0,
            "v_body_x": 0.0,
            "v_body_y": 0.0,
            "v_body_z": 0.0,
            "v_blush": 0.0,
        }

        # ── 安全平滑层 ────────────────────────────────
        self.final_smooth_params: dict[str, float] = {k: 0.0 for k in self.macro_current_params}
        self.performing_fade_weight: float = 1.0

        self.macro_duration_move: float = 2.0
        self.macro_duration_hold: float = 5.0
        self.macro_duration_return: float = 3.0
        self.macro_trigger_blink: bool = False
        self.macro_blink_at_t: float = -1.0
        self.macro_blink_triggered: bool = False
        self.macro_eye_oscillation: float = 0.0
        self.macro_head_oscillation: float = 0.0
        self.macro_head_oscillation_z: float = 0.0
        self.macro_osc_freq: float = 0.8
        self.macro_custom_blink_durations: dict[str, float] | None = None

        # 动作库（权重抽样）
        self.macro_library: list[dict[str, Any]] = [
            {
                "name": "重心左斜",
                "params": {"v_head_z": -15.0, "v_head_x": -5.0},
                "weight": 20,
                "hold": 8.0,
            },
            {
                "name": "重心右斜",
                "params": {"v_head_z": 11.0, "v_head_x": 4.0},
                "weight": 20,
                "hold": 8.0,
            },
            {
                "name": "左侧扫视",
                "params": {"v_head_x": -18.0, "v_eye_x": -0.7, "v_head_y": 2.0},
                "weight": 20,
                "hold": 3.0,
                "move_speed": 1.5,
                "trigger_blink_on_return": True,
            },
            {
                "name": "右侧扫视",
                "params": {"v_head_x": 18.0, "v_eye_x": 0.7, "v_head_y": 2.0},
                "weight": 20,
                "hold": 3.0,
                "move_speed": 1.5,
                "trigger_blink_on_return": True,
            },
            {
                "name": "失神发呆",
                "params": {"v_head_y": -10.0, "v_head_z": 3.0, "v_eye_y": -0.4},
                "weight": 20,
                "hold": 6.0,
                "move_speed": 4.0,
                "custom_blink": {"close": 1.2, "stay": 0.8, "open": 1.5},
            },
            {
                "name": "侧身偷瞄",
                "params": {"v_body_x": 15.0, "v_head_x": 5.0, "v_eye_x": 0.7},
                "weight": 20,
                "hold": 3.0,
                "move_speed": 1.8,
            },
            {
                "name": "分心远眺",
                "params": {"v_head_x": 22.0, "v_head_y": 8.0, "v_eye_x": -0.7, "v_eye_y": 0.3},
                "weight": 20,
                "hold": 4.0,
                "trigger_blink_on_return": True,
            },
            {
                "name": "好奇歪头",
                "params": {
                    "v_head_z": 12.0,
                    "v_head_x": 8.0,
                    "v_head_y": 5.0,
                    "v_eye_x": -0.4,
                    "v_eye_y": 0.2,
                },
                "weight": 20,
                "hold": 4.0,
            },
            {
                "name": "深呼吸",
                "params": {"v_head_y": 18.0, "v_body_y": 7.0, "v_head_z": 4.0},
                "weight": 10,
                "hold": 1.0,
                "move_speed": 2.0,
                "blink_at_t": 0.4,
                "custom_blink": {"close": 1.0, "stay": 1.5, "open": 0.8},
            },
            {
                "name": "向下检查",
                "params": {"v_head_y": -20.0, "v_eye_y": -0.7, "v_body_y": -2.0},
                "weight": 10,
                "hold": 2.5,
                "move_speed": 1.5,
            },
            {
                "name": "害羞回避",
                "params": {
                    "v_head_x": -15.0,
                    "v_head_y": -12.0,
                    "v_head_z": -8.0,
                    "v_eye_x": 0.5,
                    "v_eye_y": -0.3,
                    "v_blush": 1.0,
                },
                "weight": 10,
                "hold": 5.0,
            },
            {
                "name": "深度思考",
                "params": {"v_head_y": 15.0, "v_head_z": -5.0, "v_eye_y": 0.7, "v_eye_x": 0.0},
                "eye_oscillation": 0.35,
                "osc_freq": 2.5,
                "weight": 10,
                "hold": 5.0,
                "move_speed": 2.0,
            },
        ]

        # 参数 ID
        self.param_eye_l = "v_eye_left"
        self.param_eye_r = "v_eye_right"
        self.param_eye_x = "v_eye_x"
        self.param_eye_y = "v_eye_y"
        self.param_head_x = "v_head_x"
        self.param_head_y = "v_head_y"
        self.param_head_z = "v_head_z"

    # ── 工具 ─────────────────────────────────────────

    @staticmethod
    def _ease_in_out(t: float) -> float:
        """混合 S 曲线：两端顺滑，中间略带爆发。"""

        return 0.5 * (1 - math.cos(math.pi * (t * t * (3 - 2 * t))))

    # ── 主循环 ───────────────────────────────────────

    async def update(self, delta_time: float) -> dict[str, float]:
        """每帧产出全套自动化参数（含眨眼、呼吸、眼神、宏观动作）。"""

        if not self.is_active:
            return {self.param_eye_l: 1.0, self.param_eye_r: 1.0}

        logic_delta = min(0.06, delta_time)
        elapsed = time.time() - self.start_time
        output: dict[str, float] = {}

        # 1) 眨眼
        self.blink_timer += logic_delta
        if self.blink_state == 0:
            self.current_eye_open = 1.0
            if self.blink_timer >= self.next_blink_time:
                self.blink_state = 1
                self.blink_timer = 0
                self.current_close_duration = self.base_close_duration
                self.current_stay_duration = self.base_stay_duration
                self.current_open_duration = self.base_open_duration
        elif self.blink_state == 1:
            t = min(1.0, self.blink_timer / self.current_close_duration)
            self.current_eye_open = 1.0 - self._ease_in_out(t)
            if t >= 1.0:
                self.blink_state = 2
                self.blink_timer = 0
        elif self.blink_state == 2:
            self.current_eye_open = 0.0
            if self.macro_state == "RETURNING":
                self.eye_x = 0.0
                self.eye_y = 0.0
            if self.blink_timer >= self.current_stay_duration:
                self.blink_state = 3
                self.blink_timer = 0
        elif self.blink_state == 3:
            t = min(1.0, self.blink_timer / self.current_open_duration)
            self.current_eye_open = self._ease_in_out(t)
            if t >= 1.0:
                self.current_eye_open = 1.0
                self.blink_state = 0
                self.blink_timer = 0
                if random.random() < 0.10:
                    self.next_blink_time = random.uniform(0.4, 0.6)
                else:
                    self.next_blink_time = random.uniform(2.5, 6.5)

        macro_eye_l = self.macro_current_params.get("v_eye_l", 0.0)
        macro_eye_r = self.macro_current_params.get("v_eye_r", 0.0)
        output[self.param_eye_l] = max(0.0, min(1.0, self.current_eye_open + macro_eye_l))
        output[self.param_eye_r] = max(0.0, min(1.0, self.current_eye_open + macro_eye_r))

        # 2) 呼吸
        breath_z = math.sin(elapsed * self.breath_freq * 2 * math.pi) * self.breath_amplitude

        # 3) 眼神漫游
        micro_jitter_x = math.sin(elapsed * math.pi * 6) * 0.01
        micro_jitter_y = math.cos(elapsed * math.pi * 5.5) * 0.01
        self.eye_wander_x = math.sin(elapsed * 0.3) * 0.05 + math.sin(elapsed * 0.17) * 0.03
        self.eye_wander_y = math.cos(elapsed * 0.25) * 0.04 + math.cos(elapsed * 0.13) * 0.02

        if elapsed >= self.next_saccade_time:
            if random.random() < 0.8:
                self.eye_x = random.uniform(-0.16, 0.16)
                self.eye_y = random.uniform(-0.11, 0.11)
            else:
                self.eye_x = random.uniform(-0.6, 0.6)
                self.eye_y = random.uniform(-0.33, 0.33)
            self.next_saccade_time = elapsed + random.uniform(1.2, 3.5)

        # 4) 被动慢摆
        now = time.time()
        if not self.passive_sway_active:
            if now >= self.next_passive_sway_time:
                self.passive_sway_active = True
                self.passive_sway_timer = 0.0
                self.passive_sway_cycles = random.randint(1, 2)
                self.passive_sway_duration = self.passive_sway_cycles * 3.0
                self.next_passive_sway_time = now + random.uniform(40.0, 80.0)
        else:
            self.passive_sway_timer += logic_delta
            t_ratio = self.passive_sway_timer / self.passive_sway_duration
            if t_ratio >= 1.0:
                self.passive_sway_active = False
                self.passive_sway_val = 0.0
            else:
                envelope = math.sin(t_ratio * math.pi)
                cycle_val = math.sin(t_ratio * self.passive_sway_cycles * 2 * math.pi)
                self.passive_sway_val = envelope * cycle_val * 2.5

        # 5) 宏观动作状态机
        self._update_macro_actions(logic_delta)

        # 6) 头部/身体随机微动
        head_micro_x = (
            math.sin(elapsed * 0.12 + self.sway_offsets[0]) * 2.5
            + math.sin(elapsed * 0.05 + self.sway_offsets[1]) * 1.5
        )
        head_micro_y = (
            math.cos(elapsed * 0.1 + self.sway_offsets[2]) * 2.0
            + math.cos(elapsed * 0.07 + self.sway_offsets[3]) * 1.0
        )
        body_sway_z = (
            math.sin(elapsed * 0.07 + self.sway_offsets[1]) * 1.8
            + math.sin(elapsed * 0.03 + self.sway_offsets[3]) * 1.2
        )

        # 7) 状态过渡：is_performing 时让自动化淡出
        target_fade = 0.0 if self.is_performing else 1.0
        fade_speed = logic_delta * 2.0
        if self.performing_fade_weight < target_fade:
            self.performing_fade_weight = min(target_fade, self.performing_fade_weight + fade_speed)
        elif self.performing_fade_weight > target_fade:
            self.performing_fade_weight = max(target_fade, self.performing_fade_weight - fade_speed)

        base_eye_x = self.eye_x + self.eye_wander_x + micro_jitter_x
        base_eye_y = self.eye_y + self.eye_wander_y + micro_jitter_y

        # 宏观 HOLDING 阶段的振荡
        eye_osc_x = 0.0
        head_osc_x = 0.0
        head_osc_z = 0.0
        if self.macro_state == "HOLDING":
            t_ratio = min(1.0, self.macro_timer / self.macro_duration_hold)
            if self.macro_head_oscillation > 0:
                head_osc_x = math.sin(t_ratio * self.macro_osc_freq * math.pi * 2) * self.macro_head_oscillation
            if self.macro_head_oscillation_z > 0:
                head_osc_z = math.sin(t_ratio * self.macro_osc_freq * math.pi * 2) * self.macro_head_oscillation_z
            if self.macro_eye_oscillation > 0:
                eye_osc_x = math.sin(t_ratio * self.macro_osc_freq * math.pi * 2) * self.macro_eye_oscillation

        raw_output: dict[str, float] = {}
        raw_output[self.param_eye_x] = base_eye_x + self.macro_current_params.get("v_eye_x", 0.0) + eye_osc_x
        raw_output[self.param_eye_y] = base_eye_y + self.macro_current_params.get("v_eye_y", 0.0)
        raw_output[self.param_head_x] = head_micro_x + self.macro_current_params.get("v_head_x", 0.0) + head_osc_x
        raw_output[self.param_head_y] = head_micro_y + self.macro_current_params.get("v_head_y", 0.0)
        raw_output[self.param_head_z] = (
            breath_z + self.macro_current_params.get("v_head_z", 0.0) + head_osc_z + self.passive_sway_val
        )
        raw_output["v_body_x"] = self.macro_current_params.get("v_body_x", 0.0)
        raw_output["v_body_y"] = self.macro_current_params.get("v_body_y", 0.0)
        raw_output["v_body_z"] = (
            body_sway_z
            + self.macro_current_params.get("v_body_z", 0.0)
            + head_osc_z
            + (self.passive_sway_val * 0.4)
        )
        raw_output["v_blush"] = self.macro_current_params.get("v_blush", 0.0)

        # 8) 安全平滑层（阻尼 + 速率限制）
        damp_factor = 0.25
        max_speeds = {
            self.param_head_x: 60.0,
            self.param_head_y: 60.0,
            self.param_head_z: 60.0,
            "v_body_x": 40.0,
            "v_body_y": 40.0,
            "v_body_z": 40.0,
            self.param_eye_x: 4.0,
            self.param_eye_y: 4.0,
        }

        for key, target_val in raw_output.items():
            weighted_target = target_val * self.performing_fade_weight
            prev_val = self.final_smooth_params.get(key, 0.0)

            ratio = min(1.0, logic_delta / damp_factor)
            smoothed_val = prev_val + (weighted_target - prev_val) * ratio

            max_speed = max_speeds.get(key, 100.0)
            max_step = max_speed * logic_delta
            diff = smoothed_val - prev_val
            if abs(diff) > max_step:
                smoothed_val = prev_val + (max_step if diff > 0 else -max_step)

            self.final_smooth_params[key] = smoothed_val
            output[key] = smoothed_val

        return output

    # ── 外部接口 ──────────────────────────────────────

    def set_performing(self, performing: bool) -> None:
        """切换"正在表演"标志（用于淡出自动化输出）。"""

        self.is_performing = performing

    def force_blink(self, custom_durations: dict[str, float] | None = None) -> None:
        """立即触发一次眨眼，可选自定义时长。"""

        if self.blink_state == 0:
            self.blink_state = 1
            self.blink_timer = 0.0
            if custom_durations:
                self.current_close_duration = custom_durations.get("close", self.base_close_duration)
                self.current_stay_duration = custom_durations.get("stay", self.base_stay_duration)
                self.current_open_duration = custom_durations.get("open", self.base_open_duration)
            else:
                self.current_close_duration = self.base_close_duration
                self.current_stay_duration = self.base_stay_duration
                self.current_open_duration = self.base_open_duration

    def start_demo(self) -> None:
        """按动作库顺序触发所有宏观动作（用于演示）。"""

        self.macro_demo_queue = self.macro_library.copy()
        self.macro_state = "IDLE"
        self.next_macro_trigger_time = 0
        logger.info(f"启动宏观动作演示，共 {len(self.macro_demo_queue)} 个动作")

    # ── 宏观动作状态机 ────────────────────────────────

    def _update_macro_actions(self, delta_time: float) -> None:
        """推进 IDLE → MOVING → HOLDING → RETURNING 状态机。"""

        now = time.time()

        if self.macro_state == "IDLE":
            if self.is_performing:
                self.next_macro_trigger_time = now + random.uniform(20.0, 45.0)
                return

            if now < self.next_macro_trigger_time:
                return

            if self.macro_demo_queue:
                action = self.macro_demo_queue.pop(0)
                logger.info(f"[演示] 播放: {action['name']} (剩余: {len(self.macro_demo_queue)})")
            else:
                available = [a for a in self.macro_library if a["name"] not in self.macro_history]
                if not available:
                    available = self.macro_library
                action = random.choices(available, weights=[a["weight"] for a in available])[0]

            self.macro_sequence = action.get("sequence", []).copy()
            if not self.macro_sequence:
                current_step = action
            else:
                if action.get("random_order", False):
                    random.shuffle(self.macro_sequence)
                current_step = self.macro_sequence.pop(0)

            self._apply_macro_step(current_step, action["name"])
            self.macro_start_params = self.macro_current_params.copy()
            self.macro_state = "MOVING"
            self.macro_timer = 0.0

        elif self.macro_state == "MOVING":
            self.macro_timer += delta_time
            t = min(1.0, self.macro_timer / self.macro_duration_move)
            smooth_t = self._ease_in_out(t)

            for key in self.macro_current_params:
                start = self.macro_start_params.get(key, 0.0)
                target = self.macro_target_params.get(key, 0.0)
                self.macro_current_params[key] = start + (target - start) * smooth_t

            if self.macro_blink_at_t > 0 and not self.macro_blink_triggered:
                if t >= self.macro_blink_at_t:
                    self.force_blink(self.macro_custom_blink_durations)
                    self.macro_blink_triggered = True

            if t >= 1.0:
                self.macro_state = "HOLDING"
                self.macro_timer = 0.0

        elif self.macro_state == "HOLDING":
            self.macro_timer += delta_time
            if self.macro_timer >= self.macro_duration_hold:
                if self.macro_sequence:
                    self.macro_start_params = self.macro_current_params.copy()
                    next_step = self.macro_sequence.pop(0)
                    self._apply_macro_step(next_step)
                    self.macro_state = "MOVING"
                    self.macro_timer = 0.0
                    logger.info(f" -> 衔接动作段: {next_step.get('name', 'Next Step')}")
                else:
                    self.macro_state = "RETURNING"
                    self.macro_timer = 0.0
                    self.macro_start_params = self.macro_current_params.copy()
                    if self.macro_trigger_blink:
                        self.force_blink(self.macro_custom_blink_durations)

        elif self.macro_state == "RETURNING":
            self.macro_timer += delta_time
            t = min(1.0, self.macro_timer / self.macro_duration_return)
            smooth_t = self._ease_in_out(t)

            for key in self.macro_current_params:
                start = self.macro_start_params.get(key, 0.0)
                self.macro_current_params[key] = start * (1.0 - smooth_t)

            if t >= 1.0:
                for key in self.macro_current_params:
                    self.macro_current_params[key] = 0.0
                self.macro_state = "IDLE"
                self.macro_timer = 0.0
                if self.macro_demo_queue:
                    self.next_macro_trigger_time = now + 2.0
                else:
                    self.next_macro_trigger_time = now + random.uniform(20.0, 40.0)

    def _apply_macro_step(self, step: dict[str, Any], action_name: str = "") -> None:
        """切换到一个新的动作步：写入目标参数 + 时长 + 振荡参数。"""

        self.macro_target_params = step["params"]
        self.macro_duration_hold = step.get("hold", 5.0)
        move_speed = step.get("move_speed", 2.0)
        self.macro_duration_move = move_speed
        self.macro_duration_return = move_speed * 1.5
        self.macro_trigger_blink = step.get("trigger_blink_on_return", False)
        self.macro_blink_at_t = step.get("blink_at_t", -1.0)
        self.macro_blink_triggered = False
        self.macro_eye_oscillation = step.get("eye_oscillation", 0.0)
        self.macro_head_oscillation = step.get("head_oscillation", 0.0)
        self.macro_head_oscillation_z = step.get("head_oscillation_z", 0.0)
        self.macro_osc_freq = step.get("osc_freq", 0.8)
        self.macro_custom_blink_durations = step.get("custom_blink", None)

        if action_name:
            self.macro_history.append(action_name)
            if len(self.macro_history) > 2:
                self.macro_history.pop(0)
            logger.info(f"触发宏观动作: {action_name}")


__all__ = ["AutoAnimator"]
