"""本次运行已唱歌曲的历史记录。

注入到 ``sing_song`` 的 schema 描述里，让模型选歌时看到"最近已唱"，避免短
时间内重复唱同一首。仅内存保存，重启清空——一场直播一般就一个运行周期，
足够防重复。

与 :mod:`.call_state` / :mod:`.pipeline_state` 一致，用模块级单例 + 锁。
读接口 :func:`format_recent_block` 会在 ``to_schema()`` 这条**同步**路径上被
调用，因此提供一份不加锁的快照读——列表的原子 append / remove 在 CPython 下
不会让读方看到损坏的中间态。
"""

from __future__ import annotations

import asyncio


__all__ = ["clear", "format_recent_block", "record"]


# 已唱歌名，按播放先后排列（最新在末尾）。
_history: list[str] = []
_lock = asyncio.Lock()

# 注入描述时最多展示的"最近已唱"条数，太长会挤占 schema。
_SHOW_LIMIT = 10


async def record(song_name: str) -> None:
    """把刚唱的歌名记入历史。

    已存在的歌名会被移到末尾，使其成为"最近一首"。

    Args:
        song_name: 歌名；空串或纯空白会被忽略。
    """

    name = (song_name or "").strip()
    if not name:
        return
    async with _lock:
        if name in _history:
            _history.remove(name)
        _history.append(name)


async def clear() -> None:
    """清空唱歌历史。插件卸载时调用。"""

    async with _lock:
        _history.clear()


def format_recent_block() -> str:
    """构造"最近已唱"提示块，供 ``sing_song`` 的 schema 描述拼接。

    Returns:
        渲染好的提示块；历史为空时返回空串（不注入）。
    """

    if not _history:
        return ""
    # 末尾是最新，展示时倒序（最近唱的排最前）更直观。
    recent = _history[-_SHOW_LIMIT:]
    recent_desc = "、".join(f"《{name}》" for name in reversed(recent))
    return (
        "\n\n【最近已唱】（本次直播已经唱过，按时间从近到远）：\n"
        f"{recent_desc}\n"
        "除非观众明确点名要再听一遍，否则**优先选还没唱过的歌**，避免短时间重复。"
    )
