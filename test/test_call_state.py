"""通话状态中枢的单元测试。

覆盖互斥语义、基于静默的超时计算、消息入档与幂等去重。
"""

from __future__ import annotations

import pytest

from plugins.anima_chatter.runtime import call_state


_STREAM = "qq:private:12345"
_OTHER_STREAM = "qq:private:99999"


async def test_no_active_call_initially() -> None:
    """初始状态应没有进行中的通话。"""

    assert await call_state.get_active_call() is None
    assert call_state.snapshot_active_call_unlocked() is None
    assert await call_state.get_remaining_seconds() is None


async def test_set_active_call_records_metadata() -> None:
    """开始通话应记录发起方、起始时间与前一个 chatter 签名。"""

    active = await call_state.set_active_call(
        _STREAM, previous_chatter_signature="other:chatter:other"
    )

    assert active.caller_stream_id == _STREAM
    assert active.previous_chatter_signature == "other:chatter:other"
    assert active.last_activity_at == active.started_at
    assert active.messages_in_call == []


async def test_second_call_is_rejected() -> None:
    """同时只允许一个通话，第二次开启应抛异常。"""

    await call_state.set_active_call(_STREAM)

    with pytest.raises(RuntimeError, match="已有进行中的通话"):
        await call_state.set_active_call(_OTHER_STREAM)


async def test_timeout_has_lower_bound() -> None:
    """超时时长下限为 1 秒，防止配置成 0 导致立即挂断。"""

    active = await call_state.set_active_call(_STREAM, timeout_seconds=0.0)

    assert active.timeout_seconds == 1.0


async def test_is_call_active_only_for_caller_stream() -> None:
    """只有通话发起方的 stream 才被判定为通话中。"""

    await call_state.set_active_call(_STREAM)

    assert await call_state.is_call_active_for_stream(_STREAM) is True
    assert await call_state.is_call_active_for_stream(_OTHER_STREAM) is False


async def test_remaining_seconds_based_on_last_activity() -> None:
    """剩余时间基于最近活跃时刻，而非通话总时长。"""

    active = await call_state.set_active_call(_STREAM, timeout_seconds=100.0)
    # 手动把活跃时刻往前拨 40 秒，模拟"安静了 40 秒"。
    active.last_activity_at -= 40.0

    remaining = await call_state.get_remaining_seconds()

    assert remaining is not None
    assert 59.0 < remaining < 61.0


async def test_recording_message_refreshes_activity() -> None:
    """记录消息应刷新活跃时刻，实质上为通话续期。"""

    active = await call_state.set_active_call(_STREAM, timeout_seconds=100.0)
    active.last_activity_at -= 50.0

    await call_state.record_assistant_message(_STREAM, "我还在呢")

    remaining = await call_state.get_remaining_seconds()
    assert remaining is not None
    assert remaining > 99.0


async def test_record_messages_in_order() -> None:
    """三类消息应按发生顺序入档并带上正确的 role。"""

    await call_state.set_active_call(_STREAM)

    await call_state.record_system_note(_STREAM, "通话开始")
    await call_state.record_user_message(_STREAM, "你好", ts=1000.0)
    await call_state.record_assistant_message(_STREAM, "你也好")

    active = await call_state.get_active_call()
    assert active is not None
    assert [msg["role"] for msg in active.messages_in_call] == [
        "system",
        "user",
        "assistant",
    ]
    assert [msg["text"] for msg in active.messages_in_call] == [
        "通话开始",
        "你好",
        "你也好",
    ]


async def test_duplicate_user_message_is_deduplicated() -> None:
    """同文本同时间戳的用户消息只入档一次。"""

    await call_state.set_active_call(_STREAM)

    await call_state.record_user_message(_STREAM, "咪", ts=1000.0)
    await call_state.record_user_message(_STREAM, "咪", ts=1000.2)

    active = await call_state.get_active_call()
    assert active is not None
    assert len(active.messages_in_call) == 1


async def test_same_text_at_different_time_is_kept() -> None:
    """时间戳相差超过去重窗口时应视为两次独立发言。"""

    await call_state.set_active_call(_STREAM)

    await call_state.record_user_message(_STREAM, "咪", ts=1000.0)
    await call_state.record_user_message(_STREAM, "咪", ts=1005.0)

    active = await call_state.get_active_call()
    assert active is not None
    assert len(active.messages_in_call) == 2


async def test_message_without_timestamp_dedupes_by_text() -> None:
    """没有时间戳时退化为按文本去重。"""

    await call_state.set_active_call(_STREAM)

    await call_state.record_user_message(_STREAM, "咪")
    await call_state.record_user_message(_STREAM, "咪")

    active = await call_state.get_active_call()
    assert active is not None
    assert len(active.messages_in_call) == 1


async def test_message_for_wrong_stream_is_discarded() -> None:
    """stream 不匹配的消息应被静默丢弃。"""

    await call_state.set_active_call(_STREAM)

    await call_state.record_user_message(_OTHER_STREAM, "不该被记录")
    await call_state.record_assistant_message(_OTHER_STREAM, "也不该")
    await call_state.record_system_note(_OTHER_STREAM, "同样不该")

    active = await call_state.get_active_call()
    assert active is not None
    assert active.messages_in_call == []


async def test_end_call_returns_snapshot_and_clears() -> None:
    """结束通话应返回含消息的快照并清空状态。"""

    await call_state.set_active_call(_STREAM)
    await call_state.record_user_message(_STREAM, "拜拜")

    snapshot = await call_state.end_call("user")

    assert snapshot is not None
    assert len(snapshot.messages_in_call) == 1
    assert await call_state.get_active_call() is None


async def test_end_call_is_idempotent() -> None:
    """没有活跃通话时结束通话应返回 None 而非报错。"""

    assert await call_state.end_call("timeout") is None


async def test_snapshot_reflects_current_state() -> None:
    """同步快照接口应返回与异步接口一致的实例。"""

    active = await call_state.set_active_call(_STREAM)

    assert call_state.snapshot_active_call_unlocked() is active
