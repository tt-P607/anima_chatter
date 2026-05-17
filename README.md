# voice_chatter

VTube Studio 虚拟形象互动 + 实时语音通话 + 直播弹幕，三模式合一的 Chatter。

## 这玩意儿能做什么

`voice_chatter` 把"用 TTS 把 LLM 回复读出来 + 让 VTube Studio 形象同步表演"
这件事抽出三套场景，按 platform **自动切模式**：

| 模式 | 触发条件 | 场景 |
|------|---------|------|
| **voice** | `platform == "local_asr"` | 与 ASR 适配器配合做实时语音通话；用 TTS 把回复送回适配器播放 |
| **vtb** | 普通群聊 / 私聊（QQ 等），需要先 `/vtb on` 显式接管 | 在群里聊天的同时，让 VTube Studio 形象朗读 + 表演 |
| **vtb_live** | 直播平台（如 `bilibili_live`） | 直播间观众弹幕 → TTS 朗读 + VTS 表演；**消息只入不出** |

模式判定的唯一权威是 [`modes.py`](modes.py)；所有场景文案、user prompt、动作分流都由它驱动。

## 提供的组件

| signature | 类型 | 作用 |
|----|----|----|
| `voice_chatter:chatter:voice_chatter` | Chatter | 三模式共用主循环 |
| `voice_chatter:action:say` | Action | **voice 独占**——把文本送 TTS 让适配器播放 |
| `voice_chatter:action:say_and_perform` | Action | **vtb / vtb_live 共用**——本地 TTS + 驱动 VTube Studio |
| `voice_chatter:action:pass_and_wait` | Action | 三模式共用——说完后挂起等待用户 |
| `voice_chatter:command:vtb` | Command | `/vtb on` / `/vtb off` / `/vtb status`，仅 vtb 模式手动接管时用 |

## 依赖

| 插件 | 必需 | 用途 |
|------|----|----|
| [`asr_adapter`](../asr_adapter/) | voice 模式必需 | ASR 实时语音输入 |
| [`tts_http_server`](../tts_http_server/) | 三模式必需 | TTS HTTP 后端 |
| [`bilibili_live_adapter`](../bilibili_live_adapter/) | vtb_live 才需要 | B 站直播弹幕入站 |
| `pyvts >= 0.3.3` | vtb / vtb_live 才需要 | VTube Studio API 客户端（已声明在 manifest） |

## 文档导航

按你想了解的方向选：

| 我想… | 看这个 |
|------|------|
| 知道整体架构 / 模块布局 | [`docs/architecture.md`](docs/architecture.md) |
| 看完整配置参考（6 个 section / 全部字段） | [`docs/configuration.md`](docs/configuration.md) |
| 跑实时 ASR 通话 | [`docs/voice_mode.md`](docs/voice_mode.md) |
| 在 QQ 群临时切到 VTB 表演 | [`docs/vtb_mode.md`](docs/vtb_mode.md) |
| 接 B 站直播间观众弹幕 | [`docs/vtb_live_mode.md`](docs/vtb_live_mode.md) |

## 快速上手（vtb_live 直播场景）

最常用的链路：B 站直播 + 主播身份码 + VTube Studio。

```toml
# config/plugins/voice_chatter/config.toml
[plugin]
enabled = true

[tts]
endpoint = "http://127.0.0.1:23333/router/tts_http_server/api/tts/v1/synthesize"
provider = "tts_voice_plugin-neo"

[vts]
enabled = true                                # 必须开
audio_output_device = "CABLE Input@WASAPI"    # 让 VTS 与直播软件听到同一份音频
hotkey_map = {}                               # 可选 emotion/intent → VTS Hotkey ID

[vtb_attention]
enabled = true                                 # 注意力过滤；多人弹幕场景必开
enable_programmatic_controller = false         # 看你想用程序化概率门还是 LLM 决策
```

加上 [`bilibili_live_adapter`](../bilibili_live_adapter/) 配好凭证、bot 启动后，B 站直播间的弹幕就会自动以 `platform="bilibili_live"` 进入 voice_chatter，走 vtb_live 路径。

## 工作原理一句话版

```
未读消息 → resolve_mode(platform) → 选 prompt + 选 action
   ↓                                    ↓
   sub_agent 注意力过滤 ────→ create_request("actor")
   （仅 vtb / vtb_live）        + scene_guide(mode)
                                + USER_PROMPT_*(mode)
                                ↓
                                LLM tool call
                                ↓
            ┌──────────────────┼──────────────────┐
            ↓ voice            ↓ vtb / vtb_live   ↓
        say action         say_and_perform     pass_and_wait
        → asr_adapter      → 本地 TTS          → 挂起
        播放                + VTSPerformer
```
