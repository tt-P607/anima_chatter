"""voice_chatter 的 VTB 虚拟形象表演动作。

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
import re
from typing import Annotated, Any, cast

from src.app.plugin_system.api import send_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import Failure
from src.core.components.base.action import BaseAction

from ..config import SherpaOnnxVoiceChatterConfig
from ..markers import parse_speech_segments
from ..sub_agent import mark_reply_success
from ..tts import TTSRequest, _retry_empty_audio, build_tts_backend


# 兜底正则：当 parse_speech_segments 解析不出任何片段时，
# 直接从原始 content 把 [wait:n] / [emotion:xxx] / [/emotion] 标记剥掉。
_FALLBACK_WAIT_RE = re.compile(r"\[wait\s*:\s*[0-9.]+\]", re.IGNORECASE)
_FALLBACK_EMOTION_RE = re.compile(
    r"\[/?emotion(?:\s*:\s*[a-zA-Z0-9_\-]+)?\]", re.IGNORECASE
)


logger = get_logger("voice_chatter.action.say_and_perform")


class SayAndPerformAction(BaseAction):
    """通过虚拟形象（VTube Studio）说一段话并设定情绪与意图（vtb 模式）。"""

    action_name = "say_and_perform"
    action_description = (
        "通过 VTube Studio 虚拟形象说一段话。会同时把文本发到当前聊天，"
        "用 TTS 朗读，并驱动虚拟形象的嘴型 + 表情 + 头部姿态。"
        "支持 [wait:n] 控制下一段播放前等待 n 秒（仅需要长停顿时使用）。"
        "说完等待用户继续说话时，请另外调用 pass_and_wait。"
    )
    chatter_allow = ["voice_chatter"]
    primary_action = True

    async def go_activate(self) -> bool:
        """仅在非 ``local_asr`` 平台激活（即被 ``/vtb on`` 接管的 stream）。"""

        return self.chat_stream.platform != "local_asr"

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
                "- 同一次调用所有段落共享同一个 emotion / intent，跨情绪时要拆成多次调用"
            ),
        ],
        emotion: Annotated[
            str,
            "情绪类型:强度，格式如 'happy:2' / 'sad:1' / 'angry:3' / 'neutral:1' / 'surprised:2'。"
            "类型决定嘴角与眉头的基准；强度 1~3 决定表现幅度，3 级会带身体晃动。"
            "留空或 neutral:1 表示平静。",
        ] = "neutral:1",
        intent: Annotated[
            str,
            "动作意图，决定头部基准姿态：IDLE（静止）、NARRATING（叙述）、"
            "THINKING（思考，头微抬+眼神上飘）、CONFUSED（困惑，歪头）、"
            "EXCITED（兴奋，前倾抬头）、SURPRISED（惊讶）。"
            "选择最匹配本句话语气的一个；不确定时填 NARRATING。",
        ] = "NARRATING",
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
        if not isinstance(plugin_config, SherpaOnnxVoiceChatterConfig):
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
                fallback_text = _FALLBACK_WAIT_RE.sub("", text)
                fallback_text = _FALLBACK_EMOTION_RE.sub("", fallback_text).strip()
                if fallback_text:
                    from ..markers import SpeechSegment

                    segments.append(SpeechSegment(text=fallback_text))

        if not segments:
            logger.info("VTB 解析后无可播放片段，跳过本次调用。")
            return True, "无可朗读内容"

        # 3) 按句发送干净文本到聊天流（标记已剥掉，群里看到的是干净文本）。
        for seg in segments:
            clean = (seg.text or "").strip()
            if not clean:
                continue
            ok = await send_api.send_text(
                content=clean,
                stream_id=self.chat_stream.stream_id,
                platform=self.chat_stream.platform,
            )
            if not ok:
                logger.warning(
                    f"VTB 模式发送分段失败 stream_id={self.chat_stream.stream_id}: {clean[:30]}..."
                )

        # 文本发送成功 → 标记下一 tick 的概率门加成（沿用 dfc 的"刚回复就再回复"心理）。
        mark_reply_success(self.chat_stream)

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

        async def synthesize_one(seg: Any, idx: int) -> tuple[int, Any]:
            try:
                async with semaphore:
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

        tasks = [synthesize_one(seg, i) for i, seg in enumerate(segments)]

        async def consume_in_order() -> int:
            """按 idx 顺序消费合成结果并回调播放。"""

            next_to_play = 0
            completed: dict[int, Any] = {}
            played = 0

            for fut in asyncio.as_completed(tasks):
                idx, artifact = await fut
                completed[idx] = artifact

                while next_to_play in completed:
                    cur_idx = next_to_play
                    cur_art = completed.pop(cur_idx)
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

                        # 在 speaking_session 内只调用 play()，避免段间切换说话状态
                        # 触发 SpeechAnimator 的 emotion 回归，导致姿态被打断。
                        if performer is not None:
                            await performer.play(cur_art.audio)
                        elif audio_player is not None:
                            await audio_player.play_audio(cur_art.audio)
                        played += 1
                        logger.info(
                            f"已播放段 {cur_idx}: {cur_seg.text[:20]}... "
                            f"emotion={emotion} intent={intent}"
                        )

                    next_to_play += 1
            return played

        # 把整次说话作为一个完整周期：进入时设一次 emotion / intent / speaking=True，
        # 中间所有段共享，退出时再统一收尾。这样段间切换不会让 SpeechAnimator
        # 进入 emotion 缓退状态机。
        if performer is not None:
            async with performer.speaking_session(emotion=emotion, intent=intent):
                success_count = await consume_in_order()
        else:
            success_count = await consume_in_order()

        return True, f"已播放 {success_count}/{len(segments)} 段"


__all__ = ["SayAndPerformAction"]
