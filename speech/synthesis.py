"""构造 TTS 流式请求使用的片段标记。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .markers import SpeechSegment


__all__ = ["build_segment_markers"]


def build_segment_markers(
    segment: SpeechSegment,
    tts_params: dict[str, Any],
) -> dict[str, Any]:
    """合并片段自带标记与本次 Action 的 TTS 参数。

    片段自身的标记优先级高于 Action 参数。

    Args:
        segment: 待合成片段。
        tts_params: Action 收到的 TTS 参数。

    Returns:
        传递给 TTS service 的标记字典。
    """

    markers = dict(segment.markers)
    for key, value in tts_params.items():
        markers.setdefault(key, value)
    return markers
