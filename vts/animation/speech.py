"""说话联动动画器。

根据 AI 当前的 ``intent`` 与 ``emotion`` 调整虚拟形象的头部基准位置、
嘴型基准值、眼神方向；说话期间叠加微动；说话结束后保持一段情绪后缓慢回归。

与 AutoAnimator 配合时，本动画器输出 *基准/叠加偏移*，AutoAnimator 输出
*生命感扰动*，两者由 connection 层求和。
"""

from __future__ import annotations

import math
import time
from typing import Any

from .base import BaseAnimator


class SpeechAnimator(BaseAnimator):
    """说话联动 + 情绪渐变模块。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化说话动画器，载入意图/情感映射表与平滑参数。"""

        super().__init__(config)

        # 当前状态
        self.intent: str = "IDLE"
        self.emotion_type: str = "neutral"
        self.emotion_level: int = 2
        self.is_speaking: bool = False
        self.speaking_start_time: float = 0.0
        self.speaking_end_time: float = 0.0
        self.emotion_hold_duration: float = 2.0  # 说话结束后情绪保持时长

        # 目标参数（会随 intent / emotion 变化）
        self.target_params: dict[str, float] = {
            "v_head_x": 0.0,
            "v_head_y": 0.0,
            "v_head_z": 0.0,
            "v_mouth_form": 0.0,
            "v_eye_x": 0.0,
            "v_eye_y": 0.0,
        }

        # 当前实际参数（平滑插值）
        self.current_params: dict[str, float] = dict(self.target_params)

        # 平滑系数
        self.lerp_factor: float = 5.0
        self.recovery_lerp_factor: float = 1.2  # 回归到 neutral 用的更慢系数

        # 意图映射：(head_x, head_y, head_z, eye_x, eye_y)
        self.intent_map: dict[str, dict[str, float]] = {
            "IDLE": {"x": 0, "y": 0, "z": 0, "ex": 0, "ey": 0},
            "THINKING": {"x": 10, "y": 8, "z": -6, "ex": -0.5, "ey": 0.3},
            "NARRATING": {"x": 0, "y": 0, "z": 0, "ex": 0, "ey": 0},
            "CONFUSED": {"x": -8, "y": 5, "z": 8, "ex": 0.5, "ey": 0.2},
            "EXCITED": {"x": 0, "y": 8, "z": 0, "ex": 0, "ey": 0.4},
        }

        # 情感矩阵：(mouth_min, mouth_max, head_x, head_y, head_z, eye_x, eye_y)
        self.emotion_matrix: dict[str, dict[int, tuple[float, ...]]] = {
            "happy": {
                1: (0.2, 0.4, 0, 0, 0, 0, 0),
                2: (0.5, 0.7, 0, 5, 8, 0, 0.2),
                3: (0.8, 1.0, 0, 0, 0, 0, 0),
            },
            "angry": {
                1: (-0.2, -0.3, 0, -3, 0, 0, -0.2),
                2: (-0.4, -0.6, 0, -6, 0, 0, -0.4),
                3: (-0.7, -1.0, 0, -5, 0, 0, -0.6),
            },
            "sad": {
                1: (-0.1, -0.2, 0, -3, -5, 0, -0.2),
                2: (-0.3, -0.4, 0, -6, -10, 0, -0.4),
                3: (-0.5, -0.6, 0, -10, -15, 0, -0.6),
            },
            "surprised": {
                1: (0.0, 0.1, 0, 8, 0, 0, 0.3),
                2: (0.1, 0.2, 0, 18, 0, 0, 0.6),
                3: (0.2, 0.3, 0, 25, 0, 0, 0.9),
            },
            "neutral": {
                1: (0, 0, 0, 0, 0, 0, 0),
                2: (0, 0, 0, 0, 0, 0, 0),
                3: (0, 0, 0, 0, 0, 0, 0),
            },
        }

    # ── 状态写入接口 ───────────────────────────────────────

    def set_state(
        self,
        intent: str | None = None,
        emotion: str | None = None,
        is_speaking: bool | None = None,
    ) -> None:
        """统一入口；emotion 支持 ``"happy:3"`` 这种 ``类型:强度`` 格式。"""

        if intent and intent.upper() in self.intent_map:
            self.intent = intent.upper()

        if emotion:
            parts = emotion.lower().split(":")
            self.emotion_type = parts[0]
            if len(parts) > 1 and parts[1].isdigit():
                self.emotion_level = int(parts[1])
            else:
                self.emotion_level = 2

        if is_speaking is not None:
            if is_speaking and not self.is_speaking:
                self.speaking_start_time = time.time()
            elif not is_speaking:
                self.speaking_end_time = time.time()
            self.is_speaking = is_speaking

    def set_speaking(self, is_speaking: bool) -> None:
        """便捷：仅切换说话状态。"""

        self.set_state(is_speaking=is_speaking)

    def set_emotion(self, emotion: str, level: int = 2) -> None:
        """便捷：设置情感与强度。"""

        if ":" in emotion:
            self.set_state(emotion=emotion)
        else:
            self.set_state(emotion=f"{emotion}:{level}")

    def set_intent(self, intent: str) -> None:
        """便捷：仅切换意图。"""

        self.set_state(intent=intent)

    # ── 主循环：每帧更新 ───────────────────────────────────

    async def update(self, delta_time: float) -> dict[str, float]:
        """计算这一帧应注入到 VTS 的参数。"""

        if not self.is_active:
            return {}

        elapsed_since_end = time.time() - self.speaking_end_time
        current_lerp = self.lerp_factor

        # 说话结束后的情绪回归逻辑。
        if not self.is_speaking and self.speaking_end_time > 0:
            if elapsed_since_end > self.emotion_hold_duration:
                self.emotion_type = "neutral"
                self.emotion_level = 2
                self.intent = "IDLE"
                current_lerp = self.recovery_lerp_factor

        intent_cfg = self.intent_map.get(self.intent, self.intent_map["IDLE"])
        emo_table = self.emotion_matrix.get(self.emotion_type, self.emotion_matrix["neutral"])
        emo_cfg = emo_table.get(self.emotion_level, emo_table[2])
        mouth_min, mouth_max, emo_head_x, emo_head_y, emo_head_z, emo_eye_x, emo_eye_y = emo_cfg

        # 嘴型基准 + 微小肌肉颤动
        elapsed = time.time()
        mouth_base = (mouth_min + mouth_max) / 2
        mouth_amp = (mouth_max - mouth_min) / 2
        current_mouth_target = mouth_base + math.sin(elapsed * math.pi * 1.0) * mouth_amp

        self.target_params["v_head_x"] = float(intent_cfg["x"]) + emo_head_x
        self.target_params["v_head_y"] = float(intent_cfg["y"]) + emo_head_y
        self.target_params["v_head_z"] = float(intent_cfg["z"]) + emo_head_z
        self.target_params["v_mouth_form"] = current_mouth_target
        self.target_params["v_eye_x"] = float(intent_cfg.get("ex", 0)) + emo_eye_x
        self.target_params["v_eye_y"] = float(intent_cfg.get("ey", 0)) + emo_eye_y

        # 思考时眼神微漂
        if self.intent == "THINKING":
            self.target_params["v_eye_x"] += math.sin(elapsed * math.pi * 0.5) * 0.1
            self.target_params["v_eye_y"] += math.cos(elapsed * math.pi * 0.4) * 0.1

        # 说话时的动态偏移
        dynamic_y = 0.0
        dynamic_x = 0.0
        dynamic_z = 0.0
        if self.is_speaking:
            speaking_elapsed = time.time() - self.speaking_start_time
            dynamic_y = math.sin(speaking_elapsed * math.pi * 1.2) * 3.0 - 1.5

            if self.emotion_level == 3:
                if self.emotion_type == "happy":
                    dynamic_z = math.sin(speaking_elapsed * math.pi * 0.8) * 15.0
                elif self.emotion_type == "angry":
                    dynamic_x = math.sin(speaking_elapsed * math.pi * 4.0) * 1.2
                    dynamic_z = math.sin(speaking_elapsed * math.pi * 5.0) * 1.2

        # 平滑 + 速率限制
        output: dict[str, float] = {}
        max_speeds = {
            "v_head_x": 80.0,
            "v_head_y": 80.0,
            "v_head_z": 80.0,
            "v_mouth_form": 10.0,
        }
        for key, target in self.target_params.items():
            prev_val = self.current_params[key]
            step = (target - prev_val) * current_lerp * delta_time
            max_speed = max_speeds.get(key, 100.0)
            max_step = max_speed * delta_time
            if abs(step) > max_step:
                step = max_step if step > 0 else -max_step
            self.current_params[key] += step

            final_value = self.current_params[key]
            if key == "v_head_y":
                final_value += dynamic_y
            elif key == "v_head_x":
                final_value += dynamic_x
            elif key == "v_head_z":
                final_value += dynamic_z
            output[key] = final_value

        return output


__all__ = ["SpeechAnimator"]
