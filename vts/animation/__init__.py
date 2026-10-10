"""动画器子包：眨眼/呼吸/眼神 + 说话情感联动 + 运动学工具。"""

from .auto import AutoAnimator
from .base import BaseAnimator
from .dynamics import SecondOrderDynamics
from .speech import SpeechAnimator

__all__ = ["AutoAnimator", "BaseAnimator", "SecondOrderDynamics", "SpeechAnimator"]
