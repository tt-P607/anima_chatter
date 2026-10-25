# Anima 直播

Anima 直播是 Neo-MoFox 的直播互动插件。它接收观众弹幕，生成语音回复，并可驱动 VTube Studio 中的虚拟形象，也支持播放本地歌曲。

## 功能

- 根据弹幕内容和直播节奏选择回应，不要求逐条回复。
- 使用 GPT-SoVITS 流式合成语音，在生成过程中开始播放。
- 根据回复的情绪和表达方式触发 VTube Studio 表情、动作及身体律动。
- 可通过 `control_body` 设置呼吸、眨眼和左右眼睑控制；控制持续生效，直到再次设置或释放眼睑。
- 支持说话时的音频驱动动画和待机动画；不开启 VTube Studio 时仍可播放语音。
- 支持本地歌库、歌曲查找与播放，可单独关闭歌曲功能。
- 按顺序播放语音和歌曲，在播放期间准备下一轮回复。

## 使用条件

- Neo-MoFox 1.2.0-alpha 或更新版本，以及 Python 3.11 或更新版本。
- 启用 `neo_default_chatter` 和 `tts_voice_plugin-neo`，并配置好对话模型。
- TTS 插件须提供语音服务 `tts_voice_plugin-neo:service:speech` 和公共语音规则模板；仅有旧版完整音频接口的版本不能使用。
- 配置支持 V5 PCM 流的 GPT-SoVITS 服务，并在 TTS 插件中开启 `[tts_streaming].enabled`，设置 `streaming_mode = 2`。
- 安装与直播平台对应的弹幕适配器，例如 `bilibili_live_adapter`。适配器需要将聊天流标记为 `live`。
- 使用虚拟形象时，需要运行 VTube Studio、加载模型并开启插件 API。

## 安装与配置

在 Neo-MoFox 项目根目录执行：

```bash
git clone https://github.com/tt-P607/anima_chatter.git plugins/anima_chatter
```

首次加载后，配置位于 `config/plugins/anima_chatter/config.toml`。主要设置如下：

| 设置 | 用途 |
| --- | --- |
| `plugin` | 对话模型、直播提示词、插件和歌曲功能开关 |
| `vts` | VTube Studio 接入、音频输出设备、表情和热键映射 |
| `vtb_attention` | 弹幕回应策略 |
| `audio_drive` | 说话时随声音变化的动作幅度 |
| `pipelining` | 回复排播与歌曲期间的下一轮准备 |
| `idle_animation` | 待机动作频率和幅度 |

直播音频从 `vts.audio_output_device` 指定的本机设备输出。需要让直播间听到回复时，在直播软件中采集该设备的音频；使用虚拟声卡时，应避免重复采集。

VTube Studio 默认关闭。需要形象表演时开启 `vts.enabled`，并在 VTube Studio 的授权窗口中允许连接。表情与热键名称需要和所用模型对应。

歌曲放在 `data/anima_chatter/songs/`，支持准备好的人声与伴奏双轨。没有歌库或不需要歌曲功能时，关闭 `plugin.enable_singing`。此功能播放已有音频，不生成或翻唱新歌曲。

详细设置见 [配置说明](docs/configuration.md)，直播接入与回应方式见 [直播使用说明](docs/vtb_live_mode.md)。

## 身体控制

直播 Action `control_body` 立即应用 `body` 参数，不等待语音播完。`say_and_perform` 可附带同样的 `body`，在首块朗读 PCM 实际写入设备时应用，不传给 TTS。两者只修改指定字段，语音结束或取消不会释放已应用的持续控制。

语音携带的控制按提交顺序登记，实际起播时跳过已被较新指令修改的字段，仍应用未冲突的其他字段。眼睑开度和释放视为同一目标，较旧的释放不会覆盖较新的开度。

| 控制节 | 字段与单位 |
| --- | --- |
| `breath` | `frequency_hz`：0.05–2 Hz；`depth`：配置幅度的 0–3 倍；`inhale_ratio`：吸气/呼气时长比 0.2–5 |
| `breath` | `hold: true` 减速屏息，`hold: false` 原位恢复；`transition_seconds`：0.05–10 秒，初始 0.5；`hold_seconds`：停稳后保持 0–60 秒，配合 `hold: true`，省略则保持到恢复 |
| `blink` | `interval_seconds`：0.2–60 秒；`close_seconds`：0.02–5 秒；`hold_seconds`：0–10 秒；`open_seconds`：0.02–5 秒；`now: true` 主动眨眼 |
| `left_eye` / `right_eye` | `openness`：0 闭眼、1 全开；`transition_seconds`：0.02–10 秒，初始 0.3；`hold_seconds`：到达后保持 0–120 秒，再自然释放；`release: true` 显式释放，与开度/保持时长互斥 |

眼睑初始为持续保持；省略 `hold_seconds` 保留本侧已有的保持设置，显式传入 `null` 清除定时并恢复无限保持，例如 `{"body":{"left_eye":{"hold_seconds":null}}}`。左右眼睑独立，受控或释放中的眼睑不响应眨眼，也不会积压补眨。修改眨眼节奏只影响后续眨眼，不重启当前闭合或睁开。

例如，独立调慢呼吸并让左眼半闭：

```json
{
	"body": {
		"breath": {"frequency_hz": 0.2, "depth": 1.2, "inhale_ratio": 0.5},
		"left_eye": {"openness": 0.5, "transition_seconds": 0.6}
	}
}
```

主动眨眼使用 `{"body":{"blink":{"now":true}}}`；屏息使用 `{"body":{"breath":{"hold":true}}}`；恢复呼吸并释放左眼使用 `{"body":{"breath":{"hold":false},"left_eye":{"release":true}}}`。在 `say_and_perform` 调用中添加 `content` 等语音参数即可随起播应用。

能力使用既有 `v_eye_left` / `v_eye_right`、头部 `v_head_z` 和身体 `v_body_y` / `v_body_z` 参数，不提供独立胸腔形变或真实呼吸模拟。需要模型已映射这些输入；注册输入参数本身不保证皮套具备对应形变。VTS 未就绪时，独立控制或带 `body` 的语音调用会明确拒绝；不带 `body` 的语音仍可正常播放。

## 使用范围

- 只处理直播弹幕，不接管普通私聊、群聊或麦克风输入。
- 回复通过本机音频设备播放，不向直播间发送文字弹幕。
- 不提供语音通话或语音识别；需要通话功能时使用独立的 `anima_voice` 插件。
- 不负责开播、推流或画面采集，这些由直播平台和直播软件完成。
- 形象动作效果取决于模型支持的参数、表情和热键；语音延迟取决于合成服务与设备性能。
- 使用歌曲及模型素材时，应确认其使用和直播授权。

## 来源

本插件源自 [Windpicker-owo/voice_chatter](https://github.com/Windpicker-owo/voice_chatter)。
