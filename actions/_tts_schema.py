"""Action 共享的 TTS schema 注入工具。

``say`` 与 ``say_and_perform`` 都不预设任何 TTS 参数——参数集完全由 TTS Provider
的 ``get_capabilities()`` 定义，在 ``to_schema()`` 时动态注入。这样换 provider
时无需改插件代码。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service

from .._internal_compat import get_anima_chatter_plugin


logger = get_logger("anima_chatter.action.tts_schema")


__all__ = ["get_tts_capabilities", "inject_tts_params"]


_TTS_REGISTRY_SERVICE = "tts_http_server:service:tts_provider_registry"

# 注入顺序固定，保证模型每次看到的参数排列一致。
_PARAM_ORDER = ("style", "language", "speed", "effects", "aux_refer_wav_paths")


def get_tts_capabilities() -> Any | None:
    """获取 TTS Provider 能力元数据（带懒加载兜底）。

    优先读插件缓存；缓存为空时走 service API 现查并回写。``to_schema`` 在 LLM
    请求时被调用，此时 Provider 必然已注册完毕，懒加载一定能拿到。

    Returns:
        能力元数据对象；Provider 未注册或不提供 capabilities 时返回 ``None``。
    """

    plugin = get_anima_chatter_plugin()
    if plugin is not None and plugin.tts_capabilities is not None:
        return plugin.tts_capabilities

    registry = get_service(_TTS_REGISTRY_SERVICE)
    if registry is None:
        return None

    get_provider = getattr(registry, "get_provider", None)
    if not callable(get_provider):
        return None
    provider = get_provider()
    if provider is None:
        return None

    get_capabilities = getattr(provider, "get_capabilities", None)
    caps = get_capabilities() if callable(get_capabilities) else None
    if caps is not None and plugin is not None:
        plugin.tts_capabilities = caps
    return caps


def _guide_to_schema(guide: Any) -> dict[str, Any]:
    """把单个参数指南转成 JSON Schema 条目。

    Args:
        guide: Provider 提供的参数指南对象。

    Returns:
        JSON Schema 属性定义。
    """

    schema: dict[str, Any] = {
        "type": guide.param_type,
        "description": guide.description,
    }
    if guide.default is not None:
        schema["default"] = guide.default
    if guide.valid_values is not None:
        schema["enum"] = guide.valid_values
    if guide.min_value is not None:
        schema["minimum"] = guide.min_value
    if guide.max_value is not None:
        schema["maximum"] = guide.max_value
    return schema


def inject_tts_params(schema: dict[str, Any]) -> dict[str, Any]:
    """把 TTS Provider 的动态参数注入基础 schema。

    Provider 未提供 capabilities 时原样返回（不注入任何 TTS 参数）。

    Args:
        schema: ``BaseAction.to_schema()`` 生成的基础 schema。

    Returns:
        注入 TTS 参数后的 schema（就地修改并返回同一对象）。
    """

    caps = get_tts_capabilities()
    if caps is None:
        return schema

    parameters = schema.get("function", {}).get("parameters", {})
    properties = parameters.setdefault("properties", {})
    required = parameters.setdefault("required", [])

    for param_name in _PARAM_ORDER:
        guide = getattr(caps, f"{param_name}_guide", None)
        if guide is None:
            continue
        properties[param_name] = _guide_to_schema(guide)
        if guide.required and param_name not in required:
            required.append(param_name)

    return schema
