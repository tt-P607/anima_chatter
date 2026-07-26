"""anima_chatter 的 Action 集合。

按使用场景分组（实际可见性由各自的 ``go_activate`` 决定，模型在任意时刻只会
看到与当前状态匹配的动作）：

- **voice 模式**（``platform == "local_asr"`` 或通话进行中）：
  :class:`SayAction`、:class:`AnimaPassAndWaitAction`、:class:`EndVoiceCallAction`
- **vtb 模式**（其他平台，由 ``/vtb on`` 接管）：
  :class:`SayAndPerformAction`、:class:`AnimaPassAndWaitAction`
- **vtb_live 模式**：额外提供 :class:`SingSongAction`
- **跨模式**：:class:`StartVoiceCallAction`（在私聊中发起本地语音通话）

所有 Action 只负责解析参数与准备音频来源，播放逻辑统一走
[`speech/playback.py`](../speech/playback.py:1)。
"""

from __future__ import annotations

from .pass_and_wait import AnimaPassAndWaitAction
from .say import SayAction
from .say_and_perform import SayAndPerformAction
from .sing_song import SingSongAction
from .voice_call import EndVoiceCallAction, StartVoiceCallAction


__all__ = [
    "AnimaPassAndWaitAction",
    "EndVoiceCallAction",
    "SayAction",
    "SayAndPerformAction",
    "SingSongAction",
    "StartVoiceCallAction",
]
