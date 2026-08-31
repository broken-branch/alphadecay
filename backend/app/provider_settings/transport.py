from __future__ import annotations

from collections.abc import Callable

import httpx

from backend.app.evidence.classifier import (
    DEFAULT_GEMINI_BINDING,
    GeminiRequest,
    ModelBindingChangedError,
    ModelProviderBinding,
    ModelQuotaError,
    ModelTimeoutError,
    ModelTransientError,
    StructuredModelTransport,
)
from backend.app.evidence.gemini import GeminiStructuredTransport
from backend.app.evidence.openai_compatible import OpenAICompatibleStructuredTransport

from .models import ProviderKind, ProviderMetadata
from .repository import ResolvedProviderSettings, SQLAlchemyProviderSettingsRepository


class OwnerModelTransportResolver:
    def __init__(
        self,
        repository: SQLAlchemyProviderSettingsRepository,
        default_transport: StructuredModelTransport,
        *,
        gemini_factory: Callable[[str], GeminiStructuredTransport] = (
            GeminiStructuredTransport.from_api_key
        ),
        openai_client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self._repository = repository
        self._default_transport = default_transport
        self._gemini_factory = gemini_factory
        self._openai_client_factory = openai_client_factory

    def __repr__(self) -> str:
        return "OwnerModelTransportResolver(<redacted>)"

    def resolve_binding(self) -> ModelProviderBinding:
        metadata = self._repository.metadata()
        return DEFAULT_GEMINI_BINDING if metadata is None else _binding(metadata)

    def generate(self, request: GeminiRequest) -> str:
        expected = request.provider_binding
        if expected == DEFAULT_GEMINI_BINDING:
            if self._repository.metadata() is not None:
                raise ModelBindingChangedError("MODEL_PROVIDER_CHANGED")
            return self._default_transport.generate(request)

        resolved = self._repository.resolve()
        if resolved is None or _binding(resolved.metadata) != expected:
            raise ModelBindingChangedError("MODEL_PROVIDER_CHANGED")
        return self._generate_owner(resolved, request)

    def _generate_owner(
        self,
        resolved: ResolvedProviderSettings,
        request: GeminiRequest,
    ) -> str:
        provider_value = resolved.credential.reveal_text()
        if resolved.metadata.provider is ProviderKind.OWNER_GEMINI:
            try:
                transport = self._gemini_factory(provider_value)
            except Exception:
                raise RuntimeError("MODEL_PROVIDER_INIT_FAILED") from None
            try:
                return _sanitized_generate(transport, request)
            finally:
                try:
                    transport.close()
                except Exception:
                    raise RuntimeError("MODEL_PROVIDER_CLEANUP_FAILED") from None
        if resolved.metadata.provider is ProviderKind.OWNER_OPENAI_COMPATIBLE:
            transport = OpenAICompatibleStructuredTransport(
                endpoint=resolved.metadata.endpoint,
                api_key=provider_value,
                client_factory=self._openai_client_factory,
            )
            return _sanitized_generate(transport, request)
        raise RuntimeError("MODEL_PROVIDER_INVALID")


def _binding(metadata: ProviderMetadata) -> ModelProviderBinding:
    return ModelProviderBinding(
        provider=metadata.provider.value,
        endpoint=metadata.endpoint,
        model=metadata.model,
        generation=metadata.generation,
    )


def _sanitized_generate(transport: StructuredModelTransport, request: GeminiRequest) -> str:
    try:
        return transport.generate(request)
    except ModelTimeoutError:
        raise ModelTimeoutError("MODEL_TRANSPORT_TIMEOUT") from None
    except ModelQuotaError:
        raise ModelQuotaError("MODEL_QUOTA") from None
    except ModelTransientError:
        raise ModelTransientError("MODEL_PROVIDER_TRANSIENT") from None
    except Exception:
        raise RuntimeError("MODEL_PROVIDER_ERROR") from None
