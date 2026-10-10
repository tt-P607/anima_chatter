"""直播场景、模板及构建器的公开入口。"""

from __future__ import annotations

from .builder import SPEECH_RULES_TEMPLATE_NAME, AnimaChatterPromptBuilder
from .scenes import (
    EMOTION_SCHEMA_DESC,
    INTENT_SCHEMA_DESC,
    build_vtb_live_scene_guide,
)
from .templates import (
    LIVE_USER_PROMPT_PROFILE,
    PLAIN_TEXT_REMINDER_VTB,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    LivePromptProfile,
)

__all__ = [
    "EMOTION_SCHEMA_DESC",
    "INTENT_SCHEMA_DESC",
    "LIVE_USER_PROMPT_PROFILE",
    "PLAIN_TEXT_REMINDER_VTB",
    "SPEECH_RULES_TEMPLATE_NAME",
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
    "AnimaChatterPromptBuilder",
    "LivePromptProfile",
    "build_vtb_live_scene_guide",
]
