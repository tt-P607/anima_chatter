"""anima_chatter 提示词模块。

按职责拆分：

- :mod:`.scenes` — 三种模式各自的 ``<scene_and_protocol>`` 文案。
- :mod:`.templates` — system prompt + 三个 user prompt 模板字符串。
- :mod:`.builder` — :class:`AnimaChatterPromptBuilder`，把上面两层组装起来。

外部模块统一从 ``plugins.anima_chatter.prompts`` 顶层 import 即可。
"""

from __future__ import annotations

from .builder import AnimaChatterPromptBuilder
from .scenes import VOICE_SCENE_GUIDE, VTB_LIVE_SCENE_GUIDE, VTB_SCENE_GUIDE
from .templates import (
    SYSTEM_PROMPT,
    USER_PROMPT_VOICE,
    USER_PROMPT_VTB,
    USER_PROMPT_VTB_LIVE,
)


__all__ = [
    # 模板字符串（plugin.py 在 on_plugin_loaded 注册时使用）
    "SYSTEM_PROMPT",
    "USER_PROMPT_VOICE",
    "USER_PROMPT_VTB",
    "USER_PROMPT_VTB_LIVE",
    # 场景文案（一般不用直接 import，留给单测 / 调试）
    "VOICE_SCENE_GUIDE",
    "VTB_LIVE_SCENE_GUIDE",
    "VTB_SCENE_GUIDE",
    # 主入口
    "AnimaChatterPromptBuilder",
]
