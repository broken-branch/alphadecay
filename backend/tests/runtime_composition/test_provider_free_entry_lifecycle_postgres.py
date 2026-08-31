from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.alpaca.execution_evidence import LifecycleAccountEvidence
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import ActivityItem, ActivityType, Actor, ExecutionCertificate
from backend.app.lifecycle.composition import build_lifecycle_adapters
from backend.app.lifecycle.materialization import SQLAlchemyEntryMaterializer
from backend.app.lifecycle.repository import SQLAlchemyLifecycleRepository
from backend.app.persistence import AgentDecisionRepository, SQLAlchemyAgentServiceRepository
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
    LifecycleObservationManifestRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
    OrderAttemptRow,
    ThesisVersionRow,
    WholeAccountReconciliationRow,
)
from backend.app.services import AgentRunService, ObservedPaperAccountAuthority
from backend.tests.execution_lineage.test_entry_materialization import (
    CERTIFICATE_ID,
    INTENT_ID,
    _repository,
)
from backend.tests.runtime_composition.test_development_acquisition import FINGERPRINT, NOW
from backend.tests.runtime_composition.test_lifecycle_adapters import (
    TimelineAccounts,
    TimelineClassifier,
    TimelineMarkets,
    TimelineMCP,
    TimelineState,
)

POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"
NEW_YORK = ZoneInfo("America/New_York")

pytestmark = pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)


class _AccountAuthority:
    def observe(self) -> ObservedPaperAccountAuthority:
        return ObservedPaperAccountAuthority(
            AccountRole.DEVELOPMENT,
            FINGERPRINT,
            True,
            True,
        )


@dataclass
class _Clock:
    trusted_at: datetime

    def now(self, _session=None):
        return self.trusted_at


class _ForbiddenCalibration:
    def binding_for(self, _authority):
        raise AssertionError("development lifecycle must not read calibration")


@dataclass
class _Accounts:
    state: TimelineState

    def collect(self, *, context, trusted_at) -> LifecycleAccountEvidence:
        observed = TimelineAccounts(self.state).collect(
            context=context,
            trusted_at=trusted_at,
        )
        activity_hashes = tuple(
            activity_hash
            for transition in context.lifecycle_transitions
            for activity_hash in transition.activity_hashes
        )
        activities = tuple(
            ActivityItem(
                activity_id_hash=activity_hash,
                activity_type=ActivityType.OPTRD,
                occurred_at=context.lifecycle_origin_at,
                symbol=position.symbol,
                signed_quantity=position.signed_quantity,
            )
            for activity_hash, position in zip(
                activity_hashes,
                context.expected_positions,
                strict=True,
            )
        )
        return LifecycleAccountEvidence(
            replace(observed.sweep, activities=activities),
            observed.options,
        )


@dataclass
class _Markets:
    state: TimelineState

    def collect(self, *, context, trusted_at):
        observed = TimelineMarkets(self.state).collect(
            context=context,
            trusted_at=trusted_at,
        )
        shift = trusted_at - NOW
        session = observed.boundaries.market_session
        boundaries = replace(
            observed.boundaries,
            market_session=replace(
                session,
                session_date=(session.open_at + shift).astimezone(NEW_YORK).date(),
                open_at=session.open_at + shift,
                close_at=session.close_at + shift,
                retrieved_at=session.retrieved_at + shift,
            ),
            short_call_close_at=observed.boundaries.short_call_close_at + shift,
            weekend_close_at=observed.boundaries.weekend_close_at + shift,
            contest_end_at=observed.boundaries.contest_end_at + shift,
        )
        return replace(observed, boundaries=boundaries)


@dataclass
class _FakeTerminalFill:
    sessions: sessionmaker
    certificate_values: dict[str, object]
    calls: list[tuple[UUID, Actor, object]] = field(default_factory=list)

    def execute(self, intent_id: UUID, actor: Actor, now) -> ExecutionCertificate:
        self.calls.append((intent_id, actor, now))
        assert intent_id == INTENT_ID
        with self.sessions.begin() as session:
            intent = session.get(ExecutionIntentRow, intent_id)
            assert intent is not None
            assert intent.state == "APPROVED"
            intent.state = "TERMINAL"
            intent.first_fill_consumed = True
            session.add(ExecutionCertificateRow(**self.certificate_values))
        return ExecutionCertificate(
            certificate_id=CERTIFICATE_ID,
            intent_id=INTENT_ID,
            entry_approval_id=self.certificate_values["entry_approval_id"],
            assessment_certificate_id=None,
            execution_status="FILLED",
            attempt_ids=tuple(self.certificate_values["attempt_ids"]),
            actual_exposure=None,
            reconciliation_checks=tuple(self.certificate_values["reconciliation_checks"]),
            created_at=now,
        )


@dataclass
class _Runtime:
    execution: _FakeTerminalFill


def test_approved_entry_fill_materializes_then_next_tick_holds_without_another_write() -> None:
    source_sessions, source_engine, retained = _repository()
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"provider_free_vertical_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_engine(os.environ[POSTGRES_URL_ENV])

    @event.listens_for(engine, "connect")
    def set_search_path(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    sessions = sessionmaker(engine, expire_on_commit=False)
    source_models = (
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
        GreekAuthorityVersionRow,
    )
    try:
        apply_migrations(engine, discover_migrations(MIGRATIONS))
        with source_sessions() as source, sessions.begin() as target:
            target.execute(text("SET session_replication_role = replica"))
            for model in source_models:
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
            source_certificate = source.get(ExecutionCertificateRow, CERTIFICATE_ID)
            assert source_certificate is not None
            certificate_values = {
                attribute.key: getattr(source_certificate, attribute.key)
                for attribute in ExecutionCertificateRow.__mapper__.column_attrs
            }
            target.execute(text("SET session_replication_role = origin"))

        with sessions.begin() as session:
            session.execute(text("SET session_replication_role = replica"))
            intent = session.get(ExecutionIntentRow, INTENT_ID)
            assert intent is not None
            assert intent.action == "ENTRY"
            approval = session.get(EntryApprovalCertificateRow, intent.entry_approval_id)
            assert approval is not None
            entry_decision = session.get(AgentDecisionRow, approval.agent_decision_id)
            assert entry_decision is not None
            assert entry_decision.outcome == "ENTRY_APPROVED"
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
        execution = _FakeTerminalFill(sessions, certificate_values)
        fill = execution.execute(INTENT_ID, Actor.SCHEDULER, retained.thesis_frozen_at)
        position_id = materializer.materialize(
            execution_certificate_id=fill.certificate_id,
            launch_authority=retained.launch_authority,
        )

        lifecycle_repository = SQLAlchemyLifecycleRepository(sessions)
        loaded = lifecycle_repository.load(_AccountAuthority().observe())
        assert loaded.managed_position_id == position_id
        assert loaded.position_fingerprint == retained.position_fingerprint

        trusted_at = NOW
        state = TimelineState(trusted_at=trusted_at)
        adapters = build_lifecycle_adapters(
            repository=lifecycle_repository,
            accounts=_Accounts(state),
            markets=_Markets(state),
            mcp_research=TimelineMCP(state),
            classifier=TimelineClassifier(state),
        )
        clock = _Clock(trusted_at)
        decisions = SQLAlchemyAgentServiceRepository(
            AgentDecisionRepository(
                sessions,
                database_clock=clock,
                server_autonomy_enabled=True,
            ),
            server_autonomy_enabled=True,
        )
        result = asyncio.run(
            AgentRunService(
                account_authority=_AccountAuthority(),
                clock=clock,
                calibration=_ForbiddenCalibration(),
                acquisition=adapters.acquisition,
                decisions=decisions,
                runtime=_Runtime(execution),
                server_autonomy_enabled=True,
                entry_materializer=materializer,
            ).run(Actor.SCHEDULER)
        )

        assert result.decision.code == "HOLD_CERTIFIED", result.decision.provider_failure_code
        assert result.terminal_code == "HOLD_CERTIFIED"
        assert result.approved_intent_id is None
        assert result.execution_certificate_id is None
        assert execution.calls == [
            (INTENT_ID, Actor.SCHEDULER, retained.thesis_frozen_at)
        ]
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 1
            assert (
                session.scalar(select(func.count()).select_from(ExecutionCertificateRow)) == 1
            )
            assert (
                session.scalar(select(func.count()).select_from(ManagedLifecyclePositionRow))
                == 1
            )
            assert (
                session.scalar(select(func.count()).select_from(ManagedPositionTransitionRow))
                == 1
            )
            assert (
                session.scalar(select(func.count()).select_from(ManagedPositionSnapshotRow)) == 1
            )
            assert (
                session.scalar(select(func.count()).select_from(LifecycleObservationManifestRow))
                == 1
            )
            assert session.scalar(select(func.count()).select_from(AgentDecisionRow)) == 2
    finally:
        source_engine.dispose()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
