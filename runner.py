"""voice_chatter 主对话循环。

包含两套 chatter 行为：

- voice 模式（``platform == "local_asr"``）：直通调用 LLM，不做注意力过滤。
- vtb 模式（其他平台）：在 LLM 调用前先跑 :mod:`.sub_agent`（概率门 + sub_actor
  决策），群聊里能丢弃绝大多数无关消息。
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator


from src.core.components.base import Failure, Success, Wait, WaitResumeEvent
from src.core.models.message import Message
from src.core.models.stream import ChatStream
from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolRegistry, ToolResult
from src.kernel.logger import Logger

from . import sub_agent as voice_sub_agent


_PLAIN_TEXT_REMINDER_VOICE = (
    "系统提醒：当前是实时语音通话 Chatter。你必须调用 say action 输出要说的话，"
    "纯文本不会被播放。说完等待用户时，请调用 pass_and_wait。"
)

_PLAIN_TEXT_REMINDER_VTB = (
    "系统提醒：当前是 VTube Studio 虚拟形象互动 Chatter。你必须调用 say_and_perform action "
    "输出要说的话，纯文本不会被发送也不会被朗读。说完等待用户时，请调用 pass_and_wait。"
)

_VOICE_SUSPEND_TEXT = "（本回合已挂起，等待用户继续说话。）"


def _resolve_plain_text_reminder(chat_stream: Any) -> str:
    """根据流的 platform 选择对应模式的纯文本提醒文案。"""

    platform = getattr(chat_stream, "platform", "") or ""
    return _PLAIN_TEXT_REMINDER_VOICE if platform == "local_asr" else _PLAIN_TEXT_REMINDER_VTB


def _append_suspend_payload_if_tool_result_tail(response: Any, logger: Logger) -> None:
    """进入等待前用 assistant 占位闭合尾部工具结果。"""

    payloads = getattr(response, "payloads", None)
    if not payloads or payloads[-1].role != ROLE.TOOL_RESULT:
        return

    response.add_payload(LLMPayload(ROLE.ASSISTANT, Text(_VOICE_SUSPEND_TEXT)))
    logger.debug("已注入语音 SUSPEND 占位符（等待前闭合工具结果）")


def _format_tool_args(args: Any) -> str:
    """格式化工具参数，避免 panel 因异常 args 崩溃。"""

    if args is None:
        return ""
    if isinstance(args, str):
        stripped = args.strip()
        if not stripped:
            return ""
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
        return _format_tool_args(parsed)
    if isinstance(args, dict):
        if not args:
            return ""
        return ", ".join(f"{key}={value!r}" for key, value in args.items())
    return str(args)


def _build_voice_decision_panel(chat_stream: ChatStream, response: Any) -> str:
    """构建语音 Chatter 本次决策摘要面板内容。"""

    stream_name = (
        getattr(chat_stream, "stream_name", "")
        or getattr(chat_stream, "stream_id", "")
        or "未知语音流"
    )
    thought = (
        response.reasoning_content.strip()
        if getattr(response, "reasoning_content", None)
        else "（无）"
    )
    monologue = response.message.strip() if getattr(response, "message", None) else "（无）"

    tool_lines = []
    for call in getattr(response, "call_list", None) or []:
        formatted_args = _format_tool_args(getattr(call, "args", None))
        if formatted_args:
            tool_lines.append(f"    {call.name} ({formatted_args})")
        else:
            tool_lines.append(f"    {call.name}")

    tools_text = "\n".join(tool_lines) if tool_lines else "    （无）"
    return (
        f"语音流名称：{stream_name}\n\n"
        f"思考：{thought}\n\n"
        f"独白：{monologue}\n\n"
        f"调用工具：\n{tools_text}"
    )


def _print_voice_decision_panel(
    chat_stream: ChatStream,
    response: Any,
    logger: Logger,
) -> None:
    """当语音 Chatter 给出 tool call 时打印本次决策摘要。"""

    if not getattr(response, "call_list", None):
        return

    print_panel = getattr(logger, "print_panel", None)
    if callable(print_panel):
        print_panel(
            _build_voice_decision_panel(chat_stream, response),
            title="语音 Chatter 决策",
            border_style="bright_magenta",
        )


async def run_voice_conversation(
    *,
    chatter: Any,
    chat_stream: ChatStream,
    logger: Logger,
    pass_call_name: str,
    plain_text_retry_limit: int,
    enable_action_suspend: bool,
) -> AsyncGenerator[Wait | Success | Failure, WaitResumeEvent | None]:
    """执行语音 Chatter 工具调用流程。"""

    try:
        request = chatter.create_request("actor", with_reminder="actor")
    except (ValueError, KeyError) as error:
        logger.error(f"获取模型配置失败: {error}")
        yield Failure(f"模型配置错误: {error}")
        return

    system_prompt_text = await chatter._build_system_prompt(chat_stream)
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt_text)))
    usable_map = await chatter.inject_usables(request)

    plain_text_retries = 0
    pending_wait_seconds: float | None = None
    resume_event: WaitResumeEvent | None = None

    is_vtb_mode = chat_stream.platform != "local_asr"

    # 从 chatter 读取 sub-agent 配置；缺省时使用模块默认（与 dfc 行为对齐）。
    sub_agent_cfg = None
    if is_vtb_mode:
        plugin_config = getattr(getattr(chatter, "plugin", None), "config", None)
        sub_agent_section = getattr(plugin_config, "sub_agent", None) if plugin_config else None
        if sub_agent_section is not None:
            sub_agent_cfg = voice_sub_agent.SubAgentConfig(
                enabled=bool(sub_agent_section.enabled),
                enable_programmatic_controller=bool(
                    sub_agent_section.enable_programmatic_controller
                ),
            )

    while True:
        _ = resume_event
        _, unread_msgs = await chatter.fetch_unreads()
        if not unread_msgs:
            resume_event = yield Wait()
            continue

        unread_lines = "\n".join(chatter.format_message_line(msg) for msg in unread_msgs)

        # vtb 模式：在调 LLM 之前先做注意力过滤（私聊直通 / 概率门 / sub_actor 判定）。
        # voice 模式（local_asr）：跳过过滤，沿用一对一通话直通行为。
        if is_vtb_mode:
            decision = await voice_sub_agent.should_respond(
                chatter=chatter,
                logger=logger,
                unreads_text=unread_lines,
                unread_msgs=unread_msgs,
                chat_stream=chat_stream,
                config=sub_agent_cfg,
            )
            if not decision["should_respond"]:
                logger.info(
                    f"sub-agent 跳过响应 stream={chat_stream.stream_id} reason={decision['reason']}"
                )
                # 不响应：把未读移入历史（避免下一 tick 重复判定），等待下一批消息。
                await chatter.flush_unreads(unread_msgs)
                resume_event = yield Wait()
                continue
            logger.debug(f"sub-agent 决定响应 reason={decision['reason']}")

        # 每轮都重新构建 history_text：上一轮 bot 自己的回复、被处理过的未读
        # 都已经写进 chat_stream.context.history_messages，模型必须能看到
        # 这部分变化才能保持上下文连续。把构建放在循环外只跑一次会导致
        # 第二轮起每次注入都拿过期快照。
        history_text = chatter._build_history_text(chat_stream)

        user_prompt = await chatter._build_user_prompt(
            chat_stream,
            history_text=history_text,
            unread_lines=unread_lines,
            extra=chatter._build_negative_behaviors_extra(),
        )
        chatter._append_user_payload(request, user_prompt)

        trigger_msg = unread_msgs[-1]
        await chatter.flush_unreads(unread_msgs)

        while True:
            try:
                response = await request.send(stream=False)
                await response
            except Exception as error:
                logger.error(f"LLM 请求失败: {error}", exc_info=True)
                yield Failure("LLM 请求失败", error)
                break

            request = response
            calls = response.call_list or []
            _print_voice_decision_panel(chat_stream, response, logger)
            if not calls:
                message = response.message.strip() if response.message else ""
                if message and plain_text_retries < plain_text_retry_limit:
                    plain_text_retries += 1
                    logger.warning(
                        f"voice_chatter 收到纯文本输出，提醒模型改用 say/say_and_perform: {message[:100]}"
                    )
                    request.add_payload(
                        LLMPayload(ROLE.USER, Text(_resolve_plain_text_reminder(chat_stream)))
                    )
                    continue
                pending_wait_seconds = None
                break

            plain_text_retries = 0
            pending_wait_seconds, has_tool_results, should_wait = await _process_voice_tool_calls(
                chatter=chatter,
                stream_id=chat_stream.stream_id,
                calls=calls,
                response=response,
                usable_map=usable_map,
                trigger_msg=trigger_msg,
                pass_call_name=pass_call_name,
                current_wait_seconds=pending_wait_seconds,
                logger=logger,
            )

            if has_tool_results:
                continue

            action_only_round = bool(calls) and all(
                call.name.startswith("action-") for call in calls
            )
            if action_only_round and not should_wait:
                if enable_action_suspend:
                    _append_suspend_payload_if_tool_result_tail(request, logger)
                    resume_event = yield Wait()
                    break
                continue
            break

        _append_suspend_payload_if_tool_result_tail(request, logger)
        resume_event = yield Wait(time=pending_wait_seconds)


async def _process_voice_tool_calls(
    *,
    chatter: Any,
    stream_id: str,
    calls: list[ToolCall],
    response: Any,
    usable_map: ToolRegistry,
    trigger_msg: Message,
    pass_call_name: str,
    current_wait_seconds: float | None,
    logger: Logger,
) -> tuple[float | None, bool, bool]:
    """处理语音 Chatter 单轮工具调用。"""

    pending_calls: list[ToolCall] = []
    wait_seconds = current_wait_seconds
    has_tool_results = False
    should_wait = False

    async def flush_pending() -> None:
        nonlocal has_tool_results
        if not pending_calls:
            return
        current_pending = list(pending_calls)
        pending_calls.clear()
        results = await chatter.run_tool_call(current_pending, response, usable_map, trigger_msg)
        for pending_call, (appended, success) in zip(current_pending, results, strict=False):
            _ = success
            if appended and not pending_call.name.startswith("action-"):
                has_tool_results = True

    for call in calls:
        args = call.args if isinstance(call.args, dict) else {}
        if call.name == pass_call_name:
            await flush_pending()
            seconds = args.get("seconds")
            wait_seconds = None if seconds is None else float(seconds)
            should_wait = True
            response.add_payload(
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(  # type: ignore[arg-type]
                        value="已登记等待用户继续语音输入" if wait_seconds is None else f"已登记等待 {wait_seconds} 秒后继续",
                        call_id=call.id,
                        name=call.name,
                    ),
                )
            )
            continue
        if call.name == "action-stop_conversation":
            await flush_pending()
            response.add_payload(
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(  # type: ignore[arg-type]
                        value="当前语音 Chatter 不支持 stop_conversation，请改用 pass_and_wait。",
                        call_id=call.id,
                        name=call.name,
                    ),
                )
            )
            logger.warning("模型尝试调用 stop_conversation，已拒绝")
            continue
        pending_calls.append(call)

    await flush_pending()
    return wait_seconds, has_tool_results, should_wait


__all__ = ["run_voice_conversation"]
