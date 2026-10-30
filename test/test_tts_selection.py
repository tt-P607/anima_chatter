"""直播 TTS 选择、动态能力和配置边界测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from pydantic import ValidationError
import pytest
from src.app.plugin_system.base import BaseAction

from plugins.anima_chatter.actions import SayAndPerformAction, _tts_schema
from plugins.anima_chatter.chatter import AnimaChatter
from plugins.anima_chatter.config import AnimaChatterConfig, TTSEngine
from plugins.anima_chatter.plugin import AnimaChatterPlugin
from plugins.anima_chatter.speech import backend


def _guide(
    description: str, default: Any = None, *, param_type: str = "string"
) -> SimpleNamespace:
    """构造公共能力协议中的参数指南。"""
    return SimpleNamespace(
        description=description,
        default=default,
        param_type=param_type,
        valid_values=None,
        required=False,
        min_value=None,
        max_value=None,
    )


@pytest.mark.parametrize(
    "engine,signature,template_name",
    [
        (
            "gsv",
            "tts_voice_plugin-neo:service:speech",
            "tts_voice_plugin-neo.speech_rules",
        ),
        (
            "breeze",
            "breeze_tts_plugin:service:speech",
            "breeze_tts_plugin.speech_rules",
        ),
    ],
)
async def test_selected_service_preserves_pcm_contract(
    engine: TTSEngine,
    signature: str,
    template_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两种选择都经公共 Service 获取同一请求结构和 PCM 流契约。"""
    received: list[backend.TTSRequest] = []
    closed: list[bool] = []

    async def chunks() -> AsyncIterator[bytes]:
        """提供一个完整采样的 PCM 音频块。"""
        yield b"\x01\x00"

    @asynccontextmanager
    async def open_pcm_stream(
        request: backend.TTSRequest,
    ) -> AsyncIterator[SimpleNamespace]:
        """记录请求，并确认消费者退出流上下文。"""
        received.append(request)
        try:
            yield SimpleNamespace(
                sample_rate=48000, channels=1, sample_format="s16le", chunks=chunks()
            )
        finally:
            closed.append(True)

    provider = SimpleNamespace(open_pcm_stream=open_pcm_stream)
    lookup = Mock(return_value=provider)
    monkeypatch.setattr(backend.service_api, "get_service", lookup)
    selected = backend.get_tts_service(engine)
    request = backend.TTSRequest("live-stream", "完整说完这句话。")
    async with selected.open_pcm_stream(request) as stream:
        assert (stream.sample_rate, stream.channels, stream.sample_format) == (
            48000,
            1,
            "s16le",
        )
        assert b"".join([chunk async for chunk in stream.chunks]) == b"\x01\x00"
    lookup.assert_called_once_with(signature)
    assert backend.get_speech_rules_template_name(engine) == template_name
    assert received == [request] and closed == [True]


@pytest.mark.parametrize("engine", ["gsv", "breeze"])
def test_missing_selected_service_does_not_try_another_engine(
    engine: TTSEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """选定引擎未加载时只报告该服务缺失，不切换到其他提供方。"""
    lookup = Mock(return_value=None)
    monkeypatch.setattr(backend.service_api, "get_service", lookup)
    with pytest.raises(RuntimeError, match=f"所选 TTS 引擎 {engine}"):
        backend.get_tts_service(engine)
    assert lookup.call_count == 1


@pytest.mark.parametrize("engine", ["other", "../model.gguf", ""])
def test_engine_configuration_rejects_unlisted_values(engine: str) -> None:
    """引擎配置只接受两个约定名称，不接受路径或其他服务签名。"""
    with pytest.raises(ValidationError):
        AnimaChatterConfig.model_validate({"tts": {"engine": engine}})


def test_current_capabilities_change_schema_without_stale_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """切换引擎、模型和音效开关后，下一份 schema 使用当时的实际能力。"""
    config = AnimaChatterConfig()
    plugin = AnimaChatterPlugin(config)
    plugin.tts_capabilities = SimpleNamespace(style_guide=_guide("过期风格"))
    state = {"model": "pure-lertu-s250", "effects": False}
    calls: list[TTSEngine] = []

    def get_tts_service(engine: TTSEngine) -> SimpleNamespace:
        """模拟两种提供方按自身当前配置返回不同能力。"""
        calls.append(engine)

        def get_capabilities() -> SimpleNamespace:
            """每次读取都生成当前参数指南。"""
            if engine == "gsv":
                return SimpleNamespace(
                    style_guide=_guide("GSV 可选风格", "default"),
                    language_guide=_guide("GSV 语言", "zh"),
                    effects_guide=_guide("只用于完整音频的音效", param_type="array"),
                )
            return SimpleNamespace(
                model_guide=_guide("Breeze 模型", state["model"]),
                instruction_guide=_guide("Breeze 自然语言语气", ""),
                streaming_effects_guide=(
                    _guide("Breeze 可用于流式的音效", param_type="array")
                    if state["effects"]
                    else None
                ),
            )

        return SimpleNamespace(get_capabilities=get_capabilities)

    monkeypatch.setattr(_tts_schema, "get_anima_chatter_plugin", lambda: plugin)
    monkeypatch.setattr(_tts_schema, "get_tts_service", get_tts_service)
    gsv = SayAndPerformAction.to_schema()["function"]["parameters"]["properties"]
    assert "GSV" in gsv["style"]["description"]
    assert "language" in gsv and "instruction" not in gsv and "model_id" not in gsv
    assert "audio_effects" not in gsv

    config.tts.engine = "breeze"
    breeze = SayAndPerformAction.to_schema()["function"]["parameters"]["properties"]
    assert "style" not in breeze and "language" not in breeze and "speed" not in breeze
    assert breeze["model_id"]["default"] == "pure-lertu-s250"
    assert "Breeze" in breeze["instruction"]["description"]
    assert "audio_effects" not in breeze

    state["model"] = "pure-aiyuan-s225"
    state["effects"] = True
    updated = SayAndPerformAction.to_schema()["function"]["parameters"]["properties"]
    assert updated["model_id"]["default"] == "pure-aiyuan-s225"
    assert updated["audio_effects"]["items"] == {"type": "object"}

    config.tts.engine = "gsv"
    restored = SayAndPerformAction.to_schema()["function"]["parameters"]["properties"]
    assert "style" in restored and "model_id" not in restored
    assert "audio_effects" not in restored
    assert calls == ["gsv", "breeze", "breeze", "gsv"]


def test_chatter_declares_template_for_current_engine() -> None:
    """公共模板声明与当前配置同步，不给旧引擎留下重复注入入口。"""
    config = AnimaChatterConfig()
    chatter = AnimaChatter("live-stream", AnimaChatterPlugin(config))
    assert chatter.speech_rules_template_name == "tts_voice_plugin-neo.speech_rules"
    config.tts.engine = "breeze"
    assert chatter.speech_rules_template_name == "breeze_tts_plugin.speech_rules"


def test_default_engine_keeps_existing_gsv_behavior() -> None:
    """未设置新增配置项时保留已有直播引擎。"""
    assert AnimaChatterConfig().tts.engine == "gsv"


@pytest.mark.parametrize("foreign_name", ["tts_voice_action", "breeze_tts"])
async def test_live_tool_registry_only_keeps_own_speech_action(
    foreign_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通 TTS 发送动作不进入直播工具表，直播统一走本地播放流程。"""

    class OtherTTSAction(BaseAction):
        """表示普通聊天的 TTS 发送动作。"""

        name = foreign_name

        async def execute(self) -> tuple[bool, str]:
            """返回用于注册表测试的固定回执。"""
            return True, "unused"

    chatter = AnimaChatter("live-stream", AnimaChatterPlugin(AnimaChatterConfig()))
    monkeypatch.setattr(AnimaChatter, "_usables_cache", None)
    monkeypatch.setattr(
        chatter,
        "get_llm_usables",
        AsyncMock(return_value=[OtherTTSAction, SayAndPerformAction]),
    )
    monkeypatch.setattr(
        chatter, "modify_llm_usables", AsyncMock(side_effect=lambda values: values)
    )
    monkeypatch.setattr(_tts_schema, "get_tts_capabilities", lambda: None)
    registry = await chatter.inject_usables(SimpleNamespace(add_payload=Mock()))
    assert registry.get_all_names() == ["action-say_and_perform"]


async def test_cached_registry_reads_current_tts_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实工具注册表复用后，参数说明仍随所选引擎更新。"""
    config = AnimaChatterConfig()
    plugin = AnimaChatterPlugin(config)
    chatter = AnimaChatter("live-stream", plugin)
    monkeypatch.setattr(AnimaChatter, "_usables_cache", None)
    monkeypatch.setattr(
        chatter, "get_llm_usables", AsyncMock(return_value=[SayAndPerformAction])
    )
    monkeypatch.setattr(
        chatter, "modify_llm_usables", AsyncMock(side_effect=lambda values: values)
    )
    monkeypatch.setattr(_tts_schema, "get_anima_chatter_plugin", lambda: plugin)

    def get_tts_service(engine: TTSEngine) -> SimpleNamespace:
        """模拟所选提供方的公开能力形状。"""
        caps = (
            SimpleNamespace(style_guide=_guide("GSV 风格"))
            if engine == "gsv"
            else SimpleNamespace(instruction_guide=_guide("Breeze 语气"))
        )
        return SimpleNamespace(get_capabilities=lambda: caps)

    monkeypatch.setattr(_tts_schema, "get_tts_service", get_tts_service)
    request = SimpleNamespace(add_payload=Mock())
    registry = await chatter.inject_usables(request)
    assert "style" in registry.list_all()[0]["function"]["parameters"]["properties"]
    config.tts.engine = "breeze"
    assert await chatter.inject_usables(request) is registry
    properties = registry.list_all()[0]["function"]["parameters"]["properties"]
    assert "instruction" in properties and "style" not in properties
