"""语音通话的发起与挂断动作。

让模型能在私聊中主动接 / 挂电话::

    平台私聊（default_chatter / 其他 chatter）
        │  模型决策："想跟用户语音聊" → start_voice_call
        ▼
    anima_chatter 接管该 stream + ASR 转发到原 stream
        │  期间：模型 say → TTS 本地播放
        │        用户说话 → ASR → 注入原 stream 未读
        │  模型 / 用户 / 静默超时 → end_voice_call
        ▼
    清状态 + 释放接管 + 广播 voice_call.ended
        │  原 chatter 天然回归，订阅方把通话历史补回自己的对话链
        ▼
    历史无缝衔接

约束：仅私聊可用（群聊不适合一对一通话）；同时只能有一个通话进行；通话期间
整条 stream 的 chatter 都切到 anima_chatter，其他 chatter 不会被触发。

生命周期编排在 [`voice_call/lifecycle.py`](../voice_call/lifecycle.py:1)，本
文件只保留 action 类。
"""

from __future__ import annotations

import datetime
from typing import Annotated

from src.app.plugin_system.api import chat_api, event_api, send_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction
from src.app.plugin_system.types import ChatType

from .._internal_compat import build_notice_message
from ..constants import CHATTER_SIGNATURE
from ..protocol import require_plugin
from ..runtime import call_state
from ..voice_call import (
    EVENT_VOICE_CALL_STARTED,
    end_asr_voice_session,
    finalize_call,
    play_via_tts,
    resolve_caller_identity,
    restart_stream_loop_safely,
    start_asr_voice_session,
)


logger = get_logger("anima_chatter.action.voice_call")


_DEFAULT_CALL_PROMPT = "我打给你吧，咱们语音聊？"


def _build_start_note(started_at: float) -> str:
    """构造"通话开始"系统标注文案。

    Args:
        started_at: 通话开始的 Unix 时间戳。

    Returns:
        标注正文。
    """

    started_human = datetime.datetime.fromtimestamp(started_at).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return (
        f"[语音通话开始 @ {started_human}] 用户已接通本地语音通话。"
        "从此处到下一条 [语音通话结束] 之间的对话发生在电话里——"
        "你的回复以 TTS 通过本机扬声器播放，用户的话来自麦克风 ASR 识别"
        "（可能有错字），双方看不到文字。"
    )


class StartVoiceCallAction(BaseAction):
    """在当前私聊中发起一次本地语音通话。"""

    name = "start_voice_call"
    associated_types = ["text"]
    description = (
        "在当前**私聊**里发起一次本地语音通话。"
        "调用后：你的回复会被 TTS 通过本机扬声器播放出来（不再以文字形式发到当前对话），"
        "用户用麦克风说的话会被识别为文字进入当前聊天，仿佛你们正在打电话。"
        "适用场景：你和用户私聊里聊得正起劲、对方暗示想语音、或者你主动想换种交流方式。"
        "限制：只能在私聊使用（群聊不会暴露此 action）；同时只允许一个通话进行；"
        "双方静默超过 5 分钟自动挂断；通话期间用户的文字消息也会被 TTS 念出来。"
    )
    chatter_allow: list[str] = []  # 任何 chatter 都可调用
    chat_type = ChatType.PRIVATE
    primary_action = False

    async def go_activate(self) -> bool:
        """私聊且当前没有通话进行中时才暴露。

        Returns:
            是否对模型可见。
        """

        return await call_state.get_active_call() is None

    async def execute(
        self,
        reason: Annotated[
            str,
            "为什么要开通话？例如 '想直接听到你说话'、'用文字解释太麻烦了，电话里说更快'。"
            "这段会被发给用户作为接通提示，用第一人称、口语化、一两句话。",
        ] = "",
    ) -> tuple[bool, str]:
        """开启语音通话。

        执行顺序：校验互斥 → 反查对方身份 → 占用通话槽位 → 写入开始标注 →
        启动 ASR 会话 → 接管 stream → 播放接通提示 → 广播事件。任一步失败都会
        回滚已产生的副作用。

        Args:
            reason: 接通提示文本。

        Returns:
            ``(是否成功, 结果描述)``。
        """

        stream_id = self.chat_stream.stream_id
        platform = (self.chat_stream.platform or "").strip()
        if not platform:
            return False, "当前流缺少 platform 信息，无法启动语音通话"

        if await call_state.get_active_call() is not None:
            return False, "已有进行中的通话，无法同时开启第二个"

        # 必须在写入 NOTICE 边界**之前**反查——否则倒序扫描会先看到自己写的标注。
        target_user_id, target_user_name = resolve_caller_identity(self.chat_stream)
        if not target_user_id:
            return False, (
                "无法确定通话发起方在该平台上的真实 ID（最近无对方消息记录）。"
                "请等对方先在私聊中发一条消息后再重试。"
            )

        previous_signature = self._current_chatter_signature(stream_id)

        try:
            active = await call_state.set_active_call(
                stream_id, previous_chatter_signature=previous_signature
            )
        except RuntimeError as exc:
            return False, f"启动通话失败：{exc}"

        note_text = _build_start_note(active.started_at)
        await call_state.record_system_note(stream_id, note_text)
        self.chat_stream.context.add_history_message(
            build_notice_message(
                message_id=f"call_start_{active.started_at}",
                content=note_text,
                platform=platform,
                stream_id=stream_id,
                time=active.started_at,
            )
        )

        if not await start_asr_voice_session(
            platform,
            stream_id,
            user_id=target_user_id,
            user_name=target_user_name,
        ):
            await call_state.clear_active_call()
            return False, "启动 ASR 通话会话失败（asr_adapter_anima 未加载或启动失败？）"

        chatter_cls = chat_api.get_chatter_class(CHATTER_SIGNATURE)
        if chatter_cls is None:
            await end_asr_voice_session()
            await call_state.clear_active_call()
            return False, "未找到 anima_chatter 组件，无法接管"

        chat_api.bind_chatter_for_stream(
            stream_id, chatter_cls(stream_id=stream_id, plugin=self.plugin)
        )
        await restart_stream_loop_safely(stream_id)

        await self._send_call_prompt(reason, stream_id, platform)
        await self._publish_started_event(active, previous_signature)

        logger.info(
            f"语音通话已启动 stream={stream_id} prev_chatter={previous_signature!r} "
            f"timeout={active.timeout_seconds}s"
        )
        return True, "通话已开启，对方可以开始说话了"

    @staticmethod
    def _current_chatter_signature(stream_id: str) -> str:
        """读取当前接管该 stream 的 chatter 签名。

        Args:
            stream_id: 目标聊天流 ID。

        Returns:
            chatter 组件签名；没有活跃 chatter（如按评分自动绑定）时返回空串。
        """

        existing = chat_api.get_chatter_by_stream(stream_id)
        if existing is None:
            return ""
        return existing.get_signature() or ""

    async def _send_call_prompt(
        self,
        reason: str,
        stream_id: str,
        platform: str,
    ) -> None:
        """发送接通提示并同步 TTS 播放。

        接通提示比较特殊——既要让对方在聊天里**看到**一条文字提醒（知道接到电话
        了），也要作为通话第一句话**听到**。之后的对话就只有声音了。

        Args:
            reason: 模型给的提示文本；空串时用默认文案。
            stream_id: 目标聊天流 ID。
            platform: 平台标识。
        """

        prompt = reason.strip() or _DEFAULT_CALL_PROMPT
        await call_state.record_assistant_message(stream_id, prompt)
        sent = await send_api.send_text(
            content=prompt, stream_id=stream_id, platform=platform
        )
        if not sent:
            logger.warning(f"发送接通提示失败 stream={stream_id}")
        await play_via_tts(require_plugin(self.plugin), prompt, stream_id)

    @staticmethod
    async def _publish_started_event(
        active: call_state.ActiveCall,
        previous_signature: str,
    ) -> None:
        """广播 ``voice_call.started`` 事件。

        Args:
            active: 新建的通话状态。
            previous_signature: 通话前的 chatter 签名。
        """

        try:
            await event_api.publish_event(
                EVENT_VOICE_CALL_STARTED,
                {
                    "caller_stream_id": active.caller_stream_id,
                    "started_at": active.started_at,
                    "previous_chatter_signature": previous_signature,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 事件广播失败不应中断通话
            logger.warning(f"广播 {EVENT_VOICE_CALL_STARTED} 失败: {exc}")


class EndVoiceCallAction(BaseAction):
    """挂断当前语音通话并广播结束事件。"""

    name = "end_voice_call"
    associated_types = ["text"]
    description = (
        "挂断当前语音通话并切回正常聊天。"
        "调用场景：你和用户已经说完想说的话、用户说要挂断、或者你判断没有继续语音的必要了。"
        "调用后：anima_chatter 释放对当前 stream 的接管，下一轮自动绑回原 chatter，"
        "通话期间产生的消息会通过事件机制补回原 chatter 的对话历史，保证上下文不丢。"
    )
    chatter_allow = ["anima_chatter"]
    chat_type = ChatType.PRIVATE
    primary_action = False

    async def go_activate(self) -> bool:
        """只在该 stream 正处于通话中时暴露。

        Returns:
            是否对模型可见。
        """

        return await call_state.is_call_active_for_stream(self.chat_stream.stream_id)

    async def execute(
        self,
        farewell: Annotated[
            str,
            "挂断时给用户的告别话。例如 '那先这样吧，回头见'、'我得忙别的去了'。"
            "用第一人称、口语化、一两句话；不必煽情。",
        ] = "",
    ) -> tuple[bool, str]:
        """挂断通话。

        Args:
            farewell: 告别文本。

        Returns:
            ``(是否成功, 结果描述)``。
        """

        return await finalize_call(
            stream_id=self.chat_stream.stream_id,
            farewell=farewell.strip(),
            end_reason="model",
            plugin=require_plugin(self.plugin),
        )


__all__ = ["EndVoiceCallAction", "StartVoiceCallAction"]
