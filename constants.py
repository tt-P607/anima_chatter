"""anima_chatter 共享常量与归一化工具。

汇总 intent / emotion 的合法值定义与归一化逻辑，以及 chatter 组件签名，供插
件其它模块统一引用，避免出现"新增 intent 必须两处都加"这类同步契约。

本模块不 import 任何框架模块，可被 schema 序列化阶段直接引入（避免连带引入
vts 子包的副作用）。

intent 元数据的权威定义在 :data:`INTENT_REGISTRY`；:data:`VALID_INTENTS` 和
[`prompts/scenes.py`](prompts/scenes.py:1) 的 VTB / LIVE / Schema 三份文案全
部从它派生。新增 intent 只改 ``INTENT_REGISTRY`` 一处即可。
"""

from __future__ import annotations

from dataclasses import dataclass


__all__ = [
    "CHATTER_SIGNATURE",
    "INTENT_REGISTRY",
    "IntentMeta",
    "VALID_EMOTION_TYPES",
    "VALID_INTENTS",
    "normalize_intent",
    "split_emotion",
]


CHATTER_SIGNATURE: str = "anima_chatter:chatter:anima_chatter"
"""本插件 chatter 组件的签名，供 ``chat_api`` 定位 chatter 类。"""


# ── intent 元数据注册表 ─────────────────────────────────────
# 与 SpeechAnimator.intent_map 严格对齐——那里是动画姿态字典的权威定义（姿态
# 偏移量），本表是面向 LLM 的文案层元数据。两者的 keys 必须一致，由
# test/test_constants.py 强制校验。
#
# 新增 intent：先在 SpeechAnimator.intent_map 加姿态偏移量，再在这里加一行，
# scenes.py 的三份文案会自动同步。


@dataclass(frozen=True, slots=True)
class IntentMeta:
    """单个 intent 的元数据。

    Attributes:
        name: intent 标识符（大写），与 ``SpeechAnimator.intent_map`` 的 key 一致。
        group: 分组名，用于在场景文案里按"基础姿态 / 眼神方向"等归类排版。
        desc_vtb: VTB 场景下的描述（私聊 / 群聊语境），同时用于 schema 精简版。
        desc_live: 直播场景下的描述（弹幕 / 观众互动语境）。
    """

    name: str
    group: str
    desc_vtb: str
    desc_live: str


INTENT_REGISTRY: list[IntentMeta] = [
    # ── 基础姿态 ──────────────────────────────────────────
    IntentMeta(
        name="IDLE",
        group="基础姿态",
        desc_vtb="静止",
        desc_live="静止，听弹幕但不说话",
    ),
    IntentMeta(
        name="NARRATING",
        group="基础姿态",
        desc_vtb="叙述，默认",
        desc_live="默认叙述 / 回应弹幕",
    ),
    IntentMeta(
        name="THINKING",
        group="基础姿态",
        desc_vtb="思考，头微抬眼神上飘",
        desc_live="思考，被问到难题",
    ),
    IntentMeta(
        name="CONFUSED",
        group="基础姿态",
        desc_vtb="困惑，歪头眯眼",
        desc_live="困惑，看不懂梗或弹幕",
    ),
    # ── 高表现力情绪 ──────────────────────────────────────
    IntentMeta(
        name="EXCITED",
        group="高表现力情绪",
        desc_vtb="兴奋/赞同，前倾抬头眼神发亮",
        desc_live="兴奋/赞同，看到精彩弹幕",
    ),
    IntentMeta(
        name="SURPRISED",
        group="高表现力情绪",
        desc_vtb="惊讶/意外，大抬头瞪眼",
        desc_live="惊讶/意外，被弹幕逗到或被打赏",
    ),
    # ── 眼神方向 ──────────────────────────────────────────
    IntentMeta(
        name="PEEK_LEFT",
        group="眼神方向",
        desc_vtb="偷瞄左",
        desc_live='偷瞄左，回应"左边那位"这种弹幕方位词',
    ),
    IntentMeta(
        name="PEEK_RIGHT",
        group="眼神方向",
        desc_vtb="偷瞄右",
        desc_live='偷瞄右，回应"右边那位"这种弹幕方位词',
    ),
    IntentMeta(
        name="LOOKAWAY",
        group="眼神方向",
        desc_vtb="害羞回避，左下看",
        desc_live="害羞回避，被夸了不好意思",
    ),
    IntentMeta(
        name="STARE_DOWN",
        group="眼神方向",
        desc_vtb="低头盯 / 沮丧",
        desc_live="低头沉思 / 落寞",
    ),
    IntentMeta(
        name="DREAMY_GAZE",
        group="眼神方向",
        desc_vtb="神游远眺",
        desc_live="神游远眺，话题感想",
    ),
    # ── 态度倾向 ──────────────────────────────────────────
    IntentMeta(
        name="PROUD_LIFT",
        group="态度倾向",
        desc_vtb="得意抬头",
        desc_live="得意抬头，被吹捧时玩笑式自夸",
    ),
    IntentMeta(
        name="WORRIED_TILT",
        group="态度倾向",
        desc_vtb="担心歪头",
        desc_live="担心歪头，关心观众情绪",
    ),
    IntentMeta(
        name="SHY_DOWN",
        group="态度倾向",
        desc_vtb="害羞低头偏侧",
        desc_live="害羞低头，被表白 / 大额 SC 时",
    ),
    IntentMeta(
        name="ATTENTIVE",
        group="态度倾向",
        desc_vtb="认真专注",
        desc_live="认真专注，听观众讲故事",
    ),
    # ── 调皮 / 紧张 ───────────────────────────────────────
    IntentMeta(
        name="PLAYFUL_TILT",
        group="调皮 / 紧张",
        desc_vtb="调皮明显歪头",
        desc_live="调皮歪头，玩笑话",
    ),
    IntentMeta(
        name="MISCHIEF",
        group="调皮 / 紧张",
        desc_vtb="坏笑斜眼",
        desc_live="坏笑斜眼，黑色幽默",
    ),
    IntentMeta(
        name="SCARED_SHRINK",
        group="调皮 / 紧张",
        desc_vtb="害怕收身",
        desc_live="害怕收身，遇到吓人话题",
    ),
]


VALID_INTENTS: frozenset[str] = frozenset(meta.name for meta in INTENT_REGISTRY)
"""合法 intent 集合，从 :data:`INTENT_REGISTRY` 派生。"""


VALID_EMOTION_TYPES: frozenset[str] = frozenset(
    {"neutral", "happy", "sad", "angry", "surprised"}
)
"""合法 emotion 主类型集合，与 ``SpeechAnimator.emotion_matrix`` 的 keys 对齐。"""


# emotion 强度的合法区间。
_MIN_EMOTION_LEVEL = 1
_MAX_EMOTION_LEVEL = 3


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

    parts = [part.strip() for part in str(emotion).lower().split(":", 1)]
    main = parts[0] if parts[0] in VALID_EMOTION_TYPES else default_type
    level = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_level
    return (main, max(_MIN_EMOTION_LEVEL, min(_MAX_EMOTION_LEVEL, level)))
