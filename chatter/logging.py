"""传给 chat_core 的日志适配器。

chat_core 会把模型输出原样打进 Rich 面板，而模型输出里可能含有 ``[xxx]`` 这类
未闭合的方括号，被 Rich 当作标记解析后会抛异常。本包装器在 ``print_panel`` 时
先转义内容，其余方法原样透传。
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape


__all__ = ["SafeLoggerWrapper"]


class SafeLoggerWrapper:
    """转义面板内容的日志包装器。"""

    def __init__(self, inner: Any) -> None:
        """包装一个已有的 logger。

        Args:
            inner: 被包装的 logger 实例。
        """

        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        """把未显式覆写的属性透传给内部 logger。

        Args:
            name: 属性名。

        Returns:
            内部 logger 上的同名属性。
        """

        return getattr(self._inner, name)

    def print_panel(
        self,
        message: str,
        title: str | None = None,
        border_style: str | None = None,
    ) -> None:
        """转义后输出 Rich 面板。

        转义失败或面板输出失败时降级为普通 info 日志——日志不该让主流程崩。

        Args:
            message: 面板正文（可能含模型输出的方括号）。
            title: 面板标题。
            border_style: 边框样式。
        """

        safe = escape(message) if isinstance(message, str) else message
        try:
            self._inner.print_panel(safe, title=title, border_style=border_style)
        except Exception as exc:  # noqa: BLE001 - 日志输出失败不应影响主流程
            self._inner.info(f"[panel-fallback] {title or ''}\n{safe}")
            self._inner.debug(f"print_panel 输出失败: {exc}")
