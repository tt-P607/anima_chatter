"""``VTＳConnection`` 独立事件循环线程的行为测试。

验证方案 A 的核心：VTS 的动画 / 发送 / 心跳循环跑在**独立线程的私有事件循环**
里，主程序阻塞不影响 worker 循环推进；对外桥接接口（connect / close /
trigger_hotkey / set_expression）兼容。
"""

from __future__ import annotations

import asyncio
import threading
import time
import pytest

from plugins.anima_chatter.vts.connection import VTSConnection


@pytest.fixture
def conn() -> "VTSConnection":
    """构造一个不真正连 VTS 的连接实例。"""
    return VTSConnection(host="127.0.0.1", port=8001)


def test_worker_loop_self_heals_after_close(conn: "VTSConnection") -> None:
    """验证循环在 close 回收后可再次启动（自愈）。"""
    loop = conn._ensure_worker_loop()
    assert loop is not None
    assert not loop.is_closed()
    first_loop = conn._worker_loop

    # 回收后再次获取应得到新的可用循环（线程重启）。
    conn._shutdown_worker()
    loop2 = conn._ensure_worker_loop()
    assert loop2 is not None
    assert not loop2.is_closed()
    assert loop2 is not first_loop
    conn._shutdown_worker()


def test_run_coro_crosses_thread(conn: "VTSConnection") -> None:
    """验证 _run_coro 能把协程投递到 worker 循环并拿到结果。"""
    result = conn._run_coro(_add_async(2, 3), timeout=5.0)
    assert result == 5
    conn._shutdown_worker()


def test_worker_pushes_while_main_blocked(conn: "VTSConnection") -> None:
    """方案 A 核心：主线程阻塞时，worker 循环仍按计划推进。

    在主线程 sleep 阻塞 0.5s 期间，worker 循环里用一个定时器累加计数；
    验收 worker 循环未被主线程阻塞拖住，计数仍在推进。
    """
    loop = conn._ensure_worker_loop()
    counter: dict[str, int] = {"n": 0}

    def _tick() -> None:
        counter["n"] += 1

    # 在 worker 循环里每 0.02s 调度一次 tick，持续 0.6s。
    stop_event = threading.Event()

    async def _schedule() -> None:
        while not stop_event.is_set():
            loop.call_later(0.02, _tick)
            await asyncio.sleep(0.02)

    fut = asyncio.run_coroutine_threadsafe(_schedule(), loop)

    # 主线程阻塞 0.5s（模拟主程序卡顿）。
    time.sleep(0.5)
    stop_event.set()
    fut.result(timeout=2.0)

    # 0.5s / 0.02s ≈ 25 次；允许调度延迟留足余量，应远大于"完全被阻塞"的 0~1 次。
    assert counter["n"] >= 10, f"worker 循环被主线程阻塞拖住: n={counter['n']}"
    conn._shutdown_worker()


def test_trigger_hotkey_returns_false_when_not_connected(
    conn: "VTSConnection",
) -> None:
    """未连接时 trigger_hotkey / set_expression 应安全返回 False。"""

    async def _probe() -> None:
        assert await conn.trigger_hotkey("some_hotkey") is False
        assert await conn.set_expression("expr.exp3.json", True) is False

    asyncio.run(_probe())
    conn._shutdown_worker()


async def _add_async(a: int, b: int) -> int:
    """一个简单的可跨线程投递的协程，用于验证桥接。"""
    return a + b


__all__: list[str] = []
