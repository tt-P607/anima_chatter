"""vtb / vtb_live 模式的虚拟形象表演动作。

流程：

1. 把文本发送到当前聊天流（让用户先看到字）。
2. 解析内联标记并按句切分。
3. 并发合成 TTS 音频。
4. 交给 [`speech/playback.py`](../speech/playback.py:1) 播放——``vtb_live`` 且
   配置启用流水线时走"reserve + 后台播放"，其余情况阻塞播完。

VTS 未启用或未连接时会降级为"仅本地播放"，音频依然进 VB-Cable，虚拟形象的
嘴型还能靠 VTS 自带的麦克风驱动跟上。
"""

from __future__ import annotations

from typing import Annotated, Any, AsyncGenerator

from src.app.plugin_system.api import send_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

from ..modes import resolve_mode
from ..prompts.scenes import EMOTION_SCHEMA_DESC, INTENT_SCHEMA_DESC
from ..protocol import require_plugin
from ..runtime import call_state
from ..speech import (
    PerformanceStyle,
    SpeechSegment,
    build_tts_backend,
    dispatch_segments_pipelined,
    estimate_segments_duration,
    parse_speech_segments,
    play_segments_blocking,
    should_use_pipeline,
    strip_markers,
    synthesize_segments,
)
from ._tts_schema import inject_tts_params


logger = get_logger("anima_chatter.action.say_and_perform")


_CONTENT_DESC = (
    "要说的内容，按发送顺序排列的字符串列表。\n"
    "【分段规则 — 极其重要】：\n"
    "- 默认不分段！能放一段说完就传单元素列表 [\"...\"]\n"
    "- 只有内容确实很长（说出来超过 30 秒）时才拆分多段\n"
    "- 拆分时在语义转折/话题切换/情绪变化的自然断点处分开，不要在句子中间生硬截断\n"
    "- 错误示范：[\"嗯...\", \"我想想\", \"好吧\"] ← 太碎了！\n"
    "- 正确示范：[\"嗯...我想想，好吧那我跟你说说这件事吧。\"] ← 合为一段\n"
    "【内容要求】：\n"
    "- 适合朗读：短句、自然、口语化，避免 Markdown、列表、(笑)/[动作] 等无法朗读的标记\n"
    "- 善用标点传递情绪：感叹号惊讶兴奋、问号疑问好奇、省略号犹豫思考\n"
    "- 灵活使用语气词：诶咦哇呀啊（惊讶）、嗯唔额（思考）、嘛呐嘻（撒娇）\n"
    "【跨参数拆分准则】：\n"
    "- 同一次调用所有段落强制共享同一个 emotion / intent / language / style，无法分段切换。\n"
    "- **跨情绪**：必须拆分为多次 Action 调用。\n"
    "- **跨语言**：当出现不同语种的成句表达时，必须拆分为多次 Action 调用。"
    "避免在一次调用中通过 auto 模式混合多语种，以维持音色稳定。\n"
    "【行内 motion 标记 — 高级用法】：\n"
    "- 可以在 content 里用 [motion:NAME]...[/motion] 临时切换 intent 动作，"
    "让句子中段做不同动作。NAME 取值同 intent（如 EXCITED / SHY_DOWN / PROUD_LIFT）\n"
    "- 例子：\"哎呀[motion:SHY_DOWN]这真是太突然了[/motion]，[motion:EXCITED]不过我很喜欢！[/motion]\"\n"
    "- 标记块外 / 标记结束后自动回到顶层 intent\n"
    "- 不必每段都用——只在一句话里语义明显切换时用，过度切换反而显得机械"
)


class SayAndPerformAction(BaseAction):
    """通过虚拟形象说一段话并设定情绪与动作意图。"""

    name = "say_and_perform"
    associated_types = ["voice", "text"]
    description = (
        "通过 VTube Studio 虚拟形象说一段话。会同时把文本发到当前聊天，"
        "用 TTS 朗读，并驱动虚拟形象的嘴型 + 表情 + 头部姿态。"
        "支持 [wait:n] 控制下一段播放前等待 n 秒（仅需要长停顿时使用）。"
        "说完等待用户继续说话时，请另外调用 pass_and_wait。"
    )
    chatter_allow = ["anima_chatter"]
    primary_action = True

    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        """动态注入 TTS Provider capabilities 定义的参数。

        Returns:
            注入 TTS 参数后的 action schema。
        """

        return inject_tts_params(super().to_schema())

    async def go_activate(self) -> bool:
        """vtb 系模式专用动作的可见性。

        两个条件同时满足才暴露：``platform != "local_asr"``，且当前 stream 不在
        通话中（通话中应该用 say，避免出现"打电话还往群里发字"的行为）。

        Returns:
            是否对模型可见。
        """

        if self.chat_stream.platform == "local_asr":
            return False
        return not await call_state.is_call_active_for_stream(
            self.chat_stream.stream_id
        )

    async def execute(
        self,
        content: Annotated[list[str], _CONTENT_DESC],
        emotion: Annotated[str, EMOTION_SCHEMA_DESC] = "neutral:1",
        intent: Annotated[str, INTENT_SCHEMA_DESC] = "NARRATING",
        **tts_params: Any,
    ) -> AsyncGenerator[tuple[bool, str] | None, None]:
        """发文本 → 合成 TTS → 播放 + 驱动虚拟形象。

        本方法是**异步生成器**：发文本与合成起跑等准备工作在首个 ``yield None``
        之前完成（多个 action 的合成会并发进行），真正占用播放资源的关键段放在
        ``yield None`` 之后——调度器按 LLM 的 tool call 顺序放行，保证"先说话后
        唱歌"这类顺序依赖与调用顺序一致。

        Args:
            content: 要说的内容列表。
            emotion: ``"类型:强度"`` 格式的情绪标记。
            intent: 顶层动作意图。
            **tts_params: TTS 参数，由 Provider capabilities 定义。

        Yields:
            首次 yield ``None`` 作为顺序门；随后 yield ``(是否成功, 结果描述)``。
        """

        # 兼容模型偶尔传 str（schema 声明的是 list[str]）。
        raw_items: list[str] = [content] if isinstance(content, str) else list(content or [])
        content_list: list[str] = [
            str(item) for item in raw_items if str(item).strip()
        ]
        if not content_list:
            yield False, "content 不能为空"
            return

        plugin = require_plugin(self.plugin)
        config = plugin.config
        if config is None:
            logger.error("插件配置缺失，无法执行 vtb 表演")
            yield False, "插件配置缺失"
            return

        segments = self._parse_all(content_list, split_sentences=config.tts.sentence_split_enabled)
        if not segments:
            logger.info("解析后无可播放片段，跳过本次调用")
            yield True, "无可朗读内容"
            return

        await self._send_texts(content_list)

        performer = plugin.get_active_performer()
        audio_player = plugin.audio_player
        if performer is None and audio_player is None:
            logger.warning("VTS 表演器与音频播放器均未初始化，已发送文本但无法播放语音")
            yield True, "已发送文本（无音频输出）"
            return

        stream_id = self.chat_stream.stream_id
        tasks = synthesize_segments(
            backend=build_tts_backend(config.tts),
            stream_id=stream_id,
            segments=segments,
            tts_params=tts_params,
            section=config.tts,
        )

        style = PerformanceStyle.create(emotion, intent)
        estimated = estimate_segments_duration(segments)
        use_pipeline = should_use_pipeline(
            is_live_mode=resolve_mode(self.chat_stream) == "vtb_live",
            section=config.pipelining,
            estimated_duration=estimated,
        )

        # 顺序门：在占用播放资源（reserve / performer 锁）前让出，调度器会按
        # tool call 顺序放行。
        yield None

        if use_pipeline:
            yield await dispatch_segments_pipelined(
                stream_id=stream_id,
                tasks=tasks,
                segments=segments,
                performer=performer,
                audio_player=audio_player,
                style=style,
                estimated_duration=estimated,
            )
            return

        yield await play_segments_blocking(
            stream_id=stream_id,
            tasks=tasks,
            segments=segments,
            performer=performer,
            audio_player=audio_player,
            style=style,
        )

    @staticmethod
    def _parse_all(
        content_list: list[str],
        *,
        split_sentences: bool,
    ) -> list[SpeechSegment]:
        """把每段 content 解析为片段列表。

        解析不出片段时（例如整段都是标记）兜底剥掉标记后整段塞回去。

        Args:
            content_list: 原始内容列表。
            split_sentences: 是否按句切分。

        Returns:
            合并后的片段列表。
        """

        segments: list[SpeechSegment] = []
        for raw in content_list:
            text = raw.strip()
            if not text:
                continue
            parsed = parse_speech_segments(text, split_sentences=split_sentences)
            if parsed:
                segments.extend(parsed)
                continue
            fallback = strip_markers(text)
            if fallback:
                segments.append(SpeechSegment(text=fallback))
        return segments

    async def _send_texts(self, content_list: list[str]) -> None:
        """把文本发送到聊天流，并标记"刚回复"以提升下一 tick 的响应概率。

        关键：文本与动作是两条独立轨道——TTS / VTS 按片段（含 motion 切片）走，
        但**文本**只按 content 列表的每个元素发一次（剥离全部标记的整段）。这样
        群里看到的是完整一段话，而不是被 motion 标记切碎的多条短消息。

        Args:
            content_list: 原始内容列表。
        """

        for raw in content_list:
            clean = strip_markers(raw)
            if not clean:
                continue
            sent = await send_api.send_text(
                content=clean,
                stream_id=self.chat_stream.stream_id,
                platform=self.chat_stream.platform,
            )
            if not sent:
                logger.warning(
                    f"发送文本失败 stream={self.chat_stream.stream_id}: {clean[:30]}..."
                )

        # 局部导入避免循环依赖（chatter 包会引用本模块所在的 actions 包）。
        from ..chatter.attention import mark_reply_success

        mark_reply_success(self.chat_stream)


__all__ = ["SayAndPerformAction"]
