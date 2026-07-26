"""插件运行时资源的类型安全视图。

Action / Command / Chatter 拿到的 ``self.plugin`` 静态类型是框架基类，直接访问
``audio_player`` 等本插件专属属性会失去类型检查，而用 ``getattr`` 兜底又会掩盖
"资源未初始化"这类真实问题。

:func:`require_plugin` 做一次运行时校验并返回 :class:`AnimaPlugin` 视图，之后
即可直接属性访问且有完整类型提示。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .config import AnimaChatterConfig

if TYPE_CHECKING:
    from .audio import AudioPlayer
    from .song_library import SongLibrary
    from .vts import VTSPerformer


__all__ = ["AnimaPlugin", "require_plugin"]


# 插件实例必须具备的属性 / 方法，用于运行时校验。
_REQUIRED_MEMBERS = ("audio_player", "song_library", "tts_capabilities", "get_active_performer")


class AnimaPlugin:
    """anima_chatter 插件实例的类型安全视图。

    包装框架给的插件对象，把 ``config`` 收窄为具体配置类型，其余资源属性直接
    代理到被包装对象上（读取的是实时值，不做快照）。
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        """包装插件实例。

        Args:
            inner: anima_chatter 插件对象。
        """

        self._inner = inner

    @property
    def config(self) -> AnimaChatterConfig | None:
        """已校验类型的插件配置；配置加载失败或类型不符时为 ``None``。"""

        config = self._inner.config
        return config if isinstance(config, AnimaChatterConfig) else None

    @property
    def audio_player(self) -> "AudioPlayer | None":
        """本地音频播放器；``on_plugin_loaded`` 之后可用。"""

        return self._inner.audio_player

    @property
    def song_library(self) -> "SongLibrary | None":
        """清唱歌库；唱歌功能关闭时为 ``None``。"""

        return self._inner.song_library

    @property
    def tts_capabilities(self) -> Any | None:
        """TTS Provider 能力元数据；未获取到时为 ``None``。"""

        return self._inner.tts_capabilities

    @tts_capabilities.setter
    def tts_capabilities(self, value: Any) -> None:
        """回写 TTS 能力元数据缓存（懒加载路径使用）。

        Args:
            value: 能力元数据对象。
        """

        self._inner.tts_capabilities = value

    def get_active_performer(self) -> "VTSPerformer | None":
        """返回当前激活的 VTS 表演器；未启用 VTS 时为 ``None``。"""

        return self._inner.get_active_performer()


def require_plugin(plugin: Any) -> AnimaPlugin:
    """把框架给的插件实例包装为类型安全视图。

    Args:
        plugin: 组件持有的 ``self.plugin``。

    Returns:
        插件视图。

    Raises:
        TypeError: 传入对象缺少本插件的必要成员。这只可能发生在组件被错误注册
            到别的插件下，属于装配期错误，应当立即暴露而非静默降级。
    """

    missing = [name for name in _REQUIRED_MEMBERS if not hasattr(plugin, name)]
    if missing:
        raise TypeError(
            f"组件持有的 plugin 不是 anima_chatter 插件实例"
            f"（缺少 {', '.join(missing)}）: {type(plugin).__name__}"
        )
    return AnimaPlugin(plugin)
