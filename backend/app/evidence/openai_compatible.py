from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from backend.app.evidence.classifier import (
    GeminiRequest,
    ModelQuotaError,
    ModelTimeoutError,
    ModelTransientError,
)

_MAX_RESPONSE_BYTES = 65_536


class OpenAICompatibleStructuredTransport:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._client_factory = client_factory

    def __repr__(self) -> str:
        return (
            f"OpenAICompatibleStructuredTransport(endpoint={self._endpoint!r}, api_key=<redacted>)"
        )

    def generate(self, request: GeminiRequest) -> str:
        timeout = min(20.0, max(0.001, request.timeout_ms / 1000))
        payload = {
            "model": request.model,
            "messages": [{"role": "user", "content": _contents(request)}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "evidence_classifications",
                    "strict": True,
                    "schema": request.response_json_schema,
                },
            },
            "max_tokens": 4000,
        }
        try:
            with (
                self._client_factory(
                    timeout=httpx.Timeout(timeout),
                    follow_redirects=False,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                ) as client,
                client.stream(
                    "POST",
                    f"{self._endpoint}/chat/completions",
                    json=payload,
                ) as response,
            ):
                _validate_status(response)
                content = _bounded_content(response)
        except httpx.TimeoutException:
            raise ModelTimeoutError("MODEL_TRANSPORT_TIMEOUT") from None
        except httpx.TransportError:
            raise ModelTransientError("MODEL_TRANSPORT_ERROR") from None
        try:
            body: Any = json.loads(content)
            choices = body["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeError("MODEL_RESPONSE_INVALID") from None
        if (
            not isinstance(choices, list)
            or len(choices) != 1
            or not isinstance(content, str)
            or not content
            or len(content.encode()) > _MAX_RESPONSE_BYTES
        ):
            raise RuntimeError("MODEL_RESPONSE_INVALID")
        return content


def _contents(request: GeminiRequest) -> str:
    if not request.validation_errors:
        return request.contents
    return json.dumps(
        {
            "prior_validation_errors": list(request.validation_errors),
            "instruction": "Correct these errors using only the supplied IDs and schema.",
            "original_request": json.loads(request.contents),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_status(response: httpx.Response) -> None:
    if response.is_redirect:
        raise RuntimeError("MODEL_REDIRECT_FORBIDDEN")
    if response.status_code == 429:
        raise ModelQuotaError("MODEL_QUOTA")
    if response.status_code in {408, 409, 425} or 500 <= response.status_code <= 599:
        raise ModelTransientError("MODEL_SERVER_ERROR")
    if not 200 <= response.status_code <= 299:
        raise RuntimeError("MODEL_PROVIDER_REJECTED")


def _bounded_content(response: httpx.Response) -> bytes:
    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > _MAX_RESPONSE_BYTES:
                raise RuntimeError("MODEL_RESPONSE_TOO_LARGE")
        except ValueError:
            raise RuntimeError("MODEL_RESPONSE_INVALID") from None
    content = bytearray()
    for chunk in response.iter_bytes():
        content.extend(chunk)
        if len(content) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("MODEL_RESPONSE_TOO_LARGE")
    return bytes(content)
