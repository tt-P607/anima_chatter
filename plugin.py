"""anima_chatter 插件入口。

支持两种运行模式：

- voice 模式（``platform == "local_asr"``）：与 ASR 适配器配合，
  用 ``SayAction`` 把 TTS 音频回传给适配器播放。
- vtb 模式（其他平台，由 ``/vtb on`` 显式接管）：用 ``SayAndPerformAction``
  在本地播放 TTS 到 VB-Cable，并驱动 VTube Studio 虚拟形象。
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseChatter, BasePlugin, Failure, Success, Wait, WaitResumeEvent
from src.core.components.loader import register_plugin
from src.core.components.types import ChatType
from src.core.config import get_core_config
from src.core.models.stream import ChatStream
from src.core.prompt import get_prompt_manager
from src.kernel.llm import LLMPayload, ROLE, Text, ToolRegistry
from src.kernel.llm.payload.tooling import LLMUsable

from pathlib import Path

from .actions import (
    EndVoiceCallAction,
    SayAction,
    SayAndPerformAction,
    SingSongAction,
    StartVoiceCallAction,
    AnimaPassAndWaitAction,
)
from .audio import AudioPlayer
from .commands import VoiceCommand, VTBCommand
from .config import AnimaChatterConfig
from .modes import ChatterMode
from .prompts import (
    SYSTEM_PROMPT,
    USER_PROMPT_VOICE,
    USER_PROMPT_VTB,
    USER_PROMPT_VTB_LIVE,
    AnimaChatterPromptBuilder,
)
from .runner import run_voice_conversation
from .song_library import SongLibrary
from .sub_agent import (
    VOICE_CHATTER_SUB_AGENT_PROMPT_TEMPLATE,
    SUB_AGENT_PROMPT_VTB,
    SUB_AGENT_PROMPT_LIVE,
)
from .vts import VTSPerformer


logger = get_logger("anima_chatter")

_PASS_AND_WAIT = "action-pass_and_wait"

# 接管 / 释放命令使用的 chatter 签名常量。
_CHATTER_SIGNATURE = "anima_chatter:chatter:anima_chatter"


class AnimaChatter(BaseChatter):
    """anima_chatter：语音通话 / VTube Studio 虚拟形象通用 Chatter。"""

    chatter_name = "anima_chatter"
    chatter_description = (
        "语音通话与 VTube Studio 虚拟形象互动通用 Chatter。"
        "platform=local_asr 时为实时通话模式；其他平台需通过 /vtb on 显式接管。"
    )
    associated_platforms = ["local_asr", "bilibili_live"]
    chat_type = ChatType.ALL
    dependencies = ["asr_adapter_anima:adapter:asr_adapter_anima"]

    # 默认值；apply_stream_runtime_options 会按 platform 动态覆写。
    stream_tick_interval = 0.1
    allow_message_buffer = False

    def _get_plugin_config(self) -> AnimaChatterConfig | None:
        """返回插件配置。"""

        config = getattr(self.plugin, "config", None)
        return config if isinstance(config, AnimaChatterConfig) else None

    def _resolve_mode(self, chat_stream: ChatStream | None = None) -> ChatterMode:
        """根据当前流的 platform 决定运行模式。

        与 :meth:`AnimaChatterPromptBuilder.resolve_mode` 保持一致：

        - ``local_asr`` → ``voice``
        - 直播平台（如 ``bilibili_live``） → ``vtb_live``
        - 其他 → ``vtb``
        """

        if chat_stream is None:
            return "vtb"
        return AnimaChatterPromptBuilder.resolve_mode(chat_stream)

    def apply_stream_runtime_options(self, chat_stream: Any) -> None:
        """根据 platform 动态决定 tick 间隔与消息缓冲策略。

        - voice 模式（local_asr）：固定 tick=0.1，禁用 buffer，沿用现状。
        - vtb 模式（其他平台）：使用 plugin section 中的配置（默认 1.0 / True）。
        """

        plugin_config = self._get_plugin_config()
        platform = getattr(chat_stream, "platform", "") or ""
        if platform == "local_asr":
            self.stream_tick_interval = 0.1
            self.allow_message_buffer = False
        elif plugin_config is not None:
            self.stream_tick_interval = float(plugin_config.plugin.tick_interval)
            self.allow_message_buffer = bool(plugin_config.plugin.allow_message_buffer)
        super().apply_stream_runtime_options(chat_stream)

    async def _build_system_prompt(self, chat_stream: ChatStream) -> str:
        """根据 platform 自动选择 voice / vtb 场景的系统提示词。

        如果 plugin 已经初始化 vts_performer，会把它的 expression_hints
        （配置 expression_map 时附带的动作描述）一并注入场景文案，让模型
        知道选某些 intent / emotion 会触发什么手部 / 道具表情。
        """

        # 仅在 plugin 上下文里能拿到 vts_performer；
        # 没有时返回空 dict，builder 会跳过额外注入逻辑。
        expression_hints: dict[str, str] = {}
        performer = getattr(self.plugin, "vts_performer", None)
        if performer is not None and hasattr(performer, "get_expression_hints"):
            try:
                expression_hints = performer.get_expression_hints()
            except Exception:
                # 取 hints 失败不致命，让 prompt 走纯静态路径。
                expression_hints = {}

        return await AnimaChatterPromptBuilder.build_system_prompt(
            self._get_plugin_config(),
            chat_stream,
            mode=self._resolve_mode(chat_stream),
            expression_hints=expression_hints,
        )

    def _build_history_text(self, chat_stream: ChatStream) -> str:
        """构建历史消息文本。"""

        return AnimaChatterPromptBuilder.build_history_text(chat_stream, self.format_message_line)

    async def _build_user_prompt(
        self,
        chat_stream: ChatStream,
        history_text: str,
        unread_lines: str,
        extra: str = "",
    ) -> str:
        """构建用户提示词（按模式选择不同模板）。"""

        return await AnimaChatterPromptBuilder.build_user_prompt(
            chat_stream,
            history_text,
            unread_lines,
            extra,
            mode=self._resolve_mode(chat_stream),
        )

    @staticmethod
    def _build_negative_behaviors_extra() -> str:
        """构建行为提醒。"""

        return AnimaChatterPromptBuilder.build_negative_behaviors_extra()

    def _is_action_suspend_enabled(self) -> bool:
        """读取纯 Action 回合的挂起开关。"""

        plugin_config = self._get_plugin_config()
        return plugin_config is None or bool(plugin_config.plugin.enable_action_suspend)

    @staticmethod
    def _append_user_payload(response: Any, text: str) -> None:
        """向当前 LLM 上下文追加 USER 文本。"""

        response.add_payload(LLMPayload(ROLE.USER, Text(text)))

    async def inject_usables(self, request: Any) -> ToolRegistry:
        """注入 Chatter 可用工具，排除 stop/send_text/sub-agent 管理工具。

        say / say_and_perform 的互斥可见由各自 ``go_activate`` 决定，
        此处不再单独过滤。
        """

        usables: list[type[LLMUsable]] = await self.get_llm_usables()
        usables = await self.modify_llm_usables(usables)
        blocked_names = {
            "action-send_text",
            "action-stop_conversation",
            "create_agent",
            "get_agent",
            "kill_agent",
        }

        registry = ToolRegistry()
        for usable in usables:
            schema = usable.to_schema()
            name = str(schema.get("function", {}).get("name", ""))
            if name in blocked_names:
                continue
            registry.register(usable)

        if registry.get_all():
            request.add_payload(LLMPayload(ROLE.TOOL, registry.get_all()))  # type: ignore[arg-type]
        return registry

    async def execute(self) -> AsyncGenerator[Wait | Success | Failure, WaitResumeEvent | None]:
        """执行主循环。"""

        from src.core.managers.stream_manager import get_stream_manager

        stream_manager = get_stream_manager()
        chat_stream = await stream_manager.activate_stream(self.stream_id)
        if chat_stream is None:
            logger.error(f"无法激活聊天流: {self.stream_id}")
            yield Failure("无法激活聊天流")
            return

        self.apply_stream_runtime_options(chat_stream)
        plugin_config = self._get_plugin_config()
        retry_limit = 1 if plugin_config is None else int(plugin_config.plugin.plain_text_retry_limit)

        runner = run_voice_conversation(
            chatter=self,
            chat_stream=chat_stream,
            logger=logger,
            pass_call_name=_PASS_AND_WAIT,
            plain_text_retry_limit=max(0, retry_limit),
            enable_action_suspend=self._is_action_suspend_enabled(),
        )
        resume_event: WaitResumeEvent | None = None
        while True:
            try:
                result = await runner.asend(resume_event)
            except StopAsyncIteration:
                return
            resume_event = yield result


@register_plugin
class AnimaChatterPlugin(BasePlugin):
    """anima_chatter 插件：通话 + VTB 虚拟形象通用 chatter。"""

    plugin_name = "anima_chatter"
    plugin_version = "1.1.0"
    plugin_description = (
        "anima_chatter：sherpa-onnx ASR 实时语音通话 + VTube Studio 虚拟形象互动 通用 Chatter"
    )
    configs = [AnimaChatterConfig]
    dependent_components = ["asr_adapter_anima:adapter:asr_adapter_anima"]

    # vtb 模式运行时资源；on_plugin_loaded 中按配置初始化。
    audio_player: AudioPlayer | None = None
    vts_performer: VTSPerformer | None = None
    # 直播清唱歌库；扫描 plugins/anima_chatter/songs/ 目录下的清唱文件。
    song_library: SongLibrary | None = None

    async def on_plugin_loaded(self) -> None:
        """注册提示词模板，并按配置初始化 VTB 资源（AudioPlayer + VTS）。"""

        from src.core.prompt import min_len, optional, wrap

        personality = get_core_config().personality
        get_prompt_manager().get_or_create(
            name="anima_chatter_system_prompt",
            template=SYSTEM_PROMPT,
            policies={
                "nickname": optional(personality.nickname),
                "alias_names": optional("、".join(personality.alias_names)),
                "personality_core": optional(personality.personality_core),
                "personality_side": optional(personality.personality_side),
                "identity": optional(personality.identity),
                "reply_style": optional(personality.reply_style),
                "background_story": optional(personality.background_story)
                .then(min_len(10))
                .then(wrap("# 背景故事\n", "\n")),
                "safety_guidelines": optional("\n".join(personality.safety_guidelines)),
                "negative_behaviors": optional("\n".join(personality.negative_behaviors)),
                "scene_guide": optional(""),
            },
        )

        # voice 模式专用 user prompt（保持原模板名以兼容现有 ASR 行为）。
        get_prompt_manager().get_or_create(
            name="anima_chatter_user_prompt",
            template=USER_PROMPT_VOICE,
            policies={
                "stream_name": optional("未知通话"),
                "current_time": optional("未知时间"),
                "platform": optional("local_asr"),
                "history": optional("").then(min_len(2)).then(wrap("# 历史通话内容\n", "\n")),
                "unreads": optional("").then(min_len(2)).then(wrap("# 新识别到的语音\n", "\n")),
                "extra": optional("").then(min_len(2)).then(wrap("# 额外提醒\n", "\n")),
            },
        )

        # vtb 模式 user prompt（Q聊/私聊等普通平台）。
        get_prompt_manager().get_or_create(
            name="anima_chatter_vtb_user_prompt",
            template=USER_PROMPT_VTB,
            policies={
                "stream_name": optional("未知聊天"),
                "current_time": optional("未知时间"),
                "platform": optional(""),
                "history": optional("").then(min_len(2)).then(wrap("# 历史对话\n", "\n")),
                "unreads": optional("").then(min_len(2)).then(wrap("# 新收到的消息\n", "\n")),
                "extra": optional("").then(min_len(2)).then(wrap("# 额外提醒\n", "\n")),
            },
        )

        # vtb_live 模式 user prompt（B 站等直播间弹幕场景）。
        # 与 vtb 模板平行：占位符一致，但提示语全部改为"直播 / 弹幕 / 直播间"
        # 措辞，让模型在直播场景下调出更合适的回应风格。
        get_prompt_manager().get_or_create(
            name="anima_chatter_vtb_live_user_prompt",
            template=USER_PROMPT_VTB_LIVE,
            policies={
                "stream_name": optional("未知直播间"),
                "current_time": optional("未知时间"),
                "platform": optional(""),
                "history": optional("").then(min_len(2)).then(wrap("# 直播历史弹幕\n", "\n")),
                "unreads": optional("").then(min_len(2)).then(wrap("# 新到弹幕\n", "\n")),
                "extra": optional("").then(min_len(2)).then(wrap("# 额外提醒\n", "\n")),
            },
        )

        # vtb 模式 sub-agent（"是否要回复"决策器）prompt —— 区分群聊与直播。
        get_prompt_manager().get_or_create(
            name="anima_chatter_sub_agent_prompt_vtb",
            template=SUB_AGENT_PROMPT_VTB,
            policies={
                "nickname": optional(personality.nickname),
                "bot_id": optional(""),
                "bot_id_section": optional(""),
                "personality_core_section": optional(personality.personality_core)
                .then(wrap("它的核心人格是：", "\n")),
                "personality_side_section": optional(personality.personality_side)
                .then(wrap("它的人格侧面是：", "\n")),
            },
        )

        get_prompt_manager().get_or_create(
            name="anima_chatter_sub_agent_prompt_vtb_live",
            template=SUB_AGENT_PROMPT_LIVE,
            policies={
                "nickname": optional(personality.nickname),
                "bot_id": optional(""),
                "bot_id_section": optional(""),
                "personality_core_section": optional(personality.personality_core)
                .then(wrap("它的核心人格是：", "\n")),
                "personality_side_section": optional(personality.personality_side)
                .then(wrap("它的人格侧面是：", "\n")),
            },
        )

        # ── VTB 资源初始化 ───────────────────────────
        config = self.config if isinstance(self.config, AnimaChatterConfig) else None
        if config is None:
            logger.warning(
                "插件配置加载异常，VTB 模式将无法播放音频或驱动 VTS。"
            )
            return

        # 0 表示关闭响度归一化；否则把目标 dBFS 传给 AudioPlayer。
        loudness_target = float(config.audio_drive.loudness_target_dbfs)
        loudness_arg: float | None = (
            None if loudness_target == 0.0 else loudness_target
        )
        self.audio_player = AudioPlayer(
            output_device=config.vts.audio_output_device,
            loudness_target_dbfs=loudness_arg,
        )
        if loudness_arg is None:
            logger.info("响度归一化已关闭（按原音量播放所有音频）")
        else:
            logger.info(
                f"响度归一化已启用：目标 {loudness_arg:.1f} dBFS"
                "（TTS 说话 / 唱歌 / 其它播放统一拉齐）"
            )

        # 初始化直播清唱歌库（统一移到全局 data/anima_chatter/songs/ 目录）
        plugin_dir = Path(__file__).resolve().parent
        try:
            self.song_library = SongLibrary(plugin_dir=plugin_dir, songs_rel_path="")
            song_count = len(self.song_library.get_song_names())
            if song_count > 0:
                logger.info(
                    f"清唱歌库已加载 {song_count} 首：{self.song_library.songs_dir}"
                )
            else:
                logger.info(
                    f"清唱歌库为空（路径：{self.song_library.songs_dir}），"
                    "把清唱文件放进去后无需重启即可被 sing_song action 识别。"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"清唱歌库初始化失败: {exc}")
            self.song_library = None

        if config.vts.enabled:
            self.vts_performer = VTSPerformer(
                plugin_config=config,
                audio_player=self.audio_player,
            )
            ok = await self.vts_performer.initialize()
            if ok:
                logger.info(
                    "VTSPerformer 已就绪，vtb 模式回复将驱动 VTube Studio 嘴型/动作。"
                )
            else:
                logger.warning(
                    "VTSPerformer 初始化未连接 VTS，vtb 模式将仅播放 TTS 音频。"
                )
        else:
            logger.info("配置中 vts.enabled=false，跳过 VTS 初始化（vtb 模式仅播音频）。")

    async def on_plugin_unloaded(self) -> None:
        """卸载时关闭 VTS 连接。"""

        if self.vts_performer is not None:
            try:
                await self.vts_performer.shutdown()
            except Exception as exc:
                logger.warning(f"关闭 VTSPerformer 失败: {exc}")
        self.vts_performer = None
        self.audio_player = None

    def get_components(self) -> list[type]:
        """返回插件组件。"""

        return [
            AnimaChatter,
            SayAction,
            SayAndPerformAction,
            SingSongAction,
            AnimaPassAndWaitAction,
            StartVoiceCallAction,
            EndVoiceCallAction,
            VTBCommand,
            VoiceCommand,
        ]


__all__ = [
    "EndVoiceCallAction",
    "SayAction",
    "SayAndPerformAction",
    "SingSongAction",
    "AnimaChatter",
    "AnimaChatterPlugin",
    "StartVoiceCallAction",
    "VTBCommand",
    "VoiceCommand",
    "AnimaPassAndWaitAction",
]
