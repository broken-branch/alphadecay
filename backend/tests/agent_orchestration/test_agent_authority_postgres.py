from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pg8000.dbapi import DatabaseError as PG8000DatabaseError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import AccountRole
from backend.app.execution import EntryApprovalAuthorization, ExecutionBlocked
from backend.app.persistence.agent_repository import AgentDecisionRepository
from backend.app.persistence.runtime import apply_migrations, discover_migrations
from backend.app.persistence.sqlalchemy_repository import SQLAlchemyExecutionRepository

POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"
FINGERPRINT = "a" * 64
HASH = "b" * 64

pytestmark = pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)


@pytest.fixture
def postgres_engine():
    database_url = os.environ[POSTGRES_URL_ENV]
    admin_engine = create_engine(database_url)
    if admin_engine.dialect.name != "postgresql":
        pytest.fail(f"{POSTGRES_URL_ENV} must use PostgreSQL")
    schema = f"agent_authority_{uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        database_url,
        connect_args={"startup_params": {"search_path": schema}},
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def _insert_account(connection, *, locked: bool = False) -> None:
    connection.execute(
        text(
            "INSERT INTO account_roles ("
            "role, account_fingerprint, equity, autonomous_enabled, execution_locked, "
            "execution_lock_reason, execution_locked_at, execution_lock_id, "
            "execution_lock_generation) VALUES ("
            "'DEVELOPMENT', :fingerprint, 100000, false, :locked, :reason, :locked_at, "
            ":lock_id, :generation)"
        ),
        {
            "fingerprint": FINGERPRINT,
            "locked": locked,
            "reason": "RECONCILIATION_MISMATCH" if locked else None,
            "locked_at": datetime(2026, 8, 29, tzinfo=UTC) if locked else None,
            "lock_id": uuid4() if locked else None,
            "generation": 1 if locked else 0,
        },
    )


def test_postgres_fresh_install_restart_and_permanent_latch_guards(postgres_engine) -> None:
    migrations = discover_migrations(MIGRATIONS)
    apply_migrations(postgres_engine, migrations)
    postgres_engine.dispose()
    apply_migrations(postgres_engine, migrations)

    with postgres_engine.begin() as connection:
        _insert_account(connection, locked=True)
    with (
        pytest.raises(DBAPIError, match="account execution lock is permanent"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE account_roles SET execution_locked = false, "
                "execution_lock_reason = NULL, execution_locked_at = NULL, "
                "execution_lock_id = NULL WHERE role = 'DEVELOPMENT'"
            )
        )
    for table in ("recovery_cases", "recovery_events", "recovery_certificates"):
        with (
            pytest.raises(DBAPIError, match="account recovery is permanently disabled"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(text(f"INSERT INTO {table} DEFAULT VALUES"))


def test_postgres_stateful_0012_upgrade_refuses_to_invent_thesis_lineage(
    postgres_engine,
) -> None:
    migrations = discover_migrations(MIGRATIONS)
    apply_migrations(postgres_engine, migrations[:11])
    intent_id = uuid4()
    approval_id = uuid4()
    with postgres_engine.begin() as connection:
        _insert_account(connection)
        connection.execute(
            text(
                "INSERT INTO entry_approval_certificates (approval_id, account_role, "
                "policy_hash, book_fingerprint, envelope_hash, approved_max_loss, quantity, "
                "valid_from, expires_at) VALUES (:approval, 'DEVELOPMENT', :hash, :hash, "
                ":hash, 1, 1, now() - interval '1 hour', now() + interval '1 hour')"
            ),
            {"approval": approval_id, "hash": HASH},
        )
        connection.execute(
            text(
                "INSERT INTO execution_intents (intent_id, account_role, intent_digest, action, "
                "policy_hash, event_key, trading_day, entry_approval_id, fingerprint, "
                "envelope_hash, envelope_payload, legs, quantity, minimum_limit, maximum_limit, "
                "approved_max_loss, "
                "state) VALUES (:intent, 'DEVELOPMENT', :zero, 'ENTRY', :hash, 'legacy-baseline', "
                "current_date, :approval, :hash, :hash, '{}'::jsonb, '[]'::jsonb, 1, 0, 0, 1, "
                "'APPROVED')"
            ),
            {"intent": intent_id, "approval": approval_id, "zero": "0" * 64, "hash": HASH},
        )
        connection.execute(
            text(
                "INSERT INTO whole_account_reconciliations (reconciliation_id, "
                "reconciliation_hash, expectation_hash, execution_intent_id, intent_digest, "
                "account_role, account_fingerprint, purpose, attempt_ordinal, request_hash, "
                "accepted_at, expectation_payload, sweep_payload, positions_manifest_hash, "
                "orders_manifest_hash, activities_manifest_hash, safe, block_codes) VALUES "
                "(:id, :hash, :hash2, :intent, :zero, 'DEVELOPMENT', :fingerprint, 'LOCK_CLEAR', "
                "0, :zero, now(), '{}'::jsonb, '{}'::jsonb, :hash3, :hash4, :hash5, true, "
                "'[]'::jsonb)"
            ),
            {
                "id": uuid4(),
                "intent": intent_id,
                "fingerprint": FINGERPRINT,
                "zero": "0" * 64,
                "hash": "1" * 64,
                "hash2": "2" * 64,
                "hash3": "3" * 64,
                "hash4": "4" * 64,
                "hash5": "5" * 64,
            },
        )
    apply_migrations(postgres_engine, migrations[:12])
    with pytest.raises(
        (DBAPIError, PG8000DatabaseError),
        match="LIFECYCLE_AUTHORITY_REQUIRES_VERIFIED_ZERO_HISTORY",
    ):
        apply_migrations(postgres_engine, migrations)


def test_postgres_empty_0012_upgrade_and_restart_are_supported(postgres_engine) -> None:
    migrations = discover_migrations(MIGRATIONS)
    apply_migrations(postgres_engine, migrations[:12])
    apply_migrations(postgres_engine, migrations)
    apply_migrations(postgres_engine, migrations)


def test_postgres_thesis_hash_is_derived_and_orm_entry_write_binds_lineage(
    postgres_engine,
) -> None:
    apply_migrations(postgres_engine, discover_migrations(MIGRATIONS))
    thesis_version_id = uuid4()
    with postgres_engine.begin() as connection:
        _insert_account(connection)
        connection.execute(
            text(
                "INSERT INTO thesis_versions (thesis_version_id, thesis_id, account_role, "
                "version, thesis_hash, policy_hash, underlying, thesis_code, frozen_at, "
                "target_at, intended_exposure, exposure_limits, volatility_view, entry_atm_iv, "
                "approved_max_loss, portfolio_risk_cap, invalidation_codes, thesis_payload, "
                "created_at) VALUES (:version_id, :thesis_id, 'DEVELOPMENT', 1, :caller_hash, "
                ":policy, 'NVDA', 'CATALYST_CONTINUATION', :frozen, :target, '{}'::jsonb, "
                "'{}'::jsonb, 'LONG', 0.4, 500, 500, '[\"GUIDANCE_REVERSED\"]'::jsonb, "
                "jsonb_build_object('frozen', true), :frozen)"
            ),
            {
                "version_id": thesis_version_id,
                "thesis_id": uuid4(),
                "caller_hash": "0" * 64,
                "policy": HASH,
                "frozen": datetime(2026, 8, 29, tzinfo=UTC),
                "target": datetime(2026, 9, 4, tzinfo=UTC),
            },
        )
        assert (
            connection.execute(
                text("SELECT thesis_hash FROM thesis_versions WHERE thesis_version_id=:id"),
                {"id": thesis_version_id},
            ).scalar_one()
            != "0" * 64
        )

    repository = SQLAlchemyExecutionRepository(
        sessionmaker(postgres_engine, expire_on_commit=False)
    )
    approval_id = uuid4()
    repository.add_entry_approval(
        EntryApprovalAuthorization(
            approval_id=approval_id,
            account_role=AccountRole.DEVELOPMENT,
            policy_hash=HASH,
            book_fingerprint="c" * 64,
            envelope_hash="d" * 64,
            approved_max_loss=Decimal("500"),
            quantity=1,
            valid_from=datetime(2026, 8, 29, tzinfo=UTC),
            expires_at=datetime(2026, 8, 30, tzinfo=UTC),
            thesis_version_id=thesis_version_id,
        )
    )
    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT thesis_version_id FROM entry_approval_certificates "
                    "WHERE approval_id=:id"
                ),
                {"id": approval_id},
            ).scalar_one()
            == thesis_version_id
        )


def test_postgres_rejects_invalid_origin_and_serializes_tick_reservation(
    postgres_engine,
) -> None:
    apply_migrations(postgres_engine, discover_migrations(MIGRATIONS))
    tick_id = uuid4()
    reservation_token = uuid4()
    now = datetime.now(UTC) - timedelta(minutes=5)
    boundary = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    with postgres_engine.begin() as connection:
        _insert_account(connection)

    def reserve() -> bool:
        try:
            with postgres_engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO agent_ticks (tick_id, account_role, account_fingerprint, "
                        "tick_key, tick_boundary, actor, status, reservation_token, created_at) "
                        "VALUES (:tick, 'DEVELOPMENT', :fingerprint, 'shared', :boundary, "
                        "'SCHEDULER', 'RESERVED', :token, now())"
                    ),
                    {
                        "tick": tick_id,
                        "fingerprint": FINGERPRINT,
                        "boundary": boundary,
                        "token": reservation_token,
                    },
                )
            return True
        except DBAPIError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: reserve(), range(2))) == [False, True]

    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    repository = AgentDecisionRepository(sessions)

    def decide(candidate: str):
        try:
            return repository.record_decision(
                account_role=AccountRole.DEVELOPMENT,
                account_fingerprint=FINGERPRINT,
                decision_kind="OPPORTUNITY",
                decision_boundary=boundary,
                observed_at=boundary,
                normalized_input={"candidate": candidate},
                outcome="NO_TRADE",
                reason_code="POLICY_REJECTED",
                policy_hash=HASH,
                result_payload={},
                tick_id=tick_id,
                reservation_token=reservation_token,
            )
        except ExecutionBlocked as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(decide, ("SPY", "QQQ")))
    decisions = [result for result in results if not isinstance(result, str)]
    assert len(decisions) == 1
    assert [result for result in results if isinstance(result, str)] == [
        "AGENT_INPUT_BOUNDARY_CONFLICT"
    ]
    completed = repository.complete_tick(
        tick_id=tick_id,
        reservation_token=reservation_token,
        terminal_code="NO_TRADE",
        decision_id=decisions[0].decision_id,
        execution_certificate_id=None,
    )
    restarted = AgentDecisionRepository(sessions)
    assert (
        restarted.reserve_tick(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            actor="SCHEDULER",
            trusted_at=boundary,
            tick_key="shared",
        )
        == completed
    )

    invalid_boundary = boundary - timedelta(minutes=5)
    snapshot_id = uuid4()
    with (
        pytest.raises(DBAPIError, match="agent decision lineage mismatch"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO agent_input_snapshots VALUES (:snapshot, 'DEVELOPMENT', "
                ":fingerprint, 'OPPORTUNITY', :boundary, :boundary, '{}'::jsonb, :hash, now())"
            ),
            {
                "snapshot": snapshot_id,
                "fingerprint": FINGERPRINT,
                "boundary": invalid_boundary,
                "hash": HASH,
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_decisions VALUES (:decision, :wrong_tick, :snapshot, "
                "'DEVELOPMENT', :fingerprint, 'OPPORTUNITY', 'NO_TRADE', 'NO_TRADE', :hash, "
                "'{}'::jsonb, :result_hash, false, :boundary, now())"
            ),
            {
                "decision": uuid4(),
                "wrong_tick": uuid4(),
                "snapshot": snapshot_id,
                "fingerprint": FINGERPRINT,
                "hash": HASH,
                "result_hash": "c" * 64,
                "boundary": invalid_boundary,
            },
        )
