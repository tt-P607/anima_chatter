"""voice_chatter 插件入口。

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

from .actions import SayAction, SayAndPerformAction, VoicePassAndWaitAction
from .audio import AudioPlayer
from .commands import VTBCommand
from .config import SherpaOnnxVoiceChatterConfig
from .modes import ChatterMode
from .prompts import (
    SYSTEM_PROMPT,
    USER_PROMPT_VOICE,
    USER_PROMPT_VTB,
    USER_PROMPT_VTB_LIVE,
    VoiceChatterPromptBuilder,
)
from .runner import run_voice_conversation
from .sub_agent import VOICE_CHATTER_SUB_AGENT_PROMPT_TEMPLATE
from .vts import VTSPerformer


logger = get_logger("voice_chatter")

_PASS_AND_WAIT = "action-pass_and_wait"

# 接管 / 释放命令使用的 chatter 签名常量。
_CHATTER_SIGNATURE = "voice_chatter:chatter:voice_chatter"


class SherpaOnnxVoiceChatter(BaseChatter):
    """voice_chatter：语音通话 / VTube Studio 虚拟形象通用 Chatter。"""

    chatter_name = "voice_chatter"
    chatter_description = (
        "语音通话与 VTube Studio 虚拟形象互动通用 Chatter。"
        "platform=local_asr 时为实时通话模式；其他平台需通过 /vtb on 显式接管。"
    )
    associated_platforms = ["local_asr"]
    chat_type = ChatType.ALL
    dependencies = ["asr_adapter:adapter:asr_adapter"]

    # 默认值；apply_stream_runtime_options 会按 platform 动态覆写。
    stream_tick_interval = 0.1
    allow_message_buffer = False

    def _get_plugin_config(self) -> SherpaOnnxVoiceChatterConfig | None:
        """返回插件配置。"""

        config = getattr(self.plugin, "config", None)
        return config if isinstance(config, SherpaOnnxVoiceChatterConfig) else None

    def _resolve_mode(self, chat_stream: ChatStream | None = None) -> ChatterMode:
        """根据当前流的 platform 决定运行模式。

        与 :meth:`VoiceChatterPromptBuilder.resolve_mode` 保持一致：

        - ``local_asr`` → ``voice``
        - 直播平台（如 ``bilibili_live``） → ``vtb_live``
        - 其他 → ``vtb``
        """

        if chat_stream is None:
            return "vtb"
        return VoiceChatterPromptBuilder.resolve_mode(chat_stream)

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
        """根据 platform 自动选择 voice / vtb 场景的系统提示词。"""

        return await VoiceChatterPromptBuilder.build_system_prompt(
            self._get_plugin_config(),
            chat_stream,
            mode=self._resolve_mode(chat_stream),
        )

    def _build_history_text(self, chat_stream: ChatStream) -> str:
        """构建历史消息文本。"""

        return VoiceChatterPromptBuilder.build_history_text(chat_stream, self.format_message_line)

    async def _build_user_prompt(
        self,
        chat_stream: ChatStream,
        history_text: str,
        unread_lines: str,
        extra: str = "",
    ) -> str:
        """构建用户提示词（按模式选择不同模板）。"""

        return await VoiceChatterPromptBuilder.build_user_prompt(
            chat_stream,
            history_text,
            unread_lines,
            extra,
            mode=self._resolve_mode(chat_stream),
        )

    @staticmethod
    def _build_negative_behaviors_extra() -> str:
        """构建行为提醒。"""

        return VoiceChatterPromptBuilder.build_negative_behaviors_extra()

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
class SherpaOnnxVoiceChatterPlugin(BasePlugin):
    """voice_chatter 插件：通话 + VTB 虚拟形象通用 chatter。"""

    plugin_name = "voice_chatter"
    plugin_version = "1.1.0"
    plugin_description = (
        "voice_chatter：sherpa-onnx ASR 实时语音通话 + VTube Studio 虚拟形象互动 通用 Chatter"
    )
    configs = [SherpaOnnxVoiceChatterConfig]
    dependent_components = ["asr_adapter:adapter:asr_adapter"]

    # vtb 模式运行时资源；on_plugin_loaded 中按配置初始化。
    audio_player: AudioPlayer | None = None
    vts_performer: VTSPerformer | None = None

    async def on_plugin_loaded(self) -> None:
        """注册提示词模板，并按配置初始化 VTB 资源（AudioPlayer + VTS）。"""

        from src.core.prompt import min_len, optional, wrap

        personality = get_core_config().personality
        get_prompt_manager().get_or_create(
            name="voice_chatter_system_prompt",
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
            name="voice_chatter_user_prompt",
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
            name="voice_chatter_vtb_user_prompt",
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
            name="voice_chatter_vtb_live_user_prompt",
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

        # vtb 模式 sub-agent（"是否要回复"决策器）prompt。
        get_prompt_manager().get_or_create(
            name="voice_chatter_sub_agent_prompt",
            template=VOICE_CHATTER_SUB_AGENT_PROMPT_TEMPLATE,
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
        config = self.config if isinstance(self.config, SherpaOnnxVoiceChatterConfig) else None
        if config is None:
            logger.warning(
                "插件配置加载异常，VTB 模式将无法播放音频或驱动 VTS。"
            )
            return

        self.audio_player = AudioPlayer(output_device=config.vts.audio_output_device)

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
            SherpaOnnxVoiceChatter,
            SayAction,
            SayAndPerformAction,
            VoicePassAndWaitAction,
            VTBCommand,
        ]


__all__ = [
    "SayAction",
    "SayAndPerformAction",
    "SherpaOnnxVoiceChatter",
    "SherpaOnnxVoiceChatterPlugin",
    "VTBCommand",
    "VoicePassAndWaitAction",
]
