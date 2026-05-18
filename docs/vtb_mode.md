# vtb 模式：群聊 / 私聊手动接管的 VTube Studio 表演

非直播平台（如 QQ）默认绑定的是 `default_chatter` 之类的标准 chatter。要让某条
流走 anima_chatter 的 VTB 表演链路，**必须**用 [`/vtb on` 命令手动接管](#vtb-命令)。

> 直播流（`platform == "bilibili_live"` 等）不需要这一步——见 [vtb_live_mode.md](vtb_live_mode.md)。

## 触发条件

- `platform != "local_asr"` 且 `platform` 不在 [`LIVE_PLATFORMS`](../modes.py) 中
- 该流必须由 `/vtb on` 显式接管为 anima_chatter

## 运行特征

| 维度 | 设定 |
|------|------|
| `stream_tick_interval` | 取自 `[plugin] tick_interval`（默认 1.0） |
| `allow_message_buffer` | 取自 `[plugin] allow_message_buffer`（默认 true） |
| 注意力过滤 | ✓ 启用（私聊场景自动放行；群聊跑概率门 + sub_actor LLM）|
| Action 暴露 | `say_and_perform` + `pass_and_wait` |
| 提示词 | [`USER_PROMPT_VTB`](../prompts/templates.py) + [`VTB_SCENE_GUIDE`](../prompts/scenes.py) |
| VTS / 本地音频 | 必须开（`[vts] enabled = true`），否则模式不可用 |

`apply_stream_runtime_options` 看到 `platform != "local_asr"` 后会读取
`[plugin]` section 的两个字段，覆写到流的 `context`。

## /vtb 命令

定义于 [`commands.py`](../commands.py)，权限：`OWNER`。

| 子命令 | 行为 |
|--------|------|
| `/vtb on` | 释放当前流的活跃 chatter → 注册 anima_chatter 实例 → 重启 stream loop（销毁旧 chatter 生成器）|
| `/vtb off` | 反向操作 → 下一轮自动绑回 default_chatter |
| `/vtb status` | 打印当前流的活跃 chatter、是否处于 VTB 接管、平台名 |

> **重要细节**：仅 `register_active_chatter` 不够——`StreamLoopManager` 缓存了
> `chatter.execute()` 返回的异步生成器到 `_chatter_genes`。不重启 stream loop
> 的话下一 tick 还会推进旧 chatter 生成器，命令"看似执行成功但完全不生效"。
> 这就是 [`commands.py`](../commands.py) 直接 import 内部模块 `stream_loop_manager`
> 的唯一原因，等公开 API 补齐后会替换。

## 核心 Action：`say_and_perform`

签名：`anima_chatter:action:say_and_perform`，定义于 [`actions/say_and_perform.py`](../actions/say_and_perform.py)。

**`go_activate`**：仅 `platform != "local_asr"` 激活，覆盖 vtb / vtb_live 两种模式。

**输入参数**：

| 参数 | 类型 | 描述 |
|------|------|------|
| `content` | `list[str]` | 按发送顺序排列的字符串列表；默认 1 段，超 30s 才拆 |
| `emotion` | `str` | `"类型:强度"`（`{neutral,happy,sad,angry,surprised}`，强度 1~3） |
| `intent` | `str` | `IDLE` / `NARRATING` / `THINKING` / `CONFUSED` / `EXCITED` / `SURPRISED` |

**执行流程**：

1. 解析每段 content 的 markers，输出 `SpeechSegment[]`。
2. 对每段调 `send_api.send_text()` 把**干净文本**发到聊天流（群里能看到字）。
3. 调 [`sub_agent.mark_reply_success`](../sub_agent.py)：标记下一 tick 的概率门加成 +0.5
   （刚回复完，下一条更可能继续）。
4. `performer.speaking_session(emotion, intent)` 包整个会话；段间共享一次 emotion / intent / hotkey 触发，避免段间打断 SpeechAnimator 状态机。
5. 多段 TTS 流水线合成（`max_parallel_segments` 并发）→ 顺序消费 → `audio_player.play_audio` 输出到 VB-Cable。
6. SpeechAnimator 在 session 内通过 `envelope_tracker` 拿音频包络驱动头部 / 身体律动。

**与 voice 模式的关键差异**：

- 文本会**额外**通过 `send_text` 发到群里——朗读 + 文字双轨。
- TTS 不走 `backend.emit`，直接给 `audio_player` / `VTSPerformer`。
- 触发 sub_agent 的"下一 tick 加成"机制。

## 提示词

[`VTB_SCENE_GUIDE`](../prompts/scenes.py) 告诉模型：

- 你的输出会**同时**出现在文本 / TTS / VTS 表演三个通道
- 群里所有人能看到字，**不要**装"只能听见的旁白"
- emotion 决定心情幅度（1~3 级），intent 决定头部姿态与眼神方向
- 高兴 → `happy:2` `EXCITED`；共情 → `sad:1` `NARRATING`；卡壳 → `neutral:1` `THINKING`

## VTS 集成

vtb 模式必须开 `[vts] enabled = true`，否则 plugin 启动时只会创建 AudioPlayer，
`VTSPerformer` 不会构造，`say_and_perform` 会降级为"仅本地播音频，不驱动形象"。

正常路径：

- [`VTSConnection`](../vts/connection.py) 通过 pyvts 建立 ws 长连，跑三条 loop（heartbeat / animation / param sender）
- [`VTSPerformer`](../vts/performer.py) 暴露 `speaking_session` / `play` / `perform` / `trigger_demo`
- [`AutoAnimator`](../vts/animation/auto.py) 待机时眨眼 / 呼吸 / 扫视 / 宏观动作
- [`SpeechAnimator`](../vts/animation/speech.py) 说话期 emotion 表情基准 + 音频包络驱动头部 / 身体

具体可调旋钮见 [`configuration.md`](configuration.md) 的 `[idle_animation]` /
`[audio_drive]` / `[vts] hotkey_map` 章节。

## 注意力过滤（vtb_attention）

群聊里直接每条都触发 LLM 既费 token 又会让你的 bot 显得"啥都接茬"。
anima_chatter 内置的 sub_agent 复刻了 dfc 的两层过滤：

1. **概率门**（`enable_programmatic_controller = true` 时）：基础概率 +
   命中名字 / 别名 / 未读条数 / 上一回合刚回复 加成。命中即直通。
2. **决策 LLM**：未命中时调 `sub_actor` 任务模型给出 `should_respond + reason`。

私聊场景一对一直通；多人群聊建议保持 `[vtb_attention] enabled = true`。

## 故障排查

| 现象 | 可能原因 | 处理 |
|------|------|------|
| `/vtb on` 后行为没变 | stream loop 没重启 | 查 `anima_chatter.commands.vtb` 日志；`_force_restart_loop` 异常会留 warning |
| 群里收到字但听不到声音 | VTS 没连上、VB-Cable 设备名不对 | 看 `anima_chatter.vts` 启动日志；检查 `[vts] audio_output_device` 是否能在 sounddevice 里找到 |
| VTS 形象动得僵硬 | `[idle_animation] motion_speed_scale` 太低 | 默认 2.0；调高让动作更利落 |
| 群聊每条都被回 | `[vtb_attention] enabled = false` 或 `enable_programmatic_controller = false` 在你的场景下不合适 | 按场景调；私聊建议关，群聊建议开 |
