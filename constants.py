"""anima_chatter 共享常量与归一化工具。

本模块汇总了原先散落在 4 个文件里的 ``_VALID_INTENTS``、3 处 emotion / intent
解析逻辑，以及 3 处 ``_CHATTER_SIGNATURE``，供插件其它模块统一引用，避免出
现"新增 intent 必须两边都加"这类同步契约。

不依赖 ``src.kernel.*`` / ``src.core.*`` 任何模块，可被 schema 序列化阶段直
接 import（避免引入 vts 子模块的副作用）。

intent 元数据的权威定义在 :data:`INTENT_REGISTRY`；:data:`VALID_INTENTS`
和 [`scenes.py`](prompts/scenes.py) 的三份文案（VTB / LIVE / Schema）全部从
它派生。新增 intent 只改 ``INTENT_REGISTRY`` 一处即可。
"""

from __future__ import annotations

from dataclasses import dataclass


# ── chatter 签名常量 ─────────────────────────────────────────
# anima_chatter 插件的 chatter 组件签名。``register_active_chatter`` /
# ``get_chatter_class`` 等公开 API 用此字符串定位本插件的 chatter 类。
CHATTER_SIGNATURE: str = "anima_chatter:chatter:anima_chatter"


# ── intent 元数据注册表 ─────────────────────────────────────
# 与 [`SpeechAnimator.intent_map`](vts/animation/speech.py:103) 严格对齐——
# 那里是动画姿态字典的权威定义（姿态偏移量），本表是面向 LLM 的文案层元数据。
# 两者的 keys 必须一致（单元测试硬性要求，见 test/plugins/anima_chatter）。
#
# 新增 intent：在 SpeechAnimator.intent_map 加姿态偏移量，再在这里加一行，
# scenes.py 的 VTB / LIVE / Schema 三份文案会自动同步。


@dataclass(frozen=True, slots=True)
class IntentMeta:
    """单个 intent 的元数据。

    Args:
        name: intent 标识符（大写），与 ``SpeechAnimator.intent_map`` 的 key 一致。
        group: 分组名，用于在场景文案里按"基础姿态 / 眼神方向"等归类排版。
        desc_general: 通用一句话描述（不区分场景），用于 schema 精简版。
        desc_vtb: VTB 场景下的描述（私聊 / 群聊语境）。
        desc_live: 直播场景下的描述（弹幕 / 观众互动语境）。
    """

    name: str
    group: str
    desc_general: str
    desc_vtb: str
    desc_live: str


INTENT_REGISTRY: list[IntentMeta] = [
    # ── 基础姿态 ──────────────────────────────────────────
    IntentMeta(
        name="IDLE",
        group="基础姿态",
        desc_general="静止",
        desc_vtb="静止",
        desc_live="静止，听弹幕但不说话",
    ),
    IntentMeta(
        name="NARRATING",
        group="基础姿态",
        desc_general="叙述，默认",
        desc_vtb="叙述，默认",
        desc_live="默认叙述 / 回应弹幕",
    ),
    IntentMeta(
        name="THINKING",
        group="基础姿态",
        desc_general="思考，头微抬眼神上飘",
        desc_vtb="思考，头微抬眼神上飘",
        desc_live="思考，被问到难题",
    ),
    IntentMeta(
        name="CONFUSED",
        group="基础姿态",
        desc_general="困惑，歪头眯眼",
        desc_vtb="困惑，歪头眯眼",
        desc_live="困惑，看不懂梗或弹幕",
    ),
    # ── 高表现力情绪 ──────────────────────────────────────
    IntentMeta(
        name="EXCITED",
        group="高表现力情绪",
        desc_general="兴奋/赞同，前倾抬头眼神发亮",
        desc_vtb="兴奋/赞同，前倾抬头眼神发亮",
        desc_live="兴奋/赞同，看到精彩弹幕",
    ),
    IntentMeta(
        name="SURPRISED",
        group="高表现力情绪",
        desc_general="惊讶/意外，大抬头瞪眼",
        desc_vtb="惊讶/意外，大抬头瞪眼",
        desc_live="惊讶/意外，被弹幕逗到或被打赏",
    ),
    # ── 眼神方向 ──────────────────────────────────────────
    IntentMeta(
        name="PEEK_LEFT",
        group="眼神方向",
        desc_general="偷瞄左",
        desc_vtb="偷瞄左",
        desc_live='偷瞄左，回应"左边那位"这种弹幕方位词',
    ),
    IntentMeta(
        name="PEEK_RIGHT",
        group="眼神方向",
        desc_general="偷瞄右",
        desc_vtb="偷瞄右",
        desc_live='偷瞄右，回应"右边那位"这种弹幕方位词',
    ),
    IntentMeta(
        name="LOOKAWAY",
        group="眼神方向",
        desc_general="害羞回避，左下看",
        desc_vtb="害羞回避，左下看",
        desc_live="害羞回避，被夸了不好意思",
    ),
    IntentMeta(
        name="STARE_DOWN",
        group="眼神方向",
        desc_general="低头盯 / 沮丧",
        desc_vtb="低头盯 / 沮丧",
        desc_live="低头沉思 / 落寞",
    ),
    IntentMeta(
        name="DREAMY_GAZE",
        group="眼神方向",
        desc_general="神游远眺",
        desc_vtb="神游远眺",
        desc_live="神游远眺，话题感想",
    ),
    # ── 态度倾向 ──────────────────────────────────────────
    IntentMeta(
        name="PROUD_LIFT",
        group="态度倾向",
        desc_general="得意抬头",
        desc_vtb="得意抬头",
        desc_live="得意抬头，被吹捧时玩笑式自夸",
    ),
    IntentMeta(
        name="WORRIED_TILT",
        group="态度倾向",
        desc_general="担心歪头",
        desc_vtb="担心歪头",
        desc_live="担心歪头，关心观众情绪",
    ),
    IntentMeta(
        name="SHY_DOWN",
        group="态度倾向",
        desc_general="害羞低头偏侧",
        desc_vtb="害羞低头偏侧",
        desc_live="害羞低头，被表白 / 大额 SC 时",
    ),
    IntentMeta(
        name="ATTENTIVE",
        group="态度倾向",
        desc_general="认真专注",
        desc_vtb="认真专注",
        desc_live="认真专注，听观众讲故事",
    ),
    # ── 调皮 / 紧张 ───────────────────────────────────────
    IntentMeta(
        name="PLAYFUL_TILT",
        group="调皮 / 紧张",
        desc_general="调皮明显歪头",
        desc_vtb="调皮明显歪头",
        desc_live="调皮歪头，玩笑话",
    ),
    IntentMeta(
        name="MISCHIEF",
        group="调皮 / 紧张",
        desc_general="坏笑斜眼",
        desc_vtb="坏笑斜眼",
        desc_live="坏笑斜眼，黑色幽默",
    ),
    IntentMeta(
        name="SCARED_SHRINK",
        group="调皮 / 紧张",
        desc_general="害怕收身",
        desc_vtb="害怕收身",
        desc_live="害怕收身，遇到吓人话题",
    ),
]


# ── 合法 intent 集（从 INTENT_REGISTRY 派生） ────────────────
VALID_INTENTS: frozenset[str] = frozenset(m.name for m in INTENT_REGISTRY)


# ── 合法 emotion 主类型 ─────────────────────────────────────
# 与 [`SpeechAnimator.emotion_matrix`](vts/animation/speech.py:134) 对齐。
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
    "INTENT_REGISTRY",
    "IntentMeta",
    "VALID_EMOTION_TYPES",
    "VALID_INTENTS",
    "normalize_intent",
    "split_emotion",
]
