"""语音 Chatter 输出标记解析与句子切分。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_WAIT_RE = re.compile(r"\[wait\s*:\s*([0-9]+(?:\.[0-9]+)?)\]", re.IGNORECASE)
_EMOTION_RE = re.compile(
    r"\[emotion\s*:\s*([a-zA-Z0-9_\-]+)\](.*?)\[/emotion\]",
    re.IGNORECASE | re.DOTALL,
)
_EMOTION_OPEN_RE = re.compile(r"\[emotion\s*:\s*([a-zA-Z0-9_\-]+)\]", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"(.+?(?:……|[。！？!?]|\n+))", re.DOTALL)


@dataclass
class SpeechSegment:
    """单个待合成/播放的语音片段。"""

    text: str
    wait_before: float = 0.0
    emotion: str | None = None
    markers: dict[str, Any] = field(default_factory=dict)


def parse_speech_segments(content: str, *, split_sentences: bool = True) -> list[SpeechSegment]:
    """解析模型输出中的语音标记，并返回可播放片段。"""

    segments: list[SpeechSegment] = []
    pending_wait = 0.0
    cursor = 0

    for match in _EMOTION_RE.finditer(content):
        prefix = content[cursor:match.start()]
        pending_wait = _append_plain_segments(
            segments,
            prefix,
            pending_wait=pending_wait,
            split_sentences=split_sentences,
            emotion=None,
        )
        emotion = match.group(1).strip() or None
        pending_wait = _append_plain_segments(
            segments,
            match.group(2),
            pending_wait=pending_wait,
            split_sentences=split_sentences,
            emotion=emotion,
        )
        cursor = match.end()

    # 清理尾部残留的标签
    tail = _EMOTION_OPEN_RE.sub("", content[cursor:])
    tail = tail.replace("[/emotion]", "").replace("[/EMOTION]", "")
    _append_plain_segments(
        segments,
        tail,
        pending_wait=pending_wait,
        split_sentences=split_sentences,
        emotion=None,
    )
    return [segment for segment in segments if segment.text.strip()]


def _append_plain_segments(
    segments: list[SpeechSegment],
    text: str,
    *,
    pending_wait: float,
    split_sentences: bool,
    emotion: str | None,
) -> float:
    """解析 wait 标记并追加普通文本片段，返回尚未消耗的等待时间。"""

    cursor = 0
    current_wait = pending_wait
    for match in _WAIT_RE.finditer(text):
        current_wait = _append_text_chunks(
            segments,
            text[cursor:match.start()],
            wait_before=current_wait,
            split_sentences=split_sentences,
            emotion=emotion,
        )
        try:
            current_wait = float(match.group(1))
        except ValueError:
            current_wait = 0.0
        cursor = match.end()

    return _append_text_chunks(
        segments,
        text[cursor:],
        wait_before=current_wait,
        split_sentences=split_sentences,
        emotion=emotion,
    )


def _append_text_chunks(
    segments: list[SpeechSegment],
    text: str,
    *,
    wait_before: float,
    split_sentences: bool,
    emotion: str | None,
) -> float:
    """按句切分文本并追加片段，返回未被消费的等待时间。"""

    chunks = split_complete_sentences(text) if split_sentences else [text]
    current_wait = wait_before
    for chunk in chunks:
        clean = chunk.strip()
        if not clean:
            continue
        markers: dict[str, Any] = {}
        if emotion:
            markers["emotion"] = emotion
        if current_wait > 0:
            markers["wait_before"] = current_wait
        segments.append(
            SpeechSegment(
                text=clean,
                wait_before=current_wait,
                emotion=emotion,
                markers=markers,
            )
        )
        current_wait = 0.0
    return current_wait


def split_complete_sentences(text: str) -> list[str]:
    """按完整句子边界切分文本，保留句末标点。"""

    stripped = text.strip()
    if not stripped:
        return []

    chunks: list[str] = []
    cursor = 0
    for match in _SENTENCE_END_RE.finditer(stripped):
        chunk = match.group(1).strip()
        if chunk:
            chunks.append(chunk)
        cursor = match.end()

    tail = stripped[cursor:].strip()
    if tail:
        chunks.append(tail)
    return chunks


__all__ = ["SpeechSegment", "parse_speech_segments", "split_complete_sentences"]
