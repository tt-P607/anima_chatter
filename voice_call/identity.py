"""通话对方身份反查。

语音通话需要把 ASR 识别出的文本以"对方"的身份注入回原 stream，因此必须先拿
到对方在该平台上的真实 ``(user_id, user_name)``——用错 ID 会导致下游 adapter
崩溃或生成新 stream_id，把 ASR 文本路由到空白 chatter 上。
"""

from __future__ import annotations

from src.app.plugin_system.types import ChatStream, Message, MessageType


__all__ = ["resolve_caller_identity"]


# 不能作为通话对方的 sender_id 关键字（兜底，覆盖 message_type 没标对的情况）。
_SYSTEM_SENDER_IDS = frozenset({"system"})


def _is_counterpart(message: Message, bot_id: str) -> bool:
    """判断一条消息是否来自通话对方。

    排除三类消息：

    - bot 自己发的（``sender_id == bot_id``）
    - 系统通知类（``message_type == NOTICE``）——通话开始 / 结束的边界标注
      就是这种类型，误认会导致用 ``"system"`` 当 user_id
    - ``sender_id`` 命中系统关键字

    Args:
        message: 待判断的消息。
        bot_id: bot 自身在该平台的 ID。

    Returns:
        是通话对方发的消息时返回 ``True``。
    """

    sender_id = (message.sender_id or "").strip()
    if not sender_id or sender_id.lower() in _SYSTEM_SENDER_IDS:
        return False
    if bot_id and sender_id == bot_id:
        return False
    return message.message_type != MessageType.NOTICE


def resolve_caller_identity(chat_stream: ChatStream) -> tuple[str, str]:
    """从聊天流反查通话对方在该平台上的真实身份。

    倒序扫描未读 + 历史消息，取最近一条**对方发的**消息的发送方信息。

    Args:
        chat_stream: 通话所在的聊天流。

    Returns:
        ``(user_id, user_name)``；找不到时 ``user_id`` 为空串，调用方应据此
        拒绝发起通话（通话靠对方真实 ID 路由，没有 ID 后续会崩）。
    """

    bot_id = (chat_stream.bot_id or "").strip()
    context = chat_stream.context
    # 未读比历史新：先倒序扫未读，再倒序扫历史，取第一条命中的。
    for messages in (context.unread_messages, context.history_messages):
        for message in reversed(list(messages)):
            if not _is_counterpart(message, bot_id):
                continue
            sender_id = (message.sender_id or "").strip()
            return sender_id, (message.sender_name or "").strip() or sender_id
    return "", ""
