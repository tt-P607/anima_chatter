"""intent / emotion 常量与归一化逻辑的单元测试。

重点是**跨模块一致性契约**：:data:`INTENT_REGISTRY` 的 keys 必须与
``SpeechAnimator.intent_map`` 完全一致，``VALID_EMOTION_TYPES`` 必须与
``SpeechAnimator.emotion_matrix`` 完全一致——否则模型选了某个 intent 却没有对应
的动画姿态，表现为"选了动作但形象没反应"。
"""

from __future__ import annotations

import pytest

from plugins.anima_chatter.constants import (
    INTENT_REGISTRY,
    VALID_EMOTION_TYPES,
    VALID_INTENTS,
    normalize_intent,
    split_emotion,
)
from plugins.anima_chatter.vts.animation.speech import SpeechAnimator


def test_intent_registry_matches_animator_intent_map() -> None:
    """intent 文案表与动画姿态表的 keys 必须完全一致。"""

    animator = SpeechAnimator()
    assert VALID_INTENTS == frozenset(animator.intent_map)


def test_emotion_types_match_animator_matrix() -> None:
    """emotion 主类型集合与动画情绪矩阵的 keys 必须完全一致。"""

    animator = SpeechAnimator()
    assert VALID_EMOTION_TYPES == frozenset(animator.emotion_matrix)


def test_intent_registry_has_no_duplicate_names() -> None:
    """intent 名不得重复，否则文案会出现同名两条。"""

    names = [meta.name for meta in INTENT_REGISTRY]
    assert len(names) == len(set(names))


def test_intent_registry_entries_have_both_descriptions() -> None:
    """每个 intent 都必须同时提供 VTB 与直播两套描述。"""

    for meta in INTENT_REGISTRY:
        assert meta.desc_vtb, f"{meta.name} 缺少 desc_vtb"
        assert meta.desc_live, f"{meta.name} 缺少 desc_live"
        assert meta.group, f"{meta.name} 缺少 group"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("EXCITED", "EXCITED"),
        ("excited", "EXCITED"),
        ("  ShY_DoWn  ", "SHY_DOWN"),
    ],
)
def test_normalize_intent_accepts_case_insensitive(raw: str, expected: str) -> None:
    """合法 intent 应被归一化为大写形式，大小写与空白不敏感。"""

    assert normalize_intent(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "NOT_AN_INTENT"])
def test_normalize_intent_falls_back_on_invalid(raw: str | None) -> None:
    """非法或缺失的 intent 应降级为默认值。"""

    assert normalize_intent(raw) == "NARRATING"


def test_normalize_intent_respects_custom_default() -> None:
    """调用方指定的兜底值应生效。"""

    assert normalize_intent("bogus", default="IDLE") == "IDLE"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("happy:2", ("happy", 2)),
        ("HAPPY:3", ("happy", 3)),
        ("  sad : 1 ", ("sad", 1)),
    ],
)
def test_split_emotion_parses_type_and_level(
    raw: str, expected: tuple[str, int]
) -> None:
    """``类型:强度`` 应被正确拆分，大小写与空白不敏感。"""

    assert split_emotion(raw) == expected


def test_split_emotion_uses_default_level_without_colon() -> None:
    """没有 ``:强度`` 时应使用默认强度。"""

    assert split_emotion("happy") == ("happy", 2)


@pytest.mark.parametrize(
    ("raw", "expected_level"),
    [("happy:0", 1), ("happy:9", 3), ("happy:-5", 2)],
)
def test_split_emotion_clamps_level(raw: str, expected_level: int) -> None:
    """强度越界应被截断到 ``[1, 3]``。

    注意 ``happy:-5`` 的 ``-5`` 不是纯数字串，会走"缺省强度"分支。
    """

    assert split_emotion(raw)[1] == expected_level


def test_split_emotion_falls_back_on_invalid_type() -> None:
    """非法主类型应降级为默认类型，但保留给出的强度。"""

    assert split_emotion("bogus:3") == ("neutral", 3)


def test_split_emotion_respects_custom_defaults() -> None:
    """调用方指定的兜底类型与强度应生效（唱歌场景用到）。"""

    assert split_emotion(None, default_type="happy", default_level=1) == ("happy", 1)
