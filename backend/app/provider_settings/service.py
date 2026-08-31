from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import Lock

from backend.app.contracts.v1 import (
    OwnerModelProvider,
    ProviderSettingsResponse,
    ProviderSettingsUpdateRequest,
)

from .codec import CredentialSecret
from .models import ProviderKind, ProviderMetadata
from .repository import ProviderSettingsRepositoryError, SQLAlchemyProviderSettingsRepository


class OwnerProviderSettingsService:
    def __init__(
        self,
        repository: SQLAlchemyProviderSettingsRepository,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._lock = Lock()
        self._closed = False

    def status(self) -> ProviderSettingsResponse:
        with self._lock:
            self._require_open()
            return _response(self._repository.metadata())

    def replace(self, request: ProviderSettingsUpdateRequest) -> ProviderSettingsResponse:
        with self._lock:
            self._require_open()
            provider = {
                OwnerModelProvider.GEMINI: ProviderKind.OWNER_GEMINI,
                OwnerModelProvider.OPENAI_COMPATIBLE: ProviderKind.OWNER_OPENAI_COMPATIBLE,
            }[request.provider]
            metadata = self._repository.replace(
                provider=provider,
                endpoint=request.endpoint,
                model=request.model,
                value=CredentialSecret.from_text(request.api_key.get_secret_value()),
                trusted_at=self._clock(),
            )
            return _response(metadata)

    def clear(self) -> ProviderSettingsResponse:
        with self._lock:
            self._require_open()
            self._repository.clear(trusted_at=self._clock())
            return ProviderSettingsResponse(configured=False)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise ProviderSettingsRepositoryError("PROVIDER_SETTINGS_CLOSED")


def _response(metadata: ProviderMetadata | None) -> ProviderSettingsResponse:
    if metadata is None:
        return ProviderSettingsResponse(configured=False)
    provider = {
        ProviderKind.OWNER_GEMINI: OwnerModelProvider.GEMINI,
        ProviderKind.OWNER_OPENAI_COMPATIBLE: OwnerModelProvider.OPENAI_COMPATIBLE,
    }[metadata.provider]
    return ProviderSettingsResponse(
        configured=True,
        provider=provider,
        endpoint=metadata.endpoint,
        model=metadata.model,
        generation=metadata.generation,
    )
