import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.contracts.v1 import AccountRole
from backend.app.execution import Actor, ExecutionBlocked
from backend.app.persistence import AgentDecisionRepository, SQLAlchemyAgentServiceRepository
from backend.app.persistence.agent_authority import agent_result_material, canonical_agent_hash
from backend.app.persistence.agent_codec import encode_agent_value
from backend.app.persistence.opportunity_authority import (
    OpportunityAuthorityError,
    SQLAlchemyOpportunityAuthorityRepository,
)
from backend.app.persistence.sqlalchemy_models import AccountRoleRow, AgentDecisionRow, Base
from backend.app.services import (
    AcquisitionFailure,
    AcquisitionKind,
    AgentRunService,
    CalibrationBinding,
    DevelopmentAcquisitionRouter,
    DevelopmentRoute,
    DevelopmentRouteAuthority,
    ObservedPaperAccountAuthority,
    OpportunityNoTradeAcquisition,
)
from backend.tests.agent_orchestration.test_agent_run_service import (
    approved_opportunity_bundle,
)

NOW = datetime(2026, 8, 29, 15, 2, 37, tzinfo=UTC)
BOUNDARY = datetime(2026, 8, 28, 20, tzinfo=UTC)
FINGERPRINT = "a" * 64
DEVELOPMENT_FINGERPRINT = "d" * 64


class DatabaseClock:
    def now(self, _session):
        return NOW + timedelta(seconds=1)


class Authority:
    def observe(self):
        return ObservedPaperAccountAuthority(AccountRole.SUBMISSION, FINGERPRINT, True, False)


class DevelopmentAuthority:
    def observe(self):
        return ObservedPaperAccountAuthority(
            AccountRole.DEVELOPMENT,
            DEVELOPMENT_FINGERPRINT,
            True,
            True,
        )


class Clock:
    def now(self):
        return NOW


class Calibration:
    def binding_for(self, _authority):
        return CalibrationBinding(
            AccountRole.SUBMISSION,
            FINGERPRINT,
            "CALIBRATION_BINDING_NO_TRADE",
            "b" * 64,
            "c" * 64,
            BOUNDARY,
            BOUNDARY + timedelta(minutes=1),
        )


class Acquisition:
    def __init__(self):
        self.calls = 0

    async def acquire(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("duplicate tick reached acquisition")


class EmptyPositions:
    def load_development_route(self, *, expected_account_fingerprint):
        assert expected_account_fingerprint == DEVELOPMENT_FINGERPRINT
        return DevelopmentRouteAuthority(
            account_fingerprint=DEVELOPMENT_FINGERPRINT,
            route=DevelopmentRoute.EMPTY,
            active_position_count=0,
            managed_position_id=None,
            position_fingerprint=None,
            authority_hash="e" * 64,
        )


class SchedulerOpportunity:
    def __init__(self) -> None:
        self.calls: list[Actor] = []

    async def acquire(self, _authority, _trusted_at, _tick_id, *, actor):
        self.calls.append(actor)
        raise AcquisitionFailure(AcquisitionKind.OPPORTUNITY, "NO_CANDIDATE")


class DeterministicNoTrade:
    def __init__(self, acquisition: OpportunityNoTradeAcquisition) -> None:
        self.acquisition = acquisition
        self.calls = 0

    async def acquire(self, *_args, **_kwargs):
        self.calls += 1
        return self.acquisition


class Runtime:
    class Execution:
        def execute(self, *_args):
            raise AssertionError("submission reached execution")

    execution = Execution()


def repositories():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            (
                AccountRoleRow(
                    role=AccountRole.SUBMISSION.value,
                    account_fingerprint=FINGERPRINT,
                    equity=Decimal("100000"),
                    autonomous_enabled=False,
                ),
                AccountRoleRow(
                    role=AccountRole.DEVELOPMENT.value,
                    account_fingerprint=DEVELOPMENT_FINGERPRINT,
                    equity=Decimal("100000"),
                    autonomous_enabled=True,
                ),
            )
        )
    low = AgentDecisionRepository(
        sessions, database_clock=DatabaseClock(), server_autonomy_enabled=False
    )
    return low, sessions


def service(adapter, acquisition):
    return AgentRunService(
        account_authority=Authority(),
        clock=Clock(),
        calibration=Calibration(),
        acquisition=acquisition,
        decisions=adapter,
        runtime=Runtime(),
        server_autonomy_enabled=False,
    )


def test_adapter_persists_stable_submission_decision_and_completed_retry() -> None:
    low, _ = repositories()
    acquisition = Acquisition()
    first_adapter = SQLAlchemyAgentServiceRepository(low, server_autonomy_enabled=False)

    first = asyncio.run(service(first_adapter, acquisition).run(Actor.SCHEDULER))
    restarted = SQLAlchemyAgentServiceRepository(low, server_autonomy_enabled=False)
    repeated = asyncio.run(service(restarted, acquisition).run(Actor.SCHEDULER))

    assert repeated == first
    assert first.terminal_code == "CALIBRATION_BINDING_NO_TRADE"
    assert acquisition.calls == 0


def test_restart_reserved_duplicate_fails_before_acquisition_without_token() -> None:
    low, _ = repositories()
    first = SQLAlchemyAgentServiceRepository(low, server_autonomy_enabled=False)
    first.begin_tick(Authority().observe(), Actor.SCHEDULER, NOW)
    acquisition = Acquisition()
    restarted = SQLAlchemyAgentServiceRepository(low, server_autonomy_enabled=False)

    with pytest.raises(ExecutionBlocked, match="AGENT_TICK_IN_PROGRESS"):
        asyncio.run(service(restarted, acquisition).run(Actor.SCHEDULER))

    assert acquisition.calls == 0


def test_future_five_minute_tick_boundary_is_rejected_by_database_clock() -> None:
    low, _ = repositories()
    adapter = SQLAlchemyAgentServiceRepository(low, server_autonomy_enabled=False)

    with pytest.raises(ExecutionBlocked, match="AGENT_TICK_FROM_FUTURE"):
        adapter.begin_tick(Authority().observe(), Actor.OWNER, NOW + timedelta(minutes=10))


def test_adapter_rejects_mismatched_autonomy_construction() -> None:
    low, _ = repositories()

    with pytest.raises(ValueError, match="AGENT_AUTONOMY_CONFIGURATION_MISMATCH"):
        SQLAlchemyAgentServiceRepository(low, server_autonomy_enabled=True)


def test_owner_tick_cannot_consume_same_boundary_scheduler_opportunity() -> None:
    low, _ = repositories()
    adapter = SQLAlchemyAgentServiceRepository(low, server_autonomy_enabled=False)
    opportunity = SchedulerOpportunity()
    lifecycle = Acquisition()
    router = DevelopmentAcquisitionRouter(EmptyPositions(), opportunity, lifecycle)
    target = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=Clock(),
        calibration=object(),
        acquisition=router,
        decisions=adapter,
        runtime=Runtime(),
        server_autonomy_enabled=False,
    )

    owner = asyncio.run(target.run(Actor.OWNER))
    scheduler = asyncio.run(target.run(Actor.SCHEDULER))

    assert owner.tick_id != scheduler.tick_id
    assert owner.decision.provider_failure_code == "OPPORTUNITY_ACQUISITION_SCHEDULER_REQUIRED"
    assert owner.decision.provider_failure_kind is AcquisitionKind.LIFECYCLE
    assert scheduler.decision.provider_failure_code == "NO_CANDIDATE"
    assert scheduler.decision.provider_failure_kind is AcquisitionKind.OPPORTUNITY
    assert opportunity.calls == [Actor.SCHEDULER]
    assert lifecycle.calls == 0


def test_development_no_trade_round_trips_exact_decision_across_restart() -> None:
    from backend.app.policy import evaluate_opportunity

    low, sessions = repositories()
    approved = approved_opportunity_bundle()
    values = replace(
        approved.values,
        market_open=False,
        account=replace(
            approved.values.account,
            book_fingerprint=DEVELOPMENT_FINGERPRINT,
        ),
    )
    expected = evaluate_opportunity(approved.policy, values)
    acquisition = DeterministicNoTrade(
        OpportunityNoTradeAcquisition(approved.policy, values, expected)
    )
    first_adapter = SQLAlchemyAgentServiceRepository(low, server_autonomy_enabled=False)
    first_service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=Clock(),
        calibration=object(),
        acquisition=acquisition,
        decisions=first_adapter,
        runtime=Runtime(),
        server_autonomy_enabled=False,
    )

    first = asyncio.run(first_service.run(Actor.SCHEDULER))
    restarted_adapter = SQLAlchemyAgentServiceRepository(low, server_autonomy_enabled=False)
    restarted_service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=Clock(),
        calibration=object(),
        acquisition=acquisition,
        decisions=restarted_adapter,
        runtime=Runtime(),
        server_autonomy_enabled=False,
    )
    repeated = asyncio.run(restarted_service.run(Actor.SCHEDULER))

    assert repeated == first
    assert first.decision.opportunity == expected
    assert first.decision.normalized_input == values
    assert first.decision.thesis_version_id is None
    assert first.decision.provider_failure_code is None
    assert first.decision.provider_failure_kind is None
    assert acquisition.calls == 1
    tick = low.get_tick(first.tick_id)
    assert tick is not None and tick.decision_id is not None
    persisted = low.get_decision(tick.decision_id)
    assert persisted is not None
    assert persisted.outcome == "NO_TRADE"
    assert persisted.reason_code == "MARKET_CLOSED"
    assert persisted.thesis_version_id is None
    prior = SQLAlchemyOpportunityAuthorityRepository(sessions).load_prior_opportunity_decision(
        expected_account_fingerprint=DEVELOPMENT_FINGERPRINT,
        expected_opportunity_key=values.opportunity_key,
        decision_boundary=values.observed_decision_boundary,
        as_of=NOW + timedelta(seconds=1),
    )
    assert prior.outcome is expected.outcome
    assert prior.reason_code == expected.reason_codes[0].value

    with sessions.begin() as session:
        row = session.get(AgentDecisionRow, tick.decision_id)
        assert row is not None
        substituted_payload = {
            "typed": encode_agent_value(replace(first.decision, code="ENTRY_APPROVED"))
        }
        row.result_payload = substituted_payload
        row.result_hash = canonical_agent_hash(
            agent_result_material(
                input_hash=persisted.input_hash,
                outcome=persisted.outcome,
                reason_code=persisted.reason_code,
                policy_hash=persisted.policy_hash,
                thesis_version_id=None,
                result_payload=substituted_payload,
                authorization_id=None,
                intent_id=None,
                intent_digest=None,
                autonomy_authorized=False,
            )
        )

    with pytest.raises(OpportunityAuthorityError, match="PRIOR_DECISION_PAYLOAD_MISMATCH"):
        SQLAlchemyOpportunityAuthorityRepository(sessions).load_prior_opportunity_decision(
            expected_account_fingerprint=DEVELOPMENT_FINGERPRINT,
            expected_opportunity_key=values.opportunity_key,
            decision_boundary=values.observed_decision_boundary,
            as_of=NOW + timedelta(seconds=1),
        )
