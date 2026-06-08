"""anima_chatter 语音通话生命周期辅助。

把"通话发起 / 终结"的公共逻辑从 [`actions/voice_call.py`](actions/voice_call.py:1) 抽出来：
``finalize_call`` 是公共"通话终结协议"，被 :class:`EndVoiceCallAction`、超时
回调（runner 检查超时）、``/voice off`` 兜底命令复用。历史上它叫
``_finalize_call`` 写在 action 文件里，名字带下划线但被外部模块导入——这是
误导。挪到本模块统一以"无下划线公共函数"形式暴露。

同时迁移：

- :func:`finalize_call` —— 通话终结公共路径
- :func:`_play_farewell_via_tts` —— 走 plugin.audio_player 播 TTS 告别音频
- :func:`_resolve_caller_identity` —— 从 chat_stream 反查通话发起方真实身份
- :func:`_start_asr_voice_session` / :func:`_end_asr_voice_session` —— ASR
  转发 service 调用
- ``EVENT_VOICE_CALL_STARTED`` / ``EVENT_VOICE_CALL_ENDED`` 事件名常量
- ``_ASR_REDIRECT_SERVICE`` 服务签名常量

[`actions/voice_call.py`](actions/voice_call.py:1) 仍持有
``StartVoiceCallAction`` / ``EndVoiceCallAction`` 两个 action 类，但内部
调用都转发到本模块的公共函数。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from src.app.plugin_system.api import chat_api, event_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service

from . import call_state, pipeline_state
from ._internal_compat import build_notice_message, feed_watchdog, restart_stream_loop
from .constants import CHATTER_SIGNATURE as _VOICE_CHATTER_SIGNATURE


logger = get_logger("anima_chatter.voice_call_lifecycle")


# Watchdog 喂狗间隔。框架 stream_warning_threshold 默认 40s、stream_restart_threshold
# 默认 300s；2s 间隔足够把心跳保持在 warning 阈值之下。
_WATCHDOG_FEED_INTERVAL_SECONDS: float = 2.0


@asynccontextmanager
async def _keep_watchdog_alive(stream_id: str) -> AsyncIterator[None]:
    """在长阻塞操作期间持续喂 watchdog，避免触发 stream_restart_threshold。

    通话发起时 ``_start_asr_voice_session`` 会同步等待 ASR runtime 启动
    （冷启动可能阻塞 25 秒以上），告别时 ``_play_farewell_via_tts`` 也会
    阻塞数秒做 TTS 合成与播放。如果不喂狗，框架会先打 warning，进而触发
    stream 强制重启，把通话流程整个打断。

    本 context manager 起一个后台 task 每 ``_WATCHDOG_FEED_INTERVAL_SECONDS``
    秒喂一次狗，退出时自动取消该任务。喂狗本身是同步操作，开销可忽略。
    """
    stop_event = asyncio.Event()

    async def _feed_loop() -> None:
        # 进入即喂一次，确保即便阻塞动作还没让出协程也能抢先标记心跳。
        try:
            feed_watchdog(stream_id)
        except Exception:  # noqa: BLE001 - 喂狗失败不应影响主流程
            pass
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=_WATCHDOG_FEED_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                try:
                    feed_watchdog(stream_id)
                except Exception:  # noqa: BLE001
                    pass

    task = asyncio.create_task(
        _feed_loop(),
        name=f"anima_chatter.watchdog_keepalive.{stream_id[:8]}",
    )
    try:
        yield
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()
        except Exception:  # noqa: BLE001 - 清理失败不应影响主流程
            task.cancel()


# asr_adapter 的转发服务签名。
_ASR_REDIRECT_SERVICE = "asr_adapter_anima:service:asr_redirect"

# 事件名（与设计文档第 10 节一致；kfc handler 会订阅 ended）。
EVENT_VOICE_CALL_STARTED = "voice_call.started"
EVENT_VOICE_CALL_ENDED = "voice_call.ended"


__all__ = [
    "EVENT_VOICE_CALL_ENDED",
    "EVENT_VOICE_CALL_STARTED",
    "_ASR_REDIRECT_SERVICE",
    "_end_asr_voice_session",
    "_play_farewell_via_tts",
    "_resolve_caller_identity",
    "_restart_stream_loop",
    "_start_asr_voice_session",
    "finalize_call",
]


# ── 内部小工具：通话上下文派生 ───────────────────────────


def _resolve_caller_identity(chat_stream: Any) -> tuple[str, str]:
    """从 chat_stream 反查"通话对方在该平台上的真实 (user_id, user_name)"。

    扫描 ``context.history_messages`` 找最近一条**对方发的**消息——
    在私聊里 bot 自己的 ``sender_id == bot_id``，对方就是非 bot 的发送方。

    跳过条件：
    - bot 自己发的消息（``sender_id == bot_id``）
    - 系统通知类消息（``message_type == MessageType.NOTICE``）。
      ``start_voice_call`` 会在调用本函数前往 history 写一条边界标注
      （``sender_id="system"``）；如果不跳过，本函数会把它当作通话对方
      返回，导致 ASR 转发使用 ``"system"`` 作为 user_id，下游 onebot
      adapter 在 ``int("system")`` 时崩溃，并且会因 sender_id 错误
      生成新 stream_id，把后续 ASR 文本路由到一个空白 chatter 上。
    - ``sender_id`` 为 ``"system"`` 等通用关键字（兜底，覆盖 NOTICE 没正确
      标记 message_type 的边角情况）。

    Returns:
        ``(user_id, user_name)``。找不到时 user_id 为空字符串，调用方应据此
        提前拒绝 voice_call（通话靠对方真实 ID 路由，没有 ID 后续会崩）。
    """

    # 延迟 import：MessageType 是框架内部模型，与 plugin_system.api 暴露
    # 的公共类型解耦；import 失败时退化为只用 sender_id 关键字过滤。
    try:
        from src.core.models.message import MessageType
        notice_type: Any = MessageType.NOTICE
    except Exception:  # noqa: BLE001 - import 失败应退化而非崩溃
        notice_type = None

    bot_id = str(getattr(chat_stream, "bot_id", "") or "")
    history = list(getattr(chat_stream.context, "history_messages", []) or [])
    unread = list(getattr(chat_stream.context, "unread_messages", []) or [])

    # 倒序扫描，先看 unread 再看 history——优先用最新一条。
    for msg in reversed(unread + history):
        sender_id = str(getattr(msg, "sender_id", "") or "")
        if not sender_id or sender_id.lower() == "system":
            continue
        if bot_id and sender_id == bot_id:
            continue  # 跳过 bot 自己发的消息
        # 跳过系统通知类消息（如 voice_call 自己写入的边界标注）
        if notice_type is not None and getattr(msg, "message_type", None) == notice_type:
            continue
        sender_name = str(getattr(msg, "sender_name", "") or sender_id)
        return sender_id, sender_name
    return "", ""


# ── ASR 转发 service 调用 ────────────────────────────────


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

    ASR runtime 冷启动（首次加载模型 / 检查依赖）可能阻塞 20 秒以上，期间
    chatter generator 处于 await 状态、不会喂 watchdog；本函数包一层
    ``_keep_watchdog_alive`` 持续喂狗，避免触发 ``stream_restart_threshold``
    把整个通话流程打断。
    """

    service = get_service(_ASR_REDIRECT_SERVICE)
    if service is None or not hasattr(service, "start_voice_call_session"):
        logger.error(
            f"未找到 {_ASR_REDIRECT_SERVICE} 服务（或版本过旧），无法启动 ASR 通话会话。"
            "请检查 asr_adapter_anima 是否启用 + 版本是否 >= 1.1.0"
        )
        return False
    try:
        async with _keep_watchdog_alive(stream_id):
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


# ── stream loop 重启（兼容包装） ─────────────────────────


async def _restart_stream_loop(stream_id: str) -> None:
    """重启 stream 循环，让缓存的 chatter 生成器被销毁后下一 tick 用新 chatter 重建。

    实际工作交给 :func:`_internal_compat.restart_stream_loop`；本函数只加一层
    异常保护，避免重启失败把整个挂断流程也弄崩。
    """

    try:
        await restart_stream_loop(stream_id)
    except Exception as exc:
        logger.warning(f"重启流循环失败 stream={stream_id}: {exc}", exc_info=True)


# ── TTS 告别音频 ─────────────────────────────────────────


async def _play_farewell_via_tts(
    plugin: Any,
    farewell_text: str,
    stream_id: str,
) -> None:
    """挂断时通过 plugin.audio_player 播放告别语（不发回 QQ 文本）。

    通话语义下"告别"应该是通话里的最后一句话——通过扬声器说出来，而不是
    挂断后再单独发一条 QQ 文字消息。

    TTS 合成 + 播放是个数秒级的阻塞操作，整段包在 ``_keep_watchdog_alive``
    内持续喂狗，避免框架在告别期间触发 stream 重启。

    出错时就静默吞掉——告别语不是关键路径，挂断流程必须能继续走完。
    """

    try:
        from .config import AnimaChatterConfig
        from .tts import TTSRequest, build_tts_backend

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

        async with _keep_watchdog_alive(stream_id):
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


# ── 通话终结公共路径 ─────────────────────────────────────


async def finalize_call(
    *,
    stream_id: str,
    platform: str,
    farewell: str,
    end_reason: str,
    plugin: Any = None,
) -> tuple[bool, str]:
    """终结一次通话的公共路径（公共 API，命令 / runner / action 共用）。

    被 :class:`actions.voice_call.EndVoiceCallAction`、超时回调（runner 检查
    超时）、``/voice off`` 兜底命令复用——保证副作用顺序统一：

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

    # ── 1.5) 清空流水线状态 ──────────
    # 通话挂断后切回原 chatter（kfc / dfc 等）；流水线只对 vtb_live 模式生
    # 效，但 stream 上残留的 audio_finish_at 可能影响下一次 stream 进入
    # vtb_live 时的首次 reserve（让它误以为还有未播完的音频要等）。挂断
    # 时一并清掉，保证下次重新进入流水线模式时从零开始。
    try:
        await pipeline_state.clear(stream_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"清空流水线状态失败（忽略）: {exc}")

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
        # 找 snapshot 里的最后一条 system note（即刚才 record_system_note 写入的那条）
        note_text = "📞 语音通话已结束。"
        for m in reversed(snapshot.messages_in_call):
            if m.get("role") == "system":
                note_text = m.get("text", note_text)
                break

        history_msg = build_notice_message(
            message_id=f"call_end_{ended_now}",
            content=note_text,
            # 简单从 stream_id 拆 platform
            platform=active.caller_stream_id.split(":", 1)[0],
            stream_id=stream_id,
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
