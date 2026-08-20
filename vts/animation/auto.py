"""生命感自动化动画器：自动眨眼/呼吸/眼神漫游/宏观动作。

完整移植自旧版 ``soul_chatter_plugin`` 的 ``blink_animator.AutoAnimator``，
仅做以下调整：

- ``logger`` 改为 ``src.kernel.logger.get_logger("anima_chatter.vts.auto_animator")``。
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

from src.app.plugin_system.api.log_api import get_logger

from ...config import IdleAnimationSection
from .base import BaseAnimator
from .dynamics import SecondOrderDynamics
from .noise import fbm


logger = get_logger("anima_chatter.vts.auto_animator")


class AutoAnimator(BaseAnimator):
    """生命感自动化模块（整合版）。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        idle_animation_config: IdleAnimationSection | None = None,
    ) -> None:
        """初始化所有子状态机：眨眼 / 呼吸 / 眼神 / 宏观动作 / 安全平滑。

        Args:
            config: 兼容 BaseAnimator 的基础配置 dict。
            idle_animation_config: 待机动画配置段；``None`` 时用该段的默认值
                （测试可裸跑，生产由插件注入）。
        """

        super().__init__(config)
        self.start_time: float = time.time()
        self.is_performing: bool = False

        cfg = idle_animation_config or IdleAnimationSection()

        # ── 眨眼状态 ────────────────────────────────────
        self.blink_state: int = 0  # 0 睁, 1 闭中, 2 闭停, 3 开中
        self.blink_timer: float = 0.0
        # 眨眼间隔（秒）。真人平均 4 秒一次，但 VTB 看起来活泼一点更好。
        self._blink_min_interval = cfg.blink_min_interval
        self._blink_max_interval = cfg.blink_max_interval
        self.next_blink_time: float = random.uniform(
            self._blink_min_interval, self._blink_max_interval
        )

        self.base_close_duration: float = 0.12
        self.base_stay_duration: float = 0.05
        self.base_open_duration: float = 0.35

        self.current_close_duration: float = self.base_close_duration
        self.current_stay_duration: float = self.base_stay_duration
        self.current_open_duration: float = self.base_open_duration

        self.current_eye_open: float = 1.0

        # ── 呼吸 ──────────────────────────────────────
        self.breath_freq = cfg.breath_freq
        self.breath_amplitude = cfg.breath_amplitude

        # ── 眼神漫游 ──────────────────────────────────
        self.eye_x: float = 0.0
        self.eye_y: float = 0.0
        self.eye_wander_x: float = 0.0
        self.eye_wander_y: float = 0.0
        # 首次扫视延后：避免刚连接时眼珠立刻跳动，让模型先自然稳定几秒。
        # 比较用的是相对 elapsed（秒），故此处存一个"过多少秒后首次扫视"的时长。
        self.next_saccade_time: float = random.uniform(1.5, 3.0)
        # 扫视回中时刻；>0 表示当前扫视等待回中，到点后眼神归零回镜头中心
        self._saccade_return_time: float = 0.0
        # 扫视间隔（秒）。真人微眼动 0.2-0.6 秒一次，但全做太疲劳；
        # 给 0.5-1.8 秒一次，让眼神持续微动不显呆。
        self._saccade_min_interval = cfg.saccade_min_interval
        self._saccade_max_interval = cfg.saccade_max_interval
        self._saccade_big_probability = cfg.saccade_big_probability
        self._saccade_small_amplitude_x = cfg.saccade_small_amplitude_x
        self._saccade_small_amplitude_y = cfg.saccade_small_amplitude_y
        self._saccade_big_amplitude_x = cfg.saccade_big_amplitude_x
        self._saccade_big_amplitude_y = cfg.saccade_big_amplitude_y

        # ── 头部微动幅度倍率 ──────────────────────────
        self._head_micro_scale = cfg.head_micro_scale

        # ── 有机微动：用 value noise 替代 sin 叠加，去掉机械周期感 ──
        self._organic_enabled = cfg.organic_enabled
        # 呼吸带动身体起伏的幅度（度），0 关闭。
        self._breath_body_amplitude = cfg.breath_body_amplitude
        # 噪声相位随机起点，避免每次启动从同一处开始。
        self._noise_phase: float = random.uniform(0, 1000)

        # ── 身体随机晃动相位 ──────────────────────────
        self.sway_offsets: list[float] = [random.uniform(0, 100) for _ in range(4)]

        # ── 被动慢速摆动 ──────────────────────────────
        self._passive_sway_min_interval = cfg.passive_sway_min_interval
        self._passive_sway_max_interval = cfg.passive_sway_max_interval
        self.next_passive_sway_time: float = time.time() + random.uniform(
            self._passive_sway_min_interval, self._passive_sway_max_interval
        )
        self.passive_sway_timer: float = 0.0
        self.passive_sway_active: bool = False
        self.passive_sway_val: float = 0.0
        self.passive_sway_duration: float = 4.0
        self.passive_sway_cycles: int = 1

        # ── 宏观动作（idle 偶尔触发的大动作） ─────────
        self._macro_min_interval = cfg.macro_min_interval
        self._macro_max_interval = cfg.macro_max_interval
        # 宏观动作整体速度倍率：>1 = 加快（动作时长压缩），<1 = 放慢。
        self._motion_speed_scale = cfg.motion_speed_scale
        self.macro_state: str = "IDLE"  # IDLE / MOVING / HOLDING / RETURNING
        self.macro_timer: float = 0.0
        # 首个宏观动作延后（12-22 秒）：刚连接时模型刚从"静止"进入驱动，
        # 若像默认 6-15 秒那样过早触发大幅摆位，观感会像开机抽搐。首启先
        # 自然稳定十几秒，之后动作回到正常的 6-15 秒节奏。
        self.next_macro_trigger_time: float = time.time() + random.uniform(12.0, 22.0)

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

        # 动作库（权重抽样 + 可选动作链）
        # 每个动作可加 chain_to: {下一动作名: 概率}，让 RETURNING 完成后有概率
        # 立即衔接到指定动作而不是等下次随机周期，做出"动作有因果"的连贯感。
        # 例如：歪头 → 思考 → 失神发呆，看着像"她正在想什么"。
        # 链式衔接最多发生 _max_chain_count 次（默认 2），防止无限连。
        self.macro_library: list[dict[str, Any]] = [
            {
                "name": "重心左斜",
                "params": {"v_head_z": -5.8, "v_head_x": -2.1},
                "weight": 20,
                "hold": 8.0,
                # 重心斜常常承接侧身偷瞄、好奇歪头
                "chain_to": {"侧身偷瞄": 0.3, "好奇歪头": 0.2},
            },
            {
                "name": "重心右斜",
                "params": {"v_head_z": 4.6, "v_head_x": 1.75},
                "weight": 20,
                "hold": 8.0,
                "chain_to": {"侧身偷瞄": 0.3, "好奇歪头": 0.2},
            },
            {
                "name": "左侧扫视",
                "params": {"v_head_x": -6.1, "v_eye_x": -0.33, "v_head_y": 1.1},
                "weight": 20,
                "hold": 3.0,
                "move_speed": 1.5,
                "trigger_blink_on_return": True,
                # 扫视后常常停在分心远眺或回过神去思考
                "chain_to": {"分心远眺": 0.4, "深度思考": 0.2},
            },
            {
                "name": "右侧扫视",
                "params": {"v_head_x": 6.1, "v_eye_x": 0.33, "v_head_y": 1.1},
                "weight": 20,
                "hold": 3.0,
                "move_speed": 1.5,
                "trigger_blink_on_return": True,
                "chain_to": {"分心远眺": 0.4, "深度思考": 0.2},
            },
            {
                "name": "失神发呆",
                "params": {"v_head_y": -2.8, "v_head_z": 1.35, "v_eye_y": -0.17},
                "weight": 20,
                "hold": 6.0,
                "move_speed": 4.0,
                "custom_blink": {"close": 1.2, "stay": 0.8, "open": 1.5},
                # 发呆完了往往是缓缓深呼吸"回神"
                "chain_to": {"深呼吸": 0.4},
            },
            {
                "name": "侧身偷瞄",
                "params": {"v_body_x": 5.2, "v_head_x": 2.3, "v_eye_x": 0.34},
                "weight": 20,
                "hold": 3.0,
                "move_speed": 1.8,
                # 偷瞄后常常害羞回避（被发现了的感觉）
                "chain_to": {"害羞回避": 0.3, "分心远眺": 0.2},
            },
            {
                "name": "分心远眺",
                "params": {"v_head_x": 7.5, "v_head_y": 2.9, "v_eye_x": -0.34, "v_eye_y": 0.17},
                "weight": 20,
                "hold": 4.0,
                "trigger_blink_on_return": True,
                # 远眺过后多是思考或继续发呆
                "chain_to": {"深度思考": 0.3, "失神发呆": 0.2},
            },
            {
                "name": "好奇歪头",
                "params": {
                    "v_head_z": 6.3,
                    "v_head_x": 2.9,
                    "v_head_y": 1.7,
                    "v_eye_x": -0.23,
                    "v_eye_y": 0.11,
                },
                "weight": 20,
                "hold": 4.0,
                # 歪头思考很自然过渡到深度思考
                "chain_to": {"深度思考": 0.4, "向下检查": 0.2},
            },
            {
                "name": "深呼吸",
                "params": {"v_head_y": 3.9, "v_body_y": 2.5, "v_head_z": 1.1},
                "weight": 10,
                "hold": 1.0,
                "move_speed": 2.0,
                "blink_at_t": 0.4,
                "custom_blink": {"close": 1.0, "stay": 1.5, "open": 0.8},
                # 深呼吸是"段落终止符"，不再衔接任何动作
            },
            {
                "name": "向下检查",
                "params": {"v_head_y": -5.5, "v_eye_y": -0.39, "v_body_y": -0.9},
                "weight": 10,
                "hold": 2.5,
                "move_speed": 1.5,
                "chain_to": {"好奇歪头": 0.25},
            },
            {
                "name": "害羞回避",
                "params": {
                    "v_head_x": -5.2,
                    "v_head_y": -3.5,
                    "v_head_z": -2.9,
                    "v_eye_x": 0.28,
                    "v_eye_y": -0.17,
                    "v_blush": 0.55,
                },
                "weight": 10,
                "hold": 5.0,
                # 害羞过后通常是深呼吸缓和情绪
                "chain_to": {"深呼吸": 0.5},
            },
            {
                "name": "深度思考",
                "params": {"v_head_y": 4.5, "v_head_z": -2.3, "v_eye_y": 0.28, "v_eye_x": 0.0},
                "eye_oscillation": 0.15,
                "osc_freq": 1.5,
                "weight": 10,
                "hold": 5.0,
                "move_speed": 2.0,
                # 思考完了恍然大悟 → 失神发呆 / 终止
                "chain_to": {"失神发呆": 0.3},
            },
        ]

        # 动作链上下文
        # _pending_chain_action：下次 IDLE → 触发时强制选这个动作（绕过权重抽样）。
        # _chain_count：累计已发生的链式衔接次数，>= _max_chain_count 时强制断链。
        # _current_chain_to：当前正在执行的动作的 chain_to 表，RETURNING 完成时
        # 抽签用。每次进入新动作时由 IDLE 分支写入。
        self._pending_chain_action: str | None = None
        self._chain_count: int = 0
        self._max_chain_count: int = 2  # 最多连续衔接 2 次，避免一直在动
        self._current_chain_to: dict[str, float] = {}

        # 调试轮询开关：开启后持续循环播放所有宏观动作
        self._macro_debug_loop: bool = cfg.macro_debug_loop

        # ── 身体与上半身灵动度 ──────────────────────────
        # 头部 → 身体耦合：两个二阶弹簧系统分别让 v_body_x 跟随 v_head_x、
        # v_body_z 跟随 v_head_z，产生"带惯性滞后 + 轻微回弹"的从动质感。
        self._body_follow_head_enabled: bool = cfg.body_follow_head_enabled
        self._body_freq: float = cfg.body_follow_head_f
        self._body_damp: float = cfg.body_follow_head_z
        self._body_w_rx: float = cfg.body_follow_head_w_rx
        self._body_w_rz: float = cfg.body_follow_head_w_rz
        self._body_w_comp: float = cfg.body_follow_head_w_comp
        self._body_coupling_x = SecondOrderDynamics(
            frequency=max(0.1, self._body_freq),
            damping=self._body_damp,
            response=0.0,
            x0=0.0,
        )
        self._body_coupling_z = SecondOrderDynamics(
            frequency=max(0.1, self._body_freq),
            damping=self._body_damp,
            response=0.0,
            x0=0.0,
        )
        # 呼吸肩相位差：胸腔前后仰与肩膀起伏不同相，形成肌肉拉动感。
        self._breath_shoulder_enabled: bool = cfg.breath_shoulder_enabled
        self._breath_shoulder_amplitude: float = cfg.breath_shoulder_amplitude
        self._breath_shoulder_lag: float = cfg.breath_shoulder_lag

        # ── 身体常驻律动 ──────────────────────────────
        # 真人站立时身体从不静止：横向重心、纵向沉稳、侧向肩腰总在持续但
        # 轻缓地摇动。用 value noise 三轴独立生成常驻摇摆，作为身体"活着"的
        # 底噪，叠加上去后待机不再钉在原地。关闭总开关则退回仅有呼吸的极弱
        # 摆动（几乎不可感知）。
        self._body_idle_enabled: bool = cfg.body_idle_enabled
        self._body_idle_scale: float = cfg.body_idle_scale
        self._body_idle_x_amp: float = cfg.body_idle_x_amp
        self._body_idle_y_amp: float = cfg.body_idle_y_amp
        self._body_idle_z_amp: float = cfg.body_idle_z_amp
        # 常驻律动的随机相位：每次启动走一条不完全相同的曲线，避免千篇一律。
        self._body_idle_phase: float = random.uniform(0, 1000)

        # 参数 ID
        self.param_eye_l = "v_eye_left"
        self.param_eye_r = "v_eye_right"
        self.param_eye_x = "v_eye_x"
        self.param_eye_y = "v_eye_y"
        self.param_head_x = "v_head_x"
        self.param_head_y = "v_head_y"
        self.param_head_z = "v_head_z"

        # 调试轮询：启动时自动填充演示队列并立即开始循环
        if self._macro_debug_loop:
            self.macro_demo_queue = self.macro_library.copy()
            self.next_macro_trigger_time = 0
            logger.info(f"调试轮询模式已启用，将循环播放所有 {len(self.macro_library)} 个宏观动作（间隔 2s）")

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
                # 10% 概率短间隔双连眨（人类常有的"再眨一下"），其余按主区间。
                if random.random() < 0.10:
                    self.next_blink_time = random.uniform(0.4, 0.6)
                else:
                    self.next_blink_time = random.uniform(
                        self._blink_min_interval, self._blink_max_interval
                    )

        macro_eye_l = self.macro_current_params.get("v_eye_l", 0.0)
        macro_eye_r = self.macro_current_params.get("v_eye_r", 0.0)
        output[self.param_eye_l] = max(0.0, min(1.0, self.current_eye_open + macro_eye_l))
        output[self.param_eye_r] = max(0.0, min(1.0, self.current_eye_open + macro_eye_r))

        # 2) 呼吸
        breath_z = math.sin(elapsed * self.breath_freq * 2 * math.pi) * self.breath_amplitude

        # 3) 眼神漫游
        micro_jitter_x = math.sin(elapsed * math.pi * 6) * 0.01
        micro_jitter_y = math.cos(elapsed * math.pi * 5.5) * 0.01
        self.eye_wander_x = math.sin(elapsed * 0.3) * 0.025 + math.sin(elapsed * 0.17) * 0.015
        self.eye_wander_y = math.cos(elapsed * 0.25) * 0.02 + math.cos(elapsed * 0.13) * 0.01

        if elapsed >= self.next_saccade_time:
            if random.random() >= self._saccade_big_probability:
                # 小幅扫视：短暂一瞥后回中——眼神 95% 时间停留在镜头中心
                self.eye_x = random.uniform(
                    -self._saccade_small_amplitude_x, self._saccade_small_amplitude_x
                )
                self.eye_y = random.uniform(
                    -self._saccade_small_amplitude_y, self._saccade_small_amplitude_y
                )
                self._saccade_return_time = elapsed + random.uniform(0.2, 0.5)
            else:
                # 大幅扫视：偶发的一瞥（读弹幕感），上下瞟幅度收窄避免"不看镜头"
                self.eye_x = random.uniform(
                    -self._saccade_big_amplitude_x, self._saccade_big_amplitude_x
                )
                self.eye_y = random.uniform(
                    -self._saccade_big_amplitude_y, self._saccade_big_amplitude_y
                )
                self._saccade_return_time = elapsed + random.uniform(0.3, 0.6)
            self.next_saccade_time = elapsed + random.uniform(
                self._saccade_min_interval, self._saccade_max_interval
            )
        elif self._saccade_return_time > 0 and elapsed >= self._saccade_return_time:
            # 回中：一瞥结束，眼神回到镜头中心（观众/摄像头）
            self.eye_x = 0.0
            self.eye_y = 0.0
            self._saccade_return_time = 0.0

        # 4) 被动慢摆
        now = time.time()
        if not self.passive_sway_active:
            if now >= self.next_passive_sway_time:
                self.passive_sway_active = True
                self.passive_sway_timer = 0.0
                self.passive_sway_cycles = random.randint(1, 2)
                self.passive_sway_duration = self.passive_sway_cycles * 3.0
                self.next_passive_sway_time = now + random.uniform(
                    self._passive_sway_min_interval,
                    self._passive_sway_max_interval,
                )
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

        # 6) 头部/身体随机微动（_head_micro_scale 控制总幅度，1.0 为原版）
        scale = self._head_micro_scale
        if self._organic_enabled:
            # 有机微动：用 value noise 替代 sin 叠加，去掉可预判的周期感。
            # 不同 seed 让三个轴各走一条独立的平滑随机曲线。
            nt = (elapsed + self._noise_phase) * 0.12
            head_micro_x = fbm(nt, seed=1) * 4.0 * scale
            head_micro_y = fbm(nt, seed=2) * 3.0 * scale
            body_sway_z = fbm(nt * 0.6, seed=3) * 3.0 * scale
        else:
            head_micro_x = (
                math.sin(elapsed * 0.12 + self.sway_offsets[0]) * 2.5 * scale
                + math.sin(elapsed * 0.05 + self.sway_offsets[1]) * 1.5 * scale
            )
            head_micro_y = (
                math.cos(elapsed * 0.1 + self.sway_offsets[2]) * 2.0 * scale
                + math.cos(elapsed * 0.07 + self.sway_offsets[3]) * 1.0 * scale
            )
            body_sway_z = (
                math.sin(elapsed * 0.07 + self.sway_offsets[1]) * 1.8 * scale
                + math.sin(elapsed * 0.03 + self.sway_offsets[3]) * 1.2 * scale
            )

        # 呼吸带动身体上下起伏：与头部呼吸 breath_z 同相，注入 v_body_y。
        breath_body_y = (
            math.sin(elapsed * self.breath_freq * 2 * math.pi)
            * self._breath_body_amplitude
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

        # 头眼联动：眼神偏移时头部轻微跟随（0.3 倍，上限 ±1.5°），
        # 避免"眼珠在动头完全僵着"的假人感；回中后联动值随 eye_x 归零。
        head_follow_eye = max(-1.5, min(1.5, self.eye_x * 0.3 * 10.0))

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

        # 呼吸肩相位差：真实呼吸是胸腔扩张伴随耸肩、肩滞后约 lag 弧度。
        # 用独立相位给 v_body_z 一个滞后分量，替代"全身同相上下浮"的机械感。
        breath_shoulder_z = 0.0
        if self._breath_shoulder_enabled and self._breath_shoulder_amplitude > 0.0:
            breath_shoulder_z = (
                math.sin(
                    elapsed * self.breath_freq * 2 * math.pi - self._breath_shoulder_lag
                )
                * self._breath_shoulder_amplitude
            )

        # 身体跟随头部二阶耦合：以"真实头部角度"（微动 + 宏观 + 振荡 + 头眼联动）
        # 为输入，输出带惯性滞后 / 轻微回弹的身体角度。刻意排除宏观 v_body_*，
        # 避免大动作目标被二阶系统二次耦合放大；也排除呼吸 Z 轴避免同频抖动。
        body_coupling_x = 0.0
        body_coupling_z = 0.0
        if self._body_follow_head_enabled:
            head_eff_x = (
                head_micro_x
                + self.macro_current_params.get("v_head_x", 0.0)
                + head_osc_x
                + head_follow_eye
            )
            head_eff_z = self.macro_current_params.get("v_head_z", 0.0) + head_osc_z
            body_coupling_x = self._body_coupling_x.update(
                head_eff_x * self._body_w_rx, logic_delta
            )
            # 侧倾跟随 + 重心代偿：头横向转时身体反向微补偿，产生重心侧移感。
            body_coupling_z = self._body_coupling_z.update(
                head_eff_z * self._body_w_rz - head_eff_x * self._body_w_comp,
                logic_delta,
            )

        # 身体常驻律动：用 value noise 三轴独立、持续生成身体摇摆底噪。
        # 频率很慢（0.05~0.08），像真人站立时无意识的重心浮动；幅度由
        # body_idle_* 控制，scale 统一放大缩小。作 Independent 的"活着"底噪，
        # 与头部微动、被动慢摆、宏观动作互不耦合，确保待机也在"动"。
        body_idle_x = 0.0
        body_idle_y = 0.0
        body_idle_z = 0.0
        if self._body_idle_enabled:
            # 频率 ~0.15Hz（约 6.6 秒一个周期）：快过呼吸那点几乎不可察的摆动，
            # 又慢到像"无意识重心浮动"，不会像抽搐。三轴走独立噪声曲线 + 轻微
            # 倍率差，避免三轴同频显得僵硬。
            idle_t = (elapsed + self._body_idle_phase) * 0.15
            body_idle_x = fbm(idle_t, seed=21) * self._body_idle_x_amp
            body_idle_y = fbm(idle_t * 0.9 + 7.0, seed=22) * self._body_idle_y_amp
            body_idle_z = fbm(idle_t * 1.1 + 13.0, seed=23) * self._body_idle_z_amp
            body_idle_x *= self._body_idle_scale
            body_idle_y *= self._body_idle_scale
            body_idle_z *= self._body_idle_scale

        raw_output: dict[str, float] = {}
        raw_output[self.param_eye_x] = base_eye_x + self.macro_current_params.get("v_eye_x", 0.0) + eye_osc_x
        raw_output[self.param_eye_y] = base_eye_y + self.macro_current_params.get("v_eye_y", 0.0)
        raw_output[self.param_head_x] = (
            head_micro_x + self.macro_current_params.get("v_head_x", 0.0) + head_osc_x + head_follow_eye
        )
        raw_output[self.param_head_y] = head_micro_y + self.macro_current_params.get("v_head_y", 0.0)
        raw_output[self.param_head_z] = (
            breath_z + self.macro_current_params.get("v_head_z", 0.0) + head_osc_z + self.passive_sway_val
        )
        raw_output["v_body_x"] = (
            self.macro_current_params.get("v_body_x", 0.0)
            + body_coupling_x
            + body_idle_x
        )
        raw_output["v_body_y"] = (
            self.macro_current_params.get("v_body_y", 0.0)
            + breath_body_y
            + body_idle_y
        )
        raw_output["v_body_z"] = (
            body_sway_z
            + self.macro_current_params.get("v_body_z", 0.0)
            + head_osc_z
            + (self.passive_sway_val * 0.4)
            + body_coupling_z
            + breath_shoulder_z
            + body_idle_z
        )
        raw_output["v_blush"] = self.macro_current_params.get("v_blush", 0.0)

        # 8) 安全平滑层（阻尼 + 速率限制）
        # damp_factor 影响整体平滑（越小越跟手）；max_speeds 限制单帧最大变化
        # 速率（度/秒）。幅度差异大的两个动作切换时，跟踪过快会形成
        # "急冲→急停"的顿挫感，故用较大阻尼 + 适中速率让过渡自然融合。
        damp_factor = 0.35
        max_speeds = {
            self.param_head_x: 40.0,
            self.param_head_y: 40.0,
            self.param_head_z: 40.0,
            "v_body_x": 40.0,
            "v_body_y": 40.0,
            "v_body_z": 40.0,
            self.param_eye_x: 3.0,
            self.param_eye_y: 3.0,
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

    def _maybe_pick_chain_action(self) -> str | None:
        """根据 _current_chain_to 表抽签决定下一个衔接动作。

        - 演示模式 / 已达 _max_chain_count → 直接 None（断链）
        - chain_to 表为空 → None
        - 否则按概率字典依次掷骰子，命中即返回该动作名；都没命中也返回 None
        """

        if self.macro_demo_queue:
            return None
        if self._chain_count >= self._max_chain_count:
            return None
        if not self._current_chain_to:
            return None

        # 按字典里的"动作名 → 概率"顺序掷骰子；任意一个命中就用它。
        # 总概率不必等于 1：剩下的概率就是"不衔接，断链"。
        for name, probability in self._current_chain_to.items():
            if random.random() < float(probability):
                return name
        return None

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
            # 调试轮询开启时忽略"表演中"状态，持续循环宏观动作供观察。
            if self.is_performing and not self._macro_debug_loop:
                # 表演中暂停宏观动作触发；下次重排在表演结束之后。
                self.next_macro_trigger_time = now + random.uniform(
                    self._macro_min_interval, self._macro_max_interval
                )
                # 表演时清空动作链上下文，避免说话结束后还接着上次的链。
                self._pending_chain_action = None
                self._chain_count = 0
                self._current_chain_to = {}
                return

            if now < self.next_macro_trigger_time:
                return

            # 1) 优先消费动作链衔接：上一个动作的 chain_to 抽签命中时直接用，
            # 不走加权随机；上次衔接缓冲已用过，立即清空。
            action = None
            if self._pending_chain_action is not None:
                chain_name = self._pending_chain_action
                self._pending_chain_action = None
                hit = next(
                    (a for a in self.macro_library if a["name"] == chain_name),
                    None,
                )
                if hit is not None:
                    self._chain_count += 1
                    logger.info(
                        f"链式衔接: -> {chain_name}（已连 {self._chain_count} 次）"
                    )
                    action = hit
                else:
                    self._chain_count = 0  # 找不到目标动作，断链
            else:
                self._chain_count = 0

            # 2) 没有链式衔接 → 演示队列 / 加权随机
            if action is None:
                if self.macro_demo_queue:
                    action = self.macro_demo_queue.pop(0)
                    logger.info(f"[演示] 播放: {action['name']} (剩余: {len(self.macro_demo_queue)})")
                else:
                    available = [a for a in self.macro_library if a["name"] not in self.macro_history]
                    if not available:
                        available = self.macro_library
                    action = random.choices(available, weights=[a["weight"] for a in available])[0]

            # 记下当前动作的 chain_to 表，等 RETURNING 完成时按它抽签下个衔接动作
            self._current_chain_to = dict(action.get("chain_to", {}) or {})

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

                # 动作链衔接：本次动作刚收完，按 chain_to 抽签下一个动作。
                # 命中 → 立即触发（next_macro_trigger_time = now，IDLE 分支
                # 下一次进来就处理）；没命中 → 走原本的 6-15s 等待。
                # 演示模式 / 已达最大连接数 → 跳过抽签，避免动作不断。
                next_in_chain = self._maybe_pick_chain_action()
                if next_in_chain is not None:
                    self._pending_chain_action = next_in_chain
                    self.next_macro_trigger_time = now  # 立即进入下一个
                elif self.macro_demo_queue:
                    # 演示模式：动作之间留 2 秒间隙就够了。
                    self.next_macro_trigger_time = now + 2.0
                elif self._macro_debug_loop:
                    # 调试轮询：队列耗尽后自动重填，无限循环
                    self.macro_demo_queue = self.macro_library.copy()
                    self.next_macro_trigger_time = now + 2.0
                    logger.info("调试轮询：一轮播放完毕，重新开始循环")
                else:
                    self.next_macro_trigger_time = now + random.uniform(
                        self._macro_min_interval, self._macro_max_interval
                    )
                # 当前动作执行完，链表清空（下一个动作进 IDLE 分支时会重新写入）
                self._current_chain_to = {}

    def _apply_macro_step(self, step: dict[str, Any], action_name: str = "") -> None:
        """切换到一个新的动作步：写入目标参数 + 时长 + 振荡参数。

        所有时长（move / hold / return）都按 :attr:`_motion_speed_scale` 压缩——
        默认 2.0 让动作执行速度翻倍，避免"慢吞吞像慢放"。
        """

        self.macro_target_params = step["params"]
        scale = max(0.1, self._motion_speed_scale)  # 防 0 / 负数
        # hold 时长按 sqrt(scale) 压缩，比 move 时长压缩得温和点：
        # 因为造型 hold 时间太短会显得"刚摆好就跑"，过度压缩反而违和。
        self.macro_duration_hold = step.get("hold", 5.0) / (scale ** 0.5)
        move_speed = step.get("move_speed", 2.0) / scale
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
