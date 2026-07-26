"""anima_chatter 的 LLM 请求构造。

支持通过 ``[plugin].models`` 指定自定义模型列表（按顺序 fallback），未指定时
回退到 ``[plugin].model_task`` 对应的任务模型集。同时按 ``BaseChatter`` 的约定
注册全局与流私有两个 SystemReminder bucket。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.app.plugin_system.api.llm_api import (
    get_model_set_by_name,
    get_model_set_by_task,
)
from src.app.plugin_system.types import (
    LLMContextManager,
    LLMRequest,
    ModelSet,
    ReminderSourceSpec,
)

from .._internal_compat import (
    default_context_compression_handler,
    stream_reminder_bucket,
)

if TYPE_CHECKING:
    from ..config import PluginSection


__all__ = ["build_reminder_sources", "create_request", "resolve_model_set"]


def resolve_model_set(section: "PluginSection", fallback_task: str) -> ModelSet:
    """解析本次请求使用的模型集。

    优先用 ``models`` 列出的模型（按顺序拼成 fallback 链）；为空时回退到
    ``model_task``；``model_task`` 也没配时用调用方给的任务名。

    Args:
        section: 插件配置的 ``plugin`` 段。
        fallback_task: 调用方指定的任务名（如 ``"sub_actor"``）。

    Returns:
        模型集。

    Raises:
        ValueError: 配置的模型与任务都解析不到可用模型集。这是配置错误，应当
            立即暴露而非静默降级到默认模型。
    """

    if section.models:
        entries: ModelSet = []
        for model_name in section.models:
            model_set = get_model_set_by_name(
                model_name,
                temperature=section.temperature,
                max_tokens=section.max_tokens,
            )
            if model_set:
                entries.extend(model_set)
        if entries:
            return entries

    task_name = section.model_task or fallback_task
    model_set = get_model_set_by_task(task_name)
    if not model_set:
        raise ValueError(f"无法解析模型集：models 为空且任务 '{task_name}' 未配置")
    return model_set


def build_reminder_sources(
    bucket: str | None,
    stream_id: str,
) -> list[ReminderSourceSpec] | None:
    """构造 SystemReminder 来源列表。

    与 ``BaseChatter.create_request`` 对齐：同时注册全局 bucket 与
    ``stream:{stream_id}:{bucket}`` 流私有 bucket。

    Args:
        bucket: 基础 bucket 名；``None`` 表示不注入提醒。
        stream_id: 当前聊天流 ID。

    Returns:
        来源列表；``bucket`` 为 ``None`` 时返回 ``None``。
    """

    if bucket is None:
        return None

    sources = [ReminderSourceSpec(bucket=bucket, wrap_with_system_tag=True)]
    if stream_id:
        sources.append(
            ReminderSourceSpec(
                bucket=stream_reminder_bucket(stream_id, bucket),
                wrap_with_system_tag=True,
            )
        )
    return sources


def create_request(
    *,
    section: "PluginSection",
    stream_id: str,
    task: str,
    request_name: str,
    with_reminder: str | None,
) -> LLMRequest:
    """构造一次 LLM 请求。

    Args:
        section: 插件配置的 ``plugin`` 段。
        stream_id: 当前聊天流 ID。
        task: 任务名（``models`` 未配置且 ``model_task`` 为空时作为兜底）。
        request_name: 请求名，用于监控与日志。
        with_reminder: 要注入的 SystemReminder bucket；``None`` 表示不注入。

    Returns:
        构造好的请求对象。
    """

    return LLMRequest(
        model_set=resolve_model_set(section, task),
        request_name=request_name,
        meta_data={"stream_id": stream_id},
        context_manager=LLMContextManager(
            context_compression_handler=default_context_compression_handler(),
            reminder_sources=build_reminder_sources(with_reminder, stream_id),
        ),
    )
