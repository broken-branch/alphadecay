import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import sleep
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.alpaca.market_data import NormalizedGreeks, NormalizedOptionSnapshot
from backend.app.contracts.v1 import AccountRole, GreekExposure, PositionIntent
from backend.app.execution import (
    AccountObservation,
    ActivityItem,
    ActivityPaginationEvidence,
    ActivityType,
    Actor,
    AmbiguousBrokerResponse,
    AttemptObservationSource,
    BrokerResult,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionBlocked,
    ExecutionPending,
    ExecutionPendingCode,
    FrozenThesisVersion,
    InventoryItem,
    InventoryKind,
    OpenOrderItem,
    OpenOrderLeg,
    OrderEnvelope,
    OrderLegIntent,
    SweepObservation,
    intent_digest,
    order_envelope_hash,
)
from backend.app.execution.models import PositionGreekObservation
from backend.app.order_limits import EntryBudgetLimits
from backend.app.persistence import SQLAlchemyExecutionRepository
from backend.app.persistence.agent_authority import (
    agent_input_material,
    agent_result_material,
    canonical_agent_hash,
)
from backend.app.persistence.sqlalchemy_models import (
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AgentTickRow,
    Base,
    EntryApprovalCertificateRow,
)
from backend.app.services import (
    AcquisitionFailure,
    AcquisitionKind,
    AgentDecision,
    AgentRunResult,
    AgentRunService,
    AgentTick,
    CalibrationBinding,
    ExecutionService,
    ObservedPaperAccountAuthority,
    PermanentAccountLatch,
    PersistedAgentDecision,
    WholeAccountEvidence,
)
from ops.launch.submission_market_window import (
    MarketWindowSchedule,
    RuntimeDependencies,
    run_window,
)

INTENT_ID = UUID("00000000-0000-0000-0000-000000000201")
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-000000000202")
SECOND_INTENT_ID = UUID("00000000-0000-0000-0000-000000000203")
SECOND_AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-000000000204")
FINGERPRINT = "a" * 64
POLICY_HASH = "a" * 64
ENTRY_LIMITS = EntryBudgetLimits(
    policy_hash=POLICY_HASH,
    equity_floor=Decimal("99000"),
    maximum_lifetime_entries=3,
    maximum_lifetime_risk=Decimal("1500"),
    maximum_position_loss=Decimal("800"),
    maximum_entry_quantity=4,
)


def _fixture_client_reference(ordinal: int) -> str:
    return f"ad-20260903-e-bbecc98d27f37ca59b49f37b-a{ordinal}"


CLIENT_A0 = _fixture_client_reference(0)
CLIENT_A1 = _fixture_client_reference(1)


def _execution_service(repository, broker, preflight, quotes=None) -> ExecutionService:
    return ExecutionService(
        repository,
        broker,
        preflight,
        quotes,
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
    )


class CallTrap:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.calls.append(name)
        raise AssertionError(f"unexpected collaborator call: {name}")


class ClaimBlockedRepository:
    def claim_intent(
        self,
        intent_id: UUID,
        actor: Actor,
        *,
        now: datetime,
        account_role: AccountRole,
        account_fingerprint: str,
    ) -> object:
        del intent_id, actor, now, account_role, account_fingerprint
        raise ExecutionBlocked("INTENT_NOT_APPROVED")

    def plan_broker_mutation(self, claim: object, purpose: object) -> object:
        raise AssertionError(f"unexpected plan: {claim}, {purpose}")


def test_execution_service_stops_before_provider_when_claim_is_blocked() -> None:
    broker = CallTrap()
    preflight = CallTrap()
    service = _execution_service(ClaimBlockedRepository(), broker, preflight)

    with pytest.raises(ExecutionBlocked, match="INTENT_NOT_APPROVED"):
        service.execute(
            UUID("00000000-0000-0000-0000-000000000201"),
            Actor.OWNER,
            datetime(2026, 9, 3, 16, 0, tzinfo=UTC),
        )

    assert broker.calls == []
    assert preflight.calls == []


def test_advance_requires_bound_observed_identity_before_repository_access() -> None:
    repository = CallTrap()
    service = _execution_service(repository, CallTrap(), CallTrap())

    with pytest.raises(ExecutionBlocked, match="OBSERVED_ACCOUNT_AUTHORITY_MISMATCH"):
        service.advance(
            INTENT_ID,
            Actor.SCHEDULER,
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint="b" * 64,
        )

    assert repository.calls == []


def test_advance_claims_authority_before_getting_intent() -> None:
    service = _execution_service(ClaimBlockedRepository(), CallTrap(), CallTrap())

    with pytest.raises(ExecutionBlocked, match="INTENT_NOT_APPROVED"):
        service.advance(
            INTENT_ID,
            Actor.SCHEDULER,
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
        )


def test_adjusted_entry_stops_before_any_broker_or_preflight_call() -> None:
    order = replace(
        envelope(),
        legs=(
            OrderLegIntent("DEMO1260918C00100000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO1260918C00105000", PositionIntent.SELL_TO_OPEN, 1),
        ),
    )
    calls: list[str] = []

    class Repository:
        def claim_intent(self, *args, **kwargs):
            del args, kwargs
            calls.append("claim")
            return SimpleNamespace(
                state=SimpleNamespace(value="CLAIMED"),
                claimed_by=Actor.SCHEDULER,
                envelope=order,
            )

        def next_broker_mutation(self, _claim):
            calls.append("schedule")
            raise AssertionError("schedule must not be read for an unsupported contract")

    broker = CallTrap()
    preflight = CallTrap()
    service = _execution_service(Repository(), broker, preflight)

    with pytest.raises(ExecutionBlocked) as raised:
        service.advance(
            INTENT_ID,
            Actor.SCHEDULER,
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
        )

    assert str(raised.value) == "NON_STANDARD_CONTRACT_UNSUPPORTED"
    assert calls == ["claim"]
    assert broker.calls == []
    assert preflight.calls == []


def test_fresh_preflight_equity_at_stop_latches_before_provider_write() -> None:
    repo, baseline_at = authorized_repository()
    broker = CallTrap()
    service = _execution_service(repo, broker, EquityStopSweepPort(baseline_at))

    with pytest.raises(ExecutionBlocked, match="BROKER_PREFLIGHT_BLOCKED"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert broker.calls == []
    lock = repo.get_execution_lock(AccountRole.SUBMISSION)
    assert lock.locked is True
    assert lock.reason == "ENTRY_EQUITY_FLOOR"


def test_execution_service_finalizes_rejected_submit_through_permit_authority() -> None:
    repo, baseline_at = authorized_repository()
    broker = RejectingBroker()
    service = _execution_service(repo, broker, FreshSweepPort(baseline_at))

    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == "REJECTED"
    assert certificate.actual_exposure is None
    assert certificate.reconciliation_id is not None
    assert certificate.reconciliation_hash is not None
    assert certificate.last_observation_hash is not None
    assert certificate.attempt_ids == (CLIENT_A0,)
    assert broker.submitted_envelope == envelope()
    assert broker.submitted_client_id == certificate.attempt_ids[0]
    assert repo.get_execution_certificate(certificate.certificate_id) == certificate
    assert repo.attempts_for(INTENT_ID)[0].state == "REJECTED"


@pytest.mark.parametrize("state", ["CANCELED", "EXPIRED"])
def test_execution_service_finalizes_other_zero_fill_terminal_states(
    state: str,
) -> None:
    repo, baseline_at = authorized_repository()
    service = _execution_service(
        repo,
        ZeroFillTerminalBroker(state),
        FreshSweepPort(baseline_at),
    )

    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == state
    assert certificate.actual_exposure is None


def test_execution_service_finalizes_a_full_calculated_fill() -> None:
    repo, baseline_at = authorized_repository()
    service = _execution_service(
        repo,
        CalculatedFilledBroker(),
        FilledSweepPort(baseline_at),
    )

    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == "FILLED"
    assert repo.attempts_for(INTENT_ID)[0].state == "CALCULATED"


def test_ambiguous_submit_uses_exact_lookup_without_redispatch() -> None:
    repo, baseline_at = authorized_repository()
    broker = AmbiguousRejectingBroker()
    service = _execution_service(repo, broker, FreshSweepPort(baseline_at))

    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == "REJECTED"
    assert broker.submitted_client_id == certificate.attempt_ids[0]
    assert broker.looked_up_client_id == certificate.attempt_ids[0]
    observations = repo.get_attempt_observations(INTENT_ID)
    assert len(observations) == 1
    assert observations[0].source == AttemptObservationSource.TARGETED_LOOKUP
    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is False


def test_ambiguous_submit_absence_remains_lookup_only_and_cannot_redispatch() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = AmbiguousAbsentBroker()
    service = _execution_service(repo, broker, FreshSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionPending) as first:
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    assert first.value.code is ExecutionPendingCode.LOOKUP_ABSENT

    clock.advance(timedelta(minutes=5))
    with pytest.raises(ExecutionPending) as retried:
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)

    assert retried.value.code is ExecutionPendingCode.LOOKUP_ABSENT
    assert broker.submit_calls == 1
    assert broker.lookup_calls == 2
    assert len(repo.attempts_for(INTENT_ID)) == 1


@pytest.mark.parametrize("first_lookup", ("failure", "absence"))
def test_ambiguous_submit_recovers_cancelled_no_fill_by_lookup_only(
    first_lookup: str,
) -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = AmbiguousSubmitThenCanceledBroker(first_lookup)
    service = _execution_service(repo, broker, FreshSweepPort(baseline_at, clock))

    expected = {
        "failure": ExecutionPendingCode.LOOKUP_DEFERRED,
        "absence": ExecutionPendingCode.LOOKUP_ABSENT,
    }[first_lookup]
    with pytest.raises(ExecutionPending) as pending:
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    assert pending.value.code is expected

    clock.advance(timedelta(minutes=5))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)

    assert certificate.execution_status == "CANCELED"
    assert certificate.actual_exposure is None
    assert broker.submit_calls == 1
    assert broker.lookup_calls == 2
    assert len(repo.attempts_for(INTENT_ID)) == 1


def test_ambiguous_submit_lookup_new_continues_to_later_terminal_fill() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = AmbiguousSubmitNewThenFilledBroker()
    service = _execution_service(repo, broker, FilledSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionPending) as pending:
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    assert pending.value.code is ExecutionPendingCode.ADVANCE

    with pytest.raises(ExecutionPending) as cadence_wait:
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    assert cadence_wait.value.code is ExecutionPendingCode.ADVANCE
    assert broker.lookup_calls == 1

    clock.advance(timedelta(minutes=5))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)

    assert certificate.execution_status == "FILLED"
    assert broker.submit_calls == 1
    assert broker.lookup_calls == 2
    assert len(repo.attempts_for(INTENT_ID)) == 1


def test_reconciled_ambiguous_entry_fill_keeps_lifecycle_execution_available() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = AmbiguousSubmitAbsentThenFilledBroker()
    service = _execution_service(repo, broker, FilledSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionPending) as pending:
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    assert pending.value.code is ExecutionPendingCode.LOOKUP_ABSENT

    clock.advance(timedelta(minutes=5))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)

    assert certificate.execution_status == "FILLED"
    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is False


def test_crash_after_dispatch_leaves_attempt_non_redispatchable() -> None:
    repo, baseline_at = authorized_repository()
    broker = CrashingBroker()
    service = _execution_service(repo, broker, FreshSweepPort(baseline_at))

    with pytest.raises(SimulatedProcessCrash):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    with pytest.raises(ExecutionPending) as still_dispatching:
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    assert still_dispatching.value.code is ExecutionPendingCode.ADVANCE

    assert broker.submit_calls == 1
    assert repo.attempts_for(INTENT_ID)[0].state == "PREPARED"
    assert repo.get_attempt_observations(INTENT_ID) == ()


def test_filled_submit_persists_exposure_and_evolves_expected_book() -> None:
    repo, baseline_at = authorized_repository()
    service = _execution_service(repo, FilledBroker(), FilledSweepPort(baseline_at))

    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == "FILLED"
    assert certificate.actual_exposure == GreekExposure(
        delta=Decimal("60"),
        gamma=Decimal("2"),
        theta_per_day=Decimal("-6"),
        vega_per_iv_point=Decimal("10"),
    )
    state = repo.get_reconciliation_state(AccountRole.SUBMISSION)
    assert state.expected_cash == Decimal("99760")
    assert state.expected_positions == filled_positions()


def test_fill_activity_visible_on_second_final_sweep_is_adopted_without_redispatch() -> None:
    repo, baseline_at = authorized_repository()
    broker = CountingFilledBroker()
    service = _execution_service(repo, broker, LaggedFillActivitySweepPort(baseline_at))

    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == "FILLED"
    assert broker.submit_calls == 1
    state = repo.get_reconciliation_state(AccountRole.SUBMISSION)
    assert state.resolved_activity_hashes == ("c" * 64, "d" * 64)


@pytest.mark.parametrize(
    "sweep_port_name",
    ["provider", "client"],
)
def test_fill_activity_for_unrelated_order_latches_account(
    sweep_port_name: str,
) -> None:
    repo, baseline_at = authorized_repository()
    sweep_port = {
        "provider": UnrelatedFillActivitySweepPort,
        "client": WrongClientFillActivitySweepPort,
    }[sweep_port_name]
    service = _execution_service(repo, FilledBroker(), sweep_port(baseline_at))

    with pytest.raises(ExecutionBlocked, match="FINAL_RECONCILIATION_BLOCKED"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is True


def test_reconciled_open_position_allows_second_entry_preflight() -> None:
    repo, baseline_at = authorized_repository()
    _execution_service(repo, FilledBroker(), FilledSweepPort(baseline_at)).execute(
        INTENT_ID,
        Actor.SCHEDULER,
        datetime.now(UTC),
    )
    approve_second_entry(repo)
    broker = CallTrap()

    with pytest.raises(AssertionError, match="unexpected collaborator call: submit"):
        _execution_service(repo, broker, ExistingFilledBookSweepPort(baseline_at)).execute(
            SECOND_INTENT_ID,
            Actor.SCHEDULER,
            datetime.now(UTC),
        )

    assert broker.calls == ["submit"]
    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is False


def test_partial_fill_is_canceled_by_persisted_provider_identity_and_reconciled() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = PartialThenCanceledBroker()
    service = _execution_service(repo, broker, PartialSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    clock.advance(timedelta(seconds=30))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == "PARTIAL_CANCELED_RECONCILED"
    assert certificate.actual_exposure == GreekExposure(
        delta=Decimal("30"),
        gamma=Decimal("1"),
        theta_per_day=Decimal("-3"),
        vega_per_iv_point=Decimal("5"),
    )
    assert broker.canceled_provider_order_id == "f1"
    attempt = repo.attempts_for(INTENT_ID)[0]
    assert attempt.state == "CANCELED"
    assert attempt.filled_quantity == 1
    state = repo.get_reconciliation_state(AccountRole.SUBMISSION)
    assert state.expected_cash == Decimal("99880")
    assert state.expected_positions == partial_positions()
    lock = repo.get_execution_lock(AccountRole.SUBMISSION)
    assert lock.locked is True
    assert lock.reason == "UNMANAGED_PARTIAL_EXPOSURE"


def test_cancel_race_to_full_fill_reconciles_and_certifies() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = CancelRaceFilledBroker()
    service = _execution_service(repo, broker, CancelRaceFilledSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    clock.advance(timedelta(seconds=30))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == "FILLED"
    assert broker.cancel_calls == 1
    assert repo.attempts_for(INTENT_ID)[0].state == "FILLED"


def test_pending_cancel_is_polled_without_redispatch() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = PendingThenCanceledBroker()
    service = _execution_service(repo, broker, PartialSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    clock.advance(timedelta(seconds=30))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == "PARTIAL_CANCELED_RECONCILED"
    assert broker.cancel_calls == 1
    assert broker.lookup_calls == 3


@pytest.mark.parametrize(
    ("broker_kind", "expected_code"),
    (
        ("PENDING", ExecutionPendingCode.CANCEL_PENDING),
        ("ACTIVE", ExecutionPendingCode.CANCEL_NOT_TERMINAL),
    ),
)
def test_continuing_cancel_states_use_typed_pending_boundary(
    broker_kind: str,
    expected_code: ExecutionPendingCode,
) -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = AlwaysPendingCancelBroker() if broker_kind == "PENDING" else ActiveAfterCancelBroker()
    service = _execution_service(repo, broker, PartialSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionPending):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    clock.advance(timedelta(seconds=30))
    with pytest.raises(ExecutionPending) as continuing:
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert continuing.value.code is expected_code


def test_active_submit_waits_for_database_due_boundary_without_replacement() -> None:
    repo, baseline_at = authorized_repository()
    broker = NewThenFilledBroker()
    service = _execution_service(repo, broker, ReplacementSweepPort(baseline_at))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    attempts = repo.attempts_for(INTENT_ID)
    assert len(attempts) == 1
    assert attempts[0].state == "NEW"
    assert broker.replaced_provider_order_id is None


def test_fill_after_pending_submit_is_found_before_replacement() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = PendingSubmitBroker(("FILLED",))
    service = _execution_service(repo, broker, FilledSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionPending):
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    clock.advance(timedelta(seconds=30))

    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)

    assert certificate.execution_status == "FILLED"
    assert broker.lookup_calls == 1
    assert broker.replace_calls == 0
    assert broker.cancel_calls == 0
    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is False


def test_pending_submit_is_looked_up_then_replaced_on_existing_schedule() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = PendingSubmitBroker(("PENDING_NEW",))
    service = _execution_service(
        repo,
        broker,
        ReplacementSweepPort(baseline_at, clock),
        ReplacementQuotes(clock),
    )

    with pytest.raises(ExecutionPending):
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    clock.advance(timedelta(seconds=150))

    result = service.advance(
        INTENT_ID,
        Actor.SCHEDULER,
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
    )

    assert result.status == "REPLACED"
    assert broker.lookup_calls == 1
    assert broker.replace_calls == 1
    assert broker.cancel_calls == 0


def test_pending_submit_lookup_respects_durable_cadence() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = PendingSubmitBroker(("PENDING_NEW", "PENDING_NEW"))
    service = _execution_service(repo, broker, FreshSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionPending):
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    clock.advance(timedelta(seconds=30))
    with pytest.raises(ExecutionPending):
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    with pytest.raises(ExecutionPending):
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)

    assert broker.lookup_calls == 1
    observations = repo.get_attempt_observations(INTENT_ID)
    assert (
        tuple(item.source for item in observations).count(AttemptObservationSource.TARGETED_LOOKUP)
        == 1
    )


def test_pending_replace_advances_by_lookup_without_mutation_or_redispatch() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = PendingReplaceBroker(("PENDING_REPLACE", "NEW"))
    service = _execution_service(repo, broker, FreshSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    assert broker.lookup_calls == 0
    clock.advance(timedelta(seconds=30))
    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    clock.advance(timedelta(seconds=30))
    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert repo.attempts_for(INTENT_ID)[0].state == "NEW"
    assert broker.submit_calls == 1
    assert broker.lookup_calls == 2
    assert broker.replace_calls == 0
    assert broker.cancel_calls == 0


@pytest.mark.parametrize("terminal_state", ["REPLACED", "CANCELED"])
def test_pending_replace_lookup_can_reach_a_zero_fill_terminal_state(
    terminal_state: str,
) -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = PendingReplaceBroker((terminal_state,))
    service = _execution_service(repo, broker, FreshSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    clock.advance(timedelta(seconds=30))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == terminal_state
    assert broker.submit_calls == 1
    assert broker.lookup_calls == 1
    assert broker.replace_calls == 0
    assert broker.cancel_calls == 0


def test_pending_replace_lookup_can_reach_a_full_fill() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = PendingReplaceBroker(("FILLED",), filled=2)
    service = _execution_service(repo, broker, FilledSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    clock.advance(timedelta(seconds=30))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert certificate.execution_status == "FILLED"
    assert broker.lookup_calls == 1
    assert broker.replace_calls == 0
    assert broker.cancel_calls == 0


def test_pending_replace_deadline_latches_but_keeps_read_only_reconciliation() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = PendingReplaceBroker(("PENDING_REPLACE", "REPLACED"))
    service = _execution_service(repo, broker, FreshSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    clock.advance(timedelta(minutes=10))
    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    lock = repo.get_execution_lock(AccountRole.SUBMISSION)
    assert lock.locked is True
    assert lock.reason == "BROKER_TRANSITION_STALLED"
    clock.advance(timedelta(seconds=30))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    assert certificate.execution_status == "REPLACED"
    assert broker.submit_calls == 1
    assert broker.lookup_calls == 2
    assert broker.replace_calls == 0
    assert broker.cancel_calls == 0


def test_transitional_lookup_transport_failure_obeys_durable_cadence() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = TransportFailureThenReplacedBroker()
    service = _execution_service(repo, broker, FreshSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    clock.advance(timedelta(seconds=30))
    with pytest.raises(ExecutionPending) as lookup_failure:
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    assert lookup_failure.value.code is ExecutionPendingCode.LOOKUP_DEFERRED
    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    assert broker.lookup_calls == 1
    assert repo.get_attempt_observations(INTENT_ID)[-1].source is (
        AttemptObservationSource.TARGETED_LOOKUP_FAILURE
    )

    clock.advance(timedelta(seconds=30))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    assert certificate.execution_status == "REPLACED"
    assert broker.lookup_calls == 2


def test_active_order_does_not_spin_through_replacements_or_cancel() -> None:
    repo, baseline_at = authorized_repository()
    broker = AlwaysActiveBroker()
    service = _execution_service(repo, broker, DeterministicActiveSweepPort(baseline_at, 4))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    attempts = repo.attempts_for(INTENT_ID)
    assert tuple(attempt.ordinal for attempt in attempts) == (0,)
    assert broker.replaced_provider_references == ()
    assert broker.canceled_provider_order_id is None


def test_active_submit_does_not_reach_ambiguous_replace_before_due() -> None:
    repo, baseline_at = authorized_repository()
    broker = AmbiguousReplacingBroker()
    service = _execution_service(repo, broker, DeterministicActiveSweepPort(baseline_at, 1))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    attempts = repo.attempts_for(INTENT_ID)
    assert len(attempts) == 1
    assert broker.replace_calls == 0
    assert broker.looked_up_client_id is None


def test_ambiguous_cancel_fails_closed_before_lookup_horizon_or_redispatch() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = AmbiguousCancelBroker()
    service = _execution_service(repo, broker, PartialSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionBlocked, match="EXECUTION_ADVANCE_PENDING"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))
    clock.advance(timedelta(seconds=30))
    with pytest.raises(ExecutionPending) as raised:
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert raised.value.code is ExecutionPendingCode.CANCEL_LOOKUP_DEFERRED
    assert broker.cancel_calls == 1
    assert broker.looked_up_order_id is None
    assert repo.attempts_for(INTENT_ID)[0].state == "PARTIALLY_FILLED"


def test_ambiguous_cancel_pending_boundary_allows_later_certificate_progression() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = RecoveringAmbiguousCancelBroker()
    service = _execution_service(repo, broker, PartialSweepPort(baseline_at, clock))

    with pytest.raises(ExecutionPending) as submitted:
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    assert submitted.value.code is ExecutionPendingCode.ADVANCE

    clock.advance(timedelta(seconds=30))
    with pytest.raises(ExecutionPending) as cancel_unknown:
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    assert cancel_unknown.value.code is ExecutionPendingCode.CANCEL_LOOKUP_DEFERRED

    clock.advance(timedelta(seconds=31))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)

    assert certificate.execution_status == "PARTIAL_CANCELED_RECONCILED"
    assert broker.cancel_calls == 1
    assert broker.lookup_calls == 2


def test_ambiguous_cancel_lookup_recovers_full_fill_without_redispatch() -> None:
    clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(clock)
    broker = AmbiguousCancelThenFilledBroker()
    service = _execution_service(
        repo,
        broker,
        CancelRaceFilledSweepPort(baseline_at, clock),
    )

    with pytest.raises(ExecutionPending) as submitted:
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    assert submitted.value.code is ExecutionPendingCode.ADVANCE

    clock.advance(timedelta(seconds=30))
    with pytest.raises(ExecutionPending) as cancel_unknown:
        service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)
    assert cancel_unknown.value.code is ExecutionPendingCode.CANCEL_LOOKUP_DEFERRED

    clock.advance(timedelta(seconds=31))
    certificate = service.execute(INTENT_ID, Actor.SCHEDULER, clock.value)

    assert certificate.execution_status == "FILLED"
    assert broker.submitted_client_id == CLIENT_A0
    assert broker.cancel_calls == 1
    assert broker.lookup_calls == 2
    assert len(repo.attempts_for(INTENT_ID)) == 1


def test_market_window_continues_real_ambiguous_cancel_recovery_to_certificate() -> None:
    database_clock = MutableDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo, baseline_at = authorized_repository(database_clock)
    controller_now = database_clock.value + timedelta(minutes=1)
    broker = AmbiguousCancelThenFilledBroker()
    execution = _execution_service(
        repo,
        broker,
        CancelRaceFilledSweepPort(baseline_at, database_clock),
    )

    with pytest.raises(ExecutionPending) as submitted:
        execution.execute(INTENT_ID, Actor.SCHEDULER, database_clock.value)
    assert submitted.value.code is ExecutionPendingCode.ADVANCE

    class ControllerClock:
        value = controller_now

        def now(self) -> datetime:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += timedelta(seconds=seconds)

    class Process:
        stopped = False

        def poll(self) -> int | None:
            return 0 if self.stopped else None

        def terminate(self) -> None:
            self.stopped = True

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            self.stopped = True

    class Authority:
        def observe(self) -> ObservedPaperAccountAuthority:
            return ObservedPaperAccountAuthority(
                role=AccountRole.SUBMISSION,
                account_fingerprint=FINGERPRINT,
                paper=True,
                persistent_autonomy_enabled=True,
            )

    class Calibration:
        def binding_for(self, authority) -> CalibrationBinding:
            return CalibrationBinding(
                account_role=authority.role,
                account_fingerprint=authority.account_fingerprint,
                decision_code="CALIBRATION_BINDING_NO_TRADE",
                machine_binding_hash="b" * 64,
                calibration_hash="c" * 64,
                decision_boundary=controller_now - timedelta(days=1),
                sealed_at=controller_now - timedelta(days=1),
            )

    class Acquisition:
        calls = 0
        kinds: list[AcquisitionKind] = []

        async def acquire(self, *_args, **_kwargs):
            self.calls += 1
            kind = (
                AcquisitionKind.LIFECYCLE
                if repo.get_intent(INTENT_ID).state.value == "TERMINAL"
                else AcquisitionKind.OPPORTUNITY
            )
            self.kinds.append(kind)
            raise AcquisitionFailure(kind, "PROVIDER_UNAVAILABLE")

    class Decisions:
        def __init__(self) -> None:
            self.decisions: list[AgentDecision] = []
            self.completed_codes: list[str] = []
            self.previews: list[UUID] = []

        def begin_tick(self, authority, actor, trusted_at):
            key = f"{actor.value}:{trusted_at.isoformat()}"
            return AgentTick(
                uuid5(NAMESPACE_URL, f"tick:{key}"),
                uuid5(NAMESPACE_URL, f"reservation:{key}"),
                authority,
                actor,
                trusted_at,
            )

        def permanent_latch(self, _authority):
            return PermanentAccountLatch(False)

        def persist_decision(self, _tick, decision, proposal):
            assert proposal is None
            self.decisions.append(decision)
            return PersistedAgentDecision(decision, None)

        def complete_tick(self, tick, terminal_code, certificate):
            self.completed_codes.append(terminal_code)
            return AgentRunResult(
                tick.tick_id,
                terminal_code,
                self.decisions[-1],
                None,
                certificate.certificate_id if certificate is not None else None,
                "d" * 64,
            )

        def submission_order_preview(self, intent_id):
            self.previews.append(intent_id)
            return object()

        def pending_submission_lifecycle_intents(self, _authority):
            return ()

    class Materializer:
        def recover_pending(self, **_kwargs):
            return ()

        def pending_execution_intents(self, **_kwargs):
            intent = repo.get_intent(INTENT_ID)
            return () if intent.state.value == "TERMINAL" else (INTENT_ID,)

        def prepare(self, **_kwargs):
            raise AssertionError("new entry preparation is forbidden during recovery")

        def materialize(self, **_kwargs):
            raise AssertionError("certificate recovery belongs to the durable materializer")

    controller_clock = ControllerClock()
    process = Process()
    acquisition = Acquisition()
    decisions = Decisions()
    agent = AgentRunService(
        account_authority=Authority(),
        clock=SimpleNamespace(now=lambda: controller_clock.now().astimezone(UTC)),
        calibration=Calibration(),
        acquisition=acquisition,
        decisions=decisions,
        runtime=SimpleNamespace(execution=execution),
        server_autonomy_enabled=True,
        submission_opportunity_enabled=True,
        entry_materializer=Materializer(),
    )
    accepted_codes: list[str] = []

    def tick_sender(_environment) -> str:
        database_clock.value = controller_clock.now().astimezone(UTC)
        result = asyncio.run(agent.run(Actor.SCHEDULER))
        accepted_codes.append(result.terminal_code)
        return result.terminal_code

    run_window(
        MarketWindowSchedule(
            runtime_config=SimpleNamespace(),
            window_start=controller_now,
            hard_cutoff=controller_now + timedelta(minutes=10),
            cadence=timedelta(minutes=5),
        ),
        SimpleNamespace(environment={"SCHEDULER_TOKEN": "dummy-scheduler-token"}),
        RuntimeDependencies(
            now=controller_clock.now,
            sleep=controller_clock.sleep,
            port_is_clear=lambda: True,
            spawn=lambda _command: process,
            readiness_probe=lambda: True,
            tick_sender=tick_sender,
            emit=lambda _event, _at: None,
        ),
    )

    certificate = repo.get_execution_certificate(
        uuid5(NAMESPACE_URL, f"alphadecay:execution:{repo.get_intent(INTENT_ID).digest}")
    )
    assert accepted_codes == [
        "ENTRY_EXECUTION_RECOVERY_PENDING",
        "PROVIDER_FAILURE_NO_ACTION",
    ]
    assert decisions.completed_codes == accepted_codes
    assert decisions.previews == [INTENT_ID, INTENT_ID]
    assert acquisition.calls == 1
    assert acquisition.kinds == [AcquisitionKind.LIFECYCLE]
    assert certificate.execution_status == "FILLED"
    assert broker.cancel_calls == 1
    assert broker.lookup_calls == 2
    assert process.stopped is True


def test_unsafe_initial_sweep_latches_account_before_any_broker_write() -> None:
    repo, baseline_at = authorized_repository()
    broker = CallTrap()
    service = _execution_service(repo, broker, UnexpectedInventorySweepPort(baseline_at))

    with pytest.raises(ExecutionBlocked, match="BROKER_PREFLIGHT_BLOCKED"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert broker.calls == []
    assert repo.attempts_for(INTENT_ID) == ()
    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is True


def test_unexpected_final_inventory_latches_account_without_certificate() -> None:
    repo, baseline_at = authorized_repository()
    service = _execution_service(
        repo,
        RejectingBroker(),
        FinalUnexpectedInventorySweepPort(baseline_at),
    )

    with pytest.raises(ExecutionBlocked, match="FINAL_RECONCILIATION_BLOCKED"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is True


def test_filled_inventory_without_greek_evidence_latches_without_certificate() -> None:
    repo, baseline_at = authorized_repository()
    service = _execution_service(
        repo,
        FilledBroker(),
        MissingGreekSweepPort(baseline_at),
    )

    with pytest.raises(ExecutionBlocked, match="FINAL_RECONCILIATION_BLOCKED"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is True


@pytest.mark.parametrize("mode", ["stale", "unsynchronized", "old_source"])
def test_stale_or_unsynchronized_greek_evidence_latches_without_certificate(
    mode: str,
) -> None:
    repo, baseline_at = authorized_repository()
    service = _execution_service(
        repo,
        FilledBroker(),
        InvalidGreekTimingSweepPort(baseline_at, mode),
    )

    with pytest.raises(ExecutionBlocked, match="FINAL_RECONCILIATION_BLOCKED"):
        service.execute(INTENT_ID, Actor.SCHEDULER, datetime.now(UTC))

    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is True


@pytest.mark.parametrize("multiplier", [True, 100.0])
def test_position_greek_evidence_requires_an_integer_contract_multiplier(
    multiplier: object,
) -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="POSITION_GREEK_OBSERVATION_INVALID"):
        PositionGreekObservation(
            symbol="DEMO260918C00100000",
            signed_quantity=Decimal("1"),
            multiplier=multiplier,
            delta=Decimal("0.6"),
            gamma=Decimal("0.02"),
            theta_per_day=Decimal("-0.05"),
            vega_per_iv_point=Decimal("0.1"),
            feed="indicative",
            source_timestamp=now - timedelta(milliseconds=1),
            retrieved_at=now,
            source_hash="c" * 64,
        )


@pytest.mark.parametrize(
    ("field", "value", "block_code"),
    [
        ("feed", "opra", "POSITION_GREEK_FEED_INVALID"),
        ("source_hash", "not-a-hash", "POSITION_GREEK_SOURCE_HASH_INVALID"),
    ],
)
def test_position_greek_evidence_requires_indicative_hashed_source(
    field: str,
    value: object,
    block_code: str,
) -> None:
    now = datetime.now(UTC)
    values = {
        "symbol": "DEMO260918C00100000",
        "signed_quantity": Decimal("1"),
        "multiplier": 100,
        "delta": Decimal("0.6"),
        "gamma": Decimal("0.02"),
        "theta_per_day": Decimal("-0.05"),
        "vega_per_iv_point": Decimal("0.1"),
        "feed": "indicative",
        "source_timestamp": now - timedelta(milliseconds=1),
        "retrieved_at": now,
        "source_hash": "c" * 64,
    }
    values[field] = value

    with pytest.raises(ValueError, match=block_code):
        PositionGreekObservation(**values)


class RejectingBroker:
    def __init__(self) -> None:
        self.submitted_envelope: OrderEnvelope | None = None
        self.submitted_client_id: str | None = None

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        return BrokerResult("f1", "REJECTED", 0, request.quantity)

    def lookup(self, client_id: str) -> BrokerResult | None:
        raise AssertionError(f"unexpected lookup: {client_id}")

    def lookup_order(self, provider_order_id: str) -> BrokerResult | None:
        raise AssertionError(f"unexpected order lookup: {provider_order_id}")

    def replace(self, provider_order_id: str, client_id: str, limit: Decimal) -> BrokerResult:
        raise AssertionError(f"unexpected replace: {provider_order_id}, {client_id}, {limit}")

    def cancel(self, provider_order_id: str) -> BrokerResult:
        raise AssertionError(f"unexpected cancel: {provider_order_id}")


class AmbiguousRejectingBroker(RejectingBroker):
    def __init__(self) -> None:
        super().__init__()
        self.looked_up_client_id: str | None = None

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        if self.submitted_client_id is not None:
            raise AssertionError("submit was retried")
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        raise AmbiguousBrokerResponse("SUBMIT_OUTCOME_UNKNOWN")

    def lookup(self, client_id: str) -> BrokerResult | None:
        self.looked_up_client_id = client_id
        return BrokerResult("f1", "REJECTED", 0, envelope().quantity)


class AmbiguousAbsentBroker(RejectingBroker):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0
        self.lookup_calls = 0

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        self.submit_calls += 1
        raise AmbiguousBrokerResponse("SUBMIT_OUTCOME_UNKNOWN")

    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == self.submitted_client_id
        self.lookup_calls += 1
        return None


class AmbiguousSubmitThenCanceledBroker(RejectingBroker):
    def __init__(self, first_lookup: str) -> None:
        super().__init__()
        self.first_lookup = first_lookup
        self.submit_calls = 0
        self.lookup_calls = 0

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submit_calls += 1
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        raise AmbiguousBrokerResponse("SUBMIT_OUTCOME_UNKNOWN")

    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == self.submitted_client_id
        self.lookup_calls += 1
        if self.lookup_calls == 1:
            if self.first_lookup == "failure":
                raise AmbiguousBrokerResponse("LOOKUP_TRANSPORT_FAILED")
            return None
        return BrokerResult("f1", "CANCELED", 0, envelope().quantity)


class AmbiguousSubmitNewThenFilledBroker(RejectingBroker):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0
        self.lookup_calls = 0

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submit_calls += 1
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        raise AmbiguousBrokerResponse("SUBMIT_OUTCOME_UNKNOWN")

    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == self.submitted_client_id
        self.lookup_calls += 1
        if self.lookup_calls == 1:
            return BrokerResult("f1", "NEW", 0, envelope().quantity)
        return BrokerResult(
            "f1",
            "FILLED",
            envelope().quantity,
            envelope().quantity,
            fill_cash_flow=Decimal("-240"),
        )


class AmbiguousSubmitAbsentThenFilledBroker(AmbiguousSubmitNewThenFilledBroker):
    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == self.submitted_client_id
        self.lookup_calls += 1
        if self.lookup_calls == 1:
            return None
        return BrokerResult(
            "f1",
            "FILLED",
            envelope().quantity,
            envelope().quantity,
            fill_cash_flow=Decimal("-240"),
        )


class SimulatedProcessCrash(RuntimeError):
    pass


class CrashingBroker(RejectingBroker):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        self.submit_calls += 1
        raise SimulatedProcessCrash


class ZeroFillTerminalBroker(RejectingBroker):
    def __init__(self, state: str) -> None:
        super().__init__()
        self._state = state

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        return BrokerResult("f1", self._state, 0, request.quantity)


class FilledBroker(RejectingBroker):
    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        return BrokerResult(
            "f1",
            "FILLED",
            request.quantity,
            request.quantity,
            fill_cash_flow=Decimal("-240"),
        )


class CalculatedFilledBroker(FilledBroker):
    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        result = super().submit(request, client_id)
        return replace(result, state="CALCULATED")


class CountingFilledBroker(FilledBroker):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submit_calls += 1
        return super().submit(request, client_id)


class PartialThenCanceledBroker(RejectingBroker):
    def __init__(self) -> None:
        super().__init__()
        self.canceled_provider_order_id: str | None = None
        self.lookup_calls = 0

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        return BrokerResult(
            "f1",
            "PARTIALLY_FILLED",
            1,
            request.quantity,
            fill_cash_flow=Decimal("-120"),
        )

    def cancel(self, provider_order_id: str) -> BrokerResult:
        self.canceled_provider_order_id = provider_order_id
        return BrokerResult(
            provider_order_id,
            "CANCELED",
            1,
            envelope().quantity,
            fill_cash_flow=Decimal("-120"),
        )

    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == CLIENT_A0
        self.lookup_calls += 1
        return BrokerResult(
            "f1",
            "PARTIALLY_FILLED",
            1,
            envelope().quantity,
            fill_cash_flow=Decimal("-120"),
        )


class CancelRaceFilledBroker(PartialThenCanceledBroker):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0

    def cancel(self, provider_order_id: str) -> BrokerResult:
        self.cancel_calls += 1
        return BrokerResult(
            provider_order_id,
            "FILLED",
            envelope().quantity,
            envelope().quantity,
            fill_cash_flow=Decimal("-240"),
        )


class PendingThenCanceledBroker(PartialThenCanceledBroker):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0

    def cancel(self, provider_order_id: str) -> BrokerResult:
        self.cancel_calls += 1
        return BrokerResult(
            provider_order_id,
            "PENDING_CANCEL",
            1,
            envelope().quantity,
            fill_cash_flow=Decimal("-120"),
        )

    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == CLIENT_A0
        self.lookup_calls += 1
        states = ("PARTIALLY_FILLED", "PENDING_CANCEL", "CANCELED")
        state = states[min(self.lookup_calls - 1, len(states) - 1)]
        return BrokerResult(
            "f1",
            state,
            1,
            envelope().quantity,
            fill_cash_flow=Decimal("-120"),
        )


class AlwaysPendingCancelBroker(PendingThenCanceledBroker):
    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == CLIENT_A0
        self.lookup_calls += 1
        state = "PARTIALLY_FILLED" if self.lookup_calls == 1 else "PENDING_CANCEL"
        return BrokerResult(
            "f1",
            state,
            1,
            envelope().quantity,
            fill_cash_flow=Decimal("-120"),
        )


class ActiveAfterCancelBroker(PartialThenCanceledBroker):
    def cancel(self, provider_order_id: str) -> BrokerResult:
        return BrokerResult(
            provider_order_id,
            "PARTIALLY_FILLED",
            1,
            envelope().quantity,
            fill_cash_flow=Decimal("-120"),
        )


class PendingReplaceBroker(RejectingBroker):
    def __init__(self, lookup_states: tuple[str, ...], *, filled: int = 0) -> None:
        super().__init__()
        self.submit_calls = 0
        self.lookup_calls = 0
        self.replace_calls = 0
        self.cancel_calls = 0
        self._lookup_states = list(lookup_states)
        self._filled = filled

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submit_calls += 1
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        return BrokerResult("f1", "PENDING_REPLACE", 0, request.quantity)

    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == self.submitted_client_id
        self.lookup_calls += 1
        state = self._lookup_states.pop(0)
        cash_flow = Decimal("-240") if self._filled else None
        return BrokerResult("f1", state, self._filled, envelope().quantity, cash_flow)

    def replace(self, provider_order_id: str, client_id: str, limit: Decimal) -> BrokerResult:
        self.replace_calls += 1
        raise AssertionError("lookup-only state must not be replaced")

    def cancel(self, provider_order_id: str) -> BrokerResult:
        self.cancel_calls += 1
        raise AssertionError("lookup-only state must not be canceled")


class TransportFailureThenReplacedBroker(PendingReplaceBroker):
    def __init__(self) -> None:
        super().__init__(("REPLACED",))

    def lookup(self, client_id: str) -> BrokerResult | None:
        if self.lookup_calls == 0:
            self.lookup_calls += 1
            raise AmbiguousBrokerResponse("LOOKUP_TRANSPORT_FAILED")
        return super().lookup(client_id)


class NewThenFilledBroker(RejectingBroker):
    def __init__(self) -> None:
        super().__init__()
        self.replaced_provider_order_id: str | None = None
        self.replacement_client_id: str | None = None
        self.replacement_limit: Decimal | None = None

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        return BrokerResult("f1", "NEW", 0, request.quantity)

    def replace(self, provider_order_id: str, client_id: str, limit: Decimal) -> BrokerResult:
        self.replaced_provider_order_id = provider_order_id
        self.replacement_client_id = client_id
        self.replacement_limit = limit
        return BrokerResult(
            "f1",
            "FILLED",
            envelope().quantity,
            envelope().quantity,
            fill_cash_flow=Decimal("-240"),
        )


class PendingSubmitBroker(RejectingBroker):
    def __init__(self, lookup_states: tuple[str, ...]) -> None:
        super().__init__()
        self._lookup_states = list(lookup_states)
        self.lookup_calls = 0
        self.replace_calls = 0
        self.cancel_calls = 0

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        return BrokerResult("f1", "PENDING_NEW", 0, request.quantity)

    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == self.submitted_client_id
        self.lookup_calls += 1
        state = self._lookup_states.pop(0)
        filled = envelope().quantity if state == "FILLED" else 0
        cash_flow = Decimal("-240") if state == "FILLED" else None
        return BrokerResult("f1", state, filled, envelope().quantity, cash_flow)

    def replace(self, provider_order_id: str, client_id: str, limit: Decimal) -> BrokerResult:
        assert provider_order_id == "f1"
        self.replace_calls += 1
        return BrokerResult("f2", "NEW", 0, envelope().quantity)

    def cancel(self, provider_order_id: str) -> BrokerResult:
        self.cancel_calls += 1
        return BrokerResult(provider_order_id, "CANCELED", 0, envelope().quantity)


class AlwaysActiveBroker(RejectingBroker):
    def __init__(self) -> None:
        super().__init__()
        self.replaced_provider_references: tuple[str, ...] = ()
        self.canceled_provider_order_id: str | None = None

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        return BrokerResult("f1", "NEW", 0, request.quantity)

    def replace(self, provider_order_id: str, client_id: str, limit: Decimal) -> BrokerResult:
        assert limit == envelope().minimum_limit
        self.replaced_provider_references += (provider_order_id,)
        return BrokerResult("f1", "NEW", 0, envelope().quantity)

    def cancel(self, provider_order_id: str) -> BrokerResult:
        self.canceled_provider_order_id = provider_order_id
        return BrokerResult(provider_order_id, "CANCELED", 0, envelope().quantity)


class AmbiguousReplacingBroker(RejectingBroker):
    def __init__(self) -> None:
        super().__init__()
        self.replace_calls = 0
        self.looked_up_client_id: str | None = None

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.submitted_envelope = request
        self.submitted_client_id = client_id
        return BrokerResult("f1", "NEW", 0, request.quantity)

    def replace(self, provider_order_id: str, client_id: str, limit: Decimal) -> BrokerResult:
        assert provider_order_id == "f1"
        assert limit == envelope().minimum_limit
        self.replace_calls += 1
        if self.replace_calls > 1:
            raise AssertionError("replace was redispatched")
        self.looked_up_client_id = client_id
        raise AmbiguousBrokerResponse("REPLACE_OUTCOME_UNKNOWN")

    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == self.looked_up_client_id
        return BrokerResult("f1", "REJECTED", 0, envelope().quantity)


class AmbiguousCancelBroker(PartialThenCanceledBroker):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0
        self.looked_up_order_id: str | None = None

    def cancel(self, provider_order_id: str) -> BrokerResult:
        assert provider_order_id == "f1"
        self.cancel_calls += 1
        if self.cancel_calls > 1:
            raise AssertionError("cancel was redispatched")
        raise AmbiguousBrokerResponse("CANCEL_OUTCOME_UNKNOWN")

    def lookup_order(self, provider_order_id: str) -> BrokerResult | None:
        self.looked_up_order_id = provider_order_id
        raise AssertionError("cancel lookup ran before the authority horizon")


class RecoveringAmbiguousCancelBroker(AmbiguousCancelBroker):
    def __init__(self) -> None:
        super().__init__()
        self.lookup_calls = 0

    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == CLIENT_A0
        self.lookup_calls += 1
        state = "PARTIALLY_FILLED" if self.lookup_calls == 1 else "CANCELED"
        return BrokerResult(
            "f1",
            state,
            1,
            envelope().quantity,
            fill_cash_flow=Decimal("-120"),
        )


class AmbiguousCancelThenFilledBroker(AmbiguousCancelBroker):
    def __init__(self) -> None:
        super().__init__()
        self.lookup_calls = 0

    def lookup(self, client_id: str) -> BrokerResult | None:
        assert client_id == CLIENT_A0
        self.lookup_calls += 1
        if self.lookup_calls == 1:
            return BrokerResult(
                "f1",
                "PARTIALLY_FILLED",
                1,
                envelope().quantity,
                fill_cash_flow=Decimal("-120"),
            )
        return BrokerResult(
            "f1",
            "FILLED",
            envelope().quantity,
            envelope().quantity,
            fill_cash_flow=Decimal("-240"),
        )


class FreshSweepPort:
    def __init__(self, baseline_at: datetime, clock: "MutableDatabaseClock | None" = None) -> None:
        self._baseline_at = baseline_at
        self._clock = clock

    def _now(self) -> datetime:
        if self._clock is not None:
            self._clock.advance(timedelta(milliseconds=20))
            return self._clock.value
        return datetime.now(UTC)

    def collect(self, expectation: object) -> WholeAccountEvidence:
        del expectation
        sleep(0.01)
        return WholeAccountEvidence(clean_sweep(self._now(), self._baseline_at))


class EquityStopSweepPort(FreshSweepPort):
    def collect(self, expectation: object) -> WholeAccountEvidence:
        evidence = super().collect(expectation)
        account = replace(evidence.sweep.first_account, equity=Decimal("99000"))
        return replace(
            evidence,
            sweep=replace(
                evidence.sweep,
                first_account=account,
                final_account=replace(
                    evidence.sweep.final_account,
                    equity=Decimal("99000"),
                ),
            ),
        )


class FilledSweepPort(FreshSweepPort):
    def __init__(self, baseline_at: datetime, clock: "MutableDatabaseClock | None" = None) -> None:
        super().__init__(baseline_at, clock)
        self._collection = 0

    def collect(self, expectation: object) -> WholeAccountEvidence:
        self._collection += 1
        if self._collection == 1:
            return super().collect(expectation)
        del expectation
        sleep(0.01)
        sweep = clean_sweep(self._now(), self._baseline_at)
        first_account = replace(sweep.first_account, cash=Decimal("99760"))
        final_account = replace(sweep.final_account, cash=Decimal("99760"))
        sweep = replace(
            sweep,
            first_account=first_account,
            final_account=final_account,
            first_positions=filled_positions(),
            final_positions=filled_positions(),
            activities=fill_activities(
                sweep,
                filled_quantity=2,
                provider_order_id="f1",
                client_order_id=CLIENT_A0,
            ),
        )
        return WholeAccountEvidence(
            sweep,
            filled_position_greeks(sweep.retrieval_completed_at),
        )


class ExistingFilledBookSweepPort(FilledSweepPort):
    def __init__(self, baseline_at: datetime) -> None:
        super().__init__(baseline_at)
        self._collection = 1


class MissingGreekSweepPort(FilledSweepPort):
    def collect(self, expectation: object) -> WholeAccountEvidence:
        return replace(super().collect(expectation), position_greeks=())


class InvalidGreekTimingSweepPort(FilledSweepPort):
    def __init__(self, baseline_at: datetime, mode: str) -> None:
        super().__init__(baseline_at)
        self._mode = mode

    def collect(self, expectation: object) -> WholeAccountEvidence:
        evidence = super().collect(expectation)
        if self._collection == 1:
            return evidence
        if self._mode in {"stale", "old_source"}:
            greeks = tuple(
                replace(
                    item,
                    source_timestamp=item.source_timestamp - timedelta(seconds=30),
                    retrieved_at=(
                        item.retrieved_at - timedelta(seconds=30)
                        if self._mode == "stale"
                        else item.retrieved_at
                    ),
                )
                for item in evidence.position_greeks
            )
        else:
            greeks = (
                evidence.position_greeks[0],
                replace(
                    evidence.position_greeks[1],
                    source_timestamp=evidence.position_greeks[1].source_timestamp
                    - timedelta(seconds=1),
                    retrieved_at=evidence.position_greeks[1].retrieved_at - timedelta(seconds=1),
                ),
            )
        return replace(evidence, position_greeks=greeks)


class LaggedFillActivitySweepPort(FilledSweepPort):
    def collect(self, expectation: object) -> WholeAccountEvidence:
        evidence = super().collect(expectation)
        if self._collection == 2:
            return replace(
                evidence,
                sweep=replace(
                    evidence.sweep,
                    activities=tuple(
                        item
                        for item in evidence.sweep.activities
                        if item.activity_type == ActivityType.INITIAL_FUNDING
                    ),
                ),
            )
        return evidence


class UnrelatedFillActivitySweepPort(FilledSweepPort):
    def collect(self, expectation: object) -> WholeAccountEvidence:
        evidence = super().collect(expectation)
        if self._collection > 1:
            activities = tuple(
                replace(item, provider_order_id="x")
                if item.activity_type == ActivityType.FILL
                else item
                for item in evidence.sweep.activities
            )
            return replace(evidence, sweep=replace(evidence.sweep, activities=activities))
        return evidence


class WrongClientFillActivitySweepPort(FilledSweepPort):
    def collect(self, expectation: object) -> WholeAccountEvidence:
        evidence = super().collect(expectation)
        if self._collection > 1:
            activities = tuple(
                replace(item, client_order_id="x")
                if item.activity_type == ActivityType.FILL
                else item
                for item in evidence.sweep.activities
            )
            return replace(evidence, sweep=replace(evidence.sweep, activities=activities))
        return evidence


class PartialSweepPort(FreshSweepPort):
    def __init__(self, baseline_at: datetime, clock: "MutableDatabaseClock | None" = None) -> None:
        super().__init__(baseline_at, clock)
        self._collection = 0

    def collect(self, expectation: object) -> WholeAccountEvidence:
        self._collection += 1
        if self._collection == 1:
            return super().collect(expectation)
        del expectation
        sleep(0.01)
        sweep = clean_sweep(self._now(), self._baseline_at)
        sweep = replace(
            sweep,
            first_account=replace(sweep.first_account, cash=Decimal("99880")),
            final_account=replace(sweep.final_account, cash=Decimal("99880")),
            first_positions=partial_positions(),
            final_positions=partial_positions(),
            first_open_orders=(partial_open_order(),) if self._collection == 2 else (),
            final_open_orders=(partial_open_order(),) if self._collection == 2 else (),
            activities=(
                sweep.activities
                if self._collection == 2
                else fill_activities(
                    sweep,
                    filled_quantity=1,
                    provider_order_id="f1",
                    client_order_id=CLIENT_A0,
                )
            ),
        )
        return WholeAccountEvidence(
            sweep,
            partial_position_greeks(sweep.retrieval_completed_at),
        )


class CancelRaceFilledSweepPort(PartialSweepPort):
    def collect(self, expectation: object) -> WholeAccountEvidence:
        evidence = super().collect(expectation)
        if self._collection != 3:
            return evidence
        sweep = evidence.sweep
        return WholeAccountEvidence(
            replace(
                sweep,
                first_account=replace(sweep.first_account, cash=Decimal("99760")),
                final_account=replace(sweep.final_account, cash=Decimal("99760")),
                first_positions=filled_positions(),
                final_positions=filled_positions(),
                activities=fill_activities(
                    sweep,
                    filled_quantity=2,
                    provider_order_id="f1",
                    client_order_id=CLIENT_A0,
                ),
            ),
            filled_position_greeks(sweep.retrieval_completed_at),
        )


class ReplacementSweepPort(FilledSweepPort):
    def __init__(
        self,
        baseline_at: datetime,
        clock: "MutableDatabaseClock | None" = None,
    ) -> None:
        super().__init__(baseline_at, clock)

    def collect(self, expectation: object) -> WholeAccountEvidence:
        if self._collection != 1:
            evidence = super().collect(expectation)
            if self._collection == 3:
                return replace(
                    evidence,
                    sweep=replace(
                        evidence.sweep,
                        activities=fill_activities(
                            evidence.sweep,
                            filled_quantity=2,
                            provider_order_id="f1",
                            client_order_id=CLIENT_A1,
                        ),
                    ),
                )
            return evidence
        self._collection += 1
        sleep(0.01)
        sweep = clean_sweep(self._now(), self._baseline_at)
        active = expectation.expected_open_orders[0]
        return WholeAccountEvidence(
            replace(
                sweep,
                first_open_orders=(active,),
                final_open_orders=(active,),
            )
        )


class ReplacementQuotes:
    def __init__(self, clock: "MutableDatabaseClock") -> None:
        self._clock = clock

    def collect(self, symbols: tuple[str, ...]) -> tuple[NormalizedOptionSnapshot, ...]:
        return tuple(
            NormalizedOptionSnapshot(
                symbol=symbol,
                underlying="DEMO",
                retrieved_at=self._clock.value,
                quote_timestamp=self._clock.value - timedelta(seconds=1),
                bid_price=bid,
                ask_price=ask,
                bid_size=10,
                ask_size=10,
                greeks=NormalizedGreeks(
                    delta_per_share=Decimal("0.5"),
                    gamma_per_share_per_usd=Decimal("0.01"),
                    theta_per_share_per_day=Decimal("-0.02"),
                    vega_per_share_per_iv_point=Decimal("0.03"),
                ),
            )
            for symbol, bid, ask in zip(
                symbols,
                (Decimal("0.50"), Decimal("2.00")),
                (Decimal("0.60"), Decimal("2.10")),
                strict=True,
            )
        )


class DeterministicActiveSweepPort(FreshSweepPort):
    def __init__(self, baseline_at: datetime, active_count: int) -> None:
        super().__init__(baseline_at)
        self._active_count = active_count
        self._collection = 0

    def collect(self, expectation: object) -> WholeAccountEvidence:
        del expectation
        self._collection += 1
        sleep(0.01)
        sweep = clean_sweep(datetime.now(UTC), self._baseline_at)
        ordinal = self._collection - 2
        active = (
            (active_open_order_for_ordinal(ordinal),) if 0 <= ordinal < self._active_count else ()
        )
        return WholeAccountEvidence(
            replace(
                sweep,
                first_open_orders=active,
                final_open_orders=active,
            )
        )


class UnexpectedInventorySweepPort(FreshSweepPort):
    def collect(self, expectation: object) -> WholeAccountEvidence:
        del expectation
        sleep(0.01)
        sweep = clean_sweep(datetime.now(UTC), self._baseline_at)
        unexpected = (InventoryItem(InventoryKind.EQUITY, "SURPRISE", Decimal("1"), 1),)
        return WholeAccountEvidence(
            replace(
                sweep,
                first_positions=unexpected,
                final_positions=unexpected,
            )
        )


class FinalUnexpectedInventorySweepPort(FreshSweepPort):
    def __init__(self, baseline_at: datetime) -> None:
        super().__init__(baseline_at)
        self._collection = 0

    def collect(self, expectation: object) -> WholeAccountEvidence:
        self._collection += 1
        if self._collection == 1:
            return super().collect(expectation)
        return UnexpectedInventorySweepPort(self._baseline_at).collect(expectation)


class MutableDatabaseClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self, _session) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def authorized_repository(
    clock: MutableDatabaseClock | None = None,
) -> tuple[SQLAlchemyExecutionRepository, datetime]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repo = SQLAlchemyExecutionRepository(sessions, entry_limits=ENTRY_LIMITS, trusted_clock=clock)
    now = datetime.now(UTC)
    baseline_at = now - timedelta(days=2)
    order = envelope()
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint=FINGERPRINT,
        equity=Decimal("100000"),
        autonomous_enabled=False,
    )
    repo.capture_baseline(
        role=AccountRole.SUBMISSION,
        fingerprint=FINGERPRINT,
        equity=Decimal("100000"),
        captured_at=baseline_at,
        positions_hash="positions",
        orders_hash="orders",
        activities_hash="activities",
    )
    repo.initialize_reconciliation_state(clean_sweep(now, baseline_at))
    repo.set_autonomous_enabled(AccountRole.SUBMISSION, True, actor=Actor.OWNER)
    repo.add_thesis_version(
        FrozenThesisVersion(
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            thesis_id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            account_role=AccountRole.SUBMISSION,
            version=1,
            thesis_hash="f" * 64,
            policy_hash=order.policy_hash,
            underlying="DEMO",
            thesis_code="TEST_THESIS",
            frozen_at=baseline_at,
            target_at=baseline_at + timedelta(days=7),
            intended_exposure={},
            exposure_limits={},
            volatility_view="NEUTRAL",
            entry_atm_iv=Decimal("0.4"),
            approved_max_loss=order.approved_max_loss,
            portfolio_risk_cap=order.approved_max_loss,
            invalidation_codes=("TEST_INVALIDATION",),
            thesis_payload={"fixture": True},
            created_at=baseline_at,
        )
    )
    repo.add_entry_approval(
        EntryApprovalAuthorization(
            approval_id=AUTHORIZATION_ID,
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            account_role=AccountRole.SUBMISSION,
            policy_hash=order.policy_hash,
            book_fingerprint=order.position_or_book_fingerprint,
            envelope_hash=order_envelope_hash(order),
            approved_max_loss=order.approved_max_loss,
            quantity=order.quantity,
            valid_from=datetime(2020, 1, 1, tzinfo=UTC),
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, order)
    _attach_test_agent_origin(sessions, order, INTENT_ID, ordinal=1)
    return repo, baseline_at


def approve_second_entry(repo: SQLAlchemyExecutionRepository) -> None:
    order = replace(
        envelope(),
        authorization_certificate_id=SECOND_AUTHORIZATION_ID,
        event_key="PANW-2026-09-04",
        trading_day=datetime(2026, 9, 4, tzinfo=UTC).date(),
    )
    repo.add_entry_approval(
        EntryApprovalAuthorization(
            approval_id=SECOND_AUTHORIZATION_ID,
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            account_role=AccountRole.SUBMISSION,
            policy_hash=order.policy_hash,
            book_fingerprint=order.position_or_book_fingerprint,
            envelope_hash=order_envelope_hash(order),
            approved_max_loss=order.approved_max_loss,
            quantity=order.quantity,
            valid_from=datetime(2020, 1, 1, tzinfo=UTC),
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    repo.approve_intent(SECOND_INTENT_ID, AccountRole.SUBMISSION, order)
    _attach_test_agent_origin(repo._sessions, order, SECOND_INTENT_ID, ordinal=2)


def _attach_test_agent_origin(
    sessions, order: OrderEnvelope, intent_id: UUID, *, ordinal: int
) -> None:
    boundary = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=ordinal)
    thesis_version_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    normalized = {"fixture": "downstream_execution", "ordinal": ordinal}
    input_hash = canonical_agent_hash(
        agent_input_material(
            account_role=AccountRole.SUBMISSION.value,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=boundary,
            observed_at=boundary,
            normalized_input=normalized,
            thesis_version_id=thesis_version_id,
        )
    )
    snapshot_id = uuid5(NAMESPACE_URL, f"test-agent-input:{input_hash}")
    decision_id = uuid5(NAMESPACE_URL, f"test-agent-decision:{input_hash}")
    tick_id = uuid5(NAMESPACE_URL, f"test-agent-tick:{intent_id}")
    result_payload = {"fixture": "downstream_execution"}
    result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash=input_hash,
            outcome="ENTRY_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash=order.policy_hash,
            thesis_version_id=thesis_version_id,
            result_payload=result_payload,
            authorization_id=order.authorization_certificate_id,
            intent_id=intent_id,
            intent_digest=intent_digest(order),
            autonomy_authorized=True,
        )
    )
    with sessions.begin() as session:
        session.execute(text("PRAGMA ignore_check_constraints=ON"))
        tick = AgentTickRow(
            tick_id=tick_id,
            account_role=AccountRole.SUBMISSION.value,
            account_fingerprint=FINGERPRINT,
            tick_key=f"fixture:{intent_id}",
            tick_boundary=boundary,
            actor="SCHEDULER",
            status="RESERVED",
            reservation_token=uuid5(NAMESPACE_URL, f"test-agent-reservation:{intent_id}"),
            created_at=boundary,
        )
        session.add(tick)
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=snapshot_id,
                thesis_version_id=thesis_version_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint=FINGERPRINT,
                decision_kind="OPPORTUNITY",
                decision_boundary=boundary,
                observed_at=boundary,
                normalized_payload=normalized,
                input_hash=input_hash,
                created_at=boundary,
            )
        )
        session.flush()
        session.add(
            AgentDecisionRow(
                decision_id=decision_id,
                thesis_version_id=thesis_version_id,
                origin_tick_id=tick_id,
                input_snapshot_id=snapshot_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint=FINGERPRINT,
                decision_kind="OPPORTUNITY",
                outcome="ENTRY_APPROVED",
                reason_code="POLICY_APPROVED",
                policy_hash=order.policy_hash,
                result_payload=result_payload,
                result_hash=result_hash,
                autonomy_authorized=True,
                decision_boundary=boundary,
                created_at=boundary,
            )
        )
        session.flush()
        tick.decision_id = decision_id
        approval = session.get(EntryApprovalCertificateRow, order.authorization_certificate_id)
        assert approval is not None
        approval.agent_decision_id = decision_id
        session.flush()
        session.execute(text("PRAGMA ignore_check_constraints=OFF"))


def envelope() -> OrderEnvelope:
    return OrderEnvelope(
        action=ExecutionAction.ENTRY,
        authorization_certificate_id=AUTHORIZATION_ID,
        policy_hash=POLICY_HASH,
        account_fingerprint=FINGERPRINT,
        position_or_book_fingerprint="book-fingerprint",
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        quantity=2,
        minimum_limit=Decimal("1.00"),
        maximum_limit=Decimal("1.50"),
        approved_max_loss=Decimal("700"),
        event_key="PANW-2026-09-03",
        trading_day=datetime(2026, 9, 3, tzinfo=UTC).date(),
    )


def filled_positions() -> tuple[InventoryItem, ...]:
    return (
        InventoryItem(
            InventoryKind.OPTION,
            "DEMO260918C00100000",
            Decimal("2"),
            100,
        ),
        InventoryItem(
            InventoryKind.OPTION,
            "DEMO260918C00105000",
            Decimal("-2"),
            100,
        ),
    )


def filled_position_greeks(
    retrieved_at: datetime,
) -> tuple[PositionGreekObservation, ...]:
    return (
        PositionGreekObservation(
            symbol="DEMO260918C00100000",
            signed_quantity=Decimal("2"),
            multiplier=100,
            delta=Decimal("0.6"),
            gamma=Decimal("0.02"),
            theta_per_day=Decimal("-0.05"),
            vega_per_iv_point=Decimal("0.1"),
            feed="indicative",
            source_timestamp=retrieved_at - timedelta(milliseconds=1),
            retrieved_at=retrieved_at,
            source_hash="c" * 64,
        ),
        PositionGreekObservation(
            symbol="DEMO260918C00105000",
            signed_quantity=Decimal("-2"),
            multiplier=100,
            delta=Decimal("0.3"),
            gamma=Decimal("0.01"),
            theta_per_day=Decimal("-0.02"),
            vega_per_iv_point=Decimal("0.05"),
            feed="indicative",
            source_timestamp=retrieved_at - timedelta(milliseconds=1),
            retrieved_at=retrieved_at,
            source_hash="d" * 64,
        ),
    )


def partial_positions() -> tuple[InventoryItem, ...]:
    return tuple(
        replace(position, signed_quantity=position.signed_quantity / 2)
        for position in filled_positions()
    )


def partial_position_greeks(
    retrieved_at: datetime,
) -> tuple[PositionGreekObservation, ...]:
    return tuple(
        replace(observation, signed_quantity=observation.signed_quantity / 2)
        for observation in filled_position_greeks(retrieved_at)
    )


def partial_open_order() -> OpenOrderItem:
    return OpenOrderItem(
        provider_order_id="f1",
        client_order_id=CLIENT_A0,
        state="PARTIALLY_FILLED",
        quantity=2,
        filled_quantity=1,
        replaces_client_order_id=None,
        replaced_by_client_order_id=None,
        order_class="MLEG",
        legs=tuple(OpenOrderLeg(leg.symbol, leg.intent, leg.ratio) for leg in envelope().legs),
    )


def active_open_order() -> OpenOrderItem:
    return replace(partial_open_order(), state="NEW", filled_quantity=0)


def active_open_order_for_ordinal(ordinal: int) -> OpenOrderItem:
    provider_reference = "f1"
    client_reference = f"fixture-ad-20260903-e-bbecc98d27f37ca59b49f37b-a{ordinal}"
    previous_client_reference = (
        f"fixture-ad-20260903-e-bbecc98d27f37ca59b49f37b-a{ordinal - 1}" if ordinal > 0 else None
    )
    return replace(
        active_open_order(),
        provider_order_id=provider_reference,
        client_order_id=client_reference,
        replaces_client_order_id=previous_client_reference,
    )


def fill_activities(
    sweep: SweepObservation,
    *,
    filled_quantity: int,
    provider_order_id: str,
    client_order_id: str,
) -> tuple[ActivityItem, ...]:
    occurred_at = sweep.activity_pagination.requested_start + timedelta(milliseconds=1)
    baseline_activities = tuple(
        item for item in sweep.activities if item.activity_type == ActivityType.INITIAL_FUNDING
    )
    return (
        *baseline_activities,
        ActivityItem(
            activity_id_hash="c" * 64,
            activity_type=ActivityType.FILL,
            occurred_at=occurred_at,
            symbol="DEMO260918C00100000",
            signed_quantity=Decimal(filled_quantity),
            provider_order_id=provider_order_id,
            client_order_id=client_order_id,
        ),
        ActivityItem(
            activity_id_hash="d" * 64,
            activity_type=ActivityType.FILL,
            occurred_at=occurred_at,
            symbol="DEMO260918C00105000",
            signed_quantity=Decimal(-filled_quantity),
            provider_order_id=provider_order_id,
            client_order_id=client_order_id,
        ),
    )


def clean_sweep(now: datetime, baseline_at: datetime) -> SweepObservation:
    completed = now - timedelta(milliseconds=5)
    started = completed - timedelta(milliseconds=5)
    first_at = completed - timedelta(milliseconds=3)
    activity_at = completed - timedelta(milliseconds=2)
    final_at = completed - timedelta(milliseconds=1)
    account = AccountObservation(
        role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
        paper=True,
        status="ACTIVE",
        account_blocked=False,
        trading_blocked=False,
        options_trading_blocked=False,
        equity=Decimal("100000"),
        buying_power=Decimal("400000"),
        cash=Decimal("100000"),
        observed_at=first_at,
        time_quality="RETRIEVAL_TIME_ONLY",
    )
    final_account = AccountObservation(**{**account.__dict__, "observed_at": final_at})
    funding = ActivityItem(
        activity_id_hash="b" * 64,
        activity_type=ActivityType.INITIAL_FUNDING,
        occurred_at=baseline_at - timedelta(minutes=1),
        symbol=None,
        signed_quantity=Decimal("100000"),
    )
    return SweepObservation(
        retrieval_started_at=started,
        retrieval_completed_at=completed,
        activity_pagination=ActivityPaginationEvidence(
            requested_start=baseline_at,
            requested_end=first_at,
            retrieved_through=first_at,
            established_at=activity_at,
            page_count=1,
            terminal_page_seen=True,
            visibility_complete_through=baseline_at,
            visibility_horizon=timedelta(hours=24),
        ),
        first_account=account,
        final_account=final_account,
        first_positions=(),
        final_positions=(),
        first_open_orders=(),
        final_open_orders=(),
        activities=(funding,),
        positions_complete=True,
        orders_complete=True,
    )
