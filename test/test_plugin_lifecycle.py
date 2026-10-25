"""插件装配、生命周期与注意力过滤的单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from plugins.anima_chatter.actions import ControlBodyAction, SingSongAction
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
from plugins.anima_chatter.prompts import scenes
from plugins.anima_chatter.protocol import require_plugin
from plugins.anima_chatter.runtime import pipeline_state, sung_history

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


def test_live_plugin_registers_only_live_components() -> None:
    """直播插件不注册通话动作或普通聊天测试命令。"""

    components = AnimaChatterPlugin(AnimaChatterConfig()).get_components()
    names = {component.name for component in components}

    assert {
        "anima_chatter",
        "say_and_perform",
        "control_body",
        "sing_song",
        "pass_and_wait",
    } <= names
    assert not {"say", "start_voice_call", "end_voice_call", "vtb", "voice"} & names
    assert cast(Any, AnimaChatterPlugin).dependent_components == []


async def test_control_body_is_visible_through_chatter_tool_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """身体控制 Action 经 chatter 生产过滤路径进入真实 ToolRegistry。"""

    from plugins.anima_chatter.chatter.core import AnimaChatter
    from src.app.plugin_system.api import action_api, agent_api, tool_api
    from src.core import managers
    from src.kernel.llm.payload.tooling import ToolRegistry

    chatter = AnimaChatter(
        "body-tool-registry", AnimaChatterPlugin(AnimaChatterConfig())
    )
    monkeypatch.setattr(AnimaChatter, "_usables_cache", None)
    monkeypatch.setattr(
        chatter, "get_llm_usables", AsyncMock(return_value=[ControlBodyAction])
    )

    async def filter_actions(actions: list[type], **kwargs: Any) -> list[type]:
        """隔离动作公共 API，同时校验 chatter 上下文。"""

        assert actions == [ControlBodyAction]
        assert kwargs["chatter_name"] == "anima_chatter"
        return actions

    async def filter_empty(components: list[type], **kwargs: Any) -> list[type]:
        """隔离没有候选项的 Agent 与 Tool API。"""

        assert components == []
        return []

    stream_manager = SimpleNamespace(
        get_or_create_stream=AsyncMock(return_value=SimpleNamespace(chat_type="all"))
    )
    monkeypatch.setattr(managers, "get_stream_manager", lambda: stream_manager)
    monkeypatch.setattr(action_api, "filter_actions", filter_actions)
    monkeypatch.setattr(agent_api, "filter_agents", filter_empty)
    monkeypatch.setattr(tool_api, "filter_tools", filter_empty)
    payloads: list[Any] = []
    request = SimpleNamespace(add_payload=payloads.append)

    registry = await chatter.inject_usables(request)

    assert isinstance(registry, ToolRegistry)
    stream_manager.get_or_create_stream.assert_awaited_once_with(
        stream_id="body-tool-registry"
    )
    tool_name = ControlBodyAction.to_schema()["function"]["name"]
    assert tool_name.endswith("control_body")
    assert registry.get(tool_name) is ControlBodyAction
    assert registry.get_all_names() == [tool_name]
    assert "body" in registry.list_all()[0]["function"]["parameters"]["properties"]
    assert len(payloads) == 1


def test_body_action_schema_keeps_definitions_at_parameters_root() -> None:
    """ControlBodyAction 的嵌套引用可从 parameters 根解析。"""

    parameters = ControlBodyAction.to_schema()["function"]["parameters"]
    assert "body" in parameters["properties"]
    assert parameters["required"] == ["body"]
    definitions = parameters["$defs"]
    references: list[str] = []

    def collect_references(value: Any) -> None:
        """收集参数 schema 中的 JSON Schema 引用。"""

        if isinstance(value, dict):
            reference = value.get("$ref")
            if reference is not None:
                references.append(reference)
            for child in value.values():
                collect_references(child)
        elif isinstance(value, list):
            for child in value:
                collect_references(child)

    collect_references(parameters)
    assert references
    assert all(
        reference.startswith("#/$defs/")
        and reference.removeprefix("#/$defs/").split("/")[0] in definitions
        for reference in references
    )


async def test_control_body_rejects_unready_performer() -> None:
    """VTS 未连接时身体控制返回明确失败且不调用 performer。"""

    performer = SimpleNamespace(is_ready=False, apply_body_control=AsyncMock())
    plugin = AnimaChatterPlugin(AnimaChatterConfig())
    plugin.vts_performer = performer
    action: Any = SimpleNamespace(plugin=plugin)

    success, message = await ControlBodyAction.execute(
        action, body={"left_eye": {"openness": 0.5}}
    )

    assert success is False
    assert "未就绪" in message
    performer.apply_body_control.assert_not_awaited()


def test_live_scene_exports_have_no_removed_mode_templates() -> None:
    """场景公开入口只导出存在的直播能力。"""

    assert all(hasattr(scenes, name) for name in scenes.__all__)
    assert not {"VOICE_SCENE_GUIDE", "VTB_SCENE_GUIDE"} & set(scenes.__all__)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝组件清单中的重复 JSON 键。"""

    result: dict[str, Any] = {}
    for name, value in pairs:
        assert name not in result, f"重复的 manifest 键：{name}"
        result[name] = value
    return result


def test_plugin_declares_required_metadata() -> None:
    """插件身份与 manifest 一致，版本和描述以 manifest 为准。"""

    manifest_path = Path(__file__).resolve().parents[1] / "manifest.json"
    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
    )

    assert manifest["name"] == AnimaChatterPlugin.plugin_name == "anima_chatter"
    assert manifest["description"]
    assert manifest["version"]
    registered_names = {
        component.name
        for component in AnimaChatterPlugin(AnimaChatterConfig()).get_components()
    }
    assert {entry["component_name"] for entry in manifest["include"]} == registered_names
    assert "asr_adapter_anima" not in manifest["dependencies"]["plugins"]
    assert "tts_http_server" not in manifest["dependencies"]["plugins"]
    assert "tts_voice_plugin-neo" in manifest["dependencies"]["plugins"]
    assert "tts_voice_plugin-neo:service:speech" in manifest["dependencies"]["components"]


async def test_unload_releases_runtime_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """卸载仅清理本插件流水线、VTS 与运行时缓存。"""

    clear_all = AsyncMock()
    monkeypatch.setattr(pipeline_state, "clear_all", clear_all)

    performer = SimpleNamespace(shutdown=AsyncMock())
    plugin: Any = AnimaChatterPlugin(AnimaChatterConfig())
    plugin.vts_performer = performer
    plugin.audio_player = object()
    plugin.song_library = object()
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
    """ "刚回复"加成只在下一次计算时生效一次。"""

    stream = _chat_stream()
    baseline, _ = compute_bypass_probability([_message("嗯")], stream)

    mark_reply_success(stream)  # type: ignore[arg-type]
    boosted, reason = compute_bypass_probability([_message("嗯")], stream)
    after, _ = compute_bypass_probability([_message("嗯")], stream)

    assert boosted > baseline
    assert "刚回复" in reason
    assert after == pytest.approx(baseline)


def test_sub_agent_prompt_source_is_live_only() -> None:
    """注意力决策只使用直播模板。"""

    template_name, fallback = resolve_sub_agent_prompt_source()

    assert template_name.endswith("_vtb_live")
    assert fallback
