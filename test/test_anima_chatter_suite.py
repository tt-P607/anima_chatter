"""Anima 协议桥接、流水线状态与插件卸载测试。"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from plugins.anima_chatter import pipeline_state  # noqa: E402
from plugins.anima_chatter.actions.sing_song import SingSongAction  # noqa: E402
from plugins.anima_chatter.chat_core_bridge import (  # noqa: E402
    AnimaSessionAdapters,
    AnimaSessionOptions,
)
from plugins.anima_chatter.config import AnimaChatterConfig  # noqa: E402
from plugins.anima_chatter.plugin import AnimaChatterPlugin  # noqa: E402


def test_chat_core_bridge_defaults_are_isolated() -> None:
    """会话选项的可变默认值不得在实例之间共享。"""

    first = AnimaSessionOptions()
    second = AnimaSessionOptions()
    first.theme_guide["voice"] = "guide"

    assert second.theme_guide == {}
    assert first.filter_mode == "sub_only"
    assert first.enable_action_suspend is True


def test_chat_core_adapters_preserve_structural_fields() -> None:
    """本地桥接结构应完整保存 chat_core 所需适配器字段。"""

    adapter = object()
    adapters = AnimaSessionAdapters(
        request_adapter=adapter,
        prompt_adapter=adapter,
        unread_adapter=adapter,
        usable_adapter=adapter,
        tool_execution_adapter=adapter,
        sub_agent_adapter=adapter,
        logger_adapter=adapter,
    )

    assert adapters.request_adapter is adapter
    assert adapters.plain_text_adapter is None
    assert adapters.stream_event_observer is None


@pytest.mark.asyncio
async def test_pipeline_gate_resets_and_clear_all_removes_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """达到门槛后应报告待处理，重置轮次与全量清理应清除状态。"""

    await pipeline_state.clear_all()
    pipeline_state.configure(
        pipeline_state.PipelineSettings(
            enabled=True,
            trigger_percent=0.5,
            silence_gap_seconds=0.0,
            silence_gap_jitter=0.0,
            min_duration_seconds=1.0,
            min_remaining_seconds=0.0,
        )
    )
    monkeypatch.setattr(pipeline_state, "_schedule_wakeup_unlocked", lambda *_args: None)

    start_at, finish_at = await pipeline_state.reserve("stream-1", 10.0)

    assert finish_at - start_at == pytest.approx(10.0)
    assert await pipeline_state.is_gate_pending("stream-1") is True

    await pipeline_state.reset_round("stream-1")
    assert await pipeline_state.is_gate_pending("stream-1") is False

    await pipeline_state.reserve("stream-2", 2.0)
    assert pipeline_state._states
    await pipeline_state.clear_all()
    assert pipeline_state._states == {}


@pytest.mark.asyncio
async def test_plugin_unload_releases_runtime_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无活跃通话时卸载应清理流水线、VTS 与运行时缓存。"""

    from plugins.anima_chatter import call_state

    clear_all = AsyncMock()
    monkeypatch.setattr(call_state, "get_active_call", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline_state, "clear_all", clear_all)

    performer = SimpleNamespace(shutdown=AsyncMock())
    plugin = AnimaChatterPlugin(AnimaChatterConfig())
    plugin.vts_performer = performer
    plugin.audio_player = object()  # type: ignore[assignment]
    plugin.song_library = object()
    plugin.tts_capabilities = {"provider": "demo"}

    await plugin.on_plugin_unloaded()

    clear_all.assert_awaited_once_with()
    performer.shutdown.assert_awaited_once_with()
    assert plugin.vts_performer is None
    assert plugin.audio_player is None
    assert plugin.song_library is None
    assert plugin.tts_capabilities is None


def test_singing_component_respects_configuration() -> None:
    """唱歌功能关闭时不应注册 SingSongAction。"""

    enabled_config = AnimaChatterConfig()
    enabled_config.plugin.enable_singing = True
    enabled_plugin = AnimaChatterPlugin(enabled_config)
    assert SingSongAction in enabled_plugin.get_components()

    disabled_config = AnimaChatterConfig()
    disabled_config.plugin.enable_singing = False
    disabled_plugin = AnimaChatterPlugin(disabled_config)
    assert SingSongAction not in disabled_plugin.get_components()
