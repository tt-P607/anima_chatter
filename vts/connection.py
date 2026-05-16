"""VTube Studio WebSocket 连接管理。

封装 pyvts 的连接、认证、自定义参数注册、动画循环 + 参数发送循环。
所有后台任务通过 :mod:`src.kernel.concurrency.task_manager` 派发，
不直接使用 ``asyncio.create_task``。

旧版基于独立子进程 + Socket IPC 的设计在新框架下不再必要——Neo 的
task_manager 与 watchdog 已经能保证主事件循环不被阻塞。
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

from src.kernel.concurrency import get_task_manager
from src.kernel.logger import get_logger

if TYPE_CHECKING:
    from .animation.base import BaseAnimator


logger = get_logger("voice_chatter.vts.connection")


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


class VTSConnection:
    """与 VTube Studio 维持 WebSocket 长连接，并跑动画循环。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8001,
        plugin_name: str = "MoFox-Bot-VoiceChatter",
        developer: str = "MoFox Team",
        token_path: str | None = None,
    ) -> None:
        """初始化连接参数；token 文件用于免重复授权。"""

        self.host = host
        self.port = port
        self.plugin_name = plugin_name
        self.developer = developer
        self.token_path = token_path

        # pyvts.vts 实例；保持 Any 避免在未安装 pyvts 的开发环境下出现导入错误。
        self.vts: Any = None
        self.is_connected: bool = False

        self._connect_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._buffer_lock = asyncio.Lock()
        self._param_buffer: dict[str, float] = {}

        self._heartbeat_handle: Any = None
        self._animation_handle: Any = None
        self._sender_handle: Any = None
        self.animators: list[BaseAnimator] = []

    # ── 生命周期 ──────────────────────────────────────

    async def connect(self) -> bool:
        """建立 WebSocket 连接，注册自定义参数，启动后台循环。"""

        async with self._connect_lock:
            if self.is_connected and self.vts is not None:
                return True
            if self.vts is not None:
                try:
                    await self.vts.close()
                except Exception:
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

                tm = get_task_manager()
                if self._heartbeat_handle is None:
                    self._heartbeat_handle = tm.create_task(
                        self._heartbeat_loop(),
                        name="voice_chatter.vts.heartbeat",
                    )
                if self._animation_handle is None:
                    self._animation_handle = tm.create_task(
                        self._animation_loop(),
                        name="voice_chatter.vts.animation",
                    )
                if self._sender_handle is None:
                    self._sender_handle = tm.create_task(
                        self._param_sender_loop(),
                        name="voice_chatter.vts.sender",
                    )
                return True
            except Exception as exc:
                logger.warning(f"连接 VTube Studio 失败: {exc}")
                self.is_connected = False
                self.vts = None
                return False

    async def close(self) -> None:
        """终止后台循环并断开 WebSocket。"""

        for handle_attr in ("_heartbeat_handle", "_animation_handle", "_sender_handle"):
            handle = getattr(self, handle_attr, None)
            if handle is not None:
                try:
                    handle.cancel()
                except Exception:
                    pass
                setattr(self, handle_attr, None)

        if self.vts is not None and self.is_connected:
            try:
                await self.vts.close()
            except Exception:
                pass
        self.vts = None
        self.is_connected = False
        logger.info("VTube Studio 连接已关闭")

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
            except Exception as exc:
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

        # 重连退避：成功一次清零；失败一次乘 1.5（封顶 60s）。
        backoff_seconds = 5.0
        max_backoff = 60.0
        consecutive_failures = 0

        while True:
            try:
                # 断连状态：进入重连分支（不固定 sleep 10s，避免重连等太久）。
                if not self.is_connected or self.vts is None:
                    logger.info(
                        f"VTS 未连接，{backoff_seconds:.0f}s 后尝试重连…"
                        f"（连续失败 {consecutive_failures} 次）"
                    )
                    await asyncio.sleep(backoff_seconds)
                    ok = await self.connect()
                    if ok:
                        logger.info("✅ VTS 自动重连成功")
                        consecutive_failures = 0
                        backoff_seconds = 5.0
                    else:
                        consecutive_failures += 1
                        backoff_seconds = min(backoff_seconds * 1.5, max_backoff)
                    continue

                # 正常心跳
                await asyncio.sleep(10)
                if not self.is_connected or self.vts is None:
                    continue
                async with self._request_lock:
                    msg = self.vts.vts_request.BaseRequest("StatisticsRequest")
                    await self.vts.request(msg)
                # 心跳成功：清零退避状态。
                consecutive_failures = 0
                backoff_seconds = 5.0
            except asyncio.CancelledError:
                logger.info("VTS 心跳循环已停止")
                return
            except Exception as exc:
                logger.warning(f"VTS 心跳检测失败，连接可能已断开: {exc}")
                self.is_connected = False
                # 故意不在这里 sleep / connect；交给下一轮循环开头的重连分支
                # 统一处理，避免逻辑分叉。

    # ── 动画聚合循环（30Hz） ─────────────────────────

    async def _animation_loop(self) -> None:
        """以 ~30Hz 调用所有动画器，把输出汇总到参数缓冲区。"""

        last_time = asyncio.get_event_loop().time()
        while True:
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
                    except Exception as exc:
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
            except Exception as exc:
                logger.error(f"VTS 动画循环异常: {exc}")
                await asyncio.sleep(1)

    # ── 参数发送循环（消费 buffer） ──────────────────

    async def _param_sender_loop(self) -> None:
        """高频消费 ``_param_buffer``，把最新值送进 VTS。"""

        while True:
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
                    try:
                        msg = self.vts.vts_request.requestSetMultiParameterValue(
                            list(snapshot.keys()),
                            list(snapshot.values()),
                        )
                        await self.vts.request(msg)
                    except Exception:
                        # 单次发送失败不致命，等下次循环。
                        pass

                await asyncio.sleep(1 / 60)
            except asyncio.CancelledError:
                logger.info("VTS 参数发送循环已停止")
                return
            except Exception as exc:
                logger.error(f"VTS 参数发送循环异常: {exc}")
                await asyncio.sleep(0.5)

    # ── 直接控制接口 ─────────────────────────────────

    async def trigger_hotkey(self, hotkey_id: str) -> bool:
        """触发 VTube Studio 中已配置的热键（动作/表情按钮）。"""

        if not self.is_connected or self.vts is None or not hotkey_id:
            return False

        async with self._request_lock:
            try:
                msg = self.vts.vts_request.requestTriggerHotKey(hotkey_id)
                response = await self.vts.request(msg)
                if response is None:
                    return False
                return response.get("data", {}).get("hotkeyID") == hotkey_id
            except Exception as exc:
                logger.error(f"触发热键失败: {exc}")
                return False


def build_default_token_path() -> str:
    """返回插件目录下的 token 缓存路径（默认 ``data/voice_chatter/vts_token.txt``）。"""

    base_dir = os.path.join(os.getcwd(), "data", "voice_chatter")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "vts_token.txt")


__all__ = ["VTSConnection", "build_default_token_path"]
