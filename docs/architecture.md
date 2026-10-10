# 整体架构

`anima_chatter` 把"用 TTS 让 LLM 输出 + 让 VTube Studio 形象同步表演"封装成
一个仅面向 ``platform == "live"`` 的 Chatter。本文档解释直播会话、表演和排播的职责边界。

## 模块布局

```
anima_chatter/
├── plugin.py                 # 主入口：注册直播组件与初始化资源
├── config.py                 # 7 个 section 的 BaseConfig 类（含 max_backlog_seconds）
├── protocol.py               # 插件实例的类型安全视图
├── _internal_compat.py       # 框架内部模块的唯一兼容层（wake/wait_state/task/prompt）
│
├── chatter/                  # Chatter 实现 + NDFC 事件 seam
│   ├── core.py               #   主类：chat_core 各 adapter 协议（schema 注入带缓存）
│   ├── attention.py          #   注意力过滤（概率门 + sub_actor 决策）
│   ├── request_factory.py    #   LLM 请求构造
│   ├── session_bridge.py     #   与 chat_core 的结构化协议
│   └── ndfc_handlers.py      #   neo_default_chatter:* 事件转发（7 个 handler）
│
├── speech/                   # 语音合成与播放（唯一实现）
│   ├── markers.py            #   内联标记解析与 wait 分段
│   ├── backend.py            #   TTS request、PCM 结构协议与 service 查询
│   ├── streaming.py          #   回复 PCM 缓存与连续播放
│   └── playback.py           #   说话/唱歌 FIFO、任务清理、进度与背压
│
├── runtime/                  # 跨模块共享的运行时状态
│   ├── pipeline_state.py     #   回复轮次容量、实际起播与歌曲尾段
│   ├── sung_history.py       #   已唱歌曲历史
│   └── heartbeat.py          #   长阻塞期间喂 watchdog
│
├── actions/                  # say_and_perform / sing_song / pass_and_wait
├── audio/                    # 本地音频播放、响度归一化、包络提取
├── vts/                      # VTS 连接与动画
│   ├── connection.py         #   pyvts 长连 + worker 线程循环
│   ├── performer.py          #   ★表演器（contextvars 会话隔离 + 锁粒度=单段）
│   └── animation/            #   auto / speech / dynamics / noise
└── docs/
```

## 依赖方向

`plugin` 装配资源；`chatter` 负责会话、注意力与排播门；`actions` 编排参数；`speech` 负责合成与播放；`vts` 负责虚拟形象；`runtime` 只保存直播排播和歌曲状态。

本插件与 `anima_voice` 不相互导入。通话、ASR 接管、开始/结束事件与原 Chatter 恢复均由通话插件独立管理。两个插件只依赖现有 ChatCore 和 TTS 服务。

**所有直播 adapter 把 platform 统一写成 ``"live"``**（B 站 / 抖音合并到同一
chat_stream 串行决策），真实来源由 envelope 的 ``source_platform`` 携带。
新增直播平台只需 adapter 侧声明 ``platform = "live"``，无需改本插件。

## 语音提示词

SYSTEM 构建器通过公开 `prompt_api.get_template("tts_voice_plugin-neo.speech_rules")`
取得副本并异步渲染公共语音规则，每次构建重新获取，不在直播插件中复制读音、标点和语言切换文案。
TTS 插件必须已启用并加载；模板缺失会明确报错，不使用备用文案。
直播的 `say_and_perform`、停顿、表情与动作协议仍由本插件提供。

`AnimaChatter.speech_rules_template_name` 声明 SYSTEM 中使用的公共模板。
TTS 根据当前绑定 Chatter 跳过自己的同名规则提醒，后续 USER 模板构建也不会重复注入；
其他全局及流私有提醒仍沿原请求路径保留。

## 直播流水线

Action 返回已接收，物理播放按 FIFO 串行。实际起播、结束与取消事件决定回复轮次容量，估算时长仅用于拒排背压，不作为起播计划。

### 1. 回复轮次容量

```
真实 Actor 轮入口 → 容量登记 → 后台接收 → FIFO 设备输出
                                                 │ 首写成功
                                                 ▼
                           有新弹幕且无下一轮待播 → 最多提前一轮
```

轮次由 NDFC 的 `wait_user -> model_turn` 入口推进，同轮多个情绪 Action 共用容量。读取未读快照前等待门开放，但不推进轮次；follow-up 不额外 claim。无音频轮在返回等待时封存，不占住下一轮。

歌曲与说话共享 FIFO，不被普通弹幕自动打断。歌曲使用真实时长，从设备实际起播计算尾段；距结束 `song_prepare_lead_seconds` 时允许准备下一轮，开场停顿不计入歌曲尾段。没有百分比门、短音频阻塞分支、预计起播时间等待或固定跨轮静默。

### 2. 背压上限（拒排）

[`max_backlog_seconds`](../config.py)（默认 60s）：reserve 前检查队列积压，
超限拒排并返回明确反馈。当前轮歌曲不作为积压，下一轮排队歌曲受限。拒排不打开 PCM 流，也不提前发送文本或启动表演。

### 3. 表演会话与 FIFO

- 每次 `say_and_perform` 将 `content` 列表按换行合并；仅 `[wait:n]` 拆分 TTS 请求，连续 wait 累加，末尾 wait 保留。motion/emotion/list inline 标记只剥离，不触发分段或句中切换；顶层 emotion/intent 作用于整条回复。
- 各段 PCM 可在当前回复内完整缓存。Receiver 串行 FIFO，不设块数或秒数接收上限；当前段 EOF 并关闭 Provider 后立即请求下一段，不等待设备起播、播放或停顿。后端内部 `text_split_method` 与 `fragment_interval` 保留，不改变 V5 参数或 Provider 公开协议。
- 整条回复只调用一次 `AudioPlayer.play_pcm_stream`，一个声卡输出流；段间 wait 转换为 48000 Hz、单声道、s16le 定长静音，保留前置和尾部静音。首块成功设备写入后反馈整条回复文本并开始 VTS 会话，控制反馈不阻塞 feeder。
- PCM feeder 使用独立受管理 Task；文本反馈和 VTS 仍由 `speaking_session` 的原 Task 清理。说话和唱歌共享 FIFO 顺序门，取消排队项不会使后续项越过正在播放的项；PCM 与整轨、双轨共用播放器的同一把播放锁。

### 4. 直接消费 TTS service PCM 流

- [`streaming.py`](../speech/streaming.py) 通过串行 Receiver FIFO 调用 `tts_voice_plugin-neo:service:speech` 的 `open_pcm_stream()`。当前回复可完整缓存，不设 20 块、2 秒或其他接收上限；回复数量、轮次容量和 FIFO 顺序维持现有规则。
- 收到首块后立即开始该段接收与缓存，不等待短段边界或下一段预生成。每段 EOF 后关闭 Provider 上下文并释放锁，随即请求下一段，不等待设备起播、播放或 wait 停顿。
- 提供方持有 GPT-SoVITS HTTP 会话和权重锁；直播不创建第二套客户端。各段 PCM 与 wait 静音合并为整条回复的一次 `AudioPlayer.play_pcm_stream` 输入，由单个连续声卡输出流播放；任意块边界保留完整采样帧，空流、截断、格式或设备错误明确失败，不重试已部分输出的段。
- 首块成功设备写入后反馈整条清洗后的回复文本并开始 VTS 会话；控制反馈不阻塞 PCM feeder。长文本不按普通 WAV 的 `max_text_length` 静默截断，后端长度限制通过请求错误报告。普通 WAV 和通话路径保持不变。
- 增量包络仅处理已写出的 PCM；流式不做整段响度归一化，歌曲整轨处理保持独立。

### 5. 唤醒必达

门到点的唤醒使用 **注入 → 确认 → 重试**（0.5s × 10
次）：确认 stream 已脱离 Wait 或循环已推进；全部失败记 ERROR。冷场期唤醒
丢失不再靠"下一条弹幕"救场。

### 6. 资源所有权

直播持有自己的排播任务和 PCM writer；取消时关闭本次 service PCM 上下文，
等待输出线程结束，释放播放锁和排播占位，不关闭共享 TTS service。

### 7. 故障可感知

直播动作只报告已接收，后台错误有明确日志，
所有退出路径回收占位。任务在首次运行前被取消时也执行占位清理。

提交、请求开始、首 PCM、实际设备起播、网络 EOF 与播放完成分别记录耗时，不记录合成正文或连接凭据。V5 mode 2 仍先准备文本片段的语义内容，因此不能保证零首音延迟或推理慢于播放时绝无间断。

## 与 neo_default_chatter（NDFC）的关系

复用 NDFC 的 ``chat_core`` 主会话状态机，通过订阅 ``neo_default_chatter:*``
事件注入直播行为（见 [`chatter/ndfc_handlers.py`](../chatter/ndfc_handlers.py)）：
preprocess（注意力）、inject_unread_payload（prompt）、inject_usables（工具，
带指纹缓存）、create_request、fetch_unreads（流水线门）、format/history。

## 已知边界（待框架公开 API）

| 依赖 | 位置 | 状态 |
|------|------|------|
| 唤醒 Wait 状态 / 读取 wait 快照 | [`_internal_compat.wake_stream_from_wait` / `wait_state_snapshot`](../_internal_compat.py) | 访问 StreamLoopManager 私有结构，集中收敛；失败记 WARNING/ERROR |
| 喂 watchdog / task_manager | `_internal_compat` | kernel 公开入口，可直接 import |
| 人设 / prompt 渲染策略 | `_internal_compat` | `src.core` 内部入口，集中收敛 |

框架补齐对应公开 API 后仅需替换 `_internal_compat` 实现，其余代码不动。

## 数据与生命周期

插件身份与直播流 ID 保持不变，歌库和 VTS 授权缓存继续位于 `data/anima_chatter/`。卸载先取消并等待直播播放及清理任务，再清空排播状态并关闭 VTS，不读取或清理通话插件状态。完整音频辅助接口不参与直播动作，也不作为流式失败后的自动回退路径。
