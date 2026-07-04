"""anima_chatter 的"放本地翻唱歌曲"动作。

`SingSongAction` 在 vtb / vtb_live 模式（即非 local_asr 平台）激活，让模型
能直接选歌库里的清唱、通过本地 audio_player 推到 VB-Cable，让直播间观众能
听到。

歌库位置：``data/anima_chatter/songs/``，由 :class:`SongLibrary` 在插件
启动时扫描，挂在 ``plugin.song_library`` 上。

关键设计：
- **不走 send_voice**：B 站直播间不接受第三方 bot 发语音消息，所以这条 action
  绕开消息发送链路，直接 ``audio_player.play_audio``。
- **发文本提示**：默认发一条 ``"♪ 我来唱一首《XXX》"`` 文本到 chat_stream
  （QQ 群朋友能看到字），直播间观众只能听到声音。
- **VTS 嘴型 + 时间轴动作**：复用 :meth:`VTSPerformer.speaking_session` 让
  虚拟形象在唱歌期间嘴型同步（基于音频包络的"麦克风口型"）。同时支持模型
  通过 ``motion_timeline`` 给出"什么时间切到什么 intent / emotion"的剧本，
  播放期间后台 task 按时间触发 :meth:`VTSPerformer.switch_segment_intent`，
  让虚拟形象不至于唱整首都同一个姿势。
- **响度归一化**：歌曲 / TTS 都走 audio_player 同一套 loudness_target_dbfs
  做 RMS 拉齐，避免直播间观众一会儿响一会儿轻。
- **watchdog 喂狗**：阻塞模式下整段播放期间通过 ``feed_watchdog_during`` 防
  止 chatter 心跳过期被强制重启（清唱可能 30 秒以上）。
- **Schema 动态歌单**：``to_schema()`` 被覆写——在每次序列化 action schema
  时实时读取插件挂的 ``song_library``，把当前歌单（含时长）拼到 song_keyword
  的描述里。模型每次看到的都是最新真实歌库，不会虚构曲名。

**vtb_live 流水线模式**（仅在 ``mode == "vtb_live"`` + ``[pipelining].enabled``
同时满足时启用）：

- 通过 :func:`pipeline_state.reserve` 申请播放时段（基于 SongInfo 的预读时长，
  无需重新解码）；
- pre_song_delay 也算进 reserve 总时长——后台 task 才会真正等待这段静默；
- 派发到后台 task 执行播放，Action 立即返回 Success；
- 唱歌轻松超过 min_duration_seconds（默认 10s），实际场景下 100% 走流水线。
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, AsyncGenerator

from src.app.plugin_system.api import send_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.action import BaseAction

from .. import pipeline_state
from .._internal_compat import create_background_task
from ..audio import read_duration_from_path
from ..config import AnimaChatterConfig
from ..constants import normalize_intent, split_emotion
from ..heartbeat import feed_watchdog_during
from ..modes import resolve_mode


logger = get_logger("anima_chatter.action.sing_song")


def _format_duration(seconds: float | None) -> str:
    """秒数 → ``M:SS``；无效返回 ``"?"``。"""

    if seconds is None or seconds < 0:
        return "?"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def _get_singing_plugin_song_library() -> Any | None:
    """到全局 plugin manager 找 anima_chatter 的 song_library。

    ``to_schema`` 是 classmethod，没有 ``self.plugin`` 上下文，只能反向通过
    plugin_manager 拿到本插件实例再读 ``song_library`` 属性。
    """

    from .._internal_compat import get_anima_chatter_plugin

    plugin = get_anima_chatter_plugin()
    if plugin is None:
        return None
    return getattr(plugin, "song_library", None)


# ── 唱歌历史（仅内存，重启清空）──────────────────────────────
# 记录本次运行已经唱过的歌名，按播放先后排列（最新在末尾）。注入到
# song_keyword 动态描述里，让模型选歌时看到"最近已唱"，避免短时间重复唱同
# 一首。重启清空即可——一场直播一般就一个运行周期，足够防重复。
_SUNG_HISTORY: list[str] = []
# 注入描述时最多展示的"最近已唱"条数，太长会挤占 schema。
_SUNG_HISTORY_SHOW = 10


def _record_sung(song_name: str) -> None:
    """把刚唱的歌名记入历史（去重后追加到末尾，使其成为"最近一首"）。"""

    name = (song_name or "").strip()
    if not name:
        return
    if name in _SUNG_HISTORY:
        _SUNG_HISTORY.remove(name)
    _SUNG_HISTORY.append(name)


def _build_sung_history_block() -> str:
    """构造"最近已唱"提示块；历史为空时返回空串（不注入）。"""

    if not _SUNG_HISTORY:
        return ""
    recent = _SUNG_HISTORY[-_SUNG_HISTORY_SHOW:]
    # 末尾是最新，展示时倒序（最近唱的排最前）更直观。
    recent_desc = "、".join(f"《{name}》" for name in reversed(recent))
    return (
        "\n\n【最近已唱】（本次直播已经唱过，按时间从近到远）：\n"
        f"{recent_desc}\n"
        "除非观众明确点名要再听一遍，否则**优先选还没唱过的歌**，避免短时间重复。"
    )


def _build_song_keyword_desc(library: Any | None) -> str:
    """根据当前歌库构造 song_keyword 参数的描述（含动态歌单 + 最近已唱）。

    歌库未就绪时退回静态描述；就绪时把每首歌名 + 时长拼进 schema，让模型
    选歌时能看到真实库存。末尾再追加"最近已唱"历史块，提示模型避免重复。
    """

    base = (
        "要唱的歌曲名称，必须从下方【可用歌单】里选，不能虚构。\n"
        "**强烈建议直接复制下方歌单里的完整歌名**（含歌手 / 出处前缀，如 "
        "``三Z-STUDIO _ HOYO-MiX - 捉迷藏``），匹配最稳。\n"
        "也支持只写核心歌名做关键词匹配（如 ``捉迷藏``）——会按 精确 / 归一化 / "
        "子串包含 / 模糊 的顺序去命中。但若核心歌名太短或多首撞名，仍可能匹配偏差，"
        "所以能写全名就写全名。\n"
        "留空或填 ``random`` / ``随机`` 则随机选一首。\n"
        "歌单外的歌名会直接失败（无随机回退）——遇到点歌不在歌单里时，"
        "改用 say_and_perform 按人设回应，不要硬调本动作。调用前先核对下方歌单。"
    )
    history_block = _build_sung_history_block()
    if library is None:
        return base + "\n\n【可用歌单】（暂未加载，请稍后重试）" + history_block

    try:
        songs = library.get_songs()
    except Exception:  # noqa: BLE001
        return base + "\n\n【可用歌单】（读取失败）" + history_block

    if not songs:
        return (
            base
            + "\n\n【可用歌单】（歌库目前为空，请按你的人设口吻处理此情况）"
            + history_block
        )

    lines = [
        f"- ``{song.name}``（时长 {_format_duration(song.duration_seconds)}）"
        for song in songs
    ]
    return (
        base
        + "\n\n【可用歌单】（共 "
        + str(len(songs))
        + " 首）：\n"
        + "\n".join(lines)
        + history_block
    )


# sing_song 自己的兜底默认值（与通话 / 普通说话不同）：唱歌默认轻表现。
_SING_DEFAULT_EMOTION_TYPE = "happy"
_SING_DEFAULT_EMOTION_LEVEL = 1


def _normalize_sing_emotion(emotion: str | None) -> tuple[str, int]:
    """sing_song 专用 emotion 解析：缺省 / 非法时降级为 ``("happy", 1)``。

    与 :func:`constants.split_emotion` 共享一份解析规则，只是兜底默认换成
    "唱歌时表现轻一点"——避免每次出意料外的 emotion 输入就强行套上中性 2 级。
    """

    return split_emotion(
        emotion,
        default_type=_SING_DEFAULT_EMOTION_TYPE,
        default_level=_SING_DEFAULT_EMOTION_LEVEL,
    )


def _parse_motion_timeline(timeline: list[dict[str, Any]] | None) -> list[tuple[float, str, str]]:
    """把模型给的 motion_timeline 参数解析为 ``[(at_seconds, intent, emotion_main), ...]``。

    输入示例（list of dict）::

        [
          {"at": 0,    "intent": "EXCITED",     "emotion": "happy:2"},
          {"at": 30,   "intent": "PROUD_LIFT",  "emotion": "happy:2"},
          {"at": 75,   "intent": "SHY_DOWN",    "emotion": "happy:1"}
        ]

    解析时会：
    - 跳过 ``at`` 不是数字 / 负数的项；
    - 按 ``at`` 升序排序；
    - 非法 intent / emotion 降级（详见 ``_normalize_intent`` / ``_normalize_emotion``）。

    返回的 ``emotion_main`` 是主类型字符串（不带 :level），用于 expression
    匹配兜底（switch_segment_intent 的 emotion_main_for_expression 参数）。
    """

    if not timeline:
        return []

    parsed: list[tuple[float, str, str]] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        at_raw = item.get("at")
        if at_raw is None:
            continue
        try:
            at_sec = float(at_raw)
        except (TypeError, ValueError):
            continue
        if at_sec < 0:
            continue
        intent = normalize_intent(item.get("intent"))
        emo_main, _ = _normalize_sing_emotion(item.get("emotion"))
        parsed.append((at_sec, intent, emo_main))

    parsed.sort(key=lambda triple: triple[0])
    return parsed


async def _run_motion_timeline(
    *,
    timeline: list[tuple[float, str, str]],
    performer: Any,
    started_at: float,
    stop_event: asyncio.Event,
) -> None:
    """后台任务：按 ``timeline`` 在播放期间触发 VTS intent 切换。

    每个 cue 在 ``started_at + at_sec`` 时刻执行一次
    :meth:`VTSPerformer.switch_segment_intent`。``stop_event`` 触发后立即退出
    （播放结束 / 异常时由调用方触发）。

    Args:
        timeline: 已排序的 ``(at_seconds, intent, emotion_main)`` 列表。
        performer: VTSPerformer 实例。None 时本任务直接退出。
        started_at: ``time.monotonic()`` 时刻基准。
        stop_event: 让调用方主动停止本任务的信号。
    """

    if performer is None or not timeline:
        return

    for at_sec, intent, emo_main in timeline:
        target = started_at + at_sec
        now = time.monotonic()
        delay = target - now
        if delay > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                # stop_event 在 wait_for 内被触发：歌已播完，整段任务结束
                return
            except asyncio.TimeoutError:
                # 正常路径：到点了
                pass

        if stop_event.is_set():
            return

        try:
            await performer.switch_segment_intent(
                intent, emotion_main_for_expression=emo_main
            )
            logger.info(
                f"sing_song timeline cue @ {at_sec:.1f}s → "
                f"intent={intent} emotion_main={emo_main}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"sing_song timeline cue 切换失败（忽略）: {exc}")


class SingSongAction(BaseAction):
    """在 vtb / vtb_live 模式下播放歌库里的本地清唱到 VB-Cable。"""

    action_name = "sing_song"
    associated_types = ["voice", "text"]
    action_description = (
        "唱歌动作——播放歌库里已翻唱好的本地音频。"
        "直播间观众能听到声音，QQ 群里只显示一条文字提示。\n"
        "\n"
        "与 say_and_perform 的区别：say_and_perform 是用 TTS 朗读文本（不会唱歌）；"
        "本动作直接播放翻唱音频。想唱歌只能用本动作，"
        "不要用 TTS 朗读歌词冒充唱歌。\n"
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
        """覆写 schema 序列化：song_keyword 的 description 改为动态注入歌单。

        基类 :meth:`BaseAction.to_schema` 通过 ``parse_function_signature`` 解析
        ``execute`` 的 :class:`Annotated` 字段；这里先调基类拿到完整 schema，
        再就地把 song_keyword 的 description 改写成"基础说明 + 当前真实歌单"。
        这样每次 LLM 请求重新生成 schema 时都拿到最新歌库。
        """

        schema = super().to_schema()
        try:
            params = (
                schema.get("function", {})
                .get("parameters", {})
                .get("properties", {})
            )
            if "song_keyword" in params:
                library = _get_singing_plugin_song_library()
                params["song_keyword"]["description"] = _build_song_keyword_desc(library)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"动态注入歌单到 schema 失败（忽略）: {exc}")
        return schema

    async def go_activate(self) -> bool:
        """激活条件：

        - 非 local_asr（语音通话场景不让放歌，纯耳朵交互体验会乱）；
        - 当前 stream 不在 voice_call 通话中（同上）；
        - song_library 已就绪且歌库非空。
        """

        if self.chat_stream.platform == "local_asr":
            return False
        from .. import call_state as _cs

        if await _cs.is_call_active_for_stream(self.chat_stream.stream_id):
            return False

        library = getattr(self.plugin, "song_library", None)
        if library is None:
            return False
        return not library.is_empty

    async def execute(
        self,
        song_keyword: Annotated[
            str,
            "要唱的歌曲名称（动态歌单见 to_schema 注入）。",
        ] = "",
        announce: Annotated[
            str,
            "唱歌前发到聊天的开场白文本（可选）。例：``那我给大家唱一首《XX》吧♪``。\n"
            "只作为文字发到聊天流（QQ 群可见），不会被 TTS 朗读。"
            "想让观众也听到口播开场，先调一次 say_and_perform 再调本动作。",
        ] = "",
        motion_timeline: Annotated[
            list[dict[str, Any]],
            "可选的动作时间轴：让虚拟形象在歌曲不同段落切换动作 / 情绪，避免整首一个姿势。"
            "留空（``[]``）则全程默认 NARRATING + happy:1。\n"
            "\n"
            "格式：``[{\"at\": 秒数, \"intent\": \"动作名\", \"emotion\": \"类型:强度\"}, ...]``\n"
            "- ``at``：从歌曲开始算起的秒数（0 = 开头，75 = 1 分 15 秒）\n"
            "- ``intent``：动作意图（NARRATING / EXCITED / PROUD_LIFT / SHY_DOWN / "
            "PLAYFUL_TILT 等，完整列表见 say_and_perform）\n"
            "- ``emotion``：情绪:强度，如 ``happy:2`` ``sad:1`` ``neutral:1``\n"
            "\n"
            "动作保持时长 = 相邻两个 cue 的 ``at`` 时间差（``at:0`` 与 ``at:8`` 表示"
            "第一个动作保持 8 秒后切换）。段数自定、别太频繁；跟着歌曲段落"
            "（前奏 / 主歌 / 副歌 / 尾奏）或情绪起伏切换最自然。\n"
            "\n"
            "示例：``[{\"at\": 0, \"intent\": \"EXCITED\", \"emotion\": \"happy:2\"}, "
            "{\"at\": 8, \"intent\": \"NARRATING\", \"emotion\": \"happy:1\"}, "
            "{\"at\": 75, \"intent\": \"SHY_DOWN\", \"emotion\": \"happy:1\"}]``",
        ] = [],
        pre_song_delay: Annotated[
            float,
            "开场白发完后、歌曲开始前的停顿秒数（默认 4 秒）。\n"
            "给观众听完文字开场、调整心情的缓冲。想更隆重传 ``5`` ~ ``8``，"
            "想立刻开唱传 ``0`` ~ ``2``。\n"
            "停顿期间虚拟形象会显示 motion_timeline 第一个 cue 的动作，"
            "可用来做『准备开口』的姿态（如 EXCITED 抬手、SHY_DOWN 深呼吸）。",
        ] = 4.0,
    ) -> AsyncGenerator[tuple[bool, str] | None, None]:
        """执行播放：找歌 → 发开场白 → (流水线模式：reserve + 派发后台) → 立即返回。

        ``vtb_live`` 模式下走流水线：通过 :func:`pipeline_state.reserve` 申请
        播放时段后立即返回，pre_song_delay + 歌曲实际时长 = reserve 总时长。
        其他模式（vtb）保留原阻塞行为。

        本方法是**异步生成器**：找歌 / 发开场白 / 读文件等准备工作在首个
        ``yield None`` 之前完成，真正占用播放资源（``reserve`` / 阻塞播放）的
        关键段放在 ``yield None`` 之后——调度器会按 LLM 的 tool call 顺序放行，
        保证“先说话后唱歌”这类顺序依赖与调用顺序一致。
        """

        plugin_config = getattr(self.plugin, "config", None)
        if not isinstance(plugin_config, AnimaChatterConfig):
            yield False, "插件配置缺失，无法播放歌曲"
            return

        library = getattr(self.plugin, "song_library", None)
        if library is None:
            yield False, "song_library 未初始化（插件可能未正确加载）"
            return
        if library.is_empty:
            yield False, "歌库为空——请把清唱文件放到 plugins/anima_chatter/songs/ 目录"
            return

        # 1) 找歌（返回完整 SongInfo，含可选伴奏轨 inst_path）
        keyword = (song_keyword or "").strip()
        song_info = None
        if not keyword or keyword.lower() in {"random", "随机"}:
            song_info = library.get_random_song_info()
            if song_info is None:
                yield False, "歌库为空，无法随机选曲"
                return
        else:
            song_info = library.find_song_info(keyword)
            if song_info is None:
                # 关键变更：找不到不再回退随机；明确告诉模型这首没有，
                # 让它去说话拒绝而不是糊弄一首。
                available = library.get_song_names()
                preview = "、".join(available[:8]) if available else "（歌库为空）"
                more = f"...等共 {len(available)} 首" if len(available) > 8 else ""
                logger.info(
                    f"歌单内未找到歌曲 '{keyword}'，拒绝执行（不回退随机）"
                )
                yield False, (
                    f"执行失败：歌单里没有《{keyword}》这首歌。可用歌单预览：{preview}{more}。"
                    "请根据此执行结果，以符合你人设的口吻做出回应。"
                )
                return

        song_path = song_info.path
        inst_path = song_info.inst_path
        song_name = song_info.name

        # 2) 发开场白
        text_to_send = announce.strip() or f"♪ 我来唱一首《{song_name}》"
        try:
            await send_api.send_text(
                content=text_to_send,
                stream_id=self.chat_stream.stream_id,
                platform=self.chat_stream.platform,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"发送唱歌开场白失败: {exc}")

        # 3) 拿 audio_player + performer
        # 通过 get_active_performer 取当前激活的表演器（VTS）。
        audio_player = getattr(self.plugin, "audio_player", None)
        get_active = getattr(self.plugin, "get_active_performer", None)
        performer: Any = get_active() if callable(get_active) else None

        if audio_player is None:
            yield False, "audio_player 未初始化（可能 audio 配置缺失），无法播放"
            return

        # 4) 读文件 bytes（响度归一化由 audio_player 在播放时统一处理）
        try:
            audio_bytes = song_path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"读取歌曲文件失败 path={song_path}: {exc}", exc_info=True)
            yield False, f"读取歌曲文件失败: {exc}"
            return

        if not audio_bytes:
            yield False, "歌曲文件为空"
            return

        # 4.5) 双轨歌：读伴奏 bytes（伴奏走系统扬声器，不进 VB-Cable，不带动口型）
        inst_bytes: bytes | None = None
        if inst_path is not None:
            try:
                inst_bytes = inst_path.read_bytes() or None
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"读取伴奏失败 path={inst_path}: {exc}，退化为单轨人声")
                inst_bytes = None
            if inst_bytes:
                logger.info(f"《{song_name}》双轨：人声→VB-Cable，伴奏→系统扬声器")

        # 5) 解析 motion_timeline
        timeline = _parse_motion_timeline(motion_timeline)
        if timeline:
            logger.info(
                f"sing_song timeline 已解析 {len(timeline)} 个 cue: "
                f"{[(round(t[0], 1), t[1]) for t in timeline]}"
            )

        # 6) 计算开场静默 + 歌曲时长
        pre_delay = max(0.0, float(pre_song_delay))
        song_duration = read_duration_from_path(song_path) or 0.0
        if song_duration <= 0:
            logger.warning(
                f"无法读取歌曲时长 path={song_path}，流水线模式下将以 60s 兜底"
            )
            song_duration = 60.0

        logger.info(
            f"🎵 sing_song 处理歌曲: 《{song_name}》 ({len(audio_bytes)} bytes, "
            f"时长 {song_duration:.1f}s, pre_delay {pre_delay:.1f}s)"
        )

        # 7) 流水线门判定
        mode = resolve_mode(self.chat_stream)
        pipeline_enabled = (
            mode == "vtb_live"
            and bool(plugin_config.pipelining.enabled)
        )

        # 顺序门：准备工作（找歌 / 发开场白 / 读文件 / 算时长）已完成，在真正
        # 占用播放资源（reserve / 阻塞播放）前 yield None，调度器会按 tool call
        # 顺序放行，保证此前调用的 say_and_perform 先完成 reserve。
        yield None

        # 记录唱歌历史：此刻歌已确定要播（找歌成功、文件就绪、门已通过），
        # 三条播放路径（流水线 / 短歌退化 / 阻塞）都会经过这里，统一在此记录。
        _record_sung(song_name)

        # ── 流水线分支：reserve + 派发后台 + 立即返回 ──
        if pipeline_enabled:
            total_duration = pre_delay + song_duration
            min_duration = float(plugin_config.pipelining.min_duration_seconds)
            if total_duration < min_duration:
                # 短歌（极罕见）走阻塞模式
                logger.info(
                    f"⚠️ sing_song 流水线退化：歌曲总时长 {total_duration:.2f}s < "
                    f"min_duration {min_duration:.2f}s，本次走原阻塞模式"
                )
                yield await self._play_blocking(
                    audio_bytes=audio_bytes,
                    inst_bytes=inst_bytes,
                    audio_player=audio_player,
                    performer=performer,
                    timeline=timeline,
                    pre_delay=pre_delay,
                    song_name=song_name,
                )
                return

            logger.info(
                f"🎬 sing_song 进入流水线：《{song_name}》"
                f" 总 {total_duration:.1f}s ({pre_delay:.1f}s 静默 + "
                f"{song_duration:.1f}s 歌曲)，开始 reserve"
            )
            start_at, finish_at = await pipeline_state.reserve(
                self.chat_stream.stream_id, total_duration
            )

            create_background_task(
                self._background_play(
                    start_at=start_at,
                    pre_delay=pre_delay,
                    audio_bytes=audio_bytes,
                    inst_bytes=inst_bytes,
                    audio_player=audio_player,
                    performer=performer,
                    timeline=timeline,
                    song_name=song_name,
                    stream_id=self.chat_stream.stream_id,
                ),
                name=f"anima_chatter.background_sing.{self.chat_stream.stream_id[:8]}",
            )

            logger.info(
                f"✈️ sing_song 已派发后台、Action 立即返回 "
                f"(《{song_name}》, 后台播放预计 {total_duration:.1f}s)"
            )
            yield True, (
                f"已派发歌曲《{song_name}》到后台播放队列 "
                f"(总时长 {total_duration:.1f}s, 流水线 enabled)"
            )
            return

        # ── 阻塞分支（vtb 模式或流水线禁用）：原行为 ──
        yield await self._play_blocking(
            audio_bytes=audio_bytes,
            inst_bytes=inst_bytes,
            audio_player=audio_player,
            performer=performer,
            timeline=timeline,
            pre_delay=pre_delay,
            song_name=song_name,
        )
        return

    # ── 共享播放路径 ────────────────────────────────────────

    async def _play_blocking(
        self,
        *,
        audio_bytes: bytes,
        inst_bytes: bytes | None = None,
        audio_player: Any,
        performer: Any,
        timeline: list[tuple[float, str, str]],
        pre_delay: float,
        song_name: str,
    ) -> tuple[bool, str]:
        """阻塞模式：在 chatter generator 内部播完才返回。沿用原行为 + 喂狗。"""

        # 开场停顿（在 speaking_session 外，避免占用 VTS 锁）
        if pre_delay > 0:
            logger.info(f"sing_song 开场停顿 {pre_delay:.1f}s 后开始播放")
            await asyncio.sleep(pre_delay)

        stop_event = asyncio.Event()
        timeline_task: asyncio.Task[None] | None = None

        try:
            async with feed_watchdog_during(self.chat_stream.stream_id):
                await self._play_with_timeline(
                    audio_bytes=audio_bytes,
                    inst_bytes=inst_bytes,
                    audio_player=audio_player,
                    performer=performer,
                    timeline=timeline,
                    stop_event=stop_event,
                    song_name=song_name,
                )
        except asyncio.CancelledError:
            stop_event.set()
            raise
        except Exception as exc:  # noqa: BLE001
            stop_event.set()
            logger.error(f"播放歌曲失败: {exc}", exc_info=True)
            return False, f"播放歌曲失败: {exc}"
        finally:
            stop_event.set()
            if timeline_task is not None and not timeline_task.done():
                timeline_task.cancel()
                try:
                    await timeline_task
                except (asyncio.CancelledError, Exception):
                    pass

        logger.info(f"歌曲播放完成: {song_name}")
        return True, f"已播放歌曲: {song_name}"

    async def _background_play(
        self,
        *,
        start_at: float,
        pre_delay: float,
        audio_bytes: bytes,
        inst_bytes: bytes | None = None,
        audio_player: Any,
        performer: Any,
        timeline: list[tuple[float, str, str]],
        song_name: str,
        stream_id: str,
    ) -> None:
        """后台播放任务：等到 start_at → pre_delay → 播放歌曲。

        本协程脱离 chatter generator，无需喂狗——stream loop 已经 yield 进入
        下一轮。异常仅记日志（流水线模式失败反馈给 LLM 成本太高）。
        """

        try:
            now = time.monotonic()
            wait = start_at - now
            if wait > 0:
                logger.info(
                    f"⏳ [bg_sing {stream_id[:8]}] 排队中：等待 {wait:.2f}s "
                    f"到 start_at（《{song_name}》）"
                )
                await asyncio.sleep(wait)

            if pre_delay > 0:
                logger.info(
                    f"🎤 [bg_sing {stream_id[:8]}] 开场停顿 {pre_delay:.1f}s "
                    f"后开始播放《{song_name}》"
                )
                await asyncio.sleep(pre_delay)

            logger.info(
                f"🎵 [bg_sing {stream_id[:8]}] 开唱：《{song_name}》"
            )
            stop_event = asyncio.Event()
            try:
                await self._play_with_timeline(
                    audio_bytes=audio_bytes,
                    inst_bytes=inst_bytes,
                    audio_player=audio_player,
                    performer=performer,
                    timeline=timeline,
                    stop_event=stop_event,
                    song_name=song_name,
                )
            finally:
                stop_event.set()

            logger.info(
                f"✅ [bg_sing {stream_id[:8]}] 唱完了：《{song_name}》"
            )
        except asyncio.CancelledError:
            logger.info(f"[bg_sing {stream_id[:8]}] 后台歌曲被取消: {song_name}")
            raise
        except Exception as exc:
            logger.error(
                f"[bg_sing {stream_id[:8]}] 后台播放异常 {song_name}: {exc}",
                exc_info=True,
            )

    async def _play_with_timeline(
        self,
        *,
        audio_bytes: bytes,
        inst_bytes: bytes | None = None,
        audio_player: Any,
        performer: Any,
        timeline: list[tuple[float, str, str]],
        stop_event: asyncio.Event,
        song_name: str,
    ) -> None:
        """共享播放循环：在 ``speaking_session`` 内启动 timeline + 播音频。

        阻塞模式 / 流水线后台模式都用这条共享路径，避免逻辑分叉。
        ``stop_event`` 仅用于让 timeline_task 在异常时同步退出。

        ``inst_bytes`` 非空时走双轨：人声进 VB-Cable 驱动口型、伴奏进系统扬声器，
        伴奏不带动口型。为空时走原单轨 play_audio。
        """

        async def _play_track() -> None:
            """根据是否有伴奏选单轨 / 双轨播放。"""

            if inst_bytes:
                await audio_player.play_dual(audio_bytes, inst_bytes)
            else:
                await audio_player.play_audio(audio_bytes)

        timeline_task: asyncio.Task[None] | None = None

        if performer is not None:
            # 时间轴第一个 cue 决定 speaking_session 的初始 emotion / intent；
            # 这样首段就能立即生效，不用等 0 秒 cue 触发后再切。
            if timeline and timeline[0][0] <= 0.5:
                first_intent = timeline[0][1]
                first_emo_main = timeline[0][2]
                # 用 happy:2 作为强度默认；模型如果传了带 level 的会保留。
                initial_emotion = f"{first_emo_main}:2"
                initial_intent = first_intent
            else:
                initial_emotion = "happy:1"
                initial_intent = "NARRATING"

            async with performer.speaking_session(
                emotion=initial_emotion, intent=initial_intent
            ):
                # 启动时间轴后台任务（如果 timeline 非空）
                if timeline:
                    started_at = time.monotonic()
                    # 跳过首个 cue 如果它已经被 speaking_session 的 initial 设了
                    tail_timeline = (
                        timeline[1:]
                        if timeline[0][0] <= 0.5
                        else timeline
                    )
                    if tail_timeline:
                        timeline_task = asyncio.create_task(
                            _run_motion_timeline(
                                timeline=tail_timeline,
                                performer=performer,
                                started_at=started_at,
                                stop_event=stop_event,
                            ),
                            name=f"sing_song_timeline_{song_name[:20]}",
                        )

                try:
                    # 不调 start_speech_playback——唱歌不需要触发 hotkey。
                    # 让 audio_player 跑，VTS 麦克风口型 + 时间轴 task 自动同步。
                    await _play_track()
                finally:
                    if timeline_task is not None and not timeline_task.done():
                        stop_event.set()
                        timeline_task.cancel()
                        try:
                            await timeline_task
                        except (asyncio.CancelledError, Exception):
                            pass
        else:
            await _play_track()


__all__ = ["SingSongAction"]
