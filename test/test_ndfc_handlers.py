"""anima_chatter NDFC 事件转发处理器（ndfc_handlers）的单元测试。

验证各 handler 把 ``neo_default_chatter:*`` 事件转发到绑定的
:class:`AnimaChatter` adapter 方法，并正确写回 NDFC 预填的 payload 字段；
非 anima 接管的 stream 应返回 ``PASS`` 放行 NDFC 默认行为。

区分同步/异步 mock：handler 里 ``create_request`` / ``format_message_line`` /
``add_payload`` / ``_build_negative_behaviors_extra`` 是同步调用，其余
（``sub_agent`` / ``fetch_unreads`` / ``inject_usables`` / ``_build_*_prompt`` /
``_build_enhanced_history_text``）是 ``await`` 的异步调用。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.anima_chatter.chatter import AnimaChatter
from plugins.anima_chatter.chatter.ndfc_handlers import (
    AnimaBuildHistoryTextHandler,
    AnimaCreateRequestHandler,
    AnimaFetchUnreadsHandler,
    AnimaFormatUnreadLineHandler,
    AnimaInjectUnreadPayloadHandler,
    AnimaInjectUsablesHandler,
    AnimaPreprocessHandler,
    _get_anima_chatter,
)
from plugins.anima_chatter.prompts import (
    SPEECH_RULES_TEMPLATE_NAME,
    SYSTEM_PROMPT,
    AnimaChatterPromptBuilder,
)
from plugins.anima_chatter.prompts import builder as prompt_builder
from plugins.anima_chatter.runtime import pipeline_state
from src.app.plugin_system.api import prompt_api
from src.app.plugin_system.types import PromptTemplate
from src.core import prompt as core_prompt
from src.kernel.event import EventDecision


# ── 工具函数 ───────────────────────────────────────────────


def _plugin() -> Any:
    """构造一个插件替身（EventHandler 构造只读 plugin，类型可放宽）。"""
    return SimpleNamespace()


def _make_chatter() -> AnimaChatter:
    """构造一个绑定了 _active_stream 的 AnimaChatter 替身。"""
    chatter = object.__new__(AnimaChatter)
    chatter.stream_id = "stream-1"
    chatter._active_stream = SimpleNamespace(
        context=SimpleNamespace(history_messages=[]),  # type: ignore[attr-defined]
    )
    return chatter


def _patch_get_chatter(chatter: AnimaChatter | None) -> Any:
    """mock chat_api.get_chatter_by_stream 返回指定实例。"""
    return patch(
        "plugins.anima_chatter.chatter.ndfc_handlers.chat_api.get_chatter_by_stream",
        return_value=chatter,
    )


def _payload() -> dict:
    """构造一个最小 NDFC 事件 payload。"""
    return {"stream_id": "stream-1"}


def _message(text: str = "hi") -> SimpleNamespace:
    """构造一个 format_message_line 可消费的消息替身。"""
    return SimpleNamespace(
        time=0,
        sender_role="member",
        sender_id="u1",
        sender_name="Alice",
        sender_cardname="",
        message_id="m1",
        processed_plain_text=text,
        content=text,
        extra={},
    )


# ── _get_anima_chatter ─────────────────────────────────────


@pytest.mark.asyncio
async def test_live_system_prompt_reuses_shared_template_and_preserves_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直播按公共名称渲染规则，每轮读取更新且不混入原 TTS 动作协议。"""

    monkeypatch.setattr(core_prompt.get_prompt_manager(), "_templates", {})
    monkeypatch.setattr(prompt_builder, "get_personality", lambda: SimpleNamespace(nickname="actor"))
    monkeypatch.setattr(prompt_builder, "_detect_active_live_sources", lambda: frozenset())
    template = PromptTemplate(name="anima_chatter_system_prompt", template=SYSTEM_PROMPT)
    for key in (
        "alias_names", "personality_core", "personality_side", "identity", "reply_style",
        "background_story", "safety_guidelines",
    ):
        template.set(key, "")
    prompt_api.register_template(template)
    stream = SimpleNamespace(stream_id="live-stream")
    for word in ("shared speech guidance", "updated speech guidance"):
        prompt_api.register_template(PromptTemplate(
            name=SPEECH_RULES_TEMPLATE_NAME,
            template="<speech_rules>{word}</speech_rules>",
        ).set("word", word))
        rendered = await AnimaChatterPromptBuilder.build_system_prompt(None, stream)
        assert rendered.count("<speech_rules>") == 1
        assert word in rendered
        assert "say_and_perform action" in rendered
        assert "[wait:0.5]" in rendered
        assert not any(name in rendered for name in (
            "tts_segments", "merge_voice", "voice_style", "# 标点规范", "# 多语言准则",
        ))
    assert AnimaChatter.speech_rules_template_name == SPEECH_RULES_TEMPLATE_NAME
    prompt_api.unregister_template(SPEECH_RULES_TEMPLATE_NAME)
    with pytest.raises(RuntimeError, match="公共语音模板未注册"):
        await AnimaChatterPromptBuilder.build_system_prompt(None, stream)


def test_get_anima_chatter_returns_none_when_stream_absent() -> None:
    """流上没有绑定 chatter 时应返回 None。"""

    with _patch_get_chatter(None):
        assert _get_anima_chatter("stream-1") is None


@pytest.mark.parametrize("platform", ["qq", "local_asr", "discord"])
async def test_live_chatter_rejects_non_live_stream(platform: str) -> None:
    """直播控制器拒绝普通聊天和 ASR 流，不再启动三模式会话。"""

    from src.app.plugin_system.base import Failure

    chatter = _make_chatter()
    with patch(
        "plugins.anima_chatter.chatter.core.stream_api.activate_stream",
        AsyncMock(return_value=SimpleNamespace(platform=platform)),
    ), patch("plugins.anima_chatter.chatter.core.get_service") as get_service:
        results = [result async for result in chatter.execute()]

    assert len(results) == 1
    assert isinstance(results[0], Failure)
    assert AnimaChatter.associated_platforms == ["live"]
    get_service.assert_not_called()


def test_get_anima_chatter_returns_none_for_foreign_chatter() -> None:
    """绑定了非 anima chatter（如 NDFC 自身）时应返回 None。"""

    foreign = SimpleNamespace()
    with _patch_get_chatter(foreign):  # type: ignore[arg-type]
        assert _get_anima_chatter("stream-1") is None


def test_get_anima_chatter_returns_chatter_when_bound() -> None:
    """绑定 anima chatter 时应返回该实例。"""

    chatter = _make_chatter()
    with _patch_get_chatter(chatter):
        assert _get_anima_chatter("stream-1") is chatter


def test_get_anima_chatter_returns_none_on_runtime_error() -> None:
    """chat_api 反查抛错时应返回 None（fail-open）。"""

    with patch(
        "plugins.anima_chatter.chatter.ndfc_handlers.chat_api.get_chatter_by_stream",
        side_effect=RuntimeError("not ready"),
    ):
        assert _get_anima_chatter("stream-1") is None


# ── 各 handler 转发 ────────────────────────────────────────


async def test_preprocess_forwards_to_sub_agent_and_proceeds() -> None:
    """注意力决策 should_respond=True 时应写 proceed=True 并 STOP。"""

    chatter = _make_chatter()
    chatter.sub_agent = AsyncMock(
        return_value={"should_respond": True, "reason": "概率直通"}
    )
    params = _payload()
    params["unreads"] = [_message()]
    params["chat_stream"] = SimpleNamespace(context=SimpleNamespace())
    params["history_text"] = "历史"

    handler = AnimaPreprocessHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute("neo_default_chatter:preprocess", params)

    assert decision == EventDecision.STOP
    assert out["proceed"] is True
    assert out["reason"] == "概率直通"
    chatter.sub_agent.assert_awaited_once()


async def test_preprocess_blocks_when_not_respond() -> None:
    """注意力决策 should_respond=False 时应写 proceed=False 并 STOP。"""

    chatter = _make_chatter()
    chatter.sub_agent = AsyncMock(
        return_value={"should_respond": False, "reason": "不值得"}
    )
    params = _payload()
    params["unreads"] = [_message()]
    params["chat_stream"] = SimpleNamespace(context=SimpleNamespace())

    handler = AnimaPreprocessHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute("neo_default_chatter:preprocess", params)

    assert decision == EventDecision.STOP
    assert out["proceed"] is False


async def test_preprocess_fails_open_on_exception() -> None:
    """注意力决策异常时应 proceed=True 放行（避免误拦截）。"""

    chatter = _make_chatter()
    chatter.sub_agent = AsyncMock(side_effect=RuntimeError("llm down"))
    params = _payload()
    params["unreads"] = [_message()]
    params["chat_stream"] = SimpleNamespace(context=SimpleNamespace())

    handler = AnimaPreprocessHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute("neo_default_chatter:preprocess", params)

    assert decision == EventDecision.STOP
    assert out["proceed"] is True


async def test_preprocess_passes_for_non_anima_stream() -> None:
    """非 anima 流应 PASS 放行默认行为。"""

    foreign = SimpleNamespace()
    params = _payload()
    params["unreads"] = [_message()]
    params["chat_stream"] = SimpleNamespace(context=SimpleNamespace())

    handler = AnimaPreprocessHandler(_plugin())
    with _patch_get_chatter(foreign):  # type: ignore[arg-type]
        decision, out = await handler.execute("neo_default_chatter:preprocess", params)

    assert decision == EventDecision.PASS
    assert out is params


async def test_inject_unread_payload_replaces_system_and_injects_user() -> None:
    """应替换 SYSTEM payload 并注入 anima 的 USER prompt。"""

    chatter = _make_chatter()
    chatter._build_system_prompt = AsyncMock(return_value="anima system")
    chatter._build_user_prompt = AsyncMock(return_value="anima user")
    chatter._build_enhanced_history_text = MagicMock(return_value="history")
    chatter._build_negative_behaviors_extra = MagicMock(return_value="")

    payloads = [SimpleNamespace(role="system", content=[])]
    response = SimpleNamespace(payloads=payloads, add_payload=MagicMock())
    params = _payload()
    params["response"] = response
    params["unread_msgs"] = []
    params["formatted_text"] = "formatted"

    handler = AnimaInjectUnreadPayloadHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute(
            "neo_default_chatter:inject_unread_payload", params
        )

    assert decision == EventDecision.STOP
    assert out["skip"] is True
    # SYSTEM payload 内容被替换为 anima system。
    assert len(payloads[0].content) == 1
    assert payloads[0].content[0].text == "anima system"
    response.add_payload.assert_called_once()


async def test_inject_unread_payload_passes_when_no_response() -> None:
    """缺少 response 时应 PASS 放行。"""

    chatter = _make_chatter()
    params = _payload()

    handler = AnimaInjectUnreadPayloadHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute(
            "neo_default_chatter:inject_unread_payload", params
        )

    assert decision == EventDecision.PASS


async def test_inject_usables_forwards_registry() -> None:
    """工具注入应把 ToolRegistry 填入 payload。"""

    chatter = _make_chatter()
    registry = object()
    chatter.inject_usables = AsyncMock(return_value=registry)
    params = _payload()
    params["request"] = object()

    handler = AnimaInjectUsablesHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute(
            "neo_default_chatter:inject_usables", params
        )

    assert decision == EventDecision.STOP
    assert out["tool_registry"] is registry
    chatter.inject_usables.assert_awaited_once_with(params["request"])


async def test_create_request_forwards_request() -> None:
    """请求构造应把 LLMRequest 填入 payload（create_request 是同步调用）。"""

    chatter = _make_chatter()
    request = object()
    chatter.create_request = MagicMock(return_value=request)
    params = _payload()
    params["task_name"] = "actor"
    params["request_name"] = ""
    params["with_reminder"] = "actor"

    handler = AnimaCreateRequestHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute(
            "neo_default_chatter:create_request", params
        )

    assert decision == EventDecision.STOP
    assert out["request"] is request
    chatter.create_request.assert_called_once_with("actor", "", "actor")


async def test_fetch_unreads_forwards_messages() -> None:
    """未读拉取应把 messages 填入 payload。"""

    chatter = _make_chatter()
    msgs = [_message()]
    chatter.fetch_unreads = AsyncMock(return_value=("", msgs))
    params = _payload()

    handler = AnimaFetchUnreadsHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute(
            "neo_default_chatter:fetch_unreads", params
        )

    assert decision == EventDecision.STOP
    assert out["messages"] is msgs


async def test_fetch_unreads_waits_before_base_snapshot() -> None:
    """容量门必须在基类读取 unread_messages 之前通过。"""

    chatter = _make_chatter()
    calls: list[str] = []

    async def wait_gate(stream_id: str) -> None:
        calls.append(f"gate:{stream_id}")

    async def fetch_unreads(
        self: Any, time_format: str = "%H:%M"
    ) -> tuple[str, list[Any]]:
        calls.append("snapshot")
        return "", []

    with patch(
        "plugins.anima_chatter.chatter.core.pipeline_state.wait_gate",
        AsyncMock(side_effect=wait_gate),
    ), patch(
        "plugins.anima_chatter.chatter.core.BaseChatter.fetch_unreads",
        fetch_unreads,
    ):
        await chatter.fetch_unreads()

    assert calls == ["gate:stream-1", "snapshot"]


@pytest.mark.parametrize(
    ("from_phase", "to_phase", "should_prepare"),
    [
        ("wait_user", "model_turn", True),
        ("tool_exec", "follow_up", False),
        ("wait_user", "wait_user", False),
    ],
)
async def test_session_transition_gates_only_new_actor_round(
    from_phase: str, to_phase: str, should_prepare: bool
) -> None:
    """仅进入新 MODEL_TURN 的 phase 事件等待并 reset 流水线轮次。"""

    chatter = _make_chatter()
    chatter.prepare_response_round = AsyncMock()
    params = {
        **_payload(),
        "from_phase": from_phase,
        "to_phase": to_phase,
        "turn_result": None,
    }

    handler = AnimaFetchUnreadsHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute(
            "neo_default_chatter:session_transition", params
        )

    assert decision == EventDecision.PASS
    assert out is params
    if should_prepare:
        chatter.prepare_response_round.assert_awaited_once_with()
    else:
        chatter.prepare_response_round.assert_not_awaited()


async def test_session_transition_seals_empty_tool_round() -> None:
    """纯 Action 轮回到 WAIT_USER 时封存无音频 claim。"""

    params = {
        **_payload(),
        "from_phase": "tool_exec",
        "to_phase": "wait_user",
        "turn_result": None,
    }
    handler = AnimaFetchUnreadsHandler(_plugin())

    with _patch_get_chatter(_make_chatter()), patch.object(
        pipeline_state, "seal_empty_round", AsyncMock()
    ) as seal_empty_round:
        decision, out = await handler.execute(
            "neo_default_chatter:session_transition", params
        )

    assert decision == EventDecision.PASS
    assert out is params
    seal_empty_round.assert_awaited_once_with("stream-1")


async def test_format_unread_line_forwards_formatted_line() -> None:
    """消息格式化应把 formatted_line 填入 payload（同步调用）。"""

    chatter = _make_chatter()
    chatter.format_message_line = MagicMock(return_value="【00:00】Alice：hi")
    msg = _message()
    params = _payload()
    params["message"] = msg
    params["time_format"] = "%H:%M"

    handler = AnimaFormatUnreadLineHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute(
            "neo_default_chatter:format_unread_line", params
        )

    assert decision == EventDecision.STOP
    assert out["formatted_line"] == "【00:00】Alice：hi"
    chatter.format_message_line.assert_called_once_with(msg, "%H:%M")


async def test_build_history_text_forwards_lines() -> None:
    """历史构建应把按行拆分的 list 填入 payload。"""

    chatter = _make_chatter()
    chatter._build_enhanced_history_text = MagicMock(return_value="行1\n行2")
    params = _payload()
    params["chat_stream"] = SimpleNamespace()

    handler = AnimaBuildHistoryTextHandler(_plugin())
    with _patch_get_chatter(chatter):
        decision, out = await handler.execute(
            "neo_default_chatter:build_history_text", params
        )

    assert decision == EventDecision.STOP
    assert out["lines"] == ["行1", "行2"]


async def test_handler_passes_when_stream_not_bound() -> None:
    """未绑定 anima chatter 时所有 handler 应 PASS。"""

    with _patch_get_chatter(None):
        handler = AnimaFetchUnreadsHandler(_plugin())
        decision, out = await handler.execute(
            "neo_default_chatter:fetch_unreads", _payload()
        )
        assert decision == EventDecision.PASS
