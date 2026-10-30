"""Action 共享的 TTS schema 注入工具。

直播语音参数由 TTS Provider 的 ``get_capabilities()`` 动态注入，
仅暴露 PCM 流支持的参数。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from .._internal_compat import get_anima_chatter_plugin
from ..config import AnimaChatterConfig, TTSSection
from ..speech.backend import get_tts_service

logger = get_logger("anima_chatter.action.tts_schema")


__all__ = ["get_tts_capabilities", "inject_tts_params"]


# 注入顺序固定，保证模型每次看到的参数排列一致。
_PARAM_GUIDES = (
    ("model_id", "model_guide"),
    ("instruction", "instruction_guide"),
    ("style", "style_guide"),
    ("language", "language_guide"),
    ("speed", "speed_guide"),
    ("aux_refer_wav_paths", "aux_refer_wav_paths_guide"),
    ("audio_effects", "streaming_effects_guide"),
)


def get_tts_capabilities() -> Any | None:
    """按当前选择重新读取能力，避免引擎或提供方配置变化后沿用旧参数。

    Returns:
        能力元数据对象；Provider 未注册或不提供 capabilities 时返回 ``None``。
    """

    plugin = get_anima_chatter_plugin()
    if plugin is None:
        engine = TTSSection().engine
    else:
        config = plugin.config
        if not isinstance(config, AnimaChatterConfig):
            raise TypeError("直播插件的 TTS 配置类型无效")
        engine = config.tts.engine
    try:
        service = get_tts_service(engine)
    except (RuntimeError, TypeError):
        if plugin is not None:
            plugin.tts_capabilities = None
        return None

    # Service 返回对象遵循动态能力协议，旧提供方可以缺少可选指南。
    get_capabilities = getattr(service, "get_capabilities", None)
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

    for param_name, guide_name in _PARAM_GUIDES:
        # 两个提供方返回不同形状的能力对象，只公开实际存在的指南。
        guide = getattr(caps, guide_name, None)
        if guide is None:
            continue
        properties[param_name] = _guide_to_schema(guide)
        if guide.param_type == "array":
            properties[param_name]["items"] = {
                "type": "object" if param_name == "audio_effects" else "string"
            }
        if guide.required and param_name not in required:
            required.append(param_name)

    return schema
