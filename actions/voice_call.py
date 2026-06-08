"""anima_chatter 的语音通话发起 / 结束动作。

提供 ``start_voice_call`` 和 ``end_voice_call`` 两个 action，让模型能够在
私聊中主动接 / 挂电话：

::

    QQ 私聊（default_chatter / kokoro_flow_chatter）
        │
        │  模型决策："想跟用户语音聊"
        │  → 调用 start_voice_call action
        │
        ▼
    本插件接管该 stream + asr_adapter_anima 转发到 QQ stream
        │
        │  期间：模型 say -> TTS 本地播放
        │        用户说话 -> ASR -> 注入 QQ stream unread
        │        用户也可以打字（与说话等价）
        │
        │  模型 / 用户 / 5min 超时 → end_voice_call
        ▼
    清状态 + 释放接管 + 广播 voice_call.ended
        │
        │  原 chatter（如 kfc）天然回归
        │  收到 voice_call.ended 事件 → 把通话期间消息补到 chain_payloads
        ▼
    历史无缝衔接

约束（来自设计文档第 9 节）：
- 仅私聊（chat_type == PRIVATE）才允许调用——群聊不适合一对一通话。
- 同时只能有一个通话进行（call_state 互斥保证）。
- 通话期间整条 QQ stream 的 chatter 都被切到 anima_chatter（voice 模式）；
  default_chatter / kfc 不会被触发，天然不需要"静默"处理。

公共函数（``finalize_call`` / ``_play_farewell_via_tts`` / ASR session 调用 /
事件名常量）全部位于 [`voice_call_lifecycle.py`](../voice_call_lifecycle.py:1)；
本文件只保留 action 类。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api import chat_api, event_api, send_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import ChatType
from src.core.components.base.action import BaseAction

from .. import call_state
from .._internal_compat import build_notice_message
from ..constants import CHATTER_SIGNATURE as _VOICE_CHATTER_SIGNATURE
from ..voice_call_lifecycle import (
    EVENT_VOICE_CALL_ENDED,
    EVENT_VOICE_CALL_STARTED,
    _end_asr_voice_session,
    _play_farewell_via_tts,
    _resolve_caller_identity,
    _restart_stream_loop,
    _start_asr_voice_session,
    finalize_call,
)


logger = get_logger("anima_chatter.action.voice_call")


# 兼容别名：早期外部模块按 ``from .actions.voice_call import _finalize_call``
# 使用本符号；统一搬到 ``voice_call_lifecycle.finalize_call`` 后保留别名直至
# 所有 import 点迁移完毕。新代码请直接 import voice_call_lifecycle.finalize_call。
_finalize_call = finalize_call


class StartVoiceCallAction(BaseAction):
    """发起一次本地语音通话。

    模型在 QQ 私聊里"想跟用户语音聊"时调用本动作，会做四件事：

    1. 校验：仅私聊 + 当前没有进行中通话。
    2. 设状态：通过 :mod:`plugins.anima_chatter.call_state` 占用通话槽位。
    3. 接管 stream：把当前 stream 的活跃 chatter 切成 anima_chatter，
       重启循环让下一 tick 走 anima_chatter。
    4. 启动 ASR 转发：让本地麦克风识别出的文本注入到当前 QQ stream。
    5. 给用户发"接通中..."的文本提示 + 广播 ``voice_call.started`` 事件。
    """

    action_name = "start_voice_call"
    associated_types = ["text"]
    action_description = (
        "在当前**私聊**里发起一次本地语音通话。"
        "调用后：你的回复会被 TTS 通过本机扬声器播放出来（不再以文字形式发到当前对话），"
        "用户用麦克风说的话会被识别为文字进入当前聊天，仿佛你们正在打电话。"
        "适用场景：你和用户私聊里聊得正起劲、对方暗示想语音、或者你主动想换种交流方式。"
        "限制：只能在私聊使用（群聊不会暴露此 action）；同时只允许一个通话进行；"
        "超过 5 分钟自动挂断；通话期间用户的文字消息也会被 TTS 念出来。"
    )
    chatter_allow: list[str] = []  # 任何 chatter（如 kokoro / default）都可调用
    chat_type = ChatType.PRIVATE  # 仅私聊
    primary_action = False

    async def go_activate(self) -> bool:
        """私聊 + 当前没有通话进行中时才暴露。"""

        # 已经在通话中：模型已经身处 voice 模式，不需要再启动；隐藏即可。
        if await call_state.is_call_active_for_stream(self.chat_stream.stream_id):
            return False
        # 已有别的通话占用：也隐藏（避免模型误调，规则也清晰）。
        if await call_state.get_active_call() is not None:
            return False
        return True

    async def execute(
        self,
        reason: Annotated[
            str,
            "为什么要开通话？例如 '想直接听到你说话'、'用文字解释太麻烦了，电话里说更快'。"
            "这段会被发给用户作为接通提示，用第一人称、口语化、一两句话。",
        ] = "",
    ) -> tuple[bool, str]:
        """开启语音通话。"""

        stream_id = self.chat_stream.stream_id
        platform = self.chat_stream.platform or ""

        # ── 1) 互斥校验（go_activate 已过滤但仍要兜底） ──
        if await call_state.get_active_call() is not None:
            return False, "已有进行中的通话，无法同时开启第二个"

        # 记录原 chatter 的签名（如果有），让 ended 事件能告诉 kfc 等
        # "通话前接管这个 stream 的是不是你"。default_chatter 评分绑定时
        # 通常没有"活跃 chatter"——返回 None，那就留空字符串。
        existing = chat_api.get_chatter_by_stream(stream_id)
        previous_signature = ""
        if existing is not None:
            try:
                previous_signature = existing.__class__.get_signature() or ""
            except Exception:
                previous_signature = ""

        # ── 2) 占用通话槽位 + 写入"通话开始"系统标注 ─────────────
        try:
            active = await call_state.set_active_call(
                stream_id,
                previous_chatter_signature=previous_signature,
            )
        except RuntimeError as exc:
            return False, f"启动通话失败：{exc}"

        # ── 2.0) 提前反查通话发起方身份（必须在写入 NOTICE 边界前做） ──
        # 为什么要先做：``_resolve_caller_identity`` 倒序扫 history 找最近的
        # 非 bot 消息。如果先把 NOTICE 边界写进 history（sender_id="system"），
        # 即便加了 message_type 过滤，仍存在过滤不彻底的边角情况。
        # 把反查放在 NOTICE 写入之前，从源头消除这种风险。
        if not platform:
            await call_state.clear_active_call()
            return False, "当前流缺少 platform 信息，无法启动语音通话"

        target_user_id, target_user_name = _resolve_caller_identity(self.chat_stream)
        if not target_user_id:
            await call_state.clear_active_call()
            return False, (
                "无法确定通话发起方在该平台上的真实 ID（最近无对方消息记录）。"
                "请等对方先在私聊中发一条消息后再重试。"
            )

        # ── 2.5) 在 messages_in_call 第一条插入系统注解，作为通话上下文的
        # 边界标记。这条会随事件 payload 透传给 kfc handler，让 kfc 在重组
        # chain_payloads 时知道"这一段是发生在电话里的"，并且能看到具体的
        # 通话开始时间。
        import datetime as _datetime

        started_human = _datetime.datetime.fromtimestamp(active.started_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        note_text = (
            f"[语音通话开始 @ {started_human}] 用户已接通本地语音通话。"
            "从此处到下一条 [语音通话结束] 之间的对话发生在电话里——"
            "你的回复以 TTS 通过本机扬声器播放，用户的话来自麦克风 ASR 识别"
            "（可能有错字），双方看不到文字。"
        )
        await call_state.record_system_note(stream_id, note_text)

        # ── 2.6) 注入 history_messages 边界（给 DFC 等无状态 chatter 看） ──
        # 构造一条 NOTICE 类型的 Message 注入到框架通用历史中。
        # 这样 DFC 切回来时能看到明确的通话边界，不必改 DFC 源码。
        history_msg = build_notice_message(
            message_id=f"call_start_{active.started_at}",
            content=note_text,
            platform=platform,
            stream_id=stream_id,
            time=active.started_at,
        )
        self.chat_stream.context.add_history_message(history_msg)

        # ── 3) 启动 ASR 通话会话（启动 runtime + 切 always_on + 设 redirect） ──

        if not await _start_asr_voice_session(
            platform,
            stream_id,
            user_id=target_user_id,
            user_name=target_user_name or target_user_id,
        ):
            await call_state.clear_active_call()
            return False, "启动 ASR 通话会话失败（asr_adapter 未加载或启动失败？）"

        # ── 4) 接管 stream ───────────────
        chatter_cls = chat_api.get_chatter_class(_VOICE_CHATTER_SIGNATURE)
        if chatter_cls is None:
            # 找不到 anima_chatter 组件：把已设置的副作用全部回滚。
            await _end_asr_voice_session()
            await call_state.clear_active_call()
            return False, "未找到 anima_chatter 组件，无法接管"

        instance = chatter_cls(stream_id=stream_id, plugin=self.plugin)
        chat_api.bind_chatter_for_stream(stream_id, instance)
        await _restart_stream_loop(stream_id)

        # ── 5) 接通提示 ─────────────────
        # 通话语境下：开场白也是电话里的第一句话，应该 TTS 播放出来，不发回 QQ。
        # 但接通提示稍特殊——bot 主动开通话时希望对方"先看到一条文字提醒
        # （比如"我打给你吧"）"知道接到电话了。所以：**保留发送一条 QQ 文字
        # 提示**，但**同时也用 TTS 把它念出来**，当作通话第一句话。
        prompt = reason.strip() or "我打给你吧，咱们语音聊？"
        # 接通提示也应入档：append 到 messages_in_call 里，最终 ended 事件
        # 透传到 kfc，让对话历史保留"我说我打给你吧 → 通话开始"的连贯性。
        await call_state.record_assistant_message(stream_id, prompt)
        try:
            await send_api.send_text(
                content=prompt,
                stream_id=stream_id,
                platform=platform,
            )
        except Exception as exc:
            logger.warning(f"发送接通提示失败 stream={stream_id}: {exc}")
        # 同步 TTS 播放——让对方在 QQ 看到字 + 听到声音，与"接电话"体验一致。
        await _play_farewell_via_tts(self.plugin, prompt, stream_id)

        # ── 6) 广播事件 ──────────────────
        try:
            await event_api.publish_event(
                EVENT_VOICE_CALL_STARTED,
                {
                    "caller_stream_id": stream_id,
                    "started_at": active.started_at,
                    "previous_chatter_signature": previous_signature,
                },
            )
        except Exception as exc:
            logger.warning(f"广播 voice_call.started 失败: {exc}")

        logger.info(
            f"语音通话已启动 stream={stream_id} prev_chatter={previous_signature!r} "
            f"timeout={active.timeout_seconds}s"
        )
        return True, "通话已开启，对方可以开始说话了"


class EndVoiceCallAction(BaseAction):
    """挂断当前语音通话并广播结束事件。"""

    action_name = "end_voice_call"
    associated_types = ["text"]
    action_description = (
        "挂断当前语音通话并切回正常聊天。"
        "调用场景：你和用户已经说完想说的话、用户说要挂断、或者你判断没有继续语音的必要了。"
        "调用后：anima_chatter 释放对当前 stream 的接管，下一轮自动绑回原 chatter "
        "（default_chatter / kokoro_flow_chatter 等），通话期间产生的消息会通过事件机制"
        "补回原 chatter 的对话历史，保证上下文不丢。"
    )
    chatter_allow = ["anima_chatter"]
    chat_type = ChatType.PRIVATE
    primary_action = False

    async def go_activate(self) -> bool:
        """只在该 stream 正处于通话中时暴露。"""

        return await call_state.is_call_active_for_stream(self.chat_stream.stream_id)

    async def execute(
        self,
        farewell: Annotated[
            str,
            "挂断时给用户的告别话。例如 '那先这样吧，回头见'、'我得忙别的去了'。"
            "用第一人称、口语化、一两句话；不必煽情。",
        ] = "",
    ) -> tuple[bool, str]:
        """挂断通话。"""

        return await finalize_call(
            stream_id=self.chat_stream.stream_id,
            platform=self.chat_stream.platform or "",
            farewell=farewell.strip(),
            end_reason="model",
            plugin=self.plugin,
        )


__all__ = [
    "EVENT_VOICE_CALL_ENDED",
    "EVENT_VOICE_CALL_STARTED",
    "EndVoiceCallAction",
    "StartVoiceCallAction",
    "_finalize_call",
    "finalize_call",
]
