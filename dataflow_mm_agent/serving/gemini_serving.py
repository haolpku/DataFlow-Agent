"""Gemini ``generateContent`` serving for a Kigress gateway."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..contracts import ImageContent, Message, TextContent
from .base_serving import ModelServing


GeminiTransport = Callable[
    [str, Mapping[str, str], Mapping[str, Any], float],
    Mapping[str, Any],
]


class GeminiKigressServing(ModelServing):
    """Call a Gemini REST endpoint carrying Kigress routing headers."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        user_key: str,
        llm_model: str | None = None,
        biz_scene: str = "offline",
        timeout: float = 1800,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_workers: int = 1,
        max_images_per_request: int | None = 8,
        request_options: Mapping[str, Any] | None = None,
        transport: GeminiTransport | None = None,
    ):
        if not model.strip():
            raise ValueError("Gemini model must be non-empty")
        if not base_url.strip():
            raise ValueError("Gemini base_url must be non-empty")
        if not api_key.strip():
            raise ValueError("Kigress x-api-key must be non-empty")
        if not user_key.strip():
            raise ValueError("Kigress x-ks-user-key must be non-empty")
        if biz_scene not in {"offline", "online"}:
            raise ValueError("Kigress biz_scene must be 'offline' or 'online'")
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if max_images_per_request is not None and max_images_per_request < 1:
            raise ValueError("max_images_per_request must be positive or None")

        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.user_key = user_key
        self.llm_model = llm_model or model
        self.biz_scene = biz_scene
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_workers = max_workers
        self.max_images_per_request = max_images_per_request
        self.request_options = dict(request_options or {})
        self.transport = transport or self._post_json

    @staticmethod
    def endpoint(base_url: str, model: str) -> str:
        """Accept a route root, a model URL, or a URL containing a placeholder."""
        encoded_model = quote(model, safe="._-")
        endpoint = base_url.strip().rstrip("/")
        placeholders = ("{model}", "{model_id}", "{模型id}", "{模型ID}")
        for placeholder in placeholders:
            if placeholder in endpoint:
                return endpoint.replace(placeholder, encoded_model)
        if endpoint.endswith(":generateContent"):
            return endpoint
        if endpoint.endswith("/v1beta/models"):
            return f"{endpoint}/{encoded_model}:generateContent"
        if endpoint.endswith("/v1beta"):
            return f"{endpoint}/models/{encoded_model}:generateContent"
        return f"{endpoint}/v1beta/models/{encoded_model}:generateContent"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "x-ks-user-key": self.user_key,
            "x-ks-llm-model": self.llm_model,
            "x-ks-biz-scene": self.biz_scene,
        }

    @classmethod
    def provider_request(
        cls,
        messages: Sequence[Message],
        *,
        max_images_per_request: int | None = None,
    ) -> dict[str, Any]:
        if max_images_per_request is not None and max_images_per_request < 1:
            raise ValueError("max_images_per_request must be positive or None")
        image_count = sum(
            isinstance(item, ImageContent)
            for message in messages
            for item in message.content
        )
        omit_images = (
            max(0, image_count - max_images_per_request)
            if max_images_per_request is not None
            else 0
        )

        system_parts: list[dict[str, Any]] = []
        contents: list[dict[str, Any]] = []
        for message in messages:
            parts: list[dict[str, Any]] = []
            if message.role == "observation":
                parts.append({
                    "text": f"[tool {message.name or 'environment'} observation]",
                })
            omitted_here = 0
            for item in message.content:
                if isinstance(item, TextContent):
                    if item.text:
                        parts.append({"text": item.text})
                    continue
                if omit_images:
                    omit_images -= 1
                    omitted_here += 1
                    continue
                parts.append({
                    "inlineData": {
                        "mimeType": item.media_type,
                        "data": item.data,
                    },
                })
            if omitted_here:
                parts.append({
                    "text": (
                        f"[{omitted_here} earlier image(s) omitted to respect "
                        "the serving request limit]"
                    ),
                })
            if not parts:
                parts.append({"text": ""})

            if message.role == "system":
                system_parts.extend(parts)
                continue
            role = "model" if message.role == "assistant" else "user"
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})

        if not contents:
            contents.append({"role": "user", "parts": [{"text": ""}]})
        request: dict[str, Any] = {"contents": contents}
        if system_parts:
            request["systemInstruction"] = {"parts": system_parts}
        return request

    @staticmethod
    def _response_text(response: Mapping[str, Any]) -> str:
        candidates = response.get("candidates")
        if not isinstance(candidates, Sequence) or not candidates:
            feedback = response.get("promptFeedback")
            raise RuntimeError(f"Gemini response has no candidates: {feedback!r}")
        candidate = candidates[0]
        if not isinstance(candidate, Mapping):
            raise RuntimeError("Gemini response candidate is not an object")
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, Mapping) else None
        if not isinstance(parts, Sequence):
            raise RuntimeError("Gemini response candidate has no content parts")
        text = "".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, Mapping) and not bool(part.get("thought"))
        )
        if not text:
            raise RuntimeError("Gemini response contains no text")
        return text

    @staticmethod
    def _post_json(
        endpoint: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> Mapping[str, Any]:
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Gemini gateway returned HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"Gemini gateway request failed: {exc.reason}") from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Gemini gateway returned invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise RuntimeError("Gemini gateway response must be a JSON object")
        return value

    def _generate_one(self, messages: Sequence[Message]) -> str:
        payload = self.provider_request(
            messages,
            max_images_per_request=self.max_images_per_request,
        )
        generation_config: dict[str, Any] = {}
        if self.max_tokens is not None:
            generation_config["maxOutputTokens"] = self.max_tokens
        if self.temperature is not None:
            generation_config["temperature"] = self.temperature
        if generation_config:
            payload["generationConfig"] = generation_config
        payload.update(self.request_options)
        response = self.transport(
            self.endpoint(self.base_url, self.model),
            self.headers,
            payload,
            self.timeout,
        )
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


__all__ = ["GeminiKigressServing", "GeminiTransport"]
