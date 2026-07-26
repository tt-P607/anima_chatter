"""语音合成调度与播放决策的单元测试。

播放层的真实音频输出依赖声卡设备，不在单元测试范围内；这里覆盖的是**决策与
调度逻辑**：时长估算、流水线启用判定、TTS 参数合并、表演参数解析。
"""

from __future__ import annotations

import pytest

from plugins.anima_chatter.config import PipeliningSection
from plugins.anima_chatter.speech import (
    PerformanceStyle,
    SpeechSegment,
    build_segment_markers,
    estimate_segments_duration,
    should_use_pipeline,
)
from plugins.anima_chatter.speech.backend import TTSArtifact


# ── 表演参数 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("emotion", "expected_main"),
    [
        ("happy:2", "happy"),
        ("HAPPY:3", "happy"),
        ("neutral", "neutral"),
        ("", "neutral"),
    ],
)
def test_performance_style_extracts_emotion_main(
    emotion: str, expected_main: str
) -> None:
    """表演参数应正确解析出 emotion 主类型，供 expression 匹配兜底。"""

    style = PerformanceStyle.create(emotion, "NARRATING")

    assert style.emotion_main == expected_main
    assert style.emotion == emotion
    assert style.intent == "NARRATING"


# ── 时长估算 ───────────────────────────────────────────────


def test_estimate_duration_scales_with_text_length() -> None:
    """文本越长估算时长越长。"""

    short = estimate_segments_duration([SpeechSegment(text="短")])
    long = estimate_segments_duration([SpeechSegment(text="这是一段明显更长的文本内容")])

    assert long > short


def test_estimate_duration_includes_wait_before() -> None:
    """估算时长应包含段前静默。"""

    without = estimate_segments_duration([SpeechSegment(text="内容")])
    with_wait = estimate_segments_duration(
        [SpeechSegment(text="内容", wait_before=5.0)]
    )

    assert with_wait == pytest.approx(without + 5.0)


def test_estimate_duration_sums_all_segments() -> None:
    """多段的估算时长应为各段之和。"""

    single = estimate_segments_duration([SpeechSegment(text="内容")])
    triple = estimate_segments_duration([SpeechSegment(text="内容")] * 3)

    assert triple == pytest.approx(single * 3)


def test_estimate_duration_of_empty_list_is_zero() -> None:
    """空片段列表估算为 0。"""

    assert estimate_segments_duration([]) == 0.0


# ── 流水线判定 ─────────────────────────────────────────────


def test_pipeline_enabled_when_all_conditions_met() -> None:
    """直播模式 + 配置启用 + 时长达标时启用流水线。"""

    section = PipeliningSection(enabled=True, min_duration_seconds=10.0)

    assert (
        should_use_pipeline(is_live_mode=True, section=section, estimated_duration=30.0)
        is True
    )


def test_pipeline_disabled_outside_live_mode() -> None:
    """非直播模式一律不走流水线。"""

    section = PipeliningSection(enabled=True, min_duration_seconds=10.0)

    assert (
        should_use_pipeline(is_live_mode=False, section=section, estimated_duration=30.0)
        is False
    )


def test_pipeline_disabled_by_config() -> None:
    """配置关闭时不走流水线。"""

    section = PipeliningSection(enabled=False, min_duration_seconds=10.0)

    assert (
        should_use_pipeline(is_live_mode=True, section=section, estimated_duration=30.0)
        is False
    )


def test_pipeline_disabled_below_min_duration() -> None:
    """时长不足最低门槛时退化为阻塞模式。"""

    section = PipeliningSection(enabled=True, min_duration_seconds=30.0)

    assert (
        should_use_pipeline(is_live_mode=True, section=section, estimated_duration=10.0)
        is False
    )


# ── TTS 参数合并 ───────────────────────────────────────────


def test_segment_markers_merge_action_params() -> None:
    """Action 顶层参数应被合并进片段 markers。"""

    segment = SpeechSegment(text="内容")
    markers = build_segment_markers(segment, {"style": "cheerful", "speed": 1.2})

    assert markers == {"style": "cheerful", "speed": 1.2}


def test_inline_markers_take_priority() -> None:
    """片段自带的行内标记优先级高于 Action 顶层参数。"""

    segment = SpeechSegment(text="内容", markers={"emotion": "sad"})
    markers = build_segment_markers(segment, {"emotion": "happy", "speed": 1.0})

    assert markers["emotion"] == "sad"
    assert markers["speed"] == 1.0


def test_segment_markers_do_not_mutate_source() -> None:
    """合并不应修改片段自身的 markers。"""

    segment = SpeechSegment(text="内容", markers={"emotion": "sad"})
    build_segment_markers(segment, {"style": "cheerful"})

    assert segment.markers == {"emotion": "sad"}


# ── 合成产物 ───────────────────────────────────────────────


def test_artifact_with_audio_is_playable() -> None:
    """有音频且无错误的产物可播放。"""

    assert TTSArtifact(text="内容", audio=b"fake").is_playable is True


def test_artifact_without_audio_is_not_playable() -> None:
    """空音频的产物不可播放。"""

    assert TTSArtifact(text="内容").is_playable is False


def test_artifact_with_error_is_not_playable() -> None:
    """带错误的产物即使有音频也不可播放。"""

    artifact = TTSArtifact(text="内容", audio=b"fake", error="合成超时")

    assert artifact.is_playable is False
