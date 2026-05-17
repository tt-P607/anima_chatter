"""语音 Chatter 输出标记解析与句子切分。

支持的内联标记：

- ``[wait:n]``：下一段播放前等待 n 秒
- ``[emotion:NAME]...[/emotion]``：包内段使用指定情绪
- ``[motion:NAME]...[/motion]``：包内段使用指定动作意图（intent）

motion 与 emotion 可以同时存在；解析顺序：先按 motion 切块，每块内再
按 emotion 切，最后处理 wait + 句子切分。motion 不允许嵌套；模型若多嵌
内层 motion 会被外层吞掉。
"""

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
_MOTION_RE = re.compile(
    r"\[motion\s*:\s*([a-zA-Z0-9_\-]+)\](.*?)\[/motion\]",
    re.IGNORECASE | re.DOTALL,
)
_MOTION_OPEN_RE = re.compile(r"\[motion\s*:\s*([a-zA-Z0-9_\-]+)\]", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"(.+?(?:……|[。！？!?]|\n+))", re.DOTALL)


@dataclass
class SpeechSegment:
    """单个待合成/播放的语音片段。"""

    text: str
    wait_before: float = 0.0
    emotion: str | None = None
    motion: str | None = None
    markers: dict[str, Any] = field(default_factory=dict)


def parse_speech_segments(content: str, *, split_sentences: bool = True) -> list[SpeechSegment]:
    """解析模型输出中的语音标记，并返回可播放片段。

    解析顺序（外到内）：

    1. ``[motion:NAME]...[/motion]`` —— 每个 motion 块独立，块内段共享同一个
       motion 字段；块外段 motion=None（说话默认动作由调用方决定）。
    2. ``[emotion:NAME]...[/emotion]`` —— 每个 emotion 块内段共享 emotion。
    3. ``[wait:n]`` —— 控制下一段的 wait_before。
    4. 句子切分（如果 ``split_sentences=True``）。

    motion 不允许嵌套；嵌套写法会被外层吞掉。
    """

    segments: list[SpeechSegment] = []
    pending_wait = 0.0
    cursor = 0

    # 第一层：按 motion 切块
    for match in _MOTION_RE.finditer(content):
        # 块外段（motion=None）
        prefix = content[cursor:match.start()]
        pending_wait = _process_emotion_layer(
            segments,
            prefix,
            pending_wait=pending_wait,
            split_sentences=split_sentences,
            motion=None,
        )
        # 块内段（绑定 motion）
        motion = match.group(1).strip() or None
        pending_wait = _process_emotion_layer(
            segments,
            match.group(2),
            pending_wait=pending_wait,
            split_sentences=split_sentences,
            motion=motion,
        )
        cursor = match.end()

    # 尾部 motion 标签清理（防止未闭合标签污染最后段）
    tail = _MOTION_OPEN_RE.sub("", content[cursor:])
    tail = tail.replace("[/motion]", "").replace("[/MOTION]", "")
    _process_emotion_layer(
        segments,
        tail,
        pending_wait=pending_wait,
        split_sentences=split_sentences,
        motion=None,
    )
    raw = [segment for segment in segments if segment.text.strip()]
    return _merge_punct_segments(raw)


# 仅含标点 / 空白的字符集合：合并相邻段时用来识别"零碎段"。
# 这些段独立送 TTS 会得到无意义的短音频，且会把动作切换的节奏踩碎。
_PUNCT_CHARS = set("，。！？、…—~ ♪♡♥♬♫·!?,.；;：:""''\"'（）()[]【】《》<>")


def _is_filler_segment(seg: SpeechSegment) -> bool:
    """判断 segment 是否仅含标点 / 空白（无实义）。"""

    stripped = (seg.text or "").strip()
    if not stripped:
        return True
    return all(ch in _PUNCT_CHARS for ch in stripped)


def _merge_punct_segments(segments: list[SpeechSegment]) -> list[SpeechSegment]:
    """把"仅含标点"的零碎段并入相邻段。

    motion 标记会把 ``，`` 这种纯标点切成独立 segment（因为它在 motion 块外）。
    独立送 TTS 会出现：
    - 短音频（200ms 内的 ``，``）
    - 动作 ``闪回顶层 intent`` 再 ``切回新 motion``

    合并优先并到**前一段**（保留文本连续性 + 前段的 motion）；前面没有
    时才并到**下一段**（拿下一段的 motion）。

    返回新列表，不修改入参。
    """

    if len(segments) <= 1:
        return list(segments)

    merged: list[SpeechSegment] = []
    pending_filler: list[SpeechSegment] = []

    for seg in segments:
        if _is_filler_segment(seg):
            # 暂存，等下一段实义段出现时合并到它前面（或合并到 merged 末尾）
            pending_filler.append(seg)
            continue

        if pending_filler:
            # 优先并到前一段（merged 末尾）
            if merged:
                tail = merged[-1]
                tail.text = tail.text + "".join(f.text for f in pending_filler)
            else:
                # 前面没实义段，标点并到当前段开头，并继承当前段的 motion
                seg.text = "".join(f.text for f in pending_filler) + seg.text
            pending_filler = []
        merged.append(seg)

    # 尾部仍有 filler：附到最后一段
    if pending_filler:
        if merged:
            tail = merged[-1]
            tail.text = tail.text + "".join(f.text for f in pending_filler)
        else:
            # 整段全是标点，保留一个原样
            merged.extend(pending_filler)

    return merged


def _process_emotion_layer(
    segments: list[SpeechSegment],
    content: str,
    *,
    pending_wait: float,
    split_sentences: bool,
    motion: str | None,
) -> float:
    """第二层：在 motion 块内按 emotion 切块。返回未消耗的 wait。"""

    cursor = 0
    current_wait = pending_wait

    for match in _EMOTION_RE.finditer(content):
        prefix = content[cursor:match.start()]
        current_wait = _append_plain_segments(
            segments,
            prefix,
            pending_wait=current_wait,
            split_sentences=split_sentences,
            emotion=None,
            motion=motion,
        )
        emotion = match.group(1).strip() or None
        current_wait = _append_plain_segments(
            segments,
            match.group(2),
            pending_wait=current_wait,
            split_sentences=split_sentences,
            emotion=emotion,
            motion=motion,
        )
        cursor = match.end()

    # 尾部 emotion 残留清理
    tail = _EMOTION_OPEN_RE.sub("", content[cursor:])
    tail = tail.replace("[/emotion]", "").replace("[/EMOTION]", "")
    return _append_plain_segments(
        segments,
        tail,
        pending_wait=current_wait,
        split_sentences=split_sentences,
        emotion=None,
        motion=motion,
    )


def _append_plain_segments(
    segments: list[SpeechSegment],
    text: str,
    *,
    pending_wait: float,
    split_sentences: bool,
    emotion: str | None,
    motion: str | None = None,
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
            motion=motion,
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
        motion=motion,
    )


def _append_text_chunks(
    segments: list[SpeechSegment],
    text: str,
    *,
    wait_before: float,
    split_sentences: bool,
    emotion: str | None,
    motion: str | None = None,
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
        if motion:
            markers["motion"] = motion
        if current_wait > 0:
            markers["wait_before"] = current_wait
        segments.append(
            SpeechSegment(
                text=clean,
                wait_before=current_wait,
                emotion=emotion,
                motion=motion,
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
