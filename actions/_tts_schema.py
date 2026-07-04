"""anima_chatter Action 共享 TTS schema 工具函数。

为 [`SayAction`](actions/say.py:63) 和
[`SayAndPerformAction`](actions/say_and_perform.py:93) 提供统一的
TTS 能力查询 + 动态 schema 注入逻辑，避免 50 行重复代码。
"""

from __future__ import annotations

from typing import Any


def get_tts_capabilities() -> Any:
    """获取 TTS Provider 能力元数据（带懒加载兜底）。

    优先读插件缓存（``plugin.tts_capabilities``）；缓存为 None 时直接走
    进程内 service API 现查。``to_schema`` 在 LLM 请求时被调用，此时
    TTS Provider 必然已经注册完毕，懒加载一定能拿到。

    Returns:
        :class:`TTSCapabilities` 实例，或 None（provider 未注册 / 无能力）。
    """

    try:
        from .._internal_compat import get_anima_chatter_plugin

        plugin = get_anima_chatter_plugin()
        if plugin is not None:
            cached = getattr(plugin, "tts_capabilities", None)
            if cached is not None:
                return cached

        # 懒加载：插件缓存为空时直接走 service API 现查
        from src.app.plugin_system.api.service_api import get_service

        registry = get_service("tts_http_server:service:tts_provider_registry")
        if registry is None:
            return None
        get_provider_fn = getattr(registry, "get_provider", None)
        if not callable(get_provider_fn):
            return None
        provider: Any = get_provider_fn()
        if provider is None:
            return None
        get_caps_fn = getattr(provider, "get_capabilities", None)
        caps: Any = get_caps_fn() if callable(get_caps_fn) else None
        if caps is not None and plugin is not None:
            plugin.tts_capabilities = caps  # 回写缓存
        return caps
    except Exception:
        return None


def inject_tts_params(schema: dict[str, Any]) -> dict[str, Any]:
    """把 TTS Provider 的动态参数注入到基础 schema。

    从 :func:`get_tts_capabilities` 获取 :class:`TTSCapabilities`，按固定顺序
    （style → language → speed → effects）把每个 :class:`TTSParameterGuide`
    转成 JSON Schema ``properties`` 条目。provider 未提供 capabilities 时直接
    返回原 schema（不注入任何 TTS 参数）。

    Args:
        schema: :meth:`BaseAction.to_schema` 生成的基础 schema（含 content /
            emotion / intent 等已在 execute 签名中显式声明的参数）。

    Returns:
        注入了 TTS 参数后的 schema dict。
    """

    try:
        caps = get_tts_capabilities()
        if caps is None:
            return schema

        params_dict = (
            schema.get("function", {}).get("parameters", {}).get("properties", {})
        )
        required_list = (
            schema.get("function", {}).get("parameters", {}).get("required", [])
        )

        # 从 TTSCapabilities 动态构造所有 TTS 参数
        # 固定注入顺序：style → language → speed → effects
        param_guides = [
            ("style", caps.style_guide),
            ("language", caps.language_guide),
            ("speed", caps.speed_guide),
            ("effects", caps.effects_guide),
        ]

        for param_name, guide in param_guides:
            if guide is None:
                continue

            # 从 TTSParameterGuide 复制所有字段
            param_schema: dict[str, Any] = {
                "type": guide.param_type,
                "description": guide.description,
            }
            if guide.default is not None:
                param_schema["default"] = guide.default
            if guide.valid_values is not None:
                param_schema["enum"] = guide.valid_values
            if guide.min_value is not None:
                param_schema["minimum"] = guide.min_value
            if guide.max_value is not None:
                param_schema["maximum"] = guide.max_value

            params_dict[param_name] = param_schema

            # guide.required=True 时加入 required 列表
            if guide.required and param_name not in required_list:
                required_list.append(param_name)
    except Exception:
        # 任何异常都静默吞掉，返回基础 schema（已有参数不变）
        pass

    return schema


__all__ = ["get_tts_capabilities", "inject_tts_params"]