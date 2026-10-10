"""直播文本、PCM 流式语音与虚拟形象表演动作。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated, Any, ClassVar

from src.app.plugin_system.api import send_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

from ..prompts.scenes import EMOTION_SCHEMA_DESC, INTENT_SCHEMA_DESC
from ..protocol import require_plugin
from ..speech import (
    PerformanceStyle,
    SpeechSegment,
    estimate_segments_duration,
    strip_markers,
)
from ..speech.backend import get_tts_service
from ..speech.markers import parse_pause_segments
from ..speech.streaming import play_streaming_segments
from ._tts_schema import inject_tts_params

logger = get_logger("anima_chatter.action.say_and_perform")


_CONTENT_DESC = (
    "要说的完整内容，按顺序排列的字符串列表。\n"
    "- 默认用单元素列表；同一次调用的列表项按换行合并为完整朗读文本。\n"
    "- 不按句号、逗号、长度或动作拆分 TTS 请求。\n"
    "- 只有需要明确停顿时使用 [wait:n]，n 是静音秒数；标点仍负责自然语气。\n"
    '- 例子：["先说这件事。[wait:2]再说下一件事。"]\n'
    "【内容要求】：\n"
    "- 适合朗读：短句、自然、口语化，避免 Markdown、列表、(笑)/[动作] 等无法朗读的标记\n"
    "- 善用标点传递情绪：感叹号惊讶兴奋、问号疑问好奇、省略号犹豫思考\n"
    "- 灵活使用语气词：诶咦哇呀啊（惊讶）、嗯唔额（思考）、嘛呐嘻（撒娇）\n"
    "【回复级参数】：\n"
    "- 同一次调用共享同一个 emotion / intent / language / style，包括显式停顿两侧。\n"
    "- 表演使用顶层 emotion / intent，不使用行内 motion / emotion 标记切换；"
    "这些行内标记会剥离，但不会切分音频或改变表演。\n"
    "- 需要不同语言或合成风格时使用不同 Action 调用。"
    "避免在一次调用中通过 auto 模式混合多语种，以维持音色稳定。\n"
)


class SayAndPerformAction(BaseAction):
    """通过虚拟形象说一段话并设定情绪与动作意图。"""

    name = "say_and_perform"
    associated_types: ClassVar[list[str]] = ["voice", "text"]
    description = (
        "通过 VTube Studio 虚拟形象说一段话。会同时把文本发到当前聊天，"
        "用 TTS 朗读，并驱动虚拟形象的嘴型 + 表情 + 头部姿态。"
        "支持 [wait:n] 插入 n 秒静音，后续文本提前合成，整条回复连续播放。"
        "说完等待用户继续说话时，请另外调用 pass_and_wait。"
    )
    chatter_allow: ClassVar[list[str]] = ["anima_chatter"]
    primary_action = True

    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        """动态注入 TTS Provider capabilities 定义的参数。

        Returns:
            注入 TTS 参数后的 action schema。
        """

        return inject_tts_params(super().to_schema())

    async def go_activate(self) -> bool:
        """只向直播流开放虚拟形象说话动作。"""

        return self.chat_stream.platform == "live"

    async def execute(
        self,
        content: Annotated[list[str], _CONTENT_DESC],
        emotion: Annotated[str, EMOTION_SCHEMA_DESC] = "neutral:1",
        intent: Annotated[str, INTENT_SCHEMA_DESC] = "NARRATING",
        **tts_params: Any,
    ) -> AsyncGenerator[tuple[bool, str] | None, None]:
        """提交流式说话项，实际起播时同步文本与虚拟形象。

        首个 ``yield None`` 是工具调用顺序门；通过后登记容量并提交后台任务。
        后台尽快缓存整条回复，拒排与未起播取消不发送文本。

        Args:
            content: 要说的内容列表。
            emotion: ``"类型:强度"`` 格式的情绪标记。
            intent: 顶层动作意图。
            **tts_params: TTS 参数，由 Provider capabilities 定义。

        Yields:
            首次 yield ``None`` 作为顺序门；随后 yield ``(是否成功, 结果描述)``。
        """

        # 兼容模型偶尔传 str（schema 声明的是 list[str]）。
        raw_items: list[str] = (
            [content] if isinstance(content, str) else list(content or [])
        )
        content_list: list[str] = [str(item) for item in raw_items if str(item).strip()]
        if not content_list:
            yield False, "content 不能为空"
            return

        plugin = require_plugin(self.plugin)
        config = plugin.config
        if config is None:
            logger.error("插件配置缺失，无法执行 vtb 表演")
            yield False, "插件配置缺失"
            return

        segments = self._parse_all(content_list)
        if not any(segment.text for segment in segments):
            logger.info("解析后无可播放片段，跳过本次调用")
            yield True, "无可朗读内容"
            return

        performer = plugin.get_active_performer()
        audio_player = plugin.audio_player
        if audio_player is None:
            logger.warning("音频播放器未初始化，无法提交语音")
            yield False, "音频播放器未初始化"
            return

        stream_id = self.chat_stream.stream_id
        try:
            service = get_tts_service()
        except (RuntimeError, TypeError) as error:
            yield False, str(error)
            return

        style = PerformanceStyle.create(emotion, intent)
        estimated = estimate_segments_duration(segments)

        async def on_segment_started(_index: int) -> None:
            """整条回复首次起播时发送其完整文本。"""

            await self._send_texts([segment.text for segment in segments if segment.text])

        # 顺序门：在占用播放资源（reserve / performer 锁）前让出，调度器会按
        # tool call 顺序放行。
        yield None

        yield await play_streaming_segments(
            stream_id=stream_id,
            provider=service,
            segments=segments,
            tts_params=tts_params,
            performer=performer,
            audio_player=audio_player,
            style=style,
            estimated_duration=estimated,
            on_segment_started=on_segment_started,
        )

    @staticmethod
    def _parse_all(content_list: list[str]) -> list[SpeechSegment]:
        """合并同一次调用的文本，仅保留显式停顿边界。

        Args:
            content_list: 原始内容列表。

        Returns:
            按显式停顿分隔的片段列表。
        """

        return parse_pause_segments("\n".join(content_list))

    async def _send_texts(self, content_list: list[str]) -> None:
        """在整条回复起播时发送文本，成功后更新回复注意力状态。

        Args:
            content_list: 原始内容列表。
        """

        any_sent = False
        for raw in content_list:
            clean = strip_markers(raw)
            if not clean:
                continue
            sent = await send_api.send_text(
                content=clean,
                stream_id=self.chat_stream.stream_id,
                platform=self.chat_stream.platform,
            )
            any_sent = any_sent or sent
            if not sent:
                logger.warning("实际起播片段的文本发送失败")

        # 局部导入避免循环依赖（chatter 包会引用本模块所在的 actions 包）。
        from ..chatter.attention import mark_reply_success

        if any_sent:
            mark_reply_success(self.chat_stream)


__all__ = ["SayAndPerformAction"]
