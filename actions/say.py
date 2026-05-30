"""anima_chatter 的 ASR 通话语音播放动作。

`SayAction` 把模型生成的文本送入 TTS 后端，再让适配器（asr_adapter）
按顺序播放。仅在 ``platform == "local_asr"`` 的实时通话流中激活。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import Failure
from src.core.components.base.action import BaseAction

# call_state 移至方法内局部延迟导入，以防模块初始化时循环导入
from ..config import AnimaChatterConfig
from ..heartbeat import feed_watchdog_during
from ..markers import parse_speech_segments
from ..prompts.scenes import LANGUAGE_SCHEMA_DESC
from ..tts import TTSRequest, _retry_empty_audio, build_tts_backend


logger = get_logger("anima_chatter.action.say")


class SayAction(BaseAction):
    """把要说的话发送到 TTS 后端并交给适配器播放（语音通话模式）。"""

    action_name = "say"
    action_description = (
        "在实时语音通话中说出一段话。content 会进入 TTS 后端并由适配器播放。"
        "支持 [wait:n] 控制下一段播放前等待 n 秒（仅在需要长停顿、换气或思考时使用，"
        "普通说话请勿使用以保持自然连贯）。"
        "[wait] 只影响语音片段播放间隔，不会让聊天流等待；说完等待用户时请另外调用 pass_and_wait。"
    )
    chatter_allow = ["anima_chatter"]
    associated_platforms = ["local_asr"]
    dependencies = ["asr_adapter_anima:adapter:asr_adapter_anima"]

    async def go_activate(self) -> bool:
        """voice 模式专用 action 的可见性。

        在两种场景激活：
        1. ``platform == "local_asr"``：本地直接通话；
        2. **当前 stream 正处于 voice_call 通话中**（platform 可能是 qq 等）：
           anima_chatter 临时接管原 stream 的语音通话场景。

        关键约束：通话中**只能**让 say 暴露给模型，**不能**让 say_and_perform
        暴露——后者会发送文本到原平台 + 驱动 VTube Studio，与"打电话"的语义
        不符（电话只该有声音，不该出现文字 + 形象表演）。
        """

        if self.chat_stream.platform == "local_asr":
            return True
        from .. import call_state as _cs
        return await _cs.is_call_active_for_stream(self.chat_stream.stream_id)

    async def execute(
        self,
        content: Annotated[str, "要通过 TTS 说出的内容，可包含 [wait:n] 和 [emotion:name] 标记"],
        style: Annotated[
            str,
            "TTS 语音风格。可选：default（默认中性，绝大多数场景用这个）、"
            "活泼（俏皮明亮，开心调皮时用）、难过（柔软低沉，共情失落时用）。"
            "切风格只在情绪明显起伏时用，平时保持 default。",
        ] = "default",
        language: Annotated[str, LANGUAGE_SCHEMA_DESC] = "zh",
    ) -> tuple[bool, str]:
        """执行语音播放动作。"""

        plugin_config = getattr(self.plugin, "config", None)
        split_enabled = True
        max_parallel = 4
        empty_audio_retry_count = 1
        if isinstance(plugin_config, AnimaChatterConfig):
            split_enabled = bool(plugin_config.tts.sentence_split_enabled)
            max_parallel = int(plugin_config.tts.max_parallel_segments)
            empty_audio_retry_count = int(plugin_config.tts.empty_audio_retry_count)

        segments = parse_speech_segments(content or "", split_sentences=split_enabled)
        if not segments:
            return True, "没有可播放的语音内容"

        # 通话期间记录 bot 说的话——把所有 segment 的 text 拼起来作为本次发言。
        # 这样 voice_call.ended 事件 payload 才能完整包含通话中的 user/assistant 对。
        from .. import call_state as _cs
        in_voice_call = await _cs.is_call_active_for_stream(self.chat_stream.stream_id)
        if in_voice_call:
            spoken_text = " ".join(seg.text for seg in segments if seg.text).strip()
            if spoken_text:
                await _cs.record_assistant_message(
                    self.chat_stream.stream_id, spoken_text
                )

        # 通话场景下用插件初始化的 audio_player 本地播放（绕开 message_send 链）。
        # 本地直接通话（platform=local_asr）保持原行为：用 backend.emit 发 voice
        # envelope 给 asr_adapter 播放——这条路径已经工作了很久，无须改。
        audio_player = getattr(self.plugin, "audio_player", None) if in_voice_call else None
        if in_voice_call and audio_player is None:
            logger.warning(
                "通话中 SayAction 找不到 plugin.audio_player（VTB 资源未初始化？），"
                "本次音频将无法播放。"
            )

        backend = build_tts_backend(plugin_config, logger)

        # 把 style / language 透传给 TTS provider：通过 TTSRequest.markers 字段。
        # 详见 say_and_perform.py 同名注释。
        def _build_markers_for_seg(seg: Any) -> dict[str, Any]:
            base = dict(getattr(seg, "markers", None) or {})
            base.setdefault("style", style)
            base.setdefault("language", language)
            return base

        async def process_segment(seg: Any, idx: int):
            """合成单个片段；失败时重试或返回 Failure。"""

            try:
                artifact = await backend.synthesize(
                    TTSRequest(
                        stream_id=self.chat_stream.stream_id,
                        text=seg.text,
                        emotion=seg.emotion,
                        markers=_build_markers_for_seg(seg),
                    )
                )
                if not artifact.audio and empty_audio_retry_count > 0:
                    artifact = await _retry_empty_audio(
                        backend=backend,
                        stream_id=self.chat_stream.stream_id,
                        segment=seg,
                        artifact=artifact,
                        retry_count=empty_audio_retry_count,
                    )
                return idx, artifact
            except Exception as exc:
                logger.error(f"TTS 合成段落 {idx} 失败: {exc}")
                return idx, Failure(str(exc))

        # 注：以前这里取 task_manager 留作未来 trace 标识，但实际从未使用，
        # 直接删除避免引入对内部 ``src.kernel.concurrency`` 的依赖。
        semaphore = asyncio.Semaphore(max_parallel)

        async def sem_process(seg: Any, idx: int):
            async with semaphore:
                return await process_segment(seg, idx)

        tasks = [sem_process(seg, i) for i, seg in enumerate(segments)]

        next_to_play = 0
        completed_artifacts: dict[int, Any] = {}
        success_count = 0

        # 整段合成 + 播放期间 generator 会 await 几十秒不 yield，主动喂 watchdog
        # 防止 stream_warning_threshold / stream_restart_threshold 误触发。
        async with feed_watchdog_during(self.chat_stream.stream_id):
            for task in asyncio.as_completed(tasks):
                idx, artifact = await task
                completed_artifacts[idx] = artifact

                while next_to_play in completed_artifacts:
                    current_idx = next_to_play
                    current_art = completed_artifacts.pop(current_idx)
                    current_seg = segments[current_idx]

                    if isinstance(current_art, Failure) or (
                        hasattr(current_art, "metadata")
                        and cast(dict, current_art.metadata).get("error")
                    ):
                        logger.error(f"跳过播放失败段落 {current_idx}: {current_seg.text}")
                    elif not current_art.audio:
                        logger.error(f"跳过无音频段落 {current_idx}: {current_seg.text}")
                    else:
                        if current_seg.wait_before >= 0.1:
                            await asyncio.sleep(current_seg.wait_before)

                        # 关键路由分流：
                        # - 本地直接通话（platform=local_asr）：走 backend.emit
                        #   发 voice envelope → asr_adapter._send_platform_message
                        #   → 本机扬声器播放。这是 SayAction 的原行为。
                        # - QQ 等平台被 anima_chatter 接管的"打电话"场景：**不能**
                        #   走 emit——emit 用 chat_stream.platform 路由，会把 voice
                        #   envelope 发给 napcat 等真平台适配器，那边要么报错要么
                        #   把音频转成 QQ 语音消息发出去（违反"打电话只有声音"的
                        #   语义）。改为直接调插件的 audio_player 本地扬声器播放。
                        played = False
                        if (
                            in_voice_call
                            and audio_player is not None
                            and current_art.audio
                        ):
                            try:
                                await audio_player.play_audio(current_art.audio)
                                played = True
                            except Exception as exc:
                                logger.error(
                                    f"通话中本地播放音频失败 段{current_idx}: {exc}",
                                    exc_info=True,
                                )
                        elif not in_voice_call:
                            played = await backend.emit(current_art, self.chat_stream)

                        if played:
                            success_count += 1
                            logger.info(
                                f"已播放段落 {current_idx} ({'本地' if in_voice_call else '通过适配器'}): "
                                f"{current_seg.text[:20]}..."
                            )

                    next_to_play += 1

        return True, f"已流水线处理 {success_count}/{len(segments)} 段语音播放"


__all__ = ["SayAction"]
