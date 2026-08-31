from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from google.genai import errors as genai_errors

from backend.app.evidence.classifier import GeminiRequest, ModelTransientError
from backend.app.evidence.gemini import GeminiStructuredTransport


@dataclass
class FixtureResponse:
    text: str


class FixtureModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> FixtureResponse:
        self.calls.append(kwargs)
        return FixtureResponse(text='{"classifications":[]}')


class FixtureClient:
    def __init__(self) -> None:
        self.models = FixtureModels()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_transport_uses_low_thinking_and_native_json_schema() -> None:
    client = FixtureClient()
    transport = GeminiStructuredTransport(client)
    schema = {
        "type": "object",
        "properties": {"classifications": {"type": "array", "items": {"type": "object"}}},
        "required": ["classifications"],
        "additionalProperties": False,
    }

    raw = transport.generate(
        GeminiRequest(
            model="gemini-3.7-flash",
            contents='{"source_clusters":[]}',
            response_json_schema=schema,
            validation_errors=("EVIDENCE_UNKNOWN_SOURCE_ID",),
        )
    )

    assert json.loads(raw) == {"classifications": []}
    call = client.models.calls[0]
    assert call["model"] == "gemini-3.7-flash"
    assert "EVIDENCE_UNKNOWN_SOURCE_ID" in call["contents"]
    config = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == schema
    assert config.thinking_config.thinking_level.value == "LOW"
    assert config.automatic_function_calling.disable is True
    assert config.max_output_tokens == 4000
    assert config.http_options.timeout == 20_000
    assert config.service_tier.value == "standard"
    assert config.should_return_http_response is None
    assert config.temperature is None
    assert config.top_p is None
    assert config.top_k is None


def test_transport_closes_the_owned_provider_client() -> None:
    client = FixtureClient()

    GeminiStructuredTransport(client).close()

    assert client.closed is True


@pytest.mark.parametrize(
    "error",
    [
        genai_errors.ServerError(503, {"message": "busy"}),
        genai_errors.ClientError(429, {"message": "rate limited"}),
    ],
)
def test_transport_maps_only_transient_provider_statuses_for_retry(error: Exception) -> None:
    class RaisingModels:
        def generate_content(self, **kwargs: Any) -> FixtureResponse:
            raise error

    class RaisingClient:
        models = RaisingModels()

    request = GeminiRequest(
        model="gemini-3.7-flash",
        contents='{"source_clusters":[]}',
        response_json_schema={"type": "object"},
    )

    with pytest.raises(ModelTransientError):
        GeminiStructuredTransport(RaisingClient()).generate(request)
