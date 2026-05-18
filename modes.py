"""anima_chatter 三态运行模式中枢。

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
"""anima_chatter 三态运行模式：

- ``voice``：``platform == "local_asr"``，本地 ASR 实时通话；或在通话进行中
  接管了原 stream（如 QQ 私聊）的 anima_chatter。
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

    1. **该 stream 当前正处于 voice_call 通话中** → 强制 :data:`voice`
       （anima_chatter 临时接管原 stream，platform 仍是 qq / discord 等，
       但行为要按 voice 通话来）
    2. ``platform == "local_asr"`` → :data:`voice`
    3. ``platform`` 在 :data:`LIVE_PLATFORMS` 中 → :data:`vtb_live`
    4. 其他 → :data:`vtb`

    优先级 1 的实现：异步查询 :mod:`.call_state`。本函数是同步的——直接尝试
    从事件循环里跑 ``asyncio.ensure_future`` 不好控制；改成读模块级变量的
    "快照视图"。:mod:`.call_state` 内部的 ``_lock`` 只保护写路径，读 ``_active_call``
    单变量在 CPython 下是原子的，对优先级 1 这种"判定 + 立即用"的场景已经
    足够稳；没必要为这条同步快路径让整个函数变成 async。
    """

    # ── 优先级 1：通话中的 stream 强制 voice ─────────
    # 直接读模块级单例，不走锁——anima_chatter 自己的 runner 持续
    # poll，不会出现"读到旧值导致模式判错一拍"的严重后果。
    from . import call_state  # 局部导入避免循环依赖（call_state 不依赖 modes）

    active = call_state._active_call  # noqa: SLF001 — 同模块快照读
    if active is not None and active.caller_stream_id == (chat_stream.stream_id or ""):
        return "voice"

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
