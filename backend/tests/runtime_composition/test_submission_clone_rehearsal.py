from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import AccountRole
from backend.app.execution import ActivityItem, ActivityType, Actor, InventoryItem, InventoryKind
from backend.app.lifecycle.repository import (
    LifecyclePersistenceError,
    SQLAlchemyLifecycleRepository,
)
from backend.app.lifecycle.terminal_materialization import SQLAlchemyLifecycleTerminalMaterializer
from backend.app.order_limits import EntryBudgetLimits
from backend.app.persistence import (
    AgentDecisionRepository,
    SQLAlchemyAgentServiceRepository,
    SQLAlchemyExecutionRepository,
)
from backend.app.persistence.sqlalchemy_models import (
    ManagedLifecyclePositionRow,
    ManagedPositionTransitionRow,
)
from backend.app.services import (
    AgentRunService,
    CalibrationBinding,
    DevelopmentLifecycleAcquisition,
    ExecutionService,
    ObservedPaperAccountAuthority,
)
from backend.tests.runtime_composition.test_development_acquisition import ObservationSource
from backend.tests.runtime_composition.test_submission_structural_vertical_postgres import (
    _Clock,
    _FilledBroker,
    _FilledSweepPort,
    _ForbiddenClassifier,
    _ForbiddenResearch,
    _lifecycle_observation,
)

DATABASE_URL_ENV = "ALPHADECAY_REHEARSAL_POSTGRES_URL"
pytestmark = pytest.mark.skipif(
    DATABASE_URL_ENV not in os.environ,
    reason="requires the cloned launch database URL",
)


@dataclass(frozen=True)
class _Authority:
    authority: ObservedPaperAccountAuthority

    def observe(self) -> ObservedPaperAccountAuthority:
        return self.authority


@dataclass(frozen=True)
class _Calibration:
    def binding_for(self, authority):
        return CalibrationBinding(
            account_role=authority.role,
            account_fingerprint=authority.account_fingerprint,
            decision_code="CALIBRATION_BINDING_NO_TRADE",
            machine_binding_hash="a" * 64,
            calibration_hash="b" * 64,
            decision_boundary=datetime(2026, 9, 3, 13, 50, tzinfo=UTC),
            sealed_at=datetime(2026, 9, 3, 13, 50, 5, tzinfo=UTC),
        )


@dataclass(frozen=True)
class _Runtime:
    execution: ExecutionService


class _ManifestForwarder:
    def __init__(self, target):
        self.target = target

    def persist(self, **record):
        try:
            return self.target.persist(**record)
        except Exception as error:
            raise LifecyclePersistenceError("MANIFEST_PERSISTENCE_FAILED") from error


def _inventory(values) -> tuple[InventoryItem, ...]:
    return tuple(
        InventoryItem(
            InventoryKind.OPTION,
            item["symbol"] if isinstance(item, dict) else item.symbol,
            Decimal(item["signed_quantity"]) if isinstance(item, dict) else item.signed_quantity,
            int(item["multiplier"]) if isinstance(item, dict) else item.multiplier,
        )
        for item in values
    )


def _clone_observation(observation, sessions):
    from backend.app.persistence.sqlalchemy_models import AlpacaMarketSessionRow

    with sessions() as session:
        row = session.scalar(
            select(AlpacaMarketSessionRow).where(
                AlpacaMarketSessionRow.session_date
                == observation.boundaries.market_session.session_date
            )
        )
        if row is None:
            return observation
        market = replace(
            observation.boundaries.market_session,
            market_session_id=row.market_session_id,
            source_hash=row.source_hash,
            request_hash=row.request_hash,
            open_at=row.open_at,
            close_at=row.close_at,
            retrieved_at=row.retrieved_at,
        )
    return replace(observation, boundaries=replace(observation.boundaries, market_session=market))


def test_submission_clone_hold_then_mandatory_close() -> None:
    engine = create_engine(os.environ[DATABASE_URL_ENV])
    sessions = sessionmaker(engine, expire_on_commit=False)
    lifecycle = SQLAlchemyLifecycleRepository(sessions)
    with sessions() as session:
        row = session.scalar(
            select(ManagedLifecyclePositionRow).where(
                ManagedLifecyclePositionRow.account_role == AccountRole.SUBMISSION.value,
                ManagedLifecyclePositionRow.closed_at.is_(None),
            )
        )
        assert row is not None
        fingerprint = row.account_fingerprint
        assert row.account_fingerprint == fingerprint

    authority = _Authority(
        ObservedPaperAccountAuthority(AccountRole.SUBMISSION, fingerprint, True, True)
    )
    retained = lifecycle.load(authority.observe())
    with sessions() as session:
        from backend.app.persistence.sqlalchemy_models import AccountReconciliationStateRow

        state = session.scalar(
            select(AccountReconciliationStateRow)
            .where(AccountReconciliationStateRow.account_role == AccountRole.SUBMISSION.value)
            .order_by(AccountReconciliationStateRow.sequence.desc())
        )
        assert state is not None
        starting_cash = state.expected_cash
        starting_positions = _inventory(state.expected_positions)
        known_activities = tuple(
            ActivityItem(
                activity_id_hash=item["activity_id_hash"],
                activity_type=ActivityType(item["activity_type"]),
                occurred_at=datetime.fromisoformat(item["occurred_at"]),
                symbol=item["symbol"],
                signed_quantity=Decimal(item["signed_quantity"]),
                provider_order_id=item["provider_order_id"],
                client_order_id=item["client_order_id"],
                time_quality=item["time_quality"],
                provider_activity_type=item["provider_activity_type"],
            )
            for item in state.known_activities
        )

    clock = _Clock(datetime(2026, 9, 4, 13, 44, 50, tzinfo=UTC))
    hold_observation = _clone_observation(
        _lifecycle_observation(
            retained,
            clock.value,
            Decimal("1.50"),
            cash=starting_cash,
        ),
        sessions,
    )
    hold = DevelopmentLifecycleAcquisition(
        lifecycle,
        ObservationSource(hold_observation),
        _ForbiddenResearch(),
        _ForbiddenClassifier(),
        lifecycle,
    )
    limits = EntryBudgetLimits(
        policy_hash=retained.policy_hash,
        equity_floor=Decimal("0"),
        maximum_lifetime_entries=1,
        maximum_lifetime_risk=Decimal("1500"),
        maximum_position_loss=Decimal("1500"),
        maximum_entry_quantity=100,
    )
    decisions = SQLAlchemyAgentServiceRepository(
        AgentDecisionRepository(sessions, database_clock=clock, server_autonomy_enabled=True),
        server_autonomy_enabled=True,
    )
    execution_repository = SQLAlchemyExecutionRepository(
        sessions, entry_limits=limits, trusted_clock=clock
    )
    hold_result = asyncio.run(
        AgentRunService(
            account_authority=authority,
            clock=clock,
            calibration=_Calibration(),
            acquisition=hold,
            decisions=decisions,
            runtime=_Runtime(None),
            server_autonomy_enabled=True,
            submission_opportunity_enabled=True,
        ).run(Actor.SCHEDULER)
    )
    assert hold_result.decision.code == "HOLD_CERTIFIED", hold_result.decision.provider_failure_code
    assert hold_result.terminal_code == "HOLD_CERTIFIED"

    close_at = datetime(2026, 9, 4, 13, 45, 10, tzinfo=UTC)
    close_observation = _clone_observation(
        _lifecycle_observation(
            retained,
            close_at,
            Decimal("1.50"),
            cash=starting_cash,
        ),
        sessions,
    )
    close_broker = _FilledBroker(Decimal("150"))
    close_execution = ExecutionService(
        execution_repository,
        close_broker,
        _FilledSweepPort(
            clock=clock,
            baseline_at=retained.account_lifecycle_origin_at,
            broker=close_broker,
            starting_cash=starting_cash,
            starting_positions=starting_positions,
            known_activities=known_activities,
            activity_complete_through=state.activity_complete_through,
        ),
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=fingerprint,
    )
    clock.value = close_at
    close = DevelopmentLifecycleAcquisition(
        lifecycle,
        ObservationSource(close_observation),
        _ForbiddenResearch(),
        _ForbiddenClassifier(),
        _ManifestForwarder(lifecycle),
    )
    result = asyncio.run(
        AgentRunService(
            account_authority=authority,
            clock=clock,
            calibration=_Calibration(),
            acquisition=close,
            decisions=decisions,
            runtime=_Runtime(close_execution),
            server_autonomy_enabled=True,
            submission_opportunity_enabled=True,
            lifecycle_terminal_materializer=SQLAlchemyLifecycleTerminalMaterializer(sessions),
        ).run(Actor.SCHEDULER)
    )
    assert result.decision.code == "CLOSE_RISK_ONLY", result.decision.provider_failure_code
    assert result.terminal_code == "FILLED"
    assert close_broker.envelope is not None
    assert close_broker.envelope.action.value == "CLOSE"
    with sessions() as session:
        position = session.scalar(select(ManagedLifecyclePositionRow))
        assert position is not None and position.closed_at is not None
        assert session.scalar(select(func.count()).select_from(ManagedPositionTransitionRow)) == 2
    engine.dispose()
