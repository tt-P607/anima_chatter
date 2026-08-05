# anima_chatter

VTube Studio 虚拟形象互动 + 实时语音通话 + 直播弹幕，三模式合一的 Chatter。

> ## 来源
>
> 本插件 fork 自 [Windpicker-owo/voice_chatter](https://github.com/Windpicker-owo/voice_chatter)（语音通话插件），在原作基础上重构扩展为：
>
> - **三模式架构**（voice / vtb / vtb_live），覆盖本地 ASR 通话、VTube Studio 表演、直播弹幕；
> - **主动语音通话**：模型可在私聊中主动发起 / 挂断语音通话（`start_voice_call` + ASR 路由重定向 + 通话历史桥接）；
> - **深度 VTS 集成**：18 项动画意图、行内 `[motion]` 标记、Live2D 表情直接 API 控制；
> - **通话状态感知**：动态注入通话时长 / 状态到 prompt、基于静默时间的超时续期。
>
> 改名为 **anima_chatter**（拉丁语：灵魂、活力）反映了本插件从"语音通话工具"演进为"虚拟人本体（声音 + 形象 + 通话）"的定位变化。

## 这玩意儿能做什么

把"用 TTS 把 LLM 回复读出来 + 让 VTube Studio 形象同步表演"抽成三套场景，按 platform **自动切模式**：

| 模式 | 触发条件 | 场景 |
|------|---------|------|
| **voice** | `platform == "local_asr"` 或通话中 | 与 ASR 适配器配合做实时语音通话，用 TTS 本地播放回复 |
| **vtb** | 普通群聊 / 私聊，需先 `/vtb on` 接管 | 在群里聊天的同时，让虚拟形象朗读 + 表演 |
| **vtb_live** | 直播平台（`platform == "live"`） | 直播间弹幕 → TTS 朗读 + VTS 表演；**消息只入不出** |

模式判定的唯一权威是 [`modes.py`](modes.py)；所有场景文案、user prompt、动作分流都由它驱动。

## 提供的组件

| signature | 类型 | 作用 |
|----|----|----|
| `anima_chatter:chatter:anima_chatter` | Chatter | 三模式共用主循环 |
| `anima_chatter:action:say` | Action | **voice 模式**——TTS 本地播放（不发文字） |
| `anima_chatter:action:say_and_perform` | Action | **vtb / vtb_live 模式**——发文字 + 本地 TTS + 驱动 VTS |
| `anima_chatter:action:sing_song` | Action | **vtb / vtb_live 模式**——播放本地翻唱音频 |
| `anima_chatter:action:start_voice_call` | Action | **跨模式**——在私聊中主动发起语音通话 |
| `anima_chatter:action:end_voice_call` | Action | **通话中**——挂断当前通话 |
| `anima_chatter:action:pass_and_wait` | Action | 三模式共用——说完后挂起等待 |
| `anima_chatter:command:vtb` | Command | `/vtb on\|off\|status`，手动接管 VTB 模式 |
| `anima_chatter:command:voice` | Command | `/voice off\|status`，语音通话兜底控制 |

## 代码结构

```text
anima_chatter/
├── plugin.py            # 插件装配与生命周期（只做资源初始化，不含对话逻辑）
├── config.py            # 全部配置字段的唯一真源（7 个 section）
├── constants.py         # intent / emotion 合法值与归一化
├── modes.py             # 三态模式判定
├── protocol.py          # 插件实例的类型安全视图
├── _internal_compat.py  # 唯一允许 import 框架内部模块的位置
│
├── chatter/             # Chatter 实现
│   ├── core.py          #   主类，实现 chat_core 的各 adapter 协议
│   ├── attention.py     #   注意力过滤（概率门 + sub_actor 决策）
│   ├── request_factory.py #  LLM 请求构造
│   ├── session_bridge.py  #  与 chat_core 的结构化协议
│   └── logging.py       #   Rich 面板转义包装器
│
├── speech/              # 语音合成与播放（唯一实现）
│   ├── markers.py       #   内联标记解析与分句
│   ├── backend.py       #   TTS HTTP 客户端
│   ├── synthesis.py     #   分段并发合成调度
│   └── playback.py      #   播放：阻塞模式 + vtb_live 流水线
│
├── runtime/             # 跨模块共享的运行时状态
│   ├── call_state.py    #   通话状态（同时只允许一个）
│   ├── pipeline_state.py#   vtb_live 音频流水线时间轴
│   ├── sung_history.py  #   已唱歌曲历史
│   └── heartbeat.py     #   长阻塞期间喂 watchdog
│
├── voice_call/          # 语音通话子域
│   ├── lifecycle.py     #   ASR 切换、告别播放、终结事件
│   └── identity.py      #   通话对方身份反查
│
├── actions/             # Action（只解析参数，播放走 speech/）
├── audio/               # 本地音频播放、响度归一化、包络提取
├── vts/                 # VTube Studio 连接与动画
├── prompts/             # 场景文案、模板与构建器
└── test/                # 单元测试
```

## 依赖与配套关系

使用**实时语音通话**功能必须搭配以下定制版本的配套插件，原版插件无法兼容：

| 插件名称 | 必需性 | 作用与来源 |
|------|----|----|
| [`asr_adapter_anima`](../asr_adapter_anima/) | voice 模式必需 | 定制版 ASR 适配器，提供 `asr_redirect` 文本重定向与按需启动服务。➜ [GitHub](https://github.com/tt-P607/asr_adapter_anima) |
| [`funasr_asr_provider_anima`](../funasr_asr_provider_anima/) | voice 模式必需 | 定制版 FunASR 后端提供商，向 ASR 适配器提供语音推理。➜ [GitHub](https://github.com/tt-P607/funasr_asr_provider_anima) |
| [`tts_http_server`](../tts_http_server/) | 三模式必需 | 语音合成本地服务，可用原作者官方版本 |
| [`neo_default_chatter`](../neo_default_chatter/) | 三模式必需 | 提供 `chat_core` 会话控制流；anima 通过订阅 `neo_default_chatter:*` 事件复用其主会话逻辑并注入三模式 prompt / 注意力过滤 |
| 直播弹幕适配器 | vtb_live 模式必需 | 需声明 `platform = "live"` 与 `source_platform` 类属性 |

Python 依赖见 [`manifest.json`](manifest.json) 的 `python_dependencies`（pyvts / sounddevice / soundfile / rapidfuzz 等）。

## 工作原理

```text
未读消息 → resolve_mode(platform) → 选 prompt + 选 action
   │                                    │
   ├─ sub_agent 注意力过滤 ────→ create_request("actor")
   │  （仅 vtb / vtb_live）        + scene_guide(mode)
   │                               + USER_PROMPT(mode)
   │                                    ↓
   │                               LLM tool call
   │                                    ↓
   │        ┌──────────────────┬────────┴────────┐
   │        ↓ voice            ↓ vtb / vtb_live   ↓
   │    say action        say_and_perform    pass_and_wait
   │    → 本地 TTS 播放    / sing_song        → 挂起
   │                      → 发送文本
   │                      + 本地 TTS
   │                      + VTSPerformer
   │                            │
   └────────────────────────────┘
      vtb_live 额外经过流水线：
      Action reserve 时段 → 派发后台播放 → 立即返回
      LLM 在 fetch_unreads 处等门 → 门到点聚合弹幕 → 新一轮
```

## 快速上手（语音通话场景）

1. 确保 `asr_adapter_anima` 已加载（即便配置为 `enabled = false` 也可以，插件会自动按需启动）。
2. 在私聊中对模型说"想跟你语音聊"，模型会调用 `start_voice_call`。
3. 听到"我打给你吧"提示后，直接对着麦克风说话即可（无需按键，通话期间 ASR 强制 `always_on`）。
4. 挂断：模型说"挂了"，或双方安静超过 5 分钟自动挂断。

## 文档导航

| 我想… | 看这个 |
|------|------|
| 知道整体架构 / 模块布局 | [`docs/architecture.md`](docs/architecture.md) |
| 看完整配置参考（7 个 section / 全部字段） | [`docs/configuration.md`](docs/configuration.md) |
| 跑实时 ASR 通话 | [`docs/voice_mode.md`](docs/voice_mode.md) |
| 在群聊临时切到 VTB 表演 | [`docs/vtb_mode.md`](docs/vtb_mode.md) |
| 接直播间观众弹幕 | [`docs/vtb_live_mode.md`](docs/vtb_live_mode.md) |

## 故障排查

| 现象 | 排查方向 |
|------|---------|
| 语音通话无法开始 | 确认 `asr_adapter_anima:service:asr_redirect` 与对应 adapter 均已注册，检查 FunASR Provider 是否加载成功 |
| TTS 返回 503 | `tts_http_server` 当前没有可用 Provider，先访问状态接口确认默认 Provider |
| TTS 返回 502 | 检查 Provider 日志——服务会拒绝 Provider 异常、空音频和非法 Base64 音频 |
| VTube Studio 无动作 | 确认 VTS API 已开启、授权令牌有效，检查配置的 Hotkey / Expression 名称是否存在 |
| 直播时 bot 突然不响应弹幕 | 检查日志里是否出现"唤醒 stream 失败"——框架内部结构变更会导致流水线唤醒失效 |
| 插件卸载后资源未释放 | 卸载流程会终止活跃通话、清空流水线任务并关闭 VTS；相关警告会记录具体失败资源 |

## 验证

```bash
uv run pytest plugins/anima_chatter/test -q --no-cov
uv run ruff check plugins/anima_chatter
```

实机验证至少覆盖：

1. voice 模式开始通话后，ASR 自动切为 `always_on` 并把真实用户身份路由到原 Stream。
2. 挂断或超时后，ASR redirect、激活覆写、通话状态和后台任务全部释放。
3. vtb 模式可以完成 TTS 播放与 VTube Studio 表演。
4. vtb_live 长音频期间流水线门能继续聚合新弹幕，卸载插件后不残留唤醒任务。
