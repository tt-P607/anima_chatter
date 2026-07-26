"""语音通话子域。

- :mod:`.identity` — 从聊天流反查通话对方的真实平台身份。
- :mod:`.lifecycle` — 通话的 ASR 会话切换、告别播放、终结事件与状态清理。

通话状态本身在 [`runtime/call_state.py`](../runtime/call_state.py:1)。
"""

from __future__ import annotations

from .identity import resolve_caller_identity
from .lifecycle import (
    EVENT_VOICE_CALL_ENDED,
    EVENT_VOICE_CALL_STARTED,
    end_asr_voice_session,
    finalize_call,
    play_via_tts,
    restart_stream_loop_safely,
    start_asr_voice_session,
)


__all__ = [
    "EVENT_VOICE_CALL_ENDED",
    "EVENT_VOICE_CALL_STARTED",
    "end_asr_voice_session",
    "finalize_call",
    "play_via_tts",
    "resolve_caller_identity",
    "restart_stream_loop_safely",
    "start_asr_voice_session",
]
