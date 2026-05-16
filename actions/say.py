"""voice_chatter 的 ASR 通话语音播放动作。

`SayAction` 把模型生成的文本送入 TTS 后端，再让适配器（asr_adapter）
按顺序播放。仅在 ``platform == "local_asr"`` 的实时通话流中激活。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import Failure
from src.core.components.base.action import BaseAction
from src.kernel.concurrency import get_task_manager

from ..config import SherpaOnnxVoiceChatterConfig
from ..markers import parse_speech_segments
from ..tts import TTSRequest, _retry_empty_audio, build_tts_backend


logger = get_logger("voice_chatter.action.say")


class SayAction(BaseAction):
    """把要说的话发送到 TTS 后端并交给适配器播放（语音通话模式）。"""

    action_name = "say"
    action_description = (
        "在实时语音通话中说出一段话。content 会进入 TTS 后端并由适配器播放。"
        "支持 [wait:n] 控制下一段播放前等待 n 秒（仅在需要长停顿、换气或思考时使用，"
        "普通说话请勿使用以保持自然连贯）。"
        "[wait] 只影响语音片段播放间隔，不会让聊天流等待；说完等待用户时请另外调用 pass_and_wait。"
    )
    chatter_allow = ["voice_chatter"]
    associated_platforms = ["local_asr"]
    dependencies = ["asr_adapter:adapter:asr_adapter"]

    async def go_activate(self) -> bool:
        """仅在 ``local_asr`` 平台激活，避免与 vtb 模式 action 同时暴露。"""

        return self.chat_stream.platform == "local_asr"

    async def execute(
        self,
        content: Annotated[str, "要通过 TTS 说出的内容，可包含 [wait:n] 和 [emotion:name] 标记"],
    ) -> tuple[bool, str]:
        """执行语音播放动作。"""

        plugin_config = getattr(self.plugin, "config", None)
        split_enabled = True
        max_parallel = 4
        empty_audio_retry_count = 1
        if isinstance(plugin_config, SherpaOnnxVoiceChatterConfig):
            split_enabled = bool(plugin_config.tts.sentence_split_enabled)
            max_parallel = int(plugin_config.tts.max_parallel_segments)
            empty_audio_retry_count = int(plugin_config.tts.empty_audio_retry_count)

        segments = parse_speech_segments(content or "", split_sentences=split_enabled)
        if not segments:
            return True, "没有可播放的语音内容"

        backend = build_tts_backend(plugin_config, logger)

        async def process_segment(seg: Any, idx: int):
            """合成单个片段；失败时重试或返回 Failure。"""

            try:
                artifact = await backend.synthesize(
                    TTSRequest(
                        stream_id=self.chat_stream.stream_id,
                        text=seg.text,
                        emotion=seg.emotion,
                        markers=seg.markers,
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

        tm = get_task_manager()
        _ = tm  # task_manager 在此函数内未直接使用，留作未来 trace 标识；保留导入避免 lint
        semaphore = asyncio.Semaphore(max_parallel)

        async def sem_process(seg: Any, idx: int):
            async with semaphore:
                return await process_segment(seg, idx)

        tasks = [sem_process(seg, i) for i, seg in enumerate(segments)]

        next_to_play = 0
        completed_artifacts: dict[int, Any] = {}
        success_count = 0

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

                    if await backend.emit(current_art, self.chat_stream):
                        success_count += 1
                        logger.info(
                            f"已发送播放段落 {current_idx}: {current_seg.text[:20]}..."
                        )

                next_to_play += 1

        return True, f"已流水线处理 {success_count}/{len(segments)} 段语音播放"


__all__ = ["SayAction"]
