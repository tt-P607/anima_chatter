"""anima_chatter 共享常量与归一化工具。

本模块汇总了原先散落在 4 个文件里的 ``_VALID_INTENTS``、3 处 emotion / intent
解析逻辑，以及 3 处 ``_CHATTER_SIGNATURE``，供插件其它模块统一引用，避免出
现"新增 intent 必须两边都加"这类同步契约。

不依赖 ``src.kernel.*`` / ``src.core.*`` 任何模块，可被 schema 序列化阶段直
接 import（避免引入 vts 子模块的副作用）。
"""

from __future__ import annotations


# ── chatter 签名常量 ─────────────────────────────────────────
# anima_chatter 插件的 chatter 组件签名。``register_active_chatter`` /
# ``get_chatter_class`` 等公开 API 用此字符串定位本插件的 chatter 类。
CHATTER_SIGNATURE: str = "anima_chatter:chatter:anima_chatter"


# ── 合法 intent 集 ───────────────────────────────────────────
# 与 [`SpeechAnimator.intent_map`](vts/animation/speech.py:97) 严格对齐——
# 那里是动画姿态字典的权威定义，本集合只是把 keys 暴露成纯字符串集合，
# 让 schema 序列化阶段无须 import vts 模块即可校验。
#
# 新增 intent 时：先在 SpeechAnimator.intent_map 里加配置，再补到下面集合。
# 这两处保持一致是单元测试的硬性要求（见 test/plugins/anima_chatter）。
VALID_INTENTS: frozenset[str] = frozenset(
    {
        # 基础姿态
        "IDLE",
        "NARRATING",
        "THINKING",
        "CONFUSED",
        # 高表现力情绪
        "EXCITED",
        "SURPRISED",
        # 眼神方向
        "PEEK_LEFT",
        "PEEK_RIGHT",
        "LOOKAWAY",
        "STARE_DOWN",
        "DREAMY_GAZE",
        # 态度倾向
        "PROUD_LIFT",
        "WORRIED_TILT",
        "SHY_DOWN",
        "ATTENTIVE",
        # 调皮 / 紧张
        "PLAYFUL_TILT",
        "MISCHIEF",
        "SCARED_SHRINK",
    }
)


# ── 合法 emotion 主类型 ─────────────────────────────────────
# 与 [`SpeechAnimator.emotion_matrix`](vts/animation/speech.py:128) 对齐。
VALID_EMOTION_TYPES: frozenset[str] = frozenset(
    {"neutral", "happy", "sad", "angry", "surprised"}
)


def normalize_intent(intent: str | None, *, default: str = "NARRATING") -> str:
    """归一化 intent；非法值或 ``None`` 降级为 ``default``。

    Args:
        intent: 模型给的 intent 字符串，大小写不敏感。
        default: 非法时返回的兜底意图，默认 ``"NARRATING"``。

    Returns:
        合法的 intent 名（已大写）。
    """

    if not intent:
        return default
    upper = str(intent).strip().upper()
    return upper if upper in VALID_INTENTS else default


def split_emotion(
    emotion: str | None,
    *,
    default_type: str = "neutral",
    default_level: int = 2,
) -> tuple[str, int]:
    """把 ``"happy:2"`` 解析为 ``("happy", 2)``。

    - 没有 ``:level`` 时使用 ``default_level``；
    - level 越界时按 ``[1, 3]`` 截断；
    - 主类型不在 :data:`VALID_EMOTION_TYPES` 中时降级为 ``default_type``。

    Args:
        emotion: ``"类型:强度"`` 字符串。
        default_type: 非法主类型时的兜底，默认 ``"neutral"``。
        default_level: 缺省强度，默认 ``2``。

    Returns:
        ``(主类型, 强度)`` 元组。
    """

    if not emotion:
        return (default_type, default_level)
    parts = str(emotion).strip().lower().split(":", 1)
    main = parts[0] if parts[0] in VALID_EMOTION_TYPES else default_type
    if len(parts) > 1 and parts[1].strip().isdigit():
        level = int(parts[1])
    else:
        level = default_level
    level = max(1, min(3, level))
    return (main, level)


__all__ = [
    "CHATTER_SIGNATURE",
    "VALID_EMOTION_TYPES",
    "VALID_INTENTS",
    "normalize_intent",
    "split_emotion",
]
