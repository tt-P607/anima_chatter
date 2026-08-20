"""VTube Studio WebSocket 连接管理。

封装 pyvts 的连接、认证、自定义参数注册、动画循环 + 参数发送循环。

**独立事件循环线程**：VTS 的动画 / 参数发送 / 心跳三个后台循环跑在一个
**独立线程 + 私有 asyncio 事件循环**里，与主程序的事件循环完全隔离。这样主程序
即便发生会阻塞主事件循环的操作（LLM 请求、音频解码、同步 I/O 等），动画与参数
发送也不会卡顿，从而避免 VTB 模型抽搐 / 卡顿。

对外接口（``connect`` / ``close`` / ``trigger_hotkey`` / ``set_expression``）
签名保持不变，主进程侧通过 :meth:`asyncio.run_coroutine_threadsafe` 把请求投递到
worker 循环执行并等待结果；因此上层调用方（VTSPerformer）无需任何改动。
"""

from __future__ import annotations

import asyncio
import math
import os
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from .animation.base import BaseAnimator


logger = get_logger("anima_chatter.vts.connection")


# 注入到 VTS 的自定义参数清单。所有动画器输出都通过这些参数生效。
_CUSTOM_PARAMETERS: list[dict[str, Any]] = [
    {"name": "v_eye_left", "min": 0, "max": 1, "def": 1},
    {"name": "v_eye_right", "min": 0, "max": 1, "def": 1},
    {"name": "v_head_x", "min": -30, "max": 30, "def": 0},
    {"name": "v_head_y", "min": -30, "max": 30, "def": 0},
    {"name": "v_head_z", "min": -30, "max": 30, "def": 0},
    {"name": "v_body_x", "min": -30, "max": 30, "def": 0},
    {"name": "v_body_y", "min": -30, "max": 30, "def": 0},
    {"name": "v_body_z", "min": -30, "max": 30, "def": 0},
    {"name": "v_mouth_form", "min": -1, "max": 1, "def": 0},
    {"name": "v_eye_x", "min": -1, "max": 1, "def": 0},
    {"name": "v_eye_y", "min": -1, "max": 1, "def": 0},
    {"name": "v_eye_smile_l", "min": 0, "max": 1, "def": 0},
    {"name": "v_eye_smile_r", "min": 0, "max": 1, "def": 0},
    {"name": "v_brow_y_l", "min": -1, "max": 1, "def": 0},
    {"name": "v_brow_y_r", "min": -1, "max": 1, "def": 0},
    {"name": "v_brow_form_l", "min": -1, "max": 1, "def": 0},
    {"name": "v_brow_form_r", "min": -1, "max": 1, "def": 0},
    {"name": "v_blush", "min": 0, "max": 1, "def": 0},
]


# 名字 → (min, max) 范围表，由 _CUSTOM_PARAMETERS 派生。sender_loop 发送前
# 用这张表对动画器产出的每个值做截断 + NaN/Inf 过滤——这是防止 VTS 主动断连
# (received 1002 protocol error) 的关键保护层。
#
# VTS 一旦收到包含 NaN / Inf / 超出注册声明 min/max 的 SetMultiParameterValue
# 帧，会直接发 close(1002) 终止 ws。在 60Hz 高频发送 + 多动画器叠加（音频驱动
# velocity 求差分、ease_in_out 三角函数等）的链路里，**一帧的脏数据就够断一次**。
# 所以无论上游怎么犯傻，到了 sender 出去前必须先 sanitize。
_PARAM_RANGE: dict[str, tuple[float, float]] = {
    p["name"]: (float(p["min"]), float(p["max"])) for p in _CUSTOM_PARAMETERS
}


def _sanitize_params(params: dict[str, float]) -> dict[str, float]:
    """对参数批做合法化：丢弃未注册的 key、过滤 NaN/Inf、截断到 min~max。

    返回**可以安全发给 VTS** 的清洗后参数；调用方负责判空。
    """

    cleaned: dict[str, float] = {}
    for key, raw_value in params.items():
        bounds = _PARAM_RANGE.get(key)
        if bounds is None:
            # 未声明的参数直接丢，避免 VTS 因不识别参数名报错。
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isnan(value) or math.isinf(value):
            # NaN / Inf 是 VTS 主动 1002 的最常见原因。
            continue
        lo, hi = bounds
        if value < lo:
            value = lo
        elif value > hi:
            value = hi
        cleaned[key] = value
    return cleaned


class VTSConnection:
    """与 VTube Studio 维持 WebSocket 长连接，并跑动画循环。

    动画 / 发送 / 心跳循环运行在**独立线程的私有事件循环**里，与主程序事件循环
    隔离，从而主程序卡顿不影响动画发送节奏。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8001,
        plugin_name: str = "MoFox-Bot-VoiceChatter",
        developer: str = "MoFox Team",
        token_path: str | None = None,
    ) -> None:
        """初始化连接参数；token 文件用于免重复授权。

        事件循环 / 线程在首次需要时惰性启动（见 :meth:`_ensure_worker_loop`），
        避免实例化即开线程造成的资源浪费与测试噪音。
        """

        self.host = host
        self.port = port
        self.plugin_name = plugin_name
        self.developer = developer
        self.token_path = token_path

        # pyvts.vts 实例；保持 Any 避免在未安装 pyvts 的开发环境下出现导入错误。
        # 始终只被 worker 循环内的协程读写（connect / 心跳 / sender / 控制接口）。
        self.vts: Any = None
        self.is_connected: bool = False

        # 以下 asyncio primitives 首次 await 发生在 worker 循环内，因此会绑定到
        # worker 循环。它们绝不会被主线程直接 await；主线程只会通过
        # run_coroutine_threadsafe 把协程投递到 worker 循环后在其中执行，从而
        # 保证锁 / future 与循环一致。
        self._connect_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._buffer_lock = asyncio.Lock()
        self._param_buffer: dict[str, float] = {}

        # 独立事件循环线程的句柄与状态。
        self._worker_thread: threading.Thread | None = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._worker_stop: threading.Event = threading.Event()
        # 三循环的任务句柄（跑在 worker 循环里，用于关闭时取消）。
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._animation_task: asyncio.Task[Any] | None = None
        self._sender_task: asyncio.Task[Any] | None = None
        self._loop_started: threading.Event = threading.Event()

        self.animators: list[BaseAnimator] = []

    # ── 独立事件循环线程 ──────────────────────────────

    def _ensure_worker_loop(self) -> "asyncio.AbstractEventLoop":
        """确保 worker 线程 + 事件循环在运行；若已停止则重启。

        返回当前可用的 worker 事件循环。每次调用都校验线程存活与循环未关闭，
        保证任何桥接调用都不会把协程投递到已停摆的循环上。

        Returns:
            worker 循环实例。
        """
        if self._worker_thread is not None and self._worker_thread.is_alive():
            loop = self._worker_loop
            if loop is not None and not loop.is_closed():
                return loop
            # 线程活着但循环已关闭：清掉引用，走下方重建路径。
            self._worker_loop = None

        self._loop_started.clear()
        self._worker_stop = threading.Event()
        self._worker_loop = None

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._worker_loop = loop
            self._loop_started.set()
            try:
                loop.run_forever()
            finally:
                # 循环结束时清理 pending 任务，避免 "Task was destroyed" 噪音。
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:  # noqa: BLE001
                    pass
                loop.close()
                if self._worker_loop is loop:
                    self._worker_loop = None

        thread = threading.Thread(
            target=_run,
            name="anima_chatter.vts.worker",
            daemon=True,
        )
        thread.start()
        self._worker_thread = thread
        if not self._loop_started.wait(timeout=10):
            raise RuntimeError("VTS worker 事件循环启动超时")
        return self._worker_loop  # type: ignore[return-value]

    def _submit(self, coro: "Coroutine[Any, Any, Any]") -> "Future[Any]":
        """把协程投递到 worker 循环执行，返回可等待的 Future。

        Args:
            coro: 要在 worker 循环执行的协程。

        Returns:
            与 coro 结果绑定的 ``concurrent.futures.Future``。
        """
        loop = self._ensure_worker_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def _run_coro(self, coro: "Coroutine[Any, Any, Any]", timeout: float) -> Any:
        """同步等待 worker 循环上的协程执行完并返回结果。

        供主线程调用：阻塞当前线程直到 worker 循环里的 coro 完成（或超时）。
        内部通过 ``Future.result`` 在 worker 线程执行，主线程不会触碰循环锁。

        Args:
            coro: 要执行的协程。
            timeout: 超时秒数。

        Returns:
            coro 的返回值。

        Raises:
            asyncio.TimeoutError: 超时未完成。
        """
        future = self._submit(coro)
        return future.result(timeout=timeout)

    # ── 生命周期 ──────────────────────────────────────

    async def connect(self) -> bool:
        """建立 WebSocket 连接，注册自定义参数，启动后台循环。

        在**独立 worker 事件循环**中执行真正的连接逻辑，以隔离主程序事件循环
        的卡顿。调用方（主线程）await 本方法，它会阻塞至连接完成或失败。

        Returns:
            是否连接成功。
        """
        # 把异步连接逻辑委托给 worker 循环；_connect_locked 在 worker 循环里跑，
        # 因此对 self._connect_lock / self._request_lock 的 await 均绑定正确循环。
        return bool(await asyncio.wrap_future(self._submit(self._connect_in_worker())))

    async def _connect_in_worker(self) -> bool:
        """worker 循环内的实际连接实现（含锁与后台循环启动）。"""
        async with self._connect_lock:
            if self.is_connected and self.vts is not None:
                return True
            # 关掉旧 vts 时必须拿 _request_lock：否则 sender / heartbeat 这些
            # 持锁的循环可能正在 ``await self.vts.request(...)``，连接被关掉
            # 后那边会触发 send-on-closed-ws；同时把 self.vts 置 None 也要在
            # 同一把锁内做完，避免外部循环看到"vts 还在但底层 ws 已断"的中间态。
            if self.vts is not None:
                async with self._request_lock:
                    try:
                        await self.vts.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.vts = None

            logger.info(f"连接 VTube Studio ({self.host}:{self.port}) ...")
            try:
                # 惰性导入：只在真正需要连接 VTS 时才 import pyvts，
                # 这样未配置 vtb 模式时 pyvts 缺失也不影响插件加载。
                import pyvts  # type: ignore

                plugin_info = {
                    "plugin_name": self.plugin_name,
                    "developer": self.developer,
                }
                if self.token_path:
                    plugin_info["authentication_token_path"] = self.token_path
                self.vts = pyvts.vts(
                    plugin_info=plugin_info,
                    host=self.host,
                    port=self.port,
                )

                async with self._request_lock:
                    await self.vts.connect()
                    await self.vts.request_authenticate_token()
                    await self.vts.request_authenticate()
                    await self._register_custom_parameters()

                self.is_connected = True
                logger.info("✅ VTube Studio 连接并认证成功")

                self._start_background_tasks()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"连接 VTube Studio 失败: {exc}")
                self.is_connected = False
                self.vts = None
                return False

    def _start_background_tasks(self) -> None:
        """在 worker 循环里创建并跟踪三个后台循环任务（幂等）。"""
        loop = self._worker_loop
        if loop is None or loop.is_closed():
            return
        if self._heartbeat_task is None:
            self._heartbeat_task = loop.create_task(
                self._heartbeat_loop(), name="anima_chatter.vts.heartbeat"
            )
        if self._animation_task is None:
            self._animation_task = loop.create_task(
                self._animation_loop(), name="anima_chatter.vts.animation"
            )
        if self._sender_task is None:
            self._sender_task = loop.create_task(
                self._param_sender_loop(), name="anima_chatter.vts.sender"
            )

    async def close(self) -> None:
        """终止后台循环并断开 WebSocket。"""
        # 若 worker 循环从未启动（未 connect 过），直接复位即可。
        if self._worker_loop is None or self._worker_loop.is_closed():
            self.vts = None
            self.is_connected = False
            logger.info("VTube Studio 连接已关闭（worker 未运行）")
            return

        # 取消后台任务并断开连接，全部在 worker 循环里完成。
        try:
            await asyncio.wrap_future(self._submit(self._close_in_worker()))
        except Exception:  # noqa: BLE001
            pass

        # 停止并回收 worker 线程与事件循环。
        self._shutdown_worker()

    async def _close_in_worker(self) -> None:
        """worker 循环内的关闭实现：取消三循环、断开 ws。"""
        for attr in ("_heartbeat_task", "_animation_task", "_sender_task"):
            task = getattr(self, attr)
            if task is not None:
                try:
                    task.cancel()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attr, None)

        if self.vts is not None and self.is_connected:
            try:
                await self.vts.close()
            except Exception:  # noqa: BLE001
                pass
        self.vts = None
        self.is_connected = False
        logger.info("VTube Studio 连接已关闭")

    def _shutdown_worker(self) -> None:
        """停止并回收 worker 线程与私有事件循环（幂等、可重复调用）。"""
        loop = self._worker_loop
        thread = self._worker_thread
        if loop is not None and not loop.is_closed():
            self._worker_stop.set()
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._worker_thread = None
        self._worker_loop = None
        self._heartbeat_task = None
        self._animation_task = None
        self._sender_task = None

    # ── 自定义参数注册 ────────────────────────────────

    async def _register_custom_parameters(self) -> None:
        """向 VTS 注册插件专属的自定义参数。重复注册会被 VTS 自动忽略。"""

        if self.vts is None:
            return
        for param in _CUSTOM_PARAMETERS:
            try:
                msg = self.vts.vts_request.requestCustomParameter(
                    parameter=param["name"],
                    min=param["min"],
                    max=param["max"],
                    default_value=param["def"],
                )
                await self.vts.request(msg)
                logger.debug(f"注册自定义参数: {param['name']}")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"注册参数 {param['name']} 跳过（可能已存在）: {exc}")

    # ── 心跳 + 自动重连 ─────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """周期发送 StatisticsRequest 维持连接活跃；断连时带指数退避自动重连。

        触发重连的两种路径：

        - 心跳请求抛异常（VTS 端关闭或网络断开）→ 标记断连，下一轮重连。
        - ``self.is_connected == False`` 但循环仍在跑 → 直接进入重连分支。

        重连成功后心跳节奏立即恢复 10s/次；连续失败时退避从 5s 涨到 60s 封顶，
        既不会让日志刷屏，又能在 VTS 重新打开后较快接上。
        """

        # 立即重连：本地场景下 VTS 通常和 bot 在同一台机器，断连基本只发生在
        # VTS 软件被关掉、模型被切换等场景；连得上就秒上，连不上才退避。
        # 第一次失败直接 sleep 0.5s 重试；连续失败 5 次后再切到指数退避，避免
        # 长时间无 VTS 时把日志刷爆。
        consecutive_failures = 0
        max_backoff = 30.0

        def _backoff_for(failures: int) -> float:
            """前 5 次连续失败保持 0.5s，超过 5 次按 2^(n-5) 增长，封顶 30s。"""
            if failures < 5:
                return 0.5
            return min(0.5 * (2 ** (failures - 4)), max_backoff)

        while not self._worker_stop.is_set():
            try:
                # 断连状态：进入重连分支。第一次进来不 sleep，立即尝试。
                if not self.is_connected or self.vts is None:
                    if consecutive_failures > 0:
                        wait = _backoff_for(consecutive_failures)
                        logger.info(
                            f"VTS 未连接，{wait:.1f}s 后尝试重连…"
                            f"（连续失败 {consecutive_failures} 次）"
                        )
                        await asyncio.sleep(wait)
                    else:
                        logger.info("VTS 未连接，立即尝试重连…")
                    ok = await self._connect_in_worker()
                    if ok:
                        logger.info("✅ VTS 自动重连成功")
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                    continue

                # 正常心跳
                await asyncio.sleep(10)
                if not self.is_connected or self.vts is None:
                    continue
                # 关键：必须在锁内重新读 self.vts 拿快照，否则可能 lock 等到时
                # 旧 vts 已被 connect() 替换，构造的 msg 会送到新 ws 上引发 1002
                # protocol error。同理 sender_loop / trigger_hotkey 也要这么做。
                async with self._request_lock:
                    vts_local = self.vts
                    if vts_local is None or not self.is_connected:
                        continue
                    msg = vts_local.vts_request.BaseRequest("StatisticsRequest")
                    await vts_local.request(msg)
                # 心跳成功：清零退避状态。
                consecutive_failures = 0
            except asyncio.CancelledError:
                logger.info("VTS 心跳循环已停止")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"VTS 心跳检测失败，连接可能已断开: {exc}")
                self.is_connected = False
                # 故意不在这里 sleep / connect；交给下一轮循环开头的重连分支
                # 统一处理，避免逻辑分叉。

    # ── 动画聚合循环（30Hz） ─────────────────────────

    async def _animation_loop(self) -> None:
        """以 ~30Hz 调用所有动画器，把输出汇总到参数缓冲区。"""

        last_time = asyncio.get_event_loop().time()
        while not self._worker_stop.is_set():
            try:
                if not self.is_connected or self.vts is None:
                    await asyncio.sleep(2)
                    continue

                now = asyncio.get_event_loop().time()
                delta = min(0.1, now - last_time)
                last_time = now

                aggregated: dict[str, float] = {}
                for animator in self.animators:
                    try:
                        if not animator.is_active:
                            continue
                        params = await animator.update(delta)
                        if not params:
                            continue
                        for key, value in params.items():
                            aggregated[key] = aggregated.get(key, 0.0) + value
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            f"animator {animator.__class__.__name__} update 出错: {exc}"
                        )

                if aggregated:
                    async with self._buffer_lock:
                        self._param_buffer.update(aggregated)

                await asyncio.sleep(1 / 30)
            except asyncio.CancelledError:
                logger.info("VTS 动画循环已停止")
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"VTS 动画循环异常: {exc}")
                await asyncio.sleep(1)

    # ── 参数发送循环（消费 buffer） ──────────────────

    async def _param_sender_loop(self) -> None:
        """高频消费 ``_param_buffer``，把最新值送进 VTS。"""

        while not self._worker_stop.is_set():
            try:
                if not self.is_connected or self.vts is None:
                    await asyncio.sleep(1)
                    continue

                async with self._buffer_lock:
                    if not self._param_buffer:
                        snapshot: dict[str, float] = {}
                    else:
                        snapshot = self._param_buffer.copy()
                        self._param_buffer.clear()

                if snapshot:
                    # sanitize：滤掉 NaN/Inf、丢未声明的 key、截断到 min~max。
                    # 这是 VTS 不发 1002 protocol error 的最后一道闸——别省。
                    snapshot = _sanitize_params(snapshot)

                if snapshot:
                    try:
                        # 关键 1：拿 _request_lock 防止与心跳 / trigger_hotkey 并发
                        # 触发 "cannot call recv while another coroutine is
                        # already running recv"。
                        # 关键 2：在锁内 **重新** 读 self.vts 拿快照，并在锁内
                        # 构造 msg。这样 connect() 的重连分支拿到锁后把旧 vts
                        # 替换成新 vts 时，我们这边能感知到——不会用旧 msg 发
                        # 到新 ws 上引发 1002 protocol error（旧 vts_request
                        # 序列化的 messageId / 内部状态对新连接是非法帧）。
                        async with self._request_lock:
                            vts_local = self.vts
                            if vts_local is None or not self.is_connected:
                                # 重连进行中，本批参数丢弃即可——下一帧动画循环
                                # 会重新生成最新值。
                                continue
                            msg = vts_local.vts_request.requestSetMultiParameterValue(
                                list(snapshot.keys()),
                                list(snapshot.values()),
                            )
                            await vts_local.request(msg)
                    except Exception:  # noqa: BLE001
                        # 单次发送失败不致命，等下次循环。
                        pass

                await asyncio.sleep(1 / 60)
            except asyncio.CancelledError:
                logger.info("VTS 参数发送循环已停止")
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"VTS 参数发送循环异常: {exc}")
                await asyncio.sleep(0.5)

    # ── 直接控制接口 ─────────────────────────────────

    async def trigger_hotkey(self, hotkey_id: str) -> bool:
        """触发 VTube Studio 中已配置的热键（动作/表情按钮）。

        在主线程被调用，实际在 worker 循环里执行；返回结果通过 :class:`Future`
        桥接回主线程。
        """
        if not hotkey_id:
            return False
        if self._worker_loop is None or self._worker_loop.is_closed():
            return False
        return bool(self._run_coro(self._trigger_hotkey_in_worker(hotkey_id), 10.0))

    async def _trigger_hotkey_in_worker(self, hotkey_id: str) -> bool:
        """worker 循环内：触发热键实现。"""
        if not self.is_connected or self.vts is None or not hotkey_id:
            return False

        async with self._request_lock:
            # 与心跳 / sender 同款约定：在锁内重新读 self.vts，避免
            # 重连切换实例后旧引用发到新 ws。
            vts_local = self.vts
            if vts_local is None or not self.is_connected:
                return False
            try:
                msg = vts_local.vts_request.requestTriggerHotKey(hotkey_id)
                response = await vts_local.request(msg)
                if response is None:
                    return False
                return response.get("data", {}).get("hotkeyID") == hotkey_id
            except Exception as exc:  # noqa: BLE001
                logger.error(f"触发热键失败: {exc}")
                return False

    async def set_expression(self, expression_file: str, active: bool) -> bool:
        """直接激活或停用一个 Live2D 表情文件（``.exp3.json``）。

        与 :meth:`trigger_hotkey` 相比，``ExpressionActivationRequest`` 走的是
        VTS 的"表情通道"而不是"热键通道"：

        - **不需要** 在 VTS 设置里预先配置 hotkey；只要 ``.exp3.json`` 文件
          存在于模型目录就能调用。
        - 多个表情**可以同时激活**（hotkey 一次只触发一个）。
        - ``active=False`` 可以**显式停用**（hotkey 是瞬时触发，停用要靠表情
          自己的 fade out 或再次 trigger）。

        Args:
            expression_file: ``.exp3.json`` 文件名（不带路径），例如
                ``"expression16.exp3.json"``。
            active: True 激活 / False 停用。

        Returns:
            VTS 是否报告成功。
        """
        if not expression_file:
            return False
        if self._worker_loop is None or self._worker_loop.is_closed():
            return False
        return bool(
            self._run_coro(
                self._set_expression_in_worker(expression_file, active), 10.0
            )
        )

    async def _set_expression_in_worker(self, expression_file: str, active: bool) -> bool:
        """worker 循环内：设置表情激活状态实现。"""
        if not self.is_connected or self.vts is None or not expression_file:
            return False

        async with self._request_lock:
            vts_local = self.vts
            if vts_local is None or not self.is_connected:
                return False
            try:
                # pyvts 提供 BaseRequest 直接构造原生 VTS 请求；
                # ExpressionActivationRequest 的 data 字段固定是这俩。
                msg = vts_local.vts_request.BaseRequest(
                    "ExpressionActivationRequest",
                    {
                        "expressionFile": expression_file,
                        "active": bool(active),
                    },
                )
                response = await vts_local.request(msg)
                if response is None:
                    return False
                # 成功时 data 里没有 errorID；有 errorID 表示失败。
                data = response.get("data") or {}
                if "errorID" in data:
                    logger.warning(
                        f"VTS 表情激活失败 file={expression_file} active={active} "
                        f"err={data.get('errorID')} msg={data.get('message')}"
                    )
                    return False
                return True
            except Exception as exc:  # noqa: BLE001
                logger.error(f"设置表情失败 {expression_file}: {exc}")
                return False


def build_default_token_path() -> str:
    """返回插件目录下的 token 缓存路径（默认 ``data/anima_chatter/vts_token.txt``）。"""

    base_dir = os.path.join(os.getcwd(), "data", "anima_chatter")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "vts_token.txt")


__all__ = ["VTSConnection", "build_default_token_path"]
