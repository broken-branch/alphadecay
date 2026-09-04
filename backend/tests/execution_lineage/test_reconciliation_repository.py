import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Lock
from time import sleep
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from pg8000.dbapi import ProgrammingError as PGProgrammingError
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from backend.app.alpaca.market_data import NormalizedGreeks, NormalizedOptionSnapshot
from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.execution import (
    AccountObservation,
    AccountReconciliationState,
    ActivityItem,
    ActivityPaginationEvidence,
    ActivityType,
    Actor,
    AmbiguousBrokerResponse,
    AssessmentCertificate,
    AttemptObservationSource,
    BrokerResult,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionBlocked,
    ExecutionCertificate,
    FrozenThesisVersion,
    InventoryItem,
    InventoryKind,
    OpenOrderItem,
    OpenOrderLeg,
    OrderEnvelope,
    OrderLegIntent,
    ReconciliationPurpose,
    SweepObservation,
    attempt_request_hash,
    client_order_id,
    order_envelope_hash,
    replacement_request_hash,
)
from backend.app.execution.models import IntentState, OrderAttempt
from backend.app.execution.reconciliation import WholeAccountReconciliation
from backend.app.order_limits import EntryBudgetLimits
from backend.app.persistence import AgentDecisionRepository, SQLAlchemyExecutionRepository
from backend.app.persistence.runtime import apply_migrations, discover_migrations
from backend.app.persistence.sqlalchemy_models import (
    AccountReconciliationStateRow,
    Base,
    BrokerMutationPermitRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    OrderAttemptRow,
    SubmissionBaselineRow,
)
from backend.app.persistence.sqlalchemy_repository import _inventory_to_json
from backend.app.services import ExecutionService
from backend.app.services.execution import WholeAccountEvidence
from ops.launch.submission_execution_preflight import (
    evaluate_submission_execution_preflight,
)
from ops.launch.submission_reconciliation_init import (
    initialize_submission_reconciliation,
)

FINGERPRINT = "a" * 64
EXECUTION_ROLE = AccountRole.DEVELOPMENT
MIGRATIONS = Path(__file__).parents[3] / "migrations"
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-000000000451")
INTENT_ID = UUID("00000000-0000-0000-0000-000000000452")
ENTRY_LIMITS = EntryBudgetLimits(
    policy_hash="d" * 64,
    equity_floor=Decimal("99000"),
    maximum_lifetime_entries=3,
    maximum_lifetime_risk=Decimal("1500"),
    maximum_position_loss=Decimal("800"),
    maximum_entry_quantity=4,
)


def test_inventory_persistence_canonicalizes_integral_decimal_scale() -> None:
    assert (
        _inventory_to_json(
            InventoryItem(
                kind=InventoryKind.OPTION,
                symbol="NVDA260918C00170000",
                signed_quantity=Decimal("1.00"),
                multiplier=100,
            )
        )["signed_quantity"]
        == "1"
    )


class _BoundExecutionService:
    def __init__(self, target: ExecutionService) -> None:
        self._target = target

    def advance(self, intent_id: UUID, actor: Actor):
        return self._target.advance(
            intent_id,
            actor,
            account_role=EXECUTION_ROLE,
            account_fingerprint=FINGERPRINT,
        )


class _PendingLookupBroker:
    def __init__(self, repository, broker) -> None:
        self._repository = repository
        self._broker = broker

    def lookup(self, client_id: str) -> BrokerResult | None:
        lookup = getattr(type(self._broker), "lookup", None)
        if lookup is not None:
            return lookup(self._broker, client_id)
        current = self._repository.attempts_for(INTENT_ID)[-1]
        assert current.client_order_id == client_id
        return BrokerResult(
            current.provider_order_id,
            current.state,
            current.filled_quantity,
            current.quantity,
            current.fill_cash_flow,
        )

    def __getattr__(self, name: str):
        return getattr(self._broker, name)


def _execution_service(repository, broker, preflight, quotes=None) -> _BoundExecutionService:
    return _BoundExecutionService(
        ExecutionService(
            repository,
            _PendingLookupBroker(repository, broker),
            preflight,
            quotes,
            account_role=EXECUTION_ROLE,
            account_fingerprint=FINGERPRINT,
        )
    )


def _claim(
    repo: SQLAlchemyExecutionRepository,
    intent_id: UUID,
    actor: Actor,
    *,
    now: datetime,
):
    return repo.claim_intent(
        intent_id,
        actor,
        now=now,
        account_role=EXECUTION_ROLE,
        account_fingerprint=FINGERPRINT,
    )


class FakeDatabaseClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self, _session) -> datetime:
        return self.value


def repository(
    *,
    permit_ttl: timedelta = timedelta(seconds=15),
    retryable_permit_ttl: timedelta | None = None,
    network_call_horizon: timedelta = timedelta(seconds=30),
    clock: FakeDatabaseClock | None = None,
) -> SQLAlchemyExecutionRepository:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return SQLAlchemyExecutionRepository(
        sessionmaker(engine, expire_on_commit=False),
        permit_ttl=permit_ttl,
        retryable_permit_ttl=retryable_permit_ttl,
        network_call_horizon=network_call_horizon,
        trusted_clock=clock,
        entry_limits=ENTRY_LIMITS,
    )


def account(
    observed_at: datetime,
    role: AccountRole = EXECUTION_ROLE,
) -> AccountObservation:
    return AccountObservation(
        role=role,
        account_fingerprint=FINGERPRINT,
        paper=True,
        status="ACTIVE",
        account_blocked=False,
        trading_blocked=False,
        options_trading_blocked=False,
        equity=Decimal("100000"),
        buying_power=Decimal("400000"),
        cash=Decimal("100000"),
        observed_at=observed_at,
        time_quality="RETRIEVAL_TIME_ONLY",
    )


def clean_sweep(
    now: datetime,
    baseline_at: datetime,
    role: AccountRole = EXECUTION_ROLE,
) -> SweepObservation:
    started = now - timedelta(milliseconds=5)
    first_at = now - timedelta(milliseconds=3)
    activity_at = now - timedelta(milliseconds=2)
    final_at = now - timedelta(milliseconds=1)
    funding = ActivityItem(
        activity_id_hash="b" * 64,
        activity_type=ActivityType.INITIAL_FUNDING,
        occurred_at=baseline_at - timedelta(minutes=1),
        symbol=None,
        signed_quantity=Decimal("100000"),
    )
    return SweepObservation(
        retrieval_started_at=started,
        retrieval_completed_at=final_at,
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
        first_account=account(first_at, role),
        final_account=account(final_at, role),
        first_positions=(),
        final_positions=(),
        first_open_orders=(),
        final_open_orders=(),
        activities=(funding,),
        positions_complete=True,
        orders_complete=True,
    )


def open_order(attempt: OrderAttempt, order: OrderEnvelope) -> OpenOrderItem:
    assert attempt.provider_order_id is not None
    return OpenOrderItem(
        provider_order_id=attempt.provider_order_id,
        client_order_id=attempt.client_order_id,
        state=attempt.state,
        quantity=attempt.quantity,
        filled_quantity=attempt.filled_quantity,
        replaces_client_order_id=attempt.replaces_client_order_id,
        replaced_by_client_order_id=None,
        order_class="MLEG",
        legs=tuple(
            OpenOrderLeg(symbol=leg.symbol, intent=leg.intent, ratio=leg.ratio)
            for leg in order.legs
        ),
    )


def sweep_with_open_order(
    now: datetime,
    baseline_at: datetime,
    item: OpenOrderItem,
) -> SweepObservation:
    sweep = clean_sweep(now, baseline_at)
    return SweepObservation(
        retrieval_started_at=sweep.retrieval_started_at,
        retrieval_completed_at=sweep.retrieval_completed_at,
        activity_pagination=sweep.activity_pagination,
        first_account=sweep.first_account,
        final_account=sweep.final_account,
        first_positions=sweep.first_positions,
        final_positions=sweep.final_positions,
        first_open_orders=(item,),
        final_open_orders=(item,),
        activities=sweep.activities,
        positions_complete=True,
        orders_complete=True,
    )


def close_envelope() -> OrderEnvelope:
    return OrderEnvelope(
        action=ExecutionAction.CLOSE,
        authorization_certificate_id=AUTHORIZATION_ID,
        policy_hash="d" * 64,
        account_fingerprint=FINGERPRINT,
        position_or_book_fingerprint="c" * 64,
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.SELL_TO_CLOSE, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.BUY_TO_CLOSE, 1),
        ),
        quantity=2,
        minimum_limit=Decimal("1.00"),
        maximum_limit=Decimal("1.50"),
        approved_max_loss=Decimal("700"),
        event_key="DEMO-2026-09-03",
        trading_day=date(2026, 9, 3),
    )


def entry_envelope() -> OrderEnvelope:
    return replace(
        close_envelope(),
        action=ExecutionAction.ENTRY,
        legs=tuple(
            replace(
                leg,
                intent=(
                    PositionIntent.BUY_TO_OPEN
                    if leg.intent is PositionIntent.BUY_TO_CLOSE
                    else PositionIntent.SELL_TO_OPEN
                ),
            )
            for leg in close_envelope().legs
        ),
    )


def claimed_submission(
    repo: SQLAlchemyExecutionRepository,
    order: OrderEnvelope | None = None,
    *,
    role: AccountRole = EXECUTION_ROLE,
) -> tuple[datetime, OrderEnvelope, object]:
    now = datetime.now(UTC)
    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    baseline_at = now - timedelta(days=2)
    order = order or close_envelope()
    thesis_frozen_at = boundary if order.action is ExecutionAction.ENTRY else baseline_at
    repo.register_account(
        role=role,
        fingerprint=FINGERPRINT,
        equity=Decimal("100000"),
        autonomous_enabled=False,
    )
    repo.add_thesis_version(
        FrozenThesisVersion(
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            thesis_id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            account_role=role,
            version=1,
            thesis_hash="f" * 64,
            policy_hash=order.policy_hash,
            underlying="DEMO",
            thesis_code="TEST_THESIS",
            frozen_at=thesis_frozen_at,
            target_at=thesis_frozen_at + timedelta(days=7),
            intended_exposure={},
            exposure_limits={},
            volatility_view="NEUTRAL",
            entry_atm_iv=Decimal("0.4"),
            approved_max_loss=Decimal("700"),
            portfolio_risk_cap=Decimal("700"),
            invalidation_codes=("TEST_INVALIDATION",),
            thesis_payload={"frozen": True},
            created_at=thesis_frozen_at,
        )
    )
    if role is AccountRole.SUBMISSION:
        repo.capture_baseline(
            role=role,
            fingerprint=FINGERPRINT,
            equity=Decimal("100000"),
            captured_at=baseline_at,
            positions_hash="4" * 64,
            orders_hash="5" * 64,
            activities_hash="6" * 64,
        )
        reconciliation_state_id = repo.initialize_reconciliation_state(
            clean_sweep(datetime.now(UTC), baseline_at, role)
        ).state_id
    else:
        reconciliation_state_id = _seed_development_reconciliation_state(
            repo,
            clean_sweep(datetime.now(UTC), baseline_at),
            baseline_at,
        )
    repo.set_autonomous_enabled(role, True, actor=Actor.OWNER)
    thesis_version_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    if order.action is ExecutionAction.ENTRY:
        authorization: EntryApprovalAuthorization | AssessmentCertificate = (
            EntryApprovalAuthorization(
                approval_id=AUTHORIZATION_ID,
                thesis_version_id=thesis_version_id,
                account_role=role,
                policy_hash=order.policy_hash,
                book_fingerprint=order.position_or_book_fingerprint,
                envelope_hash=order_envelope_hash(order),
                approved_max_loss=order.approved_max_loss,
                quantity=order.quantity,
                valid_from=datetime(2020, 1, 1, tzinfo=UTC),
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
        decision_kind = "OPPORTUNITY"
        outcome = "ENTRY_APPROVED"
    else:
        authorization = AssessmentCertificate(
            certificate_id=AUTHORIZATION_ID,
            assessment_id=UUID("00000000-0000-0000-0000-000000000453"),
            thesis_version_id=thesis_version_id,
            account_role=role,
            action=order.action,
            position_fingerprint=order.position_or_book_fingerprint,
            envelope_hash=order_envelope_hash(order),
            approved_max_loss=order.approved_max_loss,
            quantity=order.quantity,
            expected_after_exposure=None,
            policy_hash=order.policy_hash,
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        decision_kind = "ASSESSMENT"
        outcome = "ROLL_APPROVED" if order.action is ExecutionAction.ROLL else "CLOSE_APPROVED"
        _seed_managed_development_position(repo, order, reconciliation_state_id)
    agent_repository = AgentDecisionRepository(repo._sessions, server_autonomy_enabled=True)
    tick = agent_repository.reserve_tick(
        account_role=role,
        account_fingerprint=FINGERPRINT,
        actor="SCHEDULER",
        trusted_at=boundary,
        tick_key=f"reconciliation:{INTENT_ID}",
    )
    assert tick.reservation_token is not None
    agent_repository.record_decision(
        account_role=role,
        account_fingerprint=FINGERPRINT,
        decision_kind=decision_kind,
        decision_boundary=boundary,
        observed_at=boundary,
        normalized_input={"fixture": "reconciliation"},
        outcome=outcome,
        reason_code="POLICY_APPROVED",
        policy_hash=order.policy_hash,
        result_payload={"fixture": "reconciliation"},
        thesis_version_id=authorization.thesis_version_id,
        authorization=authorization,
        envelope=order,
        intent_id=INTENT_ID,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )
    actor = Actor.SCHEDULER if order.action is ExecutionAction.ENTRY else Actor.OWNER
    claim = repo.claim_intent(
        INTENT_ID,
        actor,
        now=datetime.now(UTC),
        account_role=role,
        account_fingerprint=FINGERPRINT,
    )
    return baseline_at, order, claim


def test_expired_unsubmitted_claim_can_be_previewed_and_released() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC))
    repo = repository(clock=clock)
    _, _, claim = claimed_submission(repo, entry_envelope())
    clock.value = clock.value + timedelta(seconds=31)

    assert repo.expired_unsubmitted_claims(EXECUTION_ROLE, persist=False) == (claim.intent_id,)
    assert repo.get_intent(claim.intent_id).state is IntentState.CLAIMED

    assert repo.expired_unsubmitted_claims(EXECUTION_ROLE, persist=True) == (claim.intent_id,)
    assert repo.get_intent(claim.intent_id).state is IntentState.TERMINAL
    assert repo.get_entry_budget(EXECUTION_ROLE).reserved_intent_id is None


def test_submission_seed_preview_is_zero_write_then_persisted() -> None:
    now = datetime.now(UTC)
    baseline_at = now - timedelta(days=2)
    repo = repository(clock=FakeDatabaseClock(now))
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
        positions_hash="4" * 64,
        orders_hash="5" * 64,
        activities_hash="6" * 64,
    )
    sweep = clean_sweep(now, baseline_at, AccountRole.SUBMISSION)

    preview = initialize_submission_reconciliation(repo, sweep, persist=False)
    with pytest.raises(KeyError):
        repo.get_reconciliation_state(AccountRole.SUBMISSION)
    persisted = initialize_submission_reconciliation(repo, sweep, persist=True)

    assert preview["mode"] == "PREVIEW"
    assert persisted["mode"] == "PERSISTED"
    assert preview["state_hash"] == persisted["state_hash"]
    assert (
        repo.get_reconciliation_state(AccountRole.SUBMISSION).state_hash == persisted["state_hash"]
    )


def test_submission_preflight_reports_reconciliation_before_ready() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    _, _, claim = claimed_submission(repo, entry_envelope(), role=AccountRole.SUBMISSION)

    assert evaluate_submission_execution_preflight(repo)["status"] == "READY"
    with repo._sessions.begin() as session:
        session.query(AccountReconciliationStateRow).delete()
    assert evaluate_submission_execution_preflight(repo)["status"] == (
        "RECONCILIATION_STATE_REQUIRED"
    )
    assert repo.get_intent(claim.intent_id).state is IntentState.CLAIMED


def _seed_managed_development_position(
    repo: SQLAlchemyExecutionRepository,
    order: OrderEnvelope,
    reconciliation_state_id: UUID,
) -> None:
    managed_position_id = uuid5(NAMESPACE_URL, "reconciliation-managed-position")
    snapshot_id = uuid5(NAMESPACE_URL, "reconciliation-managed-snapshot")
    with repo._sessions.begin() as session:
        position = ManagedLifecyclePositionRow(
            managed_position_id=managed_position_id,
            account_role=EXECUTION_ROLE.value,
            account_fingerprint=FINGERPRINT,
            entry_execution_certificate_id=uuid5(NAMESPACE_URL, "reconciliation-entry-cert"),
            entry_intent_id=uuid5(NAMESPACE_URL, "reconciliation-entry-intent"),
            entry_approval_id=uuid5(NAMESPACE_URL, "reconciliation-entry-approval"),
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            entry_reconciliation_id=uuid5(NAMESPACE_URL, "reconciliation-entry-sweep"),
            current_reconciliation_state_id=reconciliation_state_id,
            current_snapshot_id=None,
            active_position_fingerprint=order.position_or_book_fingerprint,
            activated_at=datetime.now(UTC) - timedelta(days=1),
            closed_at=None,
        )
        session.add(position)
        session.flush()
        session.add(
            ManagedPositionSnapshotRow(
                snapshot_id=snapshot_id,
                managed_position_id=managed_position_id,
                predecessor_snapshot_id=None,
                transition_id=uuid5(NAMESPACE_URL, "reconciliation-entry-transition"),
                reconciliation_id=uuid5(NAMESPACE_URL, "reconciliation-snapshot-sweep"),
                reconciliation_state_id=reconciliation_state_id,
                normalized_inventory=[],
                inventory_hash="1" * 64,
                activity_manifest=[],
                activity_manifest_hash="2" * 64,
                cumulative_cashflow=Decimal("0"),
                rolls_on_trading_day=0,
                market_session_id=uuid5(NAMESPACE_URL, "reconciliation-market-session"),
                position_fingerprint=order.position_or_book_fingerprint,
                accepted_at=datetime.now(UTC) - timedelta(days=1),
                snapshot_hash="3" * 64,
            )
        )
        session.flush()
        position.current_snapshot_id = snapshot_id


def _seed_development_reconciliation_state(
    repo: SQLAlchemyExecutionRepository,
    sweep: SweepObservation,
    baseline_at: datetime,
) -> UUID:
    accepted_at = datetime.now(UTC)
    state = AccountReconciliationState._from_repository_state(
        account_role=EXECUTION_ROLE,
        account_fingerprint=FINGERPRINT,
        baseline_captured_at=baseline_at,
        accepted_at=accepted_at,
        expected_cash=Decimal("100000"),
        expected_positions=(),
        expected_open_orders=(),
        known_activities=sweep.activities,
        activity_complete_through=sweep.activity_pagination.visibility_complete_through,
    )
    baseline_id = uuid5(NAMESPACE_URL, "reconciliation-development-baseline")
    activity = sweep.activities[0]
    with repo._sessions.begin() as session:
        session.add(
            SubmissionBaselineRow(
                baseline_id=baseline_id,
                account_role=EXECUTION_ROLE.value,
                account_fingerprint=FINGERPRINT,
                equity=Decimal("100000"),
                captured_at=baseline_at,
                positions_hash="4" * 64,
                orders_hash="5" * 64,
                activities_hash="6" * 64,
                contaminated=False,
            )
        )
        session.flush()
        session.add(
            AccountReconciliationStateRow(
                state_id=state.state_id,
                account_role=EXECUTION_ROLE.value,
                sequence=1,
                account_fingerprint=FINGERPRINT,
                baseline_id=baseline_id,
                baseline_captured_at=baseline_at,
                accepted_at=accepted_at,
                expected_cash=Decimal("100000"),
                expected_positions=[],
                expected_open_orders=[],
                known_activities=[
                    {
                        "activity_id_hash": activity.activity_id_hash,
                        "activity_type": activity.activity_type.value,
                        "occurred_at": activity.occurred_at.isoformat(),
                        "symbol": activity.symbol,
                        "signed_quantity": str(activity.signed_quantity),
                        "provider_order_id": activity.provider_order_id,
                        "client_order_id": activity.client_order_id,
                        "time_quality": activity.time_quality,
                        "provider_activity_type": activity.provider_activity_type,
                    }
                ],
                activity_complete_through=state.activity_complete_through,
                resolved_activity_hashes=[],
                predecessor_state_id=None,
                authority_reconciliation_id=None,
                authority_permit_id=None,
                authority_observation_id=None,
                authority_permit_request_hash=None,
                transition_hash=None,
                state_hash=state.state_hash,
            )
        )
    return state.state_id


def prepared_submit_attempt(order: OrderEnvelope, digest: str) -> OrderAttempt:
    identifier = client_order_id(order.trading_day, order.action, digest, 0)
    return OrderAttempt(
        intent_id=INTENT_ID,
        ordinal=0,
        client_order_id=identifier,
        request_hash=attempt_request_hash(digest, 0, identifier, order.minimum_limit, None),
        state="PREPARED",
        quantity=order.quantity,
    )


def consume_submit_as_new(
    repo: SQLAlchemyExecutionRepository,
    baseline_at: datetime,
    order: OrderEnvelope,
    claim,
) -> tuple[OrderAttempt, object]:
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)
    assert prepared.permit is not None
    dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    assert dispatched.dispatch_nonce is not None
    observed = replace(
        attempt,
        state="NEW",
        provider_order_id="p1",
        quantity=order.quantity,
    )
    repo.record_attempt_observation(
        prepared.permit.permit_id,
        observed,
        source=AttemptObservationSource.DISPATCH_OUTCOME,
        claim=claim,
        dispatch_nonce=dispatched.dispatch_nonce,
    )
    return observed, prepared.permit


def test_repository_seeds_clean_submission_book_at_database_time() -> None:
    repo = repository()
    baseline_at = datetime.now(UTC) - timedelta(days=2)
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
    before = datetime.now(UTC)

    state = repo.initialize_reconciliation_state(
        clean_sweep(before, baseline_at, AccountRole.SUBMISSION)
    )

    after = datetime.now(UTC)
    assert state.account_role == AccountRole.SUBMISSION
    assert state.account_fingerprint == FINGERPRINT
    assert state.baseline_captured_at == baseline_at
    assert state.expected_cash == Decimal("100000")
    assert state.expected_positions == ()
    assert state.expected_open_orders == ()
    assert len(state.known_activities) == 1
    assert state.known_activities[0].activity_type == ActivityType.INITIAL_FUNDING
    assert before - timedelta(seconds=1) <= state.accepted_at <= after
    assert len(state.state_hash) == 64
    assert repo.get_reconciliation_state(AccountRole.SUBMISSION) == state


def test_repository_preserves_activity_time_quality_and_provider_type() -> None:
    repo = repository()
    baseline_at = datetime.now(UTC) - timedelta(days=2)
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
    original = clean_sweep(datetime.now(UTC), baseline_at, AccountRole.SUBMISSION)
    funding = replace(
        original.activities[0],
        time_quality="DATE_ONLY",
        provider_activity_type="JNLC",
    )

    repo.initialize_reconciliation_state(replace(original, activities=(funding,)))

    assert repo.get_reconciliation_state(AccountRole.SUBMISSION).known_activities == (funding,)


def test_safe_submit_preparation_atomically_persists_reconciliation_permit_and_attempt() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )

    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)

    assert prepared.reconciliation.safe is True
    assert prepared.attempt == attempt
    assert prepared.permit is not None
    assert prepared.permit.reconciliation_id == prepared.reconciliation.reconciliation_id
    assert prepared.permit.intent_id == claim.intent_id
    assert prepared.permit.intent_digest == claim.digest
    assert prepared.permit.claim_token == claim.claim_token
    assert prepared.permit.claim_generation == claim.claim_generation
    assert prepared.permit.execution_epoch == claim.execution_epoch
    assert prepared.permit.mutation_kind == ReconciliationPurpose.SUBMIT
    assert prepared.permit.attempt_ordinal == 0
    assert prepared.permit.generation == 1
    assert prepared.permit.state == "PREPARED"
    assert prepared.permit.issued_at <= prepared.permit.expires_at
    assert prepared.permit.target_client_order_id is None
    assert prepared.permit.target_provider_order_id is None
    assert repo.attempts_for(claim.intent_id) == (attempt,)
    assert (
        repo.get_whole_account_reconciliation(prepared.reconciliation.reconciliation_id)
        == prepared.reconciliation
    )
    assert repo.get_broker_mutation_permit(prepared.permit.permit_id) == prepared.permit


def test_scheduler_advance_is_not_due_before_first_replacement_boundary() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    consume_submit_as_new(repo, baseline_at, order, claim)

    assert repo.next_broker_mutation(claim) is None


def test_early_scheduler_advance_does_not_touch_provider_ports() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    consume_submit_as_new(repo, baseline_at, order, claim)

    class ForbiddenPort:
        def __getattr__(self, name: str):
            def forbidden(*_args, **_kwargs):
                raise AssertionError(f"early advance touched {name}")

            return forbidden

    forbidden = ForbiddenPort()
    result = _execution_service(repo, forbidden, forbidden, forbidden).advance(
        claim.intent_id, Actor.OWNER
    )

    assert result.status == "WAITING"
    assert result.mutation is None


def test_due_replacement_uses_fresh_quotes_and_advances_once() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    _, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    due_at = dispatched.dispatch_acquired_at + timedelta(seconds=150)
    clock.value = due_at

    class Quotes:
        def collect(self, symbols: tuple[str, ...]):
            return tuple(
                NormalizedOptionSnapshot(
                    symbol=symbol,
                    underlying="DEMO",
                    retrieved_at=due_at,
                    quote_timestamp=due_at - timedelta(seconds=1),
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

    class Sweep:
        def collect(self, expectation):
            item = expectation.expected_open_orders[0]
            return WholeAccountEvidence(sweep_with_open_order(due_at, baseline_at, item))

    class Broker:
        def __init__(self) -> None:
            self.limit: Decimal | None = None

        def replace(self, provider_order_id: str, client_id: str, limit: Decimal):
            self.limit = limit
            return replace(
                repo.attempts_for(claim.intent_id)[-1],
                provider_order_id="p2",
                state="NEW",
                quantity=order.quantity,
            )

    broker = Broker()
    result = _execution_service(repo, broker, Sweep(), Quotes()).advance(
        claim.intent_id, Actor.OWNER
    )

    assert result.status == "REPLACED"
    assert broker.limit == Decimal("1.02")
    persisted = repo.attempts_for(claim.intent_id)[-1]
    assert persisted.limit_price == Decimal("1.02")
    permit = repo.broker_mutation_permits_for(claim.intent_id)[-1]
    assert permit.limit_price == persisted.limit_price
    assert permit.quote_hash == persisted.quote_hash
    assert permit.quote_source_timestamps == persisted.quote_source_timestamps
    assert permit.timing_authority_at == persisted.timing_authority_at
    assert permit.prior_request_hash == persisted.prior_request_hash


@pytest.mark.parametrize("provider_delay", [timedelta(milliseconds=1), timedelta(seconds=2)])
def test_replacement_uses_fresh_database_time_after_quote_collection(
    provider_delay: timedelta,
) -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    _, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    clock.value = dispatched.dispatch_acquired_at + timedelta(seconds=150)

    class Quotes:
        def collect(self, symbols: tuple[str, ...]):
            clock.value += provider_delay
            return replacement_snapshots(symbols, clock.value)

    class Sweep:
        def collect(self, expectation):
            return WholeAccountEvidence(
                sweep_with_open_order(
                    clock.value,
                    baseline_at,
                    expectation.expected_open_orders[0],
                )
            )

    class Broker:
        def replace(self, _provider_order_id: str, _client_id: str, _limit: Decimal):
            return BrokerResult("p2", "NEW", 0, order.quantity)

    result = _execution_service(repo, Broker(), Sweep(), Quotes()).advance(
        claim.intent_id, Actor.OWNER
    )

    assert result.status == "REPLACED"
    assert result.attempt is not None
    assert result.attempt.quote_retrieved_at == clock.value
    assert result.attempt.timing_authority_at == clock.value


def test_quote_delay_crossing_cancel_deadline_does_not_dispatch_replacement() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    _, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    clock.value = dispatched.dispatch_acquired_at + timedelta(milliseconds=599_999)

    class Quotes:
        def collect(self, symbols: tuple[str, ...]):
            clock.value += timedelta(milliseconds=2)
            return replacement_snapshots(symbols, clock.value)

    class Sweep:
        def collect(self, expectation):
            return WholeAccountEvidence(
                sweep_with_open_order(
                    clock.value,
                    baseline_at,
                    expectation.expected_open_orders[0],
                )
            )

    class Broker:
        def __init__(self) -> None:
            self.replace_calls = 0
            self.cancel_calls = 0

        def replace(self, *_args):
            self.replace_calls += 1
            raise AssertionError("deadline-crossed replacement dispatched")

        def cancel(self, provider_order_id: str):
            self.cancel_calls += 1
            return BrokerResult(provider_order_id, "CANCELED", 0, order.quantity)

    broker = Broker()
    result = _execution_service(repo, broker, Sweep(), Quotes()).advance(
        claim.intent_id, Actor.OWNER
    )

    assert result.status == "CANCELED"
    assert result.mutation == ReconciliationPurpose.CANCEL
    assert broker.replace_calls == 0
    assert broker.cancel_calls == 1
    assert tuple(attempt.ordinal for attempt in repo.attempts_for(claim.intent_id)) == (0,)


def test_sweep_crossing_cancel_deadline_does_not_dispatch_replacement() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    _, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    clock.value = dispatched.dispatch_acquired_at + timedelta(milliseconds=599_999)

    class Quotes:
        def collect(self, symbols):
            return replacement_snapshots(symbols, clock.value)

    class Sweep:
        def collect(self, expectation):
            clock.value += timedelta(milliseconds=2)
            return WholeAccountEvidence(
                sweep_with_open_order(
                    clock.value,
                    baseline_at,
                    expectation.expected_open_orders[0],
                )
            )

    class Broker:
        replace_calls = 0
        cancel_calls = 0

        def replace(self, *_args):
            self.replace_calls += 1
            raise AssertionError("deadline-crossed replacement dispatched")

        def cancel(self, provider_order_id):
            self.cancel_calls += 1
            return BrokerResult(provider_order_id, "CANCELED", 0, order.quantity)

    broker = Broker()
    result = _execution_service(repo, broker, Sweep(), Quotes()).advance(
        claim.intent_id, Actor.OWNER
    )

    assert result.mutation == ReconciliationPurpose.CANCEL
    assert result.status == "CANCELED"
    assert broker.replace_calls == 0
    assert broker.cancel_calls == 1


def replacement_snapshots(
    symbols: tuple[str, ...],
    observed_at: datetime,
    *,
    bids: tuple[Decimal, ...] = (Decimal("0.50"), Decimal("2.00")),
    asks: tuple[Decimal, ...] = (Decimal("0.60"), Decimal("2.10")),
) -> tuple[NormalizedOptionSnapshot, ...]:
    return tuple(
        NormalizedOptionSnapshot(
            symbol=symbol,
            underlying="DEMO",
            retrieved_at=observed_at,
            quote_timestamp=observed_at - timedelta(seconds=1),
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
        for symbol, bid, ask in zip(symbols, bids, asks, strict=True)
    )


def quote_replacement_from_advance(
    repo: SQLAlchemyExecutionRepository,
    order: OrderEnvelope,
    claim,
    current: OrderAttempt,
    clock: FakeDatabaseClock,
    baseline_at: datetime,
    *,
    bids: tuple[Decimal, ...] = (Decimal("0.50"), Decimal("2.00")),
    asks: tuple[Decimal, ...] = (Decimal("0.60"), Decimal("2.10")),
) -> OrderAttempt:
    permit = repo.broker_mutation_permits_for(claim.intent_id)[0]
    assert permit.dispatch_acquired_at is not None
    clock.value = permit.dispatch_acquired_at + timedelta(seconds=150)

    class Quotes:
        def collect(self, symbols):
            return replacement_snapshots(
                symbols,
                clock.value,
                bids=bids,
                asks=asks,
            )

    class Sweep:
        def collect(self, expectation):
            return WholeAccountEvidence(
                sweep_with_open_order(
                    clock.value,
                    baseline_at,
                    expectation.expected_open_orders[0],
                )
            )

    class Broker:
        def replace(self, _provider_order_id, _client_id, _limit):
            return BrokerResult("p2", "NEW", 0, order.quantity)

    result = _execution_service(repo, Broker(), Sweep(), Quotes()).advance(
        claim.intent_id, Actor.OWNER
    )
    assert result.status == "REPLACED"
    replacement = repo.attempts_for(claim.intent_id)[-1]
    assert replacement.replaces_client_order_id == current.client_order_id
    return replacement


def prepared_quote_replacement(
    repo: SQLAlchemyExecutionRepository,
    order: OrderEnvelope,
    claim,
    current: OrderAttempt,
    clock: FakeDatabaseClock,
) -> OrderAttempt:
    permit = repo.broker_mutation_permits_for(claim.intent_id)[0]
    assert permit.dispatch_acquired_at is not None
    clock.value = permit.dispatch_acquired_at + timedelta(seconds=150)
    quote_values = replacement_snapshots(tuple(leg.symbol for leg in order.legs), clock.value)
    timestamps = tuple(item.quote_timestamp for item in quote_values)
    quote_payload = [
        {
            "symbol": item.symbol,
            "underlying": item.underlying,
            "bid": str(item.bid_price),
            "ask": str(item.ask_price),
            "bid_size": item.bid_size,
            "ask_size": item.ask_size,
            "multiplier": item.multiplier,
            "quote_timestamp": item.quote_timestamp.isoformat(),
            "retrieved_at": item.retrieved_at.isoformat(),
            "provenance": item.provenance,
        }
        for item in quote_values
    ]
    quote_hash = hashlib.sha256(
        json.dumps(quote_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identifier = client_order_id(order.trading_day, order.action, claim.digest, 1)
    limit = Decimal("1.02")
    return OrderAttempt(
        intent_id=claim.intent_id,
        ordinal=1,
        client_order_id=identifier,
        request_hash=replacement_request_hash(
            claim.digest,
            1,
            identifier,
            limit,
            current.client_order_id,
            current.request_hash,
            quote_hash,
            timestamps,
            clock.value,
            clock.value,
        ),
        state="PREPARED",
        replaces_client_order_id=current.client_order_id,
        quantity=order.quantity,
        limit_price=limit,
        quote_hash=quote_hash,
        quote_source_timestamps=timestamps,
        quote_retrieved_at=clock.value,
        timing_authority_at=clock.value,
        prior_request_hash=current.request_hash,
    )


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("missing", "REPLACEMENT_QUOTE_SYMBOL_MISMATCH"),
        ("stale", "REPLACEMENT_QUOTE_INVALID"),
        ("crossed", "REPLACEMENT_QUOTE_INVALID"),
        ("wrong_symbol", "REPLACEMENT_QUOTE_SYMBOL_MISMATCH"),
        ("wrong_underlying", "REPLACEMENT_QUOTE_STRUCTURE_MISMATCH"),
    ],
)
def test_invalid_due_quote_fails_before_sweep_permit_or_broker(
    kind: str,
    code: str,
) -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    _, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    due_at = dispatched.dispatch_acquired_at + timedelta(seconds=150)
    clock.value = due_at
    symbols = tuple(leg.symbol for leg in order.legs)
    quote_values = replacement_snapshots(symbols, due_at)
    if kind == "missing":
        quote_values = quote_values[:1]
    elif kind == "stale":
        quote_values = (
            quote_values[0].model_copy(update={"quote_timestamp": due_at - timedelta(seconds=31)}),
        ) + quote_values[1:]
    elif kind == "crossed":
        quote_values = (
            quote_values[0].model_copy(update={"bid_price": Decimal("0.70")}),
        ) + quote_values[1:]
    elif kind == "wrong_symbol":
        quote_values = (
            quote_values[0].model_copy(update={"symbol": "DEMO260918C00101000"}),
        ) + quote_values[1:]
    else:
        quote_values = (quote_values[0].model_copy(update={"underlying": "OTHER"}),) + quote_values[
            1:
        ]

    class Quotes:
        def collect(self, _symbols):
            return quote_values

    class Forbidden:
        def __getattr__(self, name: str):
            def forbidden(*_args, **_kwargs):
                raise AssertionError(f"invalid quotes touched {name}")

            return forbidden

    forbidden = Forbidden()
    with pytest.raises(ExecutionBlocked, match=code):
        _execution_service(repo, forbidden, forbidden, Quotes()).advance(
            claim.intent_id, Actor.OWNER
        )

    assert len(repo.attempts_for(claim.intent_id)) == 1


@pytest.mark.parametrize(
    ("bids", "asks", "expected"),
    [
        (
            (Decimal("0.50"), Decimal("2.00")),
            (Decimal("0.52"), Decimal("2.02")),
            Decimal("1.01"),
        ),
        (
            (Decimal("0.50"), Decimal("2.00")),
            (Decimal("0.60"), Decimal("2.10")),
            Decimal("1.02"),
        ),
    ],
)
def test_replacement_increment_is_one_cent_or_ten_percent_of_net_width(
    bids: tuple[Decimal, ...],
    asks: tuple[Decimal, ...],
    expected: Decimal,
) -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    current, _ = consume_submit_as_new(repo, baseline_at, order, claim)
    replacement = quote_replacement_from_advance(
        repo,
        order,
        claim,
        current,
        clock,
        baseline_at,
        bids=bids,
        asks=asks,
    )

    assert replacement.limit_price == expected


def test_credit_replacement_improves_provider_limit_numerically() -> None:
    order = replace(
        close_envelope(),
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.BUY_TO_CLOSE, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.SELL_TO_CLOSE, 1),
        ),
        minimum_limit=Decimal("-1.50"),
        maximum_limit=Decimal("-1.00"),
    )
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, _, claim = claimed_submission(repo, order)
    current, _ = consume_submit_as_new(repo, baseline_at, order, claim)
    replacement = quote_replacement_from_advance(
        repo,
        order,
        claim,
        current,
        clock,
        baseline_at,
    )

    assert replacement.limit_price == Decimal("-1.48")


def test_zero_replacement_limit_blocks_before_sweep_permit_or_broker() -> None:
    order = replace(
        close_envelope(),
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.BUY_TO_CLOSE, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.SELL_TO_CLOSE, 1),
        ),
        minimum_limit=Decimal("-0.01"),
        maximum_limit=Decimal("0.50"),
    )
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, _, claim = claimed_submission(repo, order)
    _, permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    clock.value = dispatched.dispatch_acquired_at + timedelta(seconds=150)

    class Quotes:
        def collect(self, symbols):
            return replacement_snapshots(
                symbols,
                clock.value,
                bids=(Decimal("0.50"), Decimal("0.50")),
                asks=(Decimal("0.51"), Decimal("0.51")),
            )

    class Forbidden:
        def __getattr__(self, name):
            def forbidden(*_args, **_kwargs):
                raise AssertionError(f"zero limit touched {name}")

            return forbidden

    forbidden = Forbidden()
    with pytest.raises(ExecutionBlocked, match="REPLACEMENT_LIMIT_ZERO"):
        _execution_service(repo, forbidden, forbidden, Quotes()).advance(
            claim.intent_id, Actor.OWNER
        )


def test_same_strike_cross_expiry_roll_replaces_through_public_service() -> None:
    order = replace(
        close_envelope(),
        action=ExecutionAction.ROLL,
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.SELL_TO_CLOSE, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.BUY_TO_CLOSE, 1),
            OrderLegIntent("DEMO261016C00105000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO261016C00110000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        market_session_id=UUID("00000000-0000-0000-0000-000000000901"),
        quoted_relative_spread=Decimal("0.05"),
        maximum_relative_spread=Decimal("0.25"),
        incremental_debit=Decimal("100"),
        maximum_incremental_debit=Decimal("500"),
    )
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, _, claim = claimed_submission(repo, order)
    _, permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    clock.value = dispatched.dispatch_acquired_at + timedelta(seconds=150)

    class Quotes:
        def collect(self, symbols):
            return replacement_snapshots(
                symbols,
                clock.value,
                bids=(Decimal("2.00"), Decimal("0.50"), Decimal("0.60"), Decimal("1.80")),
                asks=(Decimal("2.10"), Decimal("0.60"), Decimal("0.70"), Decimal("1.90")),
            )

    class Sweep:
        def collect(self, expectation):
            return WholeAccountEvidence(
                sweep_with_open_order(clock.value, baseline_at, expectation.expected_open_orders[0])
            )

    class Broker:
        def replace(self, _provider_order_id, _client_id, _limit):
            return BrokerResult("p2", "NEW", 0, order.quantity)

    result = _execution_service(repo, Broker(), Sweep(), Quotes()).advance(
        claim.intent_id, Actor.OWNER
    )

    assert result.status == "REPLACED"


@pytest.mark.parametrize(
    ("bids", "asks", "code"),
    (
        (
            (Decimal("1"),) * 4,
            (Decimal("2"),) * 4,
            "ROLL_LIQUIDITY_DERIORATED",
        ),
        (
            (Decimal("1"), Decimal("1"), Decimal("4"), Decimal("1")),
            (Decimal("1.01"), Decimal("1.01"), Decimal("4.01"), Decimal("1.01")),
            "ROLL_INCREMENTAL_DEBIT_DERIORATED",
        ),
    ),
)
def test_roll_quote_deterioration_blocks_replacement_before_broker_mutation(
    bids: tuple[Decimal, ...],
    asks: tuple[Decimal, ...],
    code: str,
) -> None:
    order = replace(
        close_envelope(),
        action=ExecutionAction.ROLL,
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.SELL_TO_CLOSE, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.BUY_TO_CLOSE, 1),
            OrderLegIntent("DEMO261016C00105000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO261016C00110000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        market_session_id=uuid4(),
        quoted_relative_spread=Decimal("0.05"),
        maximum_relative_spread=Decimal("0.25"),
        incremental_debit=Decimal("100"),
        maximum_incremental_debit=Decimal("500"),
    )
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, _, claim = claimed_submission(repo, order)
    _, permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    clock.value = dispatched.dispatch_acquired_at + timedelta(seconds=150)

    class Quotes:
        def collect(self, symbols):
            return replacement_snapshots(
                symbols,
                clock.value,
                bids=bids,
                asks=asks,
            )

    class Forbidden:
        def __getattr__(self, name):
            def forbidden(*_args, **_kwargs):
                raise AssertionError(f"deteriorated roll touched {name}")

            return forbidden

    forbidden = Forbidden()
    with pytest.raises(ExecutionBlocked, match=code):
        _execution_service(repo, forbidden, forbidden, Quotes()).advance(
            claim.intent_id,
            Actor.OWNER,
        )


def test_roll_quote_deterioration_blocks_initial_submit_before_broker_mutation() -> None:
    order = replace(
        close_envelope(),
        action=ExecutionAction.ROLL,
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.SELL_TO_CLOSE, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.BUY_TO_CLOSE, 1),
            OrderLegIntent("DEMO261016C00105000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO261016C00110000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        market_session_id=uuid4(),
        quoted_relative_spread=Decimal("0.05"),
        maximum_relative_spread=Decimal("0.25"),
        incremental_debit=Decimal("100"),
        maximum_incremental_debit=Decimal("500"),
    )
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    _, _, claim = claimed_submission(repo, order)

    class Quotes:
        def collect(self, symbols):
            return replacement_snapshots(
                symbols,
                clock.value,
                bids=(Decimal("1"),) * 4,
                asks=(Decimal("2"),) * 4,
            )

    class Forbidden:
        def __getattr__(self, name):
            def forbidden(*_args, **_kwargs):
                raise AssertionError(f"deteriorated initial roll touched {name}")

            return forbidden

    forbidden = Forbidden()
    with pytest.raises(ExecutionBlocked, match="ROLL_LIQUIDITY_DERIORATED"):
        _execution_service(repo, forbidden, forbidden, Quotes()).advance(
            claim.intent_id,
            Actor.OWNER,
        )


def test_duplicate_contract_within_expiry_blocks_before_sweep_or_broker() -> None:
    order = replace(
        close_envelope(),
        action=ExecutionAction.ROLL,
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.SELL_TO_CLOSE, 1),
            OrderLegIntent("DEMO260918C00100000", PositionIntent.BUY_TO_CLOSE, 1),
            OrderLegIntent("DEMO261016C00105000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO261016C00110000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        market_session_id=UUID("00000000-0000-0000-0000-000000000902"),
        quoted_relative_spread=Decimal("0.05"),
        maximum_relative_spread=Decimal("0.25"),
        incremental_debit=Decimal("100"),
        maximum_incremental_debit=Decimal("500"),
    )
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, _, claim = claimed_submission(repo, order)
    _, permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    clock.value = dispatched.dispatch_acquired_at + timedelta(seconds=150)

    class Quotes:
        def collect(self, symbols):
            return replacement_snapshots(
                symbols,
                clock.value,
                bids=(Decimal("2.00"), Decimal("2.00"), Decimal("0.60"), Decimal("1.80")),
                asks=(Decimal("2.10"), Decimal("2.10"), Decimal("0.70"), Decimal("1.90")),
            )

    class Forbidden:
        def __getattr__(self, name):
            def forbidden(*_args, **_kwargs):
                raise AssertionError(f"duplicate contract touched {name}")

            return forbidden

    forbidden = Forbidden()
    with pytest.raises(ExecutionBlocked, match="REPLACEMENT_QUOTE_STRUCTURE_MISMATCH"):
        _execution_service(repo, forbidden, forbidden, Quotes()).advance(
            claim.intent_id, Actor.OWNER
        )


def test_capped_replacement_is_noop_before_quote_sweep_permit_or_broker() -> None:
    order = replace(close_envelope(), maximum_limit=Decimal("1.00"))
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, _, claim = claimed_submission(repo, order)
    _, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    clock.value = dispatched.dispatch_acquired_at + timedelta(seconds=150)

    class Forbidden:
        def __getattr__(self, name: str):
            def forbidden(*_args, **_kwargs):
                raise AssertionError(f"capped replacement touched {name}")

            return forbidden

    forbidden = Forbidden()
    result = _execution_service(repo, forbidden, forbidden, forbidden).advance(
        claim.intent_id, Actor.OWNER
    )

    assert result.status == "REPLACEMENT_LIMIT_CAPPED"
    assert result.mutation is None
    assert len(repo.attempts_for(claim.intent_id)) == 1


def test_capped_replacement_rechecks_database_time_before_returning() -> None:
    class DeadlineClock(FakeDatabaseClock):
        armed = False
        calls = 0
        before_deadline: datetime | None = None
        after_deadline: datetime | None = None

        def now(self, _session) -> datetime:
            if not self.armed:
                return self.value
            self.calls += 1
            assert self.before_deadline is not None
            assert self.after_deadline is not None
            return self.before_deadline if self.calls == 1 else self.after_deadline

    order = replace(close_envelope(), maximum_limit=Decimal("1.00"))
    clock = DeadlineClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, _, claim = claimed_submission(repo, order)
    current, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    initial = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert initial.dispatch_acquired_at is not None
    clock.before_deadline = initial.dispatch_acquired_at + timedelta(milliseconds=599_999)
    clock.after_deadline = initial.dispatch_acquired_at + timedelta(milliseconds=600_001)
    clock.armed = True

    class Quotes:
        def collect(self, _symbols):
            raise AssertionError("capped hard cancellation fetched replacement quotes")

    class Sweep:
        def collect(self, expectation):
            assert clock.after_deadline is not None
            return WholeAccountEvidence(
                sweep_with_open_order(
                    clock.after_deadline,
                    baseline_at,
                    expectation.expected_open_orders[0],
                )
            )

    class Broker:
        cancel_targets: list[str] = []

        def cancel(self, provider_order_id):
            self.cancel_targets.append(provider_order_id)
            return BrokerResult(provider_order_id, "CANCELED", 0, order.quantity)

    broker = Broker()
    result = _execution_service(repo, broker, Sweep(), Quotes()).advance(
        claim.intent_id, Actor.OWNER
    )

    assert result.mutation == ReconciliationPurpose.CANCEL
    assert result.status == "CANCELED"
    assert broker.cancel_targets == [current.provider_order_id]
    assert clock.calls > 1


@pytest.mark.parametrize(
    "substitution",
    [
        {"limit_price": Decimal("1.03")},
        {"quote_hash": "f" * 64},
        {"prior_request_hash": "f" * 64},
        {"quote_retrieved_at": datetime(2020, 1, 1, tzinfo=UTC)},
        {"timing_authority_at": datetime(2020, 1, 1, tzinfo=UTC)},
        {"quote_source_timestamps": (datetime(2020, 1, 1, tzinfo=UTC),) * 2},
    ],
)
def test_replacement_request_hash_substitution_is_rejected(
    substitution: dict[str, object],
) -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    current, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    replacement = quote_replacement_from_advance(
        repo,
        order,
        claim,
        current,
        clock,
        baseline_at,
    )

    with pytest.raises(ExecutionBlocked, match="REPLACEMENT_"):
        repo.plan_broker_mutation(
            claim,
            ReconciliationPurpose.REPLACE,
            replace(replacement, **substitution),
        )


def test_all_frozen_boundaries_are_one_mutation_each_and_survive_restart() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    _, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    initial_dispatch_at = dispatched.dispatch_acquired_at
    clock.value = initial_dispatch_at

    class Quotes:
        def __init__(self) -> None:
            self.calls = 0

        def collect(self, symbols):
            self.calls += 1
            return replacement_snapshots(symbols, clock.value)

    class Sweep:
        def __init__(self) -> None:
            self.calls = 0

        def collect(self, expectation):
            self.calls += 1
            item = expectation.expected_open_orders[0]
            return WholeAccountEvidence(sweep_with_open_order(clock.value, baseline_at, item))

    class Broker:
        def __init__(self) -> None:
            self.replacements: list[Decimal] = []
            self.cancels = 0

        def replace(self, _provider_order_id, _client_id, limit):
            self.replacements.append(limit)
            provider_reference = f"p5-{len(self.replacements) + 1}"
            return BrokerResult(
                provider_order_id=provider_reference,
                state="NEW",
                filled_quantity=0,
                quantity=order.quantity,
            )

        def cancel(self, provider_order_id):
            self.cancels += 1
            return BrokerResult(
                provider_order_id=provider_order_id,
                state="CANCELED",
                filled_quantity=0,
                quantity=order.quantity,
            )

    quotes = Quotes()
    sweep = Sweep()
    broker = Broker()
    service = _execution_service(repo, broker, sweep, quotes)

    clock.value = initial_dispatch_at + timedelta(seconds=150)
    assert service.advance(claim.intent_id, Actor.OWNER).mutation == ReconciliationPurpose.REPLACE
    assert service.advance(claim.intent_id, Actor.OWNER).status == "WAITING"
    clock.value = initial_dispatch_at + timedelta(seconds=300)
    restarted = _execution_service(repo, broker, sweep, quotes)
    assert restarted.advance(claim.intent_id, Actor.OWNER).mutation == ReconciliationPurpose.REPLACE
    clock.value = initial_dispatch_at + timedelta(seconds=450)
    assert restarted.advance(claim.intent_id, Actor.OWNER).mutation == ReconciliationPurpose.REPLACE
    clock.value = initial_dispatch_at + timedelta(seconds=599)
    assert restarted.advance(claim.intent_id, Actor.OWNER).status == "WAITING"
    clock.value = initial_dispatch_at + timedelta(seconds=600)
    canceled = restarted.advance(claim.intent_id, Actor.OWNER)

    assert canceled.mutation == ReconciliationPurpose.CANCEL
    assert canceled.status == "CANCELED"
    assert broker.replacements == [Decimal("1.02"), Decimal("1.04"), Decimal("1.06")]
    assert broker.cancels == 1
    assert quotes.calls == 3
    assert sweep.calls == 4
    attempts = repo.attempts_for(claim.intent_id)
    assert tuple(attempt.ordinal for attempt in attempts) == (0, 1, 2, 3)
    assert attempts[-1].state == "CANCELED"


def test_partial_fill_schedules_immediate_fresh_cancel_without_quotes() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    current, permit = consume_submit_as_new(repo, baseline_at, order, claim)
    row = repo.get_broker_mutation_permit(permit.permit_id)
    assert row.dispatch_acquired_at is not None
    observed_at = row.dispatch_acquired_at + timedelta(seconds=1)
    clock.value = observed_at
    # A targeted provider observation is the existing lineage seam for a newly seen partial fill.
    repo.record_attempt_observation(
        permit.permit_id,
        replace(
            current,
            state="PARTIALLY_FILLED",
            filled_quantity=1,
            fill_cash_flow=Decimal("-100"),
        ),
        source=AttemptObservationSource.TARGETED_LOOKUP,
        claim=claim,
    )

    schedule = repo.next_broker_mutation(claim)

    assert schedule is not None
    assert schedule.purpose == ReconciliationPurpose.CANCEL


def test_concurrent_scheduler_advances_dispatch_at_most_one_replacement(tmp_path: Path) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'replacement-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = SQLAlchemyExecutionRepository(
        sessionmaker(engine, expire_on_commit=False),
        trusted_clock=clock,
        entry_limits=ENTRY_LIMITS,
    )
    baseline_at, order, claim = claimed_submission(repo)
    _, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    due_at = dispatched.dispatch_acquired_at + timedelta(seconds=150)
    clock.value = due_at

    class Quotes:
        def collect(self, symbols):
            return replacement_snapshots(symbols, due_at)

    class Sweep:
        def collect(self, expectation):
            return WholeAccountEvidence(
                sweep_with_open_order(
                    due_at,
                    baseline_at,
                    expectation.expected_open_orders[0],
                )
            )

    class Broker:
        def __init__(self) -> None:
            self.calls = 0
            self.lock = Lock()

        def replace(self, _provider_order_id, _client_id, _limit):
            with self.lock:
                self.calls += 1
            return BrokerResult("p2", "NEW", 0, order.quantity)

    broker = Broker()
    service = _execution_service(repo, broker, Sweep(), Quotes())
    current = repo.attempts_for(claim.intent_id)[-1]
    repo.record_attempt_observation(
        original_permit.permit_id,
        current,
        source=AttemptObservationSource.TARGETED_LOOKUP,
        claim=claim,
    )
    start = Barrier(2)

    def advance_once():
        start.wait()
        try:
            return service.advance(claim.intent_id, Actor.OWNER).status
        except ExecutionBlocked as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: advance_once(), range(2)))

    assert broker.calls == 1
    assert len(repo.attempts_for(claim.intent_id)) == 2
    assert "REPLACED" in outcomes


def test_ambiguous_due_replacement_enters_lookup_only_and_never_redispatches() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    _, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    dispatched = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert dispatched.dispatch_acquired_at is not None
    due_at = dispatched.dispatch_acquired_at + timedelta(seconds=150)
    clock.value = due_at

    class Quotes:
        def collect(self, symbols):
            return replacement_snapshots(symbols, due_at)

    class Sweep:
        def collect(self, expectation):
            return WholeAccountEvidence(
                sweep_with_open_order(
                    due_at,
                    baseline_at,
                    expectation.expected_open_orders[0],
                )
            )

    class Broker:
        def __init__(self) -> None:
            self.replace_calls = 0
            self.lookup_calls = 0

        def replace(self, *_args):
            self.replace_calls += 1
            raise AmbiguousBrokerResponse

        def lookup(self, _client_id):
            self.lookup_calls += 1
            if self.lookup_calls == 1:
                return BrokerResult("p1", "NEW", 0, order.quantity)
            return None

    broker = Broker()
    service = _execution_service(repo, broker, Sweep(), Quotes())

    with pytest.raises(ExecutionBlocked, match="AMBIGUOUS_BROKER_LOOKUP_ABSENT"):
        service.advance(claim.intent_id, Actor.OWNER)
    waiting = service.advance(claim.intent_id, Actor.OWNER)

    assert waiting.status == "WAITING"
    assert broker.replace_calls == 1
    assert broker.lookup_calls == 2


def test_mutation_and_finalization_are_reference_safe_with_foreign_keys_enabled() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    repo = SQLAlchemyExecutionRepository(
        sessionmaker(engine, expire_on_commit=False),
        entry_limits=ENTRY_LIMITS,
    )
    baseline_at, order, claim = claimed_submission(repo, entry_envelope())
    attempt = prepared_submit_attempt(order, claim.digest)
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        repo.broker_reconciliation_expectation(claim, ReconciliationPurpose.SUBMIT, attempt),
        accepted_at=datetime.now(UTC),
    )

    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)

    assert prepared.permit is not None
    assert (
        repo.get_whole_account_reconciliation(prepared.permit.reconciliation_id)
        == prepared.reconciliation
    )
    dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    assert dispatched.dispatch_nonce is not None
    terminal = replace(
        attempt,
        state="CANCELED",
        provider_order_id="pr",
    )
    observation = repo.record_attempt_observation(
        prepared.permit.permit_id,
        terminal,
        source=AttemptObservationSource.DISPATCH_OUTCOME,
        claim=claim,
        dispatch_nonce=dispatched.dispatch_nonce,
    )
    target_time = observation.observed_at + timedelta(milliseconds=10)
    for _ in range(100):
        with engine.connect() as connection:
            value = connection.execute(
                text("SELECT strftime('%Y-%m-%dT%H:%M:%f', 'now')")
            ).scalar_one()
        if datetime.fromisoformat(value).replace(tzinfo=UTC) >= target_time:
            break
        sleep(0.001)
    else:
        pytest.fail("SQLite clock did not advance past the observation boundary")
    final_reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        repo.final_reconciliation_expectation(claim),
        accepted_at=datetime.now(UTC),
    )
    certificate = repo.finalize_execution_authorized(
        ExecutionCertificate(
            certificate_id=uuid5(NAMESPACE_URL, f"alphadecay:execution:{claim.digest}"),
            intent_id=claim.intent_id,
            entry_approval_id=order.authorization_certificate_id,
            assessment_certificate_id=None,
            execution_status="UNFILLED",
            attempt_ids=(terminal.client_order_id,),
            actual_exposure=None,
            reconciliation_checks=(
                "TERMINAL",
                "REMAINDER_ABSENT",
                "WHOLE_ACCOUNT_RECONCILED",
            ),
            created_at=datetime.now(UTC),
        ),
        final_reconciliation,
        "UNFILLED",
        claim=claim,
    )

    assert certificate is not None
    assert certificate.last_observation_hash == observation.observation_hash


def test_prepared_permit_has_one_dispatch_winner_and_one_bound_outcome() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)
    assert prepared.permit is not None

    dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)

    assert dispatched.state == "DISPATCHING"
    assert dispatched.dispatch_nonce is not None
    assert dispatched.dispatch_acquired_at is not None
    with pytest.raises(ExecutionBlocked, match="BROKER_PERMIT_NOT_PREPARED"):
        repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)

    observation = repo.record_attempt_observation(
        prepared.permit.permit_id,
        replace(
            attempt,
            state="FILLED",
            provider_order_id="p5",
            filled_quantity=2,
            quantity=2,
            fill_cash_flow=Decimal("-240"),
        ),
        source=AttemptObservationSource.DISPATCH_OUTCOME,
        claim=claim,
        dispatch_nonce=dispatched.dispatch_nonce,
    )
    consumed = repo.get_broker_mutation_permit(prepared.permit.permit_id)

    assert consumed.state == "CONSUMED"
    assert consumed.outcome_hash == observation.observation_hash
    assert consumed.consumed_at is not None


def test_unsafe_reconciliation_is_persisted_and_latches_without_attempt_or_permit() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    foreign = open_order(
        OrderAttempt(
            intent_id=claim.intent_id,
            ordinal=0,
            client_order_id="c3",
            request_hash="f" * 64,
            state="NEW",
            provider_order_id="p4",
            quantity=2,
        ),
        order,
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(datetime.now(UTC), baseline_at, foreign),
        expectation,
        accepted_at=datetime.now(UTC),
    )

    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)

    assert prepared.reconciliation.safe is False
    assert prepared.permit is None
    assert prepared.attempt is None
    assert repo.attempts_for(claim.intent_id) == ()
    assert repo.get_execution_lock(EXECUTION_ROLE).locked is True
    assert (
        repo.get_whole_account_reconciliation(prepared.reconciliation.reconciliation_id)
        == prepared.reconciliation
    )


def test_bare_attempt_mutations_are_retired() -> None:
    repo = repository()
    _, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)

    with pytest.raises(ExecutionBlocked, match="BROKER_PERMIT_REQUIRED"):
        repo.add_attempt(attempt)
    with pytest.raises(ExecutionBlocked, match="BROKER_PERMIT_REQUIRED"):
        repo.replace_attempt(attempt)
    with pytest.raises(ExecutionBlocked, match="PERMIT_BOUND_OBSERVATION_REQUIRED"):
        repo.record_broker_outcome(
            uuid4(),
            dispatch_nonce=uuid4(),
            outcome_hash="f" * 64,
            claim=claim,
        )


@pytest.mark.parametrize(
    "malformed",
    [
        {"client_order_id": "c1"},
        {"request_hash": "f" * 64},
        {"state": "SUBMITTING"},
        {"provider_order_id": "p2"},
        {"filled_quantity": 1},
        {"quantity": 1},
    ],
)
def test_submit_permit_rejects_caller_chosen_mutation_material(
    malformed: dict[str, object],
) -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    canonical = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, canonical
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )

    with pytest.raises(ExecutionBlocked, match="BROKER_MUTATION_MATERIAL_MISMATCH"):
        repo.prepare_broker_mutation(
            reconciliation,
            replace(canonical, **malformed),
            claim=claim,
        )
    assert repo.attempts_for(claim.intent_id) == ()
    assert repo.get_execution_lock(EXECUTION_ROLE).locked is False


def test_cancel_cannot_target_caller_chosen_order_or_create_ordinal_zero() -> None:
    repo = repository()
    _, order, claim = claimed_submission(repo)
    arbitrary = replace(
        prepared_submit_attempt(order, claim.digest),
        state="NEW",
        provider_order_id="p3",
    )

    with pytest.raises(ExecutionBlocked, match="TARGET_ATTEMPT_NOT_FOUND"):
        repo.broker_reconciliation_expectation(claim, ReconciliationPurpose.CANCEL, arbitrary)
    assert repo.attempts_for(claim.intent_id) == ()


@pytest.mark.parametrize(
    "malformed",
    [
        {"client_order_id": "c2"},
        {"request_hash": "f" * 64},
        {"state": "REPLACING"},
        {"provider_order_id": "p2"},
        {"filled_quantity": 1},
        {"quantity": 1},
        {"replaces_client_order_id": "c4"},
    ],
)
def test_replace_permit_rejects_caller_chosen_mutation_material(
    malformed: dict[str, object],
) -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    current, _ = consume_submit_as_new(repo, baseline_at, order, claim)
    replacement = prepared_quote_replacement(repo, order, claim, current, clock)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.REPLACE, replacement
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(
            replacement.timing_authority_at,
            baseline_at,
            expectation.expected_open_orders[0],
        ),
        expectation,
        accepted_at=replacement.timing_authority_at,
    )

    with pytest.raises(ExecutionBlocked, match="REPLACEMENT_"):
        repo.prepare_broker_mutation(
            reconciliation,
            replace(replacement, **malformed),
            claim=claim,
        )
    assert repo.attempts_for(claim.intent_id) == (current,)
    assert repo.get_execution_lock(EXECUTION_ROLE).locked is False


def test_cancel_permit_rejects_caller_chosen_persisted_target_material() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    current, _ = consume_submit_as_new(repo, baseline_at, order, claim)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.CANCEL, current
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(
            datetime.now(UTC),
            baseline_at,
            expectation.expected_open_orders[0],
        ),
        expectation,
        accepted_at=datetime.now(UTC),
    )

    with pytest.raises(ExecutionBlocked, match="BROKER_MUTATION_MATERIAL_MISMATCH"):
        repo.prepare_broker_mutation(
            reconciliation,
            replace(current, provider_order_id="p2"),
            claim=claim,
        )
    assert repo.attempts_for(claim.intent_id) == (current,)
    assert repo.get_execution_lock(EXECUTION_ROLE).locked is False


@pytest.mark.parametrize("purpose", [ReconciliationPurpose.REPLACE, ReconciliationPurpose.CANCEL])
def test_mutation_permit_requires_sweep_to_match_persisted_target_order(
    purpose: ReconciliationPurpose,
) -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    current, _ = consume_submit_as_new(repo, baseline_at, order, claim)
    if purpose == ReconciliationPurpose.REPLACE:
        mutation_attempt = prepared_quote_replacement(repo, order, claim, current, clock)
    else:
        mutation_attempt = current
    expectation = repo.broker_reconciliation_expectation(claim, purpose, mutation_attempt)
    foreign_target = replace(
        expectation.expected_open_orders[0],
        provider_order_id="p4",
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(datetime.now(UTC), baseline_at, foreign_target),
        expectation,
        accepted_at=datetime.now(UTC),
    )

    prepared = repo.prepare_broker_mutation(reconciliation, mutation_attempt, claim=claim)

    assert prepared.reconciliation.safe is False
    assert prepared.permit is None
    assert prepared.attempt is None
    assert repo.attempts_for(claim.intent_id) == (current,)
    assert repo.get_execution_lock(EXECUTION_ROLE).locked is True


def test_dispatch_outcome_is_permit_bound_and_attempt_observations_are_monotonic() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)
    assert prepared.permit is not None
    dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    assert dispatched.dispatch_nonce is not None
    observed = OrderAttempt(
        intent_id=claim.intent_id,
        ordinal=0,
        client_order_id=attempt.client_order_id,
        request_hash=attempt.request_hash,
        state="PARTIALLY_FILLED",
        provider_order_id="p1",
        filled_quantity=1,
        quantity=2,
        fill_cash_flow=Decimal("-120"),
    )

    observation = repo.record_attempt_observation(
        prepared.permit.permit_id,
        observed,
        source=AttemptObservationSource.DISPATCH_OUTCOME,
        claim=claim,
        dispatch_nonce=dispatched.dispatch_nonce,
    )

    assert observation.permit_id == prepared.permit.permit_id
    assert observation.attempt_ordinal == 0
    assert observation.source == AttemptObservationSource.DISPATCH_OUTCOME
    assert observation.observed_attempt == observed
    assert len(observation.observation_hash) == 64
    assert repo.attempts_for(claim.intent_id) == (observed,)
    assert repo.get_broker_mutation_permit(prepared.permit.permit_id).state == "CONSUMED"
    assert repo.get_attempt_observations(claim.intent_id) == (observation,)

    with pytest.raises(ExecutionBlocked, match="ATTEMPT_FILL_REGRESSION"):
        repo.record_attempt_observation(
            prepared.permit.permit_id,
            replace(observed, filled_quantity=0),
            source=AttemptObservationSource.TARGETED_LOOKUP,
            claim=claim,
        )


@pytest.mark.parametrize(
    ("state", "provider_order_id", "filled_quantity", "quantity", "block_code"),
    (
        ("SETTLED_BY_MAGIC", "p1", 0, 2, "ATTEMPT_STATE_INVALID"),
        ("ASSIGNMENT_LOCKED", "p1", 0, 2, "ATTEMPT_STATE_INVALID"),
        ("CANCELED", None, 0, 2, "ATTEMPT_PROVIDER_ID_REQUIRED"),
        ("CANCELED", " p1", 0, 2, "ATTEMPT_PROVIDER_ID_INVALID"),
        ("NEW", "p1", 1, 2, "ATTEMPT_STATE_FILL_INVALID"),
        ("PARTIALLY_FILLED", "p1", 0, 2, "ATTEMPT_STATE_FILL_INVALID"),
        ("FILLED", "p1", 1, 2, "ATTEMPT_STATE_FILL_INVALID"),
        ("REJECTED", "p1", 1, 2, "ATTEMPT_STATE_FILL_INVALID"),
        ("CANCELED", "p1", 0, 1, "ATTEMPT_QUANTITY_MISMATCH"),
    ),
)
def test_forged_observation_cannot_consume_permit_or_enable_finalization(
    state: str,
    provider_order_id: str | None,
    filled_quantity: int,
    quantity: int,
    block_code: str,
) -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)
    assert prepared.permit is not None
    dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    assert dispatched.dispatch_nonce is not None
    forged = replace(
        attempt,
        state=state,
        provider_order_id=provider_order_id,
        filled_quantity=filled_quantity,
        quantity=quantity,
        fill_cash_flow=(Decimal("-120") * filled_quantity if filled_quantity else None),
    )

    with pytest.raises(ExecutionBlocked, match=block_code):
        repo.record_attempt_observation(
            prepared.permit.permit_id,
            forged,
            source=AttemptObservationSource.DISPATCH_OUTCOME,
            claim=claim,
            dispatch_nonce=dispatched.dispatch_nonce,
        )

    assert repo.get_broker_mutation_permit(prepared.permit.permit_id).state == "DISPATCHING"
    assert repo.attempts_for(claim.intent_id) == (attempt,)
    assert repo.get_attempt_observations(claim.intent_id) == ()
    with pytest.raises(ExecutionBlocked, match="FINAL_RECONCILIATION_LINEAGE_MISSING"):
        repo.final_reconciliation_expectation(claim)


def test_dispatch_crash_enters_lookup_only_state_and_cannot_redispatch() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)
    assert prepared.permit is not None
    dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    assert dispatched.dispatch_nonce is not None

    lookup_only = repo.mark_broker_dispatch_ambiguous(
        prepared.permit.permit_id,
        dispatch_nonce=dispatched.dispatch_nonce,
        claim=claim,
    )

    assert lookup_only.state == "LOOKUP_ONLY"
    with pytest.raises(ExecutionBlocked, match="BROKER_PERMIT_NOT_PREPARED"):
        repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)


def test_sqlite_dispatch_race_has_one_transition_winner(tmp_path: Path) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'authority.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    repo = SQLAlchemyExecutionRepository(
        sessionmaker(engine, expire_on_commit=False),
        entry_limits=ENTRY_LIMITS,
    )
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)
    assert prepared.permit is not None
    barrier = Barrier(2)

    def acquire() -> str:
        barrier.wait()
        try:
            return repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim).state
        except (DBAPIError, ExecutionBlocked) as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _: acquire(), range(2)))

    assert outcomes.count("DISPATCHING") == 1
    assert repo.get_broker_mutation_permit(prepared.permit.permit_id).state == "DISPATCHING"
    with pytest.raises(ExecutionBlocked, match="BROKER_PERMIT_NOT_PREPARED"):
        repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    engine.dispose()


def test_found_order_after_ambiguous_submit_keeps_lookup_only_authority() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)
    assert prepared.permit is not None
    dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    assert dispatched.dispatch_nonce is not None
    repo.mark_broker_dispatch_ambiguous(
        prepared.permit.permit_id,
        dispatch_nonce=dispatched.dispatch_nonce,
        claim=claim,
    )
    observed = replace(
        attempt,
        state="NEW",
        provider_order_id="p1",
        quantity=order.quantity,
    )

    observation = repo.record_attempt_observation(
        prepared.permit.permit_id,
        observed,
        source=AttemptObservationSource.TARGETED_LOOKUP,
        claim=claim,
    )

    assert observation.observed_attempt == observed
    assert repo.attempts_for(claim.intent_id) == (observed,)
    assert repo.get_broker_mutation_permit(prepared.permit.permit_id).state == "LOOKUP_ONLY"
    assert repo.get_execution_lock(EXECUTION_ROLE).locked is False


def test_found_order_after_ambiguous_replace_blocks_another_write() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    current, _ = consume_submit_as_new(repo, baseline_at, order, claim)
    replacement = prepared_quote_replacement(repo, order, claim, current, clock)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.REPLACE, replacement
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(
            replacement.timing_authority_at,
            baseline_at,
            expectation.expected_open_orders[0],
        ),
        expectation,
        accepted_at=replacement.timing_authority_at,
    )
    prepared = repo.prepare_broker_mutation(reconciliation, replacement, claim=claim)
    assert prepared.permit is not None
    dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    assert dispatched.dispatch_nonce is not None
    repo.mark_broker_dispatch_ambiguous(
        prepared.permit.permit_id,
        dispatch_nonce=dispatched.dispatch_nonce,
        claim=claim,
    )
    observed = replace(
        replacement,
        state="NEW",
        provider_order_id="p2",
    )

    repo.record_attempt_observation(
        prepared.permit.permit_id,
        observed,
        source=AttemptObservationSource.TARGETED_LOOKUP,
        claim=claim,
    )

    assert repo.attempts_for(claim.intent_id) == (current, observed)
    assert repo.get_execution_lock(EXECUTION_ROLE).locked is False
    assert repo.next_broker_mutation(claim) is None


def test_missing_lookup_after_ambiguous_submit_keeps_lookup_only_authority() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    prepared = repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)
    assert prepared.permit is not None
    dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    assert dispatched.dispatch_nonce is not None
    repo.mark_broker_dispatch_ambiguous(
        prepared.permit.permit_id,
        dispatch_nonce=dispatched.dispatch_nonce,
        claim=claim,
    )

    observation = repo.record_attempt_absence(
        prepared.permit.permit_id,
        source=AttemptObservationSource.TARGETED_LOOKUP,
        claim=claim,
    )

    assert observation.permit_id == prepared.permit.permit_id
    assert observation.attempt_ordinal == 0
    assert observation.observed_attempt is None
    assert repo.get_attempt_observations(claim.intent_id) == (observation,)
    assert repo.get_execution_lock(EXECUTION_ROLE).locked is False
    assert repo.get_broker_mutation_permit(prepared.permit.permit_id).state == "LOOKUP_ONLY"


def test_replace_permit_targets_consumed_predecessor_and_prepares_next_attempt() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    current, submit_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    replacement = prepared_quote_replacement(repo, order, claim, current, clock)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.REPLACE, replacement
    )

    assert expectation.expected_open_orders == (open_order(current, order),)
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(
            replacement.timing_authority_at,
            baseline_at,
            expectation.expected_open_orders[0],
        ),
        expectation,
        accepted_at=replacement.timing_authority_at,
    )
    prepared = repo.prepare_broker_mutation(reconciliation, replacement, claim=claim)

    assert prepared.reconciliation.safe is True
    assert prepared.attempt == replacement
    assert prepared.permit is not None
    assert prepared.permit.mutation_kind == ReconciliationPurpose.REPLACE
    assert prepared.permit.generation == 1
    assert prepared.permit.predecessor_permit_id == submit_permit.permit_id
    assert prepared.permit.target_client_order_id == current.client_order_id
    assert prepared.permit.target_provider_order_id == current.provider_order_id
    assert repo.attempts_for(claim.intent_id) == (current, replacement)


def test_cancel_permit_targets_current_attempt_without_creating_an_attempt() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    current, submit_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.CANCEL, current
    )

    assert expectation.expected_open_orders == (open_order(current, order),)
    assert expectation.request_hash != current.request_hash
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(datetime.now(UTC), baseline_at, expectation.expected_open_orders[0]),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    prepared = repo.prepare_broker_mutation(reconciliation, current, claim=claim)

    assert prepared.reconciliation.safe is True
    assert prepared.attempt == current
    assert prepared.permit is not None
    assert prepared.permit.mutation_kind == ReconciliationPurpose.CANCEL
    assert prepared.permit.attempt_ordinal == current.ordinal
    assert prepared.permit.generation == 1
    assert prepared.permit.predecessor_permit_id == submit_permit.permit_id
    assert prepared.permit.target_client_order_id == current.client_order_id
    assert prepared.permit.target_provider_order_id == current.provider_order_id
    assert repo.attempts_for(claim.intent_id) == (current,)


def test_expired_undispatched_submit_permit_reuses_attempt_in_linked_generation() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(permit_ttl=timedelta(milliseconds=1), clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)

    def prepare_submit():
        expectation = repo.broker_reconciliation_expectation(
            claim, ReconciliationPurpose.SUBMIT, attempt
        )
        reconciliation = WholeAccountReconciliation.evaluate(
            clean_sweep(datetime.now(UTC), baseline_at),
            expectation,
            accepted_at=datetime.now(UTC),
        )
        return repo.prepare_broker_mutation(reconciliation, attempt, claim=claim)

    first = prepare_submit()
    assert first.permit is not None
    clock.value += timedelta(milliseconds=10)

    class Sweep:
        def collect(self, expectation):
            return WholeAccountEvidence(clean_sweep(clock.value, baseline_at))

    class Broker:
        def submit(self, _order, _client_id):
            return BrokerResult("p1", "NEW", 0, order.quantity)

    result = _execution_service(repo, Broker(), Sweep()).advance(claim.intent_id, Actor.OWNER)
    second = repo.broker_mutation_permits_for(claim.intent_id)[-1]

    assert result.status == "SUBMITTED"
    assert second.generation == 2
    assert second.predecessor_permit_id == first.permit.permit_id
    assert repo.get_broker_mutation_permit(first.permit.permit_id).state == "EXPIRED"
    current = repo.attempts_for(claim.intent_id)[0]
    assert current.state == "NEW"
    cancel_expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.CANCEL, current
    )
    cancel_reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(
            datetime.now(UTC), baseline_at, cancel_expectation.expected_open_orders[0]
        ),
        cancel_expectation,
        accepted_at=datetime.now(UTC),
    )
    cancel = repo.prepare_broker_mutation(cancel_reconciliation, current, claim=claim)

    assert cancel.permit is not None
    assert cancel.permit.predecessor_permit_id == second.permit_id
    assert repo.attempts_for(claim.intent_id) == (current,)


def test_expired_undispatched_replace_permit_refreshes_quotes_and_links_generation() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(
        retryable_permit_ttl=timedelta(milliseconds=1),
        clock=clock,
    )
    baseline_at, order, claim = claimed_submission(repo)
    current, _ = consume_submit_as_new(repo, baseline_at, order, claim)
    replacement = prepared_quote_replacement(repo, order, claim, current, clock)

    def prepare_replacement():
        expectation = repo.broker_reconciliation_expectation(
            claim, ReconciliationPurpose.REPLACE, replacement
        )
        reconciliation = WholeAccountReconciliation.evaluate(
            sweep_with_open_order(
                clock.value,
                baseline_at,
                expectation.expected_open_orders[0],
            ),
            expectation,
            accepted_at=clock.value,
        )
        return repo.prepare_broker_mutation(reconciliation, replacement, claim=claim)

    first = prepare_replacement()
    assert first.permit is not None
    assert first.attempt is not None
    replacement = first.attempt
    clock.value += timedelta(milliseconds=10)

    class Quotes:
        calls = 0

        def collect(self, symbols):
            self.calls += 1
            return replacement_snapshots(symbols, clock.value)

    class Sweep:
        def collect(self, expectation):
            return WholeAccountEvidence(
                sweep_with_open_order(
                    clock.value,
                    baseline_at,
                    expectation.expected_open_orders[0],
                )
            )

    class Broker:
        def replace(self, _provider_order_id, _client_id, _limit):
            return BrokerResult("p2", "NEW", 0, order.quantity)

    quotes = Quotes()
    result = _execution_service(repo, Broker(), Sweep(), quotes).advance(
        claim.intent_id, Actor.OWNER
    )
    second = repo.broker_mutation_permits_for(claim.intent_id)[-1]

    assert result.status == "REPLACED"
    assert second.generation == 2
    assert second.predecessor_permit_id == first.permit.permit_id
    assert repo.get_broker_mutation_permit(first.permit.permit_id).state == "EXPIRED"
    assert repo.attempts_for(claim.intent_id)[-1].state == "NEW"
    assert quotes.calls == 1
    assert second.quote_retrieved_at == clock.value


def test_replacement_dispatch_rechecks_quote_age_and_hard_cancel_deadline() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(retryable_permit_ttl=timedelta(minutes=20), clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    current, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    initial = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert initial.dispatch_acquired_at is not None
    replacement = prepared_quote_replacement(repo, order, claim, current, clock)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.REPLACE, replacement
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(clock.value, baseline_at, expectation.expected_open_orders[0]),
        expectation,
        accepted_at=clock.value,
    )
    prepared = repo.prepare_broker_mutation(reconciliation, replacement, claim=claim)
    assert prepared.permit is not None

    clock.value = replacement.quote_source_timestamps[0] + timedelta(seconds=30, milliseconds=1)
    with pytest.raises(ExecutionBlocked, match="REPLACEMENT_QUOTE_STALE"):
        repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)

    clock.value = initial.dispatch_acquired_at + timedelta(seconds=600, milliseconds=1)
    with pytest.raises(ExecutionBlocked, match="REPLACEMENT_CANCEL_DUE"):
        repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    assert repo.get_broker_mutation_permit(prepared.permit.permit_id).state == "PREPARED"


def test_replacement_prepared_at_599_999_cannot_dispatch_at_600_001() -> None:
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(retryable_permit_ttl=timedelta(minutes=20), clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    current, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    initial = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert initial.dispatch_acquired_at is not None
    prepared_at = initial.dispatch_acquired_at + timedelta(milliseconds=599_999)
    clock.value = prepared_at
    replacement = prepared_quote_replacement(repo, order, claim, current, clock)
    clock.value = prepared_at
    timestamps = (prepared_at - timedelta(seconds=1),) * len(order.legs)
    replacement = replace(
        replacement,
        quote_source_timestamps=timestamps,
        quote_retrieved_at=prepared_at,
        timing_authority_at=prepared_at,
        request_hash=replacement_request_hash(
            claim.digest,
            replacement.ordinal,
            replacement.client_order_id,
            replacement.limit_price,
            replacement.replaces_client_order_id,
            replacement.prior_request_hash,
            replacement.quote_hash,
            timestamps,
            prepared_at,
            prepared_at,
        ),
    )
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.REPLACE, replacement
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(prepared_at, baseline_at, expectation.expected_open_orders[0]),
        expectation,
        accepted_at=prepared_at,
    )
    prepared = repo.prepare_broker_mutation(reconciliation, replacement, claim=claim)
    assert prepared.permit is not None

    clock.value = initial.dispatch_acquired_at + timedelta(milliseconds=600_001)
    with pytest.raises(ExecutionBlocked, match="REPLACEMENT_CANCEL_DUE"):
        repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)


@pytest.mark.parametrize(
    "retryable_permit_ttl",
    [None, timedelta(minutes=20)],
    ids=["default-ttl", "long-retry-ttl"],
)
def test_restart_across_hard_deadline_cancels_a_live_prepared_replacement(
    retryable_permit_ttl: timedelta | None,
    tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'hard-cancel-restart.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    sessions = sessionmaker(engine, expire_on_commit=False)
    repo = SQLAlchemyExecutionRepository(
        sessions,
        retryable_permit_ttl=retryable_permit_ttl,
        trusted_clock=clock,
        entry_limits=ENTRY_LIMITS,
    )
    baseline_at, order, claim = claimed_submission(repo)
    current, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    initial = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert initial.dispatch_acquired_at is not None
    prepared_at = initial.dispatch_acquired_at + timedelta(milliseconds=599_999)
    replacement = prepared_quote_replacement(repo, order, claim, current, clock)
    clock.value = prepared_at
    timestamps = (prepared_at - timedelta(seconds=1),) * len(order.legs)
    replacement = replace(
        replacement,
        quote_source_timestamps=timestamps,
        quote_retrieved_at=prepared_at,
        timing_authority_at=prepared_at,
        request_hash=replacement_request_hash(
            claim.digest,
            replacement.ordinal,
            replacement.client_order_id,
            replacement.limit_price,
            replacement.replaces_client_order_id,
            replacement.prior_request_hash,
            replacement.quote_hash,
            timestamps,
            prepared_at,
            prepared_at,
        ),
    )
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.REPLACE, replacement
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(prepared_at, baseline_at, expectation.expected_open_orders[0]),
        expectation,
        accepted_at=prepared_at,
    )
    prepared = repo.prepare_broker_mutation(reconciliation, replacement, claim=claim)
    assert prepared.permit is not None
    assert prepared.permit.expires_at > initial.dispatch_acquired_at + timedelta(seconds=600)
    clock.value = initial.dispatch_acquired_at + timedelta(milliseconds=600_001)

    class Quotes:
        def collect(self, _symbols):
            raise AssertionError("hard cancellation fetched replacement quotes")

    class Sweep:
        def collect(self, expectation):
            if not expectation.expected_open_orders:
                return WholeAccountEvidence(clean_sweep(clock.value, baseline_at))
            return WholeAccountEvidence(
                sweep_with_open_order(clock.value, baseline_at, expectation.expected_open_orders[0])
            )

    class Broker:
        replace_calls = 0
        cancel_targets: list[str] = []

        def replace(self, *_args):
            self.replace_calls += 1
            raise AssertionError("hard cancellation dispatched a replacement")

        def cancel(self, provider_order_id):
            self.cancel_targets.append(provider_order_id)
            return BrokerResult(provider_order_id, "CANCELED", 0, order.quantity)

    broker = Broker()
    restarted = _execution_service(repo, broker, Sweep(), Quotes())
    result = restarted.advance(claim.intent_id, Actor.OWNER)

    assert result.mutation == ReconciliationPurpose.CANCEL
    assert result.status == "CANCELED"
    assert repo.get_broker_mutation_permit(prepared.permit.permit_id).state == "EXPIRED"
    assert broker.replace_calls == 0
    assert broker.cancel_targets == [current.provider_order_id]
    attempts = repo.attempts_for(claim.intent_id)
    assert tuple(attempt.ordinal for attempt in attempts) == (0, 1)
    assert attempts[1].replaces_client_order_id == current.client_order_id
    assert attempts[1].provider_order_id is None

    clock.value += timedelta(milliseconds=10)
    restarted_repo = SQLAlchemyExecutionRepository(
        sessions,
        retryable_permit_ttl=retryable_permit_ttl,
        trusted_clock=clock,
        entry_limits=ENTRY_LIMITS,
    )
    restarted_again = _execution_service(restarted_repo, broker, Sweep(), Quotes())
    finalized = restarted_again.advance(claim.intent_id, Actor.OWNER)
    repeated = restarted_again.advance(claim.intent_id, Actor.OWNER)

    assert finalized.status == "FINALIZED"
    assert finalized.certificate is not None
    assert finalized.certificate.execution_status == "CANCELED"
    assert finalized.certificate.attempt_ids == (current.client_order_id,)
    assert repeated == finalized
    assert restarted_repo.get_intent(claim.intent_id).state == IntentState.TERMINAL
    assert (
        restarted_repo.get_execution_certificate(finalized.certificate.certificate_id)
        == finalized.certificate
    )
    assert broker.replace_calls == 0
    assert broker.cancel_targets == [current.provider_order_id]
    engine.dispose()


def test_dispatch_deadline_race_retires_replacement_and_cancels_active_predecessor_once() -> None:
    class DispatchRaceClock(FakeDatabaseClock):
        before_deadline: datetime | None = None
        after_deadline: datetime | None = None

        def now(self, session) -> datetime:
            if self.before_deadline is None or self.after_deadline is None:
                return self.value
            prepared_replacement_exists = session.scalar(
                select(BrokerMutationPermitRow.permit_id).where(
                    BrokerMutationPermitRow.mutation_kind == ReconciliationPurpose.REPLACE.value,
                    BrokerMutationPermitRow.state == "PREPARED",
                )
            )
            return (
                self.after_deadline
                if prepared_replacement_exists is not None
                else self.before_deadline
            )

    clock = DispatchRaceClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = repository(retryable_permit_ttl=timedelta(minutes=20), clock=clock)
    baseline_at, order, claim = claimed_submission(repo)
    current, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    initial = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert initial.dispatch_acquired_at is not None
    before_deadline = initial.dispatch_acquired_at + timedelta(milliseconds=599_999)
    after_deadline = initial.dispatch_acquired_at + timedelta(milliseconds=600_001)
    clock.value = before_deadline
    clock.before_deadline = before_deadline
    clock.after_deadline = after_deadline

    class Quotes:
        def collect(self, symbols):
            return replacement_snapshots(symbols, before_deadline)

    class Sweep:
        def collect(self, expectation):
            return WholeAccountEvidence(
                sweep_with_open_order(
                    before_deadline,
                    baseline_at,
                    expectation.expected_open_orders[0],
                )
            )

    class Broker:
        replace_calls = 0
        cancel_targets: list[str] = []

        def replace(self, *_args):
            self.replace_calls += 1
            raise AssertionError("deadline-crossed replacement dispatched")

        def cancel(self, provider_order_id):
            self.cancel_targets.append(provider_order_id)
            return BrokerResult(provider_order_id, "CANCELED", 0, order.quantity)

    broker = Broker()
    result = _execution_service(repo, broker, Sweep(), Quotes()).advance(
        claim.intent_id, Actor.OWNER
    )
    permits = repo.broker_mutation_permits_for(claim.intent_id)
    replacement_permit = next(
        permit for permit in permits if permit.mutation_kind == ReconciliationPurpose.REPLACE
    )
    cancel_permit = next(
        permit for permit in permits if permit.mutation_kind == ReconciliationPurpose.CANCEL
    )

    assert result.mutation == ReconciliationPurpose.CANCEL
    assert result.status == "CANCELED"
    assert broker.replace_calls == 0
    assert broker.cancel_targets == [current.provider_order_id]
    assert replacement_permit.state == "EXPIRED"
    assert cancel_permit.state == "CONSUMED"
    assert cancel_permit.predecessor_permit_id == replacement_permit.permit_id
    assert cancel_permit.target_client_order_id == current.client_order_id
    assert cancel_permit.target_provider_order_id == current.provider_order_id


def test_concurrent_hard_cancel_is_idempotent_for_a_prepared_replacement(tmp_path: Path) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'hard-cancel-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    clock = FakeDatabaseClock(datetime.now(UTC) + timedelta(seconds=1))
    repo = SQLAlchemyExecutionRepository(
        sessionmaker(engine, expire_on_commit=False),
        retryable_permit_ttl=timedelta(minutes=20),
        trusted_clock=clock,
        entry_limits=ENTRY_LIMITS,
    )
    baseline_at, order, claim = claimed_submission(repo)
    current, original_permit = consume_submit_as_new(repo, baseline_at, order, claim)
    initial = repo.get_broker_mutation_permit(original_permit.permit_id)
    assert initial.dispatch_acquired_at is not None
    replacement = prepared_quote_replacement(repo, order, claim, current, clock)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.REPLACE, replacement
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(clock.value, baseline_at, expectation.expected_open_orders[0]),
        expectation,
        accepted_at=clock.value,
    )
    prepared = repo.prepare_broker_mutation(reconciliation, replacement, claim=claim)
    assert prepared.permit is not None
    clock.value = initial.dispatch_acquired_at + timedelta(milliseconds=600_001)

    class Quotes:
        def collect(self, _symbols):
            raise AssertionError("hard cancellation fetched replacement quotes")

    class Sweep:
        def collect(self, expectation):
            if not expectation.expected_open_orders:
                return WholeAccountEvidence(clean_sweep(clock.value, baseline_at))
            return WholeAccountEvidence(
                sweep_with_open_order(
                    clock.value,
                    baseline_at,
                    expectation.expected_open_orders[0],
                )
            )

    class Broker:
        def __init__(self) -> None:
            self.cancel_targets: list[str] = []
            self.lock = Lock()

        def cancel(self, provider_order_id):
            with self.lock:
                self.cancel_targets.append(provider_order_id)
            return BrokerResult(provider_order_id, "CANCELED", 0, order.quantity)

    broker = Broker()
    service = _execution_service(repo, broker, Sweep(), Quotes())
    start = Barrier(2)

    def advance_once() -> str:
        start.wait()
        try:
            return service.advance(claim.intent_id, Actor.OWNER).status
        except (DBAPIError, ExecutionBlocked) as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: advance_once(), range(2)))

    permits = repo.broker_mutation_permits_for(claim.intent_id)
    replacement_permit = next(
        permit for permit in permits if permit.mutation_kind == ReconciliationPurpose.REPLACE
    )
    cancel_permits = tuple(
        permit for permit in permits if permit.mutation_kind == ReconciliationPurpose.CANCEL
    )

    assert "CANCELED" in outcomes
    assert broker.cancel_targets == [current.provider_order_id]
    assert replacement_permit.state == "EXPIRED"
    assert len(cancel_permits) == 1
    assert cancel_permits[0].state == "CONSUMED"
    assert cancel_permits[0].predecessor_permit_id == replacement_permit.permit_id
    assert cancel_permits[0].target_provider_order_id == current.provider_order_id

    clock.value += timedelta(milliseconds=10)
    finalize_start = Barrier(2)

    def finalize_once():
        finalize_start.wait()
        try:
            return service.advance(claim.intent_id, Actor.OWNER)
        except (DBAPIError, ExecutionBlocked) as error:
            assert str(error) not in {
                "FINAL_OBSERVATION_NOT_CURRENT",
                "PREPARED_ATTEMPT_PERMIT_INVALID",
            }
            return service.advance(claim.intent_id, Actor.OWNER)

    with ThreadPoolExecutor(max_workers=2) as executor:
        finalized = tuple(executor.map(lambda _index: finalize_once(), range(2)))

    assert {outcome.status for outcome in finalized} == {"FINALIZED"}
    assert finalized[0].certificate == finalized[1].certificate
    assert finalized[0].certificate is not None
    assert finalized[0].certificate.attempt_ids == (current.client_order_id,)
    assert repo.get_intent(claim.intent_id).state == IntentState.TERMINAL
    assert broker.cancel_targets == [current.provider_order_id]
    assert (
        sum(
            permit.mutation_kind == ReconciliationPurpose.CANCEL
            for permit in repo.broker_mutation_permits_for(claim.intent_id)
        )
        == 1
    )
    engine.dispose()


def test_expired_undispatched_cancel_permit_gets_linked_generation() -> None:
    repo = repository(retryable_permit_ttl=timedelta(milliseconds=1))
    baseline_at, order, claim = claimed_submission(repo)
    current, _ = consume_submit_as_new(repo, baseline_at, order, claim)

    def prepare_cancel():
        expectation = repo.broker_reconciliation_expectation(
            claim, ReconciliationPurpose.CANCEL, current
        )
        reconciliation = WholeAccountReconciliation.evaluate(
            sweep_with_open_order(
                datetime.now(UTC), baseline_at, expectation.expected_open_orders[0]
            ),
            expectation,
            accepted_at=datetime.now(UTC),
        )
        return repo.prepare_broker_mutation(reconciliation, current, claim=claim)

    first = prepare_cancel()
    assert first.permit is not None
    sleep(0.01)
    second = prepare_cancel()

    assert second.permit is not None
    assert second.permit.generation == 2
    assert second.permit.predecessor_permit_id == first.permit.permit_id
    assert second.attempt == current
    assert repo.get_broker_mutation_permit(first.permit.permit_id).state == "EXPIRED"
    assert repo.attempts_for(claim.intent_id) == (current,)


def test_ambiguous_cancel_reauthorization_requires_post_horizon_active_lookup() -> None:
    repo = repository(
        retryable_permit_ttl=timedelta(milliseconds=100),
        network_call_horizon=timedelta(milliseconds=100),
    )
    baseline_at, order, claim = claimed_submission(repo)
    current, _ = consume_submit_as_new(repo, baseline_at, order, claim)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.CANCEL, current
    )
    reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(datetime.now(UTC), baseline_at, expectation.expected_open_orders[0]),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    first = repo.prepare_broker_mutation(reconciliation, current, claim=claim)
    assert first.permit is not None
    dispatched = repo.acquire_broker_dispatch(first.permit.permit_id, claim=claim)
    assert dispatched.dispatch_nonce is not None
    repo.mark_broker_dispatch_ambiguous(
        first.permit.permit_id,
        dispatch_nonce=dispatched.dispatch_nonce,
        claim=claim,
    )

    with pytest.raises(ExecutionBlocked, match="CANCEL_LOOKUP_HORIZON_ACTIVE"):
        repo.record_attempt_observation(
            first.permit.permit_id,
            current,
            source=AttemptObservationSource.TARGETED_LOOKUP,
            claim=claim,
        )

    sleep(0.12)
    repo.record_attempt_observation(
        first.permit.permit_id,
        current,
        source=AttemptObservationSource.TARGETED_LOOKUP,
        claim=claim,
    )
    next_expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.CANCEL, current
    )
    next_reconciliation = WholeAccountReconciliation.evaluate(
        sweep_with_open_order(
            datetime.now(UTC), baseline_at, next_expectation.expected_open_orders[0]
        ),
        next_expectation,
        accepted_at=datetime.now(UTC),
    )

    second = repo.prepare_broker_mutation(next_reconciliation, current, claim=claim)

    assert second.permit is not None
    assert second.permit.generation == 2
    assert second.permit.predecessor_permit_id == first.permit.permit_id


def test_unfilled_finalization_is_bound_to_post_observation_whole_account_provenance() -> None:
    repo = repository()
    baseline_at, order, claim = claimed_submission(repo)
    attempt = prepared_submit_attempt(order, claim.digest)
    expectation = repo.broker_reconciliation_expectation(
        claim, ReconciliationPurpose.SUBMIT, attempt
    )
    preflight = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        expectation,
        accepted_at=datetime.now(UTC),
    )
    prepared = repo.prepare_broker_mutation(preflight, attempt, claim=claim)
    assert prepared.permit is not None
    dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
    assert dispatched.dispatch_nonce is not None
    terminal = replace(
        attempt,
        state="CANCELED",
        provider_order_id="p1",
        quantity=order.quantity,
    )
    observation = repo.record_attempt_observation(
        prepared.permit.permit_id,
        terminal,
        source=AttemptObservationSource.DISPATCH_OUTCOME,
        claim=claim,
        dispatch_nonce=dispatched.dispatch_nonce,
    )
    sleep(0.01)
    final_expectation = repo.final_reconciliation_expectation(claim)
    final_reconciliation = WholeAccountReconciliation.evaluate(
        clean_sweep(datetime.now(UTC), baseline_at),
        final_expectation,
        accepted_at=datetime.now(UTC),
    )
    candidate = ExecutionCertificate(
        certificate_id=uuid5(NAMESPACE_URL, f"alphadecay:execution:{claim.digest}"),
        intent_id=claim.intent_id,
        entry_approval_id=None,
        assessment_certificate_id=order.authorization_certificate_id,
        execution_status="UNFILLED",
        attempt_ids=(terminal.client_order_id,),
        actual_exposure=None,
        reconciliation_checks=(
            "TERMINAL",
            "REMAINDER_ABSENT",
            "WHOLE_ACCOUNT_RECONCILED",
        ),
        created_at=datetime.now(UTC),
    )

    certificate = repo.finalize_execution_authorized(
        candidate,
        final_reconciliation,
        "UNFILLED",
        claim=claim,
    )

    assert certificate.reconciliation_id is not None
    assert certificate.reconciliation_hash is not None
    assert certificate.last_observation_hash == observation.observation_hash
    assert repo.get_execution_certificate(certificate.certificate_id) == certificate


def test_whole_account_authority_migrations_keep_0004_immutable() -> None:
    migrations = discover_migrations(MIGRATIONS)

    assert [migration.version for migration in migrations] == list(range(1, 37))
    assert migrations[3].filename == "0004_whole_account_authority.sql"
    assert migrations[3].sha256 == (
        "ef3cd0cef720a3f8c6cce5812844be929a86b6f93b2168d8c8ccfa875dfd9971"
    )
    assert migrations[4].filename == "0005_execution_authority_hardening.sql"
    assert "DROP CONSTRAINT" in migrations[4].sql
    assert "CREATE TABLE attempt_observations" in migrations[4].sql
    assert "state IN ('PREPARED', 'DISPATCHING', 'LOOKUP_ONLY')" in migrations[4].sql
    assert migrations[5].filename == "0006_execution_book_evolution.sql"
    assert "ADD COLUMN filled_cash_flow numeric(18, 6)" in migrations[5].sql
    assert migrations[6].filename == "0007_reconciliation_state_evolution.sql"
    assert "CREATE OR REPLACE FUNCTION validate_reconciliation_state_insert()" in (
        migrations[6].sql
    )
    assert "RECONCILIATION_STATE_EVOLUTION_UNAUTHORIZED" in migrations[6].sql
    assert "ADD COLUMN resolved_activity_hashes jsonb" in migrations[6].sql
    assert migrations[7].filename == "0008_reconciliation_transition_authority.sql"
    assert "ADD COLUMN predecessor_state_id uuid" in migrations[7].sql
    assert "ADD COLUMN authority_reconciliation_id uuid" in migrations[7].sql
    assert "uq_reconciliation_state_authority" in migrations[7].sql
    assert "authority.expectation_payload -> 'resolved_activity_hashes'" in (migrations[7].sql)
    assert "NEW.transition_hash := expected_transition_hash" in migrations[7].sql
    assert "permit.state = 'CONSUMED'" in migrations[7].sql
    assert migrations[8].filename == "0009_reconciliation_observation_authority.sql"
    assert "ADD COLUMN authority_permit_id uuid" in migrations[8].sql
    assert "ADD COLUMN authority_observation_id uuid" in migrations[8].sql
    assert "permit.mutation_kind IS DISTINCT FROM authority.purpose" in migrations[8].sql
    assert "later.observation_sequence > observation.observation_sequence" in (migrations[8].sql)
    assert "observation.observed_payload ?& ARRAY[" in migrations[8].sql
    assert "permit.request_hash IS DISTINCT FROM" in migrations[8].sql
    assert "attempt.filled_cash_flow" in migrations[8].sql
    assert "attempt.fill_cash_flow" not in migrations[8].sql
    assert OrderAttemptRow.fill_cash_flow.property.columns[0].name == "filled_cash_flow"
    assert "JOIN order_attempts AS attempt" in migrations[8].sql
    assert "observation.attempt_id IS DISTINCT FROM attempt.attempt_id" in migrations[8].sql
    assert "observation.observed_payload ->> 'client_order_id'" in migrations[8].sql
    assert "observation.observed_payload ->> 'provider_order_id'" in migrations[8].sql
    assert "observation.observed_payload ->> 'request_hash'" in migrations[8].sql
    assert "authority.expectation_payload ->> 'purpose'" in migrations[8].sql
    assert "authority.expectation_payload ->> 'request_hash'" in migrations[8].sql
    assert "authority.sweep_payload ->> 'retrieval_started_at'" in migrations[8].sql
    assert "observation.observed_at >" in migrations[8].sql
    assert "later.observation_sequence > observation.observation_sequence" in migrations[8].sql
    assert "later.attempt_ordinal = observation.attempt_ordinal" not in migrations[8].sql
    assert migrations[9].filename == "0010_model_call_ledger.sql"
    assert migrations[10].filename == "0011_quote_derived_replacement_orchestration.sql"
    assert "ADD COLUMN quote_hash varchar(64)" in migrations[10].sql
    assert "DROP TRIGGER broker_mutation_permit_update_guard" in migrations[10].sql
    assert "CREATE TRIGGER broker_mutation_permit_update_guard" in migrations[10].sql


def test_sqlalchemy_metadata_matches_quote_authority_migration_checks() -> None:
    attempt_checks = {
        constraint.name
        for constraint in OrderAttemptRow.__table__.constraints
        if constraint.name is not None
    }
    permit_checks = {
        constraint.name
        for constraint in BrokerMutationPermitRow.__table__.constraints
        if constraint.name is not None
    }

    assert "ck_order_attempt_quote_authority" in attempt_checks
    assert "ck_broker_permit_quote_authority" in permit_checks
    for table in (OrderAttemptRow.__table__, BrokerMutationPermitRow.__table__):
        postgres_ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        sqlite_ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        assert postgres_ddl.count("quote_authority") == 1
        assert "jsonb_array_length" in postgres_ddl
        assert " GLOB " not in postgres_ddl
        assert sqlite_ddl.count("quote_authority") == 1
        assert "json_array_length" in sqlite_ddl
        assert " GLOB " in sqlite_ddl


def test_sqlite_rejects_evolved_book_state_without_observation_authority() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)

    with (
        pytest.raises(DBAPIError, match="ck_reconciliation_state_observation_authority"),
        engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            """
            INSERT INTO account_reconciliation_states (
                state_id, account_role, sequence, account_fingerprint, baseline_id,
                baseline_captured_at, accepted_at, expected_cash, expected_positions,
                expected_open_orders, known_activities, activity_complete_through,
                resolved_activity_hashes, state_hash
            ) VALUES (
                '00000000000000000000000000000411', 'SUBMISSION', 2, :fingerprint,
                '00000000000000000000000000000412', '2026-08-28 14:00:00',
                '2026-08-28 15:00:00', 100000, '[]', '[]', '[]',
                '2026-08-28 14:00:00', '[]', :state_hash
            )
            """,
            {"fingerprint": FINGERPRINT, "state_hash": "4" * 64},
        )


@pytest.mark.skipif(
    "ALPHADECAY_TEST_POSTGRES_URL" not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
def test_postgres_lifecycle_upgrade_rejects_existing_reconciliation_history() -> None:
    engine = create_engine(os.environ["ALPHADECAY_TEST_POSTGRES_URL"])
    if engine.dialect.name != "postgresql":
        pytest.fail("ALPHADECAY_TEST_POSTGRES_URL must use PostgreSQL")
    schema = f"reconciliation_authority_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated_engine = create_engine(
        os.environ["ALPHADECAY_TEST_POSTGRES_URL"],
        connect_args={"startup_params": {"search_path": schema}},
    )
    try:
        migrations = discover_migrations(MIGRATIONS)
        apply_migrations(isolated_engine, migrations[:4])
        with isolated_engine.connect() as connection:
            assert connection.execute(
                text("SELECT array_agg(version ORDER BY version) FROM alphadecay_schema_migrations")
            ).scalar_one() == [1, 2, 3, 4]
        with isolated_engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO account_roles (
                    role, account_fingerprint, equity, autonomous_enabled
                ) VALUES ('SUBMISSION', repeat('a', 64), 100000, false);
                INSERT INTO competition_entry_budget (account_role) VALUES ('SUBMISSION');
                INSERT INTO assessment_certificates (
                    certificate_id, assessment_id, account_role, action,
                    position_fingerprint, envelope_hash, approved_max_loss, quantity,
                    expected_after_exposure, policy_hash, created_at, expires_at, valid
                ) VALUES (
                    '00000000-0000-0000-0000-000000000471',
                    '00000000-0000-0000-0000-000000000472', 'SUBMISSION', 'CLOSE',
                    repeat('b', 64), repeat('c', 64), 700, 2, NULL, repeat('d', 64),
                    '2026-08-28 15:00:00+00', '2026-09-05 15:00:00+00', true
                );
                INSERT INTO execution_intents (
                    intent_id, account_role, intent_digest, action, policy_hash, event_key,
                    trading_day, assessment_certificate_id, fingerprint, envelope_hash,
                    envelope_payload, legs, quantity, minimum_limit, maximum_limit,
                    approved_max_loss, state, claimed_by, claimed_at, claim_token,
                    claim_generation, execution_epoch, heartbeat_at, lease_expires_at
                ) VALUES (
                    '00000000-0000-0000-0000-000000000473', 'SUBMISSION', repeat('e', 64),
                    'CLOSE', repeat('d', 64), 'MIGRATION-FIXTURE', '2026-09-03',
                    '00000000-0000-0000-0000-000000000471', repeat('b', 64), repeat('c', 64),
                    '{}'::jsonb, '[]'::jsonb, 2, 1.00, 1.50, 700, 'CLAIMED', 'OWNER',
                    '2026-08-28 15:00:00+00',
                    '00000000-0000-0000-0000-000000000474', 1, 0,
                    '2026-08-28 15:00:00+00', '2026-08-28 15:01:00+00'
                );
                INSERT INTO whole_account_reconciliations (
                    reconciliation_id, reconciliation_hash, expectation_hash,
                    execution_intent_id, intent_digest, account_role, account_fingerprint,
                    purpose, attempt_ordinal, request_hash, accepted_at, expectation_payload,
                    sweep_payload, positions_manifest_hash, orders_manifest_hash,
                    activities_manifest_hash, safe, block_codes
                ) VALUES (
                    '00000000-0000-0000-0000-000000000475', repeat('f', 64), repeat('1', 64),
                    '00000000-0000-0000-0000-000000000473', repeat('e', 64), 'SUBMISSION',
                    repeat('a', 64), 'SUBMIT', 0, repeat('2', 64),
                    '2026-08-28 15:00:01+00', '{}'::jsonb, '{}'::jsonb,
                    repeat('3', 64), repeat('4', 64), repeat('5', 64), true, '[]'::jsonb
                );
                INSERT INTO broker_mutation_permits (
                    permit_id, reconciliation_id, execution_intent_id, intent_digest,
                    claim_token, claim_generation, execution_epoch, mutation_kind,
                    attempt_ordinal, permit_generation, request_hash, issued_at, expires_at, state
                ) VALUES (
                    '00000000-0000-0000-0000-000000000476',
                    '00000000-0000-0000-0000-000000000475',
                    '00000000-0000-0000-0000-000000000473', repeat('e', 64),
                    '00000000-0000-0000-0000-000000000474', 1, 0, 'SUBMIT', 0, 1,
                    repeat('2', 64), '2026-08-28 15:00:01+00',
                    '2026-08-28 15:00:16+00', 'PREPARED'
                );
                INSERT INTO order_attempts (
                    attempt_id, broker_permit_id, execution_intent_id, attempt_ordinal,
                    client_order_id, state, request_hash, filled_quantity, quantity
                ) VALUES (
                    '00000000-0000-0000-0000-000000000477',
                    '00000000-0000-0000-0000-000000000476',
                    '00000000-0000-0000-0000-000000000473', 0,
                    'migration-fixture-a0', 'PREPARED', repeat('2', 64), 0, 2
                );
                """
            )
        with pytest.raises(
            PGProgrammingError,
            match="LIFECYCLE_AUTHORITY_REQUIRES_VERIFIED_ZERO_HISTORY",
        ):
            apply_migrations(isolated_engine, migrations)
    finally:
        isolated_engine.dispose()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.mark.skipif(
    "ALPHADECAY_TEST_POSTGRES_URL" not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
@pytest.mark.skip(
    reason="strict PostgreSQL authority has no executable submission or development fixture path"
)
def test_postgres_observation_authority_is_exact_and_race_closed() -> None:
    engine = create_engine(os.environ["ALPHADECAY_TEST_POSTGRES_URL"])
    if engine.dialect.name != "postgresql":
        pytest.fail("ALPHADECAY_TEST_POSTGRES_URL must use PostgreSQL")
    schema = f"observation_authority_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated_engine = create_engine(
        os.environ["ALPHADECAY_TEST_POSTGRES_URL"],
        connect_args={"startup_params": {"search_path": schema}},
    )
    try:
        apply_migrations(isolated_engine, discover_migrations(MIGRATIONS))
        repo = SQLAlchemyExecutionRepository(
            sessionmaker(isolated_engine, expire_on_commit=False),
            entry_limits=ENTRY_LIMITS,
        )
        baseline_at, order, claim = claimed_submission(
            repo,
            entry_envelope(),
            role=AccountRole.SUBMISSION,
        )
        attempt = prepared_submit_attempt(order, claim.digest)
        preflight = WholeAccountReconciliation.evaluate(
            clean_sweep(datetime.now(UTC), baseline_at),
            repo.broker_reconciliation_expectation(claim, ReconciliationPurpose.SUBMIT, attempt),
            accepted_at=datetime.now(UTC),
        )
        prepared = repo.prepare_broker_mutation(preflight, attempt, claim=claim)
        assert prepared.permit is not None
        dispatched = repo.acquire_broker_dispatch(prepared.permit.permit_id, claim=claim)
        assert dispatched.dispatch_nonce is not None
        terminal = replace(
            attempt,
            state="CANCELED",
            provider_order_id="p1",
            quantity=order.quantity,
        )
        observation = repo.record_attempt_observation(
            prepared.permit.permit_id,
            terminal,
            source=AttemptObservationSource.DISPATCH_OUTCOME,
            claim=claim,
            dispatch_nonce=dispatched.dispatch_nonce,
        )
        target_time = observation.observed_at + timedelta(milliseconds=10)
        for _ in range(100):
            with isolated_engine.connect() as connection:
                database_time = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
                if database_time >= target_time:
                    break
            sleep(0.002)
        else:
            pytest.fail("PostgreSQL clock did not advance past the observation boundary")
        final_reconciliation = WholeAccountReconciliation.evaluate(
            clean_sweep(datetime.now(UTC), baseline_at),
            repo.final_reconciliation_expectation(claim),
            accepted_at=datetime.now(UTC),
        )
        certificate = repo.finalize_execution_authorized(
            ExecutionCertificate(
                certificate_id=uuid5(NAMESPACE_URL, f"alphadecay:execution:{claim.digest}"),
                intent_id=claim.intent_id,
                entry_approval_id=None,
                assessment_certificate_id=order.authorization_certificate_id,
                execution_status="UNFILLED",
                attempt_ids=(terminal.client_order_id,),
                actual_exposure=None,
                reconciliation_checks=(
                    "TERMINAL",
                    "REMAINDER_ABSENT",
                    "WHOLE_ACCOUNT_RECONCILED",
                ),
                created_at=datetime.now(UTC),
            ),
            final_reconciliation,
            "UNFILLED",
            claim=claim,
        )
        assert certificate is not None
        assert repo.get_reconciliation_state(AccountRole.SUBMISSION).accepted_at >= target_time

        second_authorization = UUID("00000000-0000-0000-0000-000000000461")
        second_intent_id = UUID("00000000-0000-0000-0000-000000000462")
        second_order = replace(
            close_envelope(),
            authorization_certificate_id=second_authorization,
            position_or_book_fingerprint="d" * 64,
            event_key="DEMO-SECOND-2026-09-03",
        )
        repo.add_assessment_certificate(
            AssessmentCertificate(
                certificate_id=second_authorization,
                assessment_id=UUID("00000000-0000-0000-0000-000000000463"),
                thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
                account_role=AccountRole.SUBMISSION,
                action=ExecutionAction.CLOSE,
                position_fingerprint=second_order.position_or_book_fingerprint,
                envelope_hash=order_envelope_hash(second_order),
                approved_max_loss=second_order.approved_max_loss,
                quantity=second_order.quantity,
                expected_after_exposure=None,
                policy_hash=second_order.policy_hash,
                created_at=datetime(2020, 1, 1, tzinfo=UTC),
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
        repo.approve_intent(second_intent_id, AccountRole.SUBMISSION, second_order)
        second_claim = _claim(repo, second_intent_id, Actor.OWNER, now=datetime.now(UTC))
        second_client_id = client_order_id(
            second_order.trading_day,
            second_order.action,
            second_claim.digest,
            0,
        )
        second_attempt = OrderAttempt(
            intent_id=second_intent_id,
            ordinal=0,
            client_order_id=second_client_id,
            request_hash=attempt_request_hash(
                second_claim.digest,
                0,
                second_client_id,
                second_order.minimum_limit,
                None,
            ),
            state="PREPARED",
            quantity=second_order.quantity,
        )
        second_preflight = WholeAccountReconciliation.evaluate(
            clean_sweep(datetime.now(UTC), baseline_at),
            repo.broker_reconciliation_expectation(
                second_claim, ReconciliationPurpose.SUBMIT, second_attempt
            ),
            accepted_at=datetime.now(UTC),
        )
        second_prepared = repo.prepare_broker_mutation(
            second_preflight, second_attempt, claim=second_claim
        )
        assert second_prepared.permit is not None
        second_dispatch = repo.acquire_broker_dispatch(
            second_prepared.permit.permit_id, claim=second_claim
        )
        assert second_dispatch.dispatch_nonce is not None
        second_terminal = replace(
            second_attempt,
            state="CANCELED",
            provider_order_id="p2",
        )
        second_observation = repo.record_attempt_observation(
            second_prepared.permit.permit_id,
            second_terminal,
            source=AttemptObservationSource.DISPATCH_OUTCOME,
            claim=second_claim,
            dispatch_nonce=second_dispatch.dispatch_nonce,
        )
        second_target_time = second_observation.observed_at + timedelta(milliseconds=10)
        for _ in range(100):
            with isolated_engine.connect() as connection:
                if (
                    connection.execute(text("SELECT clock_timestamp()")).scalar_one()
                    >= second_target_time
                ):
                    break
            sleep(0.002)
        else:
            pytest.fail("PostgreSQL clock did not advance past the second observation")
        second_final = WholeAccountReconciliation.evaluate(
            clean_sweep(datetime.now(UTC), baseline_at),
            repo.final_reconciliation_expectation(second_claim),
            accepted_at=datetime.now(UTC),
        )
        second_certificate = repo.finalize_execution_authorized(
            ExecutionCertificate(
                certificate_id=uuid5(NAMESPACE_URL, f"alphadecay:execution:{second_claim.digest}"),
                intent_id=second_claim.intent_id,
                entry_approval_id=None,
                assessment_certificate_id=second_authorization,
                execution_status="UNFILLED",
                attempt_ids=(second_terminal.client_order_id,),
                actual_exposure=None,
                reconciliation_checks=(
                    "TERMINAL",
                    "REMAINDER_ABSENT",
                    "WHOLE_ACCOUNT_RECONCILED",
                ),
                created_at=datetime.now(UTC),
            ),
            second_final,
            "UNFILLED",
            claim=second_claim,
        )
        assert second_certificate is not None
        with isolated_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT array_agg(sequence ORDER BY sequence) "
                    "FROM account_reconciliation_states"
                )
            ).scalar_one() == [1, 2, 3]

        capture_successor = """
            CREATE TEMP TABLE exact_successor ON COMMIT DROP AS
            SELECT * FROM account_reconciliation_states WHERE sequence = 3
        """
        disable_state_append_only = (
            "ALTER TABLE account_reconciliation_states DISABLE TRIGGER "
            "account_reconciliation_states_append_only"
        )
        delete_successor = "DELETE FROM account_reconciliation_states WHERE sequence = 3"
        insert_successor = "INSERT INTO account_reconciliation_states SELECT * FROM exact_successor"
        parameters = {
            "intent_id": second_claim.intent_id,
            "reconciliation_id": second_certificate.reconciliation_id,
            "observation_id": second_observation.observation_id,
            "permit_id": second_prepared.permit.permit_id,
        }

        with isolated_engine.begin() as connection:
            connection.exec_driver_sql(capture_successor)
            connection.exec_driver_sql(disable_state_append_only)
            connection.exec_driver_sql(delete_successor)
            connection.exec_driver_sql(insert_successor)
            assert (
                connection.execute(
                    text("SELECT count(*) FROM account_reconciliation_states WHERE sequence = 3")
                ).scalar_one()
                == 1
            )
            connection.exec_driver_sql(
                "ALTER TABLE account_reconciliation_states ENABLE TRIGGER "
                "account_reconciliation_states_append_only"
            )

        substitutions = (
            (
                "ALTER TABLE whole_account_reconciliations DISABLE TRIGGER "
                "whole_account_reconciliations_append_only",
                """
                UPDATE whole_account_reconciliations
                SET expectation_payload = jsonb_set(
                    expectation_payload, '{request_hash}', to_jsonb(repeat('f', 64))
                )
                WHERE reconciliation_id = :reconciliation_id
                """,
            ),
            (
                "ALTER TABLE whole_account_reconciliations DISABLE TRIGGER "
                "whole_account_reconciliations_append_only",
                """
                UPDATE whole_account_reconciliations SET purpose = 'CANCEL'
                WHERE reconciliation_id = :reconciliation_id
                """,
            ),
            (
                None,
                """
                UPDATE order_attempts SET client_order_id = 'forged-client-order'
                WHERE execution_intent_id = :intent_id AND attempt_ordinal = 0
                """,
            ),
            (
                "ALTER TABLE attempt_observations DISABLE TRIGGER attempt_observations_append_only",
                """
                UPDATE attempt_observations
                SET observed_payload = jsonb_set(
                    observed_payload, '{provider_order_id}', '"forged-provider-order"'
                )
                WHERE observation_id = :observation_id
                """,
            ),
            (
                "ALTER TABLE attempt_observations DISABLE TRIGGER attempt_observations_append_only",
                """
                UPDATE attempt_observations
                SET observed_at = (
                    SELECT (sweep_payload ->> 'retrieval_started_at')::timestamptz
                        + (
                            accepted_at
                            - (sweep_payload ->> 'retrieval_started_at')::timestamptz
                        ) / 2
                    FROM whole_account_reconciliations
                    WHERE reconciliation_id = :reconciliation_id
                )
                WHERE observation_id = :observation_id
                """,
            ),
            (
                "ALTER TABLE broker_mutation_permits DISABLE TRIGGER "
                "broker_mutation_permit_update_guard",
                """
                UPDATE broker_mutation_permits SET request_hash = repeat('f', 64)
                WHERE permit_id = :permit_id
                """,
            ),
        )
        for disable_trigger, mutation in substitutions:
            with (
                pytest.raises(
                    DBAPIError,
                    match="RECONCILIATION_OBSERVATION_AUTHORITY_INVALID",
                ),
                isolated_engine.begin() as connection,
            ):
                connection.exec_driver_sql(capture_successor)
                connection.exec_driver_sql(disable_state_append_only)
                connection.exec_driver_sql(delete_successor)
                if disable_trigger is not None:
                    connection.exec_driver_sql(disable_trigger)
                connection.execute(text(mutation), parameters)
                connection.exec_driver_sql(insert_successor)

        with (
            pytest.raises(
                DBAPIError,
                match="RECONCILIATION_OBSERVATION_AUTHORITY_INVALID",
            ),
            isolated_engine.begin() as connection,
        ):
            connection.exec_driver_sql(capture_successor)
            connection.exec_driver_sql(disable_state_append_only)
            connection.exec_driver_sql(delete_successor)
            connection.execute(
                text(
                    """
                    INSERT INTO order_attempts (
                        attempt_id, execution_intent_id, attempt_ordinal, client_order_id,
                        state, request_hash, filled_quantity, quantity
                    ) VALUES (
                        '00000000-0000-0000-0000-000000000499', :intent_id, 1,
                        'cross-ordinal-client', 'CANCELED', repeat('d', 64), 0, 2
                    )
                    """
                ),
                parameters,
            )
            connection.execute(
                text(
                    """
                    INSERT INTO attempt_observations (
                        observation_id, permit_id, execution_intent_id, attempt_id,
                        attempt_ordinal, observation_sequence, source, provider_present,
                        observed_payload, observed_at, observation_hash
                    ) VALUES (
                        '00000000-0000-0000-0000-000000000498', :permit_id, :intent_id,
                        '00000000-0000-0000-0000-000000000499', 1, 2,
                        'TARGETED_LOOKUP', true, '{}'::jsonb, clock_timestamp(),
                        repeat('c', 64)
                    )
                    """
                ),
                parameters,
            )
            connection.exec_driver_sql(insert_successor)

        barrier = Barrier(2)
        replay_insert = """
            INSERT INTO account_reconciliation_states
            SELECT * FROM account_reconciliation_states WHERE sequence = 3
        """

        def replay(index: int) -> str:
            del index
            barrier.wait()
            try:
                with isolated_engine.begin() as connection:
                    connection.exec_driver_sql(replay_insert)
            except DBAPIError as error:
                return str(error)
            return "ACCEPTED"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(replay, range(2)))
        assert "ACCEPTED" not in results
    finally:
        isolated_engine.dispose()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
