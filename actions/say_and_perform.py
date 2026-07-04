"""anima_chatter 的 VTB 虚拟形象表演动作。

`SayAndPerformAction` 在非 ``local_asr`` 平台（即 vtb 模式）激活：

1. 立即把文本发送到当前聊天流（让用户先看到字）。
2. 解析 markers 并按句切分（流水线合成，保持顺序播放）。
3. 通过 :class:`HttpTTSBackend` 合成音频（**不调用 emit**——vtb 不发 voice 给 adapter）。
4. 把音频交给 :class:`VTSPerformer.perform`：本地 AudioPlayer 输出到 VB-Cable，
   同时让 VTS SpeechAnimator 进入说话状态（嘴型/动作同步）。

如果 VTS 未启用或未连接，performer 会降级为"仅本地播放"，依然会进 VB-Cable，
让虚拟形象的嘴型还能跟上（通过 VTS 自带的麦克风口型驱动）。

**vtb_live 流水线模式**（仅在 ``mode == "vtb_live"`` + ``[pipelining].enabled``
同时满足时启用）：

- 合成完成后通过 :mod:`plugins.anima_chatter.pipeline_state.reserve` 申请播放
  时段，派发到后台 task 执行播放，Action 立即返回 Success。
- LLM 在 ``plugin.sub_agent`` / ``_build_user_prompt`` 入口被流水线门阻塞，
  累积时长达到 ``trigger_percent`` 时放行——音频还在播时下一轮 LLM 已经在
  推理新回复。
- 累积时长 < ``min_duration_seconds`` 时不启用流水线，走原阻塞模式。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Annotated, Any, AsyncGenerator, cast

from src.app.plugin_system.api import send_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import Failure
from src.core.components.base.action import BaseAction

from .. import pipeline_state
from .._internal_compat import create_background_task
from ..audio import read_duration_from_bytes
from ..config import AnimaChatterConfig
from ..heartbeat import feed_watchdog_during
from ..markers import parse_speech_segments, strip_markers
from ..modes import resolve_mode
from ..prompts.scenes import (
    EMOTION_SCHEMA_DESC,
    INTENT_SCHEMA_DESC,
)
from ..tts import TTSRequest, _retry_empty_audio, build_tts_backend
from ._tts_schema import inject_tts_params

if TYPE_CHECKING:
    pass


logger = get_logger("anima_chatter.action.say_and_perform")


class SayAndPerformAction(BaseAction):
    """通过虚拟形象（VTube Studio）说一段话并设定情绪与意图（vtb 模式）。"""

    action_name = "say_and_perform"
    associated_types = ["voice", "text"]
    action_description = (
        "通过 VTube Studio 虚拟形象说一段话。会同时把文本发到当前聊天，"
        "用 TTS 朗读，并驱动虚拟形象的嘴型 + 表情 + 头部姿态。"
        "支持 [wait:n] 控制下一段播放前等待 n 秒（仅需要长停顿时使用）。"
        "说完等待用户继续说话时，请另外调用 pass_and_wait。"
    )
    chatter_allow = ["anima_chatter"]
    primary_action = True

    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        """动态注入 TTS Provider capabilities 定义的 TTS 参数。"""

        return inject_tts_params(super().to_schema())

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
                "【跨参数拆分准则】：\n"
                "- 同一次调用所有段落强制共享同一个 emotion / intent / language / style，无法分段切换。\n"
                "- **跨情绪**：必须拆分为多次 Action 调用。\n"
                "- **跨语言**：当出现不同语种的成句表达时，必须拆分为多次 Action 调用。避免在一次调用中通过 auto 模式混合多语种，以维持音色稳定。\n"
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
        **tts_params: Any,
    ) -> AsyncGenerator[tuple[bool, str] | None, None]:
        """发文本 → TTS 合成 → 本地播放 + VTS 表演。

        当 ``mode == "vtb_live"`` 且 ``[pipelining].enabled`` 时走流水线模式：
        合成完成后立即把播放任务派发到后台，Action 提前返回 Success；其他场景
        保留原阻塞行为（合成 + 播放都在 Action 内完成）。

        本方法是**异步生成器**：发文本 + TTS 合成等准备工作在首个 ``yield None``
        之前完成（多个 action 的合成会并发起跑），真正占用播放资源（``reserve`` /
        拿 performer 锁）的关键段放在 ``yield None`` 之后——调度器会按 LLM 的
        tool call 顺序放行 ``_READY`` 状态的 action，从而保证“先说话后唱歌”这类
        顺序依赖与调用顺序一致。
        """

        # 1) 规范化输入：兼容模型偶尔传 str（虽然 schema 是 list[str]）。
        if isinstance(content, str):
            content_list: list[str] = [content]
        else:
            content_list = [str(item) for item in (content or []) if str(item).strip()]

        if not content_list:
            yield False, "content 不能为空"
            return

        plugin_config = getattr(self.plugin, "config", None)
        if not isinstance(plugin_config, AnimaChatterConfig):
            logger.error("插件配置缺失，无法执行 vtb 表演")
            yield False, "插件配置缺失"
            return

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
            yield True, "无可朗读内容"
            return

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

        # 4) 拿 performer / audio_player
        # 通过 get_active_performer 取当前激活的表演器（VTS），
        # 接口已对等，无需在此区分底层类型。
        get_active = getattr(self.plugin, "get_active_performer", None)
        performer: Any = get_active() if callable(get_active) else None
        audio_player = getattr(self.plugin, "audio_player", None)

        if performer is None and audio_player is None:
            logger.warning(
                "VTSPerformer 与 AudioPlayer 均未初始化（可能 audio 配置缺失），"
                "已发送文本但无法播放语音。"
            )
            yield True, "已发送文本（无音频输出）"
            return

        # 5) 流水线门判定：仅 vtb_live 模式且配置启用时走流水线
        mode = resolve_mode(self.chat_stream)
        pipeline_enabled = (
            mode == "vtb_live"
            and bool(plugin_config.pipelining.enabled)
        )

        # 6) 流水线合成（限并发） + 顺序播放
        backend = build_tts_backend(plugin_config, logger)
        semaphore = asyncio.Semaphore(max_parallel)

        # 把 style / language 透传给 TTS provider：通过 TTSRequest.markers 字段。
        # tts_voice_plugin-neo 的 TTSVoiceProvider 会读 markers["style"] 和
        # markers["language"]，分别决定语音风格（参考音色）和文本语言代码。
        # markers 是按段构造的，所以需要为每个 segment 合并：seg.markers 自身
        # （行内标记给的）+ 顶层所有 TTS 参数。seg.markers 优先（行内标记
        # 应该能覆盖默认）。
        def _build_markers_for_seg(seg: Any) -> dict[str, Any]:
            """合并 segment 自身 markers + 本次 action 的所有 TTS 参数。"""

            base = dict(getattr(seg, "markers", None) or {})
            # 合并所有 tts_params 到 markers，segment 自带的优先级更高
            for key, value in tts_params.items():
                base.setdefault(key, value)
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

        # ── 流水线模式：估算占位 reserve → 派发后台流式播放 → 立即返回 ──
        # 关键改动：不再 await gather 等全部段合成完，改用字数估算总时长立即
        # reserve 占位，后台任务按段流式播放（第一段合成完就播，后续段边播边
        # 合成）。这样长回复的首句延迟从"等全部段合成完"降到"等第一段合成完"。
        if pipeline_enabled:
            # 顺序门：在 reserve / 派发前 yield None，调度器会按 tool call 顺序
            # 放行，保证 say_and_perform 先于后续 sing_song 完成 reserve。
            yield None
            yield await self._execute_pipelined(
                segments=segments,
                tasks=tasks,
                performer=performer,
                audio_player=audio_player,
                emotion=emotion,
                intent=intent,
                emotion_main=emotion_main,
            )
            return

        # ── 阻塞模式（vtb / 通话中临时接管 / 流水线禁用）：原行为，流式播放 ──
        # 把整次说话作为一个完整周期：进入时设一次 emotion / intent / speaking=True，
        # 中间所有段共享，退出时再统一收尾。这样段间切换不会让 SpeechAnimator
        # 进入 emotion 缓退状态机。
        # 整段播放期间 generator 会 await 几十秒不 yield，需要主动喂 watchdog
        # 避免触发 stream_warning_threshold / stream_restart_threshold。
        # 顺序门：阻塞播放前 yield None，保证拿 performer 锁的顺序与调用顺序一致。
        yield None
        async with feed_watchdog_during(self.chat_stream.stream_id):
            if performer is not None:
                async with performer.speaking_session(emotion=emotion, intent=intent):
                    success_count = await self._consume_tasks_in_order(
                        tasks=tasks,
                        segments=segments,
                        performer=performer,
                        audio_player=audio_player,
                        emotion=emotion,
                        intent=intent,
                        emotion_main=emotion_main,
                    )
            else:
                success_count = await self._consume_tasks_in_order(
                    tasks=tasks,
                    segments=segments,
                    performer=None,
                    audio_player=audio_player,
                    emotion=emotion,
                    intent=intent,
                    emotion_main=emotion_main,
                )

        yield True, f"已播放 {success_count}/{len(segments)} 段"
        return

    # ── vtb_live 流水线分支 ────────────────────────────────────

    async def _consume_tasks_in_order(
        self,
        *,
        tasks: list[asyncio.Task[tuple[int, Any]]],
        segments: list[Any],
        performer: Any,
        audio_player: Any,
        emotion: str,
        intent: str,
        emotion_main: str,
    ) -> int:
        """按 idx 顺序消费合成结果并播放（流式：第一段好就播，后续边播边合成）。

        行内 motion 标记处理：每段播放前根据 ``cur_seg.motion`` 切 intent +
        expression；为 None 时切回顶层 intent。没有 performer（VTS 未连）时退回
        纯 audio_player 播放。

        tasks 是 ``asyncio.create_task`` 提前挂上事件循环的对象，合成在拿播放锁
        之前已经开始跑——这里只按 idx 顺序 await，前段播放期间后段持续在 TTS
        服务器上推理。阻塞模式与流水线后台模式共用本方法，避免逻辑分叉。
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

                # 关键时序：首段音频"已经合成完准备播放"时，才真正触发 VTS 动作
                # 链。start_speech_playback 幂等，后续段调用是 no-op。
                if performer is not None:
                    await performer.start_speech_playback()
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

    def _collect_valid_pairs(
        self,
        results: list[tuple[int, Any]],
        segments: list[Any],
    ) -> list[tuple[Any, Any, float]]:
        """从合成结果收集 ``(segment, artifact, seg_total_duration)`` 列表。

        跳过失败 / 空音频段；时长用 :func:`read_duration_from_bytes` 精确读取，
        读取失败时按字数估算。``seg_total`` 含 ``wait_before``。仅短句退化路径用。
        """

        valid_pairs: list[tuple[Any, Any, float]] = []
        for idx, art in results:
            seg = segments[idx]
            if isinstance(art, Failure) or (
                hasattr(art, "metadata")
                and cast(dict, art.metadata).get("error")
            ):
                logger.error(f"流水线模式跳过失败段 {idx}: {seg.text[:30]}...")
                continue
            if not getattr(art, "audio", None):
                logger.error(f"流水线模式跳过空音频段 {idx}: {seg.text[:30]}...")
                continue
            duration = read_duration_from_bytes(art.audio) or 0.0
            if duration <= 0:
                from ..audio import estimate_tts_duration_by_chars

                duration = estimate_tts_duration_by_chars(seg.text)
            seg_total = duration + max(0.0, seg.wait_before)
            valid_pairs.append((seg, art, seg_total))
        return valid_pairs

    async def _execute_pipelined(
        self,
        *,
        segments: list[Any],
        tasks: list[asyncio.Task[tuple[int, Any]]],
        performer: Any,
        audio_player: Any,
        emotion: str,
        intent: str,
        emotion_main: str,
    ) -> tuple[bool, str]:
        """vtb_live 流水线分支：估算占位 reserve → 派发后台流式播放 → 立即返回。

        与旧实现的关键区别：不再等全部段合成完再 reserve，而是用字数估算总时长
        立即 reserve 占位（保证 say 在 sing 前占好时间轴），后台任务按段流式播放。
        估算占位只影响 gate 的软触发时刻；物理播放有 audio_player 锁 + performer
        锁双重串行保证，多段 / 多 action 音频绝不会重叠，只会排队。

        合成已在 ``execute`` 的准备阶段并发起跑，本方法在顺序门放行后才被调用——
        只负责估算 + reserve + 派发后台，确保 reserve 顺序与 LLM 的 tool call
        顺序一致。
        """

        plugin_config = getattr(self.plugin, "config", None)
        if not isinstance(plugin_config, AnimaChatterConfig):
            return False, "插件配置缺失"

        # 1) 估算总时长（不等合成，按字数粗估占位）
        from ..audio import estimate_tts_duration_by_chars

        estimated_total = 0.0
        for seg in segments:
            estimated_total += estimate_tts_duration_by_chars(seg.text) + max(
                0.0, seg.wait_before
            )

        # 2) 估算总时长低于 min_duration → 退化阻塞模式（短句合成快，同步播完即可）
        min_duration = float(plugin_config.pipelining.min_duration_seconds)
        if estimated_total < min_duration:
            results: list[tuple[int, Any]] = await asyncio.gather(*tasks)
            results.sort(key=lambda pair: pair[0])
            valid_pairs = self._collect_valid_pairs(results, segments)
            if not valid_pairs:
                logger.warning("流水线退化：所有段合成失败，已发送文本但无音频输出")
                return True, "已发送文本（合成全部失败）"
            logger.info(
                f"⚠️ say_and_perform 流水线退化：估算总时长 "
                f"{estimated_total:.2f}s < min_duration {min_duration:.2f}s，本次走原阻塞模式"
            )
            return await self._play_blocking(
                valid_pairs=valid_pairs,
                performer=performer,
                audio_player=audio_player,
                emotion=emotion,
                intent=intent,
                emotion_main=emotion_main,
            )

        # 3) 估算占位 reserve（瞬间完成，保证 say 在 sing 前占好时间轴）
        logger.info(
            f"🎬 say_and_perform 进入流水线：{len(segments)} 段，估算总时长 "
            f"{estimated_total:.2f}s，开始 reserve（估算占位）"
        )
        start_at, finish_at = await pipeline_state.reserve(
            self.chat_stream.stream_id, estimated_total
        )

        # 4) 派发后台流式播放 task —— 不等待，按段顺序边合成边播
        play_task = create_background_task(
            self._background_play(
                start_at=start_at,
                finish_at=finish_at,
                tasks=tasks,
                segments=segments,
                performer=performer,
                audio_player=audio_player,
                emotion=emotion,
                intent=intent,
                emotion_main=emotion_main,
                stream_id=self.chat_stream.stream_id,
            ),
            name=f"anima_chatter.background_play.{self.chat_stream.stream_id[:8]}",
        )
        # play_task 是 fire-and-forget；create_background_task 走的是框架
        # task_manager，会在 stream 关闭时统一清理，无需在这里 await。
        _ = play_task

        logger.info(
            f"✈️ say_and_perform 已派发后台流式播放、Action 立即返回 "
            f"({len(segments)} 段, 估算 {estimated_total:.2f}s)"
        )
        return True, (
            f"已派发 {len(segments)} 段到后台流式播放队列 "
            f"(估算总时长 {estimated_total:.2f}s, 流水线 enabled)"
        )

    async def _play_blocking(
        self,
        *,
        valid_pairs: list[tuple[Any, Any, float]],
        performer: Any,
        audio_player: Any,
        emotion: str,
        intent: str,
        emotion_main: str,
    ) -> tuple[bool, str]:
        """阻塞播放共享逻辑（也供流水线"短句退化"路径调用）。"""

        async with feed_watchdog_during(self.chat_stream.stream_id):
            if performer is not None:
                async with performer.speaking_session(emotion=emotion, intent=intent):
                    played = await self._play_pairs_with_performer(
                        valid_pairs=valid_pairs,
                        performer=performer,
                        intent=intent,
                        emotion_main=emotion_main,
                    )
            else:
                played = 0
                for seg, art, _dur in valid_pairs:
                    if seg.wait_before >= 0.1:
                        await asyncio.sleep(seg.wait_before)
                    if audio_player is not None:
                        await audio_player.play_audio(art.audio)
                        played += 1

        return True, f"已播放 {played}/{len(valid_pairs)} 段"

    async def _background_play(
        self,
        *,
        start_at: float,
        finish_at: float,
        tasks: list[asyncio.Task[tuple[int, Any]]],
        segments: list[Any],
        performer: Any,
        audio_player: Any,
        emotion: str,
        intent: str,
        emotion_main: str,
        stream_id: str,
    ) -> None:
        """后台流式播放任务：等到 start_at → 按段顺序 await 合成结果并播放。

        与旧的"等全部段合成完才播"不同，本任务边等边播——第一段合成完立即播放，
        后续段在 TTS 服务器上继续推理，首句延迟从"等全部段"降到"等第一段"。

        本协程不属于 chatter generator——stream loop 已经 yield 进入下一轮，
        watchdog 不会管这条 task，所以**不需要喂狗**。异常仅记日志：流水线模式下
        Action 已经返回 Success，错误反馈给 LLM 的成本远高于直接吞掉 + 日志。
        """

        try:
            # 等到 reserve 的 start_at 时刻
            now = time.monotonic()
            wait = start_at - now
            if wait > 0:
                logger.info(
                    f"⏳ [bg_play {stream_id[:8]}] 排队中：等待 {wait:.2f}s 到 start_at "
                    f"(finish_at={finish_at - now:.2f}s)"
                )
                await asyncio.sleep(wait)
                logger.info(
                    f"🔊 [bg_play {stream_id[:8]}] 开始流式播放 ({len(tasks)} 段)"
                )
            else:
                logger.info(
                    f"🔊 [bg_play {stream_id[:8]}] 立即开始流式播放 "
                    f"({len(tasks)} 段)"
                )

            # 流式串行播放：按段顺序 await，第一段好就播，后续段边播边合成
            if performer is not None:
                async with performer.speaking_session(emotion=emotion, intent=intent):
                    played = await self._consume_tasks_in_order(
                        tasks=tasks,
                        segments=segments,
                        performer=performer,
                        audio_player=audio_player,
                        emotion=emotion,
                        intent=intent,
                        emotion_main=emotion_main,
                    )
            else:
                played = await self._consume_tasks_in_order(
                    tasks=tasks,
                    segments=segments,
                    performer=None,
                    audio_player=audio_player,
                    emotion=emotion,
                    intent=intent,
                    emotion_main=emotion_main,
                )

            logger.info(
                f"✅ [bg_play {stream_id[:8]}] 后台流式播放完成 "
                f"({played} 段播完)"
            )
        except asyncio.CancelledError:
            logger.info(f"[bg_play {stream_id[:8]}] 后台播放被取消")
            raise
        except Exception as exc:
            logger.error(
                f"[bg_play {stream_id[:8]}] 后台播放异常: {exc}",
                exc_info=True,
            )

    async def _play_pairs_with_performer(
        self,
        *,
        valid_pairs: list[tuple[Any, Any, float]],
        performer: Any,
        intent: str,
        emotion_main: str,
    ) -> int:
        """共享播放循环：在 ``speaking_session`` 内按 segment 顺序播。

        阻塞模式 / 流水线后台模式都用这条共享路径，避免逻辑分叉。
        """

        played = 0
        for idx, (seg, art, _dur) in enumerate(valid_pairs):
            if seg.wait_before >= 0.1:
                await asyncio.sleep(seg.wait_before)

            await performer.start_speech_playback()
            seg_intent = seg.motion or intent
            await performer.switch_segment_intent(
                seg_intent,
                emotion_main_for_expression=emotion_main,
            )
            await performer.play(art.audio)
            played += 1
            logger.info(
                f"已播放段 {idx}: {seg.text[:20]}... "
                f"intent={seg_intent}"
            )
        return played


__all__ = ["SayAndPerformAction"]
