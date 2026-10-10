"""PCM 连续播放与增量包络的受控单元测试。"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Self

import numpy as np
import pytest

from plugins.anima_chatter.audio.envelope import EnvelopeTracker
from plugins.anima_chatter.audio.player import AudioPlayer


class FakeOutputStream:
    """记录输出 PCM 的假 sounddevice 流。"""

    instances: list[FakeOutputStream]
    blocks: list[np.ndarray]
    fail_on_write: bool
    fail_next: bool = False
    write_gate: tuple[threading.Event, threading.Event] | None = None
    gate_after_first: bool = False
    closed: threading.Event
    write_entered: threading.Event

    def __init__(self, **kwargs: Any) -> None:
        self.blocks = []
        self.fail_on_write = self.__class__.fail_next
        self.__class__.fail_next = False
        self.write_gate = self.__class__.write_gate
        self.closed = threading.Event()
        self.write_entered = threading.Event()
        self.__class__.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.closed.set()

    def write(self, block: np.ndarray) -> None:
        if self.fail_on_write:
            raise RuntimeError("device write failed")
        should_gate = not self.gate_after_first or bool(self.blocks)
        if self.write_gate is not None and should_gate:
            entered, release = self.write_gate
            self.write_entered.set()
            entered.set()
            release.wait()
        self.blocks.append(block.copy())


@pytest.fixture
def player(monkeypatch: pytest.MonkeyPatch) -> AudioPlayer:
    """构造不访问真实声卡的播放器。"""

    from plugins.anima_chatter.audio import player as player_module

    FakeOutputStream.instances = []
    FakeOutputStream.fail_next = False
    FakeOutputStream.write_gate = None
    FakeOutputStream.gate_after_first = False
    monkeypatch.setattr(player_module.sd, "OutputStream", FakeOutputStream)
    monkeypatch.setattr(
        player_module.sd,
        "query_devices",
        lambda *_args, **_kwargs: {
            "default_samplerate": 1000,
            "max_output_channels": 2,
        },
    )
    result = AudioPlayer(output_device="")
    monkeypatch.setattr(result, "_build_extra_settings", lambda _: None)
    return result


def output_bytes() -> bytes:
    """返回可辨识的 little-endian PCM 测试数据。"""

    return np.arange(1000, dtype="<i2").tobytes()


async def byte_chunks(data: bytes, sizes: list[int]) -> AsyncIterator[bytes]:
    """按指定任意字节长度分块。"""

    offset = 0
    for size in sizes:
        yield data[offset : offset + size]
        offset += size
    if offset < len(data):
        yield data[offset:]


@pytest.mark.asyncio
async def test_first_pcm_is_written_before_upstream_eof(
    player: AudioPlayer,
) -> None:
    """起播回调放行上游后续读取，证明播放器没有等待 EOF。"""

    started = asyncio.Event()
    tail_requested = asyncio.Event()
    payload = output_bytes()

    async def upstream() -> AsyncIterator[bytes]:
        yield payload[:400]
        await tail_requested.wait()
        yield payload[400:]
        tail_requested.set()

    async def on_started() -> None:
        assert FakeOutputStream.instances
        started.set()
        tail_requested.set()

    written = await asyncio.wait_for(
        player.play_pcm_stream(upstream(), sample_rate=1000, on_started=on_started),
        timeout=2,
    )

    assert started.is_set()
    assert written == 1000


@pytest.mark.asyncio
async def test_slow_started_callback_does_not_block_pcm_feed(
    player: AudioPlayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同任务起播反馈等待期间，上游读取和声卡写入继续进行。"""

    callback_entered = asyncio.Event()
    output_complete = threading.Event()
    caller = asyncio.current_task()
    original_write = FakeOutputStream.write
    payload = output_bytes()

    def write(stream: FakeOutputStream, block: np.ndarray) -> None:
        """写完整条测试音频后放行反馈。"""

        original_write(stream, block)
        if sum(len(part) for part in stream.blocks) == 1000:
            output_complete.set()

    monkeypatch.setattr(FakeOutputStream, "write", write)

    async def source() -> AsyncIterator[bytes]:
        """反馈开始后才产出后续 PCM。"""

        yield payload[:200]
        await asyncio.wait_for(callback_entered.wait(), timeout=2)
        yield payload[200:]

    async def on_started() -> None:
        """等待剩余音频写完，验证供音与反馈没有串行依赖。"""

        assert asyncio.current_task() is caller
        callback_entered.set()
        assert await asyncio.to_thread(output_complete.wait, 2)

    written = await player.play_pcm_stream(
        source(), sample_rate=1000, on_started=on_started
    )
    assert written == 1000
    assert len(FakeOutputStream.instances) == 1


@pytest.mark.asyncio
async def test_odd_network_chunks_preserve_all_pcm_bytes(player: AudioPlayer) -> None:
    """任意奇数字节网络边界不得丢弃或重复 PCM 字节。"""

    payload = output_bytes()
    written = await player.play_pcm_stream(
        byte_chunks(payload, [1, 3, 5, 7, 9]), sample_rate=1000
    )

    output = np.concatenate(FakeOutputStream.instances[0].blocks)
    restored = np.rint(output * 32768).astype("<i2").tobytes()
    assert written == 1000
    assert restored == payload
    assert len(FakeOutputStream.instances) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "metadata", "error"),
    [
        ([], {}, ValueError),
        ([b"\x01"], {}, ValueError),
        ([b"\x00\x00"], {"sample_format": "float32"}, ValueError),
        ([b"\x00\x00"], {"sample_rate": 0}, ValueError),
    ],
)
async def test_invalid_or_empty_stream_fails(
    player: AudioPlayer,
    chunks: list[bytes],
    metadata: dict[str, Any],
    error: type[Exception],
) -> None:
    """空流、截断与非法格式均明确失败。"""

    async def source() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    with pytest.raises(error):
        options = {"sample_rate": 1000, **metadata}
        await player.play_pcm_stream(source(), **options)


@pytest.mark.asyncio
async def test_device_error_joins_writer_and_releases_lock(
    player: AudioPlayer,
) -> None:
    """设备写入错误传播，writer 退出且播放器锁可再次获取。"""

    async def source() -> AsyncIterator[bytes]:
        yield output_bytes()

    FakeOutputStream.fail_next = True

    with pytest.raises(RuntimeError, match="PCM output stream failed"):
        await player.play_pcm_stream(source(), sample_rate=1000)

    assert not any(
        thread.name == "anima-pcm-writer" and thread.is_alive()
        for thread in threading.enumerate()
    )
    assert not player._play_lock.locked()


@pytest.mark.asyncio
async def test_cancel_closes_source_joins_writer_and_releases_lock(
    player: AudioPlayer,
) -> None:
    """取消时关闭上游迭代器，等待设备线程退出并释放锁。"""

    closed = asyncio.Event()

    async def source() -> AsyncIterator[bytes]:
        try:
            yield output_bytes()
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(player.play_pcm_stream(source(), sample_rate=1000))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed.is_set()
    assert not player._play_lock.locked()
    assert not any(
        thread.name == "anima-pcm-writer" and thread.is_alive()
        for thread in threading.enumerate()
    )


@pytest.mark.asyncio
async def test_pcm_and_wav_playback_share_the_player_lock(
    player: AudioPlayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PCM 播放占锁期间，整轨 WAV 不得进入设备输出。"""

    from plugins.anima_chatter.audio import player as player_module

    release_source = asyncio.Event()
    pcm_started = asyncio.Event()
    wav_entered: list[bool] = []

    async def source() -> AsyncIterator[bytes]:
        yield output_bytes()
        await release_source.wait()

    async def on_started() -> None:
        pcm_started.set()

    monkeypatch.setattr(
        player_module.sf,
        "read",
        lambda _: (np.ones(10, dtype=np.float32), 1000),
    )
    monkeypatch.setattr(
        player,
        "_play_sync",
        lambda *args: wav_entered.append(True) or args[3]() or True,
    )

    pcm_task = asyncio.create_task(
        player.play_pcm_stream(source(), sample_rate=1000, on_started=on_started)
    )
    await asyncio.wait_for(pcm_started.wait(), timeout=1)
    wav_task = asyncio.create_task(player.play_audio(b"wav"))
    await asyncio.sleep(0.02)
    assert wav_entered == []

    release_source.set()
    await asyncio.gather(pcm_task, wav_task)
    assert wav_entered == [True]


@pytest.mark.asyncio
async def test_wav_started_callback_follows_successful_device_write(
    player: AudioPlayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整轨回调只在首个设备块成功写入后触发。"""

    from plugins.anima_chatter.audio import player as player_module

    monkeypatch.setattr(
        player_module.sf,
        "read",
        lambda _: (np.ones(250, dtype=np.float32), 1000),
    )
    player.loudness_target_dbfs = None
    entered = threading.Event()
    release = threading.Event()
    FakeOutputStream.write_gate = (entered, release)
    FakeOutputStream.gate_after_first = True
    callback_blocks: list[int] = []

    async def on_started() -> None:
        callback_blocks.append(
            sum(len(instance.blocks) for instance in FakeOutputStream.instances)
        )
        release.set()

    await player.play_audio(b"wav", on_started=on_started)

    assert entered.is_set()
    assert callback_blocks == [1]
    assert sum(len(instance.blocks) for instance in FakeOutputStream.instances) == 3


@pytest.mark.asyncio
async def test_wav_device_failure_propagates_without_started_callback(
    player: AudioPlayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首写失败必须传播错误且不得报告实际起播。"""

    from plugins.anima_chatter.audio import player as player_module

    monkeypatch.setattr(
        player_module.sf,
        "read",
        lambda _: (np.ones(250, dtype=np.float32), 1000),
    )
    player.loudness_target_dbfs = None
    FakeOutputStream.fail_next = True
    started = asyncio.Event()

    async def on_started() -> None:
        started.set()

    with pytest.raises(RuntimeError, match="device write failed"):
        await player.play_audio(b"wav", on_started=on_started)

    assert not started.is_set()
    assert not player._play_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_start", [False, True])
async def test_cancel_wav_joins_device_writer_and_releases_lock(
    player: AudioPlayer, monkeypatch: pytest.MonkeyPatch, after_start: bool
) -> None:
    """取消整轨时停止后续块、等待当前设备写完成并释放播放器锁。"""

    from plugins.anima_chatter.audio import player as player_module

    monkeypatch.setattr(
        player_module.sf,
        "read",
        lambda _: (np.ones(1000, dtype=np.float32), 1000),
    )
    player.loudness_target_dbfs = None
    entered = threading.Event()
    release = threading.Event()
    FakeOutputStream.write_gate = (entered, release)
    FakeOutputStream.gate_after_first = after_start
    started = asyncio.Event()
    stop_events: list[threading.Event] = []
    original_play_sync = player._play_sync

    def record_stop_event(
        data: np.ndarray,
        samplerate: int,
        stop_event: threading.Event,
        on_started: Callable[[], None],
    ) -> bool:
        """记录播放器交给设备线程的停止信号。"""

        stop_events.append(stop_event)
        return original_play_sync(data, samplerate, stop_event, on_started)

    monkeypatch.setattr(player, "_play_sync", record_stop_event)

    async def on_started() -> None:
        """记录首写通知已回到调用任务。"""

        started.set()

    task = asyncio.create_task(player.play_audio(b"wav", on_started=on_started))
    assert await asyncio.to_thread(entered.wait, 1)
    if after_start:
        await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    await asyncio.sleep(0)
    completed_early = task.done()
    stop_requested = await asyncio.to_thread(stop_events[0].wait, 1)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stop_requested
    assert not player._play_lock.locked()
    assert not completed_early
    assert FakeOutputStream.instances[0].closed.is_set()
    assert len(FakeOutputStream.instances[0].blocks) == (2 if after_start else 1)


@pytest.mark.asyncio
async def test_dual_started_callback_follows_first_device_write(
    player: AudioPlayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """双轨 callback 在 stream 打开且首块成功写出后触发。"""

    monkeypatch.setattr(
        player,
        "_decode_only",
        lambda _: (np.ones(250, dtype=np.float32), 1000),
    )
    monkeypatch.setattr(player, "_resolve_fallback_device_id", lambda: 1)
    entered = threading.Event()
    release = threading.Event()
    FakeOutputStream.write_gate = (entered, release)
    FakeOutputStream.gate_after_first = True
    callback_blocks: list[int] = []

    async def on_started() -> None:
        assert len(FakeOutputStream.instances) == 2
        callback_blocks.append(
            sum(len(instance.blocks) for instance in FakeOutputStream.instances)
        )
        release.set()

    await player.play_dual(b"vocal", b"inst", on_started=on_started)

    assert entered.is_set()
    assert callback_blocks and callback_blocks[0] >= 1
    assert sum(len(instance.blocks) for instance in FakeOutputStream.instances) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("after_start", [False, True])
async def test_cancel_dual_joins_writers_and_releases_lock(
    player: AudioPlayer, monkeypatch: pytest.MonkeyPatch, after_start: bool
) -> None:
    """取消双轨播放会停写、join 两个 writer 并释放播放器锁。"""

    monkeypatch.setattr(
        player,
        "_decode_only",
        lambda _: (np.ones(1000, dtype=np.float32), 1000),
    )
    monkeypatch.setattr(player, "_resolve_fallback_device_id", lambda: 1)
    entered = threading.Event()
    release = threading.Event()
    FakeOutputStream.write_gate = (entered, release)
    FakeOutputStream.gate_after_first = after_start
    started = asyncio.Event()

    async def on_started() -> None:
        """记录双轨实际起播回调。"""

        started.set()

    task = asyncio.create_task(
        player.play_dual(b"vocal", b"inst", on_started=on_started)
    )
    assert await asyncio.to_thread(entered.wait, 1)
    assert len(FakeOutputStream.instances) == 2
    for instance in FakeOutputStream.instances:
        assert await asyncio.to_thread(instance.write_entered.wait, 1)
    if after_start:
        await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    await asyncio.sleep(0)
    completed_early = task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(FakeOutputStream.instances) == 2
    assert not completed_early
    assert all(instance.closed.is_set() for instance in FakeOutputStream.instances)
    assert all(
        1 <= len(instance.blocks) <= (2 if after_start else 1)
        for instance in FakeOutputStream.instances
    )
    assert not player._play_lock.locked()


def test_streaming_envelope_tracks_appended_output_incrementally() -> None:
    """包络只在追加已输出 PCM 后出现，不需要预计算整段音频。"""

    tracker = EnvelopeTracker()
    tracker.begin_stream(30, hop_seconds=1.0 / 30.0)
    assert tracker.current().rms == 0.0

    tracker.append_stream_pcm(np.ones((1, 1), dtype=np.float32))

    deadline = time.monotonic() + 0.05
    while tracker.current().rms == 0.0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert tracker.current().rms > 0.0
    tracker.end()
    assert tracker.current().rms == 0.0