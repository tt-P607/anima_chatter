"""anima_chatter 的 VTB 虚拟形象表演动作。

`SayAndPerformAction` 在非 ``local_asr`` 平台（即 vtb 模式）激活：

1. 立即把文本发送到当前聊天流（让用户先看到字）。
2. 解析 markers 并按句切分（流水线合成，保持顺序播放）。
3. 通过 :class:`HttpTTSBackend` 合成音频（**不调用 emit**——vtb 不发 voice 给 adapter）。
4. 把音频交给 :class:`VTSPerformer.perform`：本地 AudioPlayer 输出到 VB-Cable，
   同时让 VTS SpeechAnimator 进入说话状态（嘴型/动作同步）。

如果 VTS 未启用或未连接，performer 会降级为"仅本地播放"，依然会进 VB-Cable，
让虚拟形象的嘴型还能跟上（通过 VTS 自带的麦克风口型驱动）。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, cast

from src.app.plugin_system.api import send_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import Failure
from src.core.components.base.action import BaseAction

from ..config import AnimaChatterConfig
from ..heartbeat import feed_watchdog_during
from ..markers import parse_speech_segments, strip_markers
from ..prompts.scenes import (
    EMOTION_SCHEMA_DESC,
    INTENT_SCHEMA_DESC,
    LANGUAGE_SCHEMA_DESC,
)
from ..tts import TTSRequest, _retry_empty_audio, build_tts_backend


logger = get_logger("anima_chatter.action.say_and_perform")


class SayAndPerformAction(BaseAction):
    """通过虚拟形象（VTube Studio）说一段话并设定情绪与意图（vtb 模式）。"""

    action_name = "say_and_perform"
    action_description = (
        "通过 VTube Studio 虚拟形象说一段话。会同时把文本发到当前聊天，"
        "用 TTS 朗读，并驱动虚拟形象的嘴型 + 表情 + 头部姿态。"
        "支持 [wait:n] 控制下一段播放前等待 n 秒（仅需要长停顿时使用）。"
        "说完等待用户继续说话时，请另外调用 pass_and_wait。"
    )
    chatter_allow = ["anima_chatter"]
    primary_action = True

    async def go_activate(self) -> bool:
        """vtb 模式专用 action 的可见性。

        激活条件（同时满足）：
        - ``platform != "local_asr"`` （local_asr 用 :class:`SayAction`）；
        - **当前 stream 不处于 voice_call 通话中**——通话中应使用 say
          （只 TTS 不发文本不驱动 VTS），不应让 say_and_perform 暴露，否则
          会出现"打电话还往群里发字 + 驱动虚拟形象"的不符合电话语义的行为。
        """

        if self.chat_stream.platform == "local_asr":
            return False
        # 通话中：让位给 say，避免双重 action 暴露
        from .. import call_state as _cs

        if await _cs.is_call_active_for_stream(self.chat_stream.stream_id):
            return False
        return True

    async def execute(
        self,
        content: Annotated[
            list[str],
            (
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
                "- 同一次调用所有段落共享同一个 emotion / intent，跨情绪时要拆成多次调用\n"
                "【行内 motion 标记 — 高级用法】：\n"
                "- 可以在 content 里用 [motion:NAME]...[/motion] 临时切换 intent 动作，"
                "让句子中段做不同动作。NAME 取值同 intent（如 EXCITED / SHY_DOWN / PROUD_LIFT）\n"
                "- 例子：\"哎呀[motion:SHY_DOWN]这真是太突然了[/motion]，[motion:EXCITED]不过我很喜欢！[/motion]\"\n"
                "- 标记块外 / 标记结束后自动回到顶层 intent\n"
                "- 不必每段都用——只在一句话里语义明显切换时用，过度切换反而显得机械"
            ),
        ],
        emotion: Annotated[str, EMOTION_SCHEMA_DESC] = "neutral:1",
        intent: Annotated[str, INTENT_SCHEMA_DESC] = "NARRATING",
        style: Annotated[
            str,
            "TTS 语音风格。可选：default（中性叙述，默认）/ 活泼（俏皮明亮，开心调皮时用）/ "
            "难过（柔软低沉，共情失落时用）。情绪明显起伏时再换，平时保持 default。",
        ] = "default",
        language: Annotated[str, LANGUAGE_SCHEMA_DESC] = "zh",
    ) -> tuple[bool, str]:
        """发文本 → TTS 合成 → 本地播放 + VTS 表演。"""

        # 1) 规范化输入：兼容模型偶尔传 str（虽然 schema 是 list[str]）。
        if isinstance(content, str):
            content_list: list[str] = [content]
        else:
            content_list = [str(item) for item in (content or []) if str(item).strip()]

        if not content_list:
            return False, "content 不能为空"

        plugin_config = getattr(self.plugin, "config", None)
        if not isinstance(plugin_config, AnimaChatterConfig):
            logger.error("插件配置缺失，无法执行 vtb 表演")
            return False, "插件配置缺失"

        split_enabled = bool(plugin_config.tts.sentence_split_enabled)
        max_parallel = max(1, int(plugin_config.tts.max_parallel_segments))
        empty_audio_retry_count = int(plugin_config.tts.empty_audio_retry_count)

        # 2) 把每个 content 段解析出 SpeechSegment（去掉 [wait]/[emotion] 标记，可选切句）。
        segments: list[Any] = []
        for raw in content_list:
            text = (raw or "").strip()
            if not text:
                continue
            parsed = parse_speech_segments(text, split_sentences=split_enabled)
            if parsed:
                segments.extend(parsed)
            else:
                # parse 不出片段（例如纯标记内容）：兜底剥掉标记后整段塞回去。
                fallback_text = strip_markers(text)
                if fallback_text:
                    from ..markers import SpeechSegment

                    segments.append(SpeechSegment(text=fallback_text))

        if not segments:
            logger.info("VTB 解析后无可播放片段，跳过本次调用。")
            return True, "无可朗读内容"

        # 3) 发送文本到聊天流。
        # 关键：文本和动作是两条独立轨道——TTS / VTS 按 segment（含 motion 标记
        # 切片）走，但**文本**只按 content 列表的每个元素发一次（剥离全部标记
        # 的整段）。这样群里看到的是完整一段话，而不是被 motion 标记切碎的多
        # 条短消息。
        for raw_text in content_list:
            clean = strip_markers(raw_text)
            if not clean:
                continue
            ok = await send_api.send_text(
                content=clean,
                stream_id=self.chat_stream.stream_id,
                platform=self.chat_stream.platform,
            )
            if not ok:
                logger.warning(
                    f"VTB 模式发送文本失败 stream_id={self.chat_stream.stream_id}: {clean[:30]}..."
                )

        # 文本发送成功 → 标记下一 tick 的概率门加成（沿用 dfc 的"刚回复就再回复"心理）。
        # 局部导入避免循环依赖（plugin.py 顶层引用本模块）。
        from ..plugin import AnimaChatter

        AnimaChatter.mark_reply_success(self.chat_stream)

        # 3) 拿 performer / audio_player
        performer = getattr(self.plugin, "vts_performer", None)
        audio_player = getattr(self.plugin, "audio_player", None)

        if performer is None and audio_player is None:
            logger.warning(
                "VTSPerformer 与 AudioPlayer 均未初始化（可能 audio 配置缺失），"
                "已发送文本但无法播放语音。"
            )
            return True, "已发送文本（无音频输出）"

        # 4) 流水线合成（限并发） + 顺序播放
        backend = build_tts_backend(plugin_config, logger)
        semaphore = asyncio.Semaphore(max_parallel)

        # 把 style / language 透传给 TTS provider：通过 TTSRequest.markers 字段。
        # tts_voice_plugin-neo 的 TTSVoiceProvider 会读 markers["style"] 和
        # markers["language"]，分别决定语音风格（参考音色）和文本语言代码。
        # markers 是按段构造的，所以需要为每个 segment 合并：seg.markers 自身
        # （行内标记给的）+ 顶层 style/language。seg.markers 优先（行内标记
        # 应该能覆盖默认）。
        def _build_markers_for_seg(seg: Any) -> dict[str, Any]:
            """合并 segment 自身 markers + 本次 action 的 style / language。"""

            base = dict(getattr(seg, "markers", None) or {})
            base.setdefault("style", style)
            base.setdefault("language", language)
            return base

        async def synthesize_one(seg: Any, idx: int) -> tuple[int, Any]:
            try:
                async with semaphore:
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

        # 关键性能优化：用 asyncio.create_task 把合成任务**立即挂到事件循环**，
        # 而不是等到 ``consume_in_order`` 里 ``asyncio.as_completed`` 才包装。
        # 这样多个 say_and_perform action 同时被调度时，每个 action 的合成都
        # 在拿 VTS performer 锁之前就开始跑——前一个 action 在播放音频时，
        # 后一个 action 的 TTS 已经在 GSV 服务器上推理了，避免"播放完才合成"
        # 的串行浪费。配合下方 consume_in_order 用 wait_for 顺序消费即可。
        tasks: list[asyncio.Task[tuple[int, Any]]] = [
            asyncio.create_task(synthesize_one(seg, i))
            for i, seg in enumerate(segments)
        ]

        # 取顶层 emotion 主类型（happy / sad / ...），用作行内 motion 切换时
        # expression 命中的兜底键（intent 不命中 expression_map 时退回 emotion）。
        emotion_main = (emotion or "neutral").split(":", 1)[0].strip().lower() or "neutral"

        async def consume_in_order() -> int:
            """按 idx 顺序消费合成结果并回调播放。

            行内 motion 标记处理：
            每段播放前根据 ``cur_seg.motion`` 切 intent + expression。
            - segment.motion 有值：用它切（行内 [motion:X] 标记）。
            - segment.motion 为 None：切回顶层 intent（标记块外 / 标记结束后）。
            没有 performer（VTS 未连）时整段静默跳过这一切。

            注意：tasks 已经是 ``asyncio.create_task`` 提前挂上事件循环的对象，
            合成在拿 VTS 锁之前已经开始跑了。这里只需按 idx 顺序 await 即可，
            前段播放期间后段会持续在 GSV 上推理。
            """

            next_to_play = 0
            played = 0

            while next_to_play < len(tasks):
                cur_idx = next_to_play
                _, cur_art = await tasks[cur_idx]
                cur_seg = segments[cur_idx]

                if isinstance(cur_art, Failure) or (
                    hasattr(cur_art, "metadata")
                    and cast(dict, cur_art.metadata).get("error")
                ):
                    logger.error(f"跳过失败段 {cur_idx}: {cur_seg.text[:30]}...")
                elif not cur_art.audio:
                    logger.error(f"跳过空音频段 {cur_idx}: {cur_seg.text[:30]}...")
                else:
                    if cur_seg.wait_before >= 0.1:
                        await asyncio.sleep(cur_seg.wait_before)

                    # 关键时序：首段音频"已经合成完准备播放"时，才真正
                    # 触发 VTS 动作链（speaking + performing + hotkey + 顶层
                    # expression）。这样推理过程中 VTB 维持 IDLE，避免出现
                    # 「动作先动起来，过几秒才发声」的诡异画面。
                    # start_speech_playback 是幂等的，后续段调用是 no-op。
                    if performer is not None:
                        await performer.start_speech_playback()
                        # 行内 motion 切换：每段播放前根据 segment.motion 切 intent。
                        # segment.motion 为 None 时切回顶层 intent，自然实现"标记块
                        # 结束就归位"。
                        seg_intent = cur_seg.motion or intent
                        await performer.switch_segment_intent(
                            seg_intent,
                            emotion_main_for_expression=emotion_main,
                        )
                        await performer.play(cur_art.audio)
                    elif audio_player is not None:
                        await audio_player.play_audio(cur_art.audio)
                    played += 1
                    seg_intent_log = cur_seg.motion or intent
                    logger.info(
                        f"已播放段 {cur_idx}: {cur_seg.text[:20]}... "
                        f"emotion={emotion} intent={seg_intent_log}"
                    )

                next_to_play += 1
            return played

        # 把整次说话作为一个完整周期：进入时设一次 emotion / intent / speaking=True，
        # 中间所有段共享，退出时再统一收尾。这样段间切换不会让 SpeechAnimator
        # 进入 emotion 缓退状态机。
        # 整段播放期间 generator 会 await 几十秒不 yield，需要主动喂 watchdog
        # 避免触发 stream_warning_threshold / stream_restart_threshold。
        async with feed_watchdog_during(self.chat_stream.stream_id):
            if performer is not None:
                async with performer.speaking_session(emotion=emotion, intent=intent):
                    success_count = await consume_in_order()
            else:
                success_count = await consume_in_order()

        return True, f"已播放 {success_count}/{len(segments)} 段"


__all__ = ["SayAndPerformAction"]
