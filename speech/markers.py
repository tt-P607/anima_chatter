"""模型输出中的内联标记解析与句子切分。

支持三类内联标记：

- ``[wait:n]``：下一段播放前等待 n 秒
- ``[emotion:NAME]...[/emotion]``：包内段使用指定情绪
- ``[motion:NAME]...[/motion]``：包内段使用指定动作意图（intent）

motion 与 emotion 可以同时存在。解析顺序为外到内：先按 motion 切块，每块内
再按 emotion 切，最后处理 wait + 句子切分。motion 不允许嵌套；模型若多嵌一
层内层 motion 会被外层吞掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


__all__ = [
    "SpeechSegment",
    "parse_speech_segments",
    "split_complete_sentences",
    "strip_markers",
]


_WAIT_RE = re.compile(r"\[wait\s*:\s*([0-9]+(?:\.[0-9]+)?)\]", re.IGNORECASE)
_EMOTION_RE = re.compile(
    r"\[emotion\s*:\s*([a-zA-Z0-9_\-]+)\](.*?)\[/emotion\]",
    re.IGNORECASE | re.DOTALL,
)
_EMOTION_OPEN_RE = re.compile(r"\[emotion\s*:\s*([a-zA-Z0-9_\-]+)\]", re.IGNORECASE)
_EMOTION_CLOSE_RE = re.compile(r"\[/emotion\]", re.IGNORECASE)
_MOTION_RE = re.compile(
    r"\[motion\s*:\s*([a-zA-Z0-9_\-]+)\](.*?)\[/motion\]",
    re.IGNORECASE | re.DOTALL,
)
_MOTION_OPEN_RE = re.compile(r"\[motion\s*:\s*([a-zA-Z0-9_\-]+)\]", re.IGNORECASE)
_MOTION_CLOSE_RE = re.compile(r"\[/motion\]", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"(.+?(?:……|[。！？!?]|\n+))", re.DOTALL)


# 用于 :func:`strip_markers` 的"宽松"剥离规则——只要长得像标记就拆掉（不要求
# 闭合，方便处理 LLM 截断输出）。
_STRIP_WAIT_RE = re.compile(r"\[wait\s*:\s*[0-9.]+\]", re.IGNORECASE)
_STRIP_EMOTION_RE = re.compile(
    r"\[/?emotion(?:\s*:\s*[a-zA-Z0-9_\-]+)?\]", re.IGNORECASE
)
_STRIP_MOTION_RE = re.compile(
    r"\[/?motion(?:\s*:\s*[a-zA-Z0-9_\-]+)?\]", re.IGNORECASE
)


# 仅含标点 / 空白的字符集合：合并相邻段时用来识别"零碎段"。这些段独立送 TTS
# 会得到无意义的短音频，且会把动作切换的节奏踩碎。
_PUNCT_CHARS = frozenset("，。！？、…—~ ♪♡♥♬♫·!?,.；;：:“”‘’\"'（）()[]【】《》<>")


@dataclass
class SpeechSegment:
    """单个待合成 / 播放的语音片段。

    Attributes:
        text: 片段正文（已剥离标记）。
        wait_before: 播放本段前的静默秒数。
        emotion: 该段使用的情绪（来自 ``[emotion]`` 标记）；无标记时为 ``None``。
        motion: 该段使用的动作意图（来自 ``[motion]`` 标记）；无标记时为 ``None``。
        markers: 传给 TTS provider 的原始标记字典。
    """

    text: str
    wait_before: float = 0.0
    emotion: str | None = None
    motion: str | None = None
    markers: dict[str, Any] = field(default_factory=dict)


def strip_markers(text: str) -> str:
    """把三类内联标记一次性剥离。

    用于把整段文本发到聊天界面，或在 :func:`parse_speech_segments` 没解析出
    片段时兜底。

    Args:
        text: 含标记的原始文本。

    Returns:
        剥离标记并 strip 首尾空白后的文本。
    """

    cleaned = _STRIP_WAIT_RE.sub("", text)
    cleaned = _STRIP_EMOTION_RE.sub("", cleaned)
    cleaned = _STRIP_MOTION_RE.sub("", cleaned)
    return cleaned.strip()


def split_complete_sentences(text: str) -> list[str]:
    """按完整句子边界切分文本，保留句末标点。

    Args:
        text: 待切分文本。

    Returns:
        句子列表；输入为空白时返回空列表。
    """

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


def parse_speech_segments(
    content: str,
    *,
    split_sentences: bool = True,
) -> list[SpeechSegment]:
    """解析模型输出中的语音标记，返回可播放片段。

    解析顺序（外到内）：

    1. ``[motion:NAME]...[/motion]`` —— 每个 motion 块内的段共享同一 motion；
       块外段 ``motion=None``（说话默认动作由调用方决定）。
    2. ``[emotion:NAME]...[/emotion]`` —— 每个 emotion 块内的段共享 emotion。
    3. ``[wait:n]`` —— 控制下一段的 ``wait_before``。
    4. 句子切分（``split_sentences=True`` 时）。

    最后会把"仅含标点"的零碎段合并到相邻段，避免独立送 TTS 产生无意义短音频。

    Args:
        content: 模型输出的原始文本。
        split_sentences: 是否按句切分。

    Returns:
        可直接送去合成的片段列表；无有效内容时返回空列表。
    """

    segments: list[SpeechSegment] = []
    pending_wait = 0.0
    cursor = 0

    # 第一层：按 motion 切块。
    for match in _MOTION_RE.finditer(content):
        # 块外段（motion=None）
        pending_wait = _process_emotion_layer(
            segments,
            content[cursor : match.start()],
            pending_wait=pending_wait,
            split_sentences=split_sentences,
            motion=None,
        )
        # 块内段（绑定 motion）
        pending_wait = _process_emotion_layer(
            segments,
            match.group(2),
            pending_wait=pending_wait,
            split_sentences=split_sentences,
            motion=match.group(1).strip() or None,
        )
        cursor = match.end()

    # 尾部残留的未闭合 motion 标签需要清理，防止污染最后一段。
    tail = _MOTION_CLOSE_RE.sub("", _MOTION_OPEN_RE.sub("", content[cursor:]))
    _process_emotion_layer(
        segments,
        tail,
        pending_wait=pending_wait,
        split_sentences=split_sentences,
        motion=None,
    )

    non_empty = [segment for segment in segments if segment.text.strip()]
    return _merge_punct_segments(non_empty)


def _is_filler_segment(segment: SpeechSegment) -> bool:
    """判断片段是否仅含标点 / 空白（无实义）。

    Args:
        segment: 待判断的片段。

    Returns:
        无实义时返回 ``True``。
    """

    stripped = segment.text.strip()
    if not stripped:
        return True
    return all(char in _PUNCT_CHARS for char in stripped)


def _merge_punct_segments(segments: list[SpeechSegment]) -> list[SpeechSegment]:
    """把"仅含标点"的零碎段并入相邻段。

    motion 标记会把 ``，`` 这种纯标点切成独立片段（因为它在 motion 块外），
    独立送 TTS 会产生 200ms 内的短音频，还会让动作先闪回顶层 intent 再切回
    新 motion。

    合并优先并到**前一段**（保留文本连续性 + 前段的 motion）；前面没有实义段
    时才并到**下一段**（继承下一段的 motion）。

    Args:
        segments: 原始片段列表。

    Returns:
        合并后的新列表（不修改入参元素以外的结构）。
    """

    if len(segments) <= 1:
        return list(segments)

    merged: list[SpeechSegment] = []
    pending_filler: list[SpeechSegment] = []

    for segment in segments:
        if _is_filler_segment(segment):
            pending_filler.append(segment)
            continue

        if pending_filler:
            filler_text = "".join(item.text for item in pending_filler)
            if merged:
                merged[-1].text += filler_text
            else:
                segment.text = filler_text + segment.text
            pending_filler = []
        merged.append(segment)

    if pending_filler:
        filler_text = "".join(item.text for item in pending_filler)
        if merged:
            merged[-1].text += filler_text
        else:
            # 整段全是标点，原样保留。
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
    """第二层：在 motion 块内按 emotion 切块。

    Args:
        segments: 结果累加列表（就地追加）。
        content: 当前 motion 块的文本。
        pending_wait: 上游尚未消耗的等待时间。
        split_sentences: 是否按句切分。
        motion: 当前块绑定的动作意图。

    Returns:
        本层处理后仍未被消耗的等待时间。
    """

    cursor = 0
    current_wait = pending_wait

    for match in _EMOTION_RE.finditer(content):
        current_wait = _append_plain_segments(
            segments,
            content[cursor : match.start()],
            pending_wait=current_wait,
            split_sentences=split_sentences,
            emotion=None,
            motion=motion,
        )
        current_wait = _append_plain_segments(
            segments,
            match.group(2),
            pending_wait=current_wait,
            split_sentences=split_sentences,
            emotion=match.group(1).strip() or None,
            motion=motion,
        )
        cursor = match.end()

    tail = _EMOTION_CLOSE_RE.sub("", _EMOTION_OPEN_RE.sub("", content[cursor:]))
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
    motion: str | None,
) -> float:
    """第三层：解析 ``[wait]`` 标记并追加文本片段。

    Args:
        segments: 结果累加列表（就地追加）。
        text: 已剥离 motion / emotion 标记的文本。
        pending_wait: 上游尚未消耗的等待时间。
        split_sentences: 是否按句切分。
        emotion: 当前块绑定的情绪。
        motion: 当前块绑定的动作意图。

    Returns:
        本层处理后仍未被消耗的等待时间。
    """

    cursor = 0
    current_wait = pending_wait
    for match in _WAIT_RE.finditer(text):
        current_wait = _append_text_chunks(
            segments,
            text[cursor : match.start()],
            wait_before=current_wait,
            split_sentences=split_sentences,
            emotion=emotion,
            motion=motion,
        )
        current_wait = float(match.group(1))
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
    motion: str | None,
) -> float:
    """第四层：按句切分并构造 :class:`SpeechSegment`。

    ``wait_before`` 只作用于本批的**第一个**非空片段，后续片段归零。

    Args:
        segments: 结果累加列表（就地追加）。
        text: 纯文本（无任何标记）。
        wait_before: 本批第一段的静默秒数。
        split_sentences: 是否按句切分。
        emotion: 片段绑定的情绪。
        motion: 片段绑定的动作意图。

    Returns:
        未被消费的等待时间（本批无有效文本时原样返回）。
    """

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
