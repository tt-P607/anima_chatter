# 直播间弹幕表演

所有直播 adapter 统一声明 `platform = "live"`，本插件自动处理直播流。真实来源由 `source_platform` 携带；普通私聊、群聊和本地 ASR 不由本插件接管。

## 触发条件

Chatter 与表演、唱歌动作均只接受 `platform == "live"`。新增直播来源沿用现有 adapter 的平台和统一虚拟群组映射，无需增加模式分支。

## 表演链路

[say_and_perform](../actions/say_and_perform.py) 负责本机朗读与 VTS 表演；[sing_song](../actions/sing_song.py) 使用本地歌库。直播回复不发送弹幕，文本仅保留在会话上下文中。场景描述由 [scenes.py](../prompts/scenes.py) 按实际来源构建。

## 排播与反馈

语音和歌曲统一按 Action 调用顺序后台播放，成功结果表示已接收。当前语音实际起播后，有新弹幕时最多提前准备一轮；一轮可包含多个情绪动作，不能继续堆积后续轮。

每次 `say_and_perform` 将完整 `content` 列表按换行合并。仅 `[wait:n]` 拆分 TTS 请求，连续 wait 累加，末尾 wait 保留；motion/emotion/list inline 标记只剥离，不分段或句中切换表演，顶层 emotion/intent 作用于整条回复。段间 wait 转成 48000 Hz 单声道 s16le 定长静音，前置和尾部静音保留。

当前回复可全量缓存，Receiver 串行 FIFO 且不设块数或秒数接收上限。每段 PCM EOF 并关闭 Provider 后立即请求下一段，不等待声卡起播、播放或 wait 停顿；首块成功设备写入后反馈整条清洗后的回复文本并开始 VTS 会话，控制反馈不阻塞 PCM feeder。整条回复只使用一次 `AudioPlayer.play_pcm_stream` 和一个声卡输出流。拒排、空音频、设备首写失败或未起播取消不会提前产生文本和表演。长文本不会按普通 WAV 的 `max_text_length` 静默截断，后端真实长度限制以请求错误报告；普通 WAV 与通话路径不变。

TTS 插件内部文本分段参数 `text_split_method` 与 `fragment_interval` 保留，不由直播 PCM 路径改写。上游生成慢于播放时仍可能停顿；可通过请求、首 PCM、起播、EOF 和播放结束日志区分推理延迟与设备排队。

## 提示词重点

[直播场景文案](../prompts/scenes.py) 说明直播礼仪、来源、节奏和记忆标识：

### 弹幕节奏与回应策略

- 弹幕飘得快是常态，**没必要每条都回**。
- 优先级：明确@/问问题/请求 > 有趣话题 > 舰长发言 > 多条同主题合并回复。
- 调 `pass_and_wait` 让自己沉默几秒在直播里很正常，比硬找话说更自然。
- 不要点名感谢每位发言者，不要刷屏式回应。

### 直播间礼仪

- 称呼用"大家"/"各位"而不是具体昵称。
- 新人友好，话题切换交代上下文。
- 梗 / 颜文字读不顺时委婉表达"看不懂"，不要硬念。
- 回避政治 / 宗教 / 地域 / 未成年充值诱导 / 平台敏感词。
- 攻击性弹幕礼貌带过或忽略，不正面对线。

### emotion / intent 调整

- **直播场景慎用 `angry`**——除非话题真的需要"不满"，平时哪怕弹幕不友好也用 `neutral:1` / `sad:1` 带过。
- 直播常用搭配：礼节性回应舰长 `happy:2 NARRATING`、看到精彩弹幕 `happy:2 EXCITED`、
  弹幕在问难题 `neutral:1 THINKING`、看不懂符号 `neutral:1 CONFUSED`。

### 长 ID 提醒（与 booku 等记忆插件协同）

```
你在的是 B 站直播间，对话方是直播间观众。如果你有记忆类工具（如 booku），
里面要求的 platform:id 形式：本平台是 bilibili_live，id 是观众的 open_id
（弹幕行 [xxx] 里的整串）。

open_id 比 QQ 号长得多（20+ 字符），调记忆工具时严格按弹幕行的 [xxx]
一字不差地抄过去，不要省略、缩写或替换。拼错一个字记忆就找不回。
```

记忆**策略**（什么时候建 person 记忆、怎么打 tag）由记忆插件本身的提示词管，
anima_chatter 这边只负责把环境描述清楚。

## sub_agent / 注意力过滤

注意力由 [attention.py](../chatter/attention.py) 与 [sub_agent.py](../prompts/sub_agent.py) 管理：

- `[vtb_attention] enabled = true`：直播弹幕基数大，**必开**。
- `[vtb_attention] enable_programmatic_controller`：
  - `true`：先按本地概率规则放行（基础 0.1 + @名字 +0.7 + 别名 +0.4 + 上一轮刚回复 +0.5 等），命中即直通；不命中再交 sub_actor LLM。
  - `false`：所有判定都走 sub_actor LLM，token 消耗高但更可控。

## 平台 envelope 字段映射

直播适配器将弹幕翻译为 envelope；真实平台和房间信息保留在来源元数据中。
常用字段如下，来源特有字段由对应适配器提供：

| envelope 字段 | 来源 | 含义 |
|------|------|------|
| `message_info.platform` | 常量 | `"live"`，用于直播 Chatter 选择 |
| `message_info.user_info.user_id` | `data.open_id` | 跨直播间稳定的脱敏 ID |
| `message_info.user_info.user_nickname` | `data.uname` | 弹幕昵称 |
| `message_info.user_info.role` | `OPERATOR` 或 `MEMBER` | 舰长 / 提督 / 总督 → `OPERATOR` |
| `message_info.group_info.group_id` | adapter 统一映射 | 直播会话虚拟群组 ID |
| `message_info.group_info.group_name` | `start_app.anchor_uname` | 主播昵称当"群名" |
| `message_info.additional_config.bilibili_uid` | `data.uid` | 原始 uid 备查 |
| `message_info.additional_config.guard_level` | `data.guard_level` | 0=非舰长 / 1=总督 / 2=提督 / 3=舰长 |
| `message_info.additional_config.fans_medal_level` | `data.fans_medal_level` | 粉丝勋章等级 |
| `message_info.additional_config.fans_medal_name` | `data.fans_medal_name` | 粉丝勋章名 |

模型在 prompt 里看到的弹幕渲染（来自 [`BaseChatter.format_message_line`](../../../src/core/components/base/chatter.py)）：

```
【19:47】<管理员> [open_id_xxxxx] 观众甲 [mid_xxx]： 主播好可爱
```

`<管理员>` 是 OPERATOR 角色的中文显示——舰长就在这个位置体现。

## 故障排查

| 现象 | 可能原因 | 处理 |
|------|------|------|
| 直播流未选中本插件 | adapter 平台或 Chatter 绑定不匹配 | 检查 platform 是否为 `live`，不要迁移已有虚拟群组和流 ID |
| 弹幕进来但 chatter 不响应 | 注意力过滤丢了；或 LLM 决策"不必响应" | 看 `anima_chatter.runner` 日志的 `sub-agent 跳过响应 reason=...` |
| 模型试图发"@张三 你好"这种弹幕回引用 | 没注意 vtb_live 文本不出 | scene_guide 第三段已经强调"直接复述弹幕内容"，但还可以在 personality 里加一条 |
| 记忆工具写入但找不回 | 模型把 `open_id` 拼短了 | 检查 booku 记录里的 `person_id` 是否完整 23+ 字符；scene_guide 已强调"一字不差" |
| 直播开播但收不到弹幕 | B 站 adapter 长连断了 / id_code 过期 | 看 [`bilibili_live_adapter`](../../bilibili_live_adapter/) 的 README 故障排查 |
