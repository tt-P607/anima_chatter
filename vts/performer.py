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

from src.app.plugin_system.api.log_api import get_logger

from ..constants import normalize_intent, split_emotion
from .animation import AutoAnimator, SpeechAnimator
from .connection import VTSConnection, build_default_token_path

if TYPE_CHECKING:
    from ..audio import AudioPlayer
    from ..config import AnimaChatterConfig


logger = get_logger("anima_chatter.vts.performer")


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
        plugin_config: "AnimaChatterConfig",
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
        vts_section = plugin_config.vts
        self._hotkey_map: dict[str, str] = {
            str(key).lower(): str(value)
            for key, value in vts_section.hotkey_map.items()
            if value
        }

        # 可选 expression_map：把 emotion / intent 映射到 .exp3.json 表情文件 + 描述。
        # 走 ExpressionActivationRequest，不需要 VTS 配 hotkey。
        # 关键设计：**互斥**——每次说话最多激活一个命中的表情，避免同时激活多个
        # 互相冲突的表情（例如多个手部表情同时 active 会显示多只手）。
        # _expression_map: 大小写归一的 key → 文件名（运行时触发用）
        # _expression_desc: 保留原 key 大小写 → 描述（注入 prompt 用）
        self._expression_map: dict[str, str] = {}
        self._expression_desc: dict[str, str] = {}
        for raw_key, entry in vts_section.expression_map.items():
            key = str(raw_key).strip()
            if not key:
                continue
            file = str(entry.get("file") or "").strip()
            if not file:
                continue
            self._expression_map[key.lower()] = file
            desc = str(entry.get("desc") or "").strip()
            if desc:
                self._expression_desc[key] = desc

        # 当前激活的表情集合；每次 speaking_session 入口由 _sync_expression
        # 维护"切换 + 互斥"。
        self._active_expressions: set[str] = set()

        # speaking_session 暂存的顶层 emotion / intent（首段播放前才激活）；
        # session 进入时填充，start_speech_playback 时取用，session 退出清。
        self._pending_session_emotion: tuple[str, int] | None = None
        self._pending_session_intent: str | None = None
        self._session_started: bool = False

    def get_expression_hints(self) -> dict[str, str]:
        """返回 ``{intent_or_emotion_key: 描述}`` 字典。

        prompt 构建器（:mod:`prompts.builder`）用它把"哪个 intent 会触发哪个
        额外动作"动态注入到 LLM 的 intent schema 中，让模型场景化选择更准。

        Returns:
            ``{原 key: desc}`` 副本（不含 file 字段，prompt 不需要知道文件名）。
        """

        return dict(self._expression_desc)

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
                idle_animation_config=self.plugin_config.idle_animation,
            )
            # 把 audio_player 的 envelope_tracker 注入 SpeechAnimator，
            # 让说话期间的头部 / 身体微动跟着 TTS 音频包络起伏，做出"语调律动"。
            # 配置 audio_drive 段控制具体增益与开关；为 None 时 SpeechAnimator
            # 会自动退化回原来的固定 sin 波动逻辑。
            self.speech_animator = SpeechAnimator(
                envelope_tracker=self.audio_player.envelope_tracker,
                audio_drive_config=self.plugin_config.audio_drive,
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

        # 关闭前先停掉所有激活的表情，避免下次启动时手部姿势/魔法杖等仍挂着。
        if self.connection is not None and self._active_expressions:
            try:
                await self._sync_expressions(None)
            except Exception as exc:
                logger.debug(f"shutdown 时停用表情失败（忽略）: {exc}")

        if self.connection is not None:
            try:
                await self.connection.close()
            except Exception as exc:
                logger.warning(f"关闭 VTS 连接出错: {exc}")
        self.connection = None
        self.auto_animator = None
        self.speech_animator = None
        self._active_expressions.clear()
        self._initialized = False
        logger.info("VTSPerformer 已关闭")

    # ── 解析 emotion / intent ────────────────────────
    # 这两个方法保留作为 staticmethod 包装层，仅转发到 :mod:`..constants` 里
    # 的统一实现。主要是为了兼容外部测试 / 历史调用方；新代码请直接 import
    # constants.normalize_intent / split_emotion。

    @staticmethod
    def _normalize_intent(intent: str) -> str:
        """把模型给的 intent 字串转成 SpeechAnimator 接受的合法值。"""

        return normalize_intent(intent)

    @staticmethod
    def _split_emotion(emotion: str) -> tuple[str, int]:
        """``"happy:2"`` → ``("happy", 2)``；非法值降级为 ``("neutral", 2)``。"""

        return split_emotion(emotion)

    def _resolve_hotkey(self, *keys: str) -> str | None:
        """按顺序在 hotkey_map 里查找；返回第一个命中的热键 ID。"""

        for key in keys:
            if not key:
                continue
            hit = self._hotkey_map.get(key.strip().lower())
            if hit:
                return hit
        return None

    def _resolve_expression(self, *keys: str) -> str | None:
        """按顺序在 expression_map 里查找；返回第一个命中的 .exp3.json 文件名。"""

        for key in keys:
            if not key:
                continue
            hit = self._expression_map.get(key.strip().lower())
            if hit:
                return hit
        return None

    async def _sync_expressions(self, target: str | None) -> None:
        """把 VTS 端激活的表情切换成 ``{target}``（或全部停用）。

        互斥实现的核心：多个手部表情同时 active 会显示多只手；本方法保证
        每次说话只激活**一个**目标表情。

        Args:
            target: 这一段要激活的 .exp3.json 文件名；为 None 则停用所有
                上次激活的表情。
        """

        if self.connection is None or not self._expression_map:
            return

        new_active: set[str] = {target} if target else set()
        # 先停用上次有但本次不要的（防止多手同时显示）。
        to_deactivate = self._active_expressions - new_active
        for f in to_deactivate:
            try:
                await self.connection.set_expression(f, active=False)
            except Exception as exc:
                logger.debug(f"停用表情 {f} 失败（忽略）: {exc}")
        # 再激活本次新增的。
        to_activate = new_active - self._active_expressions
        for f in to_activate:
            try:
                ok = await self.connection.set_expression(f, active=True)
                if not ok:
                    logger.debug(f"激活表情 {f} 失败（忽略，可能文件不存在）")
            except Exception as exc:
                logger.debug(f"激活表情 {f} 失败（忽略）: {exc}")

        self._active_expressions = new_active

    async def switch_segment_intent(
        self,
        intent: str,
        *,
        emotion_main_for_expression: str = "neutral",
    ) -> None:
        """段间切换 intent（含表情）。在已经处于 speaking_session 时调用。

        用于行内 ``[motion:NAME]`` 标记：每段播放前调一次，让虚拟形象在
        说话过程中根据当前句的语义切动作。

        与 :meth:`speaking_session` 的区别：
        - speaking_session 是**整段**会话级的 intent 默认值（顶层 say_and_perform
          的 intent 参数）。
        - switch_segment_intent 是**段内**瞬时切换，只切 intent + expression，
          不改 emotion / speaking 状态。

        Args:
            intent: 新的 intent 名（合法 18 个之一，非法值降级 NARRATING）。
            emotion_main_for_expression: 当 intent 没命中 expression_map 时的
                兜底匹配键。一般传顶层 say_and_perform 的 emotion 主类型。
        """

        if not self.is_ready or self.speech_animator is None:
            return

        normalized = self._normalize_intent(intent)
        self.speech_animator.set_intent(normalized)

        if not self._expression_map:
            return

        expression_file = self._resolve_expression(normalized, emotion_main_for_expression)
        try:
            await self._sync_expressions(expression_file)
        except Exception as exc:
            logger.debug(f"段间切换表情失败（忽略）: {exc}")

    # ── 主接口 ────────────────────────────────────────

    @asynccontextmanager
    async def speaking_session(
        self,
        *,
        emotion: str = "neutral:1",
        intent: str = "NARRATING",
    ) -> AsyncIterator["VTSPerformer"]:
        """整段对话级别的"说话作用域"。

        进入 session **不**立即触发动作 / 表情——避免在 TTS 推理还没结束时
        VTB 就开始做动作。真正的动作链由 :meth:`start_speech_playback` 在
        首段音频要播放前触发。这样的时序是：

        1. ``async with speaking_session(...)`` 进入 → 只持锁，不动 VTB
        2. ``call await tts.synthesize(...)`` 等待 TTS 推理（数秒）
        3. ``await performer.start_speech_playback(...)`` ← 此时才真正起动作
        4. ``await performer.play(audio)`` 播音频
        5. ...
        6. session 退出 → 统一收尾（清 speaking / expression）
        """

        emo_type, emo_level = self._split_emotion(emotion)
        normalized_intent = self._normalize_intent(intent)
        # 暂存顶层 emotion / intent，等 start_speech_playback 时取用。
        self._pending_session_emotion = (emo_type, emo_level)
        self._pending_session_intent = normalized_intent
        self._session_started: bool = False

        async with self._perform_lock:
            try:
                yield self
            finally:
                # 收尾：撤销 start_speech_playback 留下的状态。
                if self._session_started and self.speech_animator and self.auto_animator:
                    self.speech_animator.set_speaking(False)
                    self.auto_animator.set_performing(False)
                # 退出 session 立即清空所有激活的 expression。这样模型说完
                # 一段话、动作就回到默认（中性）状态，避免表情挂着不动。
                if self._expression_map and self._active_expressions:
                    try:
                        await self._sync_expressions(None)
                    except Exception as exc:
                        logger.debug(f"退出 session 时停用表情失败（忽略）: {exc}")
                self._pending_session_emotion = None
                self._pending_session_intent = None
                self._session_started = False

    async def start_speech_playback(self) -> None:
        """在首段音频准备好播放时**真正**触发动作链。

        由 say_and_perform 的 ``consume_in_order`` 在首段播放前调用。
        作用：

        - ``set_speaking(True)`` / ``set_performing(True)``（嘴型 + 暂停待机动画）
        - 触发顶层 hotkey（如有映射）
        - 激活顶层 expression（如有映射）

        幂等——第二次调用什么也不做（多段说话只首段触发一次）。
        """

        if self._session_started:
            return
        if not self.is_ready or not self.speech_animator or not self.auto_animator:
            return

        emotion_pair = self._pending_session_emotion
        intent_value = self._pending_session_intent
        if emotion_pair is None or intent_value is None:
            return
        emo_type, emo_level = emotion_pair

        self.speech_animator.set_emotion(emo_type, level=emo_level)
        self.speech_animator.set_intent(intent_value)
        self.speech_animator.set_speaking(True)
        self.auto_animator.set_performing(True)
        self._session_started = True

        hotkey_id = self._resolve_hotkey(intent_value, emo_type)
        if hotkey_id and self.connection is not None:
            try:
                ok = await self.connection.trigger_hotkey(hotkey_id)
                logger.info(
                    f"VTS 热键触发: emotion={emo_type}:{emo_level} "
                    f"intent={intent_value} -> hotkey={hotkey_id} ok={ok}"
                )
            except Exception as exc:
                logger.warning(f"触发 VTS 热键失败: {exc}")

        expression_file = self._resolve_expression(intent_value, emo_type)
        if self._expression_map:
            try:
                await self._sync_expressions(expression_file)
                if expression_file:
                    logger.info(
                        f"VTS 表情激活: emotion={emo_type}:{emo_level} "
                        f"intent={intent_value} -> expr={expression_file}"
                    )
            except Exception as exc:
                logger.warning(f"同步 VTS 表情失败: {exc}")

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
            await self.start_speech_playback()
            return await self.play(audio)

    async def trigger_demo(self) -> bool:
        """触发 AutoAnimator 的演示队列（依次播放所有宏观动作）。"""

        if not self.is_ready or self.auto_animator is None:
            return False
        self.auto_animator.start_demo()
        return True


__all__ = ["VTSPerformer"]
