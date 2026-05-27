"""anima_chatter 的本地音频播放器。

把 TTS 合成出来的 WAV bytes 输出到指定的 sounddevice 设备（通常是
VB-Audio Cable Input），让 VTube Studio 的麦克风输入听到声音从而驱动嘴型。

实现移植自 D:\\Desktop\\soul_chatter_plugin\\services\\audio_player.py，
仅做以下调整：

- ``get_logger`` 改为 ``src.kernel.logger.get_logger``。
- 构造参数改为接收 ``output_device: str``，不再读裸 dict，方便插件层注入。
- 单例式 ``_play_lock`` 保留，确保播放严格按顺序串行。

降级策略：

1. 优先按 ``设备名@WASAPI`` 匹配，并请求 ``latency='high'`` 缓冲，最稳。
2. WASAPI 失败时尝试 MME。
3. 最后回退到系统默认设备。
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

import numpy as np
import sounddevice as sd  # type: ignore
import soundfile as sf  # type: ignore

from src.kernel.logger import get_logger

from .envelope import EnvelopeTracker, compute_envelope


logger = get_logger("anima_chatter.audio_player")


# envelope 计算 / 消费的步长，与 VTSConnection._animation_loop 的 30Hz 对齐。
# 改这个值需要同时改 SpeechAnimator 那边的查询节奏。
_ENVELOPE_HOP_SECONDS = 1.0 / 30.0


class AudioPlayer:
    """串行播放音频到指定设备，并按目标 dBFS 做响度归一化。

    所有走这个 player 的音频（TTS / 唱歌 / 任何 ``play_audio`` 调用）都会
    被同一目标响度统一拉齐——避免直播间观众听到时一会儿大一会儿小。
    """

    def __init__(
        self,
        output_device: str = "",
        *,
        loudness_target_dbfs: float | None = -20.0,
    ) -> None:
        """初始化音频播放器。

        Args:
            output_device: ``设备名@驱动`` 或纯设备名；为空则使用系统默认。
            loudness_target_dbfs: 目标 RMS 响度（dBFS）。所有播放的音频会被
                统一拉到这个响度——TTS 自带的音量 / 翻唱歌曲音量 / 其它任何
                走 :meth:`play_audio` 的内容都按这个值归一化。
                推荐值 ``-20`` ~ ``-16``（直播 / 流媒体常用）。设为 ``None``
                关闭归一化，按原始音量播放。
        """

        self.output_device_name: str = (output_device or "").strip()
        self.output_device_id: int | None = None
        self.loudness_target_dbfs: float | None = loudness_target_dbfs
        self._play_lock = asyncio.Lock()
        # 主路径（WASAPI + latency=high）一旦失败，记住后续直接走 MME 备用路径，
        # 避免每条音频都浪费 ~1 秒走重试链路。
        self._prefer_fallback: bool = False
        self._fallback_device_id: int | None = None

        # 音频包络追踪器。播放期间由动画器（SpeechAnimator）按 30Hz 查询，
        # 用来驱动头部 / 身体的"语调微动"，让 VTB 看起来"跟着声音动"。
        # 与 sounddevice 的 sd.play 异步，但有共享线程锁；查询安全。
        self.envelope_tracker = EnvelopeTracker()

        self._resolve_device()

    def update_output_device(self, output_device: str) -> None:
        """运行时更新目标设备并重新解析。"""

        self.output_device_name = (output_device or "").strip()
        self._resolve_device()

    def _resolve_device(self) -> None:
        """根据当前 output_device_name 解析 device id。"""

        if not self.output_device_name:
            logger.info("未配置音频输出设备，将使用系统默认输出。")
            self.output_device_id = None
            return

        device_id = self._find_device_id(self.output_device_name)
        if device_id is None:
            logger.warning(
                f"未找到名为 '{self.output_device_name}' 的音频设备，将使用系统默认。"
            )
            self.output_device_id = None
            return

        logger.info(f"已锁定输出设备: {self.output_device_name} (ID: {device_id})")
        self.output_device_id = device_id

    @staticmethod
    def _read_attr(obj: Any, key: str, default: Any) -> Any:
        """兼容字典 / 对象两种 sounddevice 设备记录的属性读取。"""

        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _find_device_id(self, name_part: str) -> int | None:
        """根据名称片段查找设备 ID（支持 ``设备名@驱动名`` 格式）。"""

        try:
            devices = sd.query_devices()
            host_apis = sd.query_hostapis()

            target_device = name_part
            target_api: str | None = None
            if "@" in name_part:
                target_device, target_api = name_part.split("@", 1)

            logger.debug(
                f"搜索音频设备: target={target_device!r} api={target_api!r}"
            )

            for idx, dev in enumerate(devices):
                max_out = self._read_attr(dev, "max_output_channels", 0)
                if not max_out:
                    continue

                dev_name = self._read_attr(dev, "name", "")
                api_idx = self._read_attr(dev, "hostapi", 0)

                if target_device not in dev_name:
                    continue

                if target_api:
                    api_info = host_apis[api_idx]
                    api_name = self._read_attr(api_info, "name", "")
                    if target_api.lower() not in api_name.lower():
                        continue

                hostapi_name = self._read_attr(host_apis[api_idx], "name", "Unknown")
                logger.info(
                    f"匹配到音频设备: ID [{idx}] '{dev_name}' (Driver: {hostapi_name})"
                )
                return idx
            return None
        except Exception as exc:
            logger.error(f"查找音频设备时出错: {exc}")
            return None

    async def play_audio(self, audio_data: bytes) -> None:
        """异步播放音频 bytes（任何 ``soundfile`` 能解码的格式），等待播放完成。

        播放前会按 ``loudness_target_dbfs`` 把响度归一化到统一目标——TTS
        说话和翻唱歌曲都走同一个目标，避免直播间观众听到一会儿响一会儿轻。
        ``loudness_target_dbfs`` 设为 ``None`` 时跳过归一化（按原音量播放）。
        """

        if not audio_data:
            logger.warning("接收到的音频数据为空，跳过播放。")
            return

        async with self._play_lock:
            try:
                with io.BytesIO(audio_data) as buf:
                    data, samplerate = sf.read(buf)

                if data.dtype != np.float32:
                    data = data.astype(np.float32)

                # ── 响度归一化 ───────────────────────────
                # 在算 envelope / 播放前把 RMS 拉到目标 dBFS。这样：
                # - VTS 的麦克风口型 / SpeechAnimator 包络都基于归一化后的真实
                #   播放音量，不会因为原文件偏小导致动作幅度不够；
                # - 多次连续播放（say + sing_song 交替）观众听感一致。
                # 整段内联用 numpy，大约 1ms / 5 分钟立体声，开销可忽略。
                if self.loudness_target_dbfs is not None:
                    from .loudness import normalize_audio_array

                    data = normalize_audio_array(
                        data, target_dbfs=self.loudness_target_dbfs
                    )

                # 在 sd.play 之前先算好 envelope。这是一段已经在内存里的 PCM，
                # 离线计算很快（10s 音频大概 1ms 量级）。这样 SpeechAnimator
                # 一启动就能跟上，不用等"流式回调"。
                envelope = compute_envelope(
                    data,
                    samplerate,
                    hop_seconds=_ENVELOPE_HOP_SECONDS,
                )
                self.envelope_tracker.begin(envelope, _ENVELOPE_HOP_SECONDS)

                logger.info(
                    f"开始播放音频：{len(data)} samples @ {samplerate}Hz "
                    f"(device_id={self.output_device_id}) envelope_frames={len(envelope)}"
                )

                loop = asyncio.get_running_loop()
                try:
                    await loop.run_in_executor(None, self._play_sync, data, samplerate)
                finally:
                    # 不论播放成功失败，都把 tracker 清空，避免 SpeechAnimator
                    # 卡在最后一帧的包络值上。
                    self.envelope_tracker.end()
                logger.info("音频播放完成。")
            except Exception as exc:
                # 异常路径下也要确保 tracker 清干净。
                self.envelope_tracker.end()
                logger.error(f"播放音频时发生错误: {exc}")

    def _play_sync(self, data: Any, samplerate: int) -> None:
        """同步播放，包含设备适配 + 主路径/MME 备用路径。"""

        # 1) 选择目标设备并按需重采样
        target_device_id = (
            self._fallback_device_id
            if self._prefer_fallback and self._fallback_device_id is not None
            else self.output_device_id
        )

        if target_device_id is not None:
            try:
                dev_info = sd.query_devices(target_device_id)
                default_rate = self._read_attr(dev_info, "default_samplerate", 44100)
                target_rate = int(float(default_rate))
                if abs(samplerate - target_rate) > 10:
                    data, samplerate = self._resample(data, samplerate, target_rate)
            except Exception as exc:
                logger.warning(f"采样率自动适配失败: {exc}")

        # 2) 已记忆要走 MME 备用路径 → 直接播
        if self._prefer_fallback and self._fallback_device_id is not None:
            try:
                sd.play(
                    data,
                    samplerate,
                    device=self._fallback_device_id,
                    latency="high",
                )
                sd.wait()
                return
            except Exception as exc:
                logger.error(f"MME 备用路径播放失败: {exc}")
                # 保留 _prefer_fallback=True；继续走最终回退。

        # 3) 主路径：WASAPI + latency='high'
        if not self._prefer_fallback:
            try:
                sd.play(
                    data,
                    samplerate,
                    device=self.output_device_id,
                    latency="high",
                )
                sd.wait()
                return
            except Exception as exc:
                logger.error(f"底层播放调用失败: {exc}")
                # 一次失败即标记走 MME 备用路径，避免后续每次都浪费 1 秒。
                self._cache_fallback_device_id()
                if self._fallback_device_id is not None:
                    self._prefer_fallback = True
                    logger.info(
                        "主路径不可用，已切换为 MME 备用路径"
                        f"（device_id={self._fallback_device_id}）。"
                    )
                    try:
                        sd.play(
                            data,
                            samplerate,
                            device=self._fallback_device_id,
                            latency="high",
                        )
                        sd.wait()
                        return
                    except Exception as exc2:
                        logger.error(f"MME 备用路径首次切换播放也失败: {exc2}")

        # 4) 终极回退：系统默认设备（可能丢失口型同步）
        try:
            logger.warning("尝试终极回退：系统默认输出设备")
            sd.play(data, samplerate, device=None, latency="high")
            sd.wait()
        except Exception as exc:
            logger.error(f"所有播放尝试均告失败: {exc}")

    def _cache_fallback_device_id(self) -> None:
        """缓存 ``设备名@MME`` 的 device id，作为主路径失败后的备用通道。"""

        if self._fallback_device_id is not None or not self.output_device_name:
            return
        base = (
            self.output_device_name.split("@", 1)[0]
            if "@" in self.output_device_name
            else self.output_device_name
        )
        try:
            fallback_id = self._find_device_id(f"{base}@MME")
        except Exception:
            fallback_id = None
        self._fallback_device_id = fallback_id

    @staticmethod
    def _resample(data: Any, src_rate: int, dst_rate: int) -> tuple[Any, int]:
        """优先 scipy 重采样；不可用时降级为 numpy 线性插值。"""

        ratio = dst_rate / src_rate
        new_length = int(len(data) * ratio)
        try:
            from scipy import signal  # type: ignore

            resampled = signal.resample(data, new_length)
            new_data = resampled[0] if isinstance(resampled, tuple) else resampled
        except ImportError:
            indices = np.linspace(0, len(data) - 1, new_length)
            if data.ndim == 2:
                channels: list[np.ndarray] = []
                for c in range(data.shape[1]):
                    channels.append(np.interp(indices, np.arange(len(data)), data[:, c]))
                new_data = np.stack(channels, axis=1)
            else:
                new_data = np.interp(indices, np.arange(len(data)), data)
        return new_data.astype(np.float32), dst_rate  # type: ignore[union-attr]


__all__ = ["AudioPlayer"]
