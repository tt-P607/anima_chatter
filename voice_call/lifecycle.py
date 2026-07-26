"""语音通话生命周期编排。

集中实现通话的 ASR 会话切换、告别音频播放、终结事件广播与状态清理。
:func:`finalize_call` 由结束通话 Action、超时看门狗与强制挂断命令共同复用，
确保各入口的资源释放顺序一致。
"""

from __future__ import annotations

import asyncio
import datetime
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Protocol, runtime_checkable

from src.app.plugin_system.api import chat_api, event_api, stream_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service

from .._internal_compat import (
    build_notice_message,
    cancel_background_task,
    create_background_task,
    feed_watchdog,
    restart_stream_loop,
)
from ..constants import CHATTER_SIGNATURE
from ..protocol import AnimaPlugin
from ..runtime import call_state, pipeline_state
from ..runtime.call_state import CallEndReason
from ..speech import TTSRequest, build_tts_backend


__all__ = [
    "ASR_REDIRECT_SERVICE",
    "EVENT_VOICE_CALL_ENDED",
    "EVENT_VOICE_CALL_STARTED",
    "end_asr_voice_session",
    "finalize_call",
    "keep_watchdog_alive",
    "play_via_tts",
    "restart_stream_loop_safely",
    "start_asr_voice_session",
]


logger = get_logger("anima_chatter.voice_call.lifecycle")


ASR_REDIRECT_SERVICE = "asr_adapter_anima:service:asr_redirect"
"""ASR 转发服务的组件签名。插件之间通过公开 Service 通信，不直接 import 源码。"""

EVENT_VOICE_CALL_STARTED = "voice_call.started"
"""通话开始事件名。"""

EVENT_VOICE_CALL_ENDED = "voice_call.ended"
"""通话结束事件名。订阅方可据此把通话历史补回自己的对话链。"""


# Watchdog 喂狗间隔。框架 stream 警告阈值默认 40s，2s 间隔足够把心跳保持在
# 阈值之下。
_WATCHDOG_FEED_INTERVAL_SECONDS = 2.0

# 挂断原因对应的中文标注。
_END_REASON_LABELS: dict[str, str] = {
    "model": "由你主动挂断",
    "user": "由用户挂断",
    "timeout": "超时自动挂断",
    "manual": "管理员手动挂断",
    "plugin_unload": "服务停止，通话中断",
}

_DEFAULT_FAREWELL = "嗯，那先这样吧。"


@asynccontextmanager
async def keep_watchdog_alive(stream_id: str) -> AsyncIterator[None]:
    """在长阻塞操作期间持续喂 watchdog。

    通话发起时 ASR runtime 冷启动可能阻塞 20 秒以上，告别时 TTS 合成 + 播放
    也会阻塞数秒。不喂狗会让框架先打 warning，进而强制重启 stream，把通话
    流程整个打断。

    Args:
        stream_id: 目标聊天流 ID。

    Yields:
        ``None``——只负责喂狗生命周期。
    """

    stop_event = asyncio.Event()

    async def _feed_loop() -> None:
        """喂狗循环：进入即喂一次，之后每隔固定间隔喂一次。"""

        feed_watchdog(stream_id)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=_WATCHDOG_FEED_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                feed_watchdog(stream_id)

    handle = create_background_task(
        _feed_loop(),
        name=f"anima_chatter.watchdog_keepalive.{stream_id[:8]}",
        metadata={"stream_id": stream_id, "kind": "watchdog_keepalive"},
    )
    try:
        yield
    finally:
        stop_event.set()
        task = handle.task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                cancel_background_task(handle)
                with suppress(asyncio.CancelledError):
                    await task


# ── ASR 会话切换 ───────────────────────────────────────────


@runtime_checkable
class _AsrRedirectServiceLike(Protocol):
    """``asr_adapter_anima:service:asr_redirect`` 的最小公开形状。

    按协议边界表达跨插件依赖，不 import 对方源码。
    """

    async def start_voice_call_session(
        self,
        platform: str,
        stream_id: str,
        *,
        target_user_id: str,
        target_user_name: str,
        target_group_id: str,
    ) -> bool:
        """启动 ASR 通话会话。"""
        ...

    async def end_voice_call_session(self) -> None:
        """结束 ASR 通话会话。"""
        ...


def _get_asr_service() -> _AsrRedirectServiceLike | None:
    """获取 ASR 转发服务并校验其形状。

    Returns:
        符合协议的服务实例；服务缺失或版本过旧时返回 ``None``。
    """

    service = get_service(ASR_REDIRECT_SERVICE)
    if service is None or not isinstance(service, _AsrRedirectServiceLike):
        return None
    return service


async def start_asr_voice_session(
    platform: str,
    stream_id: str,
    *,
    user_id: str,
    user_name: str,
    group_id: str = "",
) -> bool:
    """通话开始时一次性配齐 ASR：启动 runtime + 切 always_on + 设转发目标。

    Args:
        platform: 目标流的平台标识。
        stream_id: 目标聊天流 ID。
        user_id: 通话对方在该平台的真实 ID。
        user_name: 通话对方的显示名。
        group_id: 群 ID（私聊通话时为空）。

    Returns:
        ASR 会话是否成功启动。
    """

    service = _get_asr_service()
    if service is None:
        logger.error(
            f"未找到 {ASR_REDIRECT_SERVICE} 服务（或版本过旧），无法启动 ASR 通话会话。"
            "请检查 asr_adapter_anima 是否启用且版本 >= 1.1.0"
        )
        return False

    try:
        async with keep_watchdog_alive(stream_id):
            return await service.start_voice_call_session(
                platform,
                stream_id,
                target_user_id=user_id,
                target_user_name=user_name,
                target_group_id=group_id,
            )
    except Exception as exc:  # noqa: BLE001 - 跨插件调用，异常类型不受控
        logger.error(
            f"启动 ASR 通话会话失败 platform={platform} stream={stream_id}: {exc}",
            exc_info=True,
        )
        return False


async def end_asr_voice_session() -> None:
    """通话结束时清理 ASR：清转发目标 + 还原激活模式 + 视情况停 runtime。"""

    service = _get_asr_service()
    if service is None:
        logger.warning(
            f"未找到 {ASR_REDIRECT_SERVICE} 服务，无法清理 ASR 通话会话（可能已失效）"
        )
        return
    try:
        await service.end_voice_call_session()
    except Exception as exc:  # noqa: BLE001 - 跨插件调用，异常类型不受控
        logger.warning(f"清理 ASR 通话会话失败: {exc}", exc_info=True)


async def restart_stream_loop_safely(stream_id: str) -> None:
    """重启 stream 循环，失败时只记警告不中断挂断流程。

    Args:
        stream_id: 目标聊天流 ID。
    """

    try:
        await restart_stream_loop(stream_id)
    except Exception as exc:  # noqa: BLE001 - 重启失败不应让挂断流程崩掉
        logger.warning(f"重启流循环失败 stream={stream_id}: {exc}", exc_info=True)


# ── TTS 播放 ───────────────────────────────────────────────


async def play_via_tts(plugin: AnimaPlugin, text: str, stream_id: str) -> None:
    """通过本地扬声器播放一句话（不发回原平台文本）。

    用于通话的接通提示与告别语——通话语义下这些都应该是"电话里说的话"，通过
    扬声器发出，而不是挂断后再单独发一条文字消息。

    失败时静默降级（只记日志）：这不是关键路径，通话流程必须能继续走完。

    Args:
        plugin: 插件实例，提供 audio_player 与配置。
        text: 待播放文本。
        stream_id: 目标聊天流 ID。
    """

    audio_player = plugin.audio_player
    config = plugin.config
    if audio_player is None or config is None:
        logger.warning(
            f"TTS 播放跳过：audio_player 或配置不可用 stream={stream_id}（文本='{text}'）"
        )
        return

    try:
        async with keep_watchdog_alive(stream_id):
            backend = build_tts_backend(config.tts)
            artifact = await backend.synthesize(
                TTSRequest(stream_id=stream_id, text=text)
            )
            if artifact.is_playable and artifact.audio is not None:
                await audio_player.play_audio(artifact.audio)
                logger.info(f"已通过 TTS 播放: {text[:30]}")
            else:
                logger.warning(f"TTS 返回空音频 stream={stream_id}")
    except Exception as exc:  # noqa: BLE001 - 播放失败不能中断通话流程
        logger.warning(f"TTS 播放失败 stream={stream_id}: {exc}", exc_info=True)


# ── 通话终结 ───────────────────────────────────────────────


def _format_duration(seconds: float) -> str:
    """把秒数格式化为"N 分 M 秒"。

    Args:
        seconds: 时长秒数。

    Returns:
        中文时长描述。
    """

    minutes, secs = divmod(int(max(0.0, seconds)), 60)
    return f"{minutes} 分 {secs} 秒" if minutes > 0 else f"{secs} 秒"


def _format_timestamp(timestamp: float) -> str:
    """把 Unix 时间戳格式化为本地可读时间。

    Args:
        timestamp: Unix 时间戳。

    Returns:
        ``"YYYY-MM-DD HH:MM:SS"`` 形式的字符串。
    """

    return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def build_end_note(
    *,
    started_at: float,
    ended_at: float,
    end_reason: CallEndReason,
) -> str:
    """构造"通话结束"系统标注文案。

    与通话开头的 ``[语音通话开始]`` 标注配对，让原 chatter 重新接管时能看到
    清晰的语境边界与通话时长。

    Args:
        started_at: 通话开始的 Unix 时间戳。
        ended_at: 通话结束的 Unix 时间戳。
        end_reason: 结束原因。

    Returns:
        标注正文。
    """

    reason_label = _END_REASON_LABELS.get(end_reason, f"原因={end_reason}")
    return (
        f"[语音通话结束 @ {_format_timestamp(ended_at)}] "
        f"本通话起于 {_format_timestamp(started_at)}，"
        f"持续 {_format_duration(ended_at - started_at)}，{reason_label}。"
        "接下来的对话回到普通文字聊天。"
    )


async def _inject_history_boundary(
    stream_id: str,
    note_text: str,
    timestamp: float,
) -> None:
    """把通话结束标注注入框架通用历史。

    这样切回来的其他 chatter（不订阅 ``voice_call.ended`` 事件的那些）也能看
    到明确的通话边界，无需修改它们的源码。

    Args:
        stream_id: 目标聊天流 ID。
        note_text: 标注正文。
        timestamp: 标注时间戳。
    """

    try:
        chat_stream = await stream_api.get_stream(stream_id)
        if chat_stream is None:
            return
        chat_stream.context.add_history_message(
            build_notice_message(
                message_id=f"call_end_{timestamp}",
                content=note_text,
                platform=chat_stream.platform,
                stream_id=stream_id,
                time=timestamp,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 注入失败不应中断挂断流程
        logger.warning(f"注入通话结束边界失败 stream={stream_id}: {exc}")


async def _publish_ended_event(
    snapshot: call_state.ActiveCall,
    ended_at: float,
    end_reason: CallEndReason,
) -> None:
    """广播 ``voice_call.ended`` 事件。

    Args:
        snapshot: 通话状态快照（含通话期间全部消息）。
        ended_at: 结束时间戳。
        end_reason: 结束原因。
    """

    try:
        await event_api.publish_event(
            EVENT_VOICE_CALL_ENDED,
            {
                "caller_stream_id": snapshot.caller_stream_id,
                "started_at": snapshot.started_at,
                "ended_at": ended_at,
                "previous_chatter_signature": snapshot.previous_chatter_signature,
                "duration_seconds": max(0.0, ended_at - snapshot.started_at),
                "messages_in_call": list(snapshot.messages_in_call),
                "end_reason": end_reason,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 事件广播失败不应中断挂断流程
        logger.warning(f"广播 {EVENT_VOICE_CALL_ENDED} 失败: {exc}")


async def finalize_call(
    *,
    stream_id: str,
    farewell: str,
    end_reason: CallEndReason,
    plugin: AnimaPlugin | None = None,
) -> tuple[bool, str]:
    """终结一次通话——所有挂断入口的公共路径。

    副作用按固定顺序执行：

    1. **立即关闭 ASR**：后续 TTS 合成 + 播放会阻塞数秒，期间 ASR 不能继续
       收音，否则对方在挂断瞬间说的话会被识别后注入目标流，造成"挂断后还能
       听到对方说话"的错觉。
    2. **清空流水线状态**：避免残留的排队时刻影响该 stream 下次进入直播模式。
    3. **释放 chatter 接管 + 重启循环**：让 anima_chatter 主循环立刻退出，下
       一 tick 自动绑回原 chatter。
    4. **播放告别语**：通过扬声器说出，不发回原平台。
    5. **写入结束标注 + 清状态拿快照**。
    6. **广播 ``voice_call.ended``**：让订阅方把通话历史补回。

    Args:
        stream_id: 通话所在的聊天流。
        farewell: 告别文本；空串时用默认文案。
        end_reason: 结束原因。
        plugin: 插件实例，用于播放 TTS 告别音频；``None`` 时跳过播放。

    Returns:
        ``(是否成功挂断, 结果描述)``。当前 stream 没有进行中的通话时返回
        ``(False, ...)``。
    """

    active = await call_state.get_active_call()
    if active is None or active.caller_stream_id != stream_id:
        return False, "当前 stream 没有进行中的通话"

    # 1) 关闭 ASR
    await end_asr_voice_session()

    # 2) 清空流水线状态
    await pipeline_state.clear(stream_id)

    # 3) 释放 chatter 接管
    existing = chat_api.get_chatter_by_stream(stream_id)
    if existing is not None and existing.get_signature() == CHATTER_SIGNATURE:
        chat_api.restore_stream_to_default(stream_id)
    await restart_stream_loop_safely(stream_id)

    # 4) 告别语入档 + 播放
    farewell_text = farewell or _DEFAULT_FAREWELL
    await call_state.record_assistant_message(stream_id, farewell_text)
    if plugin is not None:
        await play_via_tts(plugin, farewell_text, stream_id)

    # 5) 写入结束标注 + 清状态
    ended_at = time.time()
    note_text = build_end_note(
        started_at=active.started_at,
        ended_at=ended_at,
        end_reason=end_reason,
    )
    await call_state.record_system_note(stream_id, note_text)

    snapshot = await call_state.end_call(end_reason) or active
    await _inject_history_boundary(stream_id, note_text, ended_at)

    # 6) 广播事件
    await _publish_ended_event(snapshot, ended_at, end_reason)

    duration = max(0.0, ended_at - snapshot.started_at)
    logger.info(
        f"语音通话已结束 stream={stream_id} reason={end_reason} "
        f"duration={duration:.1f}s messages={len(snapshot.messages_in_call)}"
    )
    return True, f"通话已结束（原因：{end_reason}）"
