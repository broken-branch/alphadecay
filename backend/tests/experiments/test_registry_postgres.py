from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from backend.app.persistence.runtime import apply_migrations, discover_migrations

POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"

pytestmark = pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)


def test_reviewed_experiment_table_is_reviewed_only_and_immutable() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"experiment_registry_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ[POSTGRES_URL_ENV],
        connect_args={"startup_params": {"search_path": schema}},
    )
    experiment_id = uuid4()
    try:
        apply_migrations(engine, discover_migrations(MIGRATIONS))
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reviewed_experiment_definitions "
                    "(experiment_id, version, definition_hash, lifecycle_state, "
                    "payload_text, created_at) "
                    "VALUES (:id, 1, :hash, 'REVIEWED', '{}', now())"
                ),
                {"id": experiment_id, "hash": "a" * 64},
            )
        with pytest.raises(DBAPIError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE reviewed_experiment_definitions SET payload_text='changed' "
                    "WHERE experiment_id=:id"
                ),
                {"id": experiment_id},
            )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO compiled_experiment_versions "
                    "(experiment_id, source_version, compiled_version, "
                    "source_definition_hash, protocol_hash, lifecycle_state, arm_state, "
                    "automation_state, execution_eligible, payload_text, created_at) "
                    "VALUES (:id, 1, 1, :source_hash, :protocol_hash, 'COMPILED', "
                    "'NOT_ARMED', 'OFF', false, '{}', now())"
                ),
                {
                    "id": experiment_id,
                    "source_hash": "a" * 64,
                    "protocol_hash": "b" * 64,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO experiment_arm_events "
                    "(event_id, experiment_id, source_definition_hash, protocol_hash, "
                    "authorization_revision, action, authorization_state, entry_authorized, "
                    "existing_position_risk_management_preserved, runtime_state, "
                    "execution_eligible, paper_trading_only, event_hash, created_at) "
                    "VALUES (:event_id, :id, :source_hash, :protocol_hash, 1, 'ARM', "
                    "'ARMED', true, true, 'NOT_CONNECTED', false, true, :event_hash, now())"
                ),
                {
                    "event_id": uuid4(),
                    "id": experiment_id,
                    "source_hash": "a" * 64,
                    "protocol_hash": "b" * 64,
                    "event_hash": "c" * 64,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO experiment_arm_states "
                    "(experiment_id, source_definition_hash, protocol_hash, "
                    "authorization_revision, authorization_state, entry_authorized, "
                    "existing_position_risk_management_preserved, runtime_state, "
                    "execution_eligible, paper_trading_only, last_event_hash, updated_at) "
                    "VALUES (:id, :source_hash, :protocol_hash, 1, 'ARMED', true, true, "
                    "'NOT_CONNECTED', false, true, :event_hash, now())"
                ),
                {
                    "id": experiment_id,
                    "source_hash": "a" * 64,
                    "protocol_hash": "b" * 64,
                    "event_hash": "c" * 64,
                },
            )
        with (
            pytest.raises(
                DBAPIError,
                match="invalid experiment arm state transition",
            ),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "UPDATE experiment_arm_states SET authorization_revision=3 "
                    "WHERE experiment_id=:id"
                ),
                {"id": experiment_id},
            )
        with pytest.raises(DBAPIError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text("UPDATE experiment_arm_events SET event_hash=:hash WHERE experiment_id=:id"),
                {"id": experiment_id, "hash": "d" * 64},
            )
        with pytest.raises(DBAPIError, match="cannot be deleted"), engine.begin() as connection:
            connection.execute(
                text("DELETE FROM experiment_arm_states WHERE experiment_id=:id"),
                {"id": experiment_id},
            )
        with pytest.raises(DBAPIError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE compiled_experiment_versions SET payload_text='changed' "
                    "WHERE experiment_id=:id"
                ),
                {"id": experiment_id},
            )
        other_experiment_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reviewed_experiment_definitions "
                    "(experiment_id, version, definition_hash, lifecycle_state, "
                    "payload_text, created_at) "
                    "VALUES (:id, 1, :hash, 'REVIEWED', '{}', now())"
                ),
                {"id": other_experiment_id, "hash": "c" * 64},
            )
        with (
            pytest.raises(DBAPIError, match="source binding is invalid"),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "INSERT INTO compiled_experiment_versions "
                    "(experiment_id, source_version, compiled_version, "
                    "source_definition_hash, protocol_hash, lifecycle_state, arm_state, "
                    "automation_state, execution_eligible, payload_text, created_at) "
                    "VALUES (:id, 1, 1, :source_hash, :protocol_hash, 'COMPILED', "
                    "'NOT_ARMED', 'OFF', false, '{}', now())"
                ),
                {
                    "id": other_experiment_id,
                    "source_hash": "a" * 64,
                    "protocol_hash": "d" * 64,
                },
            )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO compiled_experiment_versions "
                    "(experiment_id, source_version, compiled_version, "
                    "source_definition_hash, protocol_hash, lifecycle_state, arm_state, "
                    "automation_state, execution_eligible, payload_text, created_at) "
                    "VALUES (:id, 1, 1, :source_hash, :protocol_hash, 'COMPILED', "
                    "'NOT_ARMED', 'OFF', false, '{}', now())"
                ),
                {
                    "id": other_experiment_id,
                    "source_hash": "c" * 64,
                    "protocol_hash": "d" * 64,
                },
            )
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO experiment_arm_events "
                    "(event_id, experiment_id, source_definition_hash, protocol_hash, "
                    "authorization_revision, action, authorization_state, entry_authorized, "
                    "existing_position_risk_management_preserved, runtime_state, "
                    "execution_eligible, paper_trading_only, event_hash, created_at) "
                    "VALUES (:event_id, :id, :source_hash, :protocol_hash, 1, 'ARM', "
                    "'ARMED', true, true, 'NOT_CONNECTED', false, true, :event_hash, now())"
                ),
                {
                    "event_id": uuid4(),
                    "id": other_experiment_id,
                    "source_hash": "a" * 64,
                    "protocol_hash": "b" * 64,
                    "event_hash": "e" * 64,
                },
            )
        with pytest.raises(DBAPIError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text("DELETE FROM compiled_experiment_versions WHERE experiment_id=:id"),
                {"id": experiment_id},
            )
        with pytest.raises(DBAPIError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text("DELETE FROM reviewed_experiment_definitions WHERE experiment_id=:id"),
                {"id": experiment_id},
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
