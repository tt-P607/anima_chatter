"""anima_chatter 插件配置。

支持三种运行模式（详见 :mod:`plugins.anima_chatter.modes`）：

- ``voice``：``platform == "local_asr"``，沿用原有 ASR 实时通话行为。
- ``vtb``：被 ``/vtb on`` 接管的普通群聊 / 私聊（VTube Studio 表演但不直播）。
- ``vtb_live``：直播平台（如 ``bilibili_live``），观众是直播间弹幕。

配置区段（共 7 个）：

============================ ===================================================
section                       适用模式 / 用途
============================ ===================================================
``[plugin]``                  通用 chatter 行为（tick / buffer / 重试 / 挂起开关）
``[tts]``                     TTS HTTP 后端（三种模式共用）
``[vts]``                     VTube Studio 连接 + 本地音频输出 + Hotkey 映射
                              （仅 vtb / vtb_live 生效）
``[vtb_attention]``           vtb / vtb_live 模式的"是否回复"过滤器
``[audio_drive]``             音频驱动律动（vtb / vtb_live 表演时让形象跟着声音动）
``[pipelining]``              vtb_live 流水线优化（让 LLM 推理与音频播放并行）
``[idle_animation]``          待机动画频率与幅度（vtb / vtb_live 共用）
============================ ===================================================
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class AnimaChatterConfig(BaseConfig):
    """anima_chatter 插件配置。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = (
        "anima_chatter 插件配置（语音通话 + VTB 表演 + 直播弹幕，三种模式共用）"
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
            ge=0.05,
            le=60.0,
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
            ge=0,
            le=5,
        )
        enable_action_suspend: bool = Field(
            default=True,
            description=(
                "是否启用纯 Action 回合的挂起机制。关闭后，纯 Action 结果会"
                "像常规工具结果一样继续 follow-up，而不是立即等待用户。"
            ),
        )
        enable_singing: bool = Field(
            default=True,
            description=(
                "是否启用唱歌能力（SingSongAction）。"
                "关闭后插件会**完全卸载**该能力——不注册 sing_song action、"
                "不初始化 song_library、prompt 中也不会出现任何关于唱歌的描述，"
                "模型完全感知不到这个功能存在。"
                "适合不想让 bot 唱歌、或者还没准备好歌库的场景。"
            ),
        )
        custom_prompt: str = Field(
            default="",
            description=(
                "自定义提示词。会以 ``<custom_instructions>`` 块形式追加到指定模式的 "
                "system prompt 末尾，用来声明部署独有的行为（口癖、台风、回复策略等）。"
                "支持多行；可以写 markdown / 标签等任意格式，模型会原样收到。"
                "留空则不注入；具体在哪些模式生效由 ``custom_prompt_modes`` 控制。"
            ),
        )
        custom_prompt_modes: list[str] = Field(
            default_factory=lambda: ["voice", "vtb", "vtb_live"],
            description=(
                "``custom_prompt`` 生效的模式列表。可选值：``voice`` / ``vtb`` / ``vtb_live``。"
                "默认三种模式都注入；想只在某些模式下生效就改成对应子集，"
                "比如只想直播时生效就写 ``[\"vtb_live\"]``。"
                "空列表 ``[]`` 等于完全禁用 ``custom_prompt``（即便其内容非空）。"
            ),
        )
        model_task: str = Field(
            default="actor",
            description="LLM 模型名称（对应 model.toml 中的 task），models 为空时使用",
        )
        models: list[str] = Field(
            default_factory=list,
            description="指定 LLM 模型列表（对应 model.toml 中的 name）。非空时覆盖 model_task，多个模型按顺序 fallback",
        )
        temperature: float = Field(
            default=0.7,
            description="模型温度，仅在 models 非空时生效",
            ge=0.0,
            le=2.0,
        )
        max_tokens: int = Field(
            default=8000,
            description="最大输出 token 数，仅在 models 非空时生效",
            ge=1,
            le=200000,
        )

    @config_section("tts", title="TTS 设置")
    class TTSSection(SectionBase):
        """TTS HTTP 后端配置（三模式共享）。"""

        endpoint: str = Field(
            default="http://127.0.0.1:8000/router/tts_http_server/api/tts/v1/synthesize",
            description="TTS HTTP 合成接口地址",
        )
        timeout: float = Field(
            default=30.0,
            description="TTS HTTP 请求超时时间（秒）",
            ge=1.0,
            le=300.0,
        )
        max_parallel_segments: int = Field(
            default=4,
            description="最大并行合成句子数",
            ge=1,
            le=32,
        )
        empty_audio_retry_count: int = Field(
            default=1,
            description="TTS 返回空音频时的重试次数",
            ge=0,
            le=5,
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
                "data/anima_chatter/vts_token.txt（之后免重复授权）。"
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
        inst_output_device: str = Field(
            default="",
            description=(
                "双轨翻唱时**伴奏**的专用输出设备，格式 '设备名@驱动名' 或纯设备名。"
                "留空则伴奏走系统默认输出。\n"
                "用途：人声走上面的 VB-Cable 驱动口型，伴奏单独送到这个设备——"
                "把它指到一个专门给直播软件采集的设备（比如另一个虚拟声卡，或某个"
                "显示器/外置扬声器），就能让伴奏单独进直播流而不经过 VB-Cable，"
                "避免人声重复采集。\n"
                "仅 vtb / vtb_live 模式的双轨歌伴奏路使用；单轨歌 / TTS 不受影响。"
            ),
        )

        # ── Hotkey 映射（原 [motion].hotkey_map） ───
        # 长详细说明、匹配规则、与 expression_map 的区别已搬到
        # docs/configuration.md 的 ``[vts.hotkey_map]`` 段；这里只保留一句口诀。
        hotkey_map: dict[str, str] = Field(
            default_factory=dict,
            description=(
                "可选：把 intent / emotion 主类型映射到 VTS Hotkey ID。"
                "详见 docs/configuration.md。留空则不触发热键。"
            ),
        )

        # ── 表情文件直激活（不走 hotkey 系统） ──────
        # 长详细说明、{file, desc} 格式、prompt 注入策略已搬到
        # docs/configuration.md 的 ``[vts.expression_map]`` 段。
        expression_map: dict[str, dict[str, str]] = Field(
            default_factory=dict,
            description=(
                "可选：把 intent / emotion 主类型映射到 Live2D .exp3.json 表情文件。"
                "格式 {key: {file, desc}}；详见 docs/configuration.md。"
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
        organic_enabled: bool = Field(
            default=True,
            description="说话时头部微动用 value noise 替代 sin，去机械周期感；关闭回退旧 sin",
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
        loudness_target_dbfs: float = Field(
            default=-14.0,
            description=(
                "全局响度目标（dBFS）。AudioPlayer 在播放任何音频（TTS / 唱歌 / "
                "其它）前，会按 RMS 把响度统一拉到这个值——不同 TTS 生成结果"
                "和翻唱歌曲音量再不齐也会被拉齐，直播间观众听感一致。"
                "默认 -14：比直播常用的 -20/-16 更响，确保送进 VB-Cable 的电平"
                "足够高、VTS 麦克风口型能把嘴张开。觉得太吵可回调到 -16 ~ -20。"
                "设为 0 关闭归一化，按原音量播放。"
            ),
        )

    @config_section("pipelining", title="vtb_live 流水线优化")
    class PipeliningSection(SectionBase):
        """**仅 ``vtb_live`` 模式**生效的"动作流水线"优化。

        痛点：直播 TTS 经常一段 30 秒以上、唱歌 1~3 分钟，原阻塞模式下
        Bot 在播放期间完全不能响应新弹幕，弹幕会堆到队尾才被处理。

        优化思路：让 Action 派发完后台播放任务后**立即返回**，并在 LLM
        即将发起新一轮调用前（``sub_agent`` / ``_build_user_prompt`` 入口）
        阻塞到累积播放进度达到 ``trigger_percent`` 才放行。这样：

        - **物理音频**永远按队列串行播放（底层 audio_player 锁 + 时段 reserve
          双重保证），不会重叠；
        - **LLM 推理**与音频播放重叠，下一轮回复在上一轮播完前就准备好；
        - **弹幕聚合**：流水线门期间积累的弹幕，被下一轮 LLM 综合处理而非
          逐条响应，节奏更接近主播本人。

        其他模式（``voice`` / ``vtb``）始终按原阻塞模式工作，不受本配置影响。
        """

        enabled: bool = Field(
            default=True,
            description=(
                "vtb_live 流水线总开关。关闭后所有 Action 走原阻塞模式"
                "（Action 阻塞到播放结束才返回），适合调试或不希望弹幕聚合的场景。"
            ),
        )
        trigger_percent: float = Field(
            default=0.6,
            ge=0.0,
            le=1.0,
            description=(
                "本轮累积音频时长达到此比例时触发 LLM 流水线门放行（0~1）。"
                "默认 0.6 表示总播放时长 60% 时让 LLM 醒来准备新一轮。"
                "调小（如 0.4）→ LLM 更激进，转场更紧凑但可能偶尔抢话；"
                "调大（如 0.8）→ LLM 更保守，节奏稳但流水线收益变小。"
            ),
        )
        silence_gap_seconds: float = Field(
            default=7.0,
            ge=0.0,
            le=120.0,
            description=(
                "**跨轮**音频之间的强制静默间隔（秒）。上一轮所有音频播完后等"
                "这么久才放下一轮第一段，避免接得太急显得机械。"
                "**不影响轮内**：同一次 LLM 响应里多个 Action 紧接排队不加间隔。"
                "**实际生效值会按 ``silence_gap_jitter`` 加随机波动**。"
            ),
        )
        silence_gap_jitter: float = Field(
            default=2.0,
            ge=0.0,
            le=120.0,
            description=(
                "跨轮静默间隔的随机抖动幅度（秒）。每次跨轮 reserve 时实际间隔 = "
                "``silence_gap_seconds + uniform(-jitter, +jitter)``。"
                "默认 2.0 表示在 7±2 秒之间随机；设为 0 关闭抖动让间隔严格固定。"
                "调大让节奏更不规律（更像真人主播），调小让节奏更稳定。"
            ),
        )
        min_remaining_seconds: float = Field(
            default=25.0,
            ge=0.0,
            le=600.0,
            description=(
                "**距结束最少剩余秒数**——本轮播放结束前至少留这么多秒给 LLM 推理。\n"
                "实际门时刻 = ``max(trigger_percent_gate, finish_at - min_remaining_seconds)``\n"
                "—— trigger_percent 算出的时刻和'结束前 N 秒'取**较晚者**，"
                "尽可能多吞吐弹幕：\n"
                "- 短回复（30s, trigger=60%）：18s 触发（按比例）\n"
                "- 长歌曲（180s, trigger=60%）：155s 触发（结束前 25s 唤醒，"
                "前 155s 全都用来聚合弹幕）\n"
                "默认 25 秒；设 0 或负数关闭，完全按 trigger_percent 等待。"
            ),
        )
        min_duration_seconds: float = Field(
            default=10.0,
            ge=0.0,
            le=600.0,
            description=(
                "最低门槛（秒）：本轮累积音频时长低于此值时**不启用**流水线，"
                "Action 走原阻塞模式（直接等播完才返回）。"
                "防止短句也参与流水线导致没必要的复杂状态切换。"
                "建议保持 10s 左右；调到 0 等于『任何时长都启用流水线』。"
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

        # 有机微动：待机头身微动用 value noise 替代 sin 叠加，去机械周期感
        organic_enabled: bool = Field(
            default=True,
            description="待机头身微动用 value noise 替代 sin；关闭回退旧 sin 行为",
        )

        # 呼吸频率（Hz）+ 振幅。0.28Hz ≈ 17 次/分，正常人呼吸节奏
        breath_freq: float = Field(default=0.28, description="呼吸频率 Hz")
        breath_amplitude: float = Field(default=0.9, description="呼吸 head_z 振幅（度）")
        breath_body_amplitude: float = Field(
            default=1.2,
            description="呼吸带动身体上下起伏振幅（v_body_y 度数），0 关闭",
        )

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
    pipelining: PipeliningSection = Field(default_factory=PipeliningSection)
    idle_animation: IdleAnimationSection = Field(default_factory=IdleAnimationSection)


__all__ = ["AnimaChatterConfig"]
