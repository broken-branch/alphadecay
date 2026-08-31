from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.evidence.classifier import (
    DEFAULT_GEMINI_BINDING,
    GeminiRequest,
    ModelBindingChangedError,
)
from backend.app.evidence.openai_compatible import OpenAICompatibleStructuredTransport
from backend.app.persistence.sqlalchemy_models import Base
from backend.app.provider_settings import (
    CredentialCodec,
    CredentialSecret,
    OwnerModelTransportResolver,
    ProviderKind,
    ProviderSettingsRepositoryError,
    SQLAlchemyProviderSettingsRepository,
)

NOW = datetime(2026, 8, 29, 20, tzinfo=UTC)
KEY = "owner-key-that-must-never-leak"


class DefaultTransport:
    def __init__(self) -> None:
        self.requests: list[GeminiRequest] = []

    def generate(self, request: GeminiRequest) -> str:
        self.requests.append(request)
        return '{"classifications":[]}'


class OwnerGeminiTransport(DefaultTransport):
    def __init__(self, credential: str) -> None:
        super().__init__()
        self.credential = credential
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


@pytest.fixture
def repository(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'providers.db'}")
    Base.metadata.create_all(engine)
    target = SQLAlchemyProviderSettingsRepository(
        sessionmaker(engine, expire_on_commit=False),
        codec=CredentialCodec(b"m" * 32),
        allowed_openai_origins=("https://models.example",),
    )
    yield target
    engine.dispose()


def request(binding=DEFAULT_GEMINI_BINDING) -> GeminiRequest:
    return GeminiRequest(
        model=binding.model,
        contents='{"source_clusters":[]}',
        response_json_schema={"type": "object"},
        provider_binding=binding,
    )


def test_default_transport_remains_active_without_owner_setting(repository) -> None:
    default = DefaultTransport()
    resolver = OwnerModelTransportResolver(repository, default)

    binding = resolver.resolve_binding()
    result = resolver.generate(request(binding))

    assert binding == DEFAULT_GEMINI_BINDING
    assert json.loads(result) == {"classifications": []}
    assert default.requests == [request(binding)]


def test_owner_gemini_is_resolved_per_request_and_closed(repository) -> None:
    metadata = repository.replace(
        provider=ProviderKind.OWNER_GEMINI,
        endpoint=None,
        model="gemini-owner-model",
        value=CredentialSecret.from_text(KEY),
        trusted_at=NOW,
    )
    transports: list[OwnerGeminiTransport] = []

    def factory(credential: str) -> OwnerGeminiTransport:
        transport = OwnerGeminiTransport(credential)
        transports.append(transport)
        return transport

    resolver = OwnerModelTransportResolver(repository, DefaultTransport(), gemini_factory=factory)
    binding = resolver.resolve_binding()

    resolver.generate(request(binding))

    assert binding.model == metadata.model
    assert binding.generation == metadata.generation
    assert len(transports) == 1
    assert transports[0].credential == KEY
    assert transports[0].close_count == 1
    assert KEY not in repr(resolver)
    assert KEY not in repr(repository.resolve())


def test_generation_change_between_capture_and_generate_fails_closed(repository) -> None:
    repository.replace(
        provider=ProviderKind.OWNER_GEMINI,
        endpoint=None,
        model="first-model",
        value=CredentialSecret.from_text(KEY),
        trusted_at=NOW,
    )
    transports: list[OwnerGeminiTransport] = []
    resolver = OwnerModelTransportResolver(
        repository,
        DefaultTransport(),
        gemini_factory=lambda credential: transports.append(OwnerGeminiTransport(credential))
        or transports[-1],
    )
    captured = resolver.resolve_binding()
    repository.replace(
        provider=ProviderKind.OWNER_GEMINI,
        endpoint=None,
        model="second-model",
        value=CredentialSecret.from_text("replacement-key"),
        trusted_at=NOW,
    )

    with pytest.raises(ModelBindingChangedError, match="MODEL_PROVIDER_CHANGED") as error:
        resolver.generate(request(captured))

    assert transports == []
    assert KEY not in str(error.value)


def test_owner_transport_error_is_sanitized_and_resource_closes_once(repository) -> None:
    repository.replace(
        provider=ProviderKind.OWNER_GEMINI,
        endpoint=None,
        model="owner-model",
        value=CredentialSecret.from_text(KEY),
        trusted_at=NOW,
    )
    transports: list[OwnerGeminiTransport] = []

    class FailingTransport(OwnerGeminiTransport):
        def generate(self, request: GeminiRequest) -> str:
            raise RuntimeError(f"provider rejected {self.credential}")

    def factory(credential: str) -> OwnerGeminiTransport:
        transport = FailingTransport(credential)
        transports.append(transport)
        return transport

    resolver = OwnerModelTransportResolver(
        repository,
        DefaultTransport(),
        gemini_factory=factory,
    )
    binding = resolver.resolve_binding()

    with pytest.raises(RuntimeError, match="MODEL_PROVIDER_ERROR") as error:
        resolver.generate(request(binding))

    assert transports[0].close_count == 1
    assert KEY not in str(error.value)


def test_openai_compatible_transport_is_bounded_and_disables_redirects() -> None:
    observed: dict[str, object] = {}

    def handler(incoming: httpx.Request) -> httpx.Response:
        observed["request"] = incoming
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"classifications":[]}'}}]},
        )

    def factory(**kwargs: object) -> httpx.Client:
        observed["kwargs"] = kwargs
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    binding = DEFAULT_GEMINI_BINDING
    transport = OpenAICompatibleStructuredTransport(
        endpoint="https://models.example/v1",
        api_key=KEY,
        client_factory=factory,
    )

    result = transport.generate(request(binding))

    assert json.loads(result) == {"classifications": []}
    assert observed["kwargs"]["follow_redirects"] is False
    incoming = observed["request"]
    assert isinstance(incoming, httpx.Request)
    assert incoming.url == httpx.URL("https://models.example/v1/chat/completions")
    payload = json.loads(incoming.content)
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert KEY not in repr(transport)


def test_resolver_uses_only_allowlisted_persisted_openai_endpoint(repository) -> None:
    metadata = repository.replace(
        provider=ProviderKind.OWNER_OPENAI_COMPATIBLE,
        endpoint="https://models.example/v1",
        model="compatible-model",
        value=CredentialSecret.from_text(KEY),
        trusted_at=NOW,
    )
    calls: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"classifications":[]}'}}]},
        )

    def factory(**kwargs: object) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    resolver = OwnerModelTransportResolver(
        repository,
        DefaultTransport(),
        openai_client_factory=factory,
    )
    binding = resolver.resolve_binding()

    result = resolver.generate(request(binding))

    assert binding.endpoint == metadata.endpoint
    assert binding.model == metadata.model
    assert json.loads(result) == {"classifications": []}
    assert len(calls) == 1
    assert calls[0].url == httpx.URL("https://models.example/v1/chat/completions")
    assert calls[0].headers["authorization"] == f"Bearer {KEY}"


def test_resolver_rejects_endpoint_removed_from_current_allowlist(repository) -> None:
    repository.replace(
        provider=ProviderKind.OWNER_OPENAI_COMPATIBLE,
        endpoint="https://models.example/v1",
        model="compatible-model",
        value=CredentialSecret.from_text(KEY),
        trusted_at=NOW,
    )
    repository._allowed_openai_origins = ()
    resolver = OwnerModelTransportResolver(repository, DefaultTransport())

    with pytest.raises(
        ProviderSettingsRepositoryError,
        match="PROVIDER_SETTINGS_STATE_INVALID",
    ):
        resolver.resolve_binding()


def test_openai_compatible_redirect_is_never_followed_and_never_leaks_key() -> None:
    calls = 0

    def handler(_incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "https://elsewhere.example/v1"})

    def factory(**kwargs: object) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    transport = OpenAICompatibleStructuredTransport(
        endpoint="https://models.example/v1",
        api_key=KEY,
        client_factory=factory,
    )

    with pytest.raises(RuntimeError, match="MODEL_REDIRECT_FORBIDDEN") as error:
        transport.generate(request())

    assert calls == 1
    assert KEY not in str(error.value)


def test_openai_compatible_response_body_is_bounded_while_streaming() -> None:
    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 65_537)

    def factory(**kwargs: object) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    transport = OpenAICompatibleStructuredTransport(
        endpoint="https://models.example/v1",
        api_key=KEY,
        client_factory=factory,
    )

    with pytest.raises(RuntimeError, match="MODEL_RESPONSE_TOO_LARGE") as error:
        transport.generate(request())

    assert KEY not in str(error.value)
