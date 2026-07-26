"""三态模式判定与通话身份反查的单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.anima_chatter.modes import (
    LIVE_PLATFORMS,
    VOICE_PLATFORM,
    resolve_mode,
)
from plugins.anima_chatter.runtime import call_state
from plugins.anima_chatter.voice_call.identity import resolve_caller_identity


def _stream(platform: str, stream_id: str = "s1") -> SimpleNamespace:
    """构造最小可用的聊天流替身。

    Args:
        platform: 平台标识。
        stream_id: 流 ID。

    Returns:
        含 ``platform`` 与 ``stream_id`` 的对象。
    """

    return SimpleNamespace(platform=platform, stream_id=stream_id)


def test_local_asr_resolves_to_voice() -> None:
    """本地 ASR 平台应判定为 voice 模式。"""

    assert resolve_mode(_stream(VOICE_PLATFORM)) == "voice"


@pytest.mark.parametrize("platform", sorted(LIVE_PLATFORMS))
def test_live_platform_resolves_to_vtb_live(platform: str) -> None:
    """直播平台应判定为 vtb_live 模式。"""

    assert resolve_mode(_stream(platform)) == "vtb_live"


@pytest.mark.parametrize("platform", ["qq", "discord", "telegram", ""])
def test_other_platform_resolves_to_vtb(platform: str) -> None:
    """其余平台一律判定为 vtb 模式。"""

    assert resolve_mode(_stream(platform)) == "vtb"


def test_platform_is_stripped_before_matching() -> None:
    """平台标识两侧的空白应被忽略。"""

    assert resolve_mode(_stream(f"  {VOICE_PLATFORM}  ")) == "voice"


async def test_active_call_forces_voice_mode() -> None:
    """通话中的 stream 强制走 voice 模式，无视原平台。"""

    await call_state.set_active_call("qq_stream")

    assert resolve_mode(_stream("qq", stream_id="qq_stream")) == "voice"


async def test_active_call_does_not_affect_other_streams() -> None:
    """通话只影响发起方 stream，其他 stream 按平台正常判定。"""

    await call_state.set_active_call("qq_stream")

    assert resolve_mode(_stream("qq", stream_id="another_stream")) == "vtb"


# ── 通话身份反查 ───────────────────────────────────────────


def _message(
    sender_id: str,
    *,
    sender_name: str = "",
    message_type: object = None,
) -> SimpleNamespace:
    """构造最小可用的消息替身。

    Args:
        sender_id: 发送方 ID。
        sender_name: 发送方显示名。
        message_type: 消息类型；``None`` 表示普通消息。

    Returns:
        消息替身对象。
    """

    return SimpleNamespace(
        sender_id=sender_id,
        sender_name=sender_name,
        message_type=message_type,
    )


def _stream_with_messages(
    *,
    bot_id: str,
    history: list[SimpleNamespace],
    unread: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    """构造带消息上下文的聊天流替身。

    Args:
        bot_id: bot 自身 ID。
        history: 历史消息列表。
        unread: 未读消息列表。

    Returns:
        聊天流替身对象。
    """

    return SimpleNamespace(
        bot_id=bot_id,
        context=SimpleNamespace(
            history_messages=history,
            unread_messages=unread or [],
        ),
    )


def test_resolves_latest_counterpart_message() -> None:
    """应取最近一条对方发的消息作为通话身份。"""

    stream = _stream_with_messages(
        bot_id="bot",
        history=[
            _message("111", sender_name="旧的"),
            _message("222", sender_name="新的"),
        ],
    )

    assert resolve_caller_identity(stream) == ("222", "新的")


def test_unread_takes_priority_over_history() -> None:
    """未读消息比历史消息更新，应优先采用。"""

    stream = _stream_with_messages(
        bot_id="bot",
        history=[_message("111", sender_name="历史")],
        unread=[_message("222", sender_name="未读")],
    )

    assert resolve_caller_identity(stream) == ("222", "未读")


def test_skips_bot_own_messages() -> None:
    """bot 自己发的消息应被跳过。"""

    stream = _stream_with_messages(
        bot_id="bot",
        history=[_message("111", sender_name="对方"), _message("bot", sender_name="我")],
    )

    assert resolve_caller_identity(stream) == ("111", "对方")


def test_skips_notice_messages() -> None:
    """系统通知类消息应被跳过——否则会用 "system" 当通话 ID。"""

    from src.app.plugin_system.types import MessageType

    stream = _stream_with_messages(
        bot_id="bot",
        history=[
            _message("111", sender_name="对方"),
            _message("system", message_type=MessageType.NOTICE),
        ],
    )

    assert resolve_caller_identity(stream) == ("111", "对方")


def test_skips_system_sender_id() -> None:
    """``sender_id`` 为 system 的消息应被跳过（message_type 兜底）。"""

    stream = _stream_with_messages(
        bot_id="bot",
        history=[_message("111", sender_name="对方"), _message("SYSTEM")],
    )

    assert resolve_caller_identity(stream) == ("111", "对方")


def test_falls_back_to_sender_id_when_name_missing() -> None:
    """没有显示名时用 ID 兜底。"""

    stream = _stream_with_messages(bot_id="bot", history=[_message("333")])

    assert resolve_caller_identity(stream) == ("333", "333")


def test_returns_empty_when_no_counterpart() -> None:
    """找不到对方消息时应返回空串，让调用方拒绝发起通话。"""

    stream = _stream_with_messages(bot_id="bot", history=[_message("bot")])

    assert resolve_caller_identity(stream) == ("", "")
