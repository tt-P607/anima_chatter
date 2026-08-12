"""anima_chatter 插件装配与生命周期。

职责边界：本文件**只**负责资源装配（prompt 注册 + 音频 / VTS / 歌库 / TTS 能力
初始化）与组件清单，不含任何对话逻辑——那些在 [`chatter/`](chatter/__init__.py:1)、
[`speech/`](speech/__init__.py:1)、[`voice_call/`](voice_call/__init__.py:1) 三个
子包里。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BasePlugin, register_plugin

from ._internal_compat import (
    get_personality,
    get_prompt_manager,
    prompt_min_len,
    prompt_optional,
    prompt_wrap,
)
from .actions import (
    AnimaPassAndWaitAction,
    EndVoiceCallAction,
    SayAction,
    SayAndPerformAction,
    SingSongAction,
    StartVoiceCallAction,
)
from .audio import AudioPlayer
from .chatter import AnimaChatter
from .chatter.ndfc_handlers import (
    AnimaBuildHistoryTextHandler,
    AnimaCreateRequestHandler,
    AnimaFetchUnreadsHandler,
    AnimaFormatUnreadLineHandler,
    AnimaInjectUnreadPayloadHandler,
    AnimaInjectUsablesHandler,
    AnimaPreprocessHandler,
)
from .commands import VTBCommand, VoiceCommand
from .config import AnimaChatterConfig
from .prompts import (
    MODE_PROMPT_PROFILES,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
)
from .prompts.sub_agent import SUB_AGENT_PROMPT_LIVE, SUB_AGENT_PROMPT_VTB
from .runtime import call_state, pipeline_state, sung_history
from .song_library import SongLibrary
from .vts import VTSPerformer


logger = get_logger("anima_chatter.plugin")


__all__ = ["AnimaChatterPlugin"]


_TTS_REGISTRY_SERVICE = "tts_http_server:service:tts_provider_registry"

# 歌库目录（相对项目根）。放在全局 data 目录下，避免污染插件代码目录。
_SONGS_RELATIVE_PATH = ("data", "anima_chatter", "songs")


@register_plugin
class AnimaChatterPlugin(BasePlugin):
    """装配 Anima 聊天器、语音动作、命令与本地表演资源。"""

    plugin_name = "anima_chatter"
    configs = [AnimaChatterConfig]
    dependent_components = ["asr_adapter_anima:adapter:asr_adapter_anima"]

    # vtb 系模式的运行时资源，由 on_plugin_loaded 按配置初始化。
    audio_player: AudioPlayer | None = None
    vts_performer: VTSPerformer | None = None
    song_library: SongLibrary | None = None
    # TTS Provider 能力元数据缓存。为 None 表示未获取到（TTS 服务未启动 / 老
    # provider 不支持），Action 的 to_schema 会懒加载兜底。
    tts_capabilities: Any | None = None

    # ── 生命周期 ────────────────────────────────────────

    async def on_plugin_loaded(self) -> None:
        """注册提示词并初始化流水线、音频、VTS、歌库与 TTS 能力。"""

        self._register_prompts()

        config = self.config
        if not isinstance(config, AnimaChatterConfig):
            logger.warning("插件配置加载异常，VTB 模式将无法播放音频或驱动 VTS")
            return

        pipeline_state.configure(config.pipelining)
        await self._init_audio_resources(config)

        if config.plugin.enable_singing:
            self._init_song_library()
        else:
            logger.info("唱歌能力已通过 plugin.enable_singing=false 关闭，跳过歌库初始化")

        self._init_tts_capabilities()

    async def on_plugin_unloaded(self) -> None:
        """终止通话并释放流水线、VTS、音频与歌库资源。"""

        active_call = await call_state.get_active_call()
        if active_call is not None:
            from .protocol import require_plugin
            from .voice_call import finalize_call

            await finalize_call(
                stream_id=active_call.caller_stream_id,
                farewell="服务正在停止，本次通话已结束。",
                end_reason="plugin_unload",
                plugin=require_plugin(self),
            )

        await pipeline_state.clear_all()
        await sung_history.clear()

        if self.vts_performer is not None:
            try:
                await self.vts_performer.shutdown()
            except Exception as exc:  # noqa: BLE001 - 卸载路径不能因单点失败中断
                logger.warning(f"关闭 VTSPerformer 失败: {exc}")

        self.vts_performer = None
        self.audio_player = None
        self.song_library = None
        self.tts_capabilities = None

    def get_components(self) -> list[type]:
        """按总开关与唱歌开关返回要注册的组件。

        Returns:
            组件类列表；插件被禁用时返回空列表。
        """

        config = self.config if isinstance(self.config, AnimaChatterConfig) else None
        if config is not None and not config.plugin.enabled:
            return []

        components: list[type] = [
            AnimaChatter,
            SayAction,
            SayAndPerformAction,
            AnimaPassAndWaitAction,
            StartVoiceCallAction,
            EndVoiceCallAction,
            VTBCommand,
            VoiceCommand,
            # NDFC 事件 seam 转发 handler：把 neo_default_chatter:* 事件转发到
            # AnimaChatter 的 adapter 方法（prompt / 注意力 / 工具 / 未读等）。
            AnimaPreprocessHandler,
            AnimaInjectUnreadPayloadHandler,
            AnimaInjectUsablesHandler,
            AnimaCreateRequestHandler,
            AnimaFetchUnreadsHandler,
            AnimaFormatUnreadLineHandler,
            AnimaBuildHistoryTextHandler,
        ]
        if config is None or config.plugin.enable_singing:
            components.append(SingSongAction)
        return components

    def get_active_performer(self) -> VTSPerformer | None:
        """返回当前激活的 VTS 表演器。

        Action 层统一通过本方法获取表演器。

        Returns:
            表演器实例；未启用 VTS 时返回 ``None``。
        """

        return self.vts_performer

    # ── Prompt 注册 ─────────────────────────────────────

    def _register_prompts(self) -> None:
        """注册本插件在 prompt manager 上的全部模板。

        - system prompt：1 个，三模式共用（场景段在运行时按模式注入）。
        - user prompt：3 个（voice / vtb / vtb_live），由模式配置表数据驱动。
        - sub-agent prompt：2 个（vtb / vtb_live），分别用群聊与直播话术。
        """

        personality = get_personality()
        prompt_manager = get_prompt_manager()

        prompt_manager.get_or_create(
            name="anima_chatter_system_prompt",
            template=SYSTEM_PROMPT,
            policies={
                "nickname": prompt_optional(personality.nickname),
                "alias_names": prompt_optional("、".join(personality.alias_names)),
                "personality_core": prompt_optional(personality.personality_core),
                "personality_side": prompt_optional(personality.personality_side),
                "identity": prompt_optional(personality.identity),
                "reply_style": prompt_optional(personality.reply_style),
                "background_story": prompt_optional(personality.background_story)
                .then(prompt_min_len(10))
                .then(prompt_wrap("# 背景故事\n", "\n")),
                "safety_guidelines": prompt_optional(
                    "\n".join(personality.safety_guidelines)
                ),
                "scene_guide": prompt_optional(""),
            },
        )

        for profile in MODE_PROMPT_PROFILES.values():
            prompt_manager.get_or_create(
                name=profile["template_name"],
                template=USER_PROMPT_TEMPLATE,
                policies={
                    "stream_name": prompt_optional(profile["stream_name_default"]),
                    "current_time": prompt_optional("未知时间"),
                    "platform": prompt_optional(""),
                    "history": prompt_optional("")
                    .then(prompt_min_len(2))
                    .then(prompt_wrap(profile["history_wrap_prefix"], "\n")),
                    "unreads": prompt_optional("")
                    .then(prompt_min_len(2))
                    .then(prompt_wrap(profile["unreads_wrap_prefix"], "\n")),
                    "extra": prompt_optional("")
                    .then(prompt_min_len(2))
                    .then(prompt_wrap("# 额外提醒\n", "\n")),
                    "mode_header": prompt_optional(profile["mode_header"]),
                    "section_tail": prompt_optional(profile["section_tail"]),
                },
            )

        sub_agent_policies = {
            "nickname": prompt_optional(personality.nickname),
            "bot_id": prompt_optional(""),
            "bot_id_section": prompt_optional(""),
            "personality_core_section": prompt_optional(
                personality.personality_core
            ).then(prompt_wrap("它的核心人格是：", "\n")),
            "personality_side_section": prompt_optional(
                personality.personality_side
            ).then(prompt_wrap("它的人格侧面是：", "\n")),
        }
        for name, template in (
            ("anima_chatter_sub_agent_prompt_vtb", SUB_AGENT_PROMPT_VTB),
            ("anima_chatter_sub_agent_prompt_vtb_live", SUB_AGENT_PROMPT_LIVE),
        ):
            prompt_manager.get_or_create(
                name=name, template=template, policies=sub_agent_policies
            )

    # ── 资源初始化 ──────────────────────────────────────

    async def _init_audio_resources(self, config: AnimaChatterConfig) -> None:
        """初始化本地音频播放器与 VTube Studio 表演器。

        Args:
            config: 插件配置。
        """

        loudness_target = config.audio_drive.loudness_target_dbfs
        # 0 表示关闭响度归一化。
        loudness_arg = None if loudness_target == 0.0 else loudness_target

        self.audio_player = AudioPlayer(
            output_device=config.vts.audio_output_device,
            loudness_target_dbfs=loudness_arg,
            inst_output_device=config.vts.inst_output_device,
        )
        if loudness_arg is None:
            logger.info("响度归一化已关闭（按原音量播放所有音频）")
        else:
            logger.info(
                f"响度归一化已启用：目标 {loudness_arg:.1f} dBFS"
                "（TTS 说话 / 唱歌 / 其它播放统一拉齐）"
            )

        if not config.vts.enabled:
            logger.info("配置中 vts.enabled=false，跳过 VTS 初始化（vtb 模式仅播音频）")
            return

        self.vts_performer = VTSPerformer(
            plugin_config=config, audio_player=self.audio_player
        )
        if await self.vts_performer.initialize():
            logger.info("VTSPerformer 已就绪，vtb 模式回复将驱动 VTube Studio")
        else:
            logger.warning("VTSPerformer 未连上 VTS，vtb 模式将仅播放 TTS 音频")

    def _init_song_library(self) -> None:
        """初始化直播清唱歌库。"""

        songs_dir = Path(os.getcwd()).resolve().joinpath(*_SONGS_RELATIVE_PATH)
        try:
            self.song_library = SongLibrary(songs_dir)
        except OSError as exc:
            logger.warning(f"清唱歌库初始化失败: {exc}")
            self.song_library = None
            return

        song_count = len(self.song_library.get_song_names())
        if song_count > 0:
            logger.info(f"清唱歌库已加载 {song_count} 首：{self.song_library.songs_dir}")
        else:
            logger.info(
                f"清唱歌库为空（路径：{self.song_library.songs_dir}），"
                "把清唱文件放进去后重启即可被 sing_song 识别"
            )

    def _init_tts_capabilities(self) -> None:
        """通过进程内 service API 获取 TTS Provider 的能力元数据并缓存。

        不走 HTTP 回环查询——本方法在 ``on_plugin_loaded`` 中被调用时 HTTP 服务
        尚未绑定端口，回环查询必然失败。

        时序容忍：调用时 provider 可能尚未注册（TTS 插件还没加载），此时
        ``tts_capabilities`` 保持 ``None``，Action 的 ``to_schema`` 会懒加载兜底。
        """

        registry = get_service(_TTS_REGISTRY_SERVICE)
        if registry is None:
            logger.debug("TTS registry service 未注册，跳过 capabilities 查询")
            return

        get_provider = getattr(registry, "get_provider", None)
        if not callable(get_provider):
            logger.debug("TTS registry 无 get_provider 方法，跳过")
            return

        provider = get_provider()
        if provider is None:
            logger.debug("无 TTS Provider 注册，capabilities 将在 to_schema 时懒加载")
            return

        get_capabilities = getattr(provider, "get_capabilities", None)
        caps = get_capabilities() if callable(get_capabilities) else None
        provider_name = str(getattr(provider, "provider_name", "") or "unknown")

        if caps is None:
            logger.info(
                f"TTS Provider '{provider_name}' 未提供 capabilities，"
                "Action schema 将使用默认参数描述"
            )
            return

        self.tts_capabilities = caps
        logger.info(
            f"已从 TTS Provider '{provider_name}' 获取能力元数据，"
            "Action schema 将动态注入参数说明"
        )
