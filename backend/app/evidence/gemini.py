from __future__ import annotations

import json
from typing import Protocol

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from httpx import TimeoutException as HttpxTimeout
from requests import Timeout as RequestsTimeout

from backend.app.evidence.classifier import (
    GeminiRequest,
    ModelTimeoutError,
    ModelTransientError,
)


class _TextResponse(Protocol):
    @property
    def text(self) -> str | None: ...


class _Models(Protocol):
    def generate_content(
        self, *, model: str, contents: str, config: types.GenerateContentConfig
    ) -> _TextResponse: ...


class _GeminiClient(Protocol):
    @property
    def models(self) -> _Models: ...


class GeminiStructuredTransport:
    def __init__(self, client: _GeminiClient) -> None:
        self._client = client

    @classmethod
    def from_api_key(cls, api_key: str) -> GeminiStructuredTransport:
        return cls(genai.Client(api_key=api_key))

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def generate(self, request: GeminiRequest) -> str:
        contents = request.contents
        if request.validation_errors:
            contents = json.dumps(
                {
                    "prior_validation_errors": list(request.validation_errors),
                    "instruction": "Correct these errors using only the supplied IDs and schema.",
                    "original_request": json.loads(request.contents),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        try:
            response = self._client.models.generate_content(
                model=request.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type=request.response_mime_type,
                    response_json_schema=request.response_json_schema,
                    thinking_config=types.ThinkingConfig(thinking_level=request.thinking_level),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    max_output_tokens=4000,
                    service_tier=types.ServiceTier(request.service_tier),
                    http_options=types.HttpOptions(timeout=request.timeout_ms),
                ),
            )
        except genai_errors.ServerError as exc:
            raise ModelTransientError("MODEL_SERVER_ERROR") from exc
        except genai_errors.ClientError as exc:
            if exc.code in {408, 429}:
                raise ModelTransientError("MODEL_TRANSIENT_CLIENT_ERROR") from exc
            raise
        except (HttpxTimeout, RequestsTimeout) as exc:
            raise ModelTimeoutError("MODEL_TRANSPORT_TIMEOUT") from exc
        if response.text is None:
            raise RuntimeError("MODEL_EMPTY_RESPONSE")
        return response.text
