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

from src.app.plugin_system.api.log_api import get_logger

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

    # 虚拟声卡名称关键词（小写匹配）。这类设备是纯软件管道，没有真实硬件
    # pin 拓扑——PortAudio 的 WASAPI 实现在 start stream 时会查询
    # KSPROPERTY_PIN_PHYSICALCONNECTION，虚拟设备返回 ERROR_NOT_FOUND，
    # 直接导致 WASAPI 流启动失败。对这类设备应优先走 MME / DirectSound。
    _VIRTUAL_DEVICE_KEYWORDS = ("cable", "virtual", "voicemeeter", "vb-audio")

    def _looks_like_virtual_device(self, name: str) -> bool:
        """根据设备名判断是否是虚拟声卡（VB-Cable / VoiceMeeter 等）。"""

        lowered = name.lower()
        return any(kw in lowered for kw in self._VIRTUAL_DEVICE_KEYWORDS)

    def _resolve_device(self) -> None:
        """根据当前 output_device_name 解析 device id。

        若用户把虚拟声卡（VB-Cable 等）配成了 ``@WASAPI``，启动时直接给一条
        WARNING 提示——WASAPI + 虚拟设备在 PortAudio 下注定失败（KS pin 物理
        连接查询返回 ERROR_NOT_FOUND），运行期会每次先失败一次再 fallback 到
        MME。建议直接把配置改成 ``@MME`` 省掉这次无谓的失败重试。
        """

        if not self.output_device_name:
            logger.info("未配置音频输出设备，将使用系统默认输出。")
            self.output_device_id = None
            return

        # 虚拟设备 + WASAPI 的组合预警（仅提示，不强制改写配置）。
        if (
            "@" in self.output_device_name
            and self.output_device_name.split("@", 1)[1].strip().lower() == "wasapi"
            and self._looks_like_virtual_device(self.output_device_name.split("@", 1)[0])
        ):
            logger.warning(
                f"检测到虚拟声卡 '{self.output_device_name}' 配置为 WASAPI——"
                "VB-Cable / VoiceMeeter 等虚拟设备在 WASAPI 下会因缺少物理 pin "
                "拓扑而启动失败（KS pin PHYSICALCONNECTION 查询返回 ERROR_NOT_FOUND）。"
                "建议把 audio_output_device 改为 '设备名@MME'。当前仍会尝试 WASAPI，"
                "失败后自动 fallback 到 MME。"
            )

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

                # 推断当前 PCM 通道数（mono = 1, 多声道 = data.shape[1]）。
                # 仅用于日志显示；真正的通道适配在 _play_sync 里按目标设备 max_output_channels 决定。
                current_ch = 1 if data.ndim == 1 else int(data.shape[1])
                logger.info(
                    f"开始播放音频：{len(data)} samples @ {samplerate}Hz, {current_ch}ch "
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
        """同步播放，包含设备适配 + 主路径/MME 备用路径。

        **每次播放前重新解析 device id** ——sounddevice 的 device id 在 Windows
        上不是稳定的（拔插任何 USB 音频 / VB-Cable / 蓝牙耳机都会让 id 重新分
        配），缓存的 id 会过期触发 ``MME error 2: 使用的设备标识号已超出本地
        系统范围``。这一段无开销（``sd.query_devices`` 在 Windows 上 < 1ms）。
        """

        # 1) 每次播放前重新解析 device id——避免缓存过期。
        if self._prefer_fallback:
            self._fallback_device_id = self._resolve_fallback_device_id()
            target_device_id = self._fallback_device_id
        else:
            target_device_id = self._find_device_id(self.output_device_name) if self.output_device_name else None
            self.output_device_id = target_device_id  # 同步更新缓存

        if target_device_id is not None:
            try:
                dev_info = sd.query_devices(target_device_id)
                default_rate = self._read_attr(dev_info, "default_samplerate", 44100)
                target_rate = int(float(default_rate))
                target_channels = int(
                    self._read_attr(dev_info, "max_output_channels", 1) or 1
                )

                # 1) 采样率适配：差距 > 10Hz 就重采样到设备默认值。
                #    WASAPI shared mode 严格要求采样率 = Windows mixer 当前值（一般 48kHz），
                #    送 32kHz 进去会被 KS pin 直接拒绝。
                if abs(samplerate - target_rate) > 10:
                    data, samplerate = self._resample(data, samplerate, target_rate)

                # 2) 通道适配：mono → stereo 等。WASAPI shared mode 对通道数一样
                #    严格——VB-Cable 的 WASAPI 输出通常 max_output_channels=2，
                #    送 mono PCM 进去会触发 "WdmSyncIoctl: DeviceIoControl GLE=0x00000490
                #    Windows WDM-KS error 0"（KS pin 属性拒绝）。
                data = self._adapt_channels(data, target_channels)
            except Exception as exc:
                logger.warning(f"采样率/通道自动适配失败: {exc}")

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

        # 3) 主路径：按 host API 选最合适的 extra_settings。
        #
        #    WASAPI shared mode 严格——即使我们已对齐了采样率 + 通道数，VB-Cable
        #    这种虚拟设备在底层查 KS pin 属性时仍可能抛 ``WdmSyncIoctl GLE 0x490
        #    [Windows WDM-KS error 0]``。最稳的做法是显式构造
        #    ``WasapiSettings(exclusive=False, auto_convert=True)``——让 WASAPI
        #    在内核层自动做格式协商（SRC + channel mapping + sample format），
        #    应用层就完全不必担心格式不匹配。
        extra_settings = self._build_extra_settings(self.output_device_id)
        if not self._prefer_fallback:
            try:
                sd.play(
                    data,
                    samplerate,
                    device=self.output_device_id,
                    latency="high",
                    extra_settings=extra_settings,
                )
                sd.wait()
                return
            except Exception as exc:
                # 对虚拟声卡（VB-Cable）来说，WASAPI 失败是**预期行为**（见
                # _resolve_device 的说明），不是真正的错误——降级为 WARNING，
                # 避免在终端刷红色 ERROR 吓人。真实硬件设备失败才是值得关注的。
                if self._looks_like_virtual_device(self.output_device_name):
                    logger.warning(
                        f"WASAPI 主路径对虚拟声卡不可用（预期，将走 MME）: {exc}"
                    )
                else:
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

    def _build_extra_settings(self, device_id: int | None) -> Any:
        """根据目标设备的 host API 构造 sounddevice ``extra_settings``。

        当目标 host API 是 WASAPI 时，返回
        ``sd.WasapiSettings(exclusive=False, auto_convert=True)`` —— 让内核层
        自动做 SRC / channel / sample format 协商，跳过 KS pin 严格属性检查。
        其它 host API（MME / DirectSound / WDM-KS）不需要 extra_settings，
        返回 ``None``。

        Args:
            device_id: 目标 sounddevice device id；为 None 时返回 None。

        Returns:
            ``sd.WasapiSettings`` 实例 / None。
        """

        if device_id is None:
            return None
        try:
            dev_info = sd.query_devices(device_id)
            api_idx = self._read_attr(dev_info, "hostapi", None)
            if api_idx is None:
                return None
            api_info = sd.query_hostapis(int(api_idx))
            api_name = str(self._read_attr(api_info, "name", "")).lower()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"查询 host API 类型失败（继续走默认 extra_settings）: {exc}")
            return None

        if "wasapi" in api_name:
            try:
                # auto_convert=True 让 WASAPI 自己做 SRC / channel mapping，
                # 是 mono TTS PCM 走 stereo 虚拟设备的最干净路径。
                return sd.WasapiSettings(exclusive=False, auto_convert=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"构造 WasapiSettings 失败（fall back to None）: {exc}")
                return None
        return None

    def _resolve_fallback_device_id(self) -> int | None:
        """每次播放前实时解析 ``设备名@MME`` 的 device id。

        Windows 上 device id 不稳定，``__init__`` 里缓存的会过期。每次播放
        都重新查 ``sd.query_devices`` —— Windows 上 < 1ms，开销可忽略。
        """

        if not self.output_device_name:
            return None
        base = (
            self.output_device_name.split("@", 1)[0]
            if "@" in self.output_device_name
            else self.output_device_name
        )
        try:
            return self._find_device_id(f"{base}@MME")
        except Exception:
            return None

    def _cache_fallback_device_id(self) -> None:
        """首次主路径失败时记下 fallback 设备名（实际 id 解析延迟到播放时）。

        这个方法仅在主路径首次失败时触发，作用只是把 ``self._fallback_device_id``
        设成非 None 让 ``_prefer_fallback`` 切换路径。真正的 id 解析在
        :meth:`_play_sync` 头部完成。
        """

        if self._fallback_device_id is not None or not self.output_device_name:
            return
        # 首次只用一次解析，后续每次播放会刷新。
        self._fallback_device_id = self._resolve_fallback_device_id()

    @staticmethod
    def _adapt_channels(data: Any, target_channels: int) -> Any:
        """把 PCM 数据适配到目标通道数（mono ↔ stereo ↔ N-ch）。

        WASAPI / DirectSound 在 shared mode 下对通道数严格——TTS 输出常是
        mono（1ch），但 VB-Cable 等虚拟设备的 WASAPI 输出口往往是 stereo
        （2ch）。不做适配会触发 KS pin 属性拒绝错误，PortAudio 报：
        ``Windows WDM-KS error 0 / WdmSyncIoctl GLE=0x00000490``。

        策略：
        - mono → multi: 复制单声道到所有目标通道；
        - multi → mono: 取所有通道平均值；
        - n → m (n > m): 截取前 m 个通道；
        - n → m (n < m): 用最后一个通道补足。

        Args:
            data: ``float32`` numpy 数组，shape=(N,) 或 (N, C)。
            target_channels: 目标通道数（≥1）。

        Returns:
            ``float32`` numpy 数组，shape=(N,) for mono、(N, target_channels) for multi。
        """

        target_channels = max(1, int(target_channels))
        # 当前通道数：1D 视为 mono；2D 取第二维
        current = 1 if data.ndim == 1 else int(data.shape[1])

        if current == target_channels:
            return data

        # mono → multi-channel：复制
        if current == 1:
            mono = data if data.ndim == 1 else data[:, 0]
            if target_channels == 1:
                return mono.astype(np.float32, copy=False)
            return np.tile(mono[:, np.newaxis], (1, target_channels)).astype(np.float32)

        # multi → mono：取均值
        if target_channels == 1:
            return data.mean(axis=1).astype(np.float32)

        # multi → multi（不同声道数）
        if data.shape[1] >= target_channels:
            return data[:, :target_channels].astype(np.float32, copy=False)
        # 通道数不足：用最后一个声道补齐
        pad = np.tile(data[:, -1:], (1, target_channels - data.shape[1]))
        return np.concatenate([data, pad], axis=1).astype(np.float32)

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
