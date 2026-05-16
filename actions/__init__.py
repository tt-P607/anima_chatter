"""voice_chatter 插件 Action 集合。

按使用场景拆成两组：

- voice 模式（``platform == "local_asr"``）：
    - :class:`SayAction`
    - :class:`VoicePassAndWaitAction`

- vtb 模式（其他平台，由 ``/vtb on`` 显式接管）：
    - :class:`SayAndPerformAction`
    - :class:`VoicePassAndWaitAction`

两组的 :meth:`go_activate` 互斥，模型在任意时刻只会看到当前模式对应的动作。
"""

from .pass_and_wait import VoicePassAndWaitAction
from .say import SayAction
from .say_and_perform import SayAndPerformAction

__all__ = [
    "SayAction",
    "SayAndPerformAction",
    "VoicePassAndWaitAction",
]
