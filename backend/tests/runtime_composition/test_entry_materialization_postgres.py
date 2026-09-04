from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import AccountRole
from backend.app.lifecycle.materialization import SQLAlchemyEntryMaterializer
from backend.app.lifecycle.repository import SQLAlchemyLifecycleRepository
from backend.app.persistence import AgentDecisionRepository
from backend.app.persistence.runtime import apply_migrations, discover_migrations
from backend.app.persistence.sqlalchemy_models import (
    AccountReconciliationStateRow,
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AgentTickRow,
    AlpacaMarketSessionRow,
    AttemptObservationRow,
    BrokerMutationPermitRow,
    EntryApprovalCertificateRow,
    ExecutionCertificateRow,
    ExecutionIntentRow,
    GreekAuthorityVersionRow,
    OrderAttemptRow,
    ThesisVersionRow,
    WholeAccountReconciliationRow,
)
from backend.app.services import ObservedPaperAccountAuthority
from backend.tests.execution_lineage.test_entry_materialization import (
    CERTIFICATE_ID,
    DECISION_ID,
    ENTRY_AT,
    INTENT_ID,
    _repository,
)

POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"

pytestmark = pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)


@pytest.mark.parametrize("account_role", (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION))
def test_terminal_entry_materialization_satisfies_postgres_lineage_guards(
    account_role: AccountRole,
) -> None:
    source_sessions, source_engine, retained = _repository(account_role)
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"entry_materialization_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ[POSTGRES_URL_ENV],
    )

    @event.listens_for(engine, "connect")
    def set_search_path(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    sessions = sessionmaker(engine, expire_on_commit=False)
    models = (
        AccountRoleRow,
        ThesisVersionRow,
        AgentTickRow,
        AgentInputSnapshotRow,
        AgentDecisionRow,
        EntryApprovalCertificateRow,
        ExecutionIntentRow,
        WholeAccountReconciliationRow,
        BrokerMutationPermitRow,
        OrderAttemptRow,
        AttemptObservationRow,
        AccountReconciliationStateRow,
        AlpacaMarketSessionRow,
        ExecutionCertificateRow,
        GreekAuthorityVersionRow,
    )
    try:
        apply_migrations(engine, discover_migrations(MIGRATIONS))
        with source_sessions() as source, sessions.begin() as target:
            target.execute(text("SET session_replication_role = replica"))
            for model in models:
                for row in source.query(model).all():
                    target.add(
                        model(
                            **{
                                attribute.key: getattr(row, attribute.key)
                                for attribute in model.__mapper__.column_attrs
                            }
                        )
                    )
                target.flush()
            target.execute(text("SET session_replication_role = origin"))

        with sessions.begin() as session:
            session.execute(text("SET session_replication_role = replica"))
            intent = session.get(ExecutionIntentRow, INTENT_ID)
            assert intent is not None
            intent.state = "APPROVED"
            intent.first_fill_consumed = False
            session.flush()
            session.execute(text("SET session_replication_role = origin"))
        materializer = SQLAlchemyEntryMaterializer(sessions)
        materializer.prepare(
            execution_intent_id=INTENT_ID,
            launch_authority=retained.launch_authority,
            prepared_at=retained.thesis_frozen_at,
        )
        with (
            pytest.raises(DBAPIError, match="ENTRY_MATERIALIZATION_JOB_IMMUTABLE"),
            sessions.begin() as session,
        ):
            session.execute(
                text(
                    "UPDATE entry_materialization_jobs "
                    "SET job_hash=repeat('9',64) WHERE execution_intent_id=:intent"
                ),
                {"intent": INTENT_ID},
            )
        with sessions.begin() as session:
            session.execute(text("SET session_replication_role = replica"))
            intent = session.get(ExecutionIntentRow, INTENT_ID)
            assert intent is not None
            intent.state = "TERMINAL"
            intent.first_fill_consumed = True
            session.flush()
            session.execute(text("SET session_replication_role = origin"))

        preparation_tick_id = UUID("93000000-0000-0000-0000-000000000001")
        preparation_token = UUID("94000000-0000-0000-0000-000000000001")
        with sessions.begin() as session:
            session.execute(text("SET session_replication_role = replica"))
            session.execute(
                text(
                    "INSERT INTO agent_ticks "
                    "(tick_id,account_role,account_fingerprint,tick_key,tick_boundary,actor,"
                    "status,reservation_token,decision_id,created_at) VALUES "
                    "(:tick,:role,:fingerprint,'entry-preparation-failure',"
                    ":boundary,'SCHEDULER','RESERVED',:token,:decision,:boundary)"
                ),
                {
                    "tick": preparation_tick_id,
                    "role": account_role.value,
                    "fingerprint": retained.account_fingerprint,
                    "boundary": ENTRY_AT,
                    "token": preparation_token,
                    "decision": DECISION_ID,
                },
            )
            session.execute(text("SET session_replication_role = origin"))
        repository = AgentDecisionRepository(sessions)
        preparation_failed = repository.complete_tick(
            tick_id=preparation_tick_id,
            reservation_token=preparation_token,
            terminal_code="ENTRY_MATERIALIZATION_PREPARATION_FAILED",
            decision_id=DECISION_ID,
            execution_certificate_id=None,
        )
        assert preparation_failed.execution_certificate_id is None
        assert preparation_failed.terminal_code == "ENTRY_MATERIALIZATION_PREPARATION_FAILED"

        tick_id = UUID("91000000-0000-0000-0000-000000000001")
        reservation_token = UUID("92000000-0000-0000-0000-000000000001")
        with sessions.begin() as session:
            session.execute(text("SET session_replication_role = replica"))
            session.execute(
                text(
                    "INSERT INTO agent_ticks "
                    "(tick_id,account_role,account_fingerprint,tick_key,tick_boundary,actor,"
                    "status,reservation_token,decision_id,created_at) VALUES "
                    "(:tick,:role,:fingerprint,'entry-materialization-failure',"
                    ":boundary,'SCHEDULER','RESERVED',:token,:decision,:boundary)"
                ),
                {
                    "tick": tick_id,
                    "role": account_role.value,
                    "fingerprint": retained.account_fingerprint,
                    "boundary": ENTRY_AT,
                    "token": reservation_token,
                    "decision": DECISION_ID,
                },
            )
            session.execute(text("SET session_replication_role = origin"))
        completed = repository.complete_tick(
            tick_id=tick_id,
            reservation_token=reservation_token,
            terminal_code="ENTRY_FILLED_MATERIALIZATION_FAILED",
            decision_id=DECISION_ID,
            execution_certificate_id=CERTIFICATE_ID,
        )
        assert completed.execution_certificate_id == CERTIFICATE_ID
        assert completed.terminal_code == "ENTRY_FILLED_MATERIALIZATION_FAILED"
        restarted = AgentDecisionRepository(sessions)
        assert (
            restarted.reserve_tick(
                account_role=account_role,
                account_fingerprint=retained.account_fingerprint,
                actor="SCHEDULER",
                trusted_at=ENTRY_AT,
                tick_key="entry-materialization-failure",
            )
            == completed
        )

        position_id = materializer.materialize(
            execution_certificate_id=CERTIFICATE_ID,
            launch_authority=retained.launch_authority,
        )
        assert (
            materializer.materialize(
                execution_certificate_id=CERTIFICATE_ID,
                launch_authority=retained.launch_authority,
            )
            == position_id
        )
        loaded = SQLAlchemyLifecycleRepository(sessions).load(
            ObservedPaperAccountAuthority(
                account_role,
                retained.account_fingerprint,
                True,
                True,
            )
        )
        assert loaded.managed_position_id == position_id
        assert loaded.position_fingerprint == retained.position_fingerprint
    finally:
        source_engine.dispose()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
