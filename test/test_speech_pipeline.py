"""语音合成调度、有限缓冲与播放顺序的单元测试。"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock

import numpy as np
import pytest

from plugins.anima_chatter.audio.player import AudioPlayer
from plugins.anima_chatter.speech import (
    PerformanceStyle,
    SpeechSegment,
    build_segment_markers,
    estimate_segments_duration,
)
from plugins.anima_chatter.speech.backend import PCMStream, TTSRequest


class FakeOutputStream:
    """记录 PCM 输出并暴露线程安全的写入事件。"""

    instances: list[FakeOutputStream]
    first_write: threading.Event

    def __init__(self, **kwargs: Any) -> None:
        self.blocks: list[np.ndarray] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def write(self, block: np.ndarray) -> None:
        self.blocks.append(block.copy())
        self.__class__.first_write.set()


class FakePCMStream:
    """提供指定格式的异步 PCM 字节流。"""

    def __init__(
        self,
        chunks: AsyncIterator[bytes],
        *,
        sample_rate: int = 48000,
        channels: int = 1,
        sample_format: str = "s16le",
    ) -> None:
        self.chunks = chunks
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_format = sample_format


class FakePCMProvider:
    """记录 PCM 请求，并允许测试控制单次 Provider 上下文。"""

    def __init__(self, factory: Any) -> None:
        self.factory = factory
        self.requests: list[TTSRequest] = []
        self.open_count = 0
        self.close_count = 0
        self.active = 0
        self.max_active = 0
        self.opened: asyncio.Queue[None] = asyncio.Queue()
        self.closed: asyncio.Queue[None] = asyncio.Queue()

    @contextlib.asynccontextmanager
    async def open_pcm_stream(self, request: TTSRequest) -> AsyncIterator[PCMStream]:
        self.requests.append(request)
        self.open_count += 1
        self.opened.put_nowait(None)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            async with self.factory(request) as stream:
                yield stream
        finally:
            self.active -= 1
            self.close_count += 1
            self.closed.put_nowait(None)


@pytest.fixture
def pcm_player(monkeypatch: pytest.MonkeyPatch) -> AudioPlayer:
    """构造不访问真实声卡的 AudioPlayer。"""

    from plugins.anima_chatter.audio import player as player_module

    FakeOutputStream.instances = []
    FakeOutputStream.first_write = threading.Event()
    monkeypatch.setattr(player_module.sd, "OutputStream", FakeOutputStream)
    monkeypatch.setattr(AudioPlayer, "_resolve_device", lambda _: None)
    monkeypatch.setattr(AudioPlayer, "_resolve_inst_device", lambda _: None)
    player = AudioPlayer(output_device="", loudness_target_dbfs=None)
    monkeypatch.setattr(player, "_build_extra_settings", lambda _: None)
    return player


@pytest.fixture
async def isolated_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[asyncio.Task[Any]]]:
    """隔离直播播放状态、任务派发、管线状态与 watchdog。"""

    from plugins.anima_chatter.runtime import heartbeat, pipeline_state
    from plugins.anima_chatter.speech import playback, streaming

    playback.activate_playback()
    class PlaybackTasks(list[asyncio.Task[Any]]):
        reservation_kinds: list[str]

    tasks = PlaybackTasks()
    tasks.reservation_kinds = []

    def create_task(coro: Any, *, name: str, metadata: Any) -> SimpleNamespace:
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return SimpleNamespace(task=task)

    @contextlib.asynccontextmanager
    async def no_heartbeat(stream_id: str) -> AsyncIterator[None]:
        yield

    async def reserve(
        stream_id: str, duration: float, *, track_id: str, kind: str = "speech"
    ) -> tuple[float, float]:
        tasks.reservation_kinds.append(kind)
        now = asyncio.get_running_loop().time()
        return now, now + duration

    async def no_op(*args: Any, **kwargs: Any) -> None:
        """隔离不影响本测试断言的管线回写。"""

    monkeypatch.setattr(playback, "create_background_task", create_task)
    monkeypatch.setattr(streaming, "feed_watchdog_during", no_heartbeat)
    monkeypatch.setattr(playback, "feed_watchdog_during", no_heartbeat)
    monkeypatch.setattr(pipeline_state, "reserve", reserve)
    monkeypatch.setattr(pipeline_state, "report_actual", no_op)
    monkeypatch.setattr(pipeline_state, "relinquish", no_op)
    monkeypatch.setattr(heartbeat, "feed_watchdog_during", no_heartbeat)
    try:
        yield tasks
    finally:
        await playback.close_playback()
        playback.activate_playback()


@pytest.fixture
async def real_pipeline_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[asyncio.Task[Any]]]:
    """保留轮次容量和自动唤醒，隔离任务管理与 watchdog。"""

    from plugins.anima_chatter.config import PipeliningSection
    from plugins.anima_chatter.runtime import pipeline_state
    from plugins.anima_chatter.speech import playback, streaming

    tasks: list[asyncio.Task[Any]] = []

    def create_task(coro: Any, *, name: str, metadata: Any) -> SimpleNamespace:
        """记录真实调度代码派发的任务。"""

        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return SimpleNamespace(task=task)

    def cancel_task(handle: SimpleNamespace | None) -> None:
        """取消当前隔离测试拥有的任务。"""

        if handle is not None:
            handle.task.cancel()

    @contextlib.asynccontextmanager
    async def no_heartbeat(
        stream_id: str, **kwargs: Any
    ) -> AsyncIterator[None]:
        """在不访问框架 watchdog 的情况下保留作用域。"""

        yield

    monkeypatch.setattr(pipeline_state, "_section", pipeline_state._section)
    pipeline_state.configure(
        PipeliningSection(song_prepare_lead_seconds=25.0, max_backlog_seconds=120.0)
    )
    monkeypatch.setattr(playback, "create_background_task", create_task)
    monkeypatch.setattr(pipeline_state, "create_background_task", create_task)
    monkeypatch.setattr(pipeline_state, "cancel_background_task", cancel_task)
    monkeypatch.setattr(pipeline_state, "_WAKEUP_CONFIRM_INTERVAL", 0.0)
    monkeypatch.setattr(pipeline_state, "feed_watchdog_during", no_heartbeat)
    monkeypatch.setattr(streaming, "feed_watchdog_during", no_heartbeat)
    monkeypatch.setattr(playback, "feed_watchdog_during", no_heartbeat)
    playback.activate_playback()
    try:
        yield tasks
    finally:
        await playback.close_playback()
        await pipeline_state.clear_all()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        playback.activate_playback()


def _stream_context(
    chunks: AsyncIterator[bytes],
    *,
    sample_rate: int = 48000,
    channels: int = 1,
    sample_format: str = "s16le",
) -> Any:
    """创建 Provider 使用的异步流上下文。"""

    @contextlib.asynccontextmanager
    async def context() -> AsyncIterator[FakePCMStream]:
        yield FakePCMStream(
            chunks,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=sample_format,
        )

    return context()


async def _wait_for_first_write() -> None:
    """在事件循环中等待设备 fake 的首块写入。"""

    await asyncio.wait_for(asyncio.to_thread(FakeOutputStream.first_write.wait), 2)


async def _wait_playback_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    """等待播放根任务及其运行期间创建的接收任务。"""

    awaited = 0
    while awaited < len(tasks):
        current = tasks[awaited:]
        awaited = len(tasks)
        await asyncio.gather(*current, return_exceptions=True)


@pytest.mark.parametrize("arrival", ["before_start", "during_playback"])
async def test_next_reply_infers_and_caches_before_current_playback_finishes(
    arrival: str,
    monkeypatch: pytest.MonkeyPatch,
    pcm_player: AudioPlayer,
    real_pipeline_playback: list[asyncio.Task[Any]],
) -> None:
    """首音后提前处理新弹幕并完整缓存下一轮，保持一轮容量和物理 FIFO。"""

    from plugins.anima_chatter.audio import player as player_module
    from plugins.anima_chatter.chatter import core
    from plugins.anima_chatter.runtime import pipeline_state
    from plugins.anima_chatter.speech.streaming import play_streaming_segments
    from src.core.managers import stream_manager
    from src.core.transport.distribution import stream_loop_manager

    stream_id = "stream-overlap-simulation"
    chatter = core.AnimaChatter(stream_id, SimpleNamespace(config=None))
    timeline: list[str] = []
    pending: list[str] = []
    snapshots: list[list[str]] = []
    resume = asyncio.Event()
    loop_manager = SimpleNamespace(
        _wait_states={stream_id: (None, None, 0)},
        _pending_wait_resume_events={},
    )
    manager = SimpleNamespace(
        _streams={
            stream_id: SimpleNamespace(context=SimpleNamespace(unread_messages=pending))
        }
    )
    idle_checked = asyncio.Event()
    first_request_waiting = asyncio.Event()
    first_pcm_allowed = asyncio.Event()
    current_started = asyncio.Event()
    llm_started = asyncio.Event()
    llm_result_allowed = asyncio.Event()
    next_tts_requested = asyncio.Event()
    next_tts_allowed = asyncio.Event()
    next_started = asyncio.Event()
    gate_calls: asyncio.Queue[None] = asyncio.Queue()
    current_tail_held = threading.Event()
    release_current = threading.Event()

    class HeldOutputStream(FakeOutputStream):
        """阻塞当前输出尾部，同时允许独立接收任务继续合成。"""

        def write(self, block: np.ndarray) -> None:
            if self is FakeOutputStream.instances[0] and self.blocks:
                current_tail_held.set()
                if not release_current.wait(timeout=5):
                    raise TimeoutError("current playback was not released")
            super().write(block)

        def __exit__(self, *_: object) -> None:
            if self is FakeOutputStream.instances[0]:
                timeline.append("current_device_finished")

    original_wake = pipeline_state.wake_stream_from_wait

    def wake(stream: str, *, only_if_new_unreads: bool = True) -> bool:
        """调用真实兼容入口并消费它注入的消息恢复事件。"""

        assert stream == stream_id
        assert only_if_new_unreads is True
        woke = original_wake(stream, only_if_new_unreads=only_if_new_unreads)
        if not woke:
            if not pending:
                idle_checked.set()
            return False
        event = loop_manager._pending_wait_resume_events.pop(stream)
        assert event.source == "message"
        loop_manager._wait_states.pop(stream)
        resume.set()
        return True

    async def fetch_snapshot(
        self: Any, time_format: str = "%H:%M"
    ) -> tuple[str, list[Any]]:
        """在真实容量门之后消费模拟弹幕快照。"""

        snapshots.append(list(pending))
        pending.clear()
        timeline.append(f"unreads_fetched_{len(snapshots)}")
        return "\n".join(snapshots[-1]), []

    original_wait_gate = pipeline_state.wait_gate

    async def observe_gate(stream: str) -> None:
        """记录调用时机但不替换容量准入判断。"""

        gate_calls.put_nowait(None)
        await original_wait_gate(stream)

    async def first_chunks() -> AsyncIterator[bytes]:
        """在首音许可后提供当前回复的两块 PCM。"""

        first_request_waiting.set()
        await first_pcm_allowed.wait()
        yield b"\x01\x00" * 9600

    async def next_chunks() -> AsyncIterator[bytes]:
        """用可控事件模拟下一轮 TTS 推理耗时。"""

        timeline.append("next_tts_requested")
        next_tts_requested.set()
        await next_tts_allowed.wait()
        yield b"\x02\x00" * 9600

    provider = FakePCMProvider(
        lambda request: _stream_context(
            first_chunks() if request.text == "current reply" else next_chunks()
        )
    )

    async def on_current_started(_index: int) -> None:
        """记录真实 PCM 首写回调。"""

        timeline.append("current_started")
        current_started.set()

    async def on_next_started(_index: int) -> None:
        """记录下一轮实际起播，而非接收完成。"""

        timeline.append("next_started")
        next_started.set()

    async def submit(
        text: str, on_started: Any
    ) -> tuple[bool, str]:
        """经真实流式协调器登记并接收语音。"""

        return await play_streaming_segments(
            stream_id=stream_id,
            provider=provider,
            segments=[SpeechSegment(text)],
            tts_params={},
            performer=None,
            audio_player=pcm_player,
            style=PerformanceStyle.create("neutral", "NARRATING"),
            estimated_duration=10.0,
            on_segment_started=on_started,
        )

    async def next_actor_round() -> None:
        """通过真实 Actor 入口拉取弹幕，再模拟 LLM 和 TTS 推理。"""

        await resume.wait()
        text, _messages = await chatter.fetch_unreads()
        assert text == "bulletin A\nbulletin B"
        await chatter.prepare_response_round()
        timeline.append("next_llm_started")
        llm_started.set()
        await llm_result_allowed.wait()
        timeline.append("next_llm_returned")
        result = await submit("next reply", on_next_started)
        assert result[0] is True

    monkeypatch.setattr(player_module.sd, "OutputStream", HeldOutputStream)
    monkeypatch.setattr(stream_manager, "get_stream_manager", lambda: manager)
    monkeypatch.setattr(stream_loop_manager, "get_stream_loop_manager", lambda: loop_manager)
    monkeypatch.setattr(pipeline_state, "wake_stream_from_wait", wake)
    monkeypatch.setattr(pipeline_state, "wait_gate", observe_gate)
    monkeypatch.setattr(core.BaseChatter, "fetch_unreads", fetch_snapshot)
    monkeypatch.setattr(
        core.stream_api, "activate_stream", AsyncMock(return_value=SimpleNamespace(platform="live"))
    )
    actor = asyncio.create_task(next_actor_round())
    third_snapshot: asyncio.Task[Any] | None = None
    try:
        if arrival == "before_start":
            pending.extend(["bulletin A", "bulletin B"])
        await chatter.prepare_response_round()
        await gate_calls.get()
        assert (await submit("current reply", on_current_started))[0] is True
        await asyncio.wait_for(first_request_waiting.wait(), timeout=2)
        assert await pipeline_state.is_gate_pending(stream_id) is True
        assert not llm_started.is_set()
        assert snapshots == []

        first_pcm_allowed.set()
        await asyncio.wait_for(current_started.wait(), timeout=2)
        assert await asyncio.to_thread(current_tail_held.wait, 2)
        if arrival == "during_playback":
            await asyncio.wait_for(idle_checked.wait(), timeout=2)
            assert not llm_started.is_set()
            assert snapshots == []
            pending.extend(["bulletin A", "bulletin B"])
            assert wake(stream_id) is True

        await asyncio.wait_for(llm_started.wait(), timeout=2)
        assert snapshots == [["bulletin A", "bulletin B"]]
        assert "current_device_finished" not in timeline
        assert provider.open_count == 1
        llm_result_allowed.set()
        await asyncio.wait_for(actor, timeout=2)
        await asyncio.wait_for(next_tts_requested.wait(), timeout=2)
        next_tts_allowed.set()
        await asyncio.wait_for(provider.closed.get(), timeout=2)
        await asyncio.wait_for(provider.closed.get(), timeout=2)
        timeline.append("next_pcm_cached")
        assert provider.close_count == 2
        assert provider.max_active == 1
        assert "current_device_finished" not in timeline
        assert not next_started.is_set()
        assert len(FakeOutputStream.instances) == 1

        await gate_calls.get()
        await gate_calls.get()
        pending.append("bulletin C")
        third_snapshot = asyncio.create_task(chatter.fetch_unreads())
        await asyncio.wait_for(gate_calls.get(), timeout=2)
        assert not third_snapshot.done()
        assert len(snapshots) == 1
        assert await pipeline_state.is_gate_pending(stream_id) is True
        release_current.set()
        await asyncio.wait_for(next_started.wait(), timeout=2)
        assert (await asyncio.wait_for(third_snapshot, timeout=2))[0] == "bulletin C"
        await asyncio.wait_for(_wait_playback_tasks(real_pipeline_playback), timeout=2)

        expected = [
            "current_started", "unreads_fetched_1", "next_llm_started",
            "next_llm_returned", "next_tts_requested", "next_pcm_cached",
            "current_device_finished", "next_started", "unreads_fetched_2",
        ]
        assert timeline == expected
        assert len(FakeOutputStream.instances) == 2
        for index, stream in enumerate(FakeOutputStream.instances, start=1):
            pcm = np.concatenate(stream.blocks).reshape(-1)
            assert len(pcm) == 9600
            assert np.all(pcm == index / 32768.0)
        assert pipeline_state._states[stream_id].reservations == {}
    finally:
        first_pcm_allowed.set()
        llm_result_allowed.set()
        next_tts_allowed.set()
        release_current.set()
        actor.cancel()
        if third_snapshot is not None:
            third_snapshot.cancel()
        await asyncio.gather(
            actor, *([third_snapshot] if third_snapshot is not None else []),
            return_exceptions=True,
        )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            ["A.[motion:EXCITED]B.[/motion]", "[emotion:happy]C.[/emotion]D!"],
            [("A.B.\nC.D!", 0.0)],
        ),
        (["A.[wait:2]B."], [("A.", 0.0), ("B.", 2.0)]),
        (["A.[wait:1][wait:2]B.[wait:0.5]"],
         [("A.", 0.0), ("B.", 3.0), ("", 0.5)]),
        (["[wait:0.25]A."], [("A.", 0.25)]),
        (["[motion:EXCITED][/motion]"], []),
    ],
)
def test_action_parses_only_explicit_pauses(
    content: list[str], expected: list[tuple[str, float]]
) -> None:
    """动作与列表项不切 TTS，显式停顿及尾部静音保留。"""

    from plugins.anima_chatter.actions.say_and_perform import SayAndPerformAction

    segments = SayAndPerformAction._parse_all(content)
    assert [(segment.text, segment.wait_before) for segment in segments] == expected
    assert all(segment.motion is None and segment.emotion is None for segment in segments)


# ── 表演参数 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("emotion", "expected_main"),
    [
        ("happy:2", "happy"),
        ("HAPPY:3", "happy"),
        ("neutral", "neutral"),
        ("", "neutral"),
    ],
)
def test_performance_style_extracts_emotion_main(
    emotion: str, expected_main: str
) -> None:
    """表演参数应正确解析出 emotion 主类型，供 expression 匹配兜底。"""

    style = PerformanceStyle.create(emotion, "NARRATING")

    assert style.emotion_main == expected_main
    assert style.emotion == emotion
    assert style.intent == "NARRATING"


# ── 时长估算 ───────────────────────────────────────────────


def test_estimate_duration_scales_with_text_length() -> None:
    """文本越长估算时长越长。"""

    short = estimate_segments_duration([SpeechSegment(text="短")])
    long = estimate_segments_duration(
        [SpeechSegment(text="这是一段明显更长的文本内容")]
    )

    assert long > short


def test_estimate_duration_includes_wait_before() -> None:
    """估算时长应包含段前静默。"""

    without = estimate_segments_duration([SpeechSegment(text="内容")])
    with_wait = estimate_segments_duration(
        [SpeechSegment(text="内容", wait_before=5.0)]
    )

    assert with_wait == pytest.approx(without + 5.0)


def test_estimate_duration_sums_all_segments() -> None:
    """多段的估算时长应为各段之和。"""

    single = estimate_segments_duration([SpeechSegment(text="内容")])
    triple = estimate_segments_duration([SpeechSegment(text="内容")] * 3)

    assert triple == pytest.approx(single * 3)


def test_estimate_duration_of_empty_list_is_zero() -> None:
    """空片段列表估算为 0。"""

    assert estimate_segments_duration([]) == 0.0


# ── TTS 参数合并 ───────────────────────────────────────────


def test_segment_markers_merge_action_params() -> None:
    """Action 顶层参数应被合并进片段 markers。"""

    segment = SpeechSegment(text="内容")
    markers = build_segment_markers(segment, {"style": "cheerful", "speed": 1.2})

    assert markers == {"style": "cheerful", "speed": 1.2}


def test_inline_markers_take_priority() -> None:
    """片段自带的行内标记优先级高于 Action 顶层参数。"""

    segment = SpeechSegment(text="内容", markers={"emotion": "sad"})
    markers = build_segment_markers(segment, {"emotion": "happy", "speed": 1.0})

    assert markers["emotion"] == "sad"
    assert markers["speed"] == 1.0


def test_segment_markers_do_not_mutate_source() -> None:
    """合并不应修改片段自身的 markers。"""

    segment = SpeechSegment(text="内容", markers={"emotion": "sad"})
    build_segment_markers(segment, {"style": "cheerful"})

    assert segment.markers == {"emotion": "sad"}


@pytest.mark.asyncio
async def test_streaming_coordinator_writes_first_chunk_before_provider_eof(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """首块实际写入设备后才允许 Provider 产出后续块并结束。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    events: list[str] = []

    async def chunks() -> AsyncIterator[bytes]:
        yield b"\x01\x00" * 4800
        await _wait_for_first_write()
        events.append("first-write")
        yield b"\x02\x00" * 4800
        events.append("eof")

    provider = FakePCMProvider(lambda _: _stream_context(chunks()))
    result = await asyncio.wait_for(
        play_streaming_segments(
            stream_id="stream-test",
            provider=provider,
            segments=[SpeechSegment("测试", markers={"priority": "high"})],
            tts_params={},
            performer=None,
            audio_player=pcm_player,
            style=PerformanceStyle.create("neutral", "NARRATING"),
            estimated_duration=1.0,
        ),
        timeout=3,
    )

    assert result[0] is True
    await _wait_playback_tasks(isolated_playback)
    assert events == ["first-write", "eof"]
    assert provider.open_count == provider.close_count == 1
    assert provider.requests[0].markers == {"priority": "high"}
    assert len(FakeOutputStream.instances) == 1
    assert sum(len(block) for block in FakeOutputStream.instances[0].blocks) == 9600
    assert isolated_playback


@pytest.mark.asyncio
async def test_vts_starts_in_speaking_session_task_only_after_first_pcm(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """首音回调继承 speaking_session 上下文且与协调器运行在同一任务。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    current_session: ContextVar[asyncio.Task[Any] | None] = ContextVar(
        "test_speaking_session", default=None
    )
    calls: list[str] = []

    class Performer:
        @contextlib.asynccontextmanager
        async def speaking_session(self, **kwargs: Any) -> AsyncIterator[None]:
            task = asyncio.current_task()
            assert task is not None
            token = current_session.set(task)
            calls.append("enter")
            try:
                yield
            finally:
                current_session.reset(token)
                calls.append("exit")

        async def start_speech_playback(self) -> None:
            assert current_session.get() is asyncio.current_task()
            calls.append("start")

        async def switch_segment_intent(self, *args: Any, **kwargs: Any) -> None:
            assert current_session.get() is asyncio.current_task()
            calls.append("switch")

    provider = FakePCMProvider(
        lambda _: _stream_context(_single_chunk(b"\x01\x00" * 300))
    )
    result = await play_streaming_segments(
        stream_id="stream-vts",
        provider=provider,
        segments=[SpeechSegment("测试")],
        tts_params={},
        performer=Performer(),
        audio_player=pcm_player,
        style=PerformanceStyle.create("happy:1", "NARRATING"),
        estimated_duration=1.0,
    )

    assert result[0] is True
    await _wait_playback_tasks(isolated_playback)
    assert calls == ["enter", "start", "switch", "exit"]
    assert isolated_playback


@pytest.mark.asyncio
async def test_empty_pcm_does_not_start_vts(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """没有可播 PCM 时只退出表演会话，不触发 VTS 起播。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    starts: list[bool] = []

    class Performer:
        @contextlib.asynccontextmanager
        async def speaking_session(self, **kwargs: Any) -> AsyncIterator[None]:
            yield

        async def start_speech_playback(self) -> None:
            starts.append(True)

        async def switch_segment_intent(self, *args: Any, **kwargs: Any) -> None:
            return None

    provider = FakePCMProvider(lambda _: _stream_context(_single_chunk(b"")))
    result = await play_streaming_segments(
        stream_id="stream-empty",
        provider=provider,
        segments=[SpeechSegment("空流")],
        tts_params={},
        performer=Performer(),
        audio_player=pcm_player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )

    assert result[0] is True
    await _wait_playback_tasks(isolated_playback)
    assert starts == []
    assert provider.close_count == 1
    assert isolated_playback


@pytest.mark.asyncio
async def test_short_reply_is_accepted_before_provider_and_vts_start(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """短语音立即接受，首块 PCM 实际起播前不触发 VTS 或文本回调。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    provider_requested = asyncio.Event()
    allow_pcm = asyncio.Event()
    vts_calls: list[str] = []
    text_started: list[int] = []

    async def gated_chunks() -> AsyncIterator[bytes]:
        provider_requested.set()
        await asyncio.wait_for(allow_pcm.wait(), timeout=5)
        yield b"\x01\x00" * 300

    class Performer:
        @contextlib.asynccontextmanager
        async def speaking_session(self, **kwargs: Any) -> AsyncIterator[None]:
            yield

        async def start_speech_playback(self) -> None:
            vts_calls.append("start")

        async def switch_segment_intent(self, *args: Any, **kwargs: Any) -> None:
            vts_calls.append("switch")

    provider = FakePCMProvider(lambda _: _stream_context(gated_chunks()))
    result = await play_streaming_segments(
        stream_id="short-accepted",
        provider=provider,
        segments=[SpeechSegment("短回复")],
        tts_params={},
        performer=Performer(),
        audio_player=pcm_player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=0.2,
        on_segment_started=text_started.append,
    )

    assert result[0] is True
    assert provider.open_count == 0
    assert vts_calls == []
    assert text_started == []
    await asyncio.wait_for(provider_requested.wait(), timeout=2)
    assert vts_calls == []
    assert text_started == []
    allow_pcm.set()
    await _wait_playback_tasks(isolated_playback)
    assert vts_calls == ["start", "switch"]
    assert text_started == [0]


@pytest.mark.asyncio
async def test_pcm_receiver_caches_full_reply_while_device_stalls(
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """设备停读不阻止接收当前回复的全部 PCM 或释放 Provider。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    reads = 0
    release_device = asyncio.Event()
    first_read = asyncio.Event()
    frames_written: list[int] = []

    async def many_chunks() -> AsyncIterator[bytes]:
        nonlocal reads
        for _ in range(500):
            reads += 1
            yield b"\x01\x00" * 2400

    class StalledPlayer:
        async def play_pcm_stream(
            self, chunks: AsyncIterator[bytes], **kwargs: Any
        ) -> int:
            first = await anext(chunks)
            await kwargs["on_started"]()
            first_read.set()
            await asyncio.wait_for(release_device.wait(), timeout=5)
            frames = len(first) // 2
            async for chunk in chunks:
                frames += len(chunk) // 2
            frames_written.append(frames)
            return frames

    provider = FakePCMProvider(lambda _: _stream_context(many_chunks()))
    result = await play_streaming_segments(
        stream_id="full-reply-buffer",
        provider=provider,
        segments=[SpeechSegment("大量 PCM")],
        tts_params={},
        performer=None,
        audio_player=StalledPlayer(),
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    assert result[0] is True
    await asyncio.wait_for(first_read.wait(), timeout=2)
    await asyncio.wait_for(provider.closed.get(), timeout=2)
    assert reads == 500
    assert provider.close_count == 1
    assert not isolated_playback[0].done()
    release_device.set()
    await _wait_playback_tasks(isolated_playback)
    assert frames_written == [500 * 2400]


@pytest.mark.asyncio
async def test_refused_reservation_does_not_open_provider(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管线拒绝排队时 Provider 不应启动请求。"""

    from plugins.anima_chatter.runtime import pipeline_state
    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    async def refuse(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(pipeline_state, "reserve", refuse)
    provider = FakePCMProvider(lambda _: _stream_context(_single_chunk(b"\x00\x00")))
    result = await play_streaming_segments(
        stream_id="stream-refused",
        provider=provider,
        segments=[SpeechSegment("未排队")],
        tts_params={},
        performer=None,
        audio_player=pcm_player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )

    assert result[0] is False
    assert provider.open_count == 0
    assert isolated_playback == []


@pytest.mark.asyncio
async def test_pauses_use_sequential_requests_and_single_output(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """停顿分隔的请求顺序生成，音频与定长静音共用一个输出流。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    provider = FakePCMProvider(
        lambda request: _stream_context(_single_chunk(
            (b"\x01\x00" if request.text == "第一段" else b"\x02\x00") * 100
        ))
    )
    result = await play_streaming_segments(
        stream_id="stream-sequential",
        provider=provider,
        segments=[SpeechSegment("第一段"), SpeechSegment("第二段", wait_before=0.25)],
        tts_params={},
        performer=None,
        audio_player=pcm_player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )

    assert result[0] is True
    await _wait_playback_tasks(isolated_playback)
    assert [request.text for request in provider.requests] == ["第一段", "第二段"]
    assert provider.max_active == 1
    assert provider.close_count == 2
    assert len(FakeOutputStream.instances) == 1
    output = np.concatenate(FakeOutputStream.instances[0].blocks)
    restored = np.rint(output * 32768).astype("<i2").tobytes()
    assert restored == b"\x01\x00" * 100 + b"\x00\x00" * 12000 + b"\x02\x00" * 100
    assert isolated_playback


@pytest.mark.asyncio
async def test_cancelled_singing_turn_cannot_overtake_active_voice(
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """后台歌曲取消后，后续语音仍按共享物理 FIFO 等待活动语音。"""

    from plugins.anima_chatter.speech.playback import dispatch_track_pipelined
    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    voice_started = asyncio.Event()
    release_voice = asyncio.Event()
    played: list[str] = []

    class OrderedPlayer:
        async def play_pcm_stream(
            self, chunks: AsyncIterator[bytes], **kwargs: Any
        ) -> int:
            async for chunk in chunks:
                if chunk:
                    voice_started.set()
                    await asyncio.wait_for(release_voice.wait(), timeout=5)
                    played.append("voice")
                    return len(chunk) // 2
            return 0

        async def play_audio(
            self, audio: bytes, *, on_started: Any = None
        ) -> None:
            if on_started is not None:
                await on_started()
            played.append(audio.decode())

    async def held_chunks() -> AsyncIterator[bytes]:
        yield b"\x01\x00"
        await asyncio.wait_for(release_voice.wait(), timeout=5)

    player = OrderedPlayer()
    provider = FakePCMProvider(lambda _: _stream_context(held_chunks()))
    await play_streaming_segments(
        stream_id="stream-fifo-a",
        provider=provider,
        segments=[SpeechSegment("A")],
        tts_params={},
        performer=None,
        audio_player=player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    await asyncio.wait_for(voice_started.wait(), timeout=2)

    task_count = len(isolated_playback)
    song = await dispatch_track_pipelined(
            stream_id="stream-fifo-b",
            audio_bytes=b"song",
            inst_bytes=None,
            audio_player=player,
            performer=None,
            timeline=None,
            pre_delay=0.0,
            song_duration=1.0,
            song_name="B",
    )
    assert song[0] is True
    await play_streaming_segments(
        stream_id="stream-fifo-c",
        provider=provider,
        segments=[SpeechSegment("C")],
        tts_params={},
        performer=None,
        audio_player=player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    song_task = next(
        task for task in isolated_playback[task_count:]
        if ".background_sing." in task.get_name()
    )
    song_task.cancel()

    assert provider.open_count == 1
    assert played == []
    release_voice.set()
    await _wait_playback_tasks(isolated_playback)

    assert provider.open_count == 2
    assert played == ["voice", "voice"]
    assert isolated_playback.reservation_kinds == ["speech", "song", "speech"]


@pytest.mark.asyncio
async def test_partial_provider_error_is_not_retried_and_reserved_turn_released(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部分音频后 Provider 失败不重播，且管线预留项最终完成回收。"""

    from plugins.anima_chatter.runtime import pipeline_state
    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    reports: list[dict[str, Any]] = []

    async def report(*args: Any, **kwargs: Any) -> None:
        reports.append(kwargs)

    monkeypatch.setattr(pipeline_state, "report_actual", report)

    async def broken_chunks() -> AsyncIterator[bytes]:
        yield b"\x01\x00" * 100
        raise RuntimeError("provider interrupted")

    provider = FakePCMProvider(lambda _: _stream_context(broken_chunks()))
    await play_streaming_segments(
        stream_id="stream-partial-error",
        provider=provider,
        segments=[SpeechSegment("部分输出")],
        tts_params={},
        performer=None,
        audio_player=pcm_player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    await asyncio.wait_for(asyncio.gather(*isolated_playback), timeout=3)

    assert provider.open_count == provider.close_count == 1
    assert reports and "finished_at" in reports[-1]


@pytest.mark.asyncio
async def test_invalid_pcm_metadata_does_not_start_vts(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """无效 PCM 元数据在起播回调前失败，不触发 VTS。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    starts: list[bool] = []

    class Performer:
        @contextlib.asynccontextmanager
        async def speaking_session(self, **kwargs: Any) -> AsyncIterator[None]:
            yield

        async def start_speech_playback(self) -> None:
            starts.append(True)

        async def switch_segment_intent(self, *args: Any, **kwargs: Any) -> None:
            return None

    provider = FakePCMProvider(
        lambda _: _stream_context(
            _single_chunk(b"\x01\x00"), sample_rate=24000
        )
    )
    result = await play_streaming_segments(
        stream_id="stream-invalid-metadata",
        provider=provider,
        segments=[SpeechSegment("无效格式")],
        tts_params={},
        performer=Performer(),
        audio_player=pcm_player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )

    assert result[0] is True
    await _wait_playback_tasks(isolated_playback)
    assert starts == []
    assert provider.close_count == 1
    assert isolated_playback and all(task.done() for task in isolated_playback)


@pytest.mark.asyncio
async def test_device_first_write_error_does_not_start_vts_or_callback(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设备首块写入失败时不报告 VTS 起播或片段起播回调。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    starts: list[bool] = []
    callbacks: list[int] = []

    def fail_write(self: FakeOutputStream, block: np.ndarray) -> None:
        raise OSError("device write failed")

    class Performer:
        @contextlib.asynccontextmanager
        async def speaking_session(self, **kwargs: Any) -> AsyncIterator[None]:
            yield

        async def start_speech_playback(self) -> None:
            starts.append(True)

        async def switch_segment_intent(self, *args: Any, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(FakeOutputStream, "write", fail_write)
    provider = FakePCMProvider(
        lambda _: _stream_context(_single_chunk(b"\x01\x00" * 300))
    )
    result = await play_streaming_segments(
        stream_id="device-write-error",
        provider=provider,
        segments=[SpeechSegment("设备失败")],
        tts_params={},
        performer=Performer(),
        audio_player=pcm_player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
        on_segment_started=callbacks.append,
    )

    assert result[0] is True
    await _wait_playback_tasks(isolated_playback)
    assert starts == []
    assert callbacks == []
    assert provider.open_count == provider.close_count == 1
    assert not pcm_player._play_lock.locked()


@pytest.mark.asyncio
async def test_close_playback_waits_for_pcm_writer_and_closes_provider(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """关闭播放会等待 PCM writer 退出并关闭在途 Provider 上下文。"""

    from plugins.anima_chatter.speech.playback import close_playback
    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    source_closed = asyncio.Event()

    async def held_chunks() -> AsyncIterator[bytes]:
        try:
            yield b"\x01\x00" * 4800
            await asyncio.wait_for(asyncio.Event().wait(), timeout=5)
        finally:
            source_closed.set()

    provider = FakePCMProvider(lambda _: _stream_context(held_chunks()))
    await play_streaming_segments(
        stream_id="stream-unload",
        provider=provider,
        segments=[SpeechSegment("卸载期间")],
        tts_params={},
        performer=None,
        audio_player=pcm_player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    await play_streaming_segments(
        stream_id="stream-unload-queued",
        provider=provider,
        segments=[SpeechSegment("排队项")],
        tts_params={},
        performer=None,
        audio_player=pcm_player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    await _wait_for_first_write()
    await asyncio.wait_for(close_playback(), timeout=3)

    assert source_closed.is_set()
    assert provider.open_count == provider.close_count == 1
    assert not any(
        thread.name == "anima-pcm-writer" and thread.is_alive()
        for thread in threading.enumerate()
    )
    assert not pcm_player._play_lock.locked()
    assert all(task.done() for task in isolated_playback)


async def _single_chunk(chunk: bytes) -> AsyncIterator[bytes]:
    """产出单个 PCM 测试块。"""

    yield chunk


async def test_provider_closes_before_device_drains_tail(
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """接收 EOF 后关闭 Provider，设备仍可继续播放已缓冲的尾音。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    draining = asyncio.Event()
    release = asyncio.Event()

    class TailPlayer:
        """读取网络缓冲后保持设备在播状态。"""

        async def play_pcm_stream(
            self, chunks: AsyncIterator[bytes], **kwargs: Any
        ) -> int:
            """排空缓冲，再等待尾音播放完成。"""

            frames = 0
            async for chunk in chunks:
                if not frames:
                    await kwargs["on_started"]()
                frames += len(chunk) // 2
            draining.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return frames

    provider = FakePCMProvider(
        lambda _: _stream_context(_single_chunk(b"\x01\x00" * 4800))
    )
    result = await play_streaming_segments(
        stream_id="tail-test", provider=provider,
        segments=[SpeechSegment("message")], tts_params={}, performer=None,
        audio_player=TailPlayer(),
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    assert result[0] is True
    await asyncio.wait_for(draining.wait(), timeout=2)
    assert provider.close_count == 1
    assert not isolated_playback[0].done()
    release.set()
    release.set()
    await _wait_playback_tasks(isolated_playback)


def test_pcm_service_uses_registered_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """直播通过公开 Service API 获取共享 TTS 服务实例。"""

    from plugins.anima_chatter.speech import backend

    service = FakePCMProvider(lambda _: _stream_context(_single_chunk(b"")))

    def get_service(signature: str) -> SimpleNamespace:
        """仅允许读取 TTS speech service。"""

        assert signature == "tts_voice_plugin-neo:service:speech"
        return service

    monkeypatch.setattr(backend.service_api, "get_service", get_service)
    assert backend.get_tts_service() is service
    assert service.open_count == 0


def test_tts_schema_uses_service_capabilities_without_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动态参数来自 speech service，但流式 schema 不暴露 effects。"""

    from plugins.anima_chatter.actions import _tts_schema

    guide = SimpleNamespace(
        param_type="number",
        description="语速",
        default=1.0,
        valid_values=None,
        min_value=0.5,
        max_value=2.0,
        required=False,
    )
    service = SimpleNamespace(
        get_capabilities=lambda: SimpleNamespace(
            speed_guide=guide,
            effects_guide=guide,
        )
    )

    def get_service(signature: str) -> SimpleNamespace:
        """仅允许读取 TTS speech service。"""

        assert signature == "tts_voice_plugin-neo:service:speech"
        return service

    monkeypatch.setattr(_tts_schema, "get_anima_chatter_plugin", lambda: None)
    monkeypatch.setattr(_tts_schema, "get_service", get_service)
    schema = {
        "function": {"parameters": {"properties": {}, "required": []}}
    }

    assert _tts_schema.inject_tts_params(schema) is schema
    properties = schema["function"]["parameters"]["properties"]
    assert "speed" in properties
    assert "effects" not in properties


async def test_speech_action_opens_stream_only_after_tool_order_gate(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """说话动作先让出工具调用顺序门，再直调 TTS service 输出 PCM。"""

    from plugins.anima_chatter.actions import say_and_perform
    from plugins.anima_chatter.config import AnimaChatterConfig
    from plugins.anima_chatter.plugin import AnimaChatterPlugin
    provider = FakePCMProvider(
        lambda _: _stream_context(_single_chunk(b"\x01\x00" * 300))
    )
    monkeypatch.setattr(say_and_perform, "get_tts_service", lambda: provider)
    config = AnimaChatterConfig()
    plugin: Any = AnimaChatterPlugin(config)
    plugin.audio_player = pcm_player
    action: Any = SimpleNamespace(
        plugin=plugin,
        chat_stream=SimpleNamespace(stream_id="stream-action", platform="live"),
        _parse_all=say_and_perform.SayAndPerformAction._parse_all,
        _send_texts=AsyncMock(),
    )
    generator = say_and_perform.SayAndPerformAction.execute(
        action, ["message"], speed=1.1
    )
    assert await anext(generator) is None
    assert provider.open_count == 0
    action._send_texts.assert_not_awaited()
    assert (await anext(generator))[0] is True
    await generator.aclose()
    await _wait_playback_tasks(isolated_playback)
    assert provider.open_count == provider.close_count == 1
    assert provider.requests[0].markers["speed"] == 1.1
    action._send_texts.assert_awaited_once_with(["message"])


@pytest.mark.asyncio
async def test_speech_action_rejection_does_not_send_text(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """容量拒绝时 Action 返回失败且不向聊天发送未起播文本。"""

    from plugins.anima_chatter.actions import say_and_perform
    from plugins.anima_chatter.config import AnimaChatterConfig
    from plugins.anima_chatter.plugin import AnimaChatterPlugin
    from plugins.anima_chatter.runtime import pipeline_state

    async def refuse(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(pipeline_state, "reserve", refuse)
    provider = FakePCMProvider(lambda _: _stream_context(_single_chunk(b"\x00\x00")))
    monkeypatch.setattr(say_and_perform, "get_tts_service", lambda: provider)
    plugin: Any = AnimaChatterPlugin(AnimaChatterConfig())
    plugin.audio_player = pcm_player
    action: Any = SimpleNamespace(
        plugin=plugin,
        chat_stream=SimpleNamespace(stream_id="rejected-action", platform="live"),
        _parse_all=say_and_perform.SayAndPerformAction._parse_all,
        _send_texts=AsyncMock(),
    )
    generator = say_and_perform.SayAndPerformAction.execute(action, ["拒绝文本"])

    assert await anext(generator) is None
    result = await anext(generator)
    assert result[0] is False
    await generator.aclose()
    action._send_texts.assert_not_awaited()
    assert provider.open_count == 0
    assert isolated_playback == []


@pytest.mark.asyncio
async def test_queued_speech_cancelled_before_start_does_not_open_provider(
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """活动语音后的排队项在起跑前取消，不请求 Provider 且不阻塞后续顺序。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    provider = FakePCMProvider(
        lambda _: _stream_context(_single_chunk(b"\x01\x00" * 100))
    )

    class HeldPlayer:
        async def play_pcm_stream(
            self, chunks: AsyncIterator[bytes], **kwargs: Any
        ) -> int:
            first = await anext(chunks)
            await kwargs["on_started"]()
            first_started.set()
            await asyncio.wait_for(release_first.wait(), timeout=5)
            return len(first) // 2

    player = HeldPlayer()
    first = await play_streaming_segments(
        stream_id="cancel-active",
        provider=provider,
        segments=[SpeechSegment("活动项")],
        tts_params={},
        performer=None,
        audio_player=player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    assert first[0] is True
    await asyncio.wait_for(first_started.wait(), timeout=2)

    task_count = len(isolated_playback)
    queued = await play_streaming_segments(
        stream_id="cancel-queued",
        provider=provider,
        segments=[SpeechSegment("取消项")],
        tts_params={},
        performer=None,
        audio_player=player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    assert queued[0] is True
    queued_root = next(
        task for task in isolated_playback[task_count:]
        if ".pcm_play." in task.get_name()
    )
    queued_root.cancel()
    await asyncio.gather(queued_root, return_exceptions=True)
    assert provider.open_count == 1

    release_first.set()
    await _wait_playback_tasks(isolated_playback)
    third = await play_streaming_segments(
        stream_id="cancel-followup",
        provider=provider,
        segments=[SpeechSegment("后续项")],
        tts_params={},
        performer=None,
        audio_player=player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    assert third[0] is True


@pytest.mark.asyncio
async def test_next_item_prefetches_after_eof_without_overlapping_device_tail(
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """前项 Provider EOF 后可预取下一项，但设备排尾期间不开始下一次写入。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    first_draining = asyncio.Event()
    release_first = asyncio.Event()
    provider = FakePCMProvider(
        lambda _: _stream_context(_single_chunk(b"\x01\x00" * 100))
    )
    device_calls: list[int] = []

    class TailPlayer:
        async def play_pcm_stream(
            self, chunks: AsyncIterator[bytes], **kwargs: Any
        ) -> int:
            device_calls.append(len(device_calls) + 1)
            frames = 0
            async for chunk in chunks:
                if not frames:
                    await kwargs["on_started"]()
                frames += len(chunk) // 2
            if len(device_calls) == 1:
                first_draining.set()
                await asyncio.wait_for(release_first.wait(), timeout=5)
            return frames

    player = TailPlayer()
    first = await play_streaming_segments(
        stream_id="prefetch-first",
        provider=provider,
        segments=[SpeechSegment("第一项")],
        tts_params={},
        performer=None,
        audio_player=player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    assert first[0] is True
    await asyncio.wait_for(first_draining.wait(), timeout=2)
    await asyncio.wait_for(provider_closed(provider, expected=1), timeout=2)
    second = await play_streaming_segments(
        stream_id="prefetch-second",
        provider=provider,
        segments=[SpeechSegment("第二项")],
        tts_params={},
        performer=None,
        audio_player=player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0,
    )
    assert second[0] is True
    await asyncio.wait_for(provider_opened(provider, expected=2), timeout=2)
    assert device_calls == [1]
    release_first.set()
    await _wait_playback_tasks(isolated_playback)
    assert device_calls == [1, 2]


@pytest.mark.asyncio
async def test_action_merges_content_and_sends_only_after_start(
    isolated_playback: list[asyncio.Task[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """列表与表演标记不切请求，整条回复在首次起播后反馈。"""

    from plugins.anima_chatter.actions import say_and_perform
    from plugins.anima_chatter.config import AnimaChatterConfig
    from plugins.anima_chatter.plugin import AnimaChatterPlugin

    device_ready = asyncio.Event()
    release_device = asyncio.Event()

    class DelayedPlayer:
        def __init__(self) -> None:
            self.calls = 0

        async def play_pcm_stream(
            self, chunks: AsyncIterator[bytes], **kwargs: Any
        ) -> int:
            self.calls += 1
            first = await anext(chunks)
            device_ready.set()
            await asyncio.wait_for(release_device.wait(), timeout=5)
            await kwargs["on_started"]()
            frames = len(first) // 2
            async for chunk in chunks:
                frames += len(chunk) // 2
            return frames

    provider = FakePCMProvider(
        lambda _: _stream_context(_single_chunk(b"\x01\x00" * 100))
    )
    monkeypatch.setattr(say_and_perform, "get_tts_service", lambda: provider)
    plugin: Any = AnimaChatterPlugin(AnimaChatterConfig())
    plugin.audio_player = DelayedPlayer()
    action: Any = SimpleNamespace(
        plugin=plugin,
        chat_stream=SimpleNamespace(stream_id="segment-text", platform="live"),
        _parse_all=say_and_perform.SayAndPerformAction._parse_all,
        _send_texts=AsyncMock(),
    )
    generator = say_and_perform.SayAndPerformAction.execute(
        action, ["第一句。第二句！", "[motion:EXCITED]第三句。[/motion]"]
    )
    assert await anext(generator) is None
    assert (await anext(generator))[0] is True
    await generator.aclose()
    await asyncio.wait_for(device_ready.wait(), timeout=2)
    action._send_texts.assert_not_awaited()
    assert [request.text for request in provider.requests] == ["第一句。第二句！\n第三句。"]
    release_device.set()
    await _wait_playback_tasks(isolated_playback)
    action._send_texts.assert_awaited_once_with(["第一句。第二句！\n第三句。"])
    assert plugin.audio_player.calls == 1


@pytest.mark.asyncio
async def test_next_pause_prefetches_before_device_start(
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """前段 EOF 后立即生成下段，不依赖设备起播或停顿开始。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    release_device = asyncio.Event()
    calls: list[str] = []

    class HeldPlayer:
        """保持输出等待，允许后台先接收完整回复。"""

        async def play_pcm_stream(
            self, chunks: AsyncIterator[bytes], **kwargs: Any
        ) -> int:
            await asyncio.wait_for(release_device.wait(), timeout=5)
            frames = 0
            async for chunk in chunks:
                if not frames:
                    calls.append("started")
                    await kwargs["on_started"]()
                frames += len(chunk) // 2
            return frames

    provider = FakePCMProvider(
        lambda _: _stream_context(_single_chunk(b"\x01\x00" * 100))
    )
    result = await play_streaming_segments(
        stream_id="pause-prefetch", provider=provider,
        segments=[SpeechSegment("A"), SpeechSegment("B", wait_before=2.0)],
        tts_params={}, performer=None, audio_player=HeldPlayer(),
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=3.0,
    )
    assert result[0] is True
    await asyncio.wait_for(provider_closed(provider, expected=2), timeout=2)
    assert [request.text for request in provider.requests] == ["A", "B"]
    assert provider.max_active == 1
    assert calls == []
    release_device.set()
    await _wait_playback_tasks(isolated_playback)
    assert calls == ["started"]


@pytest.mark.asyncio
async def test_leading_and_trailing_pauses_preserve_pcm_frames(
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
) -> None:
    """前后静音均输出，前置静音不触发朗读反馈或空文本请求。"""

    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    provider = FakePCMProvider(
        lambda _: _stream_context(_single_chunk(b"\x01\x00" * 100))
    )
    feedback_frames: list[int] = []

    async def on_started(index: int) -> None:
        """记录首次朗读反馈发生时已写入的帧数。"""

        assert index == 0
        feedback_frames.append(sum(len(block) for block in FakeOutputStream.instances[0].blocks))

    result = await play_streaming_segments(
        stream_id="edge-pauses", provider=provider,
        segments=[SpeechSegment("A", wait_before=0.0125), SpeechSegment("", wait_before=0.05)],
        tts_params={}, performer=None, audio_player=pcm_player,
        style=PerformanceStyle.create("neutral", "NARRATING"),
        estimated_duration=1.0, on_segment_started=on_started,
    )
    assert result[0] is True
    await _wait_playback_tasks(isolated_playback)
    assert len(FakeOutputStream.instances) == 1
    assert [request.text for request in provider.requests] == ["A"]
    output = np.concatenate(FakeOutputStream.instances[0].blocks)
    restored = np.rint(output * 32768).astype("<i2").tobytes()
    assert restored == b"\x00\x00" * 600 + b"\x01\x00" * 100 + b"\x00\x00" * 2400
    assert len(feedback_frames) == 1
    assert feedback_frames[0] > 600


async def provider_closed(provider: FakePCMProvider, *, expected: int) -> None:
    """等待指定数量的 Provider 上下文退出。"""

    for _ in range(expected):
        await asyncio.wait_for(provider.closed.get(), timeout=2)


async def provider_opened(provider: FakePCMProvider, *, expected: int) -> None:
    """等待指定数量的 Provider 上下文打开。"""

    for _ in range(expected):
        await asyncio.wait_for(provider.opened.get(), timeout=2)


@pytest.mark.parametrize("song", [False, True])
async def test_cancel_before_playback_task_start_reclaims_reservation(
    song: bool,
    pcm_player: AudioPlayer,
    isolated_playback: list[asyncio.Task[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台任务在首次运行前取消也会释放说话或歌曲占位。"""

    from plugins.anima_chatter.runtime import pipeline_state
    from plugins.anima_chatter.speech.playback import (
        close_playback,
        dispatch_track_pipelined,
    )
    from plugins.anima_chatter.speech.streaming import play_streaming_segments

    relinquish = AsyncMock()
    monkeypatch.setattr(pipeline_state, "relinquish", relinquish)
    provider = FakePCMProvider(lambda _: _stream_context(_single_chunk(b"\x00\x00")))
    if song:
        result = await dispatch_track_pipelined(
            stream_id="stream-before-start",
            audio_bytes=b"song",
            inst_bytes=None,
            audio_player=pcm_player,
            performer=None,
            timeline=None,
            pre_delay=0.0,
            song_duration=1.0,
            song_name="song",
        )
    else:
        result = await play_streaming_segments(
            stream_id="stream-before-start",
            provider=provider,
            segments=[SpeechSegment("message")],
            tts_params={},
            performer=None,
            audio_player=pcm_player,
            style=PerformanceStyle.create("neutral", "NARRATING"),
            estimated_duration=1.0,
        )
    assert result[0] is True
    assert isolated_playback.reservation_kinds == ["song" if song else "speech"]
    isolated_playback[0].cancel()
    await asyncio.wait_for(close_playback(), timeout=2)
    relinquish.assert_awaited_once()
    assert provider.open_count == 0
    assert FakeOutputStream.instances == []


def test_pcm_envelope_waits_for_output_and_resets_for_full_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首音延迟和流式结束不会污染后续歌曲的整轨包络。"""

    from plugins.anima_chatter.audio import envelope

    clock = [10.0]
    monkeypatch.setattr(envelope.time, "monotonic", lambda: clock[0])
    tracker = envelope.EnvelopeTracker()
    tracker.begin_stream(48000)
    clock[0] = 20.0
    tracker.append_stream_pcm(np.ones(4800, dtype=np.float32))
    assert tracker.current().rms > 0
    clock[0] = 30.0
    tracker.append_stream_pcm(np.ones(4800, dtype=np.float32))
    assert tracker.current().rms > 0
    tracker.end()
    tracker.begin([0.6, 0.2])
    assert tracker.current().rms == pytest.approx(0.6)
