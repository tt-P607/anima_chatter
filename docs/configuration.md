# 配置参考

`anima_chatter` 用 6 个 section 表达完整配置。所有字段都有合理默认值，第一次跑
只需要按 [README 快速上手](../README.md) 那段填几项关键字段，剩下的等需要时再调。

配置文件路径：``config/plugins/anima_chatter/config.toml``

## 整体结构

| Section | 适用模式 | 用途 |
|---------|------|------|
| [`[plugin]`](#plugin) | 三模式共享 | 通用 chatter 行为 |
| [`[tts]`](#tts) | 三模式共享 | TTS HTTP 后端 |
| [`[vts]`](#vts) | vtb / vtb_live | VTube Studio 长连 + 本地音频 + Hotkey 映射 |
| [`[vtb_attention]`](#vtb_attention) | vtb / vtb_live | "是否回复"过滤器（注意力门）|
| [`[audio_drive]`](#audio_drive) | vtb / vtb_live | 音频驱动头部 / 身体律动 |
| [`[idle_animation]`](#idle_animation) | vtb / vtb_live | 待机动画频率 / 幅度 |

类型定义都在 [`config.py`](../config.py)，下面按 section 列字段。

---

## [plugin]

通用 chatter 行为（三模式共享）。

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `enabled` | bool | `true` | 是否启用本 chatter |
| `tick_interval` | float | `1.0` | vtb / vtb_live 模式的 tick 间隔（秒）；**voice 模式强制 0.1** |
| `allow_message_buffer` | bool | `true` | vtb / vtb_live 模式是否允许消息缓冲；**voice 模式强制 false** |
| `plain_text_retry_limit` | int | `1` | 模型返回纯文本（未调用 say / say_and_perform）时的提醒重试次数 |
| `enable_action_suspend` | bool | `true` | 启用纯 Action 回合的挂起；关闭则纯 Action 结果继续 follow-up |

---

## [tts]

TTS HTTP 后端配置（三模式共享）。

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `endpoint` | str | `http://127.0.0.1:8000/router/tts_http_server/api/tts/v1/synthesize` | TTS 合成接口 |
| `timeout` | float | `30.0` | HTTP 请求超时（秒） |
| `max_parallel_segments` | int | `4` | 最大并行合成句子数 |
| `empty_audio_retry_count` | int | `1` | TTS 返回空音频时的重试次数 |
| `sentence_split_enabled` | bool | `true` | 是否按句切分并并行合成 |
| `mime_type` | str | `audio/wav` | TTS 音频 MIME 类型 |
| `provider` | str | `qwen_tts` | TTS provider 名；留空使用服务端默认 |
| `emit_text_on_tts_failure` | bool | `false` | TTS 失败时是否回退发送文本 |

---

## [vts]

VTube Studio 长连 + 本地音频输出 + Hotkey 映射。仅 vtb / vtb_live 模式生效。

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
| `enabled` | bool | `false` | 是否启用 VTS；**关闭后 vtb 系仅播 TTS，不驱动虚拟形象** |
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

注意力过滤器（旧 `[sub_agent]`，重命名后语义更直观）。仅 vtb / vtb_live 生效。

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `enabled` | bool | `true` | 启用注意力过滤；关闭后每条未读直接触发 LLM。一对一私聊建议关，群聊 / 直播间建议开 |
| `enable_programmatic_controller` | bool | `true` | 启用程序化概率门；关闭后所有判定走 sub_actor LLM |

权重数值（基础概率 0.1 / 名字命中 +0.7 / 别名命中 +0.4 / 每条未读 +0.05 /
上一回合刚回复 +0.5）与 `default_chatter` 保持一致，硬编码不暴露。这两个开关
和 dfc 同名设置完全平行。

---

## [audio_drive]

音频驱动头部 / 身体律动。仅 vtb / vtb_live 生效。

实时计算 TTS 音频包络（RMS + 变化率），按下面增益叠加到 SpeechAnimator 的输出
参数上。原理：声音大时头部微抬、激动；声音突变时身体一震；让程序化动画看起来
像跟着语调起伏。

| 字段 | 类型 | 默认 | 说明 |
|------|------|----|------|
| `enabled` | bool | `true` | 启用音频驱动律动；关闭后退回固定 sin 波动逻辑 |
| `head_y_gain` | float | `8.0` | 头部前后倾灵敏度（rms × gain → v_head_y 度数） |
| `head_x_gain` | float | `3.0` | 头部横向摆动幅度（rms × gain × sin → v_head_x 度数） |
| `body_y_gain` | float | `30.0` | 身体律动灵敏度（velocity × gain → v_body_y 度数） |
| `neutral_attenuation` | float | `0.5` | emotion=neutral 时整体增益乘数。`0.5` 平静叙述律动减半；`0.0` 平静时完全不动 |

**调参建议**：第一次跑出来八成"太激进"或"太迟钝"，按你的模型的灵敏度配置看着调：

- 模型整体动得过头 → 把所有 `*_gain` 调小一半
- 模型几乎不动 → 把所有 `*_gain` 调大 1.5 倍
- 平静叙述时还是乱抖 → 调小 `neutral_attenuation` 到 `0.0~0.3`

---

## [idle_animation]

待机自动化动画的频率与幅度。仅 vtb / vtb_live 生效。

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

---

## 三种模式的最小配置

### 仅 voice 模式（ASR 通话）

```toml
[plugin]
enabled = true

[tts]
endpoint = "http://127.0.0.1:8000/router/tts_http_server/api/tts/v1/synthesize"
provider = "qwen_tts"

# vts / vtb_attention / audio_drive / idle_animation 全部走默认即可——voice 模式不读它们
```

### 仅 vtb 模式（QQ 群手动接管）

```toml
[plugin]
enabled = true

[tts]
endpoint = "http://127.0.0.1:8000/router/tts_http_server/api/tts/v1/synthesize"
provider = "qwen_tts"

[vts]
enabled = true
audio_output_device = "CABLE Input@WASAPI"

[vtb_attention]
enabled = true
enable_programmatic_controller = true   # 群聊建议开
```

### 仅 vtb_live 模式（B 站直播间）

```toml
[plugin]
enabled = true

[tts]
endpoint = "http://127.0.0.1:8000/router/tts_http_server/api/tts/v1/synthesize"
provider = "qwen_tts"

[vts]
enabled = true
audio_output_device = "CABLE Input@WASAPI"

[vtb_attention]
enabled = true
enable_programmatic_controller = true   # 直播弹幕基数大，必开
```

加上 [`bilibili_live_adapter`](../../bilibili_live_adapter/) 配好凭证就能跑。
