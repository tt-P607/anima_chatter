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
"""

from __future__ import annotations

from typing import Annotated, Any

from src.app.plugin_system.api import chat_api, event_api, send_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.types import ChatType
from src.core.components.base.action import BaseAction

from .. import call_state


logger = get_logger("anima_chatter.action.voice_call")


# anima_chatter 的 chatter 签名（与 :data:`plugins.anima_chatter.plugin._CHATTER_SIGNATURE` 一致）。
_VOICE_CHATTER_SIGNATURE = "anima_chatter:chatter:anima_chatter"

# asr_adapter 的转发服务签名。
_ASR_REDIRECT_SERVICE = "asr_adapter_anima:service:asr_redirect"

# 事件名（与设计文档第 10 节一致；kfc handler 会订阅 ended）。
EVENT_VOICE_CALL_STARTED = "voice_call.started"
EVENT_VOICE_CALL_ENDED = "voice_call.ended"


async def _restart_stream_loop(stream_id: str) -> None:
    """重启 stream 循环，让缓存的 chatter 生成器被销毁后下一 tick 用新 chatter 重建。

    这是 default_chatter / kokoro_flow_chatter / VTBCommand 都用的同一套路。
    StreamLoopManager 当前没有暴露公开 API，等公开后再换公开调用。
    """

    # NOTE: 触碰内部模块——目的同 :class:`plugins.anima_chatter.commands.VTBCommand`。
    from src.core.transport.distribution.stream_loop_manager import (
        get_stream_loop_manager,
    )

    try:
        await get_stream_loop_manager().restart_stream_loop(stream_id)
    except Exception as exc:
        logger.warning(f"重启流循环失败 stream={stream_id}: {exc}", exc_info=True)


def _resolve_caller_identity(chat_stream: Any) -> tuple[str, str]:
    """从 chat_stream 反查"通话对方在该平台上的真实 (user_id, user_name)"。

    扫描 ``context.history_messages`` 找最近一条**对方发的**消息——
    在私聊里 bot 自己的 ``sender_id == bot_id``，对方就是非 bot 的发送方。

    Returns:
        ``(user_id, user_name)``。找不到时 user_id 为空字符串，调用方应据此
        提前拒绝 voice_call（通话靠对方真实 ID 路由，没有 ID 后续会崩）。
    """

    bot_id = str(getattr(chat_stream, "bot_id", "") or "")
    history = list(getattr(chat_stream.context, "history_messages", []) or [])
    unread = list(getattr(chat_stream.context, "unread_messages", []) or [])

    # 倒序扫描，先看 unread 再看 history——优先用最新一条。
    for msg in reversed(unread + history):
        sender_id = str(getattr(msg, "sender_id", "") or "")
        if not sender_id:
            continue
        if bot_id and sender_id == bot_id:
            continue  # 跳过 bot 自己发的消息
        sender_name = str(getattr(msg, "sender_name", "") or sender_id)
        return sender_id, sender_name
    return "", ""


async def _start_asr_voice_session(
    platform: str,
    stream_id: str,
    *,
    user_id: str,
    user_name: str,
    group_id: str = "",
) -> bool:
    """通话开始时一次性配齐 ASR：启动 runtime + 切 always_on + 设 redirect（含真实身份）。

    通过 ``asr_adapter:service:asr_redirect`` Service 调用——保证插件之间
    通过公开 Service 接口通信，不直接 import 对方源码。
    """

    service = get_service(_ASR_REDIRECT_SERVICE)
    if service is None or not hasattr(service, "start_voice_call_session"):
        logger.error(
            f"未找到 {_ASR_REDIRECT_SERVICE} 服务（或版本过旧），无法启动 ASR 通话会话。"
            "请检查 asr_adapter_anima 是否启用 + 版本是否 >= 1.1.0"
        )
        return False
    try:
        return await service.start_voice_call_session(  # type: ignore[attr-defined]
            platform,
            stream_id,
            target_user_id=user_id,
            target_user_name=user_name,
            target_group_id=group_id,
        )
    except Exception as exc:
        logger.error(
            f"启动 ASR 通话会话失败 platform={platform} stream={stream_id}: {exc}",
            exc_info=True,
        )
        return False


async def _end_asr_voice_session() -> None:
    """通话结束时一次性清理 ASR：清 redirect + 还原激活模式 + 视情况停 runtime。"""

    service = get_service(_ASR_REDIRECT_SERVICE)
    if service is None or not hasattr(service, "end_voice_call_session"):
        logger.warning(
            f"未找到 {_ASR_REDIRECT_SERVICE} 服务，无法清理 ASR 通话会话（可能已无效）"
        )
        return
    try:
        await service.end_voice_call_session()  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning(f"清理 ASR 通话会话失败: {exc}", exc_info=True)


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

        # 在 messages_in_call 第一条插入系统注解，作为通话上下文的边界标记。
        # 这条会随事件 payload 透传给 kfc handler，让 kfc 在重组 chain_payloads
        # 时知道"这一段是发生在电话里的"，并且能看到具体的通话开始时间。
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

        # ── 2.5) 注入 history_messages 边界（给 DFC 等无状态 chatter 看） ──
        # 构造一条 system 类型的 Message 注入到框架通用历史中。
        # 这样 DFC 切回来时能看到明确的通话边界，不必改 DFC 源码。
        from src.core.models.message import Message, MessageType

        history_msg = Message(
            message_id=f"call_start_{active.started_at}",
            content=note_text,
            processed_plain_text=note_text,
            message_type=MessageType.NOTICE,
            platform=platform,
            stream_id=stream_id,
            sender_id="system",
            sender_name="系统通知",
            time=active.started_at,
        )
        self.chat_stream.context.add_history_message(history_msg)

        # ── 3) 启动 ASR 通话会话（启动 runtime + 切 always_on + 设 redirect） ──
        if not platform:
            await call_state.clear_active_call()
            return False, "当前流缺少 platform 信息，无法启动语音通话"

        # 从最近的 history_messages 反查通话发起方在该平台上的真实 ID。
        # 为什么要这样：ASR 识别出的文本要"以通话发起方的身份"上行到目标
        # stream，否则下游会把 ASR 适配器自己的 speaker_id（"local_microphone"）
        # 当成新用户，触发"创建新流 + napcat 发消息时把 'local_microphone'
        # 当 QQ 号 int(...) 直接崩"的连环错误。
        target_user_id, target_user_name = _resolve_caller_identity(self.chat_stream)
        if not target_user_id:
            await call_state.clear_active_call()
            return False, (
                "无法确定通话发起方在该平台上的真实 ID（最近无对方消息记录）。"
                "请等对方先在私聊中发一条消息后再重试。"
            )

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

        return await _finalize_call(
            stream_id=self.chat_stream.stream_id,
            platform=self.chat_stream.platform or "",
            farewell=farewell.strip(),
            end_reason="model",
            plugin=self.plugin,
        )


async def _play_farewell_via_tts(
    plugin: Any,
    farewell_text: str,
    stream_id: str,
) -> None:
    """挂断时通过 plugin.audio_player 播放告别语（不发回 QQ 文本）。

    通话语义下"告别"应该是通话里的最后一句话——通过扬声器说出来，而不是
    挂断后再单独发一条 QQ 文字消息。

    出错时就静默吞掉——告别语不是关键路径，挂断流程必须能继续走完。
    """

    try:
        from ..config import AnimaChatterConfig
        from ..tts import TTSRequest, build_tts_backend

        audio_player = getattr(plugin, "audio_player", None)
        if audio_player is None:
            logger.warning(
                f"挂断告别音频：plugin.audio_player 不可用 stream={stream_id}，"
                f"跳过 TTS 播放（告别词='{farewell_text}'）"
            )
            return

        plugin_config = getattr(plugin, "config", None)
        if not isinstance(plugin_config, AnimaChatterConfig):
            logger.warning("挂断告别音频：插件配置不可用，跳过")
            return

        backend = build_tts_backend(plugin_config, logger)
        artifact = await backend.synthesize(
            TTSRequest(stream_id=stream_id, text=farewell_text)
        )
        if artifact.audio:
            await audio_player.play_audio(artifact.audio)
            logger.info(f"已通过 TTS 播放挂断告别词: {farewell_text[:30]}")
        else:
            logger.warning(f"挂断告别 TTS 返回空音频 stream={stream_id}")
    except Exception as exc:
        logger.warning(f"播放挂断告别音频失败 stream={stream_id}: {exc}", exc_info=True)


async def _finalize_call(
    *,
    stream_id: str,
    platform: str,
    farewell: str,
    end_reason: str,
    plugin: Any = None,
) -> tuple[bool, str]:
    """终结一次通话的公共路径。

    被 :class:`EndVoiceCallAction`、超时回调（runner 检查超时）、
    ``/voice off`` 兜底命令复用——保证副作用顺序统一：

    1. **立即关闭 ASR**：放最前面。后续 TTS 合成 + 播放告别音频会阻塞数秒，
       期间 ASR 不能继续收音注入消息（否则对方说的话会被当成挂断后的新
       消息发到 QQ 流，看起来像"挂断后 ASR 还在工作"）。
    2. **释放 chatter 接管 + 重启循环**：让 anima_chatter 主循环立刻退出，
       下一 tick 自动绑回原 chatter（kfc / default 等）。
    3. **清状态拿快照**：保留通话期间的 messages_in_call，用于事件 payload。
    4. **TTS 播放告别词**：通话语境里告别就是电话里最后一句话，通过扬声器
       播放，不发回原平台（之前会发一条 QQ 文字消息，违反"电话只有声音"
       的语义）。
    5. **广播 voice_call.ended 事件**：让 kfc 等订阅方把通话历史补回。

    Args:
        stream_id: 通话所在的 stream。
        platform: stream 的 platform（保留参数，目前不再用 send_text 路径）。
        farewell: 给用户的告别文本；空串则用默认。
        end_reason: ``"model" / "user" / "timeout" / "manual"``。
        plugin: anima_chatter 插件实例，用于拿 audio_player 播放 TTS 告别音频。
            为 None 时跳过 TTS 播放（仅记日志）。
    """

    import time as _time

    _ = platform  # 保留参数；目前不再用 send_text 路径，不再需要走原平台
    active = await call_state.get_active_call()
    if active is None or active.caller_stream_id != stream_id:
        return False, "当前 stream 没有进行中的通话"

    # ── 1) 立即关闭 ASR 通话会话（清 redirect + 还原激活模式 + 停 runtime） ──
    # 必须放最前面：后续 TTS 合成 + 播放需要数秒，期间 ASR 必须停止收音，
    # 否则对方在挂断瞬间说的话会被识别后注入到目标 QQ 流，造成"挂断后
    # 还能听到对方说话"的错觉。
    await _end_asr_voice_session()

    # ── 2) 释放 chatter 接管，让 anima_chatter 主循环退出 ──
    # 也要尽早做：anima_chatter 主循环还在跑就可能继续生成消息。
    existing = chat_api.get_chatter_by_stream(stream_id)
    if existing is not None and existing.__class__.get_signature() == _VOICE_CHATTER_SIGNATURE:
        chat_api.restore_stream_to_default(stream_id)
    await _restart_stream_loop(stream_id)

    # ── 3) 告别词录入 + TTS 本地播放（不发回 QQ 文本） ──
    farewell_text = farewell or "嗯，那先这样吧。"
    await call_state.record_assistant_message(stream_id, farewell_text)
    if plugin is not None:
        await _play_farewell_via_tts(plugin, farewell_text, stream_id)

    # ── 3.5) 写入"通话结束"系统标注（与开头的 [语音通话开始] 配对） ──
    # 让原 chatter 重新接管时，对话历史能清晰看到"这一段是通话语境"的边界，
    # 并明确通话起讫时间和持续时长。
    import datetime as _datetime

    ended_now = _time.time()
    elapsed_seconds = max(0.0, ended_now - active.started_at)
    minutes_part = int(elapsed_seconds // 60)
    seconds_part = int(elapsed_seconds % 60)
    duration_str = (
        f"{minutes_part} 分 {seconds_part} 秒"
        if minutes_part > 0
        else f"{seconds_part} 秒"
    )
    reason_label = {
        "model": "由你主动挂断",
        "user": "由用户挂断",
        "timeout": "超时自动挂断",
        "manual": "管理员手动挂断",
    }.get(end_reason, f"原因={end_reason}")
    started_human = _datetime.datetime.fromtimestamp(active.started_at).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    ended_human = _datetime.datetime.fromtimestamp(ended_now).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await call_state.record_system_note(
        stream_id,
        f"[语音通话结束 @ {ended_human}] 本通话起于 {started_human}，"
        f"持续 {duration_str}，{reason_label}。"
        "接下来的对话回到普通文字聊天。",
    )

    # ── 4) 清状态拿快照 ──────────
    snapshot = await call_state.end_call(end_reason)
    if snapshot is None:
        snapshot = active

    # ── 4.5) 注入 history_messages 结束边界（给 DFC 等无状态 chatter 看） ──
    # 同样构造一条 NOTICE 类型的 Message 注入到框架通用历史。
    # 这样 DFC 切回来时能看到明确的通话结束点。
    try:
        from src.core.models.message import Message, MessageType

        # 找 snapshot 里的最后一条 system note（即刚才 record_system_note 写入的那条）
        note_text = "📞 语音通话已结束。"
        for m in reversed(snapshot.messages_in_call):
            if m.get("role") == "system":
                note_text = m.get("text", note_text)
                break

        history_msg = Message(
            message_id=f"call_end_{ended_now}",
            content=note_text,
            processed_plain_text=note_text,
            message_type=MessageType.NOTICE,
            platform=active.caller_stream_id.split(":", 1)[0],  # 简单从 stream_id 拆 platform
            stream_id=stream_id,
            sender_id="system",
            sender_name="系统通知",
            time=ended_now,
        )
        # 通过 stream_api 拿 stream 引用并注入
        from src.app.plugin_system.api import stream_api

        stream = await stream_api.get_stream(stream_id)
        if stream:
            stream.context.add_history_message(history_msg)
    except Exception as exc:
        logger.warning(f"注入通话结束 history_message 失败: {exc}")

    # ── 5) 广播事件 ───────────────
    ended_at = _time.time()
    duration = max(0.0, ended_at - snapshot.started_at)
    try:
        await event_api.publish_event(
            EVENT_VOICE_CALL_ENDED,
            {
                "caller_stream_id": snapshot.caller_stream_id,
                "started_at": snapshot.started_at,
                "ended_at": ended_at,
                "previous_chatter_signature": snapshot.previous_chatter_signature,
                "duration_seconds": duration,
                "messages_in_call": list(snapshot.messages_in_call),
                "end_reason": end_reason,
            },
        )
    except Exception as exc:
        logger.warning(f"广播 voice_call.ended 失败: {exc}")

    logger.info(
        f"语音通话已结束 stream={stream_id} reason={end_reason} "
        f"duration={duration:.1f}s messages={len(snapshot.messages_in_call)}"
    )
    return True, f"通话已结束（原因：{end_reason}）"


__all__ = [
    "EVENT_VOICE_CALL_ENDED",
    "EVENT_VOICE_CALL_STARTED",
    "EndVoiceCallAction",
    "StartVoiceCallAction",
    "_finalize_call",
]
