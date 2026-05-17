"""voice_chatter 三态运行模式中枢。

把"什么 platform 进入什么模式"的判定收敛在这一个文件里。除了 :data:`ChatterMode`
本身和 :func:`resolve_mode`，还导出 :data:`LIVE_PLATFORMS`（直播平台白名单）
供其它模块读取——比如插件 + 直播适配器要保持平台名一致时就能直接 import。

新增运行模式时（如新增 Twitch / YouTube 直播平台），改这里就够了：

1. 在 :data:`LIVE_PLATFORMS` 里加上对应 ``adapter.platform`` 字符串。
2. 如果是新模式（不只是新平台），还要在 :data:`ChatterMode` Literal 里追加一
   项，并同步更新 :func:`resolve_mode` 的分支与 prompts 目录下的场景文案。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from src.core.models.stream import ChatStream


ChatterMode = Literal["voice", "vtb", "vtb_live"]
"""voice_chatter 三态运行模式：

- ``voice``：``platform == "local_asr"``，本地 ASR 实时通话。
- ``vtb``：被 ``/vtb on`` 接管的普通群聊 / 私聊，VTube Studio 表演但不在直播。
- ``vtb_live``：直播平台（``platform`` 在 :data:`LIVE_PLATFORMS` 中），
  观众是陌生弹幕、消息只入不出，要按直播间礼仪行事。
"""


LIVE_PLATFORMS: frozenset[str] = frozenset({"bilibili_live"})
"""被识别为"直播"的 ``adapter.platform`` 字符串集合。

必须与对应 adapter 类的 ``platform`` 类属性保持一致，例如
:class:`plugins.bilibili_live_adapter.plugin.BilibiliLiveAdapter.platform`。
"""


VOICE_PLATFORM = "local_asr"
"""ASR 实时通话使用的 platform 字符串（与 ``asr_adapter`` 一致）。"""


def resolve_mode(chat_stream: "ChatStream") -> ChatterMode:
    """根据流的 platform 自动判定运行模式。

    判定优先级：

    1. ``platform == "local_asr"`` → :data:`voice`
    2. ``platform`` 在 :data:`LIVE_PLATFORMS` 中 → :data:`vtb_live`
    3. 其他 → :data:`vtb`
    """

    platform = (chat_stream.platform or "").strip()
    if platform == VOICE_PLATFORM:
        return "voice"
    if platform in LIVE_PLATFORMS:
        return "vtb_live"
    return "vtb"


__all__ = [
    "ChatterMode",
    "LIVE_PLATFORMS",
    "VOICE_PLATFORM",
    "resolve_mode",
]
