# vtb_live 模式：直播间弹幕表演

`platform` 命中 [`LIVE_PLATFORMS`](../modes.py)（当前是 `bilibili_live`）时
**自动激活**，不需要 `/vtb on`。流由对应直播 adapter（如
[`bilibili_live_adapter`](../../bilibili_live_adapter/)）创建并入站。

## 触发条件

```python
# modes.resolve_mode 判定优先级
if platform == "local_asr":     return "voice"
if platform in LIVE_PLATFORMS:  return "vtb_live"  ← 直播流
return "vtb"
```

加新直播平台只需要在 [`modes.LIVE_PLATFORMS`](../modes.py) 加一行字符串，
**不需要改 chatter / action / prompt**。

## 与 vtb 模式的关键差异

| 维度 | vtb | vtb_live |
|------|-----|---------|
| 触发 | `/vtb on` 手动接管 | platform 自动判定 |
| 受众 | 群友 / 熟人 | 陌生观众，可能很多 |
| 输入特征 | 一两人慢慢对话 | 弹幕飘得快、多人并发 |
| 文本去向 | 通过 `send_text` 发回群里所有人都能看到字 | **不发回任何渠道**——观众只能听 TTS（B 站不允许第三方 bot 发弹幕）|
| 用户标识 | QQ 号（短数字） | `open_id`（20+ 字符脱敏 ID）|
| 提示词 | [`VTB_SCENE_GUIDE`](../prompts/scenes.py) | [`VTB_LIVE_SCENE_GUIDE`](../prompts/scenes.py)：直播间礼仪 + 节奏控制 + 长 ID 注意 |
| User prompt | [`USER_PROMPT_VTB`](../prompts/templates.py) | [`USER_PROMPT_VTB_LIVE`](../prompts/templates.py)：标题改"直播弹幕输入" |
| 注意力过滤 | 启用时跑概率门 + sub_actor | **强烈建议保持启用**——直播弹幕基数大，不过滤会让 bot 刷屏 |

## 表演链路（与 vtb 共用 say_and_perform）

vtb_live 复用 vtb 的 [`say_and_perform`](../actions/say_and_perform.py) action，
但**有一处隐含的关键差异**：底层 `send_api.send_text` 调到 B 站 adapter 时会被
[`BilibiliLiveAdapter._send_platform_message`](../../bilibili_live_adapter/plugin.py)
**默认丢弃**（B 站平台不允许 bot 发弹幕）。

也就是说：

- **vtb** 模式：文本进群 + TTS 朗读 + VTS 表演（三轨）
- **vtb_live** 模式：仅 TTS 朗读 + VTS 表演（两轨，文本只在 chatter context 里留个底）

模型必须意识到这点——所以 [`VTB_LIVE_SCENE_GUIDE`](../prompts/scenes.py) 反复强调
"观众只能听到声音，不要说'刚才那位说……'之类需要看回引用的措辞"。

## 提示词重点

[`VTB_LIVE_SCENE_GUIDE`](../prompts/scenes.py) 比 vtb 多了几段直播专属内容：

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

vtb_live 共用 [`sub_agent.py`](../sub_agent.py)，行为和 vtb 一致，但配置上
建议：

- `[vtb_attention] enabled = true`：直播弹幕基数大，**必开**。
- `[vtb_attention] enable_programmatic_controller`：
  - `true`：先按本地概率规则放行（基础 0.1 + @名字 +0.7 + 别名 +0.4 + 上一轮刚回复 +0.5 等），命中即直通；不命中再交 sub_actor LLM。
  - `false`：所有判定都走 sub_actor LLM，token 消耗高但更可控。

## 平台 envelope 字段映射

[`bilibili_live_adapter/src/dispatcher.py`](../../bilibili_live_adapter/src/dispatcher.py)
从 B 站 `LIVE_OPEN_PLATFORM_DM` 弹幕 cmd 翻译成 envelope，关键字段：

| envelope 字段 | 来源 | 含义 |
|------|------|------|
| `message_info.platform` | 常量 | `"bilibili_live"`（modes 判定的依据） |
| `message_info.user_info.user_id` | `data.open_id` | 跨直播间稳定的脱敏 ID |
| `message_info.user_info.user_nickname` | `data.uname` | 弹幕昵称 |
| `message_info.user_info.role` | `OPERATOR` 或 `MEMBER` | 舰长 / 提督 / 总督 → `OPERATOR` |
| `message_info.group_info.group_id` | `data.room_id` | 直播间号当群 ID |
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
| 模式没切到 vtb_live，走了 vtb | `LIVE_PLATFORMS` 没含目标平台 / dispatcher 写错 platform | 检查 [`modes.LIVE_PLATFORMS`](../modes.py) 和 adapter 的 `platform` 类属性是否一致 |
| 弹幕进来但 chatter 不响应 | 注意力过滤丢了；或 LLM 决策"不必响应" | 看 `anima_chatter.runner` 日志的 `sub-agent 跳过响应 reason=...` |
| 模型试图发"@张三 你好"这种弹幕回引用 | 没注意 vtb_live 文本不出 | scene_guide 第三段已经强调"直接复述弹幕内容"，但还可以在 personality 里加一条 |
| 记忆工具写入但找不回 | 模型把 `open_id` 拼短了 | 检查 booku 记录里的 `person_id` 是否完整 23+ 字符；scene_guide 已强调"一字不差" |
| 直播开播但收不到弹幕 | B 站 adapter 长连断了 / id_code 过期 | 看 [`bilibili_live_adapter`](../../bilibili_live_adapter/) 的 README 故障排查 |
