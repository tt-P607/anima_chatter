"""动画器子包：眨眼/呼吸/眼神 + 说话情感联动。"""

from .auto import AutoAnimator
from .base import BaseAnimator
from .speech import SpeechAnimator

__all__ = ["AutoAnimator", "BaseAnimator", "SpeechAnimator"]
