import asyncio
import hashlib
import json
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
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    AgentDecisionRow,
    AgentTickRow,
    Base,
)
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
from backend.app.services.agent import AgentDecision
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


class DatabaseClockAt:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self, _session):
        return self.value


class Authority:
    def observe(self):
        return ObservedPaperAccountAuthority(AccountRole.SUBMISSION, FINGERPRINT, True, False)


class AutonomousSubmissionAuthority:
    def observe(self):
        return ObservedPaperAccountAuthority(AccountRole.SUBMISSION, FINGERPRINT, True, True)


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


class ClockAt:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self):
        return self.value


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


class BoundaryOpportunity:
    def __init__(self, boundary: datetime) -> None:
        self.boundary = boundary
        self.calls: list[datetime] = []

    async def acquire(self, _authority, trusted_at, _tick_id, *, actor):
        assert actor is Actor.SCHEDULER
        self.calls.append(trusted_at)
        if trusted_at < self.boundary:
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY,
                "OPPORTUNITY_DECISION_BOUNDARY_NOT_REACHED",
            )
        raise AcquisitionFailure(AcquisitionKind.OPPORTUNITY, "PROVIDER_UNAVAILABLE")


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


def repositories(
    *,
    server_autonomy_enabled: bool = False,
    database_now: datetime | None = None,
):
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
                    autonomous_enabled=server_autonomy_enabled,
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
        sessions,
        database_clock=(DatabaseClockAt(database_now) if database_now else DatabaseClock()),
        server_autonomy_enabled=server_autonomy_enabled,
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


def record_provider_failure(
    low: AgentDecisionRepository,
    *,
    key: str,
    provider_code: str,
    decision_boundary: datetime,
    observed_at: datetime,
):
    decision = AgentDecision(
        code="PROVIDER_FAILURE_NO_TRADE",
        decided_at=observed_at,
        provider_failure_code=provider_code,
        provider_failure_kind=AcquisitionKind.OPPORTUNITY,
    )
    normalized = {
        "code": decision.code,
        "provider_failure_code": provider_code,
        "provider_failure_kind": AcquisitionKind.OPPORTUNITY.value,
    }
    policy_hash = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    tick = low.reserve_tick(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
        actor=Actor.SCHEDULER.value,
        trusted_at=NOW,
        tick_key=key,
    )
    assert tick.reservation_token is not None
    return low.record_decision(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=decision_boundary,
        observed_at=observed_at,
        normalized_input={"typed": encode_agent_value(normalized)},
        outcome=decision.code,
        reason_code=decision.code,
        policy_hash=policy_hash,
        result_payload={"typed": encode_agent_value(decision)},
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
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


def test_provider_failure_does_not_become_prior_opportunity_decision() -> None:
    low, sessions = repositories(server_autonomy_enabled=True)
    adapter = SQLAlchemyAgentServiceRepository(low, server_autonomy_enabled=True)
    target = AgentRunService(
        account_authority=AutonomousSubmissionAuthority(),
        clock=Clock(),
        calibration=Calibration(),
        acquisition=SchedulerOpportunity(),
        decisions=adapter,
        runtime=Runtime(),
        server_autonomy_enabled=True,
        submission_opportunity_enabled=True,
    )

    failed = asyncio.run(target.run(Actor.SCHEDULER))
    prior = SQLAlchemyOpportunityAuthorityRepository(
        sessions,
        account_role=AccountRole.SUBMISSION,
    ).load_prior_opportunity_decision(
        expected_account_fingerprint=FINGERPRINT,
        expected_opportunity_key="event-1",
        decision_boundary=NOW,
        as_of=NOW + timedelta(seconds=1),
    )

    assert failed.terminal_code == "PROVIDER_FAILURE_NO_TRADE"
    assert prior.outcome is None
    assert prior.decision_id is None


def test_preboundary_tick_is_pending_and_exact_boundary_retries() -> None:
    early = datetime(2026, 9, 2, 13, 45, tzinfo=UTC)
    boundary = datetime(2026, 9, 2, 13, 50, tzinfo=UTC)
    low, sessions = repositories(
        server_autonomy_enabled=True,
        database_now=boundary + timedelta(seconds=1),
    )
    acquisition = BoundaryOpportunity(boundary)

    def run(at: datetime):
        return asyncio.run(
            AgentRunService(
                account_authority=AutonomousSubmissionAuthority(),
                clock=ClockAt(at),
                calibration=Calibration(),
                acquisition=acquisition,
                decisions=SQLAlchemyAgentServiceRepository(
                    low,
                    server_autonomy_enabled=True,
                ),
                runtime=Runtime(),
                server_autonomy_enabled=True,
                submission_opportunity_enabled=True,
            ).run(Actor.SCHEDULER)
        )

    pending = run(early)
    eligible = run(boundary)
    prior = SQLAlchemyOpportunityAuthorityRepository(
        sessions,
        account_role=AccountRole.SUBMISSION,
    ).load_prior_opportunity_decision(
        expected_account_fingerprint=FINGERPRINT,
        expected_opportunity_key="event-1",
        decision_boundary=boundary,
        as_of=boundary + timedelta(seconds=1),
    )

    assert pending.terminal_code == "OPPORTUNITY_DECISION_PENDING"
    assert pending.decision.code == "OPPORTUNITY_DECISION_PENDING"
    assert eligible.terminal_code == "PROVIDER_FAILURE_NO_TRADE"
    assert acquisition.calls == [early, boundary]
    assert prior.outcome is None
    assert prior.decision_id is None


def test_provider_failures_do_not_hide_later_terminal_policy_decision() -> None:
    from backend.app.policy import evaluate_opportunity

    low, sessions = repositories(server_autonomy_enabled=True)
    approved = approved_opportunity_bundle()
    selected_boundary = approved.policy.selected_decision_boundary
    first = record_provider_failure(
        low,
        key="failure-1",
        provider_code="PROVIDER_UNAVAILABLE",
        decision_boundary=selected_boundary,
        observed_at=NOW,
    )
    record_provider_failure(
        low,
        key="failure-2",
        provider_code="OPTION_CHAIN_TIMEOUT",
        decision_boundary=selected_boundary,
        observed_at=NOW + timedelta(milliseconds=100),
    )
    repository = SQLAlchemyOpportunityAuthorityRepository(
        sessions,
        account_role=AccountRole.SUBMISSION,
    )
    missing = repository.load_prior_opportunity_decision(
        expected_account_fingerprint=FINGERPRINT,
        expected_opportunity_key="ACME_EVENT",
        decision_boundary=selected_boundary,
        as_of=NOW + timedelta(seconds=1),
    )
    assert missing.outcome is None
    assert missing.decision_id is None

    values = replace(
        approved.values,
        market_open=False,
        account=replace(
            approved.values.account,
            account_role=AccountRole.SUBMISSION,
            book_fingerprint=FINGERPRINT,
        ),
    )
    outcome = evaluate_opportunity(approved.policy, values)
    decision = AgentDecision(
        code=outcome.outcome.value,
        decided_at=NOW + timedelta(milliseconds=200),
        opportunity=outcome,
        normalized_input=values,
    )
    tick = low.reserve_tick(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
        actor=Actor.SCHEDULER.value,
        trusted_at=NOW,
        tick_key="policy-decision",
    )
    assert tick.reservation_token is not None
    terminal = low.record_decision(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=selected_boundary,
        observed_at=decision.decided_at,
        normalized_input={"typed": encode_agent_value(values)},
        outcome=decision.code,
        reason_code=outcome.reason_codes[0].value,
        policy_hash=outcome.policy_hash,
        result_payload={"typed": encode_agent_value(decision)},
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )
    record_provider_failure(
        low,
        key="failure-after-policy",
        provider_code="LATE_PROVIDER_FAILURE",
        decision_boundary=selected_boundary,
        observed_at=NOW + timedelta(milliseconds=300),
    )

    found = repository.load_prior_opportunity_decision(
        expected_account_fingerprint=FINGERPRINT,
        expected_opportunity_key="ACME_EVENT",
        decision_boundary=selected_boundary,
        as_of=NOW + timedelta(seconds=1),
    )
    assert found.outcome is outcome.outcome
    assert found.decision_id == terminal.decision_id
    assert found.decision_id != first.decision_id

    duplicate_values = replace(values, catalyst_score=values.catalyst_score + 1)
    duplicate_outcome = evaluate_opportunity(approved.policy, duplicate_values)
    duplicate_decision = replace(
        decision,
        opportunity=duplicate_outcome,
        normalized_input=duplicate_values,
    )
    duplicate_tick = low.reserve_tick(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
        actor=Actor.SCHEDULER.value,
        trusted_at=NOW,
        tick_key="second-policy-decision",
    )
    assert duplicate_tick.reservation_token is not None
    with pytest.raises(ExecutionBlocked, match="AGENT_INPUT_BOUNDARY_CONFLICT"):
        low.record_decision(
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=selected_boundary,
            observed_at=duplicate_decision.decided_at,
            normalized_input={"typed": encode_agent_value(duplicate_values)},
            outcome=duplicate_decision.code,
            reason_code=duplicate_outcome.reason_codes[0].value,
            policy_hash=duplicate_outcome.policy_hash,
            result_payload={"typed": encode_agent_value(duplicate_decision)},
            tick_id=duplicate_tick.tick_id,
            reservation_token=duplicate_tick.reservation_token,
        )


def test_unknown_opportunity_outcome_fails_closed() -> None:
    low, sessions = repositories()
    tick = low.reserve_tick(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=DEVELOPMENT_FINGERPRINT,
        actor=Actor.SCHEDULER.value,
        trusted_at=NOW,
        tick_key="unknown-policy-outcome",
    )
    assert tick.reservation_token is not None
    low.record_decision(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=DEVELOPMENT_FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=NOW,
        observed_at=NOW,
        normalized_input={"opportunity_key": "event-1"},
        outcome="UNKNOWN_POLICY_OUTCOME",
        reason_code="UNKNOWN_POLICY_OUTCOME",
        policy_hash="f" * 64,
        result_payload={},
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )

    with pytest.raises(OpportunityAuthorityError, match="PRIOR_DECISION_OUTCOME_INVALID"):
        SQLAlchemyOpportunityAuthorityRepository(sessions).load_prior_opportunity_decision(
            expected_account_fingerprint=DEVELOPMENT_FINGERPRINT,
            expected_opportunity_key="event-1",
            decision_boundary=NOW,
            as_of=NOW + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "corruption",
    ("decision_account", "decision_role", "decision_boundary", "origin_tick"),
)
def test_provider_failure_audit_requires_exact_lineage(corruption: str) -> None:
    low, sessions = repositories(server_autonomy_enabled=True)
    persisted = record_provider_failure(
        low,
        key=f"provider-lineage-{corruption}",
        provider_code="PROVIDER_UNAVAILABLE",
        decision_boundary=NOW,
        observed_at=NOW,
    )
    with sessions.begin() as session:
        decision = session.get(AgentDecisionRow, persisted.decision_id)
        assert decision is not None
        tick = session.get(AgentTickRow, decision.origin_tick_id)
        assert tick is not None
        if corruption == "decision_account":
            decision.account_fingerprint = "e" * 64
        elif corruption == "decision_role":
            decision.account_role = AccountRole.DEVELOPMENT.value
        elif corruption == "decision_boundary":
            decision.decision_boundary = NOW + timedelta(minutes=5)
        else:
            tick.decision_id = None

    with pytest.raises(
        OpportunityAuthorityError,
        match="PRIOR_DECISION_(AUTHORITY_MISMATCH|TICK_LINEAGE_INVALID)",
    ):
        SQLAlchemyOpportunityAuthorityRepository(
            sessions,
            account_role=AccountRole.SUBMISSION,
        ).load_prior_opportunity_decision(
            expected_account_fingerprint=FINGERPRINT,
            expected_opportunity_key="event-1",
            decision_boundary=NOW,
            as_of=NOW + timedelta(seconds=1),
        )


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
