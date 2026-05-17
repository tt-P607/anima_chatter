"""语音 Chatter 的 HTTP TTS 客户端协议实现。"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from src.core.models.message import Message, MessageType
from src.core.models.stream import ChatStream
from src.kernel.logger import Logger

if TYPE_CHECKING:
    from .markers import SpeechSegment


TTS_PROTOCOL_VERSION = "mfx-tts-http-v1"


@dataclass
class TTSRequest:
    """一次 TTS HTTP 合成请求。"""

    stream_id: str
    text: str
    emotion: str | None = None
    markers: dict[str, Any] = field(default_factory=dict)


@dataclass
class TTSArtifact:
    """一次 TTS HTTP 合成产物。"""

    text: str
    audio: bytes | None = None
    mime_type: str = "audio/wav"
    emotion: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class TTSBackend(Protocol):
    """TTS 后端协议。"""

    async def synthesize(self, request: TTSRequest) -> TTSArtifact:
        """把文本合成为可发送给适配器的产物。"""
        ...

    async def emit(self, artifact: TTSArtifact, chat_stream: ChatStream) -> bool:
        """把产物按顺序发送给适配器播放。"""
        ...

class HttpTTSBackend:
    """通过 HTTP 协议请求外部 TTS 服务合成音频。"""

    def __init__(
        self,
        *,
        endpoint: str,
        timeout: float = 30.0,
        mime_type: str = "audio/wav",
        provider: str = "",
    ) -> None:
        """初始化 HTTP TTS 后端。"""

        self.endpoint = endpoint
        self.timeout = timeout
        self.mime_type = mime_type
        self.provider = provider

    async def synthesize(self, request: TTSRequest) -> TTSArtifact:
        """调用 HTTP TTS 合成接口。"""

        import httpx

        options: dict[str, Any] = {
            "mime_type": self.mime_type,
            "provider": self.provider,
        }
        if not self.provider:
            options.pop("provider", None)

        payload = {
            "protocol": TTS_PROTOCOL_VERSION,
            "stream_id": request.stream_id,
            "text": request.text,
            "emotion": request.emotion,
            "markers": request.markers,
            "options": options,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.endpoint, json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                body = response.text.strip()
                message = str(error)
                if body:
                    message = f"{message}; response={body}"
                raise RuntimeError(message) from error
            data = response.json()

        audio_base64 = data.get("audio_base64")
        audio = None
        if isinstance(audio_base64, str) and audio_base64:
            audio = base64.b64decode(audio_base64)

        metadata = {
            "protocol": data.get("protocol", TTS_PROTOCOL_VERSION),
            "format": data.get("format", "wav"),
            "sample_rate": data.get("sample_rate"),
            "duration_ms": data.get("duration_ms"),
            "provider": data.get("provider"),
        }
        metadata.update(data.get("metadata") or {})

        return TTSArtifact(
            text=str(data.get("text") or request.text),
            audio=audio,
            mime_type=str(data.get("mime_type") or self.mime_type),
            emotion=request.emotion,
            metadata=metadata,
        )

    async def emit(self, artifact: TTSArtifact, chat_stream: ChatStream) -> bool:
        """发送 voice 消息给适配器，由适配器完成播放。"""

        from src.core.transport.message_send import get_message_sender

        tts_meta: dict[str, Any] = {
            "backend": "http_tts",
            "mime_type": artifact.mime_type,
            "emotion": artifact.emotion,
            "text": artifact.text,
            **artifact.metadata,
        }

        content: str | dict[str, Any]
        if artifact.audio:
            audio_base64 = base64.b64encode(artifact.audio).decode("ascii")
            content = audio_base64
            tts_meta["audio_base64"] = audio_base64
        else:
            content = {"data": "", "tts": tts_meta}

        message = Message(
            message_id=f"tts_{uuid4().hex}",
            content=content,
            processed_plain_text=artifact.text,
            message_type=MessageType.VOICE,
            platform=chat_stream.platform,
            chat_type=chat_stream.chat_type,
            stream_id=chat_stream.stream_id,
        )
        message.extra["tts"] = tts_meta
        return await get_message_sender().send_message(message)


class LoggingTTSBackend:
    """测试/降级用 TTS 后端。"""

    def __init__(self, logger: Logger, *, mime_type: str = "audio/wav") -> None:
        """初始化日志 TTS 后端。"""

        self.logger = logger
        self.mime_type = mime_type

    async def synthesize(self, request: TTSRequest) -> TTSArtifact:
        """返回仅含文本的产物。"""

        return TTSArtifact(
            text=request.text,
            mime_type=self.mime_type,
            emotion=request.emotion,
            metadata=dict(request.markers),
        )

    async def emit(self, artifact: TTSArtifact, chat_stream: ChatStream) -> bool:
        """记录待播放语音。"""

        self.logger.info(
            f"TTS[{chat_stream.stream_id[:8]}] emotion={artifact.emotion}: {artifact.text}"
        )
        return True


def build_tts_backend(config: Any, logger: Logger) -> TTSBackend:
    """根据配置构建 HTTP TTS 后端。"""

    tts_config = getattr(config, "tts", None)
    endpoint = str(
        getattr(
            tts_config,
            "endpoint",
            "http://127.0.0.1:8000/router/tts_http_server/api/tts/v1/synthesize",
        )
        or "http://127.0.0.1:8000/router/tts_http_server/api/tts/v1/synthesize"
    )
    timeout = float(getattr(tts_config, "timeout", 30.0) or 30.0)
    mime_type = str(getattr(tts_config, "mime_type", "audio/wav") or "audio/wav")
    provider = str(getattr(tts_config, "provider", "qwen_tts") or "")
    if endpoint == "logging":
        return LoggingTTSBackend(logger, mime_type=mime_type)
    return HttpTTSBackend(endpoint=endpoint, timeout=timeout, mime_type=mime_type, provider=provider)


async def _retry_empty_audio(
    *,
    backend: TTSBackend,
    stream_id: str,
    segment: "SpeechSegment",
    artifact: TTSArtifact,
    retry_count: int,
) -> TTSArtifact:
    """TTS 返回空音频时重试，避免单个分段静默丢失。

    本函数被 :class:`SayAction` / :class:`SayAndPerformAction` 直接调用。
    """

    current = artifact
    for _ in range(max(0, int(retry_count or 0))):
        if current.audio:
            return current
        try:
            current = await backend.synthesize(
                TTSRequest(
                    stream_id=stream_id,
                    text=segment.text,
                    emotion=segment.emotion,
                    markers=segment.markers,
                )
            )
        except Exception as error:
            return TTSArtifact(text=segment.text, metadata={"error": str(error)})
    return current


__all__ = [
    "HttpTTSBackend",
    "LoggingTTSBackend",
    "TTSArtifact",
    "TTSBackend",
    "TTS_PROTOCOL_VERSION",
    "TTSRequest",
    "build_tts_backend",
]
