"""anima_chatter 提示词模块。

按职责拆分：

- :mod:`.scenes` — 三种模式各自的 ``<scene_and_protocol>`` 文案 + action
  schema 共用描述（INTENT / EMOTION / LANGUAGE）。
- :mod:`.templates` — system prompt + 统一的 user prompt 模板，以及每个模式
  的 :class:`ModePromptProfile` 配置（标题 / 段头 / 尾部指令）。
- :mod:`.builder` — :class:`AnimaChatterPromptBuilder`，把上面两层组装起来。

外部模块统一从 ``plugins.anima_chatter.prompts`` 顶层 import 即可。
"""

from __future__ import annotations

from .builder import AnimaChatterPromptBuilder
from .scenes import (
    EMOTION_SCHEMA_DESC,
    INTENT_SCHEMA_DESC,
    LANGUAGE_SCHEMA_DESC,
    VOICE_SCENE_GUIDE,
    VTB_LIVE_SCENE_GUIDE,
    VTB_SCENE_GUIDE,
)
from .templates import (
    MODE_PROMPT_PROFILES,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    ModePromptProfile,
)


__all__ = [
    # 模板字符串（plugin.py 在 on_plugin_loaded 注册时使用）
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
    "MODE_PROMPT_PROFILES",
    "ModePromptProfile",
    # action schema 通用描述
    "EMOTION_SCHEMA_DESC",
    "INTENT_SCHEMA_DESC",
    "LANGUAGE_SCHEMA_DESC",
    # 场景文案（一般不用直接 import，留给单测 / 调试）
    "VOICE_SCENE_GUIDE",
    "VTB_LIVE_SCENE_GUIDE",
    "VTB_SCENE_GUIDE",
    # 主入口
    "AnimaChatterPromptBuilder",
]
