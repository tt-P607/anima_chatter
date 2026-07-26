"""voice 模式的语音播放动作。

``say`` 把模型生成的文本送入 TTS 后端并播放。两种播放路径：

- **本地直接通话**（``platform == "local_asr"``）：走 ``backend.emit`` 发 voice
  消息给 asr_adapter，由它在本机扬声器播放。
- **平台通话**（QQ 等被临时接管的 stream）：直接调本地 AudioPlayer。**不能**
  走 emit——那会把 voice envelope 发给真实平台适配器，要么报错，要么把音频转
  成平台语音消息发出去，违反"电话里只有声音"的语义。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

from ..protocol import require_plugin
from ..runtime import call_state
from ..speech import (
    build_tts_backend,
    parse_speech_segments,
    synthesize_segments,
)
from ..runtime.heartbeat import feed_watchdog_during
from ._tts_schema import inject_tts_params


logger = get_logger("anima_chatter.action.say")


class SayAction(BaseAction):
    """在实时语音通话中说出一段话。"""

    name = "say"
    associated_types = ["voice", "text"]
    description = (
        "在实时语音通话中说出一段话。content 会进入 TTS 后端并由适配器播放。"
        "支持 [wait:n] 控制下一段播放前等待 n 秒（仅在需要长停顿、换气或思考时使用，"
        "普通说话请勿使用以保持自然连贯）。"
        "[wait] 只影响语音片段播放间隔，不会让聊天流等待；说完等待用户时请另外调用 pass_and_wait。"
    )
    chatter_allow = ["anima_chatter"]

    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        """动态注入 TTS Provider capabilities 定义的参数。

        Returns:
            注入 TTS 参数后的 action schema。
        """

        return inject_tts_params(super().to_schema())

    async def go_activate(self) -> bool:
        """voice 模式专用动作的可见性。

        在两种场景激活：本地直接通话（``platform == "local_asr"``），或当前
        stream 正处于通话中（platform 可能是 qq 等，anima_chatter 临时接管）。

        通话中**只能**暴露 say，不能暴露 say_and_perform——后者会发文本到原平台
        并驱动虚拟形象，与"打电话"语义不符。

        Returns:
            是否对模型可见。
        """

        if self.chat_stream.platform == "local_asr":
            return True
        return await call_state.is_call_active_for_stream(self.chat_stream.stream_id)

    async def execute(
        self,
        content: Annotated[
            str,
            "要通过 TTS 说出的内容。注意：跨语言表达必须拆分为多次 Action 调用，"
            "每轮调用仅包含一种成句语言并设置对应的 language 参数。",
        ],
        **tts_params: Any,
    ) -> tuple[bool, str]:
        """合成并播放语音。

        Args:
            content: 要说的内容，支持内联标记。
            **tts_params: TTS 参数（style / language / speed / effects 等），
                参数集完全由 TTS Provider 的 capabilities 定义。

        Returns:
            ``(是否成功, 给模型的执行结果描述)``。
        """

        plugin = require_plugin(self.plugin)
        config = plugin.config
        if config is None:
            return False, "插件配置缺失，无法合成语音"

        segments = parse_speech_segments(
            content or "", split_sentences=config.tts.sentence_split_enabled
        )
        if not segments:
            return True, "没有可播放的语音内容"

        stream_id = self.chat_stream.stream_id
        in_voice_call = await call_state.is_call_active_for_stream(stream_id)

        # 通话期间把 bot 说的话入档，供 voice_call.ended 事件透传给订阅方。
        if in_voice_call:
            spoken = " ".join(segment.text for segment in segments).strip()
            if spoken:
                await call_state.record_assistant_message(stream_id, spoken)

        audio_player = plugin.audio_player if in_voice_call else None
        if in_voice_call and audio_player is None:
            logger.warning(
                "通话中找不到 audio_player（VTB 资源未初始化？），本次音频无法播放"
            )

        backend = build_tts_backend(config.tts)
        tasks = synthesize_segments(
            backend=backend,
            stream_id=stream_id,
            segments=segments,
            tts_params=tts_params,
            section=config.tts,
        )

        played = 0
        # 整段合成 + 播放期间 generator 不会 yield，主动喂 watchdog。
        async with feed_watchdog_during(stream_id):
            for index, task in enumerate(tasks):
                artifact = await task
                segment = segments[index]
                if not artifact.is_playable or artifact.audio is None:
                    logger.error(f"跳过不可播放段落 {index}: {segment.text[:30]}...")
                    continue

                if segment.wait_before >= 0.1:
                    await asyncio.sleep(segment.wait_before)

                if in_voice_call:
                    if audio_player is None:
                        continue
                    await audio_player.play_audio(artifact.audio)
                elif not await backend.emit(artifact, self.chat_stream):
                    logger.error(f"段落 {index} 发送给适配器失败")
                    continue

                played += 1
                logger.info(
                    f"已播放段落 {index}"
                    f"（{'本地' if in_voice_call else '经适配器'}）: {segment.text[:20]}..."
                )

        return True, f"已播放 {played}/{len(segments)} 段语音"


__all__ = ["SayAction"]
