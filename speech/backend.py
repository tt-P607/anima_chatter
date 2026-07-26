"""TTS HTTP 后端客户端。

把文本送到 ``tts_http_server`` 提供的合成接口，拿回音频 bytes。voice 模式下
还需要把音频作为 voice 消息发给适配器播放（:meth:`TTSBackend.emit`）；vtb 系
模式直接用本地 AudioPlayer 播，不走 emit。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from src.app.plugin_system.api import send_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import ChatStream, Message, MessageType

if TYPE_CHECKING:
    from ..config import TTSSection


__all__ = [
    "HttpTTSBackend",
    "TTSArtifact",
    "TTSBackend",
    "TTS_PROTOCOL_VERSION",
    "TTSRequest",
    "build_tts_backend",
    "retry_empty_audio",
]


logger = get_logger("anima_chatter.speech.backend")


TTS_PROTOCOL_VERSION = "mfx-tts-http-v1"
"""与 ``tts_http_server`` 约定的合成协议版本。"""


@dataclass(slots=True)
class TTSRequest:
    """一次 TTS 合成请求。

    Attributes:
        stream_id: 请求所属聊天流，供服务端做日志关联。
        text: 待合成文本。
        emotion: 情绪标记（``"类型:强度"``）；``None`` 表示不指定。
        markers: 透传给 provider 的参数（style / language / speed / effects 等）。
    """

    stream_id: str
    text: str
    emotion: str | None = None
    markers: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TTSArtifact:
    """一次 TTS 合成产物。

    Attributes:
        text: 服务端回显的文本（一般与请求一致）。
        audio: 音频 bytes；合成失败或返回空音频时为 ``None``。
        mime_type: 音频 MIME 类型。
        emotion: 请求时使用的情绪标记。
        metadata: 服务端返回的附加信息（采样率 / 时长 / provider 等）。
        error: 合成失败时的错误描述；成功时为 ``None``。
    """

    text: str
    audio: bytes | None = None
    mime_type: str = "audio/wav"
    emotion: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def is_playable(self) -> bool:
        """是否有可播放的音频（无错误且音频非空）。"""

        return self.error is None and bool(self.audio)


class TTSBackend(Protocol):
    """TTS 后端协议。"""

    async def synthesize(self, request: TTSRequest) -> TTSArtifact:
        """把文本合成为音频产物。

        Args:
            request: 合成请求。

        Returns:
            合成产物；失败时 ``error`` 字段非空。
        """
        ...

    async def emit(self, artifact: TTSArtifact, chat_stream: ChatStream) -> bool:
        """把产物作为 voice 消息发送给适配器播放。

        Args:
            artifact: 合成产物。
            chat_stream: 目标聊天流。

        Returns:
            是否发送成功。
        """
        ...


class HttpTTSBackend:
    """通过 HTTP 请求外部 TTS 服务合成音频。"""

    def __init__(
        self,
        *,
        endpoint: str,
        timeout: float,
        mime_type: str,
        provider: str,
    ) -> None:
        """初始化 HTTP TTS 后端。

        Args:
            endpoint: 合成接口地址。
            timeout: 请求超时时间（秒）。
            mime_type: 期望的音频 MIME 类型。
            provider: provider 名称；空串表示用服务端默认。
        """

        self.endpoint = endpoint
        self.timeout = timeout
        self.mime_type = mime_type
        self.provider = provider

    async def synthesize(self, request: TTSRequest) -> TTSArtifact:
        """调用 HTTP 合成接口。

        Args:
            request: 合成请求。

        Returns:
            合成产物；HTTP / 解析失败时返回带 ``error`` 的产物，不抛异常。
        """

        options: dict[str, Any] = {"mime_type": self.mime_type}
        if self.provider:
            options["provider"] = self.provider

        payload = {
            "protocol": TTS_PROTOCOL_VERSION,
            "stream_id": request.stream_id,
            "text": request.text,
            "emotion": request.emotion,
            "markers": request.markers,
            "options": options,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.endpoint, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as error:
            body = error.response.text.strip()
            message = f"{error}; response={body}" if body else str(error)
            logger.error(f"TTS 合成失败: {message}")
            return TTSArtifact(text=request.text, error=message)
        except (httpx.HTTPError, ValueError) as error:
            logger.error(f"TTS 合成失败: {error}")
            return TTSArtifact(text=request.text, error=str(error))

        audio_base64 = data.get("audio_base64")
        audio = (
            base64.b64decode(audio_base64)
            if isinstance(audio_base64, str) and audio_base64
            else None
        )

        metadata: dict[str, Any] = {
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
        """发送 voice 消息给适配器，由适配器完成播放。

        仅 voice 模式（``platform == "local_asr"``）使用；vtb 系模式直接走本地
        AudioPlayer，不经过消息发送链路。

        Args:
            artifact: 合成产物。
            chat_stream: 目标聊天流。

        Returns:
            是否发送成功。
        """

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
            message_id=f"tts_{chat_stream.stream_id}_{id(artifact):x}",
            content=content,
            processed_plain_text=artifact.text,
            message_type=MessageType.VOICE,
            platform=chat_stream.platform,
            chat_type=chat_stream.chat_type,
            stream_id=chat_stream.stream_id,
        )
        message.extra["tts"] = tts_meta
        return await send_api.send_message(message)


def build_tts_backend(section: "TTSSection") -> TTSBackend:
    """根据 ``[tts]`` 配置段构建 HTTP TTS 后端。

    Args:
        section: 插件配置的 ``tts`` 段。

    Returns:
        可用的 TTS 后端实例。
    """

    return HttpTTSBackend(
        endpoint=section.endpoint,
        timeout=section.timeout,
        mime_type=section.mime_type,
        provider=section.provider,
    )


async def retry_empty_audio(
    *,
    backend: TTSBackend,
    request: TTSRequest,
    artifact: TTSArtifact,
    retry_count: int,
) -> TTSArtifact:
    """TTS 返回空音频时重试，避免单个分段静默丢失。

    Args:
        backend: TTS 后端。
        request: 原始合成请求（重试时原样重发）。
        artifact: 首次合成结果。
        retry_count: 最大重试次数；``<= 0`` 时直接返回原产物。

    Returns:
        重试后拿到的产物；重试用尽仍为空时返回最后一次结果。
    """

    current = artifact
    for _ in range(max(0, retry_count)):
        if current.is_playable:
            return current
        current = await backend.synthesize(request)
    return current
