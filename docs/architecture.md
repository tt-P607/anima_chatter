# 整体架构

`anima_chatter` 把"用 TTS 让 LLM 输出 + 让 VTube Studio 形象同步表演"封装成
一个三模式 Chatter，按 ``chat_stream.platform`` 自动分流。本文档解释模块布局、
模式判定中枢和共享层的关系。

## 模块布局（rev. 2026-05-17）

```
anima_chatter/
├── plugin.py                 # 主入口：注册 chatter / actions / command + 初始化 VTS
├── modes.py                  # ★模式判定中枢：ChatterMode + LIVE_PLATFORMS + resolve_mode
├── config.py                 # 6 个 section 的 BaseConfig 类
├── manifest.json
├── README.md
│
├── runner.py                 # 主对话循环（三模式共用）
├── sub_agent.py              # vtb / vtb_live 注意力过滤（与 dfc 同步）
├── markers.py                # [wait] / [emotion] 标记解析 + 句子切分
├── tts.py                    # TTS HTTP 后端（HttpTTSBackend / LoggingTTSBackend）
├── commands.py               # /vtb on|off|status 命令
│
├── prompts/                  # 提示词层（按职责拆 3 个文件）
│   ├── __init__.py           #   对外暴露
│   ├── scenes.py             #   三种模式的 <scene_and_protocol> 文案
│   ├── templates.py          #   SYSTEM_PROMPT + 三个 USER_PROMPT_* 模板
│   └── builder.py            #   AnimaChatterPromptBuilder 组装类
│
├── actions/                  # LLM 工具
│   ├── say.py                #   voice 独占：文本 → TTS → ASR adapter 播放
│   ├── say_and_perform.py    #   vtb / vtb_live 共用：本地 TTS + VTS 表演
│   └── pass_and_wait.py      #   三模式共用：挂起等用户
│
├── audio/                    # 本地音频（vtb / vtb_live 共用）
│   ├── player.py             #   AudioPlayer（sounddevice → VB-Cable）
│   └── envelope.py           #   音频包络抽取（驱动头部 / 身体律动）
│
├── vts/                      # VTube Studio 集成（vtb / vtb_live 共用）
│   ├── connection.py         #   VTSConnection（pyvts 长连 + 三个后台 loop）
│   ├── performer.py          #   VTSPerformer（speaking_session / play / 热键）
│   └── animation/            #   动画引擎
│       ├── base.py           #     BaseAnimator
│       ├── auto.py           #     AutoAnimator（眨眼 / 呼吸 / 扫视 / 宏观动作）
│       └── speech.py         #     SpeechAnimator（说话期 emotion + 音频驱动）
│
└── docs/
    ├── architecture.md       # 本文档
    ├── configuration.md      # 配置完整参考
    ├── voice_mode.md         # voice 模式细节
    ├── vtb_mode.md           # vtb 模式细节
    └── vtb_live_mode.md      # vtb_live 模式细节
```

## 模式判定中枢

[`modes.py`](../modes.py) 是**整个插件唯一的模式判定来源**，导出三样东西：

```python
ChatterMode = Literal["voice", "vtb", "vtb_live"]
LIVE_PLATFORMS: frozenset[str] = frozenset({"bilibili_live"})
def resolve_mode(chat_stream) -> ChatterMode: ...
```

判定优先级：

1. ``platform == "local_asr"`` → ``voice``
2. ``platform`` 在 ``LIVE_PLATFORMS`` → ``vtb_live``
3. 其它 → ``vtb``

下游所有"按模式分流"的地方都调 ``resolve_mode``：

- [`prompts/builder.py`](../prompts/builder.py) → 选场景文案 + 选 user 模板
- [`plugin.py:_resolve_mode`](../plugin.py) → action_suspend_guidance 文案 / mode 透传
- [`runner.py`](../runner.py) → 是否要跑 sub_agent 注意力过滤（vtb 系才跑）
- 各 action 的 ``go_activate()`` → 按 ``platform == "local_asr"`` 直接判，与中枢逻辑一致

**新增模式 / 新增直播平台只改一处**：

- 加新直播平台 → 在 ``LIVE_PLATFORMS`` 加一行字符串。
- 加新模式（如 Twitch 完全独立场景） → 同时改 ``ChatterMode`` Literal、``resolve_mode``
  分支、``prompts/scenes.py`` 加新 GUIDE、``prompts/templates.py`` 加新 USER_PROMPT、
  ``prompts/builder.py`` 的 ``get_scene_guide`` / ``build_user_prompt`` 加分支。

## 共享层 vs 模式专属

| 层 | voice | vtb | vtb_live | 说明 |
|----|:--:|:--:|:--:|----|
| TTS HTTP 后端（``tts.py``） | ✓ | ✓ | ✓ | 三模式共用 |
| 主对话循环（``runner.py``） | ✓ | ✓ | ✓ | 三模式共用 |
| ``markers.py`` 标记解析 | ✓ | ✓ | ✓ | 三模式共用 |
| ``pass_and_wait`` action | ✓ | ✓ | ✓ | 三模式共用 |
| 提示词层（``prompts/``） | ✓ | ✓ | ✓ | 文案按 mode 分支，模板字符串集中 |
| ``sub_agent.py`` 注意力过滤 | ✗ | ✓ | ✓ | vtb 系共用，群聊 / 直播间避免无脑回 |
| ``vts/`` VTube Studio 集成 | ✗ | ✓ | ✓ | vtb 系共用 |
| ``audio/`` 本地播放 | ✗ | ✓ | ✓ | vtb 系共用 |
| ``say`` action | ✓ | ✗ | ✗ | voice 独占（输出走 ASR adapter） |
| ``say_and_perform`` action | ✗ | ✓ | ✓ | vtb 系共用（本地 TTS + VTS） |
| ``/vtb`` 命令 | ✗ | ✓ | ✗ | vtb 独占（直播流由 platform 自动分流，不需手动接管） |

## 运行时关键流程

### 1. 流绑定阶段

ChatterManager 看到一条流，按平台 / chat_type 评分挑 chatter；anima_chatter
通过 ``associated_platforms = ["local_asr"]`` 的弱声明吸引 ASR 流，其余平台
默认绑回 default_chatter——直到用户 ``/vtb on``，或这条流来自直播平台
（platform 命中 ``LIVE_PLATFORMS`` 自动判定为 vtb_live）。

详细：``commands.py`` 的 ``/vtb`` 实现里调 ``chat_api.register_active_chatter``
+ 重启 stream loop 销毁旧 chatter 生成器；这是手动接管的核心。

### 2. 单 tick 对话循环（``runner.py``）

```
while not stopping:
    fetch_unreads() → 没有就 yield Wait()
    if mode in (vtb, vtb_live):
        sub_agent.should_respond() → 不响应就 flush 后 yield Wait()
    build_history + build_user_prompt(mode) → 注入 LLM 上下文
    LLM call → tool calls
    分发：
      - say / say_and_perform → 进 TTS → 进 audio / asr_adapter
      - pass_and_wait → 标记本轮等待
    根据 enable_action_suspend 决定是 yield Wait() 还是继续 follow-up
```

### 3. say_and_perform 表演链路（vtb 系）

```
LLM 输出 content + emotion + intent
 ↓
markers.parse_speech_segments()  # [wait] / [emotion] 解析 + 句子切分
 ↓
分段并行 TTS（HttpTTSBackend）→ 按 idx 顺序消费
 ↓
performer.speaking_session(emotion, intent):
   ├─ SpeechAnimator.set_emotion / set_intent / set_speaking(True)
   ├─ AutoAnimator.set_performing(True)（淡出待机动画）
   ├─ 解析 hotkey_map 触发 VTS Hotkey（可选）
   └─ for each 段:
        audio_player.play_audio(wav)         # sounddevice → VB-Cable
                ↓
        envelope_tracker  ← 离线 RMS 包络 → SpeechAnimator.update()
                                               用 v_head_y / v_body_y 等参数
```

## 与 default_chatter 的关系

- ``sub_agent.py`` 的概率门权重和 ``sub_actor`` 调用流程**手动复刻**自 dfc，
  没有共享代码。dfc 升级时需同步本插件——文件顶部 ``.. warning::`` 块有提醒。
- ``runner.py`` 的"读未读 / 调 LLM / 处理 tool calls / 挂起"骨架与 dfc 高度
  相似但不复用，因为 voice / vtb / vtb_live 各有专属处理（纯文本提醒措辞、
  stop_conversation 拒绝、纯 Action 挂起策略）。

## 待优化项（已知未做）

| 项 | 严重度 | 处置 |
|----|:--:|----|
| ``commands.py`` 直接 import ``src.core.transport.distribution.stream_loop_manager`` | 低 | 文件内 ``# NOTE`` 块说明等公开 API 后替换；default_chatter / kokoro_flow_chatter 同样这么干 |
| ``vts/animation/auto.py`` 670 行单文件 | 低 | 内部多个动作字典 + 注释占多数；强行拆分会让共享上下文复杂化 |
