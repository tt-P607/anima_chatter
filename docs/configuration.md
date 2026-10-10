# 配置参考

`anima_chatter` 用 6 个 section 表达直播配置。所有字段均有默认值，第一次跑
只需要按 [README 快速上手](../README.md) 那段填几项关键字段，剩下的等需要时再调。

配置文件路径：``config/plugins/anima_chatter/config.toml``

## 整体结构

| Section | 适用模式 | 用途 |
|---------|------|------|
| [`[plugin]`](#plugin) | 直播 | Chatter、模型与提示词 |
| [`[vts]`](#vts) | 直播 | VTube Studio 长连 + 本地音频 + Hotkey 映射 |
| [`[vtb_attention]`](#vtb_attention) | 直播 | 注意力门 |
| [`[audio_drive]`](#audio_drive) | 直播 | 音频驱动头部 / 身体律动 |
| [`[pipelining]`](#pipelining) | 直播 | 排播门、进度与背压 |
| [`[idle_animation]`](#idle_animation) | 直播 | 待机动画频率 / 幅度 |

类型定义都在 [`config.py`](../config.py)，下面按 section 列字段。

---

## [plugin]

直播 Chatter、模型和提示词配置。

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `enabled` | bool | `true` | 是否启用本 chatter |
| `tick_interval` | float | `1.0` | 直播 tick 间隔（秒） |
| `allow_message_buffer` | bool | `true` | 是否允许消息缓冲 |
| `plain_text_retry_limit` | int | `1` | 未调用 say_and_perform 时的提醒重试次数 |
| `enable_action_suspend` | bool | `true` | 启用纯 Action 回合的挂起；关闭则纯 Action 结果继续 follow-up |
| `enable_singing` | bool | `true` | 是否注册唱歌动作并初始化歌库 |
| `custom_prompt` | str | `""` | 追加到直播 system prompt 的部署指令 |
| `custom_prompt_enabled` | bool | `true` | 是否注入部署指令 |
| `model_task` | str | `actor` | 模型任务名 |
| `models` | list[str] | `[]` | 非空时指定模型列表 |
| `temperature` | float | `0.7` | 指定模型时的温度 |
| `max_tokens` | int | `8000` | 指定模型时的输出上限 |

旧 `custom_prompt_modes` 在顶层加载入口转换为 `custom_prompt_enabled`，依据是否包含 `vtb_live`。自动写回前保留 `.anima_voice.bak` 原始备份，供通话配置迁移使用；不覆盖已有且不同内容的备份。

---

TTS 参数与动态 Action schema 由 `tts_voice_plugin-neo:service:speech` 提供。直播只调用该共享服务的 PCM 流接口，不配置 endpoint、provider、重试或 HTTP 超时；这些字段不属于本插件配置。

GPT-SoVITS V5 的流式参数在 TTS 提供方配置中设置，不在直播插件复制音色、参考音频或推理配置：

```toml
[tts_streaming]
enabled = true
streaming_mode = 2
streaming_chunk_seconds = 2.0
chunk_size = 4096
sample_steps = 32
cfg_rate = 0.0
```

PCM 固定请求 `media_type = "raw"` 和 `batch_size = 1`，要求 48000 Hz 单声道 s16le。步数和 CFG 始终按流式配置中的数值发送，不继承普通合成的这两项参数，也不随模型自动选择。步数必须为正整数，CFG 必须为有限非负数，`0` 关闭 CFG。2 秒窗口不表示零首音等待：V5 仍先进行文本片段的语义准备。配置级效果器必须关闭，直播动作不暴露 `effects`。流式 TTS 保留原始电平，不使用整段 RMS 归一化；歌曲仍使用整轨响度处理。

直播将一次 `say_and_perform` 的 `content` 列表按换行合并，inline 表演标记只剥离，顶层 emotion/intent 共享整条回复；motion/emotion 标记与列表项不会拆分 TTS 或触发表演切换。仅 wait 标记拆分请求。Provider 内部 `text_split_method` 与 `fragment_interval` 保持原配置和行为，不代表直播分句；声卡设置及 V5 的 32 步、0 CFG 参数不因该消费路径改变。PCM 接收没有块数或时长上限，当前回复可全量缓存。

---

## [vts]

VTube Studio 长连、本地直播音频输出与 Hotkey 映射。

```toml
[vts]
enabled = true
host = "127.0.0.1"
port = 8001
auth_token = ""
audio_output_device = "CABLE Input@WASAPI"
hotkey_map = {}
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `enabled` | bool | `false` | 是否启用 VTS；关闭后只播放 TTS |
| `host` | str | `127.0.0.1` | VTS 主机地址 |
| `port` | int | `8001` | VTS WebSocket 端口（VTube Studio 默认） |
| `auth_token` | str | `""` | 鉴权 token；首次留空，VTS 会弹授权窗，pyvts 自动写入 `data/anima_chatter/vts_token.txt` |
| `audio_output_device` | str | `CABLE Input@WASAPI` | 本地播放 TTS 的 sounddevice 输出设备，格式 `设备名@驱动名`。通常指向 VB-Cable Input，让 VTS 与直播软件听到同一份音频 |
| `hotkey_map` | dict[str, str] | `{}` | 可选：intent / emotion → VTS Hotkey ID 映射，详见下文 |
| `expression_map` | dict[str, dict[str, str]] | `{}` | 可选：intent / emotion → Live2D `.exp3.json` 文件，详见下文 |

### `hotkey_map` 用法

VTube Studio 的 Hotkeys 面板里每个动画都有一个 Hotkey ID（不是显示名）。在这里
映射后，模型给出的 emotion / intent 会额外触发对应热键：

```toml
[vts]
hotkey_map = { "THINKING" = "ThinkAnim", "happy" = "SmileExpr" }
```

匹配规则（先 intent 后 emotion）：

1. 先查 intent 名（`THINKING` / `EXCITED` / `SURPRISED` / ...）
2. 没命中再查 emotion 主类型（`happy` / `sad` / `angry` / `surprised`）

留空（默认）则完全不触发热键，所有表演由 emotion + intent 参数注入完成
（嘴型 / 表情 / 头部姿态 / 身体晃动）。

适合**复合 hotkey**（动画 + 道具 + 声音组合按钮）。如果你只是想切换某个
`.exp3.json` 表情文件，用 `expression_map` 更直接。

### `expression_map` 用法

把 intent / emotion 映射到 Live2D 表情文件（`.exp3.json`）。走 VTS 的
`ExpressionActivationRequest`，不需要在 VTS 里预先配 hotkey，只要文件物理
存在于模型目录就能调用。

格式：每个键映射到 `{file, desc}` 双字段：

- `file` — `.exp3.json` 文件名（不含路径）
- `desc` — 动作描述。**会被注入到模型 prompt**，让 LLM 知道选哪个 intent 会触
  发什么表情；这一段是模型选对率的关键，描述写得越具体生动，场景化匹配越准

```toml
[vts.expression_map]
EXCITED     = { file = "expression17.exp3.json", desc = "兴奋时左手高举挥舞" }
PROUD_LIFT  = { file = "expression18.exp3.json", desc = "得意时双手比心炫耀" }
```

匹配规则与 `hotkey_map` 一致：先按 intent 名（已大写归一）查，没命中再按
emotion 主类型查。

**互斥设计**——每次说话最多激活一个表情，避免多个手部表情同时显示（多个手
部表情同时 active 会出现"多只手"的视觉错乱）。退出 `speaking_session` 时
自动停用所有上次激活的表情，下一次说话时由 `_sync_expressions` 决定要不要
重新激活。

### 找设备名

```python
import sounddevice as sd
print(sd.query_devices())
```

在输出里找 `Output: ... [WASAPI]` 这一类，把"设备名"和"驱动名"用 `@` 拼起来。
VB-Cable 的 Input 通常显示为 `CABLE Input (VB-Audio Virtual Cable)` —— 配置写
`CABLE Input@WASAPI` 即可。

---

## [vtb_attention]

直播弹幕注意力过滤器。

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `enabled` | bool | `true` | 启用注意力过滤；关闭后未读直接触发 LLM |
| `enable_programmatic_controller` | bool | `true` | 启用程序化概率门；关闭后所有判定走 sub_actor LLM |

权重数值（基础概率 0.1 / 名字命中 +0.7 / 别名命中 +0.4 / 每条未读 +0.05 /
上一回合刚回复 +0.5）与 `default_chatter` 保持一致，硬编码不暴露。这两个开关
和 dfc 同名设置完全平行。

---

## [audio_drive]

直播音频驱动头部与身体律动。

实时计算 TTS 音频包络（RMS + 变化率），按下面增益叠加到 SpeechAnimator 的输出
参数上。原理：声音大时头部微抬、激动；声音突变时身体一震；让程序化动画看起来
像跟着语调起伏。

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `enabled` | bool | `true` | 启用音频驱动律动；关闭后退回固定 sin 波动逻辑 |
| `head_y_gain` | float | `8.0` | 头部前后倾灵敏度（rms × gain → v_head_y 度数） |
| `head_x_gain` | float | `3.0` | 头部横向摆动幅度（rms × gain × sin → v_head_x 度数） |
| `body_y_gain` | float | `30.0` | 身体律动灵敏度（velocity × gain → v_body_y 度数） |
| `body_x_gain` | float | `6.0` | 说话韵律横向轻摆（rms × gain → v_body_x 度数） |
| `body_z_gain` | float | `8.0` | 说话韵率节拍侧向（velocity × gain → v_body_z 度数） |
| `body_bounce_k` | float | `1.2` | 音量 → 身体上下弹跳系数（rms × k → v_body_y 附加弹跳） |
| `neutral_attenuation` | float | `0.5` | emotion=neutral 时整体增益乘数。`0.5` 平静叙述律动减半；`0.0` 平静时完全不动 |

**调参建议**：第一次跑出来八成"太激进"或"太迟钝"，按你的模型的灵敏度配置看着调：

- 模型整体动得过头 → 把所有 `*_gain` 调小一半
- 模型几乎不动 → 把所有 `*_gain` 调大 1.5 倍
- 平静叙述时还是乱抖 → 调小 `neutral_attenuation` 到 `0.0~0.3`

---

## [pipelining]

按实际起播与回复轮次限制预取，积压超过上限时拒排。所有直播语音均后台播放，Action 返回已接收而非已播完。

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `song_prepare_lead_seconds` | float | `25.0` | 歌曲实际起播后，距结束此秒数时允许准备下一回复轮；不包含开场停顿 |
| `max_backlog_seconds` | float | `60.0` | 以估算语音时长限制积压；当前播放轮的歌曲不计入，排队歌曲仍受限 |

当前回复实际起播后，有新弹幕且没有下一轮待播时，Actor 最多提前一轮。同轮多个情绪 Action 不分别占用轮次名额。取未读快照前等待容量开放，实际 NDFC 回复轮入口才推进轮次，follow-up 不额外推进。

旧 `enabled`、`min_duration_seconds`、`trigger_percent` 及固定跨轮静默字段不再参与门控，自动配置同步时移除；不把旧百分比换算为新的歌曲尾段窗口。显式 `[wait:n]` 与 `pre_song_delay` 仍保留，当前回复的音频独立于播放和停顿尽快接收并缓存。字段范围以 [config.py](../config.py) 为准。

---

## [idle_animation]

直播待机动画的频率与幅度。

AutoAnimator 负责眨眼 / 呼吸 / 眼神扫视 / 被动摆动 / 宏观大动作。默认值已经
比原版激进——让 VTB 待机时看起来"活"一些。所有数值都可以按你的模型调整：动得
太狂就调小，呆就调大。

### 眨眼 / 呼吸

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `blink_min_interval` | float | `1.8` | 眨眼最小间隔（秒） |
| `blink_max_interval` | float | `4.0` | 眨眼最大间隔（秒） |
| `breath_freq` | float | `0.28` | 呼吸频率 Hz（0.28Hz ≈ 17 次/分） |
| `breath_amplitude` | float | `0.9` | 呼吸 head_z 振幅（度） |

### 眼神扫视

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `saccade_min_interval` | float | `0.5` | 扫视最小间隔（秒） |
| `saccade_max_interval` | float | `1.8` | 扫视最大间隔（秒） |
| `saccade_big_probability` | float | `0.35` | 大幅扫视概率（其余为小幅微动）；0~1 |
| `saccade_small_amplitude_x` | float | `0.22` | 小扫视水平幅度（0~1） |
| `saccade_small_amplitude_y` | float | `0.15` | 小扫视垂直幅度（0~1） |
| `saccade_big_amplitude_x` | float | `0.7` | 大扫视水平幅度（0~1） |
| `saccade_big_amplitude_y` | float | `0.4` | 大扫视垂直幅度（0~1） |

### 头部 / 身体微动

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `head_micro_scale` | float | `1.5` | 头部微动幅度倍率，`1.0` 为原版基准 |
| `passive_sway_min_interval` | float | `8.0` | 被动慢摆最小间隔（秒） |
| `passive_sway_max_interval` | float | `25.0` | 被动慢摆最大间隔（秒） |

### 宏观动作（重心斜 / 好奇歪头 / 害羞回避等）

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `macro_min_interval` | float | `6.0` | 宏观动作最小间隔（秒） |
| `macro_max_interval` | float | `15.0` | 宏观动作最大间隔（秒） |
| `motion_speed_scale` | float | `2.0` | 宏观动作执行速度倍率（>1 加快，<1 放慢） |

`motion_speed_scale = 2.0` 让宏观动作的 move 阶段从原版 ~2 秒压缩到 ~1 秒，
接近真人头部转向速度。调高 = 动作更快更利落；调低 = 慢镜头风。

### 身体与上半身灵动度

用二阶弹簧阻尼让身体作为头部的"带惯性从动体"：转头时胸腔 / 腰部同向但
滞后跟随，侧歪时反向重心代偿，消除"头转身体不转"的假人感。呼吸则升级为
"胸腔前后仰 + 肩膀滞后起伏"的层次感。

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `body_follow_head_enabled` | bool | `true` | 身体跟随头部耦合总开关（v_body_x / v_body_z 随头部二阶波动） |
| `body_follow_head_f` | float | `1.6` | 身体跟随头部的二阶系统固有频率 Hz，越大跟随越快 |
| `body_follow_head_z` | float | `0.75` | 阻尼比；`1.0` 临界无过冲，`<1.0` 有轻微回弹 |
| `body_follow_head_w_rx` | float | `0.4` | 水平跟随权重：头转 30° 时身体跟转约 `w_rx × 30°` |
| `body_follow_head_w_rz` | float | `0.3` | 侧倾跟随权重：头侧歪时身体侧向跟随比例 |
| `body_follow_head_w_comp` | float | `0.08` | 重心代偿权重：头部横向转时身体反向微补偿（重心侧移感） |
| `breath_shoulder_enabled` | bool | `true` | 呼吸肩相位差开关（胸腔与肩膀不同相起伏） |
| `breath_shoulder_amplitude` | float | `0.9` | 呼吸时肩膀起伏幅度（v_body_z 度数），`0` 关闭 |
| `breath_shoulder_lag` | float | `0.5` | 肩膀相对胸腔的滞后（弧度，约 π/6 ≈ 0.52） |

### 身体常驻律动

真人站立时身体从不静止：横向重心、纵向沉稳、侧向肩腰总在持续但轻缓地摇动。
这条通道用 value noise 三轴独立生成**常驻**身体摇摆底噪，让待机不再"钉在原地"，
逼近 Neuro-sama 那种"始终在活"的质感。与头部耦合、被动慢摆、宏观动作互不干扰。

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `body_idle_enabled` | bool | `true` | 待机身体常驻律动总开关；关闭后只剩呼吸那点可察觉的极弱摆动 |
| `body_idle_scale` | float | `1.0` | 总幅度倍率；调大更明显，调小更安静 |
| `body_idle_x_amp` | float | `4.5` | 横向重心律动幅度（v_body_x 度），左右轻微晃重心 |
| `body_idle_y_amp` | float | `2.5` | 上下沉稳起伏幅度（v_body_y 度） |
| `body_idle_z_amp` | float | `3.5` | 侧向肩腰律动幅度（v_body_z 度） |

调参建议：
- 身体"动得太明显/像抖" → 整体调小 `body_idle_scale`（如 `0.6`），或单轴调小对应 `*_amp`。
- 想要更"活" → 调大 `body_idle_scale` 到 `1.3~1.5`。
- 只想去掉某一轴的晃动 → 单独把该轴 `*_amp` 设 `0`。

> 身体跟得太紧/僵硬 → 调小 `body_follow_head_w_rx` / `body_follow_head_w_rz`
> 或调大阻尼 `body_follow_head_z`；肩膀起伏太明显 → 调小 `breath_shoulder_amplitude`。

---

---

## 直播最小配置

```toml
[plugin]
enabled = true

[vts]
enabled = true
audio_output_device = "CABLE Input@WASAPI"

[vtb_attention]
enabled = true
enable_programmatic_controller = true   # 直播弹幕基数大，必开
```

加上 [`bilibili_live_adapter`](../../bilibili_live_adapter/) 配好凭证就能跑。
