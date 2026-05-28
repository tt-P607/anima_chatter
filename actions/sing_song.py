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
- **watchdog 喂狗**：整段播放期间通过 ``feed_watchdog_during`` 防止 chatter
  心跳过期被强制重启（清唱可能 30 秒以上）。
- **Schema 动态歌单**：``to_schema()`` 被覆写——在每次序列化 action schema
  时实时读取插件挂的 ``song_library``，把当前歌单（含时长）拼到 song_keyword
  的描述里。模型每次看到的都是最新真实歌库，不会虚构曲名。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated, Any

from src.app.plugin_system.api import send_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.action import BaseAction

from ..config import AnimaChatterConfig
from ..heartbeat import feed_watchdog_during


logger = get_logger("anima_chatter.action.sing_song")


# 与 VTSPerformer 同步的合法 intent 集合；非法值降级为 NARRATING。
# 复制而非 import 是为了避免 schema 序列化阶段引入 vts 模块。
_VALID_INTENTS: frozenset[str] = frozenset(
    {
        "IDLE", "NARRATING", "THINKING", "CONFUSED",
        "EXCITED", "SURPRISED",
        "PEEK_LEFT", "PEEK_RIGHT", "LOOKAWAY", "STARE_DOWN", "DREAMY_GAZE",
        "PROUD_LIFT", "WORRIED_TILT", "SHY_DOWN", "ATTENTIVE",
        "PLAYFUL_TILT", "MISCHIEF", "SCARED_SHRINK",
    }
)

# 唱歌时的合法 emotion 主类型。带不带 :level 都接收，没传或非法会归一。
_VALID_EMOTION_TYPES: frozenset[str] = frozenset(
    {"neutral", "happy", "sad", "angry", "surprised"}
)


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

    try:
        from src.core.managers import get_plugin_manager

        plugin = get_plugin_manager().get_plugin("anima_chatter")
        if plugin is None:
            return None
        return getattr(plugin, "song_library", None)
    except Exception:  # noqa: BLE001
        return None


def _build_song_keyword_desc(library: Any | None) -> str:
    """根据当前歌库构造 song_keyword 参数的描述（含动态歌单）。

    歌库未就绪时退回静态描述；就绪时把每首歌名 + 时长拼进 schema，让模型
    选歌时能看到真实库存。
    """

    base = (
        "要唱的歌曲名称——必须从下面【可用歌单】里选，**不能虚构曲名**。\n"
        "支持精确匹配 / 归一化匹配（去掉括号 / 空格）/ 模糊匹配。\n"
        "留空或填 ``random`` / ``随机`` 时由系统随机选一首。\n"
        "**重要**：如果观众点的歌不在歌单里，**不要**硬调本动作（系统会自动"
        "回退到随机一首，但歌名不对）——你应该改用 ``say_and_perform`` "
        "礼貌地说\"这首我没练过\"或\"等我下次直播再练\"之类的话术，再问"
        "他要不要听其他歌。"
    )
    if library is None:
        return base + "\n\n【可用歌单】（暂未加载，请稍后重试）"

    try:
        songs = library.get_songs()
    except Exception:  # noqa: BLE001
        return base + "\n\n【可用歌单】（读取失败）"

    if not songs:
        return base + "\n\n【可用歌单】（歌库为空）"

    lines = [
        f"- ``{song.name}``（时长 {_format_duration(song.duration_seconds)}）"
        for song in songs
    ]
    return base + "\n\n【可用歌单】（共 " + str(len(songs)) + " 首）：\n" + "\n".join(lines)


def _normalize_intent(intent: str | None) -> str:
    """归一化 intent；非法值降级为 NARRATING。"""

    if not intent:
        return "NARRATING"
    upper = str(intent).strip().upper()
    return upper if upper in _VALID_INTENTS else "NARRATING"


def _normalize_emotion(emotion: str | None) -> tuple[str, int]:
    """``"happy:2"`` → ``("happy", 2)``；非法降级 ``("happy", 1)``（唱歌默认轻微表现）。"""

    if not emotion:
        return ("happy", 1)
    parts = str(emotion).strip().lower().split(":", 1)
    main = parts[0] if parts[0] in _VALID_EMOTION_TYPES else "happy"
    if len(parts) > 1 and parts[1].strip().isdigit():
        level = int(parts[1])
    else:
        level = 1
    level = max(1, min(3, level))
    return (main, level)


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
        intent = _normalize_intent(item.get("intent"))
        emo_main, _ = _normalize_emotion(item.get("emotion"))
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
    action_description = (
        "**唱歌专用动作**——播放歌库里的本地清唱（无背景音乐的翻唱）到本地音频通道，"
        "直播间观众能听到声音、QQ 群里只会看到一条文字提示。\n"
        "\n"
        "**和 say_and_perform 的本质区别**：\n"
        "- ``say_and_perform`` 是**说话**——用 TTS 把你写的文本朗读出来，TTS 不能唱歌\n"
        "- ``sing_song`` 是**唱歌**——直接播放本地已经翻唱好的音频文件\n"
        "  TTS 朗读歌词不算唱歌，听起来很怪。如果你想唱歌就只能调这个动作\n"
        "\n"
        "**只能选歌库里已有的歌**——歌单见 ``song_keyword`` 参数描述。"
        "**库里没有的歌**：用 say_and_perform 礼貌说\"这首没练过\"\"下次准备\"，"
        "**不要**强行调本动作，更**不要**用 TTS 朗读歌词冒充。\n"
        "\n"
        "**唱歌期间你不能说话**——歌曲会全程播放完才能继续 say_and_perform。"
        "想表达\"切歌\"\"停下\"等想法，可以等歌唱完再 say_and_perform。"
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
            "唱歌前发到聊天的开场白文本（可选）。比如 "
            "``那我给大家唱一首《XX》吧♪``。"
            "**只**会作为文字发到聊天流，QQ 群朋友能看到，**不**会被 TTS 朗读"
            "——为了让直播间观众尽快听到歌，开场白只走文字。"
            "想要观众也听到口播开场，请先调一次 say_and_perform 再调本动作。",
        ] = "",
        motion_timeline: Annotated[
            list[dict[str, Any]],
            "可选的动作时间轴：让虚拟形象在歌曲不同时间段切换 intent / emotion，"
            "避免唱整首一直一个姿势显得呆。\n"
            "格式：``[{\"at\": 秒数, \"intent\": \"动作名\", \"emotion\": \"类型:强度\"}, ...]``。\n"
            "字段说明：\n"
            "- ``at``：从歌曲开始算起的秒数（比如 0 = 开头、5 = 5 秒处、75 = 1 分 15 秒）\n"
            "- ``intent``：动作意图，可选 18 个之一（NARRATING / EXCITED / PROUD_LIFT / "
            "SHY_DOWN / PLAYFUL_TILT 等，详见 say_and_perform 的 intent 列表）\n"
            "- ``emotion``：情绪 + 强度，格式 ``happy:2`` ``sad:1`` ``neutral:1`` 等\n"
            "\n"
            "**段数和密度由你自由决定**——可以只 1 段（开头切一次然后保持），也可以分几段"
            "（不宜太频繁）。**动作之间的间隔等于相邻两个 cue 的 ``at`` 时间差**——比如 "
            "``at: 0`` 和 ``at: 8`` 就意味着第一个动作保持 8 秒后才切换，这是 LLM 控制"
            "节奏的方式。\n"
            "\n"
            "示例（一首 1 分半的歌曲，按段落切）：\n"
            "``[{\"at\": 0, \"intent\": \"EXCITED\", \"emotion\": \"happy:2\"}, "
            "{\"at\": 8, \"intent\": \"NARRATING\", \"emotion\": \"happy:1\"}, "
            "{\"at\": 30, \"intent\": \"PROUD_LIFT\", \"emotion\": \"happy:2\"}, "
            "{\"at\": 75, \"intent\": \"SHY_DOWN\", \"emotion\": \"happy:1\"}]``\n"
            "\n"
            "选段思路：跟着歌曲段落（前奏 / 主歌 / 副歌 / 桥段 / 尾奏）切动作最自然，"
            "也可以按情绪起伏（平静 → 渐强 → 高潮 → 收束）。\n"
            "留空（``[]``）则全程保持默认 NARRATING + happy:1，不做时间轴切换。",
        ] = [],
        pre_song_delay: Annotated[
            float,
            "开场白发完后、歌曲正式开始播放前的停顿秒数。\n"
            "用途：让观众听完文字开场后有几秒静默缓冲，再开始唱——避免开场白和歌"
            "之间衔接太紧、观众没来得及调好心情。\n"
            "**默认 4 秒**，这是直播场景下的体感最佳值（让观众有时间『屏息听歌』）。"
            "想更隆重可以加大到 ``5`` ~ ``8``；想立刻开唱传 ``0`` ~ ``2``。\n"
            "**注意**：这段停顿期间虚拟形象会显示 ``motion_timeline`` 第一个 cue 的"
            "动作（如果有），所以可以用来做\"准备唱歌的姿态\"——比如先 EXCITED 抬手、"
            "或 SHY_DOWN 低头深呼吸，让观众看着虚拟形象\"准备开口\"的过程。",
        ] = 4.0,
    ) -> tuple[bool, str]:
        """执行播放：找歌 → 发开场白 → 启动时间轴 task → 推到 audio_player → 等播完。"""

        plugin_config = getattr(self.plugin, "config", None)
        if not isinstance(plugin_config, AnimaChatterConfig):
            return False, "插件配置缺失，无法播放歌曲"

        library = getattr(self.plugin, "song_library", None)
        if library is None:
            return False, "song_library 未初始化（插件可能未正确加载）"
        if library.is_empty:
            return False, "歌库为空——请把清唱文件放到 plugins/anima_chatter/songs/ 目录"

        # 找歌路径——library.find_song 已包含模糊匹配；空关键词随机。
        keyword = (song_keyword or "").strip()
        song_path: Path | None = None
        if keyword and keyword.lower() not in {"random", "随机"}:
            song_path = library.find_song(keyword)
            if song_path is None:
                logger.info(f"未匹配到歌曲 '{keyword}'，回退随机选曲")
                song_path = library.get_random_song_path()
        else:
            song_path = library.get_random_song_path()

        if song_path is None:
            return False, "歌库为空或无法选出歌曲"

        song_name = song_path.stem

        # 1) 发开场白。直播间观众听不到这段，但 QQ 群朋友能看到字。
        text_to_send = announce.strip() or f"♪ 我来唱一首《{song_name}》"
        try:
            await send_api.send_text(
                content=text_to_send,
                stream_id=self.chat_stream.stream_id,
                platform=self.chat_stream.platform,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"发送唱歌开场白失败: {exc}")

        # 2) 拿 audio_player + performer
        audio_player = getattr(self.plugin, "audio_player", None)
        performer = getattr(self.plugin, "vts_performer", None)

        if audio_player is None:
            return False, "audio_player 未初始化（可能 audio 配置缺失），无法播放"

        # 3) 读文件（响度归一化由 audio_player 在 play_audio 内统一处理）
        try:
            audio_bytes = song_path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"读取歌曲文件失败 path={song_path}: {exc}", exc_info=True)
            return False, f"读取歌曲文件失败: {exc}"

        if not audio_bytes:
            return False, "歌曲文件为空"

        # 4) 解析 motion_timeline
        timeline = _parse_motion_timeline(motion_timeline)
        if timeline:
            logger.info(
                f"sing_song timeline 已解析 {len(timeline)} 个 cue: "
                f"{[(round(t[0], 1), t[1]) for t in timeline]}"
            )

        logger.info(
            f"开始播放歌曲: {song_name} ({len(audio_bytes)} bytes) → audio_player"
        )

        # 4.5) 开场停顿：让观众有时间静下来再开始唱。
        # 这段停顿在 speaking_session **外**进行，避免占用 VTS 锁，让其它流程
        # 能继续走（虽然这里 anima_chatter 是单流，但出于良好实践仍这样做）。
        pre_delay = max(0.0, float(pre_song_delay))
        if pre_delay > 0:
            logger.info(f"sing_song 开场停顿 {pre_delay:.1f}s 后开始播放")
            await asyncio.sleep(pre_delay)

        # 5) 唱歌期间防 watchdog；用 speaking_session 让虚拟形象嘴型 / 身体跟动。
        # 时间轴 task 在 speaking_session 内启动，在歌结束 / 异常时通过 stop_event
        # 触发它退出。
        stop_event = asyncio.Event()
        timeline_task: asyncio.Task[None] | None = None

        try:
            async with feed_watchdog_during(self.chat_stream.stream_id):
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

                        # 不调 start_speech_playback——唱歌不需要触发 hotkey。
                        # 让 audio_player 跑，VTS 麦克风口型 + 时间轴 task 自动同步。
                        await audio_player.play_audio(audio_bytes)
                else:
                    await audio_player.play_audio(audio_bytes)
        except asyncio.CancelledError:
            stop_event.set()
            raise
        except Exception as exc:  # noqa: BLE001
            stop_event.set()
            logger.error(f"播放歌曲失败: {exc}", exc_info=True)
            return False, f"播放歌曲失败: {exc}"
        finally:
            # 不论成功失败，让时间轴任务安静退出
            stop_event.set()
            if timeline_task is not None and not timeline_task.done():
                timeline_task.cancel()
                try:
                    await timeline_task
                except (asyncio.CancelledError, Exception):
                    pass

        logger.info(f"歌曲播放完成: {song_name}")
        return True, f"已播放歌曲: {song_name}"


__all__ = ["SingSongAction"]
