"""voice_chatter 插件配置。

支持三种运行模式（详见 :mod:`plugins.voice_chatter.modes`）：

- ``voice``：``platform == "local_asr"``，沿用原有 ASR 实时通话行为。
- ``vtb``：被 ``/vtb on`` 接管的普通群聊 / 私聊（VTube Studio 表演但不直播）。
- ``vtb_live``：直播平台（如 ``bilibili_live``），观众是直播间弹幕。

配置区段（共 6 个）：

============================ ===================================================
section                       适用模式 / 用途
============================ ===================================================
``[plugin]``                  通用 chatter 行为（tick / buffer / 重试 / 挂起开关）
``[tts]``                     TTS HTTP 后端（三种模式共用）
``[vts]``                     VTube Studio 连接 + 本地音频输出 + Hotkey 映射
                              （仅 vtb / vtb_live 生效）
``[vtb_attention]``           vtb / vtb_live 模式的"是否回复"过滤器
``[audio_drive]``             音频驱动律动（vtb / vtb_live 表演时让形象跟着声音动）
``[idle_animation]``          待机动画频率与幅度（vtb / vtb_live 共用）
============================ ===================================================
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class SherpaOnnxVoiceChatterConfig(BaseConfig):
    """voice_chatter 插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = (
        "voice_chatter 插件配置（语音通话 + VTB 表演 + 直播弹幕，三种模式共用）"
    )

    @config_section("plugin", title="插件设置", tag="plugin")
    class PluginSection(SectionBase):  # noqa: D401
        """插件基础配置（三模式共享）。"""

        enabled: bool = Field(default=True, description="是否启用本 chatter")
        tick_interval: float = Field(
            default=1.0,
            description=(
                "vtb / vtb_live 模式下的 tick 间隔（秒）。"
                "voice 模式始终强制为 0.1，无法被此项影响。"
            ),
        )
        allow_message_buffer: bool = Field(
            default=True,
            description=(
                "vtb / vtb_live 模式下是否允许消息缓冲。"
                "voice 模式始终强制为 False。"
            ),
        )
        plain_text_retry_limit: int = Field(
            default=1,
            description="模型返回纯文本（未调用 say / say_and_perform）时的提醒重试次数",
        )
        enable_action_suspend: bool = Field(
            default=True,
            description=(
                "是否启用纯 Action 回合的挂起机制。关闭后，纯 Action 结果会"
                "像常规工具结果一样继续 follow-up，而不是立即等待用户。"
            ),
        )

    @config_section("tts", title="TTS 设置")
    class TTSSection(SectionBase):
        """TTS HTTP 后端配置（三模式共享）。"""

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

    @config_section("vts", title="VTube Studio 接入")
    class VTSSection(SectionBase):
        """VTube Studio 连接 + 音频输出 + Hotkey 映射（仅 vtb / vtb_live 生效）。

        在 vtb 系模式下集中表达"虚拟形象那一边的所有接入参数"——以前散在
        ``[vts]`` / ``[audio]`` / ``[motion]`` 三个 section，全部合并到这里。
        """

        # ── 长连接 ─────────────────────────────────
        enabled: bool = Field(
            default=False,
            description="是否启用 VTS（关闭后 vtb 系模式仅播 TTS，不驱动虚拟形象）",
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

        # ── 音频输出（原 [audio].output_device） ────
        audio_output_device: str = Field(
            default="CABLE Input@WASAPI",
            description=(
                "vtb / vtb_live 模式下用于本地播放 TTS 的输出设备，"
                "格式为 '设备名@驱动名'。通常指向 VB-Cable Input，"
                "使虚拟形象与直播软件能听到同一份音频。"
            ),
        )

        # ── Hotkey 映射（原 [motion].hotkey_map） ───
        hotkey_map: dict[str, str] = Field(
            default_factory=dict,
            description=(
                "可选：把 emotion / intent 映射到 VTS 已配置的 Hotkey ID。"
                "在 VTube Studio 的 Hotkeys 面板里给每个动画起一个 Hotkey ID"
                "（不是显示名），然后在这里映射，例如 "
                '{"THINKING": "ThinkAnim", "happy": "SmileExpr"}。'
                "默认为空，所有表演由 emotion + intent 参数注入完成。"
                "匹配规则：先按 intent（``THINKING / EXCITED / SURPRISED ...``）查，"
                "没命中再按 emotion 主类型（``happy / sad / angry / surprised``）查。"
            ),
        )

    @config_section("vtb_attention", title="VTB 注意力过滤")
    class VTBAttentionSection(SectionBase):
        """vtb / vtb_live 模式下"是否回复"过滤器（原 ``[sub_agent]``）。

        与 dfc 行为一致——权重数值（基础概率 / 各类加成）保持与 dfc 同款硬编码，
        避免插件之间行为漂移。这里只暴露和 dfc 平行的两个总控开关。
        """

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用 VTB 注意力过滤。关闭后每条未读消息都会直接触发 LLM 回复，"
                "适合一对一私聊或低流量群聊；多人群聊 / 直播间建议保持启用。"
            ),
        )
        enable_programmatic_controller: bool = Field(
            default=True,
            description=(
                "是否启用 sub-agent 程序化控制器（与 dfc 同名设置一致）。"
                "开启后会先按本地概率规则判断是否直接响应；关闭后始终交由 sub_actor LLM 决策。"
            ),
        )

    @config_section("audio_drive", title="音频驱动律动")
    class AudioDriveSection(SectionBase):
        """vtb / vtb_live 模式下"音频驱动头部 / 身体律动"配置。

        实时计算 TTS 音频包络（RMS + 变化率），按下面的增益叠加到 SpeechAnimator
        的输出参数上。原理：声音大时头部微抬、激动；声音突变时身体一震；让程序
        化动画看起来像跟着语调起伏。

        所有增益都是经验值，第一次跑出来八成会"太激进"或"太迟钝"，根据自己模型
        看着调即可。
        """

        enabled: bool = Field(
            default=True,
            description="是否启用音频驱动律动；关闭后退回固定 sin 波动逻辑",
        )
        head_y_gain: float = Field(
            default=8.0,
            description="头部前后倾灵敏度（rms × gain → v_head_y 度数）",
        )
        head_x_gain: float = Field(
            default=3.0,
            description="头部横向摆动幅度（rms × gain × sin → v_head_x 度数）",
        )
        body_y_gain: float = Field(
            default=30.0,
            description="身体律动灵敏度（velocity × gain → v_body_y 度数）",
        )
        neutral_attenuation: float = Field(
            default=0.5,
            description=(
                "emotion=neutral 时整体增益乘数。0.5 表示平静叙述时律动减半，"
                "避免显得乱抖；调到 0.0 等于平静时完全不动。"
            ),
        )

    @config_section("idle_animation", title="待机动画频率 / 幅度")
    class IdleAnimationSection(SectionBase):
        """vtb / vtb_live 模式下"待机自动化"动画的频率与幅度。

        AutoAnimator 负责眨眼 / 呼吸 / 眼神扫视 / 被动摆动 / 宏观大动作。
        默认值已经比原版激进——让 VTB 待机时看起来"活"一些。所有数值都
        可以按你的模型调整：动得太狂就调小，呆就调大。

        不调时这些字段全部走默认值；调一两个旋钮就能整体调风格，不需要
        改 auto.py 源码。
        """

        # 眨眼间隔（秒）。真人 2-4 秒一次，1.8-4.0 让 VTB 更显灵动
        blink_min_interval: float = Field(default=1.8, description="眨眼最小间隔（秒）")
        blink_max_interval: float = Field(default=4.0, description="眨眼最大间隔（秒）")

        # 呼吸频率（Hz）+ 振幅。0.28Hz ≈ 17 次/分，正常人呼吸节奏
        breath_freq: float = Field(default=0.28, description="呼吸频率 Hz")
        breath_amplitude: float = Field(default=0.9, description="呼吸 head_z 振幅（度）")

        # 眼神扫视：真人微眼动 0.2-0.6 秒/次，0.5-1.8 让眼神持续微动不显呆
        saccade_min_interval: float = Field(default=0.5, description="扫视最小间隔（秒）")
        saccade_max_interval: float = Field(default=1.8, description="扫视最大间隔（秒）")
        saccade_big_probability: float = Field(
            default=0.35,
            description="大幅扫视概率（其余为小幅微动）；0~1",
        )
        saccade_small_amplitude_x: float = Field(
            default=0.22, description="小扫视水平幅度（0~1）"
        )
        saccade_small_amplitude_y: float = Field(
            default=0.15, description="小扫视垂直幅度（0~1）"
        )
        saccade_big_amplitude_x: float = Field(
            default=0.7, description="大扫视水平幅度（0~1）"
        )
        saccade_big_amplitude_y: float = Field(
            default=0.4, description="大扫视垂直幅度（0~1）"
        )

        # 头部 / 身体微动总幅度倍率。1.0 是原版，1.5 比原版动得明显
        head_micro_scale: float = Field(
            default=1.5, description="头部微动幅度倍率，1.0 为原版基准"
        )

        # 被动慢摆触发频率（秒）。原版 40-80 秒太罕见
        passive_sway_min_interval: float = Field(
            default=8.0, description="被动慢摆最小间隔（秒）"
        )
        passive_sway_max_interval: float = Field(
            default=25.0, description="被动慢摆最大间隔（秒）"
        )

        # 宏观动作（重心斜 / 好奇歪头 / 害羞回避等）触发频率（秒）。
        # 原版 20-45 秒触发一次太罕见；改成 6-15 秒，对话期间能多看几个不同动作
        macro_min_interval: float = Field(
            default=6.0, description="宏观动作最小间隔（秒）"
        )
        macro_max_interval: float = Field(
            default=15.0, description="宏观动作最大间隔（秒）"
        )

        # 宏观动作执行速度倍率。原版 move 2 秒 / hold 几秒 看着像慢动作；
        # 默认 2.0 让 move 压缩到 ~1 秒（接近真人头部转向速度）。
        # 调高 = 动作更快更利落；调低 = 慢镜头风。
        motion_speed_scale: float = Field(
            default=2.0, description="宏观动作执行速度倍率（>1 加快，<1 放慢）"
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    tts: TTSSection = Field(default_factory=TTSSection)
    vts: VTSSection = Field(default_factory=VTSSection)
    vtb_attention: VTBAttentionSection = Field(default_factory=VTBAttentionSection)
    audio_drive: AudioDriveSection = Field(default_factory=AudioDriveSection)
    idle_animation: IdleAnimationSection = Field(default_factory=IdleAnimationSection)


__all__ = ["SherpaOnnxVoiceChatterConfig"]
