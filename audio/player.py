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
        inst_output_device: str = "",
    ) -> None:
        """初始化音频播放器。

        Args:
            output_device: 人声 / TTS 的输出设备，``设备名@驱动`` 或纯设备名；
                为空则使用系统默认。通常指向 VB-Cable，驱动 VTS 口型。
            loudness_target_dbfs: 目标 RMS 响度（dBFS）。所有播放的音频会被
                统一拉到这个响度——TTS 自带的音量 / 翻唱歌曲音量 / 其它任何
                走 :meth:`play_audio` 的内容都按这个值归一化。
                推荐值 ``-20`` ~ ``-16``（直播 / 流媒体常用）。设为 ``None``
                关闭归一化，按原始音量播放。
            inst_output_device: 双轨翻唱时**伴奏**的专用输出设备，``设备名@驱动``
                或纯设备名；为空则走系统默认输出。用于把伴奏单独送到一个给直播
                软件采集的设备（与人声的 VB-Cable 分开），方便伴奏进直播流而不
                经过 VB-Cable。仅 :meth:`play_dual` 的伴奏路使用。
        """

        self.output_device_name: str = (output_device or "").strip()
        self.output_device_id: int | None = None
        self.inst_output_device_name: str = (inst_output_device or "").strip()
        self.inst_output_device_id: int | None = None
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
        self._resolve_inst_device()

    def _resolve_inst_device(self) -> None:
        """解析伴奏专用输出设备 id；为空或找不到时退回系统默认（None）。"""

        if not self.inst_output_device_name:
            logger.info("未配置伴奏专用输出设备，双轨伴奏将走系统默认输出。")
            self.inst_output_device_id = None
            return

        device_id = self._find_device_id(self.inst_output_device_name)
        if device_id is None:
            logger.warning(
                f"未找到伴奏输出设备 '{self.inst_output_device_name}'，"
                "双轨伴奏将退回系统默认输出。"
            )
            self.inst_output_device_id = None
            return

        logger.info(
            f"已锁定伴奏输出设备: {self.inst_output_device_name} (ID: {device_id})"
        )
        self.inst_output_device_id = device_id

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

    def _reset_audio_backend(self) -> None:
        """重新初始化 PortAudio 后端，强制刷新设备拓扑。

        设备热拔插（拔出蓝牙音箱 / USB 耳机等）后，PortAudio 在进程启动时
        枚举的设备列表会过期、底层句柄进入错误状态——表现为 ``sd.play`` 抛
        PortAudioError 或 ``MME error``，且单纯重新 ``query_devices`` 也救不
        回来（句柄本身已坏）。``sd._terminate()`` + ``sd._initialize()`` 会强制
        PortAudio 卸载并重新枚举整个设备拓扑，等价于"软重启音频子系统"，
        让进程在不重启的前提下重新感知当前可用设备。

        重置后所有缓存的 device id 全部作废，这里顺带清空 fallback 状态并
        重新解析配置中的目标设备，供下一次播放使用。
        """

        try:
            sd._terminate()  # type: ignore[attr-defined]
            sd._initialize()  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"重新初始化 PortAudio 失败: {exc}")
            return

        # 句柄重建后旧 device id 全部失效，强制下次播放重新解析。
        self._prefer_fallback = False
        self._fallback_device_id = None
        self.output_device_id = None
        self.inst_output_device_id = None
        self._resolve_device()
        self._resolve_inst_device()
        logger.info("已重新初始化 PortAudio 音频后端（设备拓扑已刷新）。")

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
                # 设备热拔插（拔蓝牙 / USB 耳机）会让本次播放失败。失败后重新
                # 初始化 PortAudio 刷新设备拓扑，再用**同一段音频**重试一次，
                # 避免这一条语音被直接丢掉。
                played = False
                try:
                    played = await loop.run_in_executor(
                        None, self._play_sync, data, samplerate
                    )
                    if not played:
                        logger.warning(
                            "首次播放失败，重新初始化音频后端后重试同一段音频…"
                        )
                        await loop.run_in_executor(None, self._reset_audio_backend)
                        played = await loop.run_in_executor(
                            None, self._play_sync, data, samplerate
                        )
                finally:
                    # 不论播放成功失败，都把 tracker 清空，避免 SpeechAnimator
                    # 卡在最后一帧的包络值上。
                    self.envelope_tracker.end()
                if played:
                    logger.info("音频播放完成。")
                else:
                    logger.error("重试后仍无法播放该段音频，已跳过。")
            except Exception as exc:
                # 异常路径下也要确保 tracker 清干净。
                self.envelope_tracker.end()
                logger.error(f"播放音频时发生错误: {exc}")

    async def play_dual(self, vocal_data: bytes, inst_data: bytes) -> None:
        """双轨同步播放：人声进 VB-Cable（驱动口型），伴奏进系统扬声器。

        用于完整翻唱——伴奏不进 VB-Cable，VTS uLipSync 只听到人声，嘴只跟人声
        动，伴奏不会带动口型。

        **响度归一化**：若 ``self.loudness_target_dbfs`` 非 ``None``，两轨会
        基于**混合后的 RMS** 计算统一增益并同时应用，保留 DAW 调好的人声/伴奏
        相对比例（与"各自独立归一化"不同）。关闭归一化时保持原电平。

        **对齐策略（关键）**：两路必须**采样级**精准对齐（像 AU 里那样），
        不只是"同时调用 write"。难点在于人声走 VB-Cable（MME，输出延迟约
        80~150ms）、伴奏走系统默认（WASAPI，延迟约 10~30ms），即使两路在同一
        瞬间 ``write()``，声音真正落到输出的时刻也差几十毫秒——这就是"略微延迟"
        的根因。做法是：
        1. **预处理全部完成**——解码、归一化、重采样、通道适配在调用方线程里
           做完，executor 线程拿到的就是"可直接 write 的最终 PCM"。
        2. **两路 ``OutputStream`` 各自打开后读 ``stream.latency``**——这是
           PortAudio 报告的该流实际输出延迟（秒）。
        3. **延迟补偿（核心）**——两路交换 latency 后，给延迟**较低**的那一路
           在 PCM 前面补 ``round((高延迟 - 低延迟) × 采样率)`` 个静音帧，让两路
           "可听起点"对齐到采样级，残余误差 < 1 帧（亚毫秒，人耳无法分辨）。
        4. **threading.Barrier(2) 同步起跑**——补偿后两条线程在 ``stream.write()``
           之前 ``barrier.wait()``，最后释放的瞬间同时进入 PortAudio 写入。

        与单轨 ``play_audio`` 的关键区别：放弃 ``sd.play()`` 全局单例机制，
        改用显式 ``OutputStream`` 上下文，否则两路并发互相 abort（表现为
        "只听到一路"）。VB-Cable 这条放弃 WASAPI 协商，直接走 MME device id
        以避开 ``sd.play`` 路径里的 fallback 复杂度。

        **统一归一化（关键）**：人声 + 伴奏**作为一个整体**计算一个共用增益，
        再把同一个增益系数同时乘到两轨上。这样整体响度被拉到 ``loudness_target_dbfs``
        统一目标（和单轨 ``play_audio`` 听感一致），又因为乘的是同一个数，AU 等
        DAW 导出时调好的人声/伴奏相对比例被**完整保留**——不会像"各自独立归一化"
        那样把混音平衡抹平。``loudness_target_dbfs`` 为 ``None`` 时跳过归一化，
        原样播放。某一轨缺失退回单轨 ``play_audio`` 时按单轨全局响度归一化。
        """

        if not vocal_data:
            logger.warning("play_dual 人声为空，退回单轨伴奏播放。")
            if inst_data:
                await self.play_audio(inst_data)
            return
        if not inst_data:
            logger.warning("play_dual 伴奏为空，退回单轨人声播放。")
            await self.play_audio(vocal_data)
            return

        # 设备热拔插会让双轨播放失败；失败后重新初始化 PortAudio 刷新设备
        # 拓扑再重试整段，避免这次翻唱被直接丢掉。_play_dual_once 自带锁，
        # 两次调用顺序获取 / 释放，不会死锁。
        played = await self._play_dual_once(vocal_data, inst_data)
        if not played:
            logger.warning("双轨播放失败，重新初始化音频后端后重试整段…")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._reset_audio_backend)
            played = await self._play_dual_once(vocal_data, inst_data)
        if not played:
            logger.error("重试后双轨播放仍失败，已跳过该段翻唱。")

    async def _play_dual_once(self, vocal_data: bytes, inst_data: bytes) -> bool:
        """执行一次双轨同步播放，返回两路是否都成功写入。

        从 :meth:`play_dual` 主体抽出以支持设备热拔插失败后的重试；自带
        ``_play_lock``，保证与单轨 :meth:`play_audio` 串行。
        """

        async with self._play_lock:
            try:
                # 解码 vocal + inst（尚未设备适配）
                vocal, v_sr = self._decode_only(vocal_data)
                inst, i_sr = self._decode_only(inst_data)

                # ── 0. 统一归一化（若启用）——在设备适配前，基于混合 RMS 算增益 ──
                if self.loudness_target_dbfs is not None:
                    try:
                        from .loudness import normalize_dual_tracks

                        # 两轨采样率必须一致才能混合计算 RMS；不一致时先对齐到人声采样率
                        if v_sr != i_sr:
                            logger.warning(
                                f"人声({v_sr}Hz) 与伴奏({i_sr}Hz) 采样率不一致，"
                                f"归一化前先对齐到 {v_sr}Hz"
                            )
                            inst, i_sr = self._resample(inst, i_sr, v_sr)

                        # 通道数也要对齐（mono 变 stereo 或反之）
                        v_channels = 1 if vocal.ndim == 1 else vocal.shape[1]
                        i_channels = 1 if inst.ndim == 1 else inst.shape[1]
                        if v_channels != i_channels:
                            target_ch = max(v_channels, i_channels)
                            vocal = self._adapt_channels(vocal, target_ch)
                            inst = self._adapt_channels(inst, target_ch)

                        vocal, inst = normalize_dual_tracks(
                            vocal,
                            inst,
                            target_dbfs=self.loudness_target_dbfs,
                        )
                        logger.debug(
                            f"双轨统一归一化完成（目标 {self.loudness_target_dbfs:.1f} dBFS）"
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"双轨归一化失败，使用原电平: {exc}")

                # ── 1. 解析人声目标设备（VB-Cable）+ 采样率/通道预适配 ──
                vocal_device_id = self._resolve_fallback_device_id()
                if vocal_device_id is None:
                    # 找不到 MME 备用 device id，退回原 _play_sync 路径（带 fallback 链）
                    logger.warning(
                        "play_dual 解析 VB-Cable MME device 失败，人声退回 _play_sync 路径"
                    )
                    vocal_data_final, vocal_sr_final = vocal, v_sr
                    vocal_channels = 1 if vocal.ndim == 1 else int(vocal.shape[1])
                    use_legacy_vocal = True
                else:
                    use_legacy_vocal = False
                    try:
                        vocal_dev_info = sd.query_devices(vocal_device_id)
                        v_target_rate = int(
                            float(self._read_attr(vocal_dev_info, "default_samplerate", 44100))
                        )
                        v_target_channels = int(
                            self._read_attr(vocal_dev_info, "max_output_channels", 2) or 2
                        )
                        if abs(v_sr - v_target_rate) > 10:
                            vocal, v_sr = self._resample(vocal, v_sr, v_target_rate)
                        vocal = self._adapt_channels(vocal, v_target_channels)
                    except Exception as exc:
                        logger.warning(f"人声路径采样率/通道适配失败: {exc}")
                    vocal_data_final, vocal_sr_final = vocal, v_sr
                    vocal_channels = 1 if vocal.ndim == 1 else int(vocal.shape[1])

                # ── 2. 解析伴奏目标设备（专用设备或系统默认）+ 采样率/通道预适配 ──
                # inst_output_device_id 为 None 时走系统默认（query kind="output"），
                # 否则查指定设备——把伴奏送到给直播采集的专用设备，与人声的 VB-Cable
                # 分开。
                inst_device_id = self.inst_output_device_id
                try:
                    if inst_device_id is None:
                        inst_dev = sd.query_devices(kind="output")
                    else:
                        inst_dev = sd.query_devices(inst_device_id)
                    i_target_rate = int(
                        float(self._read_attr(inst_dev, "default_samplerate", 44100))
                    )
                    i_target_channels = int(
                        self._read_attr(inst_dev, "max_output_channels", 2) or 2
                    )
                    if abs(i_sr - i_target_rate) > 10:
                        inst, i_sr = self._resample(inst, i_sr, i_target_rate)
                    inst = self._adapt_channels(inst, i_target_channels)
                except Exception as exc:
                    logger.warning(f"伴奏路径采样率/通道适配失败: {exc}")
                inst_channels = 1 if inst.ndim == 1 else int(inst.shape[1])

                # ── 3. 起播前算 envelope 喂 tracker（驱动 uLipSync 之外的内部口型估计）──
                envelope = compute_envelope(
                    vocal_data_final, vocal_sr_final, hop_seconds=_ENVELOPE_HOP_SECONDS
                )
                self.envelope_tracker.begin(envelope, _ENVELOPE_HOP_SECONDS)

                inst_target_desc = (
                    f"专用设备[{inst_device_id}]"
                    if inst_device_id is not None
                    else "系统扬声器"
                )
                logger.info(
                    f"双轨同步播放：人声 {len(vocal_data_final)}@{vocal_sr_final}Hz "
                    f"({vocal_channels}ch) → VB-Cable[{vocal_device_id}]，"
                    f"伴奏 {len(inst)}@{i_sr}Hz ({inst_channels}ch) → {inst_target_desc}"
                )

                # ── 4. 两阶段 Barrier：先交换 latency 做延迟补偿，再同步起跑 ──
                # latency_barrier：两路 stream 打开后在此汇合，交换各自的实际
                #   输出延迟（stream.latency）。
                # start_barrier：补偿（补静音帧）完成后在此汇合，对齐 write 起跑。
                import threading

                latency_barrier = threading.Barrier(2)
                start_barrier = threading.Barrier(2)
                # 共享延迟交换区：{"vocal": 秒, "inst": 秒}。两条线程各写一格，
                # 在 latency_barrier 之后两格都已就绪，可安全读取。
                latencies: dict[str, float] = {}
                # 两路播放成功标记；任一路失败则整段判失败，交由 play_dual 重试。
                results: dict[str, bool] = {"vocal": False, "inst": False}

                def _stream_output_latency(stream: Any) -> float:
                    """取 OutputStream 的输出延迟（秒）。

                    纯输出流 ``stream.latency`` 是单个 float；双工流是
                    ``(input, output)`` 二元组——取后者。异常时返回 0。
                    """

                    lat = getattr(stream, "latency", 0.0)
                    if isinstance(lat, (tuple, list)):
                        lat = lat[-1] if lat else 0.0
                    try:
                        return float(lat)
                    except (TypeError, ValueError):
                        return 0.0

                def _pad_leading_silence(data: Any, sr: int, seconds: float) -> Any:
                    """在 PCM 头部补 ``round(seconds × sr)`` 帧静音，对齐可听起点。"""

                    frames = int(round(max(0.0, seconds) * sr))
                    if frames <= 0:
                        return data
                    if data.ndim == 1:
                        pad = np.zeros(frames, dtype=data.dtype)
                    else:
                        pad = np.zeros((frames, data.shape[1]), dtype=data.dtype)
                    return np.concatenate([pad, data], axis=0)

                def _play_vocal_synced() -> None:
                    if use_legacy_vocal:
                        # fallback：无法读 stream.latency 做补偿，只能近似同步起跑。
                        # 让两路 barrier 都不挂死：补偿阶段直接放行。
                        try:
                            latencies["vocal"] = 0.0
                            latency_barrier.wait()
                            start_barrier.wait()
                        except threading.BrokenBarrierError:
                            return
                        results["vocal"] = self._play_sync(
                            vocal_data_final, vocal_sr_final
                        )
                        return
                    try:
                        with sd.OutputStream(
                            samplerate=vocal_sr_final,
                            channels=vocal_channels,
                            device=vocal_device_id,
                            latency="high",
                        ) as stream:
                            # 阶段一：读本路实际输出延迟，交换。
                            latencies["vocal"] = _stream_output_latency(stream)
                            latency_barrier.wait()
                            # 延迟补偿：本路延迟低于对方时，头部补静音对齐。
                            other = float(latencies.get("inst", 0.0))
                            data = vocal_data_final
                            if other > latencies["vocal"]:
                                data = _pad_leading_silence(
                                    data, vocal_sr_final, other - latencies["vocal"]
                                )
                            # 阶段二：对齐 write 起跑。
                            start_barrier.wait()
                            stream.write(data)
                            results["vocal"] = True
                    except threading.BrokenBarrierError:
                        return
                    except Exception as exc:
                        logger.error(f"人声同步播放失败: {exc}")
                        for b in (latency_barrier, start_barrier):
                            try:
                                b.abort()
                            except Exception:
                                pass

                def _play_inst_synced() -> None:
                    try:
                        with sd.OutputStream(
                            samplerate=i_sr,
                            channels=inst_channels,
                            device=inst_device_id,
                            latency="high",
                        ) as stream:
                            # 阶段一：读本路实际输出延迟，交换。
                            latencies["inst"] = _stream_output_latency(stream)
                            latency_barrier.wait()
                            # 延迟补偿：本路延迟低于对方时，头部补静音对齐。
                            other = float(latencies.get("vocal", 0.0))
                            data = inst
                            if other > latencies["inst"]:
                                data = _pad_leading_silence(
                                    data, i_sr, other - latencies["inst"]
                                )
                            # 阶段二：对齐 write 起跑。
                            start_barrier.wait()
                            stream.write(data)
                            results["inst"] = True
                    except threading.BrokenBarrierError:
                        return
                    except Exception as exc:
                        logger.error(f"伴奏同步播放失败: {exc}")
                        for b in (latency_barrier, start_barrier):
                            try:
                                b.abort()
                            except Exception:
                                pass

                loop = asyncio.get_running_loop()
                try:
                    await asyncio.gather(
                        loop.run_in_executor(None, _play_vocal_synced),
                        loop.run_in_executor(None, _play_inst_synced),
                    )
                finally:
                    self.envelope_tracker.end()
                logger.info("双轨播放完成。")
                return bool(results["vocal"] and results["inst"])
            except Exception as exc:
                self.envelope_tracker.end()
                logger.error(f"双轨播放时发生错误: {exc}")
                return False

    def _decode_and_normalize(self, audio_data: bytes) -> tuple[Any, int]:
        """解码 bytes → float32 PCM 并按统一目标做响度归一化。"""

        with io.BytesIO(audio_data) as buf:
            data, samplerate = sf.read(buf)
        if data.dtype != np.float32:
            data = data.astype(np.float32)
        if self.loudness_target_dbfs is not None:
            from .loudness import normalize_audio_array

            data = normalize_audio_array(data, target_dbfs=self.loudness_target_dbfs)
        return data, samplerate

    def _decode_only(self, audio_data: bytes) -> tuple[Any, int]:
        """仅解码 bytes → float32 PCM，**不做**任何响度归一化。

        双轨翻唱专用：人声 / 伴奏的相对音量比由 AU（或其它 DAW）导出时就调
        好了。如果两轨各自独立归一化会把这个精心调好的混音比例抹平，导致
        听感和 DAW 里不一致。这里原样保留导出电平，最贴近原始混音。
        """

        with io.BytesIO(audio_data) as buf:
            data, samplerate = sf.read(buf)
        if data.dtype != np.float32:
            data = data.astype(np.float32)
        return data, samplerate

    def _play_sync_default(self, data: Any, samplerate: int) -> None:
        """把伴奏同步播到系统默认输出设备（device=None），不做口型相关处理。

        伴奏只给观众听，不进 VB-Cable，所以无需 envelope、无需虚拟声卡 fallback
        那套逻辑。

        **不能用 ``sd.play()``**：它在 sounddevice 内部用全局 ``_last_stream``
        单例，``play_dual`` 里两路并发调用会互相 abort，最后只有一路活下来
        （表现为"只听到人声没有伴奏"）。这里改用显式 ``sd.OutputStream``
        阻塞写入——每个 stream 是独立对象，两路并发互不干扰。
        """

        try:
            # 通道适配：默认设备一般是 stereo，mono 数据需要扩展。
            try:
                default_dev = sd.query_devices(kind="output")
                target_channels = int(
                    self._read_attr(default_dev, "max_output_channels", 2) or 2
                )
            except Exception:
                target_channels = 2
            data = self._adapt_channels(data, target_channels)

            channels = 1 if data.ndim == 1 else int(data.shape[1])
            with sd.OutputStream(
                samplerate=samplerate,
                channels=channels,
                device=None,
                latency="high",
            ) as stream:
                stream.write(data)
        except Exception as exc:
            logger.error(f"伴奏播放到系统默认设备失败: {exc}")

    def _play_sync(self, data: Any, samplerate: int) -> bool:
        """同步播放，包含设备适配 + 主路径/MME 备用路径。

        **每次播放前重新解析 device id** ——sounddevice 的 device id 在 Windows
        上不是稳定的（拔插任何 USB 音频 / VB-Cable / 蓝牙耳机都会让 id 重新分
        配），缓存的 id 会过期触发 ``MME error 2: 使用的设备标识号已超出本地
        系统范围``。这一段无开销（``sd.query_devices`` 在 Windows 上 < 1ms）。

        Returns:
            ``True`` 表示某条路径成功播放完成；``False`` 表示所有路径（含终极
            回退到系统默认设备）都失败——调用方据此决定是否重置音频后端并重试。
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
                return True
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
                return True
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
                        return True
                    except Exception as exc2:
                        logger.error(f"MME 备用路径首次切换播放也失败: {exc2}")

        # 4) 终极回退：系统默认设备（可能丢失口型同步）
        try:
            logger.warning("尝试终极回退：系统默认输出设备")
            sd.play(data, samplerate, device=None, latency="high")
            sd.wait()
            return True
        except Exception as exc:
            logger.error(f"所有播放尝试均告失败: {exc}")
            return False

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
