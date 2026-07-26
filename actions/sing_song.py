"""vtb / vtb_live 模式的唱歌动作。

``sing_song`` 让模型直接播放歌库里已翻唱好的本地音频，通过 AudioPlayer 推到
VB-Cable 让直播间观众听到。

关键设计：

- **不走消息发送链路**：直播平台不接受第三方 bot 发语音消息，音频直接进
  AudioPlayer。
- **发文本提示**：默认发一条 ``"♪ 我来唱一首《XXX》"`` 到聊天流（群里能看到
  字），直播间观众只能听到声音。
- **动态歌单**：:meth:`SingSongAction.to_schema` 每次序列化都读取当前歌库，
  把歌名与时长注入参数描述，模型看到的永远是真实库存，不会虚构曲名。
- **动作时间轴**：模型可以给出"第几秒切到什么动作"的剧本，播放期间由后台任务
  按时触发，避免整首歌一个姿势。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Annotated, Any

from src.app.plugin_system.api import send_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

from .._internal_compat import get_anima_chatter_plugin
from ..audio import read_duration_from_path
from ..constants import normalize_intent, split_emotion
from ..modes import resolve_mode
from ..protocol import require_plugin
from ..runtime import call_state, sung_history
from ..song_library import SongInfo, SongLibrary, format_duration
from ..speech import (
    dispatch_track_pipelined,
    play_track_blocking,
    should_use_pipeline,
)

if TYPE_CHECKING:
    from ..vts import VTSPerformer


logger = get_logger("anima_chatter.action.sing_song")


# 唱歌默认走轻表现——比通话 / 普通说话更收敛。
_SING_DEFAULT_EMOTION_TYPE = "happy"
_SING_DEFAULT_EMOTION_LEVEL = 1

# 歌曲时长读取失败时的兜底值（秒），仅影响流水线 reserve 占位。
_FALLBACK_SONG_DURATION = 60.0

# 时间轴首个 cue 的"视为开头"阈值（秒）。
_TIMELINE_HEAD_THRESHOLD = 0.5

_RANDOM_KEYWORDS = frozenset({"random", "随机"})

_SONG_KEYWORD_BASE_DESC = (
    "要唱的歌曲名称，必须从下方【可用歌单】里选，不能虚构。\n"
    "**强烈建议直接复制下方歌单里的完整歌名**（含歌手 / 出处前缀），匹配最稳。\n"
    "也支持只写核心歌名做关键词匹配——会按 精确 / 归一化 / 子串包含 / 模糊 的"
    "顺序去命中。但若核心歌名太短或多首撞名，仍可能匹配偏差，所以能写全名就写全名。\n"
    "留空或填 ``random`` / ``随机`` 则随机选一首。\n"
    "歌单外的歌名会直接失败（无随机回退）——遇到点歌不在歌单里时，"
    "改用 say_and_perform 按人设回应，不要硬调本动作。调用前先核对下方歌单。"
)

_MOTION_TIMELINE_DESC = (
    "可选的动作时间轴：让虚拟形象在歌曲不同段落切换动作 / 情绪，避免整首一个姿势。"
    "留空（``[]``）则全程默认 NARRATING + happy:1。\n"
    "\n"
    "格式：``[{\"at\": 秒数, \"intent\": \"动作名\", \"emotion\": \"类型:强度\"}, ...]``\n"
    "- ``at``：从歌曲开始算起的秒数（0 = 开头，75 = 1 分 15 秒）\n"
    "- ``intent``：动作意图（NARRATING / EXCITED / PROUD_LIFT / SHY_DOWN / "
    "PLAYFUL_TILT 等，完整列表见 say_and_perform）\n"
    "- ``emotion``：情绪:强度，如 ``happy:2`` ``sad:1`` ``neutral:1``\n"
    "\n"
    "动作保持时长 = 相邻两个 cue 的 ``at`` 时间差。段数自定、别太频繁；跟着歌曲"
    "段落（前奏 / 主歌 / 副歌 / 尾奏）或情绪起伏切换最自然。\n"
    "\n"
    "示例：``[{\"at\": 0, \"intent\": \"EXCITED\", \"emotion\": \"happy:2\"}, "
    "{\"at\": 8, \"intent\": \"NARRATING\", \"emotion\": \"happy:1\"}, "
    "{\"at\": 75, \"intent\": \"SHY_DOWN\", \"emotion\": \"happy:1\"}]``"
)

_PRE_SONG_DELAY_DESC = (
    "开场白发完后、歌曲开始前的停顿秒数（默认 4 秒）。\n"
    "给观众听完文字开场、调整心情的缓冲。想更隆重传 ``5`` ~ ``8``，"
    "想立刻开唱传 ``0`` ~ ``2``。\n"
    "停顿期间虚拟形象会显示 motion_timeline 第一个 cue 的动作，"
    "可用来做『准备开口』的姿态。"
)


def _normalize_sing_emotion(emotion: str | None) -> tuple[str, int]:
    """解析唱歌场景的 emotion；缺省 / 非法时降级为 ``("happy", 1)``。

    与 :func:`constants.split_emotion` 共享解析规则，只是兜底默认换成"唱歌时
    表现轻一点"。

    Args:
        emotion: ``"类型:强度"`` 字符串。

    Returns:
        ``(主类型, 强度)``。
    """

    return split_emotion(
        emotion,
        default_type=_SING_DEFAULT_EMOTION_TYPE,
        default_level=_SING_DEFAULT_EMOTION_LEVEL,
    )


class MotionCue:
    """时间轴上的单个动作切换点。

    Attributes:
        at_seconds: 从歌曲开始算起的触发时刻（秒）。
        intent: 切换到的动作意图。
        emotion_main: 情绪主类型，用于 expression 匹配兜底。
    """

    __slots__ = ("at_seconds", "emotion_main", "intent")

    def __init__(self, at_seconds: float, intent: str, emotion_main: str) -> None:
        """初始化动作切换点。

        Args:
            at_seconds: 触发时刻（秒）。
            intent: 动作意图。
            emotion_main: 情绪主类型。
        """

        self.at_seconds = at_seconds
        self.intent = intent
        self.emotion_main = emotion_main


def parse_motion_timeline(timeline: list[dict[str, Any]] | None) -> list[MotionCue]:
    """把模型给的时间轴参数解析为有序的动作切换点。

    跳过 ``at`` 不是数字或为负数的项；非法 intent / emotion 按各自规则降级。

    Args:
        timeline: 模型传入的原始时间轴。

    Returns:
        按时刻升序排列的切换点列表；输入为空时返回空列表。
    """

    if not timeline:
        return []

    cues: list[MotionCue] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        try:
            at_seconds = float(item.get("at"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if at_seconds < 0:
            continue
        emotion_main, _ = _normalize_sing_emotion(item.get("emotion"))
        cues.append(
            MotionCue(
                at_seconds=at_seconds,
                intent=normalize_intent(item.get("intent")),
                emotion_main=emotion_main,
            )
        )

    cues.sort(key=lambda cue: cue.at_seconds)
    return cues


class MotionTimelineRunner:
    """按时间轴在播放期间触发 VTS 动作切换。

    实现 [`speech/playback.py`](../speech/playback.py:1) 的 ``TimelineRunner``
    协议，由播放层在 ``speaking_session`` 内以后台任务启动。
    """

    def __init__(self, cues: list[MotionCue]) -> None:
        """初始化时间轴执行器。

        Args:
            cues: 已排序的动作切换点列表。
        """

        self._cues = cues

    async def run(self, performer: "VTSPerformer", stop_event: asyncio.Event) -> None:
        """按时间轴依次触发动作切换。

        每个 cue 在"启动时刻 + ``at_seconds``"执行一次 intent 切换。
        ``stop_event`` 触发后立即退出（播放结束 / 异常时由调用方触发）。

        Args:
            performer: VTS 表演器。
            stop_event: 停止信号。
        """

        started_at = time.monotonic()
        for cue in self._cues:
            delay = started_at + cue.at_seconds - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    return  # 歌已播完
                except asyncio.TimeoutError:
                    pass
            if stop_event.is_set():
                return

            await performer.switch_segment_intent(
                cue.intent, emotion_main_for_expression=cue.emotion_main
            )
            logger.info(
                f"时间轴 cue @ {cue.at_seconds:.1f}s → "
                f"intent={cue.intent} emotion_main={cue.emotion_main}"
            )


def _build_song_keyword_desc(library: SongLibrary | None) -> str:
    """构造 ``song_keyword`` 参数描述（含动态歌单 + 最近已唱）。

    Args:
        library: 当前歌库；未就绪时为 ``None``。

    Returns:
        完整的参数描述文本。
    """

    history_block = sung_history.format_recent_block()
    if library is None:
        return _SONG_KEYWORD_BASE_DESC + "\n\n【可用歌单】（暂未加载，请稍后重试）" + history_block

    songs = library.get_songs()
    if not songs:
        return (
            _SONG_KEYWORD_BASE_DESC
            + "\n\n【可用歌单】（歌库目前为空，请按你的人设口吻处理此情况）"
            + history_block
        )

    lines = "\n".join(
        f"- ``{song.name}``（时长 {format_duration(song.duration_seconds)}）"
        for song in songs
    )
    return (
        f"{_SONG_KEYWORD_BASE_DESC}\n\n【可用歌单】（共 {len(songs)} 首）：\n{lines}"
        f"{history_block}"
    )


class SingSongAction(BaseAction):
    """播放歌库里的本地翻唱音频。"""

    name = "sing_song"
    associated_types = ["voice", "text"]
    description = (
        "唱歌动作——播放歌库里已翻唱好的本地音频。"
        "直播间观众能听到声音，群聊里只显示一条文字提示。\n"
        "\n"
        "与 say_and_perform 的区别：say_and_perform 是用 TTS 朗读文本（不会唱歌）；"
        "本动作直接播放翻唱音频。想唱歌只能用本动作，不要用 TTS 朗读歌词冒充唱歌。\n"
        "\n"
        "使用约束：\n"
        "- 只能唱歌库里已有的歌（歌单见 song_keyword 参数）。\n"
        "- 歌库没有的歌：改用 say_and_perform 按人设回应（拒绝 / 改唱 / 引导点歌），"
        "不要硬传歌单外的曲名——会直接失败，无随机回退。\n"
        "- 唱歌期间无法说话，歌曲会完整播完才能继续 say_and_perform。"
    )
    chatter_allow = ["anima_chatter"]
    primary_action = False

    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        """覆写 schema 序列化，把当前真实歌单注入 ``song_keyword`` 描述。

        ``to_schema`` 是 classmethod，拿不到 ``self.plugin``，只能反查插件实例。

        Returns:
            注入动态歌单后的 action schema。
        """

        schema = super().to_schema()
        properties = (
            schema.get("function", {}).get("parameters", {}).get("properties", {})
        )
        if "song_keyword" not in properties:
            return schema

        plugin = get_anima_chatter_plugin()
        library = plugin.song_library if plugin is not None else None
        properties["song_keyword"]["description"] = _build_song_keyword_desc(library)
        return schema

    async def go_activate(self) -> bool:
        """唱歌动作的可见性。

        三个条件同时满足：非 local_asr 平台、当前 stream 不在通话中（语音通话
        场景放歌会打断纯耳朵交互体验）、歌库已就绪且非空。

        Returns:
            是否对模型可见。
        """

        if self.chat_stream.platform == "local_asr":
            return False
        if await call_state.is_call_active_for_stream(self.chat_stream.stream_id):
            return False

        library = require_plugin(self.plugin).song_library
        return library is not None and not library.is_empty

    async def execute(
        self,
        song_keyword: Annotated[str, "要唱的歌曲名称（动态歌单见 to_schema 注入）。"] = "",
        announce: Annotated[
            str,
            "唱歌前发到聊天的开场白文本（可选）。例：``那我给大家唱一首《XX》吧♪``。\n"
            "只作为文字发到聊天流，不会被 TTS 朗读。"
            "想让观众也听到口播开场，先调一次 say_and_perform 再调本动作。",
        ] = "",
        motion_timeline: Annotated[
            list[dict[str, Any]] | None, _MOTION_TIMELINE_DESC
        ] = None,
        pre_song_delay: Annotated[float, _PRE_SONG_DELAY_DESC] = 4.0,
    ) -> Any:
        """选歌 → 发开场白 → 播放。

        本方法是**异步生成器**：选歌、发开场白、读文件等准备工作在首个
        ``yield None`` 之前完成；真正占用播放资源的关键段放在其后，调度器按
        tool call 顺序放行，保证"先说话后唱歌"的顺序依赖。

        Args:
            song_keyword: 歌名关键词。
            announce: 开场白文本。
            motion_timeline: 动作时间轴。
            pre_song_delay: 开场停顿秒数。

        Yields:
            首次 yield ``None`` 作为顺序门；随后 yield ``(是否成功, 结果描述)``。
        """

        plugin = require_plugin(self.plugin)
        config = plugin.config
        if config is None:
            yield False, "插件配置缺失，无法播放歌曲"
            return

        library = plugin.song_library
        if library is None:
            yield False, "歌库未初始化（插件可能未正确加载）"
            return
        if library.is_empty:
            yield False, f"歌库为空——请把清唱文件放到 {library.songs_dir} 目录"
            return

        song_info = self._select_song(library, song_keyword)
        if song_info is None:
            yield False, self._build_not_found_message(library, song_keyword)
            return

        await self._announce(announce, song_info.name)

        audio_player = plugin.audio_player
        if audio_player is None:
            yield False, "音频播放器未初始化（可能 audio 配置缺失），无法播放"
            return

        try:
            audio_bytes = song_info.path.read_bytes()
        except OSError as exc:
            logger.error(f"读取歌曲文件失败 path={song_info.path}: {exc}", exc_info=True)
            yield False, f"读取歌曲文件失败: {exc}"
            return
        if not audio_bytes:
            yield False, "歌曲文件为空"
            return

        inst_bytes = self._read_inst(song_info)
        cues = parse_motion_timeline(motion_timeline)
        if cues:
            logger.info(f"时间轴已解析 {len(cues)} 个 cue")

        pre_delay = max(0.0, pre_song_delay)
        song_duration = read_duration_from_path(song_info.path)
        if song_duration is None or song_duration <= 0:
            logger.warning(
                f"无法读取歌曲时长 path={song_info.path}，"
                f"流水线模式下按 {_FALLBACK_SONG_DURATION:.0f}s 兜底"
            )
            song_duration = _FALLBACK_SONG_DURATION

        logger.info(
            f"准备播放《{song_info.name}》（{len(audio_bytes)} bytes，"
            f"时长 {song_duration:.1f}s，开场停顿 {pre_delay:.1f}s）"
        )

        use_pipeline = should_use_pipeline(
            is_live_mode=resolve_mode(self.chat_stream) == "vtb_live",
            section=config.pipelining,
            estimated_duration=pre_delay + song_duration,
        )
        performer = plugin.get_active_performer()
        timeline = MotionTimelineRunner(cues) if cues and performer is not None else None

        # 顺序门：准备工作已完成，在占用播放资源前让出。
        yield None

        # 此刻歌已确定要播，统一在这里记录历史（三条播放路径都会经过）。
        await sung_history.record(song_info.name)

        stream_id = self.chat_stream.stream_id
        if use_pipeline:
            yield await dispatch_track_pipelined(
                stream_id=stream_id,
                audio_bytes=audio_bytes,
                inst_bytes=inst_bytes,
                audio_player=audio_player,
                performer=performer,
                timeline=timeline,
                pre_delay=pre_delay,
                song_duration=song_duration,
                song_name=song_info.name,
            )
            return

        yield await play_track_blocking(
            stream_id=stream_id,
            audio_bytes=audio_bytes,
            inst_bytes=inst_bytes,
            audio_player=audio_player,
            performer=performer,
            timeline=timeline,
            pre_delay=pre_delay,
            song_name=song_info.name,
        )

    @staticmethod
    def _select_song(library: SongLibrary, keyword: str) -> SongInfo | None:
        """按关键词选歌；关键词为空或表示随机时随机选。

        Args:
            library: 歌库。
            keyword: 歌名关键词。

        Returns:
            选中的歌曲；歌单内找不到时返回 ``None``（**不**回退随机）。
        """

        cleaned = keyword.strip()
        if not cleaned or cleaned.lower() in _RANDOM_KEYWORDS:
            return library.get_random_song_info()
        return library.find_song_info(cleaned)

    @staticmethod
    def _build_not_found_message(library: SongLibrary, keyword: str) -> str:
        """构造"歌单里没有这首歌"的失败描述。

        明确告诉模型这首没有，让它按人设回应而不是糊弄一首。

        Args:
            library: 歌库。
            keyword: 用户点的歌名。

        Returns:
            给模型的失败描述。
        """

        available = library.get_song_names()
        preview = "、".join(available[:8]) if available else "（歌库为空）"
        more = f"...等共 {len(available)} 首" if len(available) > 8 else ""
        logger.info(f"歌单内未找到歌曲 '{keyword}'，拒绝执行（不回退随机）")
        return (
            f"执行失败：歌单里没有《{keyword}》这首歌。可用歌单预览：{preview}{more}。"
            "请根据此执行结果，以符合你人设的口吻做出回应。"
        )

    @staticmethod
    def _read_inst(song_info: SongInfo) -> bytes | None:
        """读取伴奏轨；失败时退化为单轨。

        Args:
            song_info: 歌曲元数据。

        Returns:
            伴奏 bytes；单轨歌或读取失败时返回 ``None``。
        """

        if song_info.inst_path is None:
            return None
        try:
            inst_bytes = song_info.inst_path.read_bytes() or None
        except OSError as exc:
            logger.warning(f"读取伴奏失败 path={song_info.inst_path}: {exc}，退化为单轨")
            return None
        if inst_bytes:
            logger.info(f"《{song_info.name}》双轨：人声→VB-Cable，伴奏→独立设备")
        return inst_bytes

    async def _announce(self, announce: str, song_name: str) -> None:
        """把唱歌开场白发到聊天流。

        Args:
            announce: 模型给的开场白；空串时用默认文案。
            song_name: 歌名。
        """

        text = announce.strip() or f"♪ 我来唱一首《{song_name}》"
        sent = await send_api.send_text(
            content=text,
            stream_id=self.chat_stream.stream_id,
            platform=self.chat_stream.platform,
        )
        if not sent:
            logger.warning(f"发送唱歌开场白失败: {text[:30]}")


__all__ = ["MotionCue", "MotionTimelineRunner", "SingSongAction", "parse_motion_timeline"]
