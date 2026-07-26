"""插件装配、生命周期与注意力过滤的单元测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.anima_chatter.actions import SingSongAction
from plugins.anima_chatter.chatter.attention import (
    compute_bypass_probability,
    mark_reply_success,
    resolve_sub_agent_prompt_source,
)
from plugins.anima_chatter.chatter.session_bridge import (
    AnimaSessionAdapters,
    AnimaSessionOptions,
)
from plugins.anima_chatter.config import AnimaChatterConfig
from plugins.anima_chatter.plugin import AnimaChatterPlugin
from plugins.anima_chatter.protocol import require_plugin
from plugins.anima_chatter.runtime import call_state, pipeline_state, sung_history


# ── 插件装配 ───────────────────────────────────────────────


def test_singing_action_registered_when_enabled() -> None:
    """唱歌功能开启时应注册 sing_song 动作。"""

    config = AnimaChatterConfig()
    config.plugin.enable_singing = True

    assert SingSongAction in AnimaChatterPlugin(config).get_components()


def test_singing_action_absent_when_disabled() -> None:
    """唱歌功能关闭时不应注册 sing_song 动作。"""

    config = AnimaChatterConfig()
    config.plugin.enable_singing = False

    assert SingSongAction not in AnimaChatterPlugin(config).get_components()


def test_no_components_when_plugin_disabled() -> None:
    """插件总开关关闭时不注册任何组件。"""

    config = AnimaChatterConfig()
    config.plugin.enabled = False

    assert AnimaChatterPlugin(config).get_components() == []


def test_plugin_declares_required_metadata() -> None:
    """插件类必须声明规范要求的三个元数据属性。"""

    assert AnimaChatterPlugin.plugin_name == "anima_chatter"
    assert AnimaChatterPlugin.plugin_description
    assert AnimaChatterPlugin.plugin_version


async def test_unload_releases_runtime_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无活跃通话时卸载应清理流水线、VTS 与运行时缓存。"""

    clear_all = AsyncMock()
    monkeypatch.setattr(pipeline_state, "clear_all", clear_all)
    monkeypatch.setattr(call_state, "get_active_call", AsyncMock(return_value=None))

    performer = SimpleNamespace(shutdown=AsyncMock())
    plugin = AnimaChatterPlugin(AnimaChatterConfig())
    plugin.vts_performer = performer  # type: ignore[assignment]
    plugin.audio_player = object()  # type: ignore[assignment]
    plugin.song_library = object()  # type: ignore[assignment]
    plugin.tts_capabilities = {"provider": "demo"}

    await plugin.on_plugin_unloaded()

    clear_all.assert_awaited_once_with()
    performer.shutdown.assert_awaited_once_with()
    assert plugin.vts_performer is None
    assert plugin.audio_player is None
    assert plugin.song_library is None
    assert plugin.tts_capabilities is None


async def test_unload_clears_sung_history() -> None:
    """卸载应清空唱歌历史，避免重载后残留。"""

    await sung_history.record("某首歌")
    assert sung_history.format_recent_block() != ""

    await AnimaChatterPlugin(AnimaChatterConfig()).on_plugin_unloaded()

    assert sung_history.format_recent_block() == ""


# ── 插件视图 ───────────────────────────────────────────────


def test_require_plugin_rejects_foreign_object() -> None:
    """不符合本插件形状的对象应被立即拒绝。"""

    with pytest.raises(TypeError, match="不是 anima_chatter 插件实例"):
        require_plugin(object())


def test_require_plugin_narrows_config_type() -> None:
    """插件视图应把 config 收窄为本插件的配置类型。"""

    config = AnimaChatterConfig()
    view = require_plugin(AnimaChatterPlugin(config))

    assert view.config is config
    assert view.audio_player is None
    assert view.song_library is None


# ── 会话桥接 ───────────────────────────────────────────────


def test_session_options_defaults_are_isolated() -> None:
    """可变默认值不得在实例之间共享。"""

    first = AnimaSessionOptions()
    second = AnimaSessionOptions()
    first.theme_guide["voice"] = "guide"

    assert second.theme_guide == {}


def test_session_options_disable_unused_features() -> None:
    """anima 不需要的会话特性应默认关闭。"""

    options = AnimaSessionOptions()

    assert options.enable_cooldown is False
    assert options.enable_sub_agent_collaboration is False
    assert options.native_multimodal is False
    assert options.negative_behavior_reinforcement is False


def test_session_adapters_preserve_fields() -> None:
    """适配器集合应完整保存传入的各个适配器。"""

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


# ── 注意力过滤 ─────────────────────────────────────────────


def _chat_stream(bot_nickname: str = "小助手") -> SimpleNamespace:
    """构造带独立 context 的聊天流替身。

    Args:
        bot_nickname: bot 在该平台的昵称。

    Returns:
        聊天流替身对象。
    """

    return SimpleNamespace(bot_nickname=bot_nickname, context=SimpleNamespace())


def _message(text: str) -> SimpleNamespace:
    """构造消息替身。

    Args:
        text: 消息正文。

    Returns:
        消息替身对象。
    """

    return SimpleNamespace(processed_plain_text=text, content=text)


def test_bypass_probability_grows_with_unread_count() -> None:
    """未读消息越多，直通概率越高。"""

    stream = _chat_stream()
    few, _ = compute_bypass_probability([_message("在吗")], stream)
    many, _ = compute_bypass_probability([_message("在吗")] * 5, _chat_stream())

    assert many > few


def test_bypass_probability_is_capped_at_one() -> None:
    """概率上限为 1.0，且理由里会标注封顶。"""

    probability, reason = compute_bypass_probability(
        [_message("小助手在吗")] * 30, _chat_stream()
    )

    assert probability == 1.0
    assert "封顶" in reason


def test_reply_bonus_applies_once() -> None:
    """"刚回复"加成只在下一次计算时生效一次。"""

    stream = _chat_stream()
    baseline, _ = compute_bypass_probability([_message("嗯")], stream)

    mark_reply_success(stream)  # type: ignore[arg-type]
    boosted, reason = compute_bypass_probability([_message("嗯")], stream)
    after, _ = compute_bypass_probability([_message("嗯")], stream)

    assert boosted > baseline
    assert "刚回复" in reason
    assert after == pytest.approx(baseline)


@pytest.mark.parametrize(
    ("mode", "expected_suffix"),
    [("vtb", "_vtb"), ("vtb_live", "_vtb_live")],
)
def test_sub_agent_prompt_source_matches_mode(
    mode: str, expected_suffix: str
) -> None:
    """决策 prompt 应按模式选择对应模板。"""

    template_name, fallback = resolve_sub_agent_prompt_source(mode)  # type: ignore[arg-type]

    assert template_name.endswith(expected_suffix)
    assert fallback
