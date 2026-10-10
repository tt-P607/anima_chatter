"""TTS speech service 的请求、PCM 流与本地协议定义。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from src.app.plugin_system.api import service_api


@dataclass(slots=True)
class TTSRequest:
    """一次 TTS 合成请求。

    Attributes:
        stream_id: 请求所属聊天流。
        text: 待合成文本。
        emotion: 情绪标记；``None`` 表示不指定。
        markers: 传递给 TTS service 的参数标记。
        options: 传递给 TTS service 的附加选项。
    """

    stream_id: str
    text: str
    emotion: str | None = None
    markers: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


class PCMStream(Protocol):
    """TTS service 返回的 PCM 格式元数据与异步数据块。"""

    sample_rate: int
    channels: int
    sample_format: str
    chunks: AsyncIterator[bytes]


class TTSSynthesisResult(Protocol):
    """完整音频合成的服务响应。"""

    audio_base64: str
    mime_type: str
    text: str
    format: str


class TTSService(Protocol):
    """TTS speech service 的进程内接口。"""

    def get_capabilities(self) -> Any:
        """返回服务支持的合成参数说明。"""
        ...

    async def synthesize(self, request: TTSRequest) -> TTSSynthesisResult:
        """生成完整音频。"""
        ...

    def open_pcm_stream(
        self, request: TTSRequest
    ) -> AbstractAsyncContextManager[PCMStream]:
        """打开 PCM 流，退出上下文时结束本次请求。"""
        ...


def get_tts_service() -> TTSService:
    """通过公开 Service API 获取 TTS 插件提供的语音合成服务。"""

    service = service_api.get_service("tts_voice_plugin-neo:service:speech")
    if service is None:
        raise RuntimeError("TTS 语音合成服务未加载")
    if not callable(getattr(service, "open_pcm_stream", None)):
        raise TypeError("TTS 语音合成服务不支持 PCM 流式合成")
    return cast(TTSService, service)
