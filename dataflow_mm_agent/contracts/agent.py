"""Provider-neutral multimodal message and tool contracts.

Images are first-class message content.  They are never represented as local
paths in the agent-facing API; a serving adapter decides how to deliver their
base64 payload to a concrete model provider.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping, Sequence, Union

import jsonschema


_IMAGE_SIGNATURES = {
    "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda value: value.startswith((b"GIF87a", b"GIF89a")),
    "image/webp": lambda value: (
        len(value) >= 12
        and value[:4] == b"RIFF"
        and value[8:12] == b"WEBP"
    ),
    "image/bmp": lambda value: value.startswith(b"BM"),
}


@dataclass(frozen=True)
class TextContent:
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass(frozen=True)
class ImageContent:
    """An inline image represented by raw base64 without a data-URI prefix."""

    media_type: str
    data: str
    detail: str = "auto"

    def __post_init__(self) -> None:
        if self.media_type not in _IMAGE_SIGNATURES:
            raise ValueError(f"unsupported image media type: {self.media_type}")
        if self.detail not in {"auto", "high", "low", "original"}:
            raise ValueError(f"unsupported image detail: {self.detail}")
        if self.data.startswith("data:"):
            raise ValueError("ImageContent.data must be raw base64, not a data URI")
        self.decoded()

    def decoded(self) -> bytes:
        try:
            return base64.b64decode(self.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("image data is not valid base64") from exc

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.decoded()).hexdigest()

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        media_type: str,
        *,
        detail: str = "auto",
    ) -> "ImageContent":
        return cls(
            media_type=media_type,
            data=base64.b64encode(payload).decode("ascii"),
            detail=detail,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "image",
            "media_type": self.media_type,
            "data": self.data,
            "detail": self.detail,
        }


Content = Union[TextContent, ImageContent]


def content_from_dict(value: Mapping[str, Any]) -> Content:
    kind = value.get("type")
    if kind == "text":
        return TextContent(str(value.get("text") or ""))
    if kind == "image":
        return ImageContent(
            media_type=str(value["media_type"]),
            data=str(value["data"]),
            detail=str(value.get("detail") or "auto"),
        )
    raise ValueError(f"unsupported content type: {kind!r}")


@dataclass(frozen=True)
class Message:
    role: str
    content: tuple[Content, ...]
    name: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "observation"}:
            raise ValueError(f"unsupported message role: {self.role}")

    @classmethod
    def of(
        cls,
        role: str,
        content: Iterable[Content],
        *,
        name: str | None = None,
    ) -> "Message":
        return cls(role=role, content=tuple(content), name=name)

    @classmethod
    def text(
        cls,
        role: str,
        text: str,
        *,
        name: str | None = None,
    ) -> "Message":
        return cls(role=role, content=(TextContent(text),), name=name)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": self.role,
            "content": [item.to_dict() for item in self.content],
        }
        if self.name is not None:
            result["name"] = self.name
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Message":
        return cls.of(
            role=str(value["role"]),
            content=(content_from_dict(item) for item in value.get("content") or []),
            name=(str(value["name"]) if value.get("name") is not None else None),
        )


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    operation_type: Literal["query", "mutation"]
    input_schema: Mapping[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    def __post_init__(self) -> None:
        if not self.name or any(character.isspace() for character in self.name):
            raise ValueError("tool name must be non-empty and contain no whitespace")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("tool description must be non-empty")
        if self.operation_type not in {"query", "mutation"}:
            raise ValueError("tool operation_type must be 'query' or 'mutation'")
        if self.input_schema.get("type") != "object":
            raise ValueError("tool input_schema must describe a JSON object")
        try:
            jsonschema.Draft202012Validator.check_schema(dict(self.input_schema))
        except jsonschema.SchemaError as exc:
            raise ValueError(f"invalid tool input_schema: {exc.message}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "operation_type": self.operation_type,
            "input_schema": dict(self.input_schema),
        }


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolError":
        return cls(
            code=str(value["code"]),
            message=str(value["message"]),
            retryable=bool(value.get("retryable", False)),
        )


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: tuple[Content, ...] = ()
    error: ToolError | None = None
    is_final: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ok and self.error is not None:
            raise ValueError("successful ToolResult cannot carry an error")
        if not self.ok and self.error is None:
            raise ValueError("failed ToolResult must carry a ToolError")

    @classmethod
    def success(
        cls,
        content: Iterable[Content] = (),
        *,
        is_final: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=True,
            content=tuple(content),
            is_final=is_final,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        content: Iterable[Content] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            content=tuple(content),
            error=ToolError(code, message, retryable),
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "content": [item.to_dict() for item in self.content],
            "error": self.error.to_dict() if self.error else None,
            "is_final": self.is_final,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolResult":
        raw_error = value.get("error")
        return cls(
            ok=bool(value["ok"]),
            content=tuple(
                content_from_dict(item) for item in value.get("content") or []
            ),
            error=ToolError.from_dict(raw_error) if raw_error else None,
            is_final=bool(value.get("is_final", False)),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class ContentLimits:
    max_text_chars: int = 64_000
    max_image_bytes: int = 32 * 1024 * 1024
    max_image_pixels: int = 40_000_000


def validate_content(
    items: Sequence[Content],
    limits: ContentLimits = ContentLimits(),
) -> None:
    """Validate content before it is appended to model-visible history."""

    for item in items:
        if isinstance(item, TextContent):
            if len(item.text) > limits.max_text_chars:
                raise ValueError("text content exceeds configured character limit")
            continue
        payload = item.decoded()
        if len(payload) > limits.max_image_bytes:
            raise ValueError("image content exceeds configured byte limit")
        signature = _IMAGE_SIGNATURES[item.media_type]
        if not signature(payload):
            raise ValueError("image bytes do not match declared media type")
        try:
            from PIL import Image

            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
                image.verify()
        except ImportError:
            continue
        except Exception as exc:
            raise ValueError("image payload cannot be decoded") from exc
        if width * height > limits.max_image_pixels:
            raise ValueError("image content exceeds configured pixel limit")
