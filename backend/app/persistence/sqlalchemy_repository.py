from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.contracts.v1 import AccountRole, GreekExposure, PositionIntent, SubmissionBaseline
from backend.app.contracts.v1.models import canonical_decimal
from backend.app.execution import (
    ExecutionBlocked,
    attempt_request_hash,
    client_order_id,
    intent_digest,
    order_envelope_hash,
    replacement_request_hash,
)
from backend.app.execution.exposure import reconcile_actual_exposure
from backend.app.execution.models import (
    AccountExecutionLock,
    Actor,
    AssessmentCertificate,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionCertificate,
    ExecutionIntent,
    FrozenThesisVersion,
    IntentState,
    OrderAttempt,
    OrderEnvelope,
    OrderLegIntent,
    PositionGreekObservation,
    Reconciliation,
)
from backend.app.execution.order_status import (
    FINALIZABLE_BROKER_ORDER_STATES,
    LOOKUP_ONLY_BROKER_ORDER_STATES,
    MUTATION_ELIGIBLE_BROKER_ORDER_STATES,
    PENDING_BROKER_ORDER_STATES,
    broker_lookup_policy,
)
from backend.app.execution.permits import (
    AttemptObservation,
    AttemptObservationSource,
    BrokerMutationPermit,
    BrokerMutationPlan,
    BrokerMutationPreparation,
    BrokerMutationSchedule,
)
from backend.app.execution.reconciliation import (
    AccountObservation,
    AccountReconciliationState,
    ActivityItem,
    ActivityPaginationEvidence,
    ActivityType,
    InventoryItem,
    InventoryKind,
    OpenOrderItem,
    OpenOrderLeg,
    ReconciliationBlockCode,
    ReconciliationExpectation,
    ReconciliationPurpose,
    SweepObservation,
    WholeAccountReconciliation,
    activity_predates_window,
)
from backend.app.experiment_lineage import optional_experiment_execution_lineage
from backend.app.order_limits import EntryBudgetLimits

from .agent_authority import agent_input_material, agent_result_material, canonical_agent_hash
from .attempt_observation import validate_attempt_observation
from .authorization import AuthorizationValues, validate_authorization
from .finalization import validate_finalization
from .memory import EntryBudget
from .sqlalchemy_models import (
    AccountReconciliationStateRow,
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AgentTickRow,
    AssessmentCertificateRow,
    AttemptObservationRow,
    BrokerMutationPermitRow,
    CompetitionEntryBudgetRow,
    EntryApprovalCertificateRow,
    ExecutionCertificateRow,
    ExecutionIntentRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    OrderAttemptRow,
    SubmissionBaselineRow,
    ThesisVersionRow,
    WholeAccountReconciliationRow,
)


class TrustedDatabaseClock(Protocol):
    def now(self, session: Session) -> datetime: ...


class SQLAlchemyTrustedDatabaseClock:
    def now(self, session: Session) -> datetime:
        return _trusted_db_now(session)


class SQLAlchemyExecutionRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        permit_ttl: timedelta = timedelta(seconds=15),
        retryable_permit_ttl: timedelta | None = None,
        network_call_horizon: timedelta = timedelta(seconds=30),
        trusted_clock: TrustedDatabaseClock | None = None,
        entry_limits: EntryBudgetLimits | None = None,
    ) -> None:
        if (
            permit_ttl <= timedelta(0)
            or (retryable_permit_ttl is not None and retryable_permit_ttl <= timedelta(0))
            or network_call_horizon <= timedelta(0)
        ):
            raise ValueError("BROKER_PERMIT_TTL_INVALID")
        self._sessions = session_factory
        self._permit_ttl = permit_ttl
        self._retryable_permit_ttl = retryable_permit_ttl or permit_ttl
        self._network_call_horizon = network_call_horizon
        self._trusted_clock = trusted_clock or SQLAlchemyTrustedDatabaseClock()
        self._entry_limits = entry_limits

    def _trusted_now(self, session: Session) -> datetime:
        value = self._trusted_clock.now(session)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExecutionBlocked("TRUSTED_DATABASE_TIME_INVALID")
        return value.astimezone(UTC)

    def register_account(
        self,
        *,
        role: AccountRole,
        fingerprint: str,
        equity: Decimal,
        autonomous_enabled: bool,
    ) -> None:
        _ensure_executable_role(role)
        with self._sessions.begin() as session:
            row = session.get(AccountRoleRow, role.value, with_for_update=True)
            if row is None:
                assigned_role = session.scalar(
                    select(AccountRoleRow.role)
                    .where(AccountRoleRow.account_fingerprint == fingerprint)
                    .with_for_update()
                )
                if assigned_role is not None:
                    raise ExecutionBlocked("ACCOUNT_FINGERPRINT_ROLE_MISMATCH")
                session.add(
                    AccountRoleRow(
                        role=role.value,
                        account_fingerprint=fingerprint,
                        equity=equity,
                        autonomous_enabled=False,
                    )
                )
                session.add(CompetitionEntryBudgetRow(account_role=role.value))
                return
            if row.account_fingerprint != fingerprint:
                raise ExecutionBlocked("ACCOUNT_FINGERPRINT_MISMATCH")
            row.equity = equity

    def set_autonomous_enabled(self, role: AccountRole, enabled: bool, *, actor: Actor) -> None:
        _ensure_executable_role(role)
        if actor != Actor.OWNER:
            raise ExecutionBlocked("SCHEDULER_CANNOT_ENABLE_AUTONOMY")
        with self._sessions.begin() as session:
            row = session.get(AccountRoleRow, role.value, with_for_update=True)
            if row is None:
                raise ExecutionBlocked("ACCOUNT_NOT_REGISTERED")
            row.autonomous_enabled = enabled

    def capture_baseline(
        self,
        *,
        role: AccountRole,
        fingerprint: str,
        equity: Decimal,
        captured_at: datetime,
        positions_hash: str,
        orders_hash: str,
        activities_hash: str,
    ) -> SubmissionBaseline:
        _ensure_executable_role(role)
        with self._sessions.begin() as session:
            account = session.get(AccountRoleRow, role.value, with_for_update=True)
            if role != AccountRole.SUBMISSION or account is None:
                raise ExecutionBlocked("BASELINE_ACCOUNT_MISMATCH")
            if account.account_fingerprint != fingerprint:
                raise ExecutionBlocked("BASELINE_ACCOUNT_MISMATCH")
            if equity != Decimal("100000"):
                raise ExecutionBlocked("BASELINE_EQUITY_INVALID")
            if session.scalar(
                select(SubmissionBaselineRow).where(
                    SubmissionBaselineRow.account_role == role.value
                )
            ):
                raise ExecutionBlocked("BASELINE_ALREADY_CAPTURED")
            session.add(
                SubmissionBaselineRow(
                    baseline_id=uuid5(
                        NAMESPACE_URL, f"alphadecay:baseline:{role.value}:{fingerprint}"
                    ),
                    account_role=role.value,
                    account_fingerprint=fingerprint,
                    equity=equity,
                    captured_at=captured_at,
                    positions_hash=positions_hash,
                    orders_hash=orders_hash,
                    activities_hash=activities_hash,
                    contaminated=False,
                )
            )
        return SubmissionBaseline(
            role=AccountRole.SUBMISSION,
            equity=equity,
            captured_at=captured_at,
            account_fingerprint=fingerprint,
            positions_hash=positions_hash,
            orders_hash=orders_hash,
            activities_hash=activities_hash,
            clean=True,
        )

    def observe_account_adjustment(self, role: AccountRole, activity: str) -> None:
        _ensure_executable_role(role)
        if activity not in {
            "RESET",
            "DEPOSIT",
            "WITHDRAWAL",
            "TRANSFER",
            "JOURNAL",
            "UNKNOWN_CASH",
        }:
            return
        with self._sessions.begin() as session:
            row = session.scalar(
                select(SubmissionBaselineRow)
                .where(SubmissionBaselineRow.account_role == role.value)
                .with_for_update()
            )
            if row is not None:
                row.contaminated = True

    def normalized_return(self, role: AccountRole, current_equity: Decimal) -> Decimal | None:
        _ensure_executable_role(role)
        with self._sessions() as session:
            baseline = session.scalar(
                select(SubmissionBaselineRow).where(
                    SubmissionBaselineRow.account_role == role.value
                )
            )
            if baseline is None or baseline.contaminated:
                return None
            return (current_equity - baseline.equity) / baseline.equity * 100

    def initialize_reconciliation_state(
        self, sweep: SweepObservation
    ) -> AccountReconciliationState:
        if sweep.final_account.role != AccountRole.SUBMISSION:
            raise ExecutionBlocked("RECONCILIATION_STATE_ROLE_INVALID")
        with self._sessions.begin() as session:
            state, baseline = self._validated_initial_reconciliation_state(
                session, sweep, lock=True
            )
            session.add(
                AccountReconciliationStateRow(
                    state_id=state.state_id,
                    account_role=state.account_role.value,
                    sequence=1,
                    account_fingerprint=state.account_fingerprint,
                    baseline_id=baseline.baseline_id,
                    baseline_captured_at=state.baseline_captured_at,
                    accepted_at=state.accepted_at,
                    expected_cash=state.expected_cash,
                    expected_positions=[],
                    expected_open_orders=[],
                    known_activities=[
                        _activity_to_json(activity) for activity in state.known_activities
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
            session.flush()
            return state

    def validate_reconciliation_initialization(
        self, sweep: SweepObservation
    ) -> AccountReconciliationState:
        if sweep.final_account.role != AccountRole.SUBMISSION:
            raise ExecutionBlocked("RECONCILIATION_STATE_ROLE_INVALID")
        with self._sessions() as session:
            state, _ = self._validated_initial_reconciliation_state(session, sweep, lock=False)
            return state

    def _validated_initial_reconciliation_state(
        self, session: Session, sweep: SweepObservation, *, lock: bool
    ) -> tuple[AccountReconciliationState, SubmissionBaselineRow]:
        account = session.get(AccountRoleRow, AccountRole.SUBMISSION.value, with_for_update=lock)
        baseline_query = select(SubmissionBaselineRow).where(
            SubmissionBaselineRow.account_role == AccountRole.SUBMISSION.value
        )
        state_query = (
            select(AccountReconciliationStateRow)
            .where(AccountReconciliationStateRow.account_role == AccountRole.SUBMISSION.value)
            .order_by(AccountReconciliationStateRow.sequence)
        )
        if lock:
            baseline_query = baseline_query.with_for_update()
            state_query = state_query.with_for_update()
        baseline = session.scalar(baseline_query)
        existing = session.scalar(state_query)
        if account is None or baseline is None:
            raise ExecutionBlocked("SUBMISSION_BASELINE_REQUIRED")
        if baseline.contaminated:
            raise ExecutionBlocked("SUBMISSION_BASELINE_CONTAMINATED")
        if existing is not None:
            raise ExecutionBlocked("RECONCILIATION_STATE_ALREADY_INITIALIZED")
        if (
            account.account_fingerprint != baseline.account_fingerprint
            or sweep.final_account.account_fingerprint != account.account_fingerprint
            or sweep.first_account.account_fingerprint != account.account_fingerprint
            or sweep.first_positions
            or sweep.final_positions
            or sweep.first_open_orders
            or sweep.final_open_orders
            or len(sweep.activities) != 1
            or sweep.activities[0].activity_type != ActivityType.INITIAL_FUNDING
            or sweep.activities[0].symbol is not None
            or sweep.activities[0].signed_quantity != baseline.equity
            or sweep.activities[0].occurred_at > _utc(baseline.captured_at)
        ):
            raise ExecutionBlocked("RECONCILIATION_STATE_NOT_CLEAN")
        accepted_at = self._trusted_now(session)
        expectation = ReconciliationExpectation._from_repository_state(
            purpose=ReconciliationPurpose.BASELINE_INITIALIZATION,
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=account.account_fingerprint,
            expected_cash=baseline.equity,
            baseline_captured_at=_utc(baseline.captured_at),
            expected_positions=(),
            expected_open_orders=(),
            known_activities=sweep.activities,
            resolved_activity_hashes=(),
            required_activity_window_start=_utc(baseline.captured_at),
            required_activity_complete_through=_utc(baseline.captured_at),
            intent_id=uuid5(NAMESPACE_URL, "alphadecay:submission-state-anchor"),
            intent_digest="0" * 64,
            attempt_ordinal=0,
            request_hash="0" * 64,
        )
        result = WholeAccountReconciliation.evaluate(sweep, expectation, accepted_at=accepted_at)
        if not result.safe:
            raise ExecutionBlocked("RECONCILIATION_STATE_UNSAFE")
        return (
            AccountReconciliationState._from_repository_state(
                account_role=AccountRole.SUBMISSION,
                account_fingerprint=account.account_fingerprint,
                baseline_captured_at=_utc(baseline.captured_at),
                accepted_at=accepted_at,
                expected_cash=baseline.equity,
                expected_positions=(),
                expected_open_orders=(),
                known_activities=sweep.activities,
                activity_complete_through=_utc(baseline.captured_at),
            ),
            baseline,
        )

    def get_reconciliation_state(self, role: AccountRole) -> AccountReconciliationState:
        _ensure_executable_role(role)
        with self._sessions() as session:
            row = session.scalar(
                select(AccountReconciliationStateRow)
                .where(AccountReconciliationStateRow.account_role == role.value)
                .order_by(AccountReconciliationStateRow.sequence.desc())
            )
            if row is None:
                raise KeyError(role)
            state = AccountReconciliationState._from_repository_state(
                account_role=AccountRole(row.account_role),
                account_fingerprint=row.account_fingerprint,
                baseline_captured_at=_utc(row.baseline_captured_at),
                accepted_at=_utc(row.accepted_at),
                expected_cash=row.expected_cash,
                expected_positions=tuple(
                    _inventory_from_json(item) for item in row.expected_positions
                ),
                expected_open_orders=tuple(
                    _open_order_from_json(item) for item in row.expected_open_orders
                ),
                known_activities=tuple(
                    _activity_from_json(activity) for activity in row.known_activities
                ),
                activity_complete_through=_utc(row.activity_complete_through),
                resolved_activity_hashes=tuple(row.resolved_activity_hashes),
            )
            if state.state_id != row.state_id or state.state_hash != row.state_hash:
                raise ExecutionBlocked("RECONCILIATION_STATE_CORRUPT")
            return state

    def broker_reconciliation_expectation(
        self,
        claim: ExecutionIntent,
        purpose: ReconciliationPurpose,
        attempt: OrderAttempt,
    ) -> ReconciliationExpectation:
        with self._sessions() as session:
            intent = session.get(ExecutionIntentRow, claim.intent_id)
            account = session.get(AccountRoleRow, claim.account_role.value)
            if intent is None or account is None:
                raise ExecutionBlocked("ACCOUNT_OR_INTENT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            state = session.scalar(
                select(AccountReconciliationStateRow)
                .where(AccountReconciliationStateRow.account_role == claim.account_role.value)
                .order_by(AccountReconciliationStateRow.sequence.desc())
            )
            if state is None:
                raise ExecutionBlocked("RECONCILIATION_STATE_REQUIRED")
            attempt_rows = session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == claim.intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
            ).all()
            permit_rows = session.scalars(
                select(BrokerMutationPermitRow)
                .where(BrokerMutationPermitRow.execution_intent_id == claim.intent_id)
                .order_by(
                    BrokerMutationPermitRow.attempt_ordinal,
                    BrokerMutationPermitRow.permit_generation,
                    BrokerMutationPermitRow.permit_id,
                )
            ).all()
            if purpose == ReconciliationPurpose.REPLACE:
                _validate_replacement_attempt(
                    intent,
                    attempt_rows,
                    attempt,
                    self._trusted_now(session),
                )
                _validate_replacement_due(attempt_rows, permit_rows, attempt)
            return _reconciliation_expectation_from_rows(
                intent, state, purpose, attempt, attempt_rows
            )

    def plan_broker_mutation(
        self,
        claim: ExecutionIntent,
        purpose: ReconciliationPurpose,
        replacement: OrderAttempt | None = None,
    ) -> BrokerMutationPlan:
        with self._sessions() as session:
            intent = session.get(ExecutionIntentRow, claim.intent_id)
            account = session.get(AccountRoleRow, claim.account_role.value)
            if intent is None or account is None:
                raise ExecutionBlocked("ACCOUNT_OR_INTENT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            if account.execution_locked:
                raise ExecutionBlocked("ACCOUNT_EXECUTION_LOCKED")
            trusted_now = self._trusted_now(session)
            state = session.scalar(
                select(AccountReconciliationStateRow)
                .where(AccountReconciliationStateRow.account_role == claim.account_role.value)
                .order_by(AccountReconciliationStateRow.sequence.desc())
            )
            if state is None:
                raise ExecutionBlocked("RECONCILIATION_STATE_REQUIRED")
            attempt_rows = session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == claim.intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
            ).all()
            permit_rows = session.scalars(
                select(BrokerMutationPermitRow)
                .where(BrokerMutationPermitRow.execution_intent_id == claim.intent_id)
                .order_by(
                    BrokerMutationPermitRow.attempt_ordinal,
                    BrokerMutationPermitRow.permit_generation,
                    BrokerMutationPermitRow.permit_id,
                )
            ).all()
            if purpose == ReconciliationPurpose.REPLACE and replacement is None:
                raise ExecutionBlocked("REPLACEMENT_QUOTES_REQUIRED")
            if replacement is not None:
                if purpose != ReconciliationPurpose.REPLACE:
                    raise ExecutionBlocked("REPLACEMENT_AUTHORITY_UNEXPECTED")
                _validate_replacement_attempt(intent, attempt_rows, replacement, trusted_now)
                _validate_replacement_due(attempt_rows, permit_rows, replacement)
                attempt = replacement
            else:
                attempt = _canonical_attempt_for_mutation(intent, purpose, attempt_rows)
            expectation = _reconciliation_expectation_from_rows(
                intent,
                state,
                purpose,
                attempt,
                attempt_rows,
            )
            return BrokerMutationPlan(expectation, attempt)

    def next_broker_mutation(
        self,
        claim: ExecutionIntent,
    ) -> BrokerMutationSchedule | None:
        with self._sessions() as session:
            intent = session.get(ExecutionIntentRow, claim.intent_id)
            account = session.get(AccountRoleRow, claim.account_role.value)
            if intent is None or account is None:
                raise ExecutionBlocked("ACCOUNT_OR_INTENT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            if account.execution_locked:
                raise ExecutionBlocked("ACCOUNT_EXECUTION_LOCKED")
            trusted_now = self._trusted_now(session)
            attempt_rows = session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == claim.intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
            ).all()
            if not attempt_rows:
                return BrokerMutationSchedule(ReconciliationPurpose.SUBMIT, trusted_now)
            permit_rows = session.scalars(
                select(BrokerMutationPermitRow)
                .where(BrokerMutationPermitRow.execution_intent_id == claim.intent_id)
                .order_by(
                    BrokerMutationPermitRow.attempt_ordinal,
                    BrokerMutationPermitRow.permit_generation,
                    BrokerMutationPermitRow.permit_id,
                )
            ).all()
            execution_attempt_rows = _execution_attempt_rows(attempt_rows, permit_rows)
            latest = execution_attempt_rows[-1]
            if latest.state == "PREPARED":
                creation_permit = _creation_permit(latest, permit_rows)
                if creation_permit.state in {"DISPATCHING", "LOOKUP_ONLY"}:
                    return None
                if creation_permit.state != "PREPARED":
                    raise ExecutionBlocked("PREPARED_ATTEMPT_PERMIT_INVALID")
                if latest.attempt_ordinal > 0:
                    initial_permit = _creation_permit(execution_attempt_rows[0], permit_rows)
                    if initial_permit.dispatch_acquired_at is None:
                        raise ExecutionBlocked("INITIAL_DISPATCH_TIME_MISSING")
                    if trusted_now - _utc(initial_permit.dispatch_acquired_at) >= timedelta(
                        seconds=600
                    ):
                        return BrokerMutationSchedule(ReconciliationPurpose.CANCEL, trusted_now)
                if _utc(creation_permit.expires_at) > trusted_now:
                    return None
                purpose = (
                    ReconciliationPurpose.SUBMIT
                    if latest.attempt_ordinal == 0
                    else ReconciliationPurpose.REPLACE
                )
                return BrokerMutationSchedule(purpose, trusted_now)
            if latest.state not in MUTATION_ELIGIBLE_BROKER_ORDER_STATES:
                return None
            if any(
                permit.state in {"PREPARED", "DISPATCHING", "LOOKUP_ONLY"} for permit in permit_rows
            ):
                return None
            observations = session.scalars(
                select(AttemptObservationRow)
                .where(
                    AttemptObservationRow.execution_intent_id == claim.intent_id,
                    AttemptObservationRow.attempt_ordinal == latest.attempt_ordinal,
                )
                .order_by(
                    AttemptObservationRow.observation_sequence,
                    AttemptObservationRow.observation_id,
                )
            ).all()
            if not any(
                observation.source == AttemptObservationSource.TARGETED_LOOKUP.value
                and observation.observed_payload is not None
                for observation in observations
            ):
                return None
            initial_permit = _creation_permit(execution_attempt_rows[0], permit_rows)
            if initial_permit.dispatch_acquired_at is None:
                raise ExecutionBlocked("INITIAL_DISPATCH_TIME_MISSING")
            elapsed = trusted_now - _utc(initial_permit.dispatch_acquired_at)
            if latest.state == "PARTIALLY_FILLED" or elapsed >= timedelta(seconds=600):
                return BrokerMutationSchedule(ReconciliationPurpose.CANCEL, trusted_now)
            due_seconds = (150, 300, 450)
            if latest.attempt_ordinal < 3 and elapsed >= timedelta(
                seconds=due_seconds[latest.attempt_ordinal]
            ):
                return BrokerMutationSchedule(ReconciliationPurpose.REPLACE, trusted_now)
            return None

    def trusted_execution_time(self, claim: ExecutionIntent) -> datetime:
        with self._sessions() as session:
            intent = session.get(ExecutionIntentRow, claim.intent_id)
            account = session.get(AccountRoleRow, claim.account_role.value)
            if intent is None or account is None:
                raise ExecutionBlocked("ACCOUNT_OR_INTENT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            return self._trusted_now(session)

    def final_reconciliation_expectation(self, claim: ExecutionIntent) -> ReconciliationExpectation:
        with self._sessions() as session:
            intent = session.get(ExecutionIntentRow, claim.intent_id)
            account = session.get(AccountRoleRow, claim.account_role.value)
            if intent is None or account is None:
                raise ExecutionBlocked("ACCOUNT_OR_INTENT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            state = session.scalar(
                select(AccountReconciliationStateRow)
                .where(AccountReconciliationStateRow.account_role == claim.account_role.value)
                .order_by(AccountReconciliationStateRow.sequence.desc())
            )
            if state is None:
                raise ExecutionBlocked("RECONCILIATION_STATE_REQUIRED")
            attempt_rows = session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == claim.intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
            ).all()
            observations = session.scalars(
                select(AttemptObservationRow)
                .where(AttemptObservationRow.execution_intent_id == claim.intent_id)
                .order_by(
                    AttemptObservationRow.observation_sequence,
                    AttemptObservationRow.observation_id,
                )
            ).all()
            permit_rows = session.scalars(
                select(BrokerMutationPermitRow)
                .where(BrokerMutationPermitRow.execution_intent_id == claim.intent_id)
                .order_by(
                    BrokerMutationPermitRow.attempt_ordinal,
                    BrokerMutationPermitRow.permit_generation,
                    BrokerMutationPermitRow.permit_id,
                )
            ).all()
            latest_permit = (
                session.get(BrokerMutationPermitRow, observations[-1].permit_id)
                if observations
                else None
            )
            return _final_reconciliation_expectation_from_rows(
                intent,
                state,
                _execution_attempt_rows(attempt_rows, permit_rows),
                observations,
                latest_permit,
            )

    def prepare_broker_mutation(
        self,
        reconciliation: WholeAccountReconciliation,
        attempt: OrderAttempt,
        *,
        claim: ExecutionIntent,
    ) -> BrokerMutationPreparation:
        try:
            with self._sessions.begin() as session:
                intent = session.scalar(
                    select(ExecutionIntentRow)
                    .where(ExecutionIntentRow.intent_id == claim.intent_id)
                    .with_for_update()
                )
                if intent is None:
                    raise KeyError(claim.intent_id)
                account = session.scalar(
                    select(AccountRoleRow)
                    .where(AccountRoleRow.role == intent.account_role)
                    .with_for_update()
                )
                budget = session.scalar(
                    select(CompetitionEntryBudgetRow)
                    .where(CompetitionEntryBudgetRow.account_role == intent.account_role)
                    .with_for_update()
                )
                if account is None or budget is None:
                    raise ExecutionBlocked("ACCOUNT_NOT_REGISTERED")
                self._verify_claim_fence(intent, account, claim)
                if account.execution_locked:
                    raise ExecutionBlocked("ACCOUNT_EXECUTION_LOCKED")
                trusted_now = self._trusted_now(session)
                self._validate_origin(session, self._intent_from_row(session, intent), trusted_now)
                baseline = session.scalar(
                    select(SubmissionBaselineRow)
                    .where(SubmissionBaselineRow.account_role == intent.account_role)
                    .with_for_update()
                )
                if intent.account_role == AccountRole.SUBMISSION.value and (
                    baseline is None or baseline.contaminated
                ):
                    raise ExecutionBlocked("SUBMISSION_BASELINE_INVALID")
                attempt_rows = session.scalars(
                    select(OrderAttemptRow)
                    .where(OrderAttemptRow.execution_intent_id == intent.intent_id)
                    .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
                    .with_for_update()
                ).all()
                state = session.scalar(
                    select(AccountReconciliationStateRow)
                    .where(AccountReconciliationStateRow.account_role == intent.account_role)
                    .order_by(AccountReconciliationStateRow.sequence.desc())
                    .with_for_update()
                )
                if state is None:
                    raise ExecutionBlocked("RECONCILIATION_STATE_REQUIRED")
                session.scalars(
                    select(WholeAccountReconciliationRow)
                    .where(WholeAccountReconciliationRow.execution_intent_id == intent.intent_id)
                    .order_by(
                        WholeAccountReconciliationRow.accepted_at,
                        WholeAccountReconciliationRow.reconciliation_id,
                    )
                    .with_for_update()
                ).all()
                permit_rows = session.scalars(
                    select(BrokerMutationPermitRow)
                    .where(BrokerMutationPermitRow.execution_intent_id == intent.intent_id)
                    .order_by(
                        BrokerMutationPermitRow.attempt_ordinal,
                        BrokerMutationPermitRow.permit_generation,
                        BrokerMutationPermitRow.permit_id,
                    )
                    .with_for_update()
                ).all()
                observation_rows = session.scalars(
                    select(AttemptObservationRow)
                    .where(AttemptObservationRow.execution_intent_id == intent.intent_id)
                    .order_by(
                        AttemptObservationRow.observation_sequence,
                        AttemptObservationRow.observation_id,
                    )
                    .with_for_update()
                ).all()
                trusted_now = self._trusted_now(session)
                purpose = reconciliation.expectation.purpose
                if purpose == ReconciliationPurpose.REPLACE:
                    _validate_replacement_attempt(intent, attempt_rows, attempt, None)
                    persisted_reauthorization = bool(
                        attempt_rows
                        and attempt_rows[-1].attempt_ordinal == attempt.ordinal
                        and attempt_rows[-1].state == "PREPARED"
                    )
                    if not persisted_reauthorization:
                        attempt = _replacement_with_timing(intent, attempt, trusted_now)
                    _validate_replacement_attempt(
                        intent,
                        attempt_rows,
                        attempt,
                        trusted_now,
                    )
                    _validate_replacement_due(attempt_rows, permit_rows, attempt)
                account.equity = reconciliation.sweep.final_account.equity
                try:
                    expectation = _reconciliation_expectation_from_rows(
                        intent,
                        state,
                        purpose,
                        attempt,
                        attempt_rows,
                        reconciliation.sweep.activities,
                    )
                except ExecutionBlocked as error:
                    if str(error) not in {
                        "ATTEMPT_ACTIVITY_EVIDENCE_MISMATCH",
                        "KNOWN_ACTIVITY_MISSING",
                    }:
                        raise
                    _latch_account(account, "RECONCILIATION_MISMATCH", trusted_now)
                    session.flush()
                    return BrokerMutationPreparation(reconciliation, None, None)
                evaluated = WholeAccountReconciliation.evaluate(
                    reconciliation.sweep,
                    expectation,
                    accepted_at=trusted_now,
                )
                session.add(_reconciliation_row(evaluated))
                session.flush()
                if not evaluated.safe:
                    reason = (
                        "ASSIGNMENT_SUSPECTED"
                        if ReconciliationBlockCode.ASSIGNMENT_SUSPECTED in evaluated.block_codes
                        else "RECONCILIATION_MISMATCH"
                    )
                    _latch_account(account, reason, trusted_now)
                    session.flush()
                    return BrokerMutationPreparation(evaluated, None, None)
                if (
                    intent.action == ExecutionAction.ENTRY.value
                    and purpose == ReconciliationPurpose.SUBMIT
                ):
                    try:
                        _validate_entry_limits(
                            self._entry_limits,
                            intent=intent,
                            account=account,
                            budget=budget,
                        )
                    except ExecutionBlocked as error:
                        _latch_account(account, str(error), trusted_now)
                        session.flush()
                        return BrokerMutationPreparation(evaluated, None, None)
                matching_permits = [
                    row
                    for row in permit_rows
                    if row.mutation_kind == purpose.value and row.attempt_ordinal == attempt.ordinal
                ]
                prior_generation = matching_permits[-1] if matching_permits else None
                generation = (
                    prior_generation.permit_generation + 1 if prior_generation is not None else 1
                )
                create_attempt = False
                replaces_id = None
                target_attempt: OrderAttemptRow | None = None
                if purpose == ReconciliationPurpose.SUBMIT:
                    if attempt_rows:
                        existing = _attempt_from_row(attempt_rows[0], attempt_rows)
                        if len(attempt_rows) != 1 or existing != attempt:
                            raise ExecutionBlocked("SUBMIT_ATTEMPT_ALREADY_EXISTS")
                        if prior_generation is None:
                            raise ExecutionBlocked("ATTEMPT_PERMIT_MISSING")
                        _expire_prepared_generation(
                            prior_generation, trusted_now, expectation.request_hash
                        )
                        predecessor_permit_id = prior_generation.permit_id
                    else:
                        if prior_generation is not None:
                            raise ExecutionBlocked("SUBMIT_PERMIT_ALREADY_EXISTS")
                        create_attempt = True
                        predecessor_permit_id = None
                elif purpose == ReconciliationPurpose.REPLACE:
                    if not attempt_rows:
                        raise ExecutionBlocked("REPLACE_ORDINAL_INVALID")
                    persisted = _attempt_from_row(attempt_rows[-1], attempt_rows)
                    if persisted.ordinal == attempt.ordinal and persisted.state == "PREPARED":
                        if prior_generation is None or len(attempt_rows) < 2:
                            raise ExecutionBlocked("REPLACE_PERMIT_MISSING")
                        _expire_prepared_generation(
                            prior_generation, trusted_now, prior_generation.request_hash
                        )
                        target_attempt = attempt_rows[-2]
                        predecessor_permit_id = prior_generation.permit_id
                        _refresh_prepared_replacement(attempt_rows[-1], attempt)
                    else:
                        if attempt.ordinal != len(attempt_rows):
                            raise ExecutionBlocked("REPLACE_ORDINAL_INVALID")
                        target_attempt = attempt_rows[-1]
                        target_permit = _creation_permit(target_attempt, permit_rows)
                        if target_permit.state != "CONSUMED":
                            raise ExecutionBlocked("PREDECESSOR_PERMIT_UNRESOLVED")
                        if prior_generation is not None:
                            raise ExecutionBlocked("REPLACE_PERMIT_ALREADY_EXISTS")
                        predecessor_permit_id = target_permit.permit_id
                        replaces_id = target_attempt.attempt_id
                        create_attempt = True
                elif purpose == ReconciliationPurpose.CANCEL:
                    if not attempt_rows:
                        raise ExecutionBlocked("CANCEL_TARGET_NOT_FOUND")
                    target_attempt = attempt_rows[-1]
                    stale_replacement_permit: BrokerMutationPermitRow | None = None
                    if target_attempt.state == "PREPARED" and len(attempt_rows) >= 2:
                        stale_replacement_permit = _creation_permit(target_attempt, permit_rows)
                        _supersede_prepared_replacement_for_hard_cancel(
                            stale_replacement_permit,
                            _creation_permit(attempt_rows[0], permit_rows),
                            trusted_now,
                        )
                        target_attempt = attempt_rows[-2]
                    target_permit = _creation_permit(target_attempt, permit_rows)
                    if target_permit.state != "CONSUMED":
                        raise ExecutionBlocked("PREDECESSOR_PERMIT_UNRESOLVED")
                    if prior_generation is not None:
                        if prior_generation.state == "PREPARED":
                            _expire_prepared_generation(
                                prior_generation, trusted_now, expectation.request_hash
                            )
                        elif prior_generation.state == "CONSUMED":
                            _verify_consumed_cancel_retry(
                                prior_generation,
                                target_attempt,
                                attempt_rows,
                                observation_rows,
                                self._network_call_horizon,
                            )
                        else:
                            raise ExecutionBlocked("CANCEL_PERMIT_REAUTHORIZATION_NOT_PROVEN")
                        predecessor_permit_id = prior_generation.permit_id
                    else:
                        predecessor_permit_id = (
                            stale_replacement_permit.permit_id
                            if stale_replacement_permit is not None
                            else target_permit.permit_id
                        )
                else:
                    raise ExecutionBlocked("BASELINE_INITIALIZATION_IS_NOT_BROKER_MUTATION")
                permit_id = uuid5(
                    NAMESPACE_URL,
                    f"alphadecay:broker-permit:{evaluated.reconciliation_hash}:{generation}",
                )
                permit = BrokerMutationPermit(
                    permit_id=permit_id,
                    reconciliation_id=evaluated.reconciliation_id,
                    intent_id=intent.intent_id,
                    intent_digest=intent.intent_digest,
                    claim_token=intent.claim_token,
                    claim_generation=intent.claim_generation,
                    execution_epoch=intent.execution_epoch,
                    mutation_kind=purpose,
                    attempt_ordinal=attempt.ordinal,
                    generation=generation,
                    predecessor_permit_id=predecessor_permit_id,
                    request_hash=expectation.request_hash,
                    target_client_order_id=(
                        target_attempt.client_order_id if target_attempt is not None else None
                    ),
                    target_provider_order_id=(
                        target_attempt.provider_order_id if target_attempt is not None else None
                    ),
                    issued_at=trusted_now,
                    expires_at=trusted_now
                    + (
                        self._retryable_permit_ttl
                        if purpose
                        in {
                            ReconciliationPurpose.REPLACE,
                            ReconciliationPurpose.CANCEL,
                        }
                        else self._permit_ttl
                    ),
                    state="PREPARED",
                    limit_price=(
                        attempt.limit_price
                        if attempt.limit_price is not None
                        else intent.minimum_limit
                    ),
                    quote_hash=attempt.quote_hash,
                    quote_source_timestamps=attempt.quote_source_timestamps,
                    quote_retrieved_at=attempt.quote_retrieved_at,
                    timing_authority_at=attempt.timing_authority_at,
                    prior_request_hash=attempt.prior_request_hash,
                )
                session.add(_permit_row(permit))
                if create_attempt:
                    session.add(
                        OrderAttemptRow(
                            attempt_id=_attempt_id(attempt.intent_id, attempt.ordinal),
                            broker_permit_id=permit.permit_id,
                            execution_intent_id=attempt.intent_id,
                            attempt_ordinal=attempt.ordinal,
                            client_order_id=attempt.client_order_id,
                            provider_order_id=attempt.provider_order_id,
                            state=attempt.state,
                            request_hash=attempt.request_hash,
                            limit_price=attempt.limit_price,
                            quote_hash=attempt.quote_hash,
                            quote_source_timestamps=[
                                value.isoformat() for value in attempt.quote_source_timestamps
                            ],
                            quote_retrieved_at=attempt.quote_retrieved_at,
                            timing_authority_at=attempt.timing_authority_at,
                            prior_request_hash=attempt.prior_request_hash,
                            replaces_attempt_id=replaces_id,
                            filled_quantity=attempt.filled_quantity,
                            quantity=attempt.quantity,
                            fill_cash_flow=attempt.fill_cash_flow,
                        )
                    )
                session.flush()
                return BrokerMutationPreparation(evaluated, permit, attempt)
        except IntegrityError as error:
            raise ExecutionBlocked("BROKER_MUTATION_PREPARATION_CONFLICT") from error

    def get_whole_account_reconciliation(
        self, reconciliation_id: UUID
    ) -> WholeAccountReconciliation:
        with self._sessions() as session:
            row = session.get(WholeAccountReconciliationRow, reconciliation_id)
            if row is None:
                raise KeyError(reconciliation_id)
            return _reconciliation_from_row(row)

    def get_broker_mutation_permit(self, permit_id: UUID) -> BrokerMutationPermit:
        with self._sessions() as session:
            row = session.get(BrokerMutationPermitRow, permit_id)
            if row is None:
                raise KeyError(permit_id)
            return _permit_from_row(row)

    def broker_mutation_permits_for(self, intent_id: UUID) -> tuple[BrokerMutationPermit, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(BrokerMutationPermitRow)
                .where(BrokerMutationPermitRow.execution_intent_id == intent_id)
                .order_by(
                    BrokerMutationPermitRow.attempt_ordinal,
                    BrokerMutationPermitRow.permit_generation,
                    BrokerMutationPermitRow.permit_id,
                )
            ).all()
            return tuple(_permit_from_row(row) for row in rows)

    def acquire_broker_dispatch(
        self,
        permit_id: UUID,
        *,
        claim: ExecutionIntent,
    ) -> BrokerMutationPermit:
        with self._sessions.begin() as session:
            identity = session.get(BrokerMutationPermitRow, permit_id)
            if identity is None:
                raise KeyError(permit_id)
            intent = session.get(
                ExecutionIntentRow,
                identity.execution_intent_id,
                with_for_update=True,
            )
            if intent is None:
                raise ExecutionBlocked("BROKER_PERMIT_INTENT_MISSING")
            account = session.get(AccountRoleRow, intent.account_role, with_for_update=True)
            if account is None:
                raise ExecutionBlocked("ACCOUNT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            if account.execution_locked:
                raise ExecutionBlocked("ACCOUNT_EXECUTION_LOCKED")
            permit = session.get(BrokerMutationPermitRow, permit_id, with_for_update=True)
            if permit is None:
                raise KeyError(permit_id)
            trusted_now = self._trusted_now(session)
            if permit.state != "PREPARED":
                raise ExecutionBlocked("BROKER_PERMIT_NOT_PREPARED")
            if _utc(permit.expires_at) <= trusted_now:
                raise ExecutionBlocked("BROKER_PERMIT_EXPIRED")
            if (
                permit.intent_digest != intent.intent_digest
                or permit.claim_token != intent.claim_token
                or permit.claim_generation != intent.claim_generation
                or permit.execution_epoch != intent.execution_epoch
            ):
                raise ExecutionBlocked("BROKER_PERMIT_FENCE_MISMATCH")
            attempt = session.scalar(
                select(OrderAttemptRow).where(
                    OrderAttemptRow.execution_intent_id == intent.intent_id,
                    OrderAttemptRow.attempt_ordinal == permit.attempt_ordinal,
                )
            )
            if attempt is None:
                raise ExecutionBlocked("BROKER_PERMIT_ATTEMPT_MISSING")
            _verify_permit_material(permit, attempt, intent)
            if permit.mutation_kind == ReconciliationPurpose.REPLACE.value:
                permit_rows = session.scalars(
                    select(BrokerMutationPermitRow)
                    .where(BrokerMutationPermitRow.execution_intent_id == intent.intent_id)
                    .order_by(
                        BrokerMutationPermitRow.attempt_ordinal,
                        BrokerMutationPermitRow.permit_generation,
                        BrokerMutationPermitRow.permit_id,
                    )
                ).all()
                initial_attempt = session.scalar(
                    select(OrderAttemptRow).where(
                        OrderAttemptRow.execution_intent_id == intent.intent_id,
                        OrderAttemptRow.attempt_ordinal == 0,
                    )
                )
                if initial_attempt is None:
                    raise ExecutionBlocked("INITIAL_ATTEMPT_MISSING")
                trusted_now = self._trusted_now(session)
                if _utc(permit.expires_at) <= trusted_now:
                    raise ExecutionBlocked("BROKER_PERMIT_EXPIRED")
                _validate_replacement_dispatch_authority(
                    attempt,
                    _creation_permit(initial_attempt, permit_rows),
                    trusted_now,
                )
            dispatch_nonce = uuid4()
            transition = session.execute(
                update(BrokerMutationPermitRow)
                .where(
                    BrokerMutationPermitRow.permit_id == permit_id,
                    BrokerMutationPermitRow.state == "PREPARED",
                    BrokerMutationPermitRow.dispatch_nonce.is_(None),
                    BrokerMutationPermitRow.dispatch_acquired_at.is_(None),
                    BrokerMutationPermitRow.consumed_at.is_(None),
                    BrokerMutationPermitRow.outcome_hash.is_(None),
                    BrokerMutationPermitRow.expires_at > trusted_now,
                    BrokerMutationPermitRow.intent_digest == intent.intent_digest,
                    BrokerMutationPermitRow.claim_token == intent.claim_token,
                    BrokerMutationPermitRow.claim_generation == intent.claim_generation,
                    BrokerMutationPermitRow.execution_epoch == intent.execution_epoch,
                )
                .values(
                    state="DISPATCHING",
                    dispatch_nonce=dispatch_nonce,
                    dispatch_acquired_at=trusted_now,
                )
                .execution_options(synchronize_session=False)
            )
            if transition.rowcount != 1:
                raise ExecutionBlocked("BROKER_PERMIT_NOT_PREPARED")
            session.expire(permit)
            session.refresh(permit)
            return _permit_from_row(permit)

    def record_broker_outcome(
        self,
        permit_id: UUID,
        *,
        dispatch_nonce: UUID,
        outcome_hash: str,
        claim: ExecutionIntent,
    ) -> BrokerMutationPermit:
        del permit_id, dispatch_nonce, outcome_hash, claim
        raise ExecutionBlocked("PERMIT_BOUND_OBSERVATION_REQUIRED")

    def mark_broker_dispatch_ambiguous(
        self,
        permit_id: UUID,
        *,
        dispatch_nonce: UUID,
        claim: ExecutionIntent,
    ) -> BrokerMutationPermit:
        with self._sessions.begin() as session:
            identity = session.get(BrokerMutationPermitRow, permit_id)
            if identity is None:
                raise KeyError(permit_id)
            intent = session.get(
                ExecutionIntentRow, identity.execution_intent_id, with_for_update=True
            )
            if intent is None:
                raise ExecutionBlocked("BROKER_PERMIT_INTENT_MISSING")
            account = session.get(AccountRoleRow, intent.account_role, with_for_update=True)
            if account is None:
                raise ExecutionBlocked("ACCOUNT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            permit = session.get(BrokerMutationPermitRow, permit_id, with_for_update=True)
            if permit is None:
                raise KeyError(permit_id)
            if permit.state != "DISPATCHING":
                raise ExecutionBlocked("BROKER_PERMIT_NOT_DISPATCHING")
            if permit.dispatch_nonce != dispatch_nonce:
                raise ExecutionBlocked("BROKER_DISPATCH_NONCE_MISMATCH")
            _verify_permit_fence(permit, intent)
            permit.state = "LOOKUP_ONLY"
            session.flush()
            return _permit_from_row(permit)

    def record_attempt_observation(
        self,
        permit_id: UUID,
        observed: OrderAttempt,
        *,
        source: AttemptObservationSource,
        claim: ExecutionIntent,
        dispatch_nonce: UUID | None = None,
    ) -> AttemptObservation:
        with self._sessions.begin() as session:
            identity = session.get(BrokerMutationPermitRow, permit_id)
            if identity is None:
                raise KeyError(permit_id)
            intent = session.get(
                ExecutionIntentRow, identity.execution_intent_id, with_for_update=True
            )
            if intent is None:
                raise ExecutionBlocked("BROKER_PERMIT_INTENT_MISSING")
            account = session.get(AccountRoleRow, intent.account_role, with_for_update=True)
            if account is None:
                raise ExecutionBlocked("ACCOUNT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            if account.execution_locked and source != AttemptObservationSource.TARGETED_LOOKUP:
                raise ExecutionBlocked("ACCOUNT_EXECUTION_LOCKED")
            attempts = session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == intent.intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
                .with_for_update()
            ).all()
            permit = session.get(BrokerMutationPermitRow, permit_id, with_for_update=True)
            if permit is None:
                raise KeyError(permit_id)
            observation_rows = session.scalars(
                select(AttemptObservationRow)
                .where(AttemptObservationRow.execution_intent_id == intent.intent_id)
                .order_by(
                    AttemptObservationRow.observation_sequence,
                    AttemptObservationRow.observation_id,
                )
                .with_for_update()
            ).all()
            _verify_permit_fence(permit, intent)
            if not isinstance(source, AttemptObservationSource):
                raise ExecutionBlocked("ATTEMPT_OBSERVATION_SOURCE_INVALID")
            trusted_now = self._trusted_now(session)
            if source == AttemptObservationSource.DISPATCH_OUTCOME:
                if permit.state != "DISPATCHING":
                    raise ExecutionBlocked("BROKER_PERMIT_NOT_DISPATCHING")
                if dispatch_nonce is None or permit.dispatch_nonce != dispatch_nonce:
                    raise ExecutionBlocked("BROKER_DISPATCH_NONCE_MISMATCH")
            elif source == AttemptObservationSource.TARGETED_LOOKUP:
                if dispatch_nonce is not None or permit.state not in {
                    "CONSUMED",
                    "LOOKUP_ONLY",
                }:
                    raise ExecutionBlocked("TARGETED_LOOKUP_NOT_AUTHORIZED")
                if (
                    permit.mutation_kind == ReconciliationPurpose.CANCEL.value
                    and permit.state == "LOOKUP_ONLY"
                ):
                    if permit.dispatch_acquired_at is None:
                        raise ExecutionBlocked("BROKER_DISPATCH_TIME_MISSING")
                    not_before = max(
                        _utc(permit.expires_at),
                        _utc(permit.dispatch_acquired_at) + self._network_call_horizon,
                    )
                    if trusted_now < not_before:
                        raise ExecutionBlocked("CANCEL_LOOKUP_HORIZON_ACTIVE")
            else:
                raise ExecutionBlocked("ATTEMPT_OBSERVATION_SOURCE_INVALID")
            if observed.intent_id != intent.intent_id or observed.ordinal != permit.attempt_ordinal:
                raise ExecutionBlocked("ATTEMPT_OBSERVATION_LINEAGE_MISMATCH")
            row = next(
                (item for item in attempts if item.attempt_ordinal == observed.ordinal), None
            )
            if row is None:
                raise ExecutionBlocked("ATTEMPT_NOT_FOUND")
            existing = _attempt_from_row(row, attempts)
            if (
                observed.client_order_id != existing.client_order_id
                or observed.request_hash != existing.request_hash
                or observed.replaces_client_order_id != existing.replaces_client_order_id
                or observed.limit_price != existing.limit_price
                or observed.quote_hash != existing.quote_hash
                or observed.quote_source_timestamps != existing.quote_source_timestamps
                or observed.quote_retrieved_at != existing.quote_retrieved_at
                or observed.timing_authority_at != existing.timing_authority_at
                or observed.prior_request_hash != existing.prior_request_hash
            ):
                raise ExecutionBlocked("ATTEMPT_IMMUTABLE_FIELDS_MISMATCH")
            validate_attempt_observation(existing, observed)
            sequence = len(observation_rows) + 1
            payload = _attempt_to_json(observed)
            observation_hash = _authority_hash(
                {
                    "domain": "alphadecay.attempt-observation.v1",
                    "permit_id": str(permit.permit_id),
                    "intent_id": str(intent.intent_id),
                    "attempt_ordinal": observed.ordinal,
                    "sequence": sequence,
                    "source": source.value,
                    "observed_attempt": payload,
                    "observed_at": trusted_now.isoformat(),
                }
            )
            observation = AttemptObservation(
                observation_id=uuid5(
                    NAMESPACE_URL, f"alphadecay:attempt-observation:{observation_hash}"
                ),
                permit_id=permit.permit_id,
                intent_id=intent.intent_id,
                attempt_ordinal=observed.ordinal,
                sequence=sequence,
                source=source,
                observed_attempt=observed,
                observed_at=trusted_now,
                observation_hash=observation_hash,
            )
            session.add(
                AttemptObservationRow(
                    observation_id=observation.observation_id,
                    permit_id=permit.permit_id,
                    execution_intent_id=intent.intent_id,
                    attempt_id=row.attempt_id,
                    attempt_ordinal=observed.ordinal,
                    observation_sequence=sequence,
                    source=source.value,
                    provider_present=True,
                    observed_payload=payload,
                    observed_at=trusted_now,
                    observation_hash=observation_hash,
                )
            )
            row.state = observed.state
            row.provider_order_id = observed.provider_order_id
            row.filled_quantity = observed.filled_quantity
            row.quantity = observed.quantity
            row.fill_cash_flow = observed.fill_cash_flow
            lookup_pending = (
                permit.state == "LOOKUP_ONLY"
                and permit.mutation_kind
                in {
                    ReconciliationPurpose.SUBMIT.value,
                    ReconciliationPurpose.REPLACE.value,
                }
                and observed.state in PENDING_BROKER_ORDER_STATES
                and not (
                    observed.state == "CALCULATED" and observed.filled_quantity == observed.quantity
                )
            )
            if permit.state == "DISPATCHING" or (
                permit.state == "LOOKUP_ONLY" and not lookup_pending
            ):
                permit.state = "CONSUMED"
                permit.consumed_at = trusted_now
                permit.outcome_hash = observation_hash
            session.flush()
            return observation

    def get_attempt_observations(self, intent_id: UUID) -> tuple[AttemptObservation, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(AttemptObservationRow)
                .where(AttemptObservationRow.execution_intent_id == intent_id)
                .order_by(
                    AttemptObservationRow.observation_sequence,
                    AttemptObservationRow.observation_id,
                )
            ).all()
            return tuple(_attempt_observation_from_row(row) for row in rows)

    def record_attempt_lookup_failure(
        self, permit_id: UUID, *, claim: ExecutionIntent
    ) -> AttemptObservation:
        with self._sessions.begin() as session:
            permit = session.get(BrokerMutationPermitRow, permit_id, with_for_update=True)
            if permit is None:
                raise KeyError(permit_id)
            intent = session.get(
                ExecutionIntentRow, permit.execution_intent_id, with_for_update=True
            )
            if intent is None:
                raise ExecutionBlocked("BROKER_PERMIT_INTENT_MISSING")
            account = session.get(AccountRoleRow, intent.account_role, with_for_update=True)
            if account is None:
                raise ExecutionBlocked("ACCOUNT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            _verify_permit_fence(permit, intent)
            if permit.state not in {"CONSUMED", "LOOKUP_ONLY"}:
                raise ExecutionBlocked("TARGETED_LOOKUP_NOT_AUTHORIZED")
            target = session.scalar(
                select(OrderAttemptRow)
                .where(
                    OrderAttemptRow.execution_intent_id == intent.intent_id,
                    OrderAttemptRow.attempt_ordinal == permit.attempt_ordinal,
                )
                .with_for_update()
            )
            if target is None:
                raise ExecutionBlocked("ATTEMPT_NOT_FOUND")
            count = session.scalar(
                select(func.count(AttemptObservationRow.observation_id)).where(
                    AttemptObservationRow.execution_intent_id == intent.intent_id
                )
            )
            trusted_now = self._trusted_now(session)
            sequence = int(count or 0) + 1
            material = {
                "domain": "alphadecay.attempt-observation.v1",
                "permit_id": str(permit.permit_id),
                "intent_id": str(intent.intent_id),
                "attempt_ordinal": permit.attempt_ordinal,
                "sequence": sequence,
                "source": AttemptObservationSource.TARGETED_LOOKUP_FAILURE.value,
                "observed_attempt": None,
                "observed_at": trusted_now.isoformat(),
            }
            observation_hash = _authority_hash(material)
            observation = AttemptObservation(
                observation_id=uuid5(
                    NAMESPACE_URL, f"alphadecay:attempt-observation:{observation_hash}"
                ),
                permit_id=permit.permit_id,
                intent_id=intent.intent_id,
                attempt_ordinal=permit.attempt_ordinal,
                sequence=sequence,
                source=AttemptObservationSource.TARGETED_LOOKUP_FAILURE,
                observed_attempt=None,
                observed_at=trusted_now,
                observation_hash=observation_hash,
            )
            session.add(
                AttemptObservationRow(
                    observation_id=observation.observation_id,
                    permit_id=permit.permit_id,
                    execution_intent_id=intent.intent_id,
                    attempt_id=target.attempt_id,
                    attempt_ordinal=permit.attempt_ordinal,
                    observation_sequence=sequence,
                    source=observation.source.value,
                    provider_present=False,
                    observed_payload=None,
                    observed_at=trusted_now,
                    observation_hash=observation_hash,
                )
            )
            session.flush()
            return observation

    def record_attempt_absence(
        self,
        permit_id: UUID,
        *,
        source: AttemptObservationSource,
        claim: ExecutionIntent,
    ) -> AttemptObservation:
        if source != AttemptObservationSource.TARGETED_LOOKUP:
            raise ExecutionBlocked("ATTEMPT_ABSENCE_SOURCE_INVALID")
        with self._sessions.begin() as session:
            identity = session.get(BrokerMutationPermitRow, permit_id)
            if identity is None:
                raise KeyError(permit_id)
            intent = session.get(
                ExecutionIntentRow, identity.execution_intent_id, with_for_update=True
            )
            if intent is None:
                raise ExecutionBlocked("BROKER_PERMIT_INTENT_MISSING")
            account = session.get(AccountRoleRow, intent.account_role, with_for_update=True)
            if account is None:
                raise ExecutionBlocked("ACCOUNT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            attempts = session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == intent.intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
                .with_for_update()
            ).all()
            permit = session.get(BrokerMutationPermitRow, permit_id, with_for_update=True)
            if permit is None:
                raise KeyError(permit_id)
            observation_rows = session.scalars(
                select(AttemptObservationRow)
                .where(AttemptObservationRow.execution_intent_id == intent.intent_id)
                .order_by(
                    AttemptObservationRow.observation_sequence,
                    AttemptObservationRow.observation_id,
                )
                .with_for_update()
            ).all()
            _verify_permit_fence(permit, intent)
            if permit.state not in {"CONSUMED", "LOOKUP_ONLY"}:
                raise ExecutionBlocked("TARGETED_LOOKUP_NOT_AUTHORIZED")
            target = next(
                (row for row in attempts if row.attempt_ordinal == permit.attempt_ordinal),
                None,
            )
            if target is None:
                raise ExecutionBlocked("ATTEMPT_NOT_FOUND")
            trusted_now = self._trusted_now(session)
            if permit.mutation_kind == ReconciliationPurpose.CANCEL.value:
                if permit.dispatch_acquired_at is None:
                    raise ExecutionBlocked("BROKER_DISPATCH_TIME_MISSING")
                not_before = max(
                    _utc(permit.expires_at),
                    _utc(permit.dispatch_acquired_at) + self._network_call_horizon,
                )
                if trusted_now < not_before:
                    raise ExecutionBlocked("CANCEL_LOOKUP_HORIZON_ACTIVE")
            sequence = len(observation_rows) + 1
            material = {
                "domain": "alphadecay.attempt-observation.v1",
                "permit_id": str(permit.permit_id),
                "intent_id": str(intent.intent_id),
                "attempt_ordinal": permit.attempt_ordinal,
                "sequence": sequence,
                "source": source.value,
                "observed_attempt": None,
                "observed_at": trusted_now.isoformat(),
            }
            observation_hash = _authority_hash(material)
            observation = AttemptObservation(
                observation_id=uuid5(
                    NAMESPACE_URL, f"alphadecay:attempt-observation:{observation_hash}"
                ),
                permit_id=permit.permit_id,
                intent_id=intent.intent_id,
                attempt_ordinal=permit.attempt_ordinal,
                sequence=sequence,
                source=source,
                observed_attempt=None,
                observed_at=trusted_now,
                observation_hash=observation_hash,
            )
            session.add(
                AttemptObservationRow(
                    observation_id=observation.observation_id,
                    permit_id=permit.permit_id,
                    execution_intent_id=intent.intent_id,
                    attempt_id=target.attempt_id,
                    attempt_ordinal=permit.attempt_ordinal,
                    observation_sequence=sequence,
                    source=source.value,
                    provider_present=False,
                    observed_payload=None,
                    observed_at=trusted_now,
                    observation_hash=observation_hash,
                )
            )
            if permit.mutation_kind not in {
                ReconciliationPurpose.SUBMIT.value,
                ReconciliationPurpose.REPLACE.value,
            }:
                _latch_account(account, "RECONCILIATION_MISMATCH", trusted_now)
            session.flush()
            return observation

    @staticmethod
    def _verify_claim_fence(
        intent: ExecutionIntentRow,
        account: AccountRoleRow,
        claim: ExecutionIntent,
    ) -> None:
        if (
            intent.state != IntentState.CLAIMED.value
            or intent.intent_digest != claim.digest
            or intent.claim_token is None
            or intent.claim_token != claim.claim_token
            or intent.claim_generation != claim.claim_generation
            or intent.execution_epoch != claim.execution_epoch
            or account.claim_generation != claim.claim_generation
            or account.execution_epoch != claim.execution_epoch
        ):
            raise ExecutionBlocked("CLAIM_FENCE_MISMATCH")

    def add_entry_approval(self, approval: EntryApprovalAuthorization) -> None:
        _ensure_executable_role(approval.account_role)
        if approval.experiment_lineage is not None:
            raise ExecutionBlocked("EXPERIMENT_EXECUTION_LINEAGE_REQUIRES_AGENT_DECISION")
        try:
            with self._sessions.begin() as session:
                session.add(
                    EntryApprovalCertificateRow(
                        approval_id=approval.approval_id,
                        thesis_version_id=approval.thesis_version_id,
                        account_role=approval.account_role.value,
                        policy_hash=approval.policy_hash,
                        book_fingerprint=approval.book_fingerprint,
                        envelope_hash=approval.envelope_hash,
                        approved_max_loss=approval.approved_max_loss,
                        quantity=approval.quantity,
                        valid_from=approval.valid_from,
                        expires_at=approval.expires_at,
                        valid=approval.valid,
                    )
                )
        except IntegrityError as error:
            raise ExecutionBlocked("AUTHORIZATION_IMMUTABLE") from error

    def add_thesis_version(self, thesis: FrozenThesisVersion) -> None:
        _ensure_executable_role(thesis.account_role)
        try:
            with self._sessions.begin() as session:
                session.add(
                    ThesisVersionRow(
                        thesis_version_id=thesis.thesis_version_id,
                        thesis_id=thesis.thesis_id,
                        account_role=thesis.account_role.value,
                        version=thesis.version,
                        origin_hash=thesis.origin_hash or thesis.thesis_hash,
                        thesis_hash=thesis.thesis_hash,
                        policy_hash=thesis.policy_hash,
                        underlying=thesis.underlying,
                        thesis_code=thesis.thesis_code,
                        frozen_at=thesis.frozen_at,
                        target_at=thesis.target_at,
                        intended_exposure=thesis.intended_exposure,
                        exposure_limits=thesis.exposure_limits,
                        volatility_view=thesis.volatility_view,
                        entry_atm_iv=thesis.entry_atm_iv,
                        approved_max_loss=thesis.approved_max_loss,
                        portfolio_risk_cap=thesis.portfolio_risk_cap,
                        invalidation_codes=list(thesis.invalidation_codes),
                        thesis_payload=thesis.thesis_payload,
                        created_at=thesis.created_at,
                    )
                )
        except IntegrityError as error:
            raise ExecutionBlocked("THESIS_VERSION_IMMUTABLE") from error

    def get_thesis_version(self, thesis_version_id: UUID) -> FrozenThesisVersion:
        with self._sessions() as session:
            row = session.get(ThesisVersionRow, thesis_version_id)
            if row is None:
                raise KeyError(thesis_version_id)
            return _thesis_from_row(row)

    def add_assessment_certificate(self, certificate: AssessmentCertificate) -> None:
        _ensure_executable_role(certificate.account_role)
        if certificate.experiment_lineage is not None:
            raise ExecutionBlocked("EXPERIMENT_EXECUTION_LINEAGE_REQUIRES_AGENT_DECISION")
        exposure = _exposure_to_json(certificate.expected_after_exposure)
        try:
            with self._sessions.begin() as session:
                session.add(
                    AssessmentCertificateRow(
                        certificate_id=certificate.certificate_id,
                        thesis_version_id=certificate.thesis_version_id,
                        assessment_id=certificate.assessment_id,
                        account_role=certificate.account_role.value,
                        action=certificate.action.value,
                        position_fingerprint=certificate.position_fingerprint,
                        envelope_hash=certificate.envelope_hash,
                        approved_max_loss=certificate.approved_max_loss,
                        quantity=certificate.quantity,
                        expected_after_exposure=exposure,
                        policy_hash=certificate.policy_hash,
                        created_at=certificate.created_at,
                        expires_at=certificate.expires_at,
                        valid=certificate.valid,
                    )
                )
        except IntegrityError as error:
            raise ExecutionBlocked("CERTIFICATE_IMMUTABLE") from error

    def get_assessment_certificate(self, certificate_id: UUID) -> AssessmentCertificate:
        with self._sessions() as session:
            row = session.get(AssessmentCertificateRow, certificate_id)
            if row is None:
                raise KeyError(certificate_id)
            return _assessment_from_row(row)

    def approve_intent(
        self, intent_id: UUID, role: AccountRole, envelope: OrderEnvelope
    ) -> ExecutionIntent:
        _ensure_executable_role(role)
        digest = intent_digest(envelope)
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(ExecutionIntentRow).where(ExecutionIntentRow.intent_digest == digest)
                )
                if existing is not None:
                    intent = self._intent_from_row(session, existing)
                    if intent.account_role != role or intent.envelope != envelope:
                        raise ExecutionBlocked("INTENT_DIGEST_COLLISION")
                    return intent
                if session.get(ExecutionIntentRow, intent_id) is not None:
                    raise ExecutionBlocked("INTENT_ID_ALREADY_USED")
                account = session.get(AccountRoleRow, role.value)
                if account is None or account.account_fingerprint != envelope.account_fingerprint:
                    raise ExecutionBlocked("ACCOUNT_FINGERPRINT_MISMATCH")
                entry_approval_id = (
                    envelope.authorization_certificate_id
                    if envelope.action == ExecutionAction.ENTRY
                    else None
                )
                assessment_certificate_id = (
                    envelope.authorization_certificate_id
                    if envelope.action != ExecutionAction.ENTRY
                    else None
                )
                row = ExecutionIntentRow(
                    intent_id=intent_id,
                    account_role=role.value,
                    intent_digest=digest,
                    action=envelope.action.value,
                    policy_hash=envelope.policy_hash,
                    event_key=envelope.event_key,
                    trading_day=envelope.trading_day,
                    entry_approval_id=entry_approval_id,
                    assessment_certificate_id=assessment_certificate_id,
                    fingerprint=envelope.position_or_book_fingerprint,
                    envelope_hash=order_envelope_hash(envelope),
                    envelope_payload=_envelope_to_json(envelope),
                    legs=_legs_to_json(envelope.legs),
                    quantity=envelope.quantity,
                    minimum_limit=envelope.minimum_limit,
                    maximum_limit=envelope.maximum_limit,
                    approved_max_loss=envelope.approved_max_loss,
                    market_session_id=envelope.market_session_id,
                    quoted_relative_spread=envelope.quoted_relative_spread,
                    maximum_relative_spread=envelope.maximum_relative_spread,
                    incremental_debit=envelope.incremental_debit,
                    maximum_incremental_debit=envelope.maximum_incremental_debit,
                    state=IntentState.APPROVED.value,
                    claimed_by=None,
                    claimed_at=None,
                    first_fill_consumed=False,
                )
                session.add(row)
                session.flush()
                return self._intent_from_row(session, row)
        except IntegrityError as error:
            with self._sessions() as session:
                existing = session.scalar(
                    select(ExecutionIntentRow).where(ExecutionIntentRow.intent_digest == digest)
                )
                if existing is not None:
                    intent = self._intent_from_row(session, existing)
                    if intent.account_role == role and intent.envelope == envelope:
                        return intent
            raise ExecutionBlocked("INTENT_UNIQUENESS_CONFLICT") from error

    def claim_intent(
        self,
        intent_id: UUID,
        actor: Actor,
        *,
        now: datetime,
        account_role: AccountRole,
        account_fingerprint: str,
    ) -> ExecutionIntent:
        del now
        with self._sessions.begin() as session:
            row = session.scalar(
                select(ExecutionIntentRow)
                .where(ExecutionIntentRow.intent_id == intent_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(intent_id)
            if row.account_role != account_role.value:
                raise ExecutionBlocked("ACCOUNT_ROLE_MISMATCH")
            account = session.scalar(
                select(AccountRoleRow)
                .where(AccountRoleRow.role == account_role.value)
                .with_for_update()
            )
            if account is None or account.account_fingerprint != account_fingerprint:
                raise ExecutionBlocked("ACCOUNT_FINGERPRINT_MISMATCH")
            budget = session.scalar(
                select(CompetitionEntryBudgetRow)
                .where(CompetitionEntryBudgetRow.account_role == row.account_role)
                .with_for_update()
            )
            if budget is None:
                raise ExecutionBlocked("ACCOUNT_NOT_REGISTERED")
            if account.execution_locked:
                raise ExecutionBlocked("ACCOUNT_EXECUTION_LOCKED")
            if row.state != IntentState.APPROVED.value:
                raise ExecutionBlocked("INTENT_ALREADY_CLAIMED")
            active_lease = session.scalar(
                select(ExecutionIntentRow).where(
                    ExecutionIntentRow.account_role == row.account_role,
                    ExecutionIntentRow.state == IntentState.CLAIMED.value,
                )
            )
            if active_lease is not None:
                raise ExecutionBlocked("ACCOUNT_EXECUTION_LEASE_ACTIVE")
            intent = self._intent_from_row(session, row)
            trusted_now = self._trusted_now(session)
            if intent.envelope.action == ExecutionAction.ENTRY:
                if actor == Actor.OWNER:
                    raise ExecutionBlocked("OWNER_ENTRY_FORBIDDEN")
                if not account.autonomous_enabled:
                    raise ExecutionBlocked("AUTONOMOUS_DISABLED")
            elif actor == Actor.SCHEDULER and not account.autonomous_enabled:
                raise ExecutionBlocked("AUTONOMOUS_DISABLED")
            self._validate_origin(session, intent, trusted_now)
            if intent.envelope.action == ExecutionAction.ENTRY:
                self._reserve_entry(session, intent, account, budget)
            row.state = IntentState.CLAIMED.value
            row.claimed_by = actor.value
            row.claimed_at = trusted_now
            account.claim_generation += 1
            row.claim_token = uuid4()
            row.claim_generation = account.claim_generation
            row.execution_epoch = account.execution_epoch
            row.heartbeat_at = trusted_now
            row.lease_expires_at = trusted_now + timedelta(seconds=30)
            session.flush()
            return self._intent_from_row(session, row)

    def _validate_origin(self, session: Session, intent: ExecutionIntent, now: datetime) -> None:
        envelope = intent.envelope
        authorization_id = envelope.authorization_certificate_id
        if envelope.action == ExecutionAction.ENTRY:
            row = session.scalar(
                select(EntryApprovalCertificateRow)
                .where(EntryApprovalCertificateRow.approval_id == authorization_id)
                .with_for_update()
            )
            if row is None:
                raise ExecutionBlocked("AUTHORIZATION_ORIGIN_NOT_FOUND")
            authorization = AuthorizationValues(
                AccountRole(row.account_role),
                row.policy_hash,
                row.book_fingerprint,
                row.envelope_hash,
                row.approved_max_loss,
                row.quantity,
                row.valid,
                _utc(row.valid_from),
                _utc(row.expires_at),
            )
        else:
            row = session.scalar(
                select(AssessmentCertificateRow)
                .where(AssessmentCertificateRow.certificate_id == authorization_id)
                .with_for_update()
            )
            if row is None:
                raise ExecutionBlocked("AUTHORIZATION_ORIGIN_NOT_FOUND")
            if row.action != envelope.action.value:
                raise ExecutionBlocked("AUTHORIZATION_ACTION_MISMATCH")
            authorization = AuthorizationValues(
                AccountRole(row.account_role),
                row.policy_hash,
                row.position_fingerprint,
                row.envelope_hash,
                row.approved_max_loss,
                row.quantity,
                row.valid,
                _utc(row.created_at),
                _utc(row.expires_at),
            )
        decision = (
            session.get(AgentDecisionRow, row.agent_decision_id)
            if row.agent_decision_id is not None
            else None
        )
        snapshot = (
            session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
            if decision is not None
            else None
        )
        thesis = session.get(ThesisVersionRow, row.thesis_version_id)
        tick = session.get(AgentTickRow, decision.origin_tick_id) if decision is not None else None
        expected_input_hash = (
            canonical_agent_hash(
                agent_input_material(
                    account_role=snapshot.account_role,
                    account_fingerprint=snapshot.account_fingerprint,
                    decision_kind=snapshot.decision_kind,
                    decision_boundary=snapshot.decision_boundary,
                    observed_at=snapshot.observed_at,
                    normalized_input=snapshot.normalized_payload,
                    thesis_version_id=snapshot.thesis_version_id,
                )
            )
            if snapshot is not None and snapshot.thesis_version_id is not None
            else None
        )
        expected_result_hash = (
            canonical_agent_hash(
                agent_result_material(
                    input_hash=snapshot.input_hash,
                    outcome=decision.outcome,
                    reason_code=decision.reason_code,
                    policy_hash=decision.policy_hash,
                    thesis_version_id=decision.thesis_version_id,
                    result_payload=decision.result_payload,
                    authorization_id=authorization_id,
                    intent_id=intent.intent_id,
                    intent_digest=intent.digest,
                    autonomy_authorized=True,
                    experiment_lineage=optional_experiment_execution_lineage(
                        decision.experiment_id,
                        decision.experiment_source_definition_hash,
                        decision.experiment_protocol_hash,
                    ),
                )
            )
            if decision is not None
            and snapshot is not None
            and decision.thesis_version_id is not None
            else None
        )
        if (
            row.agent_decision_id is None
            or decision is None
            or snapshot is None
            or thesis is None
            or tick is None
            or not decision.autonomy_authorized
            or decision.thesis_version_id != row.thesis_version_id
            or snapshot.thesis_version_id != row.thesis_version_id
            or decision.account_role != intent.account_role.value
            or snapshot.account_role != intent.account_role.value
            or thesis.account_role != intent.account_role.value
            or decision.account_fingerprint != envelope.account_fingerprint
            or snapshot.account_fingerprint != envelope.account_fingerprint
            or decision.policy_hash != envelope.policy_hash
            or thesis.policy_hash != envelope.policy_hash
            or decision.input_snapshot_id != snapshot.snapshot_id
            or tick.decision_id != decision.decision_id
            or tick.account_role != intent.account_role.value
            or tick.account_fingerprint != envelope.account_fingerprint
            or snapshot.input_hash != expected_input_hash
            or decision.result_hash != expected_result_hash
            or (
                envelope.action is ExecutionAction.ENTRY
                and (
                    decision.decision_kind != "OPPORTUNITY" or decision.outcome != "ENTRY_APPROVED"
                )
            )
            or (
                envelope.action is not ExecutionAction.ENTRY
                and (
                    decision.decision_kind != "ASSESSMENT"
                    or decision.outcome
                    not in (
                        {"ROLL_APPROVED"}
                        if envelope.action is ExecutionAction.ROLL
                        else {"CLOSE_APPROVED", "CLOSE_RISK_ONLY"}
                    )
                )
            )
        ):
            raise ExecutionBlocked("AUTHORIZATION_DECISION_ORIGIN_MISMATCH")
        if envelope.action is not ExecutionAction.ENTRY:
            managed_position = session.scalar(
                select(ManagedLifecyclePositionRow)
                .where(
                    ManagedLifecyclePositionRow.account_role == intent.account_role.value,
                    ManagedLifecyclePositionRow.closed_at.is_(None),
                )
                .with_for_update()
            )
            current_snapshot = (
                session.get(ManagedPositionSnapshotRow, managed_position.current_snapshot_id)
                if managed_position is not None and managed_position.current_snapshot_id is not None
                else None
            )
            if (
                managed_position is None
                or current_snapshot is None
                or managed_position.account_fingerprint != envelope.account_fingerprint
                or managed_position.thesis_version_id != row.thesis_version_id
                or managed_position.active_position_fingerprint
                != envelope.position_or_book_fingerprint
                or current_snapshot.managed_position_id != managed_position.managed_position_id
                or current_snapshot.position_fingerprint
                != managed_position.active_position_fingerprint
            ):
                raise ExecutionBlocked("LIFECYCLE_POSITION_ORIGIN_MISMATCH")
        validate_authorization(intent, authorization, now)

    def _reserve_entry(
        self,
        session: Session,
        intent: ExecutionIntent,
        account: AccountRoleRow,
        budget: CompetitionEntryBudgetRow,
    ) -> None:
        if intent.account_role == AccountRole.SUBMISSION:
            baseline = session.scalar(
                select(SubmissionBaselineRow)
                .where(SubmissionBaselineRow.account_role == intent.account_role.value)
                .with_for_update()
            )
            if baseline is None:
                raise ExecutionBlocked("SUBMISSION_BASELINE_REQUIRED")
            if baseline.contaminated:
                raise ExecutionBlocked("SUBMISSION_BASELINE_CONTAMINATED")
        if budget.reserved_intent_id is not None:
            raise ExecutionBlocked("ENTRY_RESERVATION_ACTIVE")
        _validate_entry_limits(
            self._entry_limits,
            intent=intent,
            account=account,
            budget=budget,
        )
        budget.reserved_intent_id = intent.intent_id
        budget.reserved_risk = intent.envelope.approved_max_loss

    def release_unsubmitted_claim(self, intent_id: UUID) -> None:
        with self._sessions.begin() as session:
            row = session.get(ExecutionIntentRow, intent_id, with_for_update=True)
            if row is None:
                raise KeyError(intent_id)
            attempts = session.scalar(
                select(func.count())
                .select_from(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == intent_id)
            )
            if row.state != IntentState.CLAIMED.value or attempts:
                raise ExecutionBlocked("CLAIM_NOT_UNSUBMITTED")
            budget = session.get(CompetitionEntryBudgetRow, row.account_role, with_for_update=True)
            if budget is not None and budget.reserved_intent_id == intent_id:
                budget.reserved_intent_id = None
                budget.reserved_risk = Decimal(0)
            row.state = IntentState.TERMINAL.value

    def execution_preflight_intent(
        self, role: AccountRole, intent_id: UUID | None = None
    ) -> ExecutionIntent:
        _ensure_executable_role(role)
        with self._sessions() as session:
            query = select(ExecutionIntentRow).where(ExecutionIntentRow.account_role == role.value)
            if intent_id is not None:
                query = query.where(ExecutionIntentRow.intent_id == intent_id)
            else:
                query = query.where(
                    ExecutionIntentRow.state != IntentState.TERMINAL.value
                ).order_by(
                    ExecutionIntentRow.trading_day.desc(),
                    ExecutionIntentRow.claimed_at.desc().nullslast(),
                    ExecutionIntentRow.intent_id.desc(),
                )
            row = session.scalar(query.limit(1))
            if row is None:
                raise ExecutionBlocked("EXECUTION_INTENT_NOT_FOUND")
            intent = self._intent_from_row(session, row)
            if row.state == IntentState.CLAIMED.value:
                trusted_now = self._trusted_now(session)
                if row.lease_expires_at is None or _utc(row.lease_expires_at) <= trusted_now:
                    raise ExecutionBlocked("INTENT_CLAIM_EXPIRED")
            return intent

    def expired_unsubmitted_claims(self, role: AccountRole, *, persist: bool) -> tuple[UUID, ...]:
        _ensure_executable_role(role)
        context = self._sessions.begin() if persist else self._sessions()
        with context as session:
            trusted_now = self._trusted_now(session)
            query = (
                select(ExecutionIntentRow)
                .where(
                    ExecutionIntentRow.account_role == role.value,
                    ExecutionIntentRow.state == IntentState.CLAIMED.value,
                    ExecutionIntentRow.lease_expires_at <= trusted_now,
                    ~select(OrderAttemptRow.attempt_id)
                    .where(OrderAttemptRow.execution_intent_id == ExecutionIntentRow.intent_id)
                    .exists(),
                )
                .order_by(ExecutionIntentRow.intent_id)
            )
            if persist:
                query = query.with_for_update()
            rows = session.scalars(query).all()
            if not persist:
                return tuple(row.intent_id for row in rows)
            budget = session.get(CompetitionEntryBudgetRow, role.value, with_for_update=True)
            for row in rows:
                if budget is not None and budget.reserved_intent_id == row.intent_id:
                    budget.reserved_intent_id = None
                    budget.reserved_risk = Decimal(0)
                row.state = IntentState.TERMINAL.value
            session.flush()
            return tuple(row.intent_id for row in rows)

    def add_attempt(self, attempt: OrderAttempt) -> None:
        del attempt
        raise ExecutionBlocked("BROKER_PERMIT_REQUIRED")

    def replace_attempt(self, attempt: OrderAttempt) -> None:
        del attempt
        raise ExecutionBlocked("BROKER_PERMIT_REQUIRED")

    def attempts_for(self, intent_id: UUID) -> tuple[OrderAttempt, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal)
            ).all()
            return _attempts_from_rows(rows)

    def execution_attempts_for(self, intent_id: UUID) -> tuple[OrderAttempt, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
            ).all()
            permits = session.scalars(
                select(BrokerMutationPermitRow)
                .where(BrokerMutationPermitRow.execution_intent_id == intent_id)
                .order_by(
                    BrokerMutationPermitRow.attempt_ordinal,
                    BrokerMutationPermitRow.permit_generation,
                    BrokerMutationPermitRow.permit_id,
                )
            ).all()
            return _attempts_from_rows(_execution_attempt_rows(rows, permits))

    def targeted_lookup_authority(self, claim: ExecutionIntent) -> tuple[UUID, OrderAttempt] | None:
        with self._sessions.begin() as session:
            intent = session.get(ExecutionIntentRow, claim.intent_id, with_for_update=True)
            account = session.get(AccountRoleRow, claim.account_role.value, with_for_update=True)
            if intent is None or account is None:
                raise ExecutionBlocked("ACCOUNT_OR_INTENT_NOT_REGISTERED")
            self._verify_claim_fence(intent, account, claim)
            attempts = session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == claim.intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
                .with_for_update()
            ).all()
            permits = session.scalars(
                select(BrokerMutationPermitRow)
                .where(BrokerMutationPermitRow.execution_intent_id == claim.intent_id)
                .order_by(
                    BrokerMutationPermitRow.attempt_ordinal,
                    BrokerMutationPermitRow.permit_generation,
                    BrokerMutationPermitRow.permit_id,
                )
                .with_for_update()
            ).all()
            execution_attempts = _execution_attempt_rows(attempts, permits)
            if not execution_attempts:
                raise ExecutionBlocked("TRANSITIONAL_ATTEMPT_MISSING")
            current_row = execution_attempts[-1]
            current = _attempt_from_row(current_row, execution_attempts)
            observations = session.scalars(
                select(AttemptObservationRow)
                .where(
                    AttemptObservationRow.execution_intent_id == claim.intent_id,
                    AttemptObservationRow.attempt_ordinal == current.ordinal,
                )
                .order_by(
                    AttemptObservationRow.observation_sequence,
                    AttemptObservationRow.observation_id,
                )
                .with_for_update()
            ).all()
            active_lookup_permits = [
                permit
                for permit in permits
                if permit.attempt_ordinal == current.ordinal and permit.state == "LOOKUP_ONLY"
            ]
            if active_lookup_permits:
                if len(active_lookup_permits) != 1:
                    raise ExecutionBlocked("TARGETED_LOOKUP_AUTHORITY_CONFLICT")
                permit = active_lookup_permits[0]
                if permit.dispatch_acquired_at is None:
                    raise ExecutionBlocked("BROKER_DISPATCH_TIME_MISSING")
                trusted_now = self._trusted_now(session)
                not_before = max(
                    _utc(permit.expires_at),
                    _utc(permit.dispatch_acquired_at) + self._network_call_horizon,
                )
                if trusted_now < not_before:
                    return None
                if observations:
                    policy = broker_lookup_policy(
                        current.state
                        if current.state in PENDING_BROKER_ORDER_STATES
                        else "PENDING_NEW"
                    )
                    latest_attempt_at = _utc(observations[-1].observed_at)
                    if trusted_now < latest_attempt_at + policy.cadence:
                        return None
                    provider_observations = [
                        item for item in observations if item.observed_payload is not None
                    ]
                    if provider_observations:
                        state_observations = []
                        for item in reversed(provider_observations):
                            observed = _attempt_from_json(item.observed_payload)
                            if observed.state != current.state:
                                break
                            state_observations.append(item)
                        state_started_at = _utc(state_observations[-1].observed_at)
                    else:
                        state_started_at = _utc(observations[0].observed_at)
                    if (
                        current.state in LOOKUP_ONLY_BROKER_ORDER_STATES
                        and trusted_now >= state_started_at + policy.deadline
                        and not account.execution_locked
                    ):
                        _latch_account(account, "BROKER_TRANSITION_STALLED", trusted_now)
                return permit.permit_id, current
            if current.state not in PENDING_BROKER_ORDER_STATES:
                return None
            if current.state in MUTATION_ELIGIBLE_BROKER_ORDER_STATES and any(
                permit.state in {"PREPARED", "DISPATCHING"} for permit in permits
            ):
                return None
            if current.state == "PENDING_CANCEL":
                cancel_permits = [
                    permit
                    for permit in permits
                    if permit.attempt_ordinal == current.ordinal
                    and permit.mutation_kind == ReconciliationPurpose.CANCEL.value
                    and permit.state == "CONSUMED"
                ]
                if not cancel_permits:
                    raise ExecutionBlocked("TARGETED_LOOKUP_NOT_AUTHORIZED")
                permit = cancel_permits[-1]
            else:
                permit = _creation_permit(current_row, permits)
            if permit.state != "CONSUMED":
                raise ExecutionBlocked("TARGETED_LOOKUP_NOT_AUTHORIZED")
            provider_observations = [
                item for item in observations if item.observed_payload is not None
            ]
            if not provider_observations:
                raise ExecutionBlocked("TARGETED_LOOKUP_OBSERVATION_MISSING")
            if _attempt_from_json(provider_observations[-1].observed_payload) != current:
                raise ExecutionBlocked("TARGETED_LOOKUP_OBSERVATION_STALE")
            policy = broker_lookup_policy(current.state)
            trusted_now = self._trusted_now(session)
            latest_attempt_at = _utc(observations[-1].observed_at)
            if trusted_now < latest_attempt_at + policy.cadence:
                return None
            state_observations = []
            for item in reversed(provider_observations):
                observed = _attempt_from_json(item.observed_payload)
                if observed.state != current.state:
                    break
                state_observations.append(item)
            state_started_at = _utc(state_observations[-1].observed_at)
            if (
                current.state in LOOKUP_ONLY_BROKER_ORDER_STATES
                and trusted_now >= state_started_at + policy.deadline
                and not account.execution_locked
            ):
                _latch_account(account, "BROKER_TRANSITION_STALLED", trusted_now)
            return permit.permit_id, current

    def finalize_execution(
        self,
        certificate: ExecutionCertificate,
        reconciliation: Reconciliation,
        requested_status: str,
    ) -> ExecutionCertificate:
        del certificate, reconciliation, requested_status
        raise ExecutionBlocked("WHOLE_ACCOUNT_RECONCILIATION_REQUIRED")

    def finalize_execution_authorized(
        self,
        certificate: ExecutionCertificate,
        reconciliation: WholeAccountReconciliation,
        requested_status: str,
        *,
        claim: ExecutionIntent,
        position_greeks: tuple[PositionGreekObservation, ...] = (),
    ) -> ExecutionCertificate | None:
        try:
            with self._sessions.begin() as session:
                intent = session.get(ExecutionIntentRow, claim.intent_id, with_for_update=True)
                if intent is None:
                    raise KeyError(claim.intent_id)
                account = session.get(AccountRoleRow, intent.account_role, with_for_update=True)
                budget = session.get(
                    CompetitionEntryBudgetRow, intent.account_role, with_for_update=True
                )
                if account is None or budget is None:
                    raise ExecutionBlocked("ACCOUNT_NOT_REGISTERED")
                self._verify_claim_fence(intent, account, claim)
                trusted_now = self._trusted_now(session)
                # Finalization records an order the broker already accepted. The authorization
                # must have been valid when the first broker write was dispatched, not when the
                # terminal state is reconciled, which can be well after a short approval window.
                first_dispatch = session.scalar(
                    select(BrokerMutationPermitRow.dispatch_acquired_at)
                    .where(
                        BrokerMutationPermitRow.execution_intent_id == intent.intent_id,
                        BrokerMutationPermitRow.dispatch_acquired_at.is_not(None),
                    )
                    .order_by(BrokerMutationPermitRow.dispatch_acquired_at)
                    .limit(1)
                )
                origin_at = (
                    trusted_now
                    if first_dispatch is None
                    else min(trusted_now, _utc(first_dispatch))
                )
                self._validate_origin(session, self._intent_from_row(session, intent), origin_at)
                baseline = session.scalar(
                    select(SubmissionBaselineRow)
                    .where(SubmissionBaselineRow.account_role == intent.account_role)
                    .with_for_update()
                )
                if intent.account_role == AccountRole.SUBMISSION.value and (
                    baseline is None or baseline.contaminated
                ):
                    raise ExecutionBlocked("SUBMISSION_BASELINE_INVALID")
                attempt_rows = session.scalars(
                    select(OrderAttemptRow)
                    .where(OrderAttemptRow.execution_intent_id == intent.intent_id)
                    .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
                    .with_for_update()
                ).all()
                state = session.scalar(
                    select(AccountReconciliationStateRow)
                    .where(AccountReconciliationStateRow.account_role == intent.account_role)
                    .order_by(AccountReconciliationStateRow.sequence.desc())
                    .with_for_update()
                )
                if state is None:
                    raise ExecutionBlocked("RECONCILIATION_STATE_REQUIRED")
                session.scalars(
                    select(WholeAccountReconciliationRow)
                    .where(WholeAccountReconciliationRow.execution_intent_id == intent.intent_id)
                    .order_by(
                        WholeAccountReconciliationRow.accepted_at,
                        WholeAccountReconciliationRow.reconciliation_id,
                    )
                    .with_for_update()
                ).all()
                permit_rows = session.scalars(
                    select(BrokerMutationPermitRow)
                    .where(BrokerMutationPermitRow.execution_intent_id == intent.intent_id)
                    .order_by(
                        BrokerMutationPermitRow.attempt_ordinal,
                        BrokerMutationPermitRow.permit_generation,
                        BrokerMutationPermitRow.permit_id,
                    )
                    .with_for_update()
                ).all()
                observations = session.scalars(
                    select(AttemptObservationRow)
                    .where(AttemptObservationRow.execution_intent_id == intent.intent_id)
                    .order_by(
                        AttemptObservationRow.observation_sequence,
                        AttemptObservationRow.observation_id,
                    )
                    .with_for_update()
                ).all()
                latest_permit = next(
                    (
                        permit
                        for permit in permit_rows
                        if observations and permit.permit_id == observations[-1].permit_id
                    ),
                    None,
                )
                try:
                    expectation = _final_reconciliation_expectation_from_rows(
                        intent,
                        state,
                        _execution_attempt_rows(attempt_rows, permit_rows),
                        observations,
                        latest_permit,
                        reconciliation.sweep.activities,
                    )
                except ExecutionBlocked as error:
                    if str(error) not in {
                        "ATTEMPT_ACTIVITY_EVIDENCE_MISMATCH",
                        "KNOWN_ACTIVITY_MISSING",
                    }:
                        raise
                    _latch_account(account, "RECONCILIATION_MISMATCH", trusted_now)
                    session.flush()
                    return None
                evaluated = WholeAccountReconciliation.evaluate(
                    reconciliation.sweep,
                    expectation,
                    accepted_at=trusted_now,
                )
                session.add(_reconciliation_row(evaluated))
                session.flush()
                latest = observations[-1]
                if reconciliation.sweep.retrieval_started_at < _utc(latest.observed_at):
                    raise ExecutionBlocked("FINAL_RECONCILIATION_PRECEDES_OBSERVATION")
                if not evaluated.safe:
                    reason = (
                        "ASSIGNMENT_SUSPECTED"
                        if ReconciliationBlockCode.ASSIGNMENT_SUSPECTED in evaluated.block_codes
                        else "RECONCILIATION_MISMATCH"
                    )
                    _latch_account(account, reason, trusted_now)
                    session.flush()
                    return None
                supplied_provenance = (
                    certificate.reconciliation_id,
                    certificate.reconciliation_hash,
                    certificate.last_observation_hash,
                )
                expected_provenance = (
                    evaluated.reconciliation_id,
                    evaluated.reconciliation_hash,
                    latest.observation_hash,
                )
                if any(value is not None for value in supplied_provenance) and (
                    supplied_provenance != expected_provenance
                ):
                    raise ExecutionBlocked("CERTIFICATE_RECONCILIATION_MISMATCH")
                certificate = replace(
                    certificate,
                    reconciliation_id=evaluated.reconciliation_id,
                    reconciliation_hash=evaluated.reconciliation_hash,
                    last_observation_hash=latest.observation_hash,
                )
                attempts = _attempts_from_rows(_execution_attempt_rows(attempt_rows, permit_rows))
                try:
                    actual_exposure = reconcile_actual_exposure(
                        evaluated.sweep.final_positions,
                        position_greeks,
                        accepted_at=trusted_now,
                    )
                except ExecutionBlocked:
                    _latch_account(account, "RECONCILIATION_MISMATCH", trusted_now)
                    session.flush()
                    return None
                certificate = replace(certificate, actual_exposure=actual_exposure)
                derived = Reconciliation(
                    terminal=True,
                    remainder_absent=True,
                    matches_expected=True,
                    assignment_suspected=False,
                    actual_exposure=actual_exposure,
                )
                normalized, has_fill = validate_finalization(
                    self._intent_from_row(session, intent),
                    attempts,
                    certificate,
                    derived,
                    requested_status,
                    trusted_now,
                )
                if has_fill and requested_status.startswith("PARTIAL_"):
                    _latch_account(account, "UNMANAGED_PARTIAL_EXPOSURE", trusted_now)
                _apply_budget_finalization(
                    intent,
                    budget,
                    account,
                    has_fill,
                    self._entry_limits,
                )
                session.flush()
                next_state = AccountReconciliationState._from_repository_state(
                    account_role=AccountRole(intent.account_role),
                    account_fingerprint=state.account_fingerprint,
                    baseline_captured_at=_utc(state.baseline_captured_at),
                    accepted_at=trusted_now,
                    expected_cash=expectation.expected_cash,
                    expected_positions=expectation.expected_positions,
                    expected_open_orders=expectation.expected_open_orders,
                    known_activities=evaluated.sweep.activities,
                    activity_complete_through=(
                        evaluated.sweep.activity_pagination.visibility_complete_through
                    ),
                    resolved_activity_hashes=expectation.resolved_activity_hashes,
                )
                session.add(
                    AccountReconciliationStateRow(
                        state_id=next_state.state_id,
                        account_role=next_state.account_role.value,
                        sequence=state.sequence + 1,
                        account_fingerprint=next_state.account_fingerprint,
                        baseline_id=state.baseline_id,
                        baseline_captured_at=next_state.baseline_captured_at,
                        accepted_at=next_state.accepted_at,
                        expected_cash=next_state.expected_cash,
                        expected_positions=[
                            _inventory_to_json(item) for item in next_state.expected_positions
                        ],
                        expected_open_orders=[
                            _open_order_to_json(item) for item in next_state.expected_open_orders
                        ],
                        known_activities=[
                            _activity_to_json(item) for item in next_state.known_activities
                        ],
                        activity_complete_through=next_state.activity_complete_through,
                        resolved_activity_hashes=list(next_state.resolved_activity_hashes),
                        predecessor_state_id=state.state_id,
                        authority_reconciliation_id=evaluated.reconciliation_id,
                        authority_permit_id=latest_permit.permit_id,
                        authority_observation_id=latest.observation_id,
                        authority_permit_request_hash=latest_permit.request_hash,
                        transition_hash=None,
                        state_hash=next_state.state_hash,
                    )
                )
                session.add(_execution_certificate_row(normalized))
                intent.state = IntentState.TERMINAL.value
                session.flush()
                return normalized
        except IntegrityError as error:
            raise ExecutionBlocked("CERTIFICATE_IMMUTABLE") from error

    def get_execution_certificate(self, certificate_id: UUID) -> ExecutionCertificate:
        with self._sessions() as session:
            row = session.get(ExecutionCertificateRow, certificate_id)
            if row is None:
                raise KeyError(certificate_id)
            return ExecutionCertificate(
                certificate_id=row.certificate_id,
                intent_id=row.execution_intent_id,
                entry_approval_id=row.entry_approval_id,
                assessment_certificate_id=row.assessment_certificate_id,
                execution_status=row.execution_status,
                attempt_ids=tuple(row.attempt_ids),
                actual_exposure=_exposure_from_json(row.actual_exposure),
                reconciliation_checks=tuple(row.reconciliation_checks),
                created_at=_utc(row.created_at),
                reconciliation_id=row.reconciliation_id,
                reconciliation_hash=row.reconciliation_hash,
                last_observation_hash=row.last_observation_hash,
            )

    def recover_entry_reservations(self) -> None:
        with self._sessions.begin() as session:
            rows = session.scalars(
                select(ExecutionIntentRow)
                .where(
                    ExecutionIntentRow.action == ExecutionAction.ENTRY.value,
                    ExecutionIntentRow.state == IntentState.CLAIMED.value,
                    ExecutionIntentRow.first_fill_consumed.is_(False),
                )
                .with_for_update()
            ).all()
            for row in rows:
                budget = session.get(
                    CompetitionEntryBudgetRow, row.account_role, with_for_update=True
                )
                if budget is None:
                    raise ExecutionBlocked("ENTRY_BUDGET_MISSING")
                budget.reserved_intent_id = row.intent_id
                budget.reserved_risk = row.approved_max_loss

    def get_entry_budget(self, role: AccountRole) -> EntryBudget:
        _ensure_executable_role(role)
        with self._sessions() as session:
            row = session.get(CompetitionEntryBudgetRow, role.value)
            if row is None:
                raise KeyError(role)
            return EntryBudget(
                entries_used=row.entries_used,
                gross_approved_risk=row.gross_approved_risk,
                reserved_intent_id=row.reserved_intent_id,
                reserved_risk=row.reserved_risk,
            )

    def get_execution_lock(self, role: AccountRole) -> AccountExecutionLock:
        _ensure_executable_role(role)
        with self._sessions() as session:
            row = session.get(AccountRoleRow, role.value)
            if row is None:
                raise KeyError(role)
            return AccountExecutionLock(
                locked=row.execution_locked,
                reason=row.execution_lock_reason,
                locked_at=_utc(row.execution_locked_at) if row.execution_locked_at else None,
            )

    def get_intent(self, intent_id: UUID) -> ExecutionIntent:
        with self._sessions() as session:
            row = session.get(ExecutionIntentRow, intent_id)
            if row is None:
                raise KeyError(intent_id)
            return self._intent_from_row(session, row)

    @staticmethod
    def _intent_from_row(session: Session, row: ExecutionIntentRow) -> ExecutionIntent:
        account = session.get(AccountRoleRow, row.account_role)
        if account is None:
            raise ExecutionBlocked("ACCOUNT_NOT_REGISTERED")
        envelope = _envelope_from_json(row.envelope_payload)
        expected_authorization_id = row.entry_approval_id or row.assessment_certificate_id
        if (
            expected_authorization_id is None
            or envelope.authorization_certificate_id != expected_authorization_id
            or envelope.account_fingerprint != account.account_fingerprint
            or envelope.action.value != row.action
            or envelope.policy_hash != row.policy_hash
            or envelope.position_or_book_fingerprint != row.fingerprint
            or envelope.event_key != row.event_key
            or envelope.trading_day != row.trading_day
            or envelope.quantity != row.quantity
            or envelope.minimum_limit != row.minimum_limit
            or envelope.maximum_limit != row.maximum_limit
            or envelope.approved_max_loss != row.approved_max_loss
            or envelope.market_session_id != row.market_session_id
            or envelope.quoted_relative_spread != row.quoted_relative_spread
            or envelope.maximum_relative_spread != row.maximum_relative_spread
            or envelope.incremental_debit != row.incremental_debit
            or envelope.maximum_incremental_debit != row.maximum_incremental_debit
            or _legs_to_json(envelope.legs) != row.legs
            or order_envelope_hash(envelope) != row.envelope_hash
            or intent_digest(envelope) != row.intent_digest
        ):
            raise ExecutionBlocked("PERSISTED_INTENT_CORRUPT")
        return ExecutionIntent(
            intent_id=row.intent_id,
            account_role=AccountRole(row.account_role),
            envelope=envelope,
            digest=row.intent_digest,
            state=IntentState(row.state),
            claimed_by=Actor(row.claimed_by) if row.claimed_by else None,
            claimed_at=_utc(row.claimed_at) if row.claimed_at else None,
            first_fill_consumed=row.first_fill_consumed,
            claim_token=row.claim_token,
            claim_generation=row.claim_generation,
            execution_epoch=row.execution_epoch,
            heartbeat_at=_utc(row.heartbeat_at) if row.heartbeat_at else None,
            lease_expires_at=_utc(row.lease_expires_at) if row.lease_expires_at else None,
        )


def _latch_account(account: AccountRoleRow, reason: str, locked_at: datetime) -> None:
    account.execution_locked = True
    account.execution_lock_reason = reason
    account.execution_locked_at = locked_at
    account.execution_lock_generation += 1
    account.execution_lock_id = uuid4()
    account.recovery_pending = False


def _attempt_id(intent_id: UUID, ordinal: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"alphadecay:attempt:{intent_id}:{ordinal}")


def _creation_permit(
    attempt: OrderAttemptRow, permits: list[BrokerMutationPermitRow]
) -> BrokerMutationPermitRow:
    if attempt.broker_permit_id is None:
        raise ExecutionBlocked("ATTEMPT_PERMIT_MISSING")
    mutation_kind = (
        ReconciliationPurpose.SUBMIT.value
        if attempt.attempt_ordinal == 0
        else ReconciliationPurpose.REPLACE.value
    )
    generations = [
        permit
        for permit in permits
        if permit.execution_intent_id == attempt.execution_intent_id
        and permit.mutation_kind == mutation_kind
        and permit.attempt_ordinal == attempt.attempt_ordinal
    ]
    if not generations or generations[0].permit_id != attempt.broker_permit_id:
        raise ExecutionBlocked("ATTEMPT_PERMIT_MISSING")
    return generations[-1]


def _execution_attempt_rows(
    attempts: list[OrderAttemptRow],
    permits: list[BrokerMutationPermitRow],
) -> list[OrderAttemptRow]:
    if len(attempts) < 2 or attempts[-1].state != "PREPARED":
        return attempts
    replacement = attempts[-1]
    replacement_permit = _creation_permit(replacement, permits)
    if replacement_permit.state != "EXPIRED":
        return attempts
    predecessor = attempts[-2]
    if not any(
        permit.mutation_kind == ReconciliationPurpose.CANCEL.value
        and permit.predecessor_permit_id == replacement_permit.permit_id
        and permit.target_client_order_id == predecessor.client_order_id
        and permit.target_provider_order_id == predecessor.provider_order_id
        for permit in permits
    ):
        raise ExecutionBlocked("PREPARED_ATTEMPT_PERMIT_INVALID")
    return attempts[:-1]


def _expire_prepared_generation(
    permit: BrokerMutationPermitRow,
    trusted_now: datetime,
    request_hash: str,
) -> None:
    if permit.request_hash != request_hash:
        raise ExecutionBlocked("BROKER_PERMIT_REQUEST_MISMATCH")
    if permit.state != "PREPARED":
        raise ExecutionBlocked("BROKER_PERMIT_REAUTHORIZATION_NOT_PROVEN")
    if _utc(permit.expires_at) > trusted_now:
        raise ExecutionBlocked("BROKER_PERMIT_NOT_EXPIRED")
    permit.state = "EXPIRED"
    permit.consumed_at = trusted_now


def _supersede_prepared_replacement_for_hard_cancel(
    permit: BrokerMutationPermitRow,
    initial_permit: BrokerMutationPermitRow,
    trusted_now: datetime,
) -> None:
    if (
        permit.mutation_kind != ReconciliationPurpose.REPLACE.value
        or permit.state != "PREPARED"
        or permit.dispatch_nonce is not None
        or permit.dispatch_acquired_at is not None
        or permit.consumed_at is not None
        or permit.outcome_hash is not None
    ):
        raise ExecutionBlocked("BROKER_PERMIT_REAUTHORIZATION_NOT_PROVEN")
    if initial_permit.dispatch_acquired_at is None:
        raise ExecutionBlocked("INITIAL_DISPATCH_TIME_MISSING")
    if trusted_now - _utc(initial_permit.dispatch_acquired_at) < timedelta(seconds=600):
        raise ExecutionBlocked("REPLACEMENT_CANCEL_NOT_DUE")
    permit.state = "EXPIRED"
    permit.consumed_at = trusted_now


def _refresh_prepared_replacement(row: OrderAttemptRow, attempt: OrderAttempt) -> None:
    row.request_hash = attempt.request_hash
    row.limit_price = attempt.limit_price
    row.quote_hash = attempt.quote_hash
    row.quote_source_timestamps = [
        timestamp.isoformat() for timestamp in attempt.quote_source_timestamps
    ]
    row.quote_retrieved_at = attempt.quote_retrieved_at
    row.timing_authority_at = attempt.timing_authority_at
    row.prior_request_hash = attempt.prior_request_hash


def _verify_consumed_cancel_retry(
    permit: BrokerMutationPermitRow,
    target: OrderAttemptRow,
    attempt_rows: list[OrderAttemptRow],
    observations: list[AttemptObservationRow],
    network_call_horizon: timedelta,
) -> None:
    if permit.dispatch_acquired_at is None or permit.consumed_at is None:
        raise ExecutionBlocked("CANCEL_PERMIT_REAUTHORIZATION_NOT_PROVEN")
    matching = [row for row in observations if row.permit_id == permit.permit_id]
    if not matching:
        raise ExecutionBlocked("CANCEL_PERMIT_REAUTHORIZATION_NOT_PROVEN")
    latest = matching[-1]
    observed = (
        _attempt_from_json(latest.observed_payload) if latest.observed_payload is not None else None
    )
    expected = _attempt_from_row(target, attempt_rows)
    if (
        latest.source != AttemptObservationSource.TARGETED_LOOKUP.value
        or observed != expected
        or expected.state not in MUTATION_ELIGIBLE_BROKER_ORDER_STATES
        or _utc(latest.observed_at)
        < max(
            _utc(permit.expires_at),
            _utc(permit.dispatch_acquired_at) + network_call_horizon,
        )
    ):
        raise ExecutionBlocked("CANCEL_PERMIT_REAUTHORIZATION_NOT_PROVEN")


def _attempts_from_rows(rows: list[OrderAttemptRow]) -> tuple[OrderAttempt, ...]:
    by_id = {row.attempt_id: row.client_order_id for row in rows}
    return tuple(
        OrderAttempt(
            intent_id=row.execution_intent_id,
            ordinal=row.attempt_ordinal,
            client_order_id=row.client_order_id,
            request_hash=row.request_hash,
            state=row.state,
            replaces_client_order_id=by_id.get(row.replaces_attempt_id),
            provider_order_id=row.provider_order_id,
            filled_quantity=row.filled_quantity,
            quantity=row.quantity,
            fill_cash_flow=row.fill_cash_flow,
            limit_price=row.limit_price,
            quote_hash=row.quote_hash,
            quote_source_timestamps=tuple(
                datetime.fromisoformat(value) for value in row.quote_source_timestamps
            ),
            quote_retrieved_at=_utc(row.quote_retrieved_at) if row.quote_retrieved_at else None,
            timing_authority_at=(
                _utc(row.timing_authority_at) if row.timing_authority_at else None
            ),
            prior_request_hash=row.prior_request_hash,
        )
        for row in rows
    )


def _attempt_from_row(row: OrderAttemptRow, rows: list[OrderAttemptRow]) -> OrderAttempt:
    by_id = {item.attempt_id: item.client_order_id for item in rows}
    return OrderAttempt(
        intent_id=row.execution_intent_id,
        ordinal=row.attempt_ordinal,
        client_order_id=row.client_order_id,
        request_hash=row.request_hash,
        state=row.state,
        replaces_client_order_id=by_id.get(row.replaces_attempt_id),
        provider_order_id=row.provider_order_id,
        filled_quantity=row.filled_quantity,
        quantity=row.quantity,
        fill_cash_flow=row.fill_cash_flow,
        limit_price=row.limit_price,
        quote_hash=row.quote_hash,
        quote_source_timestamps=tuple(
            datetime.fromisoformat(value) for value in row.quote_source_timestamps
        ),
        quote_retrieved_at=_utc(row.quote_retrieved_at) if row.quote_retrieved_at else None,
        timing_authority_at=_utc(row.timing_authority_at) if row.timing_authority_at else None,
        prior_request_hash=row.prior_request_hash,
    )


def _attempt_to_json(value: OrderAttempt) -> dict[str, object]:
    return {
        "intent_id": str(value.intent_id),
        "ordinal": value.ordinal,
        "client_order_id": value.client_order_id,
        "request_hash": value.request_hash,
        "state": value.state,
        "replaces_client_order_id": value.replaces_client_order_id,
        "provider_order_id": value.provider_order_id,
        "filled_quantity": value.filled_quantity,
        "quantity": value.quantity,
        "fill_cash_flow": (str(value.fill_cash_flow) if value.fill_cash_flow is not None else None),
        "limit_price": str(value.limit_price) if value.limit_price is not None else None,
        "quote_hash": value.quote_hash,
        "quote_source_timestamps": [item.isoformat() for item in value.quote_source_timestamps],
        "quote_retrieved_at": (
            value.quote_retrieved_at.isoformat() if value.quote_retrieved_at else None
        ),
        "timing_authority_at": (
            value.timing_authority_at.isoformat() if value.timing_authority_at else None
        ),
        "prior_request_hash": value.prior_request_hash,
    }


def _attempt_from_json(value: dict[str, object]) -> OrderAttempt:
    return OrderAttempt(
        intent_id=UUID(str(value["intent_id"])),
        ordinal=int(value["ordinal"]),
        client_order_id=str(value["client_order_id"]),
        request_hash=str(value["request_hash"]),
        state=str(value["state"]),
        replaces_client_order_id=(
            str(value["replaces_client_order_id"])
            if value["replaces_client_order_id"] is not None
            else None
        ),
        provider_order_id=(
            str(value["provider_order_id"]) if value["provider_order_id"] is not None else None
        ),
        filled_quantity=int(value["filled_quantity"]),
        quantity=int(value["quantity"]),
        fill_cash_flow=(
            Decimal(str(value["fill_cash_flow"]))
            if value.get("fill_cash_flow") is not None
            else None
        ),
        limit_price=(
            Decimal(str(value["limit_price"])) if value.get("limit_price") is not None else None
        ),
        quote_hash=(str(value["quote_hash"]) if value.get("quote_hash") is not None else None),
        quote_source_timestamps=tuple(
            datetime.fromisoformat(str(item)) for item in value.get("quote_source_timestamps", [])
        ),
        quote_retrieved_at=(
            datetime.fromisoformat(str(value["quote_retrieved_at"]))
            if value.get("quote_retrieved_at") is not None
            else None
        ),
        timing_authority_at=(
            datetime.fromisoformat(str(value["timing_authority_at"]))
            if value.get("timing_authority_at") is not None
            else None
        ),
        prior_request_hash=(
            str(value["prior_request_hash"])
            if value.get("prior_request_hash") is not None
            else None
        ),
    )


def _attempt_observation_from_row(row: AttemptObservationRow) -> AttemptObservation:
    observed = (
        _attempt_from_json(row.observed_payload) if row.observed_payload is not None else None
    )
    material = {
        "domain": "alphadecay.attempt-observation.v1",
        "permit_id": str(row.permit_id),
        "intent_id": str(row.execution_intent_id),
        "attempt_ordinal": row.attempt_ordinal,
        "sequence": row.observation_sequence,
        "source": row.source,
        "observed_attempt": row.observed_payload,
        "observed_at": _utc(row.observed_at).isoformat(),
    }
    if _authority_hash(material) != row.observation_hash:
        raise ExecutionBlocked("ATTEMPT_OBSERVATION_CORRUPT")
    return AttemptObservation(
        observation_id=row.observation_id,
        permit_id=row.permit_id,
        intent_id=row.execution_intent_id,
        attempt_ordinal=row.attempt_ordinal,
        sequence=row.observation_sequence,
        source=AttemptObservationSource(row.source),
        observed_attempt=observed,
        observed_at=_utc(row.observed_at),
        observation_hash=row.observation_hash,
    )


def _legs_to_json(legs: tuple[OrderLegIntent, ...]) -> list[dict[str, object]]:
    return [{"symbol": leg.symbol, "intent": leg.intent.value, "ratio": leg.ratio} for leg in legs]


def _legs_from_json(value: list[dict[str, object]]) -> tuple[OrderLegIntent, ...]:
    return tuple(
        OrderLegIntent(
            symbol=str(leg["symbol"]),
            intent=PositionIntent(str(leg["intent"])),
            ratio=int(leg["ratio"]),
        )
        for leg in value
    )


def _envelope_to_json(envelope: OrderEnvelope) -> dict[str, object]:
    return {
        "action": envelope.action.value,
        "authorization_certificate_id": str(envelope.authorization_certificate_id),
        "policy_hash": envelope.policy_hash,
        "account_fingerprint": envelope.account_fingerprint,
        "position_or_book_fingerprint": envelope.position_or_book_fingerprint,
        "legs": _legs_to_json(envelope.legs),
        "quantity": envelope.quantity,
        "minimum_limit": str(envelope.minimum_limit),
        "maximum_limit": str(envelope.maximum_limit),
        "approved_max_loss": str(envelope.approved_max_loss),
        "event_key": envelope.event_key,
        "trading_day": envelope.trading_day.isoformat(),
        "market_session_id": (
            str(envelope.market_session_id) if envelope.market_session_id is not None else None
        ),
        "quoted_relative_spread": _decimal_or_none(envelope.quoted_relative_spread),
        "maximum_relative_spread": _decimal_or_none(envelope.maximum_relative_spread),
        "incremental_debit": _decimal_or_none(envelope.incremental_debit),
        "maximum_incremental_debit": _decimal_or_none(envelope.maximum_incremental_debit),
    }


def _envelope_from_json(value: dict[str, object]) -> OrderEnvelope:
    return OrderEnvelope(
        action=ExecutionAction(str(value["action"])),
        authorization_certificate_id=UUID(str(value["authorization_certificate_id"])),
        policy_hash=str(value["policy_hash"]),
        account_fingerprint=str(value["account_fingerprint"]),
        position_or_book_fingerprint=str(value["position_or_book_fingerprint"]),
        legs=_legs_from_json(value["legs"]),
        quantity=int(value["quantity"]),
        minimum_limit=Decimal(str(value["minimum_limit"])),
        maximum_limit=Decimal(str(value["maximum_limit"])),
        approved_max_loss=Decimal(str(value["approved_max_loss"])),
        event_key=str(value["event_key"]),
        trading_day=datetime.fromisoformat(str(value["trading_day"])).date(),
        market_session_id=(
            UUID(str(value["market_session_id"]))
            if value.get("market_session_id") is not None
            else None
        ),
        quoted_relative_spread=_optional_decimal(value.get("quoted_relative_spread")),
        maximum_relative_spread=_optional_decimal(value.get("maximum_relative_spread")),
        incremental_debit=_optional_decimal(value.get("incremental_debit")),
        maximum_incremental_debit=_optional_decimal(value.get("maximum_incremental_debit")),
    )


def _decimal_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _optional_decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _exposure_to_json(exposure: GreekExposure | None) -> dict[str, str] | None:
    if exposure is None:
        return None
    return {
        "delta": str(exposure.delta),
        "gamma": str(exposure.gamma),
        "theta_per_day": str(exposure.theta_per_day),
        "vega_per_iv_point": str(exposure.vega_per_iv_point),
    }


def _activity_to_json(activity: ActivityItem) -> dict[str, object]:
    return {
        "activity_id_hash": activity.activity_id_hash,
        "activity_type": activity.activity_type.value,
        "occurred_at": activity.occurred_at.isoformat(),
        "symbol": activity.symbol,
        "signed_quantity": (
            str(activity.signed_quantity) if activity.signed_quantity is not None else None
        ),
        "provider_order_id": activity.provider_order_id,
        "client_order_id": activity.client_order_id,
        "time_quality": activity.time_quality,
        "provider_activity_type": activity.provider_activity_type,
    }


def _activity_from_json(value: dict[str, object]) -> ActivityItem:
    quantity = value["signed_quantity"]
    return ActivityItem(
        activity_id_hash=str(value["activity_id_hash"]),
        activity_type=ActivityType(str(value["activity_type"])),
        occurred_at=_utc(datetime.fromisoformat(str(value["occurred_at"]))),
        symbol=str(value["symbol"]) if value["symbol"] is not None else None,
        signed_quantity=Decimal(str(quantity)) if quantity is not None else None,
        provider_order_id=(
            str(value["provider_order_id"]) if value.get("provider_order_id") is not None else None
        ),
        client_order_id=(
            str(value["client_order_id"]) if value.get("client_order_id") is not None else None
        ),
        time_quality=str(value.get("time_quality", "EXACT_TRANSACTION_TIME")),
        provider_activity_type=(
            str(value["provider_activity_type"])
            if value.get("provider_activity_type") is not None
            else None
        ),
    )


def _inventory_to_json(item: InventoryItem) -> dict[str, object]:
    return {
        "kind": item.kind.value,
        "symbol": item.symbol,
        "signed_quantity": canonical_decimal(item.signed_quantity),
        "multiplier": item.multiplier,
    }


def _inventory_from_json(value: dict[str, object]) -> InventoryItem:
    return InventoryItem(
        kind=InventoryKind(str(value["kind"])),
        symbol=str(value["symbol"]),
        signed_quantity=Decimal(str(value["signed_quantity"])),
        multiplier=int(value["multiplier"]),
    )


def _open_order_to_json(item: OpenOrderItem) -> dict[str, object]:
    return {
        "provider_order_id": item.provider_order_id,
        "client_order_id": item.client_order_id,
        "state": item.state,
        "quantity": item.quantity,
        "filled_quantity": item.filled_quantity,
        "replaces_client_order_id": item.replaces_client_order_id,
        "replaced_by_client_order_id": item.replaced_by_client_order_id,
        "order_class": item.order_class,
        "legs": [
            {"symbol": leg.symbol, "intent": leg.intent.value, "ratio": leg.ratio}
            for leg in item.legs
        ],
    }


def _open_order_from_json(value: dict[str, object]) -> OpenOrderItem:
    legs = value["legs"]
    return OpenOrderItem(
        provider_order_id=str(value["provider_order_id"]),
        client_order_id=str(value["client_order_id"]),
        state=str(value["state"]),
        quantity=int(value["quantity"]),
        filled_quantity=int(value["filled_quantity"]),
        replaces_client_order_id=(
            str(value["replaces_client_order_id"])
            if value["replaces_client_order_id"] is not None
            else None
        ),
        replaced_by_client_order_id=(
            str(value["replaced_by_client_order_id"])
            if value["replaced_by_client_order_id"] is not None
            else None
        ),
        order_class=str(value["order_class"]),
        legs=tuple(
            OpenOrderLeg(
                symbol=str(leg["symbol"]),
                intent=PositionIntent(str(leg["intent"])),
                ratio=int(leg["ratio"]),
            )
            for leg in legs
        ),
    )


def _account_observation_to_json(value: AccountObservation) -> dict[str, object]:
    return {
        "role": value.role.value,
        "account_fingerprint": value.account_fingerprint,
        "paper": value.paper,
        "status": value.status,
        "account_blocked": value.account_blocked,
        "trading_blocked": value.trading_blocked,
        "options_trading_blocked": value.options_trading_blocked,
        "equity": str(value.equity),
        "buying_power": str(value.buying_power),
        "cash": str(value.cash),
        "observed_at": value.observed_at.isoformat(),
        "time_quality": value.time_quality,
    }


def _account_observation_from_json(value: dict[str, object]) -> AccountObservation:
    return AccountObservation(
        role=AccountRole(str(value["role"])),
        account_fingerprint=str(value["account_fingerprint"]),
        paper=bool(value["paper"]),
        status=str(value["status"]),
        account_blocked=bool(value["account_blocked"]),
        trading_blocked=bool(value["trading_blocked"]),
        options_trading_blocked=bool(value["options_trading_blocked"]),
        equity=Decimal(str(value["equity"])),
        buying_power=Decimal(str(value["buying_power"])),
        cash=Decimal(str(value["cash"])),
        observed_at=_utc(datetime.fromisoformat(str(value["observed_at"]))),
        time_quality=str(value["time_quality"]),
    )


def _pagination_to_json(value: ActivityPaginationEvidence) -> dict[str, object]:
    return {
        "requested_start": value.requested_start.isoformat(),
        "requested_end": value.requested_end.isoformat(),
        "retrieved_through": value.retrieved_through.isoformat(),
        "established_at": value.established_at.isoformat(),
        "page_count": value.page_count,
        "terminal_page_seen": value.terminal_page_seen,
        "visibility_complete_through": value.visibility_complete_through.isoformat(),
        "visibility_horizon_seconds": int(value.visibility_horizon.total_seconds()),
    }


def _pagination_from_json(value: dict[str, object]) -> ActivityPaginationEvidence:
    return ActivityPaginationEvidence(
        requested_start=_utc(datetime.fromisoformat(str(value["requested_start"]))),
        requested_end=_utc(datetime.fromisoformat(str(value["requested_end"]))),
        retrieved_through=_utc(datetime.fromisoformat(str(value["retrieved_through"]))),
        established_at=_utc(datetime.fromisoformat(str(value["established_at"]))),
        page_count=int(value["page_count"]),
        terminal_page_seen=bool(value["terminal_page_seen"]),
        visibility_complete_through=_utc(
            datetime.fromisoformat(str(value["visibility_complete_through"]))
        ),
        visibility_horizon=timedelta(seconds=int(value["visibility_horizon_seconds"])),
    )


def _sweep_to_json(value: SweepObservation) -> dict[str, object]:
    return {
        "retrieval_started_at": value.retrieval_started_at.isoformat(),
        "retrieval_completed_at": value.retrieval_completed_at.isoformat(),
        "activity_pagination": _pagination_to_json(value.activity_pagination),
        "first_account": _account_observation_to_json(value.first_account),
        "final_account": _account_observation_to_json(value.final_account),
        "first_positions": [_inventory_to_json(item) for item in value.first_positions],
        "final_positions": [_inventory_to_json(item) for item in value.final_positions],
        "first_open_orders": [_open_order_to_json(item) for item in value.first_open_orders],
        "final_open_orders": [_open_order_to_json(item) for item in value.final_open_orders],
        "activities": [_activity_to_json(item) for item in value.activities],
        "positions_complete": value.positions_complete,
        "orders_complete": value.orders_complete,
    }


def _sweep_from_json(value: dict[str, object]) -> SweepObservation:
    return SweepObservation(
        retrieval_started_at=_utc(datetime.fromisoformat(str(value["retrieval_started_at"]))),
        retrieval_completed_at=_utc(datetime.fromisoformat(str(value["retrieval_completed_at"]))),
        activity_pagination=_pagination_from_json(value["activity_pagination"]),
        first_account=_account_observation_from_json(value["first_account"]),
        final_account=_account_observation_from_json(value["final_account"]),
        first_positions=tuple(_inventory_from_json(item) for item in value["first_positions"]),
        final_positions=tuple(_inventory_from_json(item) for item in value["final_positions"]),
        first_open_orders=tuple(_open_order_from_json(item) for item in value["first_open_orders"]),
        final_open_orders=tuple(_open_order_from_json(item) for item in value["final_open_orders"]),
        activities=tuple(_activity_from_json(item) for item in value["activities"]),
        positions_complete=bool(value["positions_complete"]),
        orders_complete=bool(value["orders_complete"]),
    )


def _expectation_to_json(value: ReconciliationExpectation) -> dict[str, object]:
    return {
        "purpose": value.purpose.value,
        "account_role": value.account_role.value,
        "account_fingerprint": value.account_fingerprint,
        "expected_cash": str(value.expected_cash),
        "baseline_captured_at": value.baseline_captured_at.isoformat(),
        "expected_positions": [_inventory_to_json(item) for item in value.expected_positions],
        "expected_open_orders": [_open_order_to_json(item) for item in value.expected_open_orders],
        "known_activities": [_activity_to_json(item) for item in value.known_activities],
        "resolved_activity_hashes": list(value.resolved_activity_hashes),
        "required_activity_window_start": value.required_activity_window_start.isoformat(),
        "required_activity_complete_through": (
            value.required_activity_complete_through.isoformat()
        ),
        "intent_id": str(value.intent_id),
        "intent_digest": value.intent_digest,
        "attempt_ordinal": value.attempt_ordinal,
        "request_hash": value.request_hash,
        "expectation_hash": value.expectation_hash,
    }


def _expectation_from_json(value: dict[str, object]) -> ReconciliationExpectation:
    expectation = ReconciliationExpectation._from_repository_state(
        purpose=ReconciliationPurpose(str(value["purpose"])),
        account_role=AccountRole(str(value["account_role"])),
        account_fingerprint=str(value["account_fingerprint"]),
        expected_cash=Decimal(str(value["expected_cash"])),
        baseline_captured_at=_utc(datetime.fromisoformat(str(value["baseline_captured_at"]))),
        expected_positions=tuple(
            _inventory_from_json(item) for item in value["expected_positions"]
        ),
        expected_open_orders=tuple(
            _open_order_from_json(item) for item in value["expected_open_orders"]
        ),
        known_activities=tuple(_activity_from_json(item) for item in value["known_activities"]),
        resolved_activity_hashes=tuple(str(item) for item in value["resolved_activity_hashes"]),
        required_activity_window_start=_utc(
            datetime.fromisoformat(str(value["required_activity_window_start"]))
        ),
        required_activity_complete_through=_utc(
            datetime.fromisoformat(str(value["required_activity_complete_through"]))
        ),
        intent_id=UUID(str(value["intent_id"])),
        intent_digest=str(value["intent_digest"]),
        attempt_ordinal=int(value["attempt_ordinal"]),
        request_hash=str(value["request_hash"]),
    )
    if expectation.expectation_hash != value["expectation_hash"]:
        raise ExecutionBlocked("RECONCILIATION_EXPECTATION_CORRUPT")
    return expectation


def _reconciliation_expectation_from_rows(
    intent: ExecutionIntentRow,
    state: AccountReconciliationStateRow,
    purpose: ReconciliationPurpose,
    attempt: OrderAttempt,
    attempt_rows: list[OrderAttemptRow],
    activity_candidates: tuple[ActivityItem, ...] | None = None,
) -> ReconciliationExpectation:
    if attempt.intent_id != intent.intent_id or attempt.ordinal > 3:
        raise ExecutionBlocked("RECONCILIATION_ATTEMPT_MISMATCH")
    if purpose == ReconciliationPurpose.BASELINE_INITIALIZATION:
        raise ExecutionBlocked("BASELINE_INITIALIZATION_IS_NOT_BROKER_MUTATION")
    if purpose == ReconciliationPurpose.REPLACE:
        if attempt.quote_hash is None:
            raise ExecutionBlocked("REPLACEMENT_QUOTES_REQUIRED")
        _validate_replacement_attempt(intent, attempt_rows, attempt, None)
        canonical_attempt = attempt
    else:
        canonical_attempt = _canonical_attempt_for_mutation(intent, purpose, attempt_rows)
    if attempt != canonical_attempt:
        raise ExecutionBlocked("BROKER_MUTATION_MATERIAL_MISMATCH")
    if purpose == ReconciliationPurpose.SUBMIT:
        if attempt.ordinal != 0:
            raise ExecutionBlocked("SUBMIT_ORDINAL_INVALID")
        if attempt_rows and (
            len(attempt_rows) != 1
            or _attempt_from_row(attempt_rows[0], attempt_rows) != attempt
            or attempt_rows[0].state != "PREPARED"
        ):
            raise ExecutionBlocked("SUBMIT_REAUTHORIZATION_MISMATCH")
        expected_open_orders = tuple(
            _open_order_from_json(item) for item in state.expected_open_orders
        )
        expected_cash = state.expected_cash
        expected_positions = tuple(_inventory_from_json(item) for item in state.expected_positions)
        request_hash = attempt.request_hash
    else:
        if not attempt_rows:
            raise ExecutionBlocked("TARGET_ATTEMPT_NOT_FOUND")
        current = _attempt_from_row(attempt_rows[-1], attempt_rows)
        if purpose == ReconciliationPurpose.REPLACE:
            reauthorizing = (
                current.state == "PREPARED"
                and current.ordinal == attempt.ordinal
                and len(attempt_rows) >= 2
            )
            target = _attempt_from_row(attempt_rows[-2], attempt_rows) if reauthorizing else current
            if (
                target.provider_order_id is None
                or target.state not in MUTATION_ELIGIBLE_BROKER_ORDER_STATES
            ):
                raise ExecutionBlocked("TARGET_ORDER_NOT_ACTIVE")
            if (
                attempt.ordinal != target.ordinal + 1
                or attempt.replaces_client_order_id != target.client_order_id
            ):
                raise ExecutionBlocked("REPLACE_LINEAGE_INVALID")
            request_hash = attempt.request_hash
        elif purpose == ReconciliationPurpose.CANCEL:
            if current.state == "PREPARED" and len(attempt_rows) >= 2:
                current = _attempt_from_row(attempt_rows[-2], attempt_rows)
            if (
                current.provider_order_id is None
                or current.quantity <= 0
                or current.state not in MUTATION_ELIGIBLE_BROKER_ORDER_STATES
            ):
                raise ExecutionBlocked("TARGET_ORDER_NOT_ACTIVE")
            if attempt != current:
                raise ExecutionBlocked("CANCEL_TARGET_MISMATCH")
            request_hash = _cancel_request_hash(intent.intent_digest, current)
        else:
            raise ExecutionBlocked("RECONCILIATION_PURPOSE_INVALID")
        expected_open_orders = (
            _open_order_from_attempt(
                intent, target if purpose == ReconciliationPurpose.REPLACE else current
            ),
        )
        filled_attempt = target if purpose == ReconciliationPurpose.REPLACE else current
        expected_cash = state.expected_cash + (filled_attempt.fill_cash_flow or Decimal(0))
        expected_positions = _positions_after_fill(intent, state, filled_attempt)
    known_activities, resolved_activity_hashes = _bound_attempt_activities(
        intent,
        state,
        filled_attempt if purpose != ReconciliationPurpose.SUBMIT else attempt,
        activity_candidates,
        require_visible=False,
    )
    return ReconciliationExpectation._from_repository_state(
        purpose=purpose,
        account_role=AccountRole(intent.account_role),
        account_fingerprint=state.account_fingerprint,
        expected_cash=expected_cash,
        baseline_captured_at=_utc(state.baseline_captured_at),
        expected_positions=expected_positions,
        expected_open_orders=expected_open_orders,
        known_activities=known_activities,
        resolved_activity_hashes=resolved_activity_hashes,
        required_activity_window_start=_utc(state.baseline_captured_at),
        required_activity_complete_through=_utc(state.activity_complete_through),
        intent_id=intent.intent_id,
        intent_digest=intent.intent_digest,
        attempt_ordinal=attempt.ordinal,
        request_hash=request_hash,
    )


def _open_order_from_attempt(intent: ExecutionIntentRow, attempt: OrderAttempt) -> OpenOrderItem:
    if attempt.provider_order_id is None:
        raise ExecutionBlocked("TARGET_PROVIDER_ORDER_ID_REQUIRED")
    envelope = _envelope_from_json(intent.envelope_payload)
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
            for leg in envelope.legs
        ),
    )


def _canonical_attempt_for_mutation(
    intent: ExecutionIntentRow,
    purpose: ReconciliationPurpose,
    attempts: list[OrderAttemptRow],
) -> OrderAttempt:
    if purpose == ReconciliationPurpose.SUBMIT:
        expected = _prepared_attempt(intent, 0, None)
        if not attempts:
            return expected
        if len(attempts) != 1 or _attempt_from_row(attempts[0], attempts) != expected:
            raise ExecutionBlocked("SUBMIT_REAUTHORIZATION_MISMATCH")
        return expected
    if purpose == ReconciliationPurpose.REPLACE:
        if not attempts:
            raise ExecutionBlocked("TARGET_ATTEMPT_NOT_FOUND")
        current = _attempt_from_row(attempts[-1], attempts)
        if current.state == "PREPARED" and current.provider_order_id is None:
            if len(attempts) < 2:
                raise ExecutionBlocked("REPLACE_REAUTHORIZATION_MISMATCH")
            predecessor = _attempt_from_row(attempts[-2], attempts)
            expected = _prepared_attempt(
                intent, predecessor.ordinal + 1, predecessor.client_order_id
            )
            if current != expected:
                raise ExecutionBlocked("REPLACE_REAUTHORIZATION_MISMATCH")
            return expected
        return _prepared_attempt(intent, current.ordinal + 1, current.client_order_id)
    if purpose == ReconciliationPurpose.CANCEL:
        if not attempts:
            raise ExecutionBlocked("TARGET_ATTEMPT_NOT_FOUND")
        current = _attempt_from_row(attempts[-1], attempts)
        if current.state == "PREPARED" and len(attempts) >= 2:
            return _attempt_from_row(attempts[-2], attempts)
        return current
    raise ExecutionBlocked("RECONCILIATION_PURPOSE_INVALID")


def _prepared_attempt(
    intent: ExecutionIntentRow,
    ordinal: int,
    replaced_client_id: str | None,
) -> OrderAttempt:
    envelope = _envelope_from_json(intent.envelope_payload)
    identifier = client_order_id(
        envelope.trading_day,
        envelope.action,
        intent.intent_digest,
        ordinal,
    )
    return OrderAttempt(
        intent_id=intent.intent_id,
        ordinal=ordinal,
        client_order_id=identifier,
        request_hash=attempt_request_hash(
            intent.intent_digest,
            ordinal,
            identifier,
            envelope.minimum_limit,
            replaced_client_id,
        ),
        state="PREPARED",
        replaces_client_order_id=replaced_client_id,
        provider_order_id=None,
        filled_quantity=0,
        quantity=envelope.quantity,
    )


def _validate_replacement_attempt(
    intent: ExecutionIntentRow,
    attempts: list[OrderAttemptRow],
    attempt: OrderAttempt,
    trusted_now: datetime | None,
) -> None:
    if not attempts:
        raise ExecutionBlocked("TARGET_ATTEMPT_NOT_FOUND")
    current = _attempt_from_row(attempts[-1], attempts)
    if current.ordinal == attempt.ordinal:
        if current.state != "PREPARED" or len(attempts) < 2:
            raise ExecutionBlocked("REPLACEMENT_REAUTHORIZATION_MISMATCH")
        current = _attempt_from_row(attempts[-2], attempts)
    envelope = _envelope_from_json(intent.envelope_payload)
    current_limit = (
        current.limit_price if current.limit_price is not None else envelope.minimum_limit
    )
    expected_client_id = client_order_id(
        envelope.trading_day,
        envelope.action,
        intent.intent_digest,
        attempt.ordinal,
    )
    required = (
        attempt.limit_price,
        attempt.quote_hash,
        attempt.quote_retrieved_at,
        attempt.timing_authority_at,
        attempt.prior_request_hash,
    )
    if (
        any(value is None for value in required)
        or attempt.client_order_id != expected_client_id
        or current.state not in MUTATION_ELIGIBLE_BROKER_ORDER_STATES - {"PARTIALLY_FILLED"}
        or current.provider_order_id is None
        or attempt.state != "PREPARED"
        or attempt.provider_order_id is not None
        or attempt.filled_quantity != 0
        or attempt.quantity != envelope.quantity
        or attempt.ordinal != current.ordinal + 1
        or attempt.replaces_client_order_id != current.client_order_id
        or attempt.prior_request_hash != current.request_hash
        or not attempt.quote_source_timestamps
        or len(attempt.quote_source_timestamps) != len(envelope.legs)
        or attempt.limit_price <= current_limit
        or attempt.limit_price == 0
        or attempt.limit_price > envelope.maximum_limit
        or not attempt.limit_price.is_finite()
        or attempt.limit_price != attempt.limit_price.quantize(Decimal("0.01"))
        or len(attempt.quote_hash) != 64
        or any(character not in "0123456789abcdef" for character in attempt.quote_hash)
        or any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (
                *attempt.quote_source_timestamps,
                attempt.quote_retrieved_at,
                attempt.timing_authority_at,
            )
        )
        or attempt.quote_retrieved_at > attempt.timing_authority_at
        or any(
            timestamp > attempt.quote_retrieved_at for timestamp in attempt.quote_source_timestamps
        )
        or max(attempt.quote_source_timestamps) - min(attempt.quote_source_timestamps)
        > timedelta(seconds=30)
    ):
        raise ExecutionBlocked("REPLACEMENT_AUTHORITY_INVALID")
    if trusted_now is not None and (
        attempt.timing_authority_at > trusted_now
        or trusted_now - attempt.timing_authority_at > timedelta(seconds=30)
        or attempt.quote_retrieved_at > trusted_now
        or any(
            timestamp > trusted_now or trusted_now - timestamp > timedelta(seconds=30)
            for timestamp in attempt.quote_source_timestamps
        )
    ):
        raise ExecutionBlocked("REPLACEMENT_QUOTE_STALE")
    expected = replacement_request_hash(
        intent.intent_digest,
        attempt.ordinal,
        attempt.client_order_id,
        attempt.limit_price,
        attempt.replaces_client_order_id,
        attempt.prior_request_hash,
        attempt.quote_hash,
        attempt.quote_source_timestamps,
        attempt.quote_retrieved_at,
        attempt.timing_authority_at,
    )
    if attempt.request_hash != expected:
        raise ExecutionBlocked("REPLACEMENT_REQUEST_HASH_MISMATCH")


def _replacement_with_timing(
    intent: ExecutionIntentRow,
    attempt: OrderAttempt,
    timing_authority_at: datetime,
) -> OrderAttempt:
    if (
        attempt.limit_price is None
        or attempt.quote_hash is None
        or attempt.quote_retrieved_at is None
        or attempt.prior_request_hash is None
    ):
        raise ExecutionBlocked("REPLACEMENT_AUTHORITY_INVALID")
    return replace(
        attempt,
        timing_authority_at=timing_authority_at,
        request_hash=replacement_request_hash(
            intent.intent_digest,
            attempt.ordinal,
            attempt.client_order_id,
            attempt.limit_price,
            attempt.replaces_client_order_id,
            attempt.prior_request_hash,
            attempt.quote_hash,
            attempt.quote_source_timestamps,
            attempt.quote_retrieved_at,
            timing_authority_at,
        ),
    )


def _validate_replacement_due(
    attempts: list[OrderAttemptRow],
    permits: list[BrokerMutationPermitRow],
    attempt: OrderAttempt,
) -> None:
    if attempt.ordinal not in {1, 2, 3} or not attempts:
        raise ExecutionBlocked("REPLACEMENT_BOUNDARY_INVALID")
    initial_permit = _creation_permit(attempts[0], permits)
    if initial_permit.dispatch_acquired_at is None:
        raise ExecutionBlocked("INITIAL_DISPATCH_TIME_MISSING")
    elapsed = attempt.timing_authority_at - _utc(initial_permit.dispatch_acquired_at)
    due_seconds = (150, 300, 450)[attempt.ordinal - 1]
    if elapsed < timedelta(seconds=due_seconds):
        raise ExecutionBlocked("REPLACEMENT_NOT_DUE")
    if elapsed >= timedelta(seconds=600):
        raise ExecutionBlocked("REPLACEMENT_CANCEL_DUE")


def _cancel_request_hash(intent_digest_value: str, target: OrderAttempt) -> str:
    return _authority_hash(
        {
            "domain": "alphadecay.cancel-request.v1",
            "intent_digest": intent_digest_value,
            "attempt_ordinal": target.ordinal,
            "target_client_order_id": target.client_order_id,
            "target_provider_order_id": target.provider_order_id,
        }
    )


def _final_reconciliation_expectation_from_rows(
    intent: ExecutionIntentRow,
    state: AccountReconciliationStateRow,
    attempts: list[OrderAttemptRow],
    observations: list[AttemptObservationRow],
    latest_permit: BrokerMutationPermitRow | None,
    activity_candidates: tuple[ActivityItem, ...] | None = None,
) -> ReconciliationExpectation:
    if not attempts or not observations or latest_permit is None:
        raise ExecutionBlocked("FINAL_RECONCILIATION_LINEAGE_MISSING")
    current = _attempt_from_row(attempts[-1], attempts)
    latest = observations[-1]
    if (
        latest.permit_id != latest_permit.permit_id
        or latest.attempt_ordinal != latest_permit.attempt_ordinal
    ):
        raise ExecutionBlocked("FINAL_RECONCILIATION_LINEAGE_MISSING")
    observed = (
        _attempt_from_json(latest.observed_payload) if latest.observed_payload is not None else None
    )
    if observed != current:
        raise ExecutionBlocked("FINAL_OBSERVATION_NOT_CURRENT")
    if current.state not in FINALIZABLE_BROKER_ORDER_STATES:
        raise ExecutionBlocked("FINAL_ATTEMPT_NOT_TERMINAL")
    expected_positions = _positions_after_fill(intent, state, current)
    expected_cash = state.expected_cash + (current.fill_cash_flow or Decimal(0))
    request_hash = _authority_hash(
        {
            "domain": "alphadecay.final-reconciliation.v1",
            "intent_digest": intent.intent_digest,
            "attempt_ordinal": current.ordinal,
            "last_observation_hash": latest.observation_hash,
        }
    )
    known_activities, resolved_activity_hashes = _bound_attempt_activities(
        intent,
        state,
        current,
        activity_candidates,
        require_visible=current.filled_quantity > 0,
    )
    return ReconciliationExpectation._from_repository_state(
        purpose=ReconciliationPurpose(latest_permit.mutation_kind),
        account_role=AccountRole(intent.account_role),
        account_fingerprint=state.account_fingerprint,
        expected_cash=expected_cash,
        baseline_captured_at=_utc(state.baseline_captured_at),
        expected_positions=expected_positions,
        expected_open_orders=tuple(
            _open_order_from_json(item) for item in state.expected_open_orders
        ),
        known_activities=known_activities,
        resolved_activity_hashes=resolved_activity_hashes,
        required_activity_window_start=_utc(state.baseline_captured_at),
        required_activity_complete_through=_utc(state.activity_complete_through),
        intent_id=intent.intent_id,
        intent_digest=intent.intent_digest,
        attempt_ordinal=current.ordinal,
        request_hash=request_hash,
    )


def _bound_attempt_activities(
    intent: ExecutionIntentRow,
    state: AccountReconciliationStateRow,
    attempt: OrderAttempt,
    candidates: tuple[ActivityItem, ...] | None,
    *,
    require_visible: bool,
) -> tuple[tuple[ActivityItem, ...], tuple[str, ...]]:
    known = tuple(_activity_from_json(item) for item in state.known_activities)
    resolved = tuple(state.resolved_activity_hashes)
    if candidates is None:
        return known, resolved
    known_by_hash = {item.activity_id_hash: item for item in known}
    candidate_by_hash = {item.activity_id_hash: item for item in candidates}
    window_start = _utc(state.baseline_captured_at)
    if any(
        candidate_by_hash.get(key) != value
        for key, value in known_by_hash.items()
        if not activity_predates_window(value, window_start)
    ):
        raise ExecutionBlocked("KNOWN_ACTIVITY_MISSING")
    # Regulatory fee postings are explainable cash adjustments, not attempt evidence.
    fees = tuple(
        item
        for item in candidates
        if item.activity_id_hash not in known_by_hash and item.activity_type is ActivityType.FEE
    )
    new = tuple(
        item
        for item in candidates
        if item.activity_id_hash not in known_by_hash and item.activity_type is not ActivityType.FEE
    )
    if not new:
        if require_visible:
            raise ExecutionBlocked("ATTEMPT_ACTIVITY_EVIDENCE_PENDING")
        if fees:
            return (*known, *fees), tuple(sorted({*resolved, *(f.activity_id_hash for f in fees)}))
        return known, resolved
    if (
        attempt.filled_quantity <= 0
        or attempt.provider_order_id is None
        or any(item.activity_type not in {ActivityType.FILL, ActivityType.OPTRD} for item in new)
        or any(item.provider_order_id != attempt.provider_order_id for item in new)
        or any(item.client_order_id != attempt.client_order_id for item in new)
    ):
        raise ExecutionBlocked("ATTEMPT_ACTIVITY_EVIDENCE_MISMATCH")
    envelope = _envelope_from_json(intent.envelope_payload)
    expected_quantities = {
        leg.symbol: Decimal(
            attempt.filled_quantity
            * leg.ratio
            * (1 if leg.intent in {PositionIntent.BUY_TO_OPEN, PositionIntent.BUY_TO_CLOSE} else -1)
        )
        for leg in envelope.legs
    }
    if (
        len(new) != len(expected_quantities)
        or len({item.symbol for item in new}) != len(new)
        or any(
            item.symbol not in expected_quantities
            or item.signed_quantity != expected_quantities[item.symbol]
            for item in new
        )
    ):
        raise ExecutionBlocked("ATTEMPT_ACTIVITY_EVIDENCE_MISMATCH")
    # Known activities that predate the sweep window (the funding journal) stay known in
    # every successor state; they cannot be re-observed and must not be forgotten.
    predating = tuple(
        item for item in known if activity_predates_window(item, _utc(state.baseline_captured_at))
    )
    combined = {item.activity_id_hash: item for item in (*predating, *candidates)}
    return tuple(sorted(combined.values(), key=lambda item: item.activity_id_hash)), tuple(
        sorted({*resolved, *(item.activity_id_hash for item in (*new, *fees))})
    )


def _positions_after_fill(
    intent: ExecutionIntentRow,
    state: AccountReconciliationStateRow,
    attempt: OrderAttempt,
) -> tuple[InventoryItem, ...]:
    positions = {
        item.symbol: item
        for item in (_inventory_from_json(value) for value in state.expected_positions)
    }
    if attempt.filled_quantity == 0:
        return tuple(sorted(positions.values(), key=lambda item: (item.kind.value, item.symbol)))
    if attempt.fill_cash_flow is None:
        raise ExecutionBlocked("ATTEMPT_CASH_FLOW_REQUIRED")
    envelope = _envelope_from_json(intent.envelope_payload)
    for leg in envelope.legs:
        signed_change = Decimal(
            attempt.filled_quantity
            * leg.ratio
            * (1 if leg.intent in {PositionIntent.BUY_TO_OPEN, PositionIntent.BUY_TO_CLOSE} else -1)
        )
        current = positions.get(leg.symbol)
        current_quantity = current.signed_quantity if current is not None else Decimal(0)
        if leg.intent == PositionIntent.BUY_TO_CLOSE and current_quantity >= 0:
            raise ExecutionBlocked("CLOSE_POSITION_MISMATCH")
        if leg.intent == PositionIntent.SELL_TO_CLOSE and current_quantity <= 0:
            raise ExecutionBlocked("CLOSE_POSITION_MISMATCH")
        next_quantity = current_quantity + signed_change
        if leg.intent in {PositionIntent.BUY_TO_CLOSE, PositionIntent.SELL_TO_CLOSE} and abs(
            next_quantity
        ) > abs(current_quantity):
            raise ExecutionBlocked("CLOSE_POSITION_MISMATCH")
        if next_quantity == 0:
            positions.pop(leg.symbol, None)
        else:
            positions[leg.symbol] = InventoryItem(
                InventoryKind.OPTION,
                leg.symbol,
                next_quantity,
                100,
            )
    return tuple(sorted(positions.values(), key=lambda item: (item.kind.value, item.symbol)))


def _apply_budget_finalization(
    intent: ExecutionIntentRow,
    budget: CompetitionEntryBudgetRow,
    account: AccountRoleRow,
    has_fill: bool,
    limits: EntryBudgetLimits | None,
) -> None:
    if intent.action == ExecutionAction.ENTRY.value:
        if budget.reserved_intent_id != intent.intent_id:
            raise ExecutionBlocked("ENTRY_RESERVATION_MISSING")
        if budget.reserved_risk != intent.approved_max_loss:
            raise ExecutionBlocked("ENTRY_RESERVATION_RISK_MISMATCH")
        if has_fill:
            _validate_entry_limits(
                limits,
                intent=intent,
                account=account,
                budget=budget,
            )
            budget.entries_used += 1
            budget.gross_approved_risk += budget.reserved_risk
            intent.first_fill_consumed = True
        budget.reserved_intent_id = None
        budget.reserved_risk = Decimal(0)
    elif has_fill:
        intent.first_fill_consumed = True


def _validate_entry_limits(
    limits: EntryBudgetLimits | None,
    *,
    intent: ExecutionIntent | ExecutionIntentRow,
    account: AccountRoleRow,
    budget: CompetitionEntryBudgetRow,
) -> None:
    if limits is None:
        raise ExecutionBlocked("ENTRY_LIMITS_REQUIRED")
    envelope = intent.envelope if isinstance(intent, ExecutionIntent) else None
    policy_hash = envelope.policy_hash if envelope is not None else intent.policy_hash
    proposed_risk = envelope.approved_max_loss if envelope is not None else intent.approved_max_loss
    proposed_quantity = envelope.quantity if envelope is not None else intent.quantity
    try:
        limits.validate_entry(
            policy_hash=policy_hash,
            equity=account.equity,
            entries_used=budget.entries_used,
            lifetime_risk=budget.gross_approved_risk,
            proposed_risk=proposed_risk,
            proposed_quantity=proposed_quantity,
        )
    except ValueError as error:
        raise ExecutionBlocked(str(error)) from error


def _execution_certificate_row(
    value: ExecutionCertificate,
) -> ExecutionCertificateRow:
    return ExecutionCertificateRow(
        certificate_id=value.certificate_id,
        execution_intent_id=value.intent_id,
        entry_approval_id=value.entry_approval_id,
        assessment_certificate_id=value.assessment_certificate_id,
        execution_status=value.execution_status,
        attempt_ids=list(value.attempt_ids),
        actual_exposure=_exposure_to_json(value.actual_exposure),
        reconciliation_checks=list(value.reconciliation_checks),
        created_at=value.created_at,
        reconciliation_id=value.reconciliation_id,
        reconciliation_hash=value.reconciliation_hash,
        last_observation_hash=value.last_observation_hash,
    )


def _reconciliation_row(value: WholeAccountReconciliation) -> WholeAccountReconciliationRow:
    return WholeAccountReconciliationRow(
        reconciliation_id=value.reconciliation_id,
        reconciliation_hash=value.reconciliation_hash,
        expectation_hash=value.expectation.expectation_hash,
        execution_intent_id=value.expectation.intent_id,
        intent_digest=value.expectation.intent_digest,
        account_role=value.expectation.account_role.value,
        account_fingerprint=value.expectation.account_fingerprint,
        purpose=value.expectation.purpose.value,
        attempt_ordinal=value.expectation.attempt_ordinal,
        request_hash=value.expectation.request_hash,
        accepted_at=value.accepted_at,
        expectation_payload=_expectation_to_json(value.expectation),
        sweep_payload=_sweep_to_json(value.sweep),
        positions_manifest_hash=value.positions_manifest_hash,
        orders_manifest_hash=value.orders_manifest_hash,
        activities_manifest_hash=value.activities_manifest_hash,
        safe=value.safe,
        block_codes=[code.value for code in value.block_codes],
    )


def _reconciliation_from_row(
    row: WholeAccountReconciliationRow,
) -> WholeAccountReconciliation:
    expectation = _expectation_from_json(row.expectation_payload)
    sweep = _sweep_from_json(row.sweep_payload)
    value = WholeAccountReconciliation.evaluate(
        sweep,
        expectation,
        accepted_at=_utc(row.accepted_at),
    )
    if (
        value.reconciliation_id != row.reconciliation_id
        or value.reconciliation_hash != row.reconciliation_hash
        or value.positions_manifest_hash != row.positions_manifest_hash
        or value.orders_manifest_hash != row.orders_manifest_hash
        or value.activities_manifest_hash != row.activities_manifest_hash
        or value.safe != row.safe
        or tuple(code.value for code in value.block_codes) != tuple(row.block_codes)
    ):
        raise ExecutionBlocked("WHOLE_ACCOUNT_RECONCILIATION_CORRUPT")
    return value


def _permit_row(value: BrokerMutationPermit) -> BrokerMutationPermitRow:
    return BrokerMutationPermitRow(
        permit_id=value.permit_id,
        reconciliation_id=value.reconciliation_id,
        execution_intent_id=value.intent_id,
        intent_digest=value.intent_digest,
        claim_token=value.claim_token,
        claim_generation=value.claim_generation,
        execution_epoch=value.execution_epoch,
        mutation_kind=value.mutation_kind.value,
        attempt_ordinal=value.attempt_ordinal,
        permit_generation=value.generation,
        predecessor_permit_id=value.predecessor_permit_id,
        request_hash=value.request_hash,
        target_client_order_id=value.target_client_order_id,
        target_provider_order_id=value.target_provider_order_id,
        issued_at=value.issued_at,
        expires_at=value.expires_at,
        state=value.state,
        dispatch_nonce=value.dispatch_nonce,
        dispatch_acquired_at=value.dispatch_acquired_at,
        consumed_at=value.consumed_at,
        outcome_hash=value.outcome_hash,
        limit_price=value.limit_price,
        quote_hash=value.quote_hash,
        quote_source_timestamps=[item.isoformat() for item in value.quote_source_timestamps],
        quote_retrieved_at=value.quote_retrieved_at,
        timing_authority_at=value.timing_authority_at,
        prior_request_hash=value.prior_request_hash,
    )


def _permit_from_row(row: BrokerMutationPermitRow) -> BrokerMutationPermit:
    return BrokerMutationPermit(
        permit_id=row.permit_id,
        reconciliation_id=row.reconciliation_id,
        intent_id=row.execution_intent_id,
        intent_digest=row.intent_digest,
        claim_token=row.claim_token,
        claim_generation=row.claim_generation,
        execution_epoch=row.execution_epoch,
        mutation_kind=ReconciliationPurpose(row.mutation_kind),
        attempt_ordinal=row.attempt_ordinal,
        generation=row.permit_generation,
        predecessor_permit_id=row.predecessor_permit_id,
        request_hash=row.request_hash,
        target_client_order_id=row.target_client_order_id,
        target_provider_order_id=row.target_provider_order_id,
        issued_at=_utc(row.issued_at),
        expires_at=_utc(row.expires_at),
        state=row.state,
        dispatch_nonce=row.dispatch_nonce,
        dispatch_acquired_at=(_utc(row.dispatch_acquired_at) if row.dispatch_acquired_at else None),
        consumed_at=_utc(row.consumed_at) if row.consumed_at else None,
        outcome_hash=row.outcome_hash,
        limit_price=row.limit_price,
        quote_hash=row.quote_hash,
        quote_source_timestamps=tuple(
            datetime.fromisoformat(value) for value in row.quote_source_timestamps
        ),
        quote_retrieved_at=_utc(row.quote_retrieved_at) if row.quote_retrieved_at else None,
        timing_authority_at=_utc(row.timing_authority_at) if row.timing_authority_at else None,
        prior_request_hash=row.prior_request_hash,
    )


def _exposure_from_json(value: dict[str, object] | None) -> GreekExposure | None:
    if value is None:
        return None
    return GreekExposure(
        delta=Decimal(str(value["delta"])),
        gamma=Decimal(str(value["gamma"])),
        theta_per_day=Decimal(str(value["theta_per_day"])),
        vega_per_iv_point=Decimal(str(value["vega_per_iv_point"])),
    )


def _assessment_from_row(row: AssessmentCertificateRow) -> AssessmentCertificate:
    return AssessmentCertificate(
        certificate_id=row.certificate_id,
        assessment_id=row.assessment_id,
        account_role=AccountRole(row.account_role),
        action=ExecutionAction(row.action),
        position_fingerprint=row.position_fingerprint,
        envelope_hash=row.envelope_hash,
        approved_max_loss=row.approved_max_loss,
        quantity=row.quantity,
        expected_after_exposure=_exposure_from_json(row.expected_after_exposure),
        policy_hash=row.policy_hash,
        created_at=_utc(row.created_at),
        expires_at=_utc(row.expires_at),
        valid=row.valid,
        thesis_version_id=row.thesis_version_id,
        experiment_lineage=optional_experiment_execution_lineage(
            row.experiment_id,
            row.experiment_source_definition_hash,
            row.experiment_protocol_hash,
        ),
    )


def _thesis_from_row(row: ThesisVersionRow) -> FrozenThesisVersion:
    return FrozenThesisVersion(
        thesis_version_id=row.thesis_version_id,
        thesis_id=row.thesis_id,
        account_role=AccountRole(row.account_role),
        version=row.version,
        origin_hash=row.origin_hash,
        thesis_hash=row.thesis_hash,
        policy_hash=row.policy_hash,
        underlying=row.underlying,
        thesis_code=row.thesis_code,
        frozen_at=_utc(row.frozen_at),
        target_at=_utc(row.target_at),
        intended_exposure=row.intended_exposure,
        exposure_limits=row.exposure_limits,
        volatility_view=row.volatility_view,
        entry_atm_iv=row.entry_atm_iv,
        approved_max_loss=row.approved_max_loss,
        portfolio_risk_cap=row.portfolio_risk_cap,
        invalidation_codes=tuple(row.invalidation_codes),
        thesis_payload=row.thesis_payload,
        created_at=_utc(row.created_at),
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _trusted_db_now(session: Session) -> datetime:
    if session.bind.dialect.name == "postgresql":
        clock = func.clock_timestamp()
    else:
        value = session.scalar(select(func.strftime("%Y-%m-%dT%H:%M:%f", "now")))
        if not isinstance(value, str):
            raise ExecutionBlocked("TRUSTED_TIME_UNAVAILABLE")
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    value = session.scalar(select(clock))
    if value is None:
        raise ExecutionBlocked("TRUSTED_TIME_UNAVAILABLE")
    return _utc(value)


def _ensure_executable_role(role: AccountRole) -> None:
    if role == AccountRole.REPLAY:
        raise ExecutionBlocked("REPLAY_EXECUTION_FORBIDDEN")


def _verify_permit_fence(permit: BrokerMutationPermitRow, intent: ExecutionIntentRow) -> None:
    if (
        permit.execution_intent_id != intent.intent_id
        or permit.intent_digest != intent.intent_digest
        or permit.claim_token != intent.claim_token
        or permit.claim_generation != intent.claim_generation
        or permit.execution_epoch != intent.execution_epoch
    ):
        raise ExecutionBlocked("BROKER_PERMIT_FENCE_MISMATCH")


def _verify_permit_material(
    permit: BrokerMutationPermitRow,
    attempt: OrderAttemptRow,
    intent: ExecutionIntentRow,
) -> None:
    if (
        permit.limit_price
        != (attempt.limit_price if attempt.limit_price is not None else intent.minimum_limit)
        or permit.quote_hash != attempt.quote_hash
        or tuple(permit.quote_source_timestamps) != tuple(attempt.quote_source_timestamps)
        or permit.quote_retrieved_at != attempt.quote_retrieved_at
        or permit.timing_authority_at != attempt.timing_authority_at
        or permit.prior_request_hash != attempt.prior_request_hash
    ):
        raise ExecutionBlocked("BROKER_PERMIT_MATERIAL_MISMATCH")


def _validate_replacement_dispatch_authority(
    attempt: OrderAttemptRow,
    initial_permit: BrokerMutationPermitRow,
    trusted_now: datetime,
) -> None:
    if initial_permit.dispatch_acquired_at is None:
        raise ExecutionBlocked("INITIAL_DISPATCH_TIME_MISSING")
    if trusted_now - _utc(initial_permit.dispatch_acquired_at) >= timedelta(seconds=600):
        raise ExecutionBlocked("REPLACEMENT_CANCEL_DUE")
    timestamps = tuple(
        _utc(datetime.fromisoformat(str(value))) for value in attempt.quote_source_timestamps
    )
    if (
        not timestamps
        or attempt.quote_retrieved_at is None
        or attempt.timing_authority_at is None
        or _utc(attempt.quote_retrieved_at) > trusted_now
        or trusted_now - _utc(attempt.quote_retrieved_at) > timedelta(seconds=30)
        or _utc(attempt.timing_authority_at) > trusted_now
        or trusted_now - _utc(attempt.timing_authority_at) > timedelta(seconds=30)
        or any(
            timestamp > trusted_now or trusted_now - timestamp > timedelta(seconds=30)
            for timestamp in timestamps
        )
    ):
        raise ExecutionBlocked("REPLACEMENT_QUOTE_STALE")


def _authority_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()
