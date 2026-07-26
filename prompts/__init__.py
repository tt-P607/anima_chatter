"""anima_chatter 提示词子包。

按职责分三层：

- :mod:`.scenes` — 三种模式各自的场景与工具协议文案 + action schema 共用描述。
- :mod:`.templates` — system prompt、统一的 user prompt 模板，以及每个模式的
  标题 / 段头 / 尾部指令配置。
- :mod:`.builder` — :class:`AnimaChatterPromptBuilder`，把上面两层组装起来。
"""

from __future__ import annotations

from .builder import AnimaChatterPromptBuilder
from .scenes import (
    EMOTION_SCHEMA_DESC,
    INTENT_SCHEMA_DESC,
    VOICE_SCENE_GUIDE,
    VTB_SCENE_GUIDE,
    build_vtb_live_scene_guide,
)
from .templates import (
    MODE_PROMPT_PROFILES,
    PLAIN_TEXT_REMINDER_VOICE,
    PLAIN_TEXT_REMINDER_VTB,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    ModePromptProfile,
)


__all__ = [
    "AnimaChatterPromptBuilder",
    "EMOTION_SCHEMA_DESC",
    "INTENT_SCHEMA_DESC",
    "MODE_PROMPT_PROFILES",
    "ModePromptProfile",
    "PLAIN_TEXT_REMINDER_VOICE",
    "PLAIN_TEXT_REMINDER_VTB",
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
    "VOICE_SCENE_GUIDE",
    "VTB_SCENE_GUIDE",
    "build_vtb_live_scene_guide",
]
