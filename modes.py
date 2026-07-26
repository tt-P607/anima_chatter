"""anima_chatter 三态运行模式判定。

把"什么 platform 进入什么模式"的判定收敛在这一个文件里。新增运行模式时：

1. 新增**平台**（如再接一个直播源）：在 :data:`LIVE_PLATFORMS` 加一项即可。
2. 新增**模式**：在 :data:`ChatterMode` Literal 追加一项，同步更新
   :func:`resolve_mode` 的分支与 [`prompts/scenes.py`](prompts/scenes.py:1) 的
   场景文案。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from .runtime import call_state

if TYPE_CHECKING:
    from src.app.plugin_system.types import ChatStream


__all__ = [
    "ChatterMode",
    "LIVE_PLATFORMS",
    "VOICE_PLATFORM",
    "resolve_mode",
]


ChatterMode = Literal["voice", "vtb", "vtb_live"]
"""anima_chatter 三态运行模式：

- ``voice``：本地 ASR 实时通话；或在通话进行中接管了原 stream（如 QQ 私聊）。
- ``vtb``：被 ``/vtb on`` 接管的普通群聊 / 私聊，VTube Studio 表演但不在直播。
- ``vtb_live``：直播平台，观众是陌生弹幕、消息只入不出，按直播间礼仪行事。
"""


VOICE_PLATFORM = "local_asr"
"""ASR 实时通话使用的 platform 字符串（与 ``asr_adapter_anima`` 一致）。"""


LIVE_PLATFORMS: frozenset[str] = frozenset({"live"})
"""被识别为"直播"的 ``adapter.platform`` 字符串集合。

为了让多平台直播（B 站 + 抖音 + 未来的 Twitch / YouTube 等）能合并到**同一个
chat_stream**、由 anima_chatter 串行决策（避免两边 chatter 同时触发 VTS hotkey
打架），所有直播 adapter 都把 ``platform`` 类属性写成统一的 ``"live"``。真实
来源由 envelope 的 ``additional_config.source_platform`` 携带，能在 prompt 里
区分（详见 [`prompts/scenes.py`](prompts/scenes.py:1)）。
"""


ASSOCIATED_PLATFORMS: list[str] = [VOICE_PLATFORM, *sorted(LIVE_PLATFORMS)]
"""本 chatter 声明关联的平台列表，供 ``BaseChatter.associated_platforms`` 使用。"""


def resolve_mode(chat_stream: "ChatStream") -> ChatterMode:
    """根据流的状态与 platform 判定运行模式。

    判定优先级：

    1. **该 stream 当前正处于通话中** → ``voice``（anima_chatter 临时接管原
       stream，platform 仍是 qq / discord 等，但行为要按通话来）
    2. ``platform == "local_asr"`` → ``voice``
    3. ``platform`` 在 :data:`LIVE_PLATFORMS` 中 → ``vtb_live``
    4. 其他 → ``vtb``

    优先级 1 走 :func:`call_state.snapshot_active_call_unlocked` 这条同步快路径
    ——本函数被 prompt 构建等同步调用链使用，不能 ``await``。

    Args:
        chat_stream: 当前聊天流。

    Returns:
        判定出的运行模式。
    """

    active = call_state.snapshot_active_call_unlocked()
    if active is not None and active.caller_stream_id == (chat_stream.stream_id or ""):
        return "voice"

    platform = (chat_stream.platform or "").strip()
    if platform == VOICE_PLATFORM:
        return "voice"
    if platform in LIVE_PLATFORMS:
        return "vtb_live"
    return "vtb"
