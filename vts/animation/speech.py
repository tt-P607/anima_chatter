"""说话联动动画器。

根据 AI 当前的 ``intent`` 与 ``emotion`` 调整虚拟形象的头部基准位置、
嘴型基准值、眼神方向；说话期间叠加微动；说话结束后保持一段情绪后缓慢回归。

与 AutoAnimator 配合时，本动画器输出 *基准/叠加偏移*，AutoAnimator 输出
*生命感扰动*，两者由 connection 层求和。

可选：传入 ``envelope_tracker``（来自 :class:`AudioPlayer`），开启"音频驱动
头部 / 身体律动"——根据 TTS 实时音量包络让头部前后倾、左右摆、身体律动，
让 VTB 看起来"跟着声音动"。这部分由 ``audio_drive_config`` 控制开关与增益。
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

from ...config import AudioDriveSection
from ...constants import normalize_intent, split_emotion
from .base import BaseAnimator
from .noise import fbm

if TYPE_CHECKING:
    from ...audio import EnvelopeTracker


class SpeechAnimator(BaseAnimator):
    """说话联动 + 情绪渐变模块。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        envelope_tracker: "EnvelopeTracker | None" = None,
        audio_drive_config: AudioDriveSection | None = None,
    ) -> None:
        """初始化说话动画器，载入意图 / 情感映射表与平滑参数。

        Args:
            config: 兼容 BaseAnimator 的基础配置 dict。
            envelope_tracker: 音频包络追踪器；``None`` 时关闭音频驱动律动。
            audio_drive_config: 音频驱动配置段；``None`` 时用该段的默认值
                （测试可裸跑，生产由插件注入）。
        """

        super().__init__(config)

        # ── 音频驱动律动 ────────────────────────────────
        self._envelope_tracker = envelope_tracker
        drive = audio_drive_config or AudioDriveSection()
        self._audio_drive_enabled = drive.enabled
        self._head_y_gain = drive.head_y_gain
        self._head_x_gain = drive.head_x_gain
        self._body_y_gain = drive.body_y_gain
        # 说话韵律向上半身扩散的增益（横向轻摆 / 节拍侧向 / 上下弹跳）。
        self._body_x_gain = drive.body_x_gain
        self._body_z_gain = drive.body_z_gain
        self._body_bounce_k = drive.body_bounce_k
        self._neutral_attenuation = drive.neutral_attenuation
        # 有机微动开关：说话时的头部摆动用 noise 替代 sin，去机械感。
        self._organic_enabled = drive.organic_enabled
        # 噪声相位随机起点。
        self._noise_phase: float = time.time() % 1000

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

        # 意图映射：每个 intent 是一组目标姿态偏移量，由 lerp_factor 平滑过渡。
        # 字段说明：
        #   x  → v_head_x：头部左右转向（- 左 / + 右）
        #   y  → v_head_y：头部上下俯仰（- 低头 / + 抬头）
        #   z  → v_head_z：头部歪侧（- 左歪 / + 右歪）
        #   ex → v_eye_x：眼神横向（- 左 / + 右）
        #   ey → v_eye_y：眼神纵向（- 下 / + 上）
        # 数值范围：head_xyz ∈ ±30 度，eye_xy ∈ ±1。
        # 反复动作（点头 / 摇头 / 来回扭动）不在静态 intent 内，应通过行内
        # 时间点标记或多次切换实现。
        self.intent_map: dict[str, dict[str, float]] = {
            # ── 基础姿态（4） ───────────────────────────
            "IDLE": {"x": 0, "y": 0, "z": 0, "ex": 0, "ey": 0},
            "NARRATING": {"x": 0, "y": 0, "z": 0, "ex": 0, "ey": 0},
            "THINKING": {"x": 3.5, "y": 3.0, "z": -2.5, "ex": -0.2, "ey": 0.15},
            "CONFUSED": {"x": -2.5, "y": 2.0, "z": 3.0, "ex": 0.2, "ey": 0.1},

            # ── 高表现力情绪（2） ───────────────────────
            "EXCITED": {"x": 0, "y": 0.5, "z": 0, "ex": 0, "ey": 0.08},
            "SURPRISED": {"x": 0, "y": 0.5, "z": 0, "ex": 0, "ey": 0.2},

            # ── 眼神类：方向性凝视（5） ─────────────────
            "PEEK_LEFT": {"x": -5.0, "y": 0, "z": 0, "ex": -0.3, "ey": 0},
            "PEEK_RIGHT": {"x": 5.0, "y": 0, "z": 0, "ex": 0.3, "ey": 0},
            "LOOKAWAY": {"x": -3.5, "y": -1.5, "z": 0, "ex": -0.2, "ey": -0.1},
            "STARE_DOWN": {"x": 0, "y": -4.5, "z": 0, "ex": 0, "ey": -0.3},
            "DREAMY_GAZE": {"x": 3.0, "y": 2.0, "z": -1.5, "ex": 0.15, "ey": 0.2},

            # ── 态度类：情感倾向（4） ───────────────────
            "PROUD_LIFT": {"x": 0, "y": 4.0, "z": 0, "ex": 0, "ey": 0.1},
            "WORRIED_TILT": {"x": 0, "y": -1.5, "z": 4.0, "ex": 0, "ey": -0.1},
            "SHY_DOWN": {"x": 0, "y": -3.0, "z": 2.0, "ex": -0.15, "ey": -0.15},
            "ATTENTIVE": {"x": 0, "y": 1.5, "z": 0, "ex": 0, "ey": 0.05},

            # ── 调皮 / 紧张（3） ────────────────────────
            "PLAYFUL_TILT": {"x": 0, "y": 1.0, "z": 6.0, "ex": 0.15, "ey": 0.1},
            "MISCHIEF": {"x": 0, "y": -1.0, "z": -3.0, "ex": 0.2, "ey": -0.05},
            "SCARED_SHRINK": {"x": 0, "y": -3.0, "z": 0, "ex": 0, "ey": -0.2},
        }

        # 情感矩阵：(mouth_min, mouth_max, head_x, head_y, head_z, eye_x, eye_y)
        self.emotion_matrix: dict[str, dict[int, tuple[float, ...]]] = {
            "happy": {
                1: (0.2, 0.4, 0, 0, 0, 0, 0),
                2: (0.4, 0.6, 0, 0, 1.5, 0, 0.05),
                3: (0.6, 0.8, 0, 0, 0, 0, 0),
            },
            "angry": {
                1: (-0.2, -0.3, 0, 0, 0, 0, -0.1),
                2: (-0.3, -0.5, 0, 0, 0, 0, -0.2),
                3: (-0.5, -0.7, 0, 0, 0, 0, -0.3),
            },
            "sad": {
                1: (-0.1, -0.2, 0, -1.5, -2.0, 0, -0.1),
                2: (-0.2, -0.3, 0, -2.5, -4.0, 0, -0.2),
                3: (-0.3, -0.5, 0, -4.0, -5.0, 0, -0.3),
            },
            "surprised": {
                1: (0.0, 0.1, 0, 0, 0, 0, 0.1),
                2: (0.1, 0.2, 0, 0, 0, 0, 0.2),
                3: (0.2, 0.3, 0, 0, 0, 0, 0.3),
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

        if intent:
            normalized = normalize_intent(intent, default=self.intent)
            if normalized in self.intent_map:
                self.intent = normalized

        if emotion:
            self.emotion_type, self.emotion_level = split_emotion(emotion)

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

        # 说话时的动态偏移：噪声微动，幅度按情绪强度放大
        dynamic_y = 0.0
        dynamic_x = 0.0
        dynamic_z = 0.0
        dynamic_body_x = 0.0
        dynamic_body_y = 0.0
        dynamic_body_z = 0.0
        if self.is_speaking:
            speaking_elapsed = time.time() - self.speaking_start_time
            if self._organic_enabled:
                # 情绪越强动得越明显（level 1~3 → 1.0~2.0 倍）。
                emo_gain = 1.0 + (self.emotion_level - 1) * 0.5
                nt = (speaking_elapsed + self._noise_phase) * 0.5
                dynamic_y = fbm(nt, seed=11) * 3.0 * emo_gain
                dynamic_x = fbm(nt, seed=12) * 2.0 * emo_gain
                dynamic_z = fbm(nt * 0.7, seed=13) * 4.0 * emo_gain
            else:
                dynamic_y = math.sin(speaking_elapsed * math.pi * 1.2) * 3.0 - 1.5
                if self.emotion_level == 3:
                    if self.emotion_type == "happy":
                        dynamic_z = math.sin(speaking_elapsed * math.pi * 0.8) * 15.0
                    elif self.emotion_type == "angry":
                        dynamic_x = math.sin(speaking_elapsed * math.pi * 4.0) * 1.2
                        dynamic_z = math.sin(speaking_elapsed * math.pi * 5.0) * 1.2

        # ── 音频驱动律动叠加 ────────────────────────────────
        # 把当前 TTS 音频包络读出来，按"音量 → 头部前倾 / 横向摆 / 身体律动"
        # 三路注入。三路用同一个 envelope 不同 gain，让说话节奏在身体上也有
        # 投影。emotion=neutral 时按 _neutral_attenuation 衰减，避免平静叙述
        # 时显得"乱抖"。
        if (
            self._audio_drive_enabled
            and self._envelope_tracker is not None
            and self.is_speaking
        ):
            frame = self._envelope_tracker.current()
            if frame.rms > 0.0:
                attenuation = (
                    self._neutral_attenuation
                    if self.emotion_type == "neutral"
                    else 1.0
                )
                # 主频带：rms 直接驱动头部前后倾。声音大时头微抬，自然感。
                dynamic_y += frame.rms * self._head_y_gain * attenuation
                # 横向轻微摆头：用一个慢振荡 + rms 振幅，让侧脸也有动作。
                dynamic_x += (
                    math.sin(time.time() * 4.0) * frame.rms
                    * self._head_x_gain * attenuation
                )
                # 身体律动：用 velocity（变化率）驱动。突变量大 = 节奏感强。
                dynamic_body_y += frame.velocity * self._body_y_gain * attenuation
                # 韵律向上半身扩散：音量 → 横向轻摆；volume → 上下弹跳；
                # velocity → 节拍侧向。三路默认增益保守，避免抢过口型 / 头部。
                dynamic_body_x += (
                    math.sin(time.time() * 4.0) * frame.rms
                    * self._body_x_gain * attenuation
                )
                dynamic_body_y += frame.rms * self._body_bounce_k * attenuation
                dynamic_body_z += frame.velocity * self._body_z_gain * attenuation

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

        # 身体律动单独走 v_body_*（不在 target_params 里，绕过情绪基准的 lerp）。
        # 直接输出叠加值，由 connection 层与 AutoAnimator 的 body_* 求和。
        if dynamic_body_x != 0.0:
            output["v_body_x"] = dynamic_body_x
        if dynamic_body_y != 0.0:
            output["v_body_y"] = dynamic_body_y
        if dynamic_body_z != 0.0:
            output["v_body_z"] = dynamic_body_z

        return output


__all__ = ["SpeechAnimator"]
