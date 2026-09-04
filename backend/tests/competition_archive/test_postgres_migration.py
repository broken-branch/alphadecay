from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from backend.app.persistence.runtime import apply_migrations, discover_migrations

POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"

pytestmark = pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)


def test_competition_archive_migration_installs_exact_source_guards() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"competition_archive_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ[POSTGRES_URL_ENV],
        connect_args={"startup_params": {"search_path": schema}},
    )
    try:
        migrations = discover_migrations(MIGRATIONS)
        apply_migrations(engine, migrations)
        with engine.connect() as connection:
            functions = set(
                connection.execute(
                    text(
                        "SELECT p.proname FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname=current_schema() "
                        "AND p.proname LIKE 'competition_record_%'"
                    )
                ).scalars()
            )
            assert {
                "competition_record_expected_projection",
                "competition_record_events",
                "competition_record_exposure",
                "competition_record_intent_matches_snapshot",
                "competition_record_public_id",
                "competition_record_source_hash",
                "competition_record_spread",
                "competition_record_utc_text",
            } <= functions
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid=t.tgrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=current_schema() "
                        "AND c.relname='competition_record_publications' "
                        "AND NOT t.tgisinternal"
                    )
                ).scalar_one()
                == 2
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
