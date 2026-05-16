"""VTube Studio 高层表演接口。

把 :class:`VTSConnection`、动画器、音频播放器粘合成一个统一对象，
让 ``say_and_perform`` 这种上层调用只需要：

.. code-block:: python

    await performer.perform(audio_bytes, motion="nod")

内部会自动：

1. ``set_speaking(True)`` 让 SpeechAnimator 进入说话模式（嘴型动）。
2. 如果 ``motion`` 落在已知映射（如 ``think``），同时切换 intent / 触发热键。
3. 异步播放音频到 VB-Cable。
4. 播完后 ``set_speaking(False)``，让 SpeechAnimator 自动回到 IDLE。

所有阶段都是异步且非阻塞的，多次连续调用会被 ``_perform_lock`` 串行化。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, TYPE_CHECKING

from src.kernel.logger import get_logger

from .animation import AutoAnimator, SpeechAnimator
from .connection import VTSConnection, build_default_token_path

if TYPE_CHECKING:
    from ..audio import AudioPlayer
    from ..config import SherpaOnnxVoiceChatterConfig


logger = get_logger("voice_chatter.vts.performer")


# SpeechAnimator 接受的合法 intent 名（其它值会被忽略，不报错）。
_VALID_INTENTS: set[str] = {"IDLE", "THINKING", "NARRATING", "CONFUSED", "EXCITED", "SURPRISED"}


class VTSPerformer:
    """统一的"说话 + 动画 + 音频"表演器，仅 vtb 模式使用。

    与 say_and_perform 的协议一致：

    - ``emotion``：``"类型:强度"`` 字符串，驱动 SpeechAnimator 的 emotion_matrix
      （嘴型基准 / 头部偏移 / 身体晃动幅度）。常用值：``neutral / happy / sad /
      angry / surprised``，强度 ``1-3``。
    - ``intent``：动作意图字符串，驱动 SpeechAnimator 的 intent_map（头部基准
      姿态、眼神方向）。合法值：``IDLE / THINKING / NARRATING / CONFUSED /
      EXCITED / SURPRISED``。
    - 可选：``hotkey_map`` 把 emotion / intent 映射到 VTS Hotkey ID 触发热键
      （需要在 VTube Studio 里手动设置好 hotkey），仅作补充。
    """

    def __init__(
        self,
        *,
        plugin_config: "SherpaOnnxVoiceChatterConfig",
        audio_player: "AudioPlayer",
    ) -> None:
        """根据插件配置构造连接 + 动画器；读取 hotkey_map（可选热键映射）。"""

        self.plugin_config = plugin_config
        self.audio_player = audio_player

        self.connection: VTSConnection | None = None
        self.auto_animator: AutoAnimator | None = None
        self.speech_animator: SpeechAnimator | None = None
        self._initialized: bool = False
        self._perform_lock = asyncio.Lock()

        # 可选 hotkey_map：把 emotion 主类型或 intent 名字映射到 VTS Hotkey ID。
        # 例如 {"happy": "SmileHotkey", "THINKING": "ThinkHotkey"}。
        # 没配置就不触发热键，全靠参数注入实现表演。
        motion_section = getattr(plugin_config, "motion", None)
        hotkey_map = getattr(motion_section, "hotkey_map", None) or {}
        self._hotkey_map: dict[str, str] = {
            str(k).lower(): str(v) for k, v in hotkey_map.items() if v
        }

    @property
    def is_ready(self) -> bool:
        """表演器是否已建立 VTS 连接（可以驱动嘴型）。"""

        return (
            self._initialized
            and self.connection is not None
            and self.connection.is_connected
        )

    # ── 生命周期 ──────────────────────────────────────

    async def initialize(self) -> bool:
        """启动 VTS 连接 + 动画器；失败时返回 False，不抛异常。"""

        if self._initialized:
            return self.is_ready

        vts_cfg = self.plugin_config.vts
        if not vts_cfg.enabled:
            logger.info("VTS 已在配置中禁用，VTSPerformer 进入空转模式。")
            self._initialized = True
            return False

        try:
            self.connection = VTSConnection(
                host=vts_cfg.host,
                port=vts_cfg.port,
                token_path=build_default_token_path(),
            )
            # AutoAnimator 接收 idle_animation 配置，把眨眼/扫视/被动摆动/
            # 宏观动作触发等频率与幅度暴露成可调旋钮（默认值已经比原版激进）。
            self.auto_animator = AutoAnimator(
                idle_animation_config=getattr(self.plugin_config, "idle_animation", None),
            )
            # 把 audio_player 的 envelope_tracker 注入 SpeechAnimator，
            # 让说话期间的头部 / 身体微动跟着 TTS 音频包络起伏，做出"语调律动"。
            # 配置 audio_drive 段控制具体增益与开关；为 None 时 SpeechAnimator
            # 会自动退化回原来的固定 sin 波动逻辑。
            self.speech_animator = SpeechAnimator(
                envelope_tracker=self.audio_player.envelope_tracker,
                audio_drive_config=getattr(self.plugin_config, "audio_drive", None),
            )
            self.connection.animators = [self.auto_animator, self.speech_animator]

            ok = await self.connection.connect()
            self._initialized = True
            if ok:
                logger.info("✅ VTSPerformer 初始化完成（已连接 VTube Studio）")
            else:
                logger.warning("VTSPerformer 初始化完成，但 VTS 未连接，将仅播放音频。")
            return ok
        except Exception as exc:
            logger.error(f"VTSPerformer 初始化失败: {exc}", exc_info=True)
            self._initialized = True  # 防止反复重试
            return False

    async def shutdown(self) -> None:
        """断开 VTS 并释放资源。"""

        if self.connection is not None:
            try:
                await self.connection.close()
            except Exception as exc:
                logger.warning(f"关闭 VTS 连接出错: {exc}")
        self.connection = None
        self.auto_animator = None
        self.speech_animator = None
        self._initialized = False
        logger.info("VTSPerformer 已关闭")

    # ── 解析 emotion / intent ────────────────────────

    @staticmethod
    def _normalize_intent(intent: str) -> str:
        """把模型给的 intent 字串转成 SpeechAnimator 接受的合法值。"""

        normalized = (intent or "").strip().upper()
        if normalized in _VALID_INTENTS:
            return normalized
        return "NARRATING"

    @staticmethod
    def _split_emotion(emotion: str) -> tuple[str, int]:
        """``"happy:2"`` → ``("happy", 2)``；非法值降级为 ``("neutral", 2)``。"""

        if not emotion:
            return ("neutral", 2)
        parts = emotion.strip().lower().split(":", 1)
        emo_type = parts[0] or "neutral"
        if len(parts) > 1 and parts[1].strip().isdigit():
            level = int(parts[1])
        else:
            level = 2
        level = max(1, min(3, level))
        return emo_type, level

    def _resolve_hotkey(self, *keys: str) -> str | None:
        """按顺序在 hotkey_map 里查找；返回第一个命中的热键 ID。"""

        for key in keys:
            if not key:
                continue
            hit = self._hotkey_map.get(key.strip().lower())
            if hit:
                return hit
        return None

    # ── 主接口 ────────────────────────────────────────

    @asynccontextmanager
    async def speaking_session(
        self,
        *,
        emotion: str = "neutral:1",
        intent: str = "NARRATING",
    ) -> AsyncIterator["VTSPerformer"]:
        """整段对话级别的"说话作用域"。

        包住整次 say_and_perform 调用：进入时只设置一次 emotion / intent /
        speaking 状态、触发一次热键；退出时统一收尾。中间多次 :meth:`play`
        播放各分段，**段与段之间不会切换 speaking 标志**，避免 SpeechAnimator
        陷入 emotion 回归状态机导致的"段间卡顿"。
        """

        emo_type, emo_level = self._split_emotion(emotion)
        normalized_intent = self._normalize_intent(intent)

        async with self._perform_lock:
            triggered_session = False
            if self.is_ready and self.speech_animator and self.auto_animator:
                self.speech_animator.set_emotion(emo_type, level=emo_level)
                self.speech_animator.set_intent(normalized_intent)
                self.speech_animator.set_speaking(True)
                self.auto_animator.set_performing(True)
                triggered_session = True

                hotkey_id = self._resolve_hotkey(normalized_intent, emo_type)
                if hotkey_id and self.connection is not None:
                    try:
                        ok = await self.connection.trigger_hotkey(hotkey_id)
                        logger.info(
                            f"VTS 热键触发: emotion={emo_type}:{emo_level} "
                            f"intent={normalized_intent} -> hotkey={hotkey_id} ok={ok}"
                        )
                    except Exception as exc:
                        logger.warning(f"触发 VTS 热键失败: {exc}")

            try:
                yield self
            finally:
                if triggered_session and self.speech_animator and self.auto_animator:
                    self.speech_animator.set_speaking(False)
                    self.auto_animator.set_performing(False)

    async def play(self, audio: bytes) -> bool:
        """在一个 :meth:`speaking_session` 上下文里串行播放一段音频。"""

        if not audio:
            logger.warning("VTSPerformer.play 接收到空音频，跳过。")
            return False
        await self.audio_player.play_audio(audio)
        return True

    async def perform(
        self,
        audio: bytes,
        *,
        emotion: str = "neutral:1",
        intent: str = "NARRATING",
    ) -> bool:
        """单段播放：自动包一层 :meth:`speaking_session`。

        多段播放请改用 :meth:`speaking_session` + 多次 :meth:`play`，
        以避免段间 emotion 缓退/重置造成卡顿。
        """

        if not audio:
            logger.warning("VTSPerformer.perform 接收到空音频，跳过。")
            return False

        async with self.speaking_session(emotion=emotion, intent=intent):
            return await self.play(audio)

    async def trigger_demo(self) -> bool:
        """触发 AutoAnimator 的演示队列（依次播放所有宏观动作）。"""

        if not self.is_ready or self.auto_animator is None:
            return False
        self.auto_animator.start_demo()
        return True


__all__ = ["VTSPerformer"]
