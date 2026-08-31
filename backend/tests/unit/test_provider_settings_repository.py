from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import OwnerModelProvider, ProviderSettingsUpdateRequest
from backend.app.persistence.sqlalchemy_models import Base, OwnerProviderSettingsRow
from backend.app.provider_settings import (
    CredentialCodec,
    CredentialSecret,
    OwnerProviderSettingsService,
    ProviderKind,
    ProviderSettingsRepositoryError,
    ProviderSettingsValidationError,
    SQLAlchemyProviderSettingsRepository,
)

NOW = datetime(2026, 8, 29, 22, tzinfo=UTC)


def repository(*, allowed_origins: tuple[str, ...] = ()):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    target = SQLAlchemyProviderSettingsRepository(
        sessions,
        codec=CredentialCodec(b"provider-settings-test-master-secret"),
        allowed_openai_origins=allowed_origins,
    )
    return target, sessions, engine


def test_gemini_credential_is_encrypted_redacted_and_generation_bound() -> None:
    target, sessions, engine = repository()
    assert target.metadata() is None
    assert target.resolve() is None

    first = target.replace(
        provider=ProviderKind.OWNER_GEMINI,
        endpoint=None,
        model="gemini-3.7-flash",
        value=CredentialSecret.from_text("owner-secret-one"),
        trusted_at=NOW,
    )
    assert first.generation == 1
    resolved = target.resolve()
    assert resolved is not None
    assert resolved.metadata == first
    assert resolved.credential.reveal_text() == "owner-secret-one"
    assert "owner-secret-one" not in repr(resolved)

    with sessions() as session:
        row = session.scalar(select(OwnerProviderSettingsRow))
        assert row is not None
        assert row.credential_ciphertext is not None
        assert b"owner-secret-one" not in row.credential_ciphertext
        assert row.credential_nonce is not None
        assert len(row.credential_nonce) == 12

    second = target.replace(
        provider=ProviderKind.OWNER_GEMINI,
        endpoint=None,
        model="gemini-3.7-flash",
        value=CredentialSecret.from_text("owner-secret-two"),
        trusted_at=NOW + timedelta(seconds=1),
    )
    assert second.generation == 2
    resolved_second = target.resolve()
    assert resolved_second is not None
    assert resolved_second.credential.reveal_text() == "owner-secret-two"
    engine.dispose()


def test_clear_retains_monotonic_generation_without_credential_material() -> None:
    target, sessions, engine = repository()
    target.replace(
        provider=ProviderKind.OWNER_GEMINI,
        endpoint=None,
        model="gemini-3.7-flash",
        value=CredentialSecret.from_text("owner-secret"),
        trusted_at=NOW,
    )

    assert target.clear(trusted_at=NOW + timedelta(seconds=1)) == 2
    assert target.metadata() is None
    assert target.resolve() is None
    assert target.clear(trusted_at=NOW + timedelta(seconds=2)) == 2
    with sessions() as session:
        row = session.scalar(select(OwnerProviderSettingsRow))
        assert row is not None
        assert row.active is False
        assert row.generation == 2
        assert row.credential_nonce is None
        assert row.credential_ciphertext is None

    restored = target.replace(
        provider=ProviderKind.OWNER_GEMINI,
        endpoint=None,
        model="gemini-3.7-flash",
        value=CredentialSecret.from_text("replacement-secret"),
        trusted_at=NOW + timedelta(seconds=3),
    )
    assert restored.generation == 3
    engine.dispose()


def test_openai_compatible_endpoint_uses_exact_server_allowlist() -> None:
    target, _, engine = repository(allowed_origins=("https://api.example.com",))
    metadata = target.replace(
        provider=ProviderKind.OWNER_OPENAI_COMPATIBLE,
        endpoint="https://api.example.com/",
        model="example-chat",
        value=CredentialSecret.from_text("owner-secret"),
        trusted_at=NOW,
    )
    assert metadata.endpoint == "https://api.example.com/v1"

    with pytest.raises(
        ProviderSettingsValidationError,
        match="PROVIDER_ENDPOINT_ORIGIN_NOT_ALLOWED",
    ):
        target.replace(
            provider=ProviderKind.OWNER_OPENAI_COMPATIBLE,
            endpoint="https://other.example.com/v1",
            model="example-chat",
            value=CredentialSecret.from_text("owner-secret"),
            trusted_at=NOW + timedelta(seconds=1),
        )
    assert target.metadata() == metadata
    engine.dispose()


def test_corrupt_active_row_fails_closed_without_exposing_ciphertext() -> None:
    target, sessions, engine = repository()
    target.replace(
        provider=ProviderKind.OWNER_GEMINI,
        endpoint=None,
        model="gemini-3.7-flash",
        value=CredentialSecret.from_text("owner-secret"),
        trusted_at=NOW,
    )
    with sessions.begin() as session:
        row = session.get(OwnerProviderSettingsRow, "owner-ai-provider")
        assert row is not None
        row.credential_ciphertext = b"x" * 17

    with pytest.raises(
        ProviderSettingsRepositoryError,
        match="PROVIDER_SETTINGS_CREDENTIAL_UNAVAILABLE",
    ) as caught:
        target.resolve()
    assert "owner-secret" not in repr(caught.value)
    assert "xxxxxxxx" not in repr(caught.value)
    engine.dispose()


def test_repository_rejects_untrusted_time_before_write() -> None:
    target, _, engine = repository()
    with pytest.raises(
        ProviderSettingsRepositoryError,
        match="PROVIDER_SETTINGS_TRUSTED_TIME_INVALID",
    ):
        target.replace(
            provider=ProviderKind.OWNER_GEMINI,
            endpoint=None,
            model="gemini-3.7-flash",
            value=CredentialSecret.from_text("owner-secret"),
            trusted_at=NOW.replace(tzinfo=None),
        )
    assert target.metadata() is None
    engine.dispose()


def test_repository_wraps_database_failures_without_storage_detail() -> None:
    class FailingSessions:
        def __call__(self):
            raise SQLAlchemyError("private-database-detail")

        def begin(self):
            raise SQLAlchemyError("private-database-detail")

    target = SQLAlchemyProviderSettingsRepository(
        FailingSessions(),  # type: ignore[arg-type]
        codec=CredentialCodec(b"provider-settings-test-master-secret"),
    )

    for operation in (
        target.metadata,
        target.resolve,
        lambda: target.replace(
            provider=ProviderKind.OWNER_GEMINI,
            endpoint=None,
            model="model",
            value=CredentialSecret.from_text("write-only-key"),
            trusted_at=NOW,
        ),
        lambda: target.clear(trusted_at=NOW),
    ):
        with pytest.raises(ProviderSettingsRepositoryError) as error:
            operation()
        assert "private-database-detail" not in str(error.value)
        assert "write-only-key" not in str(error.value)


def test_closed_service_rejects_every_operation() -> None:
    target, _, engine = repository()
    service = OwnerProviderSettingsService(target, clock=lambda: NOW)
    service.close()
    service.close()

    for operation in (
        service.status,
        service.clear,
        lambda: service.replace(
            ProviderSettingsUpdateRequest(
                provider=OwnerModelProvider.GEMINI,
                model="model",
                api_key="write-only-key",
            )
        ),
    ):
        with pytest.raises(ProviderSettingsRepositoryError, match="PROVIDER_SETTINGS_CLOSED"):
            operation()
    engine.dispose()
