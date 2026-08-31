from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.persistence.sqlalchemy_models import OwnerProviderSettingsRow

from .codec import CredentialCodec, CredentialCodecError, CredentialSecret, EncryptedCredential
from .models import (
    GEMINI_ENDPOINT,
    OWNER_SETTINGS_SINGLETON_ID,
    PROVIDER_SETTINGS_SCHEMA_VERSION,
    ProviderKind,
    ProviderMetadata,
    ProviderSettingsValidationError,
    normalize_openai_compatible_endpoint,
)


class ProviderSettingsRepositoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedProviderSettings:
    metadata: ProviderMetadata
    credential: CredentialSecret

    def __repr__(self) -> str:
        return f"ResolvedProviderSettings(metadata={self.metadata!r}, credential=<redacted>)"


class SQLAlchemyProviderSettingsRepository:
    def __init__(
        self,
        sessions: sessionmaker,
        *,
        codec: CredentialCodec,
        allowed_openai_origins: tuple[str, ...] = (),
    ) -> None:
        self._sessions = sessions
        self._codec = codec
        self._allowed_openai_origins = allowed_openai_origins

    def metadata(self) -> ProviderMetadata | None:
        try:
            with self._sessions() as session:
                row = session.get(OwnerProviderSettingsRow, OWNER_SETTINGS_SINGLETON_ID)
                return self._metadata(row)
        except ProviderSettingsRepositoryError:
            raise
        except SQLAlchemyError as error:
            raise ProviderSettingsRepositoryError("PROVIDER_SETTINGS_READ_FAILED") from error

    def resolve(self) -> ResolvedProviderSettings | None:
        try:
            with self._sessions() as session:
                row = session.get(OwnerProviderSettingsRow, OWNER_SETTINGS_SINGLETON_ID)
                metadata = self._metadata(row)
                if metadata is None:
                    return None
                assert row is not None
                if row.credential_nonce is None or row.credential_ciphertext is None:
                    raise ProviderSettingsRepositoryError("PROVIDER_SETTINGS_STATE_INVALID")
                try:
                    resolved_value = self._codec.decrypt(
                        EncryptedCredential(
                            schema_version=row.schema_version,
                            nonce=bytes(row.credential_nonce),
                            ciphertext=bytes(row.credential_ciphertext),
                        ),
                        metadata,
                    )
                except CredentialCodecError as error:
                    raise ProviderSettingsRepositoryError(
                        "PROVIDER_SETTINGS_CREDENTIAL_UNAVAILABLE"
                    ) from error
                return ResolvedProviderSettings(metadata, resolved_value)
        except ProviderSettingsRepositoryError:
            raise
        except SQLAlchemyError as error:
            raise ProviderSettingsRepositoryError("PROVIDER_SETTINGS_READ_FAILED") from error

    def replace(
        self,
        *,
        provider: ProviderKind,
        endpoint: str | None,
        model: str,
        value: CredentialSecret,
        trusted_at: datetime,
    ) -> ProviderMetadata:
        trusted_at = _trusted_utc(trusted_at)
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(OwnerProviderSettingsRow)
                    .where(OwnerProviderSettingsRow.singleton_id == OWNER_SETTINGS_SINGLETON_ID)
                    .with_for_update()
                )
                generation = 1 if row is None else row.generation + 1
                metadata = self._new_metadata(provider, endpoint, model, generation)
                encrypted = self._codec.encrypt(value, metadata)
                table = OwnerProviderSettingsRow.__table__
                values = {
                    table.c.schema_version: PROVIDER_SETTINGS_SCHEMA_VERSION,
                    table.c.provider: metadata.provider.value,
                    table.c.endpoint: metadata.endpoint,
                    table.c.model: metadata.model,
                    table.c.generation: metadata.generation,
                    table.c.credential_nonce: encrypted.nonce,
                    table.c.credential_ciphertext: encrypted.ciphertext,
                    table.c.active: True,
                    table.c.updated_at: trusted_at,
                }
                if row is None:
                    session.execute(
                        table.insert().values(
                            {
                                **values,
                                table.c.singleton_id: OWNER_SETTINGS_SINGLETON_ID,
                                table.c.created_at: trusted_at,
                            }
                        )
                    )
                else:
                    session.execute(
                        table.update()
                        .where(table.c.singleton_id == OWNER_SETTINGS_SINGLETON_ID)
                        .values(values)
                    )
                return metadata
        except (ProviderSettingsValidationError, CredentialCodecError):
            raise
        except SQLAlchemyError as error:
            raise ProviderSettingsRepositoryError("PROVIDER_SETTINGS_WRITE_FAILED") from error

    def clear(self, *, trusted_at: datetime) -> int | None:
        trusted_at = _trusted_utc(trusted_at)
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(OwnerProviderSettingsRow)
                    .where(OwnerProviderSettingsRow.singleton_id == OWNER_SETTINGS_SINGLETON_ID)
                    .with_for_update()
                )
                if row is None or not row.active:
                    return None if row is None else row.generation
                generation = row.generation + 1
                table = OwnerProviderSettingsRow.__table__
                session.execute(
                    table.update()
                    .where(table.c.singleton_id == OWNER_SETTINGS_SINGLETON_ID)
                    .values(
                        {
                            table.c.provider: None,
                            table.c.endpoint: None,
                            table.c.model: None,
                            table.c.generation: generation,
                            table.c.credential_nonce: None,
                            table.c.credential_ciphertext: None,
                            table.c.active: False,
                            table.c.updated_at: trusted_at,
                        }
                    )
                )
                return generation
        except SQLAlchemyError as error:
            raise ProviderSettingsRepositoryError("PROVIDER_SETTINGS_CLEAR_FAILED") from error

    def _new_metadata(
        self,
        provider: ProviderKind,
        endpoint: str | None,
        model: str,
        generation: int,
    ) -> ProviderMetadata:
        if provider is ProviderKind.OWNER_GEMINI:
            if endpoint not in {None, GEMINI_ENDPOINT}:
                raise ProviderSettingsValidationError("PROVIDER_SETTINGS_ENDPOINT_INVALID")
            return ProviderMetadata.for_gemini(model=model, generation=generation)
        if provider is ProviderKind.OWNER_OPENAI_COMPATIBLE:
            if endpoint is None:
                raise ProviderSettingsValidationError("PROVIDER_SETTINGS_ENDPOINT_INVALID")
            return ProviderMetadata.for_openai_compatible(
                endpoint=endpoint,
                model=model,
                generation=generation,
                allowed_origins=self._allowed_openai_origins,
            )
        raise ProviderSettingsValidationError("PROVIDER_SETTINGS_PROVIDER_INVALID")

    def _metadata(self, row: OwnerProviderSettingsRow | None) -> ProviderMetadata | None:
        if row is None or not row.active:
            return None
        try:
            metadata = ProviderMetadata(
                schema_version=row.schema_version,
                singleton_id=row.singleton_id,
                provider=ProviderKind(row.provider),
                endpoint=row.endpoint,
                model=row.model,
                generation=row.generation,
            )
            if metadata.provider is ProviderKind.OWNER_OPENAI_COMPATIBLE:
                normalized = normalize_openai_compatible_endpoint(
                    metadata.endpoint,
                    allowed_origins=self._allowed_openai_origins,
                )
                if normalized != metadata.endpoint:
                    raise ProviderSettingsValidationError("PROVIDER_SETTINGS_ENDPOINT_INVALID")
            return metadata
        except (TypeError, ValueError, ProviderSettingsValidationError) as error:
            raise ProviderSettingsRepositoryError("PROVIDER_SETTINGS_STATE_INVALID") from error


def _trusted_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderSettingsRepositoryError("PROVIDER_SETTINGS_TRUSTED_TIME_INVALID")
    return value.astimezone(UTC)
