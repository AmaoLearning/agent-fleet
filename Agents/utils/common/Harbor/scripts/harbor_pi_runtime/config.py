"""Shared Pi connection configuration for Harbor components."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PiRuntimeConfig:
    pi_bin: str = "pi"
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    timeout_seconds: int = 900
    thinking_level: str | None = None

    def with_api_key_fallback(
        self, fallback_env: str = "API_KEY"
    ) -> PiRuntimeConfig:
        inherit_api_key(self.api_key_env, fallback_env=fallback_env)
        return self


def inherit_api_key(api_key_env: str, *, fallback_env: str = "API_KEY") -> None:
    if api_key_env and not os.environ.get(api_key_env) and os.environ.get(fallback_env):
        os.environ[api_key_env] = os.environ[fallback_env]


def model_from_env(component_prefix: str) -> str:
    return os.environ.get(f"{component_prefix}_MODEL") or os.environ.get("MODEL") or ""


def base_url_from_env(component_prefix: str, *, normalize: bool = False) -> str:
    value = (
        os.environ.get(f"{component_prefix}_BASE_URL")
        or os.environ.get("BASE_URL")
        or ""
    )
    return normalized_base_url(value) if normalize else value


def normalized_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    if value and not value.endswith("/v1"):
        value = f"{value}/v1"
    return value


def models_config(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
    display_name: str,
    auth_header: bool | None = None,
) -> dict[str, Any]:
    provider_config: dict[str, Any] = {
        "baseUrl": base_url,
        "api": "openai-completions",
        "apiKey": f"${api_key_env}",
        "compat": {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": False,
            "supportsUsageInStreaming": True,
            "maxTokensField": "max_tokens",
            "thinkingFormat": "zai",
        },
        "models": [
            {
                "id": model,
                "name": display_name,
                "reasoning": True,
                "input": ["text"],
                "contextWindow": 204800,
                "maxTokens": 32768,
                "cost": {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                },
            }
        ],
    }
    if auth_header is not None:
        provider_config["authHeader"] = auth_header
    return {"providers": {provider: provider_config}}
