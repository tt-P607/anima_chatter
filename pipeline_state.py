"""anima_chatter vtb_live 流水线状态机。

仅在 ``vtb_live`` 模式下生效，目的是让"Action 提前返回 + 后台音频持续播放"
不打架——通过 :class:`PipelineState` 维护两个时间戳：

- ``audio_finish_at``：物理音频队列里最后一段音频**预计播完**的时刻；
  下一次 ``reserve`` 时新音频从这里之后开始排队。
- ``round_start_at``：**本轮 LLM 调用**第一次 ``reserve`` 时的起点；
  ``gate_at = round_start_at + round_accumulated * trigger_percent``，
  即 LLM 醒来重新执行 sub_agent / _build_user_prompt 的"流水线门"时刻。

**关键语义（与用户当面确认过）**：

1. **轮内**多个 Action 的 reserve 紧接排队，**不加 silence_gap**（同一轮
   连贯输出）。
2. **跨轮**第一个 reserve 加 ``silence_gap``——确保上一轮音频播完后再静
   默 ``silence_gap`` 秒再放新一轮的第一段（避免接得太快）。
3. **总时长低于 min_duration_seconds** 时不启用流水线，``gate_at`` 返回 0
   ——sub_agent / _build_user_prompt 不阻塞，走原阻塞行为。
4. **跨轮判定**通过 :meth:`reset_round` 显式标记，调用方负责调用——
   通过 sub_agent / _build_user_prompt 流水线门后调一次。

**Action 内的协议**：

::

    start_at, finish_at = await reserve(stream_id, duration)
    # ↑ 返回该段音频应该开始播放的时刻和结束时刻
    # 派发到后台 task：等到 start_at → 播放 → 直到 finish_at
    # Action 立即返回 Success，**不**等待 finish_at

**LLM 入口的协议**（plugin.py 的 sub_agent / _build_user_prompt）：

::

    await wait_gate(stream_id)  # 阻塞直到 gate_at（流水线门）
    reset_round(stream_id)      # 通过门 → 标记新一轮，下次 reserve 加 silence_gap

**为什么用模块级状态而不是 Service**：

- ServiceManager.get_service() 每次返回新实例（见框架 6.1.2），存状态会出错；
- anima_chatter 内 4 个文件需要读写状态（actions × 2 + plugin），写成 Service
  反而增加抽象层；
- 与 :mod:`call_state` 一致的设计模式（单例 + asyncio.Lock）。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

from src.app.plugin_system.api.log_api import get_logger

from ._internal_compat import wake_stream_from_wait


__all__ = [
    "PipelineSettings",
    "clear",
    "configure",
    "get_settings",
    "is_enabled",
    "reserve",
    "reset_round",
    "wait_gate",
]


logger = get_logger("anima_chatter.pipeline")


@dataclass(slots=True)
class PipelineSettings:
    """流水线配置（由插件加载时通过 :func:`configure` 注入）。

    所有字段对应 ``AnimaChatterConfig.pipelining`` section。
    """

    enabled: bool = False
    """流水线总开关。仅 ``vtb_live`` 模式下被读到；其他模式由 Action 层主动绕开。"""

    trigger_percent: float = 0.6
    """触发流水线门的累积时长百分比（0~1）。例如 0.6 表示总播放时长 60% 时
    LLM 醒来发起新一轮调用。"""

    silence_gap_seconds: float = 7.0
    """跨轮的静默间隔（秒）。上一轮音频播完后等这么久才放下一轮第一段。
    防止"接得太快"。**实际生效值会按 ``silence_gap_jitter`` 加随机波动**。"""

    silence_gap_jitter: float = 0.0
    """跨轮静默间隔的随机抖动幅度（秒）。每次 reserve 跨轮时，实际间隔为
    ``silence_gap_seconds + uniform(-jitter, +jitter)``。设为 0 关闭抖动，
    所有跨轮间隔严格等于 ``silence_gap_seconds``。"""

    min_duration_seconds: float = 10.0
    """最低门槛（秒）。本轮累积时长低于此值时不启用流水线，``gate_at`` 返回
    0，``wait_gate`` 立即放行。Action 也按原阻塞模式工作（由 Action 自身判断
    总时长是否够）。"""

    min_remaining_seconds: float = 25.0
    """**距结束最少剩余秒数**。本轮总播放结束前至少留这么多秒给 LLM 推理 +
    新一轮 reserve，避免在最后才唤醒导致音频断流。

    实际门时刻 = ``max(trigger_percent_gate, finish_at - min_remaining_seconds)``
    —— trigger_percent 给出的时刻和"结束前 N 秒"的时刻取**较晚者**（更靠后），
    这样能尽量多吞吐弹幕：

    - 短回复（30s, trigger=60%）: trigger_gate = 18s, "结束前25s" = 5s → 取 18s
      （按比例自然衔接）
    - 长歌曲（180s, trigger=60%）: trigger_gate = 108s, "结束前25s" = 155s → 取 155s
      （留 25s 余量给 LLM，期间 N 多弹幕都被聚合）

    设为 0 或负数关闭此上限，完全按 trigger_percent 比例等待。"""


@dataclass(slots=True)
class _StreamState:
    """单条 stream 的流水线状态。"""

    audio_finish_at: float = 0.0
    """物理音频队列中最后一段音频预计播完的 ``time.monotonic()`` 时刻。
    新 reserve 从这里之后排队。"""

    round_start_at: float = 0.0
    """本轮 LLM 调用第一次 reserve 时的起点（用于计算 gate_at）。"""

    round_accumulated: float = 0.0
    """本轮累积音频时长（秒，不含 silence_gap）。每次 reserve 累加；
    :meth:`PipelineState.reset_round` 时清零。"""

    is_round_first: bool = True
    """下一次 reserve 是否为本轮第一次。控制 silence_gap 是否加入起点。"""

    last_reserve_log: str = ""
    """最近一次 reserve 的日志摘要（用于 wait_gate 时回显，便于排查）。"""

    last_silence_gap_used: float = 0.0
    """最近一次跨轮 reserve 实际使用的 silence_gap（含 jitter 后的真实值）。
    仅供日志显示，不参与计算。"""

    wakeup_task: asyncio.Task | None = None
    """当前轮的"流水线门到点 → 唤醒 stream loop"后台任务句柄。
    每次 reserve 重新调度（取消旧的、起新的）；reset_round / clear 时取消。"""


_settings: PipelineSettings = PipelineSettings()
_states: dict[str, _StreamState] = {}
_lock = asyncio.Lock()


def configure(settings: PipelineSettings) -> None:
    """注入配置。插件 ``on_plugin_loaded`` 时调用一次。

    重复调用以最后一次为准；不需要清空状态——配置改了状态依然有效。
    """

    global _settings
    _settings = settings
    logger.info(
        f"流水线配置已应用: enabled={settings.enabled}, "
        f"trigger_percent={settings.trigger_percent:.2f}, "
        f"silence_gap={settings.silence_gap_seconds:.1f}s, "
        f"min_duration={settings.min_duration_seconds:.1f}s"
    )


def get_settings() -> PipelineSettings:
    """读取当前配置（只读快照）。"""

    return _settings


def is_enabled() -> bool:
    """流水线是否启用（配置开关）。Action 层通常配合 mode 一起判断。"""

    return _settings.enabled


def _get_state_unlocked(stream_id: str) -> _StreamState:
    """**不加锁**地获取/创建 stream 状态。仅供已持锁的内部函数使用。"""

    state = _states.get(stream_id)
    if state is None:
        state = _StreamState()
        _states[stream_id] = state
    return state


async def reserve(stream_id: str, duration: float) -> tuple[float, float]:
    """预约一段音频在播放队列中的位置。

    Args:
        stream_id: 当前 stream。
        duration: 该段音频的物理时长（秒）。**不含**任何 silence_gap。

    Returns:
        ``(start_at, finish_at)`` —— 该段音频应该开始播放的 ``time.monotonic()``
        时刻和结束时刻。调用方应：

        1. 立即派发后台 task：``await asyncio.sleep(start_at - now)`` →
           ``await play_audio()`` → 自然结束；
        2. Action 自己**立即返回**，不等 finish_at。

    Notes:
        - 跨轮的第一次 reserve 会自动加 ``silence_gap_seconds``；
        - 轮内的多次 reserve 紧接排队（无间隔）；
        - 调用方负责通过 :func:`reset_round` 标记新一轮开始。
    """

    duration = max(0.0, float(duration))
    if duration == 0.0:
        # 0 时长直接返回当前时刻；不影响累积。
        now = time.monotonic()
        return now, now

    async with _lock:
        state = _get_state_unlocked(stream_id)
        now = time.monotonic()

        if state.is_round_first:
            # 新轮第一次：跨轮 silence_gap 在这里生效（含随机抖动）
            if state.audio_finish_at > now:
                # 队列里还有上一轮音频 → 等它播完 + silence_gap (+jitter)
                jitter = _settings.silence_gap_jitter
                gap = _settings.silence_gap_seconds
                if jitter > 0:
                    gap = max(0.0, gap + random.uniform(-jitter, jitter))
                base = state.audio_finish_at + gap
                state.last_silence_gap_used = gap
            else:
                # 队列已空 → 直接开始
                base = now
                state.last_silence_gap_used = 0.0
            state.round_start_at = base
            state.is_round_first = False
        else:
            # 同轮后续 reserve：紧接队列尾（无 silence_gap）
            base = max(now, state.audio_finish_at)

        start_at = base
        finish_at = start_at + duration
        state.audio_finish_at = finish_at
        state.round_accumulated += duration

        delta_now = start_at - now
        is_first_log = state.round_accumulated == duration  # 即本次是该轮第一次
        round_marker = "🆕 新一轮第一段" if is_first_log else "➕ 同轮追加"
        gap_info = (
            f", silence_gap={state.last_silence_gap_used:.2f}s"
            if is_first_log and state.last_silence_gap_used > 0
            else ""
        )
        state.last_reserve_log = (
            f"reserve {round_marker}: duration={duration:.2f}s, "
            f"start_in={delta_now:.2f}s, "
            f"finish_in={finish_at - now:.2f}s, "
            f"round_acc={state.round_accumulated:.2f}s{gap_info}"
        )
        logger.info(
            f"[pipeline {stream_id[:8]}] 📥 {state.last_reserve_log}"
        )

        # 调度"流水线门到点 → 主动唤醒 stream loop"任务。
        # 每次 reserve 后重新调度：累计时长变化 → gate_at 时刻向后推迟。
        _schedule_wakeup_unlocked(stream_id, state)

        return start_at, finish_at


def _schedule_wakeup_unlocked(stream_id: str, state: _StreamState) -> None:
    """**不加锁**地（重新）调度"门到点唤醒 stream loop"的后台任务。

    每次 reserve 完成后调用：取消之前调度的任务（gate_at 已经被推迟）、
    根据当前 gate 重新起一个；如果 gate=0（流水线未启用 / 累积不足），
    不起任务。

    必须在已持有 ``_lock`` 的上下文中调用——本函数访问 state 字段。
    """

    # 取消已有任务（reserve 重新累加 → 门时刻被推迟）
    if state.wakeup_task is not None and not state.wakeup_task.done():
        state.wakeup_task.cancel()
    state.wakeup_task = None

    gate = _gate_at_unlocked(state)
    if gate <= 0:
        return  # 累积不足或未启用——不调度

    now = time.monotonic()
    wait_seconds = max(0.0, gate - now)

    async def _wakeup() -> None:
        try:
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            # only_if_new_unreads=True：仅在确实有新弹幕（且累积期间到达）时唤醒。
            # 没有新弹幕就让流继续 Wait，避免 LLM 没事找事主动发起对话。
            woke = wake_stream_from_wait(stream_id, only_if_new_unreads=True)
            if woke:
                logger.info(
                    f"[pipeline {stream_id[:8]}] ⏰ 流水线门到点（{gate - now:.2f}s 后）"
                    "+ 检测到新累积弹幕 → 主动唤醒 stream loop 处理"
                )
            else:
                logger.info(
                    f"[pipeline {stream_id[:8]}] ⏰ 流水线门到点（{gate - now:.2f}s 后）"
                    "但无新弹幕累积，继续保持 Wait（不主动发起对话）"
                )
        except asyncio.CancelledError:
            pass

    state.wakeup_task = asyncio.create_task(
        _wakeup(),
        name=f"anima_chatter.pipeline_wakeup.{stream_id[:8]}",
    )


def _gate_at_unlocked(state: _StreamState) -> float:
    """**不加锁**地计算流水线门时刻。返回 0 表示不启用。

    门时刻 = ``max(trigger_percent_gate, finish_at - min_remaining_seconds)``
    —— trigger_percent 给出的时刻和"结束前 N 秒"的时刻取**较晚者**：

    - 短回复（30s, trigger=60%）：trigger_gate = 起点+18s，结束前25s = 起点+5s
      → 取 起点+18s（按比例自然衔接）
    - 长歌曲（180s, trigger=60%）：trigger_gate = 起点+108s，结束前25s = 起点+155s
      → 取 起点+155s（推迟到末尾再唤醒，期间能吞吐更多弹幕）

    设 ``min_remaining_seconds <= 0`` 时关闭此后置上限，完全按 trigger_percent。
    """

    if not _settings.enabled:
        return 0.0
    if state.round_accumulated < _settings.min_duration_seconds:
        return 0.0

    trigger_gate = state.round_start_at + state.round_accumulated * _settings.trigger_percent
    min_remaining = _settings.min_remaining_seconds
    if min_remaining > 0:
        # finish_at 即 round_start_at + round_accumulated（中间没有插入间隔）
        finish_at = state.round_start_at + state.round_accumulated
        late_gate = finish_at - min_remaining
        # 取较晚者：让 LLM 尽可能往后等，但不超过"结束前 N 秒"
        return max(trigger_gate, late_gate)
    return trigger_gate


async def wait_gate(stream_id: str) -> None:
    """阻塞直到流水线门时刻到达。

    在 ``sub_agent`` 与 ``_build_user_prompt`` 入口（即将发起新一轮 LLM 调用前）
    调用，让 LLM 等到累积音频的 trigger_percent 时长后再执行。

    若：
    - 流水线未启用 / 模式不是 vtb_live → 立即返回（调用方应先判断模式）
    - 本轮累积时长低于 ``min_duration_seconds`` → 立即返回
    - gate 时刻已过 → 立即返回

    本函数**不**调用 :func:`reset_round`——上层在通过 gate 后自己决定是否
    标记新一轮（避免在 timer 唤醒等"非真正消费"的路径误清状态）。
    """

    async with _lock:
        state = _get_state_unlocked(stream_id)
        gate = _gate_at_unlocked(state)
        accumulated_snapshot = state.round_accumulated

    if gate <= 0:
        return

    now = time.monotonic()
    if gate <= now:
        logger.info(
            f"[pipeline {stream_id[:8]}] 🚪 流水线门已过（直通）: "
            f"gate-now={gate - now:.2f}s"
        )
        return

    wait_seconds = gate - now
    logger.info(
        f"[pipeline {stream_id[:8]}] 🚦 LLM 等流水线门：阻塞 {wait_seconds:.2f}s "
        f"(累计 {_settings.trigger_percent * 100:.0f}% 触发点；"
        f"本轮总时长 ≈ {accumulated_snapshot:.1f}s)"
    )

    # 流水线门可能阻塞数十秒，期间 chatter 主循环没机会 yield 心跳——必须主动
    # 喂 watchdog，避免触发 stream_warning_threshold (40s) / stream_restart
    # _threshold (60s) 把流强制重启。复用 anima_chatter.heartbeat 的基础设施。
    from .heartbeat import feed_watchdog_during

    try:
        async with feed_watchdog_during(stream_id, interval=5.0):
            await asyncio.sleep(wait_seconds)
        logger.info(
            f"[pipeline {stream_id[:8]}] ✅ 流水线门通过，LLM 开始新一轮调用"
        )
    except asyncio.CancelledError:
        # 流被取消时不阻塞清理路径
        logger.info(f"[pipeline {stream_id[:8]}] ❌ wait_gate 被取消")
        raise


async def reset_round(stream_id: str) -> None:
    """标记下一次 reserve 是新一轮的开始。

    通常在通过 :func:`wait_gate` 之后立即调用——让下一次 reserve 计算 base
    时把 ``silence_gap`` 加进去。

    清理本轮累积，但**不**清 ``audio_finish_at``（队列里还有未播完的音频，
    新轮 reserve 必须基于它排队）。
    """

    async with _lock:
        state = _get_state_unlocked(stream_id)
        had_round = state.round_accumulated > 0
        state.is_round_first = True
        state.round_accumulated = 0.0
        state.round_start_at = 0.0
        # audio_finish_at 故意不动——下一次 reserve 会基于它继续排队
        remaining = max(0.0, state.audio_finish_at - time.monotonic())
        # 取消上一轮的 wakeup_task（这一轮已经在被处理了，门已用完）
        if state.wakeup_task is not None and not state.wakeup_task.done():
            state.wakeup_task.cancel()
        state.wakeup_task = None

    if had_round:
        logger.info(
            f"[pipeline {stream_id[:8]}] 🔄 reset_round：本轮累积清零，"
            f"队列剩余 {remaining:.1f}s 音频未播完（下次 reserve 加 silence_gap）"
        )


async def clear(stream_id: str) -> None:
    """彻底清空指定 stream 的流水线状态。

    用于异常恢复 / chatter 卸载场景。清空后下一次 reserve 视同全新开始。
    """

    async with _lock:
        old = _states.pop(stream_id, None)
        if old is not None and old.wakeup_task is not None and not old.wakeup_task.done():
            old.wakeup_task.cancel()
    if old is not None:
        logger.info(f"[pipeline {stream_id[:8]}] 🧹 状态已彻底清空")
