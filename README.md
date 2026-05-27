# anima_chatter

VTube Studio 虚拟形象互动 + 实时语音通话 + 直播弹幕，三模式合一的 Chatter。

> ## 来源
>
> 本插件 fork 自 [Windpicker-owo/voice_chatter](https://github.com/Windpicker-owo/voice_chatter)（语音通话插件），在原作的基础上重构扩展为：
> - **三模式架构**（voice / vtb / vtb_live），覆盖本地 ASR 通话、VTube Studio 表演、B 站直播弹幕；
> - **主动语音通话**：模型可在 QQ 私聊中主动发起/挂断语音通话（``start_voice_call`` action + ASR 路由重定向 + 通话历史摘要桥接）；
> - **深度 VTS 集成**：18 项动画意图、行内 ``[motion]`` 标记、Live2D 表情直接 API 控制；
> - **通话状态感知**：动态注入通话时长/状态到 prompt、基于静默时间的超时续期。
>
> 改名为 **anima_chatter**（拉丁语：灵魂、活力）反映了本插件从"语音通话工具"演进为"虚拟人本体（声音 + 形象 + 通话）"的定位变化。

## 这玩意儿能做什么

`anima_chatter` 把"用 TTS 把 LLM 回复读出来 + 让 VTube Studio 形象同步表演"这件事抽出三套场景，按 platform **自动切模式**：

| 模式 | 触发条件 | 场景 |
|------|---------|------|
| **voice** | `platform == "local_asr"` 或通话中 | 与 ASR 适配器配合做实时语音通话；用 TTS 本地播放回复 |
| **vtb** | 普通群聊 / 私聊，需要先 `/vtb on` 接管 | 在群里聊天的同时，让 VTube Studio 形象朗读 + 表演 |
| **vtb_live** | 直播平台（如 `bilibili_live`） | 直播间观众弹幕 → TTS 朗读 + VTS 表演；**消息只入不出** |

模式判定的唯一权威是 [`modes.py`](modes.py)；所有场景文案、user prompt、动作分流都由它驱动。

## 提供的组件

| signature | 类型 | 作用 |
|----|----|----|
| `anima_chatter:chatter:anima_chatter` | Chatter | 三模式共用主循环 |
| `anima_chatter:action:say` | Action | **voice 模式**——TTS 本地播放（不发文字） |
| `anima_chatter:action:say_and_perform` | Action | **vtb / vtb_live 模式**——发文字 + 本地 TTS + 驱动 VTS |
| `anima_chatter:action:start_voice_call` | Action | **跨模式**——在私聊中主动发起语音通话 |
| `anima_chatter:action:end_voice_call` | Action | **通话中**——挂断当前通话 |
| `anima_chatter:action:pass_and_wait` | Action | 三模式共用——说完后挂起等待用户 |
| `anima_chatter:command:vtb` | Command | `/vtb on/off/status`，手动接管 VTB 模式 |
| `anima_chatter:command:voice` | Command | `/voice off/status`，语音通话兜底控制 |

## 依赖

| 插件 | 必需 | 用途 |
|------|----|----|
| [`asr_adapter_anima`](../asr_adapter_anima/) | voice 模式必需 | ASR 实时语音输入 + 转发路由 |
| [`tts_http_server`](../tts_http_server/) | 三模式必需 | TTS HTTP 后端 |
| [`bilibili_live_adapter`](../bilibili_live_adapter/) | vtb_live 才需要 | B 站直播弹幕入站 |
| `pyvts >= 0.3.3` | vtb / vtb_live 才需要 | VTube Studio API 客户端 |

## 文档导航

按你想了解的方向选：

| 我想… | 看这个 |
|------|------|
| 知道整体架构 / 模块布局 | [`docs/architecture.md`](docs/architecture.md) |
| 看完整配置参考（6 个 section / 全部字段） | [`docs/configuration.md`](docs/configuration.md) |
| 跑实时 ASR 通话 | [`docs/voice_mode.md`](docs/voice_mode.md) |
| 在 QQ 群临时切到 VTB 表演 | [`docs/vtb_mode.md`](docs/vtb_mode.md) |
| 接 B 站直播间观众弹幕 | [`docs/vtb_live_mode.md`](docs/vtb_live_mode.md) |

## 快速上手（语音通话场景）

1. 确保 `asr_adapter_anima` 已加载（即便配置为 `enabled = false` 也可以，插件会自动按需启动）。
2. 在 QQ 私聊中对模型说"想跟你语音聊"，模型会调用 `start_voice_call`。
3. 听到"我打给你吧"提示后，直接对着麦克风说话即可（无需按键，通话期间 ASR 强制 `always_on`）。
4. 挂断：模型说"挂了"或双方安静超过 5 分钟。

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
        → 本地 TTS          → 发送文本          → 挂起
        播放                + 本地 TTS
                           + VTSPerformer
```
