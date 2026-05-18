"""anima_chatter 插件 Action 集合。

按使用场景拆成三组：

- voice 模式（``platform == "local_asr"`` 或通话进行中）：
    - :class:`SayAction`
    - :class:`AnimaPassAndWaitAction`
    - :class:`EndVoiceCallAction`

- vtb 模式（其他平台，由 ``/vtb on`` 显式接管）：
    - :class:`SayAndPerformAction`
    - :class:`AnimaPassAndWaitAction`

- 跨模式（任何 chatter 都可调用）：
    - :class:`StartVoiceCallAction` — 在私聊里发起本地语音通话

各 action 的 :meth:`go_activate` 决定可见性，模型在任意时刻只会看到与当前
状态匹配的动作。
"""

from .pass_and_wait import AnimaPassAndWaitAction
from .say import SayAction
from .say_and_perform import SayAndPerformAction
from .voice_call import EndVoiceCallAction, StartVoiceCallAction

__all__ = [
    "EndVoiceCallAction",
    "SayAction",
    "SayAndPerformAction",
    "StartVoiceCallAction",
    "AnimaPassAndWaitAction",
]
