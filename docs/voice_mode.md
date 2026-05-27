# voice 模式：实时 ASR 语音通话

`platform == "local_asr"` 时自动激活；和 [`asr_adapter_anima`](../../asr_adapter_anima/) +
[`tts_http_server`](../../tts_http_server/) 配合做"听用户说话 → LLM → TTS 朗读"
的实时一对一对话。

## 触发条件

`local_asr` 平台的流由 [`asr_adapter_anima`](../../asr_adapter_anima/) 创建。流绑定后框架会
按 [`AnimaChatter.associated_platforms = ["local_asr"]`](../plugin.py)
评分把 chatter 绑到 anima_chatter，**不需要任何手动接管命令**。

模式判定见 [`modes.resolve_mode`](../modes.py)：

```python
if platform == "local_asr":
    return "voice"
```

## 运行特征

| 维度 | 设定 | 与 vtb / vtb_live 不同的地方 |
|------|------|-----|
| ``stream_tick_interval`` | **强制 0.1** | 通话需要紧的 tick 间隔保证响应速度；plugin section 的 `tick_interval` 字段被忽略 |
| ``allow_message_buffer`` | **强制 False** | 通话不能凑批，每个 ASR 段都得立即处理；plugin section 的同名字段被忽略 |
| 注意力过滤 | ✗ 跳过 | 一对一通话不需要 sub_agent 概率门 |
| Action 暴露 | ``say`` + ``pass_and_wait`` | ``say_and_perform`` 通过 ``go_activate()`` 排除 |
| 提示词 | [`USER_PROMPT_VOICE`](../prompts/templates.py) + [`VOICE_SCENE_GUIDE`](../prompts/scenes.py) | 强调"ASR 识别可能出错"、"输出适合 TTS 朗读" |

具体覆写逻辑见 [`AnimaChatter.apply_stream_runtime_options`](../plugin.py)。

## 核心 Action：`say`

签名：`anima_chatter:action:say`，定义于 [`actions/say.py`](../actions/say.py)。

**`go_activate`**：仅 `platform == "local_asr"` 激活，确保 vtb / vtb_live 看不到。

**输入参数**：

| 参数 | 类型 | 描述 |
|------|------|------|
| `content` | `str` | 要说的内容；可包含 `[wait:n]` / `[emotion:xxx]...[/emotion]` 标记 |

**执行流程**：

1. [`markers.parse_speech_segments`](../markers.py) 按句切分 + 标记解析，输出 `SpeechSegment[]`。
2. 多个 segment 并行调 `HttpTTSBackend.synthesize`，最大并发 = `tts.max_parallel_segments`。
3. 按原始顺序消费合成结果（`as_completed` + 顺序门控）。
4. 每段完成后调 `backend.emit(artifact, chat_stream)`——把 voice 消息送回适配器，由
   ASR adapter 那一侧负责本地播放。
5. 段间如果 segment 上有 `wait_before > 0.1` 会 `asyncio.sleep`。

**与 vtb 系的关键差异**：voice 模式 **不调用** `audio_player.play_audio`——音频
是通过 ``backend.emit`` 包装成 voice ``Message`` 推回 ``message_sender``，由
``asr_adapter`` 在它那边的链路播放。

## `pass_and_wait`

签名：`anima_chatter:action:pass_and_wait`，三模式共用。

| 参数 | 含义 |
|------|------|
| `seconds=None` | 等待新的用户输入（无限期） |
| `seconds=N` | 到时主动恢复 |

通话场景里"说完一句等用户继续说话"的标准动作。

## 提示词

[`VOICE_SCENE_GUIDE`](../prompts/scenes.py) 在 `<scene_and_protocol>` 段告诉模型：

- 输入来自 ASR，可能有错字 / 漏字 / 半句话
- 不要因为一两个识别错误就机械纠正对方
- 回复会被 TTS 播放，要适合朗读（短句 / 口语化 / 避免 Markdown / 复杂括号）
- 必须用 `say` action 输出；说完调 `pass_and_wait`

## TTS 标记

`say.content` 支持两类内联标记，见 [`markers.py`](../markers.py)：

| 标记 | 作用 |
|------|------|
| `[wait:n]` | 下一段语音播放前等待 n 秒（仅影响 TTS 段间停顿，不影响 chatter 等待） |
| `[emotion:happy]...[/emotion]` | 给该段标记 emotion，传给 TTS 后端供选 voice/style |

通常 `0.3` 秒已经能营造换气感；普通说话不要插 `[wait]`，会显得卡顿。

## 故障排查

| 现象 | 可能原因 | 处理 |
|------|------|------|
| 模型直接输出文本，没调 say | LLM 不熟悉协议，且 `plain_text_retry_limit` 太小 | 调大 `[plugin] plain_text_retry_limit`（默认 1）|
| TTS 返回空音频，画面安静 | 文本里全是无法朗读的标记 / 标点 | 看 `[tts] empty_audio_retry_count` 是否生效；查 `anima_chatter.tts` 日志的 retry 信息 |
| ASR 输入丢字 / 错字严重 | sherpa-onnx 模型选小了 | 是 ASR 的事，不是 anima_chatter 的；去 `asr_adapter_anima` 配置换更大模型 |
| 回复"听到一半就被截断" | TTS 服务端把长文本切短了 | 调小段拆分（`tts.sentence_split_enabled = true`）让每段单独合成 |
