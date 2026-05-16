"""voice_chatter 插件配置。

支持两种运行模式：

- voice 模式：``platform == "local_asr"``，沿用原有的 ASR 实时通话行为。
- vtb 模式：被 ``/vtb on`` 接管的其他平台流（如 QQ 群/私聊），通过 TTS+VTS
  驱动 VTube Studio 虚拟形象。

配置区段：

- ``[plugin]``：通用 chatter 行为（tick / buffer / 重试 / 挂起开关）。
- ``[tts]``：TTS HTTP 后端（两种模式共用）。
- ``[vts]``：VTube Studio 连接（仅 vtb 模式）。
- ``[audio]``：本地音频输出设备（仅 vtb 模式）。
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class SherpaOnnxVoiceChatterConfig(BaseConfig):
    """voice_chatter 插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "voice_chatter 插件配置（语音通话 + VTB 虚拟形象）"

    @config_section("plugin", title="插件设置", tag="plugin")
    class PluginSection(SectionBase):  # noqa: D401
        """插件基础配置。"""

        enabled: bool = Field(default=True, description="是否启用本 chatter")
        tick_interval: float = Field(
            default=1.0,
            description="非 ASR（即 vtb）模式下的 tick 间隔；voice 模式始终强制为 0.1",
        )
        allow_message_buffer: bool = Field(
            default=True,
            description="非 ASR 模式下是否允许消息缓冲；voice 模式始终强制为 False",
        )
        plain_text_retry_limit: int = Field(
            default=1,
            description="模型返回纯文本时的提醒重试次数",
        )
        enable_action_suspend: bool = Field(
            default=True,
            description=(
                "是否启用纯 Action 回合的挂起机制。关闭后，纯 Action 结果会像常规工具结果一样"
                "继续 follow-up，而不是立即等待用户。"
            ),
        )

    @config_section("tts", title="TTS 设置")
    class TTSSection(SectionBase):
        """TTS 后端配置。"""

        endpoint: str = Field(
            default="http://127.0.0.1:8000/router/tts_http_server/api/tts/v1/synthesize",
            description="TTS HTTP 合成接口地址",
        )
        timeout: float = Field(default=30.0, description="TTS HTTP 请求超时时间（秒）")
        max_parallel_segments: int = Field(default=4, description="最大并行合成句子数")
        empty_audio_retry_count: int = Field(
            default=1, description="TTS 返回空音频时的重试次数"
        )
        sentence_split_enabled: bool = Field(
            default=True, description="是否按句切分并并行合成"
        )
        mime_type: str = Field(default="audio/wav", description="TTS 音频 MIME 类型")
        provider: str = Field(
            default="qwen_tts",
            description="TTS provider 名称，留空则使用服务端默认 provider",
        )
        emit_text_on_tts_failure: bool = Field(
            default=False, description="TTS 失败时是否回退发送文本"
        )

    @config_section("vts", title="VTube Studio 配置")
    class VTSSection(SectionBase):
        """VTube Studio 连接与运行配置（仅 vtb 模式生效）。"""

        enabled: bool = Field(
            default=False,
            description="是否启用 VTS（关闭后 vtb 模式仅播 TTS，不驱动虚拟形象）",
        )
        host: str = Field(default="127.0.0.1", description="VTS 主机地址")
        port: int = Field(default=8001, description="VTS WebSocket 端口")
        auth_token: str = Field(
            default="",
            description=(
                "VTS 鉴权 token；首次留空，VTS 会弹授权窗，认证后由 pyvts 自动写入 "
                "data/voice_chatter/vts_token.txt（之后免重复授权）。"
            ),
        )

    @config_section("audio", title="音频输出（VTB 模式）")
    class AudioSection(SectionBase):
        """vtb 模式下本地音频输出设备配置。"""

        output_device: str = Field(
            default="CABLE Input@WASAPI",
            description=(
                "用于 vtb 模式 TTS 播放的输出设备，格式为 '设备名@驱动名'。"
                "通常指向 VB-Cable Input，使虚拟形象与直播软件能听到同一份音频。"
            ),
        )

    @config_section("sub_agent", title="VTB 注意力过滤")
    class SubAgentSection(SectionBase):
        """vtb 模式下"是否回复"过滤器，与 dfc 行为一致。

        权重数值（基础概率 / 各类加成）保持与 dfc 同款硬编码，避免插件之间
        行为漂移。这里只暴露和 dfc 平行的两个总控开关。
        """

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用 VTB 注意力过滤。关闭后每条未读消息都会直接触发 LLM 回复，"
                "适合一对一私聊或低流量群聊；多人群聊建议保持启用。"
            ),
        )
        enable_programmatic_controller: bool = Field(
            default=True,
            description=(
                "是否启用 sub-agent 程序化控制器（与 dfc 同名设置一致）。"
                "开启后会先按本地概率规则判断是否直接响应；关闭后始终交由 sub_actor LLM 决策。"
            ),
        )

    @config_section("motion", title="VTube Studio 动作映射（可选热键）")
    class MotionSection(SectionBase):
        """可选：把 emotion / intent 映射到 VTS 已配置的 Hotkey ID。

        默认情况下 ``say_and_perform`` 通过 emotion + intent 两个参数完成
        所有表演（嘴型基准、头部姿态、眼神方向、身体晃动），**不需要**任何
        VTS 热键。如果想让某些 emotion / intent 额外触发"点头/挥手/特定表情"
        这种 VTS 已经做好的预设动画，就在这里映射。

        匹配规则：先按 intent 名（``THINKING / EXCITED / SURPRISED ...``）查，
        没命中再按 emotion 主类型（``happy / sad / angry / surprised``）查。
        留空（默认）则完全不触发热键。
        """

        hotkey_map: dict[str, str] = Field(
            default_factory=dict,
            description=(
                "intent / emotion -> VTS Hotkey ID 映射。在 VTube Studio 的"
                "Hotkeys 面板里给每个动画起一个 Hotkey ID（不是显示名），"
                "然后在这里映射；例如 "
                "{\"THINKING\": \"ThinkAnim\", \"happy\": \"SmileExpr\"}。"
                "默认为空，所有表演由 emotion + intent 参数注入完成。"
            ),
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    tts: TTSSection = Field(default_factory=TTSSection)
    vts: VTSSection = Field(default_factory=VTSSection)
    audio: AudioSection = Field(default_factory=AudioSection)
    sub_agent: SubAgentSection = Field(default_factory=SubAgentSection)
    motion: MotionSection = Field(default_factory=MotionSection)


__all__ = ["SherpaOnnxVoiceChatterConfig"]
