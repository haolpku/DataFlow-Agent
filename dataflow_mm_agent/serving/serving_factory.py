"""Construct a serving backend from explicit or environment configuration."""

from __future__ import annotations

import os
from typing import Mapping

from .base_serving import ModelServing
from .gemini_serving import GeminiKigressServing
from .openai_serving import OpenAICompatibleServing


def create_model_serving(
    *,
    backend: str,
    model: str,
    base_url: str | None,
    api_key: str = "EMPTY",
    kigress_api_key: str | None = None,
    kigress_user_key: str | None = None,
    kigress_llm_model: str | None = None,
    kigress_biz_scene: str = "offline",
    timeout: float = 1800,
    max_tokens: int | None = None,
    temperature: float | None = None,
    max_workers: int = 1,
    max_images_per_request: int | None = 8,
) -> ModelServing:
    normalized = backend.strip().lower().replace("-", "_")
    if normalized in {"openai", "openai_compatible"}:
        return OpenAICompatibleServing(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
            max_workers=max_workers,
            max_images_per_request=max_images_per_request,
        )
    if normalized in {"gemini", "kigress", "gemini_kigress"}:
        if base_url is None:
            raise ValueError("Gemini/Kigress serving requires API_URL")
        return GeminiKigressServing(
            model=model,
            base_url=base_url,
            api_key=kigress_api_key or "",
            user_key=kigress_user_key or "",
            llm_model=kigress_llm_model,
            biz_scene=kigress_biz_scene,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
            max_workers=max_workers,
            max_images_per_request=max_images_per_request,
        )
    raise ValueError(f"unsupported serving backend: {backend!r}")


def create_model_serving_from_env(
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 1800,
    max_tokens: int | None = None,
    temperature: float | None = None,
    max_workers: int = 1,
    max_images_per_request: int | None = 8,
) -> ModelServing:
    values = os.environ if environ is None else environ
    model = values.get("MODEL", "").strip()
    if not model:
        raise ValueError("MODEL is required")
    return create_model_serving(
        backend=values.get("SERVING_BACKEND", "openai"),
        model=model,
        base_url=values.get("API_URL") or None,
        api_key=values.get("DF_API_KEY", "EMPTY"),
        kigress_api_key=values.get("KIGRESS_API_KEY"),
        kigress_user_key=values.get("KIGRESS_USER_KEY"),
        kigress_llm_model=values.get("KIGRESS_LLM_MODEL") or None,
        kigress_biz_scene=values.get("KIGRESS_BIZ_SCENE", "offline"),
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
        max_workers=max_workers,
        max_images_per_request=max_images_per_request,
    )


__all__ = ["create_model_serving", "create_model_serving_from_env"]
