"""内联标记解析与句子切分的单元测试。

覆盖 motion / emotion 嵌套、未闭合标签容错、wait 消费、纯标点段合并与分句边界。
"""

from __future__ import annotations

from plugins.anima_chatter.speech.markers import (
    parse_speech_segments,
    split_complete_sentences,
    strip_markers,
)


def test_strip_markers_removes_all_three_kinds() -> None:
    """三类标记应被一次性剥离，且不要求闭合。"""

    text = "[wait:1.5]你好[emotion:happy]世界[/emotion][motion:EXCITED]再见"
    assert strip_markers(text) == "你好世界再见"


def test_split_complete_sentences_keeps_punctuation() -> None:
    """按句切分应保留句末标点。"""

    assert split_complete_sentences("你好。世界！还有吗？") == [
        "你好。",
        "世界！",
        "还有吗？",
    ]


def test_split_complete_sentences_returns_empty_for_blank() -> None:
    """纯空白输入应返回空列表。"""

    assert split_complete_sentences("   \n  ") == []


def test_split_complete_sentences_keeps_tail_without_punctuation() -> None:
    """无句末标点的尾部应作为独立一句保留。"""

    assert split_complete_sentences("你好。世界") == ["你好。", "世界"]


def test_parse_binds_motion_to_inner_segments() -> None:
    """motion 块内的片段应绑定该 motion，块外为 None。"""

    segments = parse_speech_segments("外面。[motion:EXCITED]里面。[/motion]")

    assert [seg.motion for seg in segments] == [None, "EXCITED"]
    assert [seg.text for seg in segments] == ["外面。", "里面。"]


def test_parse_binds_emotion_inside_motion() -> None:
    """emotion 块嵌在 motion 块内时，两者应同时绑定到片段。"""

    segments = parse_speech_segments(
        "[motion:SHY_DOWN][emotion:happy]开心。[/emotion][/motion]"
    )

    assert len(segments) == 1
    assert segments[0].motion == "SHY_DOWN"
    assert segments[0].emotion == "happy"
    assert segments[0].markers == {"emotion": "happy", "motion": "SHY_DOWN"}


def test_parse_tolerates_unclosed_motion_tag() -> None:
    """未闭合的 motion 标签应被清理，不污染尾部文本。"""

    segments = parse_speech_segments("正常内容。[motion:EXCITED]被截断了")

    assert [seg.text for seg in segments] == ["正常内容。", "被截断了"]
    assert all(seg.motion is None for seg in segments)


def test_parse_tolerates_unclosed_emotion_tag() -> None:
    """未闭合的 emotion 标签同样应被清理。"""

    segments = parse_speech_segments("正常。[emotion:sad]截断")

    assert [seg.text for seg in segments] == ["正常。", "截断"]


def test_wait_applies_only_to_next_segment() -> None:
    """wait 只作用于其后的第一个片段，后续片段归零。"""

    segments = parse_speech_segments("第一句。[wait:2.5]第二句。第三句。")

    assert [seg.wait_before for seg in segments] == [0.0, 2.5, 0.0]
    assert segments[1].markers["wait_before"] == 2.5


def test_punct_only_segment_merges_into_previous() -> None:
    """motion 块外的纯标点片段应并入前一段，而不是独立成段。"""

    segments = parse_speech_segments(
        "[motion:EXCITED]开心[/motion]，[motion:SHY_DOWN]害羞[/motion]",
        split_sentences=False,
    )

    assert [seg.text for seg in segments] == ["开心，", "害羞"]
    assert [seg.motion for seg in segments] == ["EXCITED", "SHY_DOWN"]


def test_leading_punct_merges_into_next_segment() -> None:
    """前面没有实义段时，纯标点应并入下一段开头并继承其 motion。"""

    segments = parse_speech_segments(
        "，[motion:EXCITED]开心[/motion]", split_sentences=False
    )

    assert len(segments) == 1
    assert segments[0].text == "，开心"
    assert segments[0].motion == "EXCITED"


def test_all_punct_input_is_preserved() -> None:
    """整段都是标点时应原样保留，不被清空。"""

    segments = parse_speech_segments("，。！", split_sentences=False)

    assert len(segments) == 1
    assert segments[0].text == "，。！"


def test_parse_returns_empty_for_blank_input() -> None:
    """空白输入应返回空片段列表。"""

    assert parse_speech_segments("   ") == []


def test_split_sentences_disabled_keeps_single_segment() -> None:
    """关闭分句时整段文本应作为单个片段。"""

    segments = parse_speech_segments("第一句。第二句。", split_sentences=False)

    assert len(segments) == 1
    assert segments[0].text == "第一句。第二句。"
