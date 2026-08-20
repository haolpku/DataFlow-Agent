"""Structured multimodal serving backed by an OpenAI-compatible endpoint."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Mapping, Sequence

from ..contracts import ImageContent, Message, TextContent
from .base_serving import ModelServing


class OpenAICompatibleServing(ModelServing):
    """Use one canonical multimodal history with API or local vLLM servers."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str = "EMPTY",
        client: Any | None = None,
        timeout: float = 1800,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_workers: int = 1,
        max_images_per_request: int | None = 8,
        request_options: Mapping[str, Any] | None = None,
    ):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if max_images_per_request is not None and max_images_per_request < 1:
            raise ValueError("max_images_per_request must be positive or None")
        if client is None:
            from openai import OpenAI

            client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self.client = client
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_workers = max_workers
        self.max_images_per_request = max_images_per_request
        self.request_options = dict(request_options or {})

    @staticmethod
    def _content(message: Message) -> str | list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        has_image = False
        if message.role == "observation":
            blocks.append({
                "type": "text",
                "text": f"[tool {message.name or 'environment'} observation]",
            })
        for item in message.content:
            if isinstance(item, TextContent):
                if item.text:
                    blocks.append({"type": "text", "text": item.text})
            elif isinstance(item, ImageContent):
                has_image = True
                provider_detail = (
                    item.detail
                    if item.detail in {"auto", "high", "low"}
                    else "high"
                )
                blocks.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{item.media_type};base64,{item.data}",
                        "detail": provider_detail,
                    },
                })
        if has_image:
            return blocks
        return "\n".join(
            str(block["text"])
            for block in blocks
            if block.get("type") == "text"
        )

    @classmethod
    def provider_messages(
        cls,
        messages: Sequence[Message],
        *,
        max_images_per_request: int | None = None,
    ) -> list[dict[str, Any]]:
        if max_images_per_request is not None and max_images_per_request < 1:
            raise ValueError("max_images_per_request must be positive or None")
        image_count = sum(
            isinstance(item, ImageContent)
            for message in messages
            for item in message.content
        )
        omit_images = max(
            0,
            image_count - max_images_per_request,
        ) if max_images_per_request is not None else 0

        rendered: list[dict[str, Any]] = []
        for message in messages:
            role = "user" if message.role == "observation" else message.role
            if not omit_images:
                content = cls._content(message)
            else:
                projected = []
                omitted_here = 0
                for item in message.content:
                    if isinstance(item, ImageContent) and omit_images:
                        omit_images -= 1
                        omitted_here += 1
                        continue
                    projected.append(item)
                if omitted_here:
                    projected.append(TextContent(
                        f"[{omitted_here} earlier image(s) omitted to respect "
                        "the serving request limit]"
                    ))
                content = cls._content(Message.of(
                    message.role,
                    projected,
                    name=message.name,
                ))
            rendered.append({"role": role, "content": content})
        return rendered

    @staticmethod
    def _response_text(response: Any) -> str:
        content = response.choices[0].message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, dict):
                    chunks.append(str(item.get("text") or ""))
                else:
                    chunks.append(str(getattr(item, "text", "") or ""))
            return "".join(chunks)
        return str(content or "")

    def _generate_one(self, messages: Sequence[Message]) -> str:
        options: dict[str, Any] = {
            "model": self.model,
            "messages": self.provider_messages(
                messages,
                max_images_per_request=self.max_images_per_request,
            ),
            "timeout": self.timeout,
            **self.request_options,
        }
        if self.max_tokens is not None:
            options["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            options["temperature"] = self.temperature
        response = self.client.chat.completions.create(**options)
        return self._response_text(response)

    def generate_messages(
        self,
        conversations: Sequence[Sequence[Message]],
    ) -> list[str]:
        if self.max_workers == 1 or len(conversations) <= 1:
            return [self._generate_one(messages) for messages in conversations]
        results = [""] * len(conversations)
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._generate_one, messages): index
                for index, messages in enumerate(conversations)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def health_check(self) -> bool:
        try:
            self.client.models.list()
            return True
        except Exception:
            return False


__all__ = ["OpenAICompatibleServing"]
