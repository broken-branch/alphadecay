from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.persistence.runtime import apply_migrations, discover_migrations
from backend.app.provider_settings import (
    CredentialCodec,
    CredentialSecret,
    ProviderKind,
    SQLAlchemyProviderSettingsRepository,
)

POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"

pytestmark = pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)


def test_provider_settings_migration_and_generation_guard() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"provider_settings_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ[POSTGRES_URL_ENV],
        connect_args={"startup_params": {"search_path": schema}},
    )
    try:
        migrations = discover_migrations(MIGRATIONS)
        assert [migration.version for migration in migrations] == list(range(1, 27))
        assert migrations[14].filename == "0015_owner_provider_settings.sql"
        apply_migrations(engine, migrations[:14])
        apply_migrations(engine, migrations)
        apply_migrations(engine, migrations)

        with (
            pytest.raises(DBAPIError, match="OWNER_PROVIDER_SETTINGS_MUTATION_INVALID"),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "INSERT INTO owner_provider_settings "
                    "(singleton_id, schema_version, provider, endpoint, model, generation, "
                    "credential_nonce, credential_ciphertext, active, created_at, updated_at) "
                    "VALUES ('owner-ai-provider', 1, 'OWNER_GEMINI', "
                    "'https://generativelanguage.googleapis.com/v1beta', 'model', 2, "
                    ":nonce, :ciphertext, true, now(), now())"
                ),
                {"nonce": b"n" * 12, "ciphertext": b"c" * 17},
            )

        repository = SQLAlchemyProviderSettingsRepository(
            sessionmaker(engine, expire_on_commit=False),
            codec=CredentialCodec(b"provider-settings-postgres-secret"),
        )
        stored = repository.replace(
            provider=ProviderKind.OWNER_GEMINI,
            endpoint=None,
            model="gemini-3.7-flash",
            value=CredentialSecret.from_text("provider-key"),
            trusted_at=datetime(2026, 8, 29, 18, 0, tzinfo=UTC),
        )
        assert stored.generation == 1
        resolved = repository.resolve()
        assert resolved is not None
        assert resolved.credential.reveal_text() == "provider-key"

        repository.clear(trusted_at=datetime(2026, 8, 29, 18, 1, tzinfo=UTC))
        assert repository.resolve() is None
        with (
            pytest.raises(DBAPIError, match="OWNER_PROVIDER_SETTINGS_MUTATION_INVALID"),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "UPDATE owner_provider_settings "
                    "SET generation = generation + 2, updated_at = now()"
                )
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
