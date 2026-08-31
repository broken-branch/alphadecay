from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import RLock
from uuid import UUID, uuid4

from backend.app.contracts.v1 import AccountRole, SubmissionBaseline
from backend.app.execution.identity import intent_digest
from backend.app.execution.models import (
    AccountExecutionLock,
    Actor,
    AssessmentCertificate,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionBlocked,
    ExecutionCertificate,
    ExecutionIntent,
    IntentState,
    OrderAttempt,
    OrderEnvelope,
    Reconciliation,
)
from backend.app.order_limits import EntryBudgetLimits

from .attempt_observation import validate_attempt_observation
from .authorization import AuthorizationValues, validate_authorization
from .finalization import execution_lock_reason, validate_finalization


@dataclass(frozen=True)
class AccountState:
    role: AccountRole
    fingerprint: str
    equity: Decimal
    autonomous_enabled: bool
    execution_locked: bool = False
    execution_lock_reason: str | None = None
    execution_locked_at: datetime | None = None
    execution_epoch: int = 0
    claim_generation: int = 0


@dataclass(frozen=True)
class EntryBudget:
    entries_used: int = 0
    gross_approved_risk: Decimal = Decimal(0)
    reserved_intent_id: UUID | None = None
    reserved_risk: Decimal = Decimal(0)


class InMemoryExecutionRepository:
    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        *,
        entry_limits: EntryBudgetLimits | None = None,
    ) -> None:
        self._lock = RLock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entry_limits = entry_limits
        self._accounts: dict[AccountRole, AccountState] = {}
        self._baselines: dict[AccountRole, SubmissionBaseline] = {}
        self._contaminated: set[AccountRole] = set()
        self._budgets: dict[AccountRole, EntryBudget] = {}
        self._event_days: set[tuple[AccountRole, str, date]] = set()
        self._intents: dict[UUID, ExecutionIntent] = {}
        self._digest_ids: dict[str, UUID] = {}
        self._attempts: dict[UUID, list[OrderAttempt]] = {}
        self._entry_approvals: dict[UUID, EntryApprovalAuthorization] = {}
        self._assessment_certificates: dict[UUID, AssessmentCertificate] = {}
        self._assessment_ids: dict[UUID, UUID] = {}
        self._certificates: dict[UUID, ExecutionCertificate] = {}
        self._intent_certificate_ids: dict[UUID, UUID] = {}

    def register_account(
        self,
        *,
        role: AccountRole,
        fingerprint: str,
        equity: Decimal,
        autonomous_enabled: bool,
    ) -> None:
        _ensure_executable_role(role)
        with self._lock:
            current = self._accounts.get(role)
            if current and current.fingerprint != fingerprint:
                raise ExecutionBlocked("ACCOUNT_FINGERPRINT_MISMATCH")
            if any(
                registered_role is not role and account.fingerprint == fingerprint
                for registered_role, account in self._accounts.items()
            ):
                raise ExecutionBlocked("ACCOUNT_FINGERPRINT_ALREADY_REGISTERED")
            self._accounts[role] = (
                replace(current, equity=equity)
                if current
                else AccountState(role, fingerprint, equity, autonomous_enabled=False)
            )
            self._budgets.setdefault(role, EntryBudget())

    def set_autonomous_enabled(self, role: AccountRole, enabled: bool, *, actor: Actor) -> None:
        _ensure_executable_role(role)
        if actor != Actor.OWNER:
            raise ExecutionBlocked("SCHEDULER_CANNOT_ENABLE_AUTONOMY")
        with self._lock:
            account = self._accounts[role]
            self._accounts[role] = replace(account, autonomous_enabled=enabled)

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
        with self._lock:
            if role in self._baselines:
                raise ExecutionBlocked("BASELINE_ALREADY_CAPTURED")
            account = self._accounts.get(role)
            if (
                role != AccountRole.SUBMISSION
                or account is None
                or account.fingerprint != fingerprint
            ):
                raise ExecutionBlocked("BASELINE_ACCOUNT_MISMATCH")
            if equity != Decimal("100000"):
                raise ExecutionBlocked("BASELINE_EQUITY_INVALID")
            baseline = SubmissionBaseline(
                role=AccountRole.SUBMISSION,
                equity=equity,
                captured_at=captured_at,
                account_fingerprint=fingerprint,
                positions_hash=positions_hash,
                orders_hash=orders_hash,
                activities_hash=activities_hash,
                clean=True,
            )
            self._baselines[role] = baseline
            return baseline

    def observe_account_adjustment(self, role: AccountRole, activity: str) -> None:
        _ensure_executable_role(role)
        if activity in {"RESET", "DEPOSIT", "WITHDRAWAL", "TRANSFER", "JOURNAL", "UNKNOWN_CASH"}:
            with self._lock:
                self._contaminated.add(role)

    def normalized_return(self, role: AccountRole, current_equity: Decimal) -> Decimal | None:
        _ensure_executable_role(role)
        with self._lock:
            baseline = self._baselines.get(role)
            if baseline is None or role in self._contaminated:
                return None
            return (current_equity - baseline.equity) / baseline.equity * 100

    def approve_intent(
        self, intent_id: UUID, role: AccountRole, envelope: OrderEnvelope
    ) -> ExecutionIntent:
        _ensure_executable_role(role)
        digest = intent_digest(envelope)
        with self._lock:
            existing_id = self._digest_ids.get(digest)
            if existing_id is not None:
                existing = self._intents[existing_id]
                if existing.account_role != role or existing.envelope != envelope:
                    raise ExecutionBlocked("INTENT_DIGEST_COLLISION")
                return existing
            if intent_id in self._intents:
                raise ExecutionBlocked("INTENT_ID_ALREADY_USED")
            event_day = (role, envelope.event_key, envelope.trading_day)
            if envelope.action == ExecutionAction.ENTRY:
                if event_day in self._event_days:
                    raise ExecutionBlocked("EVENT_DAY_ALREADY_USED")
                if any(
                    intent.account_role == role
                    and intent.envelope.action == ExecutionAction.ENTRY
                    and intent.state != IntentState.TERMINAL
                    for intent in self._intents.values()
                ):
                    raise ExecutionBlocked("ENTRY_INTENT_ACTIVE")
            account = self._accounts.get(role)
            if account is None or account.fingerprint != envelope.account_fingerprint:
                raise ExecutionBlocked("ACCOUNT_FINGERPRINT_MISMATCH")
            intent = ExecutionIntent(intent_id, role, envelope, digest, IntentState.APPROVED)
            self._intents[intent_id] = intent
            self._digest_ids[digest] = intent_id
            if envelope.action == ExecutionAction.ENTRY:
                self._event_days.add(event_day)
            self._attempts[intent_id] = []
            return intent

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
        with self._lock:
            trusted_now = self._clock()
            intent = self._intents[intent_id]
            if intent.account_role is not account_role:
                raise ExecutionBlocked("ACCOUNT_ROLE_MISMATCH")
            _ensure_executable_role(intent.account_role)
            account = self._accounts[intent.account_role]
            if account.fingerprint != account_fingerprint:
                raise ExecutionBlocked("ACCOUNT_FINGERPRINT_MISMATCH")
            if account.execution_locked:
                raise ExecutionBlocked("ACCOUNT_EXECUTION_LOCKED")
            if intent.state != IntentState.APPROVED:
                raise ExecutionBlocked("INTENT_ALREADY_CLAIMED")
            if any(
                other.account_role == intent.account_role and other.state == IntentState.CLAIMED
                for other in self._intents.values()
            ):
                raise ExecutionBlocked("ACCOUNT_EXECUTION_LEASE_ACTIVE")
            self._require_authorization_origin(intent, trusted_now)
            if intent.envelope.action == ExecutionAction.ENTRY:
                if actor == Actor.OWNER:
                    raise ExecutionBlocked("OWNER_ENTRY_FORBIDDEN")
                if not account.autonomous_enabled:
                    raise ExecutionBlocked("AUTONOMOUS_DISABLED")
                self._reserve_entry(intent, account)
            elif actor == Actor.SCHEDULER and not account.autonomous_enabled:
                raise ExecutionBlocked("AUTONOMOUS_DISABLED")
            claimed = replace(
                intent,
                state=IntentState.CLAIMED,
                claimed_by=actor,
                claimed_at=trusted_now,
                claim_token=uuid4(),
                claim_generation=account.claim_generation + 1,
                execution_epoch=account.execution_epoch,
                heartbeat_at=trusted_now,
                lease_expires_at=trusted_now + timedelta(seconds=30),
            )
            self._accounts[intent.account_role] = replace(
                account, claim_generation=account.claim_generation + 1
            )
            self._intents[intent_id] = claimed
            return claimed

    def _require_authorization_origin(self, intent: ExecutionIntent, now: datetime) -> None:
        authorization_id = intent.envelope.authorization_certificate_id
        if intent.envelope.action == ExecutionAction.ENTRY:
            if authorization_id in self._assessment_certificates:
                raise ExecutionBlocked("AUTHORIZATION_ACTION_MISMATCH")
            approval = self._entry_approvals.get(authorization_id)
            if approval is None:
                raise ExecutionBlocked("AUTHORIZATION_ORIGIN_NOT_FOUND")
            validate_authorization(
                intent,
                AuthorizationValues(
                    approval.account_role,
                    approval.policy_hash,
                    approval.book_fingerprint,
                    approval.envelope_hash,
                    approval.approved_max_loss,
                    approval.quantity,
                    approval.valid,
                    approval.valid_from,
                    approval.expires_at,
                ),
                now,
            )
            return
        if authorization_id in self._entry_approvals:
            raise ExecutionBlocked("AUTHORIZATION_ACTION_MISMATCH")
        certificate = self._assessment_certificates.get(authorization_id)
        if certificate is None:
            raise ExecutionBlocked("AUTHORIZATION_ORIGIN_NOT_FOUND")
        if certificate.action != intent.envelope.action:
            raise ExecutionBlocked("AUTHORIZATION_ACTION_MISMATCH")
        validate_authorization(
            intent,
            AuthorizationValues(
                certificate.account_role,
                certificate.policy_hash,
                certificate.position_fingerprint,
                certificate.envelope_hash,
                certificate.approved_max_loss,
                certificate.quantity,
                certificate.valid,
                certificate.created_at,
                certificate.expires_at,
            ),
            now,
        )

    def _reserve_entry(self, intent: ExecutionIntent, account: AccountState) -> None:
        budget = self._budgets[intent.account_role]
        if intent.account_role == AccountRole.SUBMISSION:
            if intent.account_role not in self._baselines:
                raise ExecutionBlocked("SUBMISSION_BASELINE_REQUIRED")
            if intent.account_role in self._contaminated:
                raise ExecutionBlocked("SUBMISSION_BASELINE_CONTAMINATED")
        if budget.reserved_intent_id is not None:
            raise ExecutionBlocked("ENTRY_RESERVATION_ACTIVE")
        _validate_entry_limits(
            self._entry_limits,
            policy_hash=intent.envelope.policy_hash,
            equity=account.equity,
            budget=budget,
            proposed_risk=intent.envelope.approved_max_loss,
            proposed_quantity=intent.envelope.quantity,
        )
        self._budgets[intent.account_role] = replace(
            budget,
            reserved_intent_id=intent.intent_id,
            reserved_risk=intent.envelope.approved_max_loss,
        )

    def release_unsubmitted_claim(self, intent_id: UUID) -> None:
        with self._lock:
            intent = self._intents[intent_id]
            if intent.state != IntentState.CLAIMED or self._attempts[intent_id]:
                raise ExecutionBlocked("CLAIM_NOT_UNSUBMITTED")
            budget = self._budgets[intent.account_role]
            if budget.reserved_intent_id == intent_id:
                self._budgets[intent.account_role] = replace(
                    budget, reserved_intent_id=None, reserved_risk=Decimal(0)
                )
            self._intents[intent_id] = replace(intent, state=IntentState.TERMINAL)

    def add_attempt(self, attempt: OrderAttempt) -> None:
        with self._lock:
            intent = self._intents[attempt.intent_id]
            _ensure_executable_role(intent.account_role)
            if self._accounts[intent.account_role].execution_locked:
                raise ExecutionBlocked("ACCOUNT_EXECUTION_LOCKED")
            if intent.state != IntentState.CLAIMED:
                raise ExecutionBlocked("INTENT_NOT_CLAIMED")
            attempts = self._attempts[attempt.intent_id]
            if attempt.ordinal != len(attempts) or attempt.ordinal > 3:
                raise ExecutionBlocked("ATTEMPT_ORDINAL_INVALID")
            expected_replaced_id = attempts[-1].client_order_id if attempts else None
            if attempt.replaces_client_order_id != expected_replaced_id:
                raise ExecutionBlocked("ATTEMPT_REPLACEMENT_LINEAGE_INVALID")
            attempts.append(attempt)

    def replace_attempt(self, attempt: OrderAttempt) -> None:
        with self._lock:
            intent = self._intents[attempt.intent_id]
            _ensure_executable_role(intent.account_role)
            if self._accounts[intent.account_role].execution_locked:
                raise ExecutionBlocked("ACCOUNT_EXECUTION_LOCKED")
            if intent.state != IntentState.CLAIMED:
                raise ExecutionBlocked("INTENT_NOT_CLAIMED")
            attempts = self._attempts[attempt.intent_id]
            if not attempts or attempts[-1].ordinal != attempt.ordinal:
                raise ExecutionBlocked("ATTEMPT_NOT_FOUND")
            existing = attempts[-1]
            if (
                attempt.client_order_id != existing.client_order_id
                or attempt.request_hash != existing.request_hash
                or attempt.replaces_client_order_id != existing.replaces_client_order_id
                or attempt.intent_id != existing.intent_id
            ):
                raise ExecutionBlocked("ATTEMPT_IMMUTABLE_FIELDS_MISMATCH")
            validate_attempt_observation(existing, attempt)
            attempts[-1] = attempt

    def attempts_for(self, intent_id: UUID) -> tuple[OrderAttempt, ...]:
        with self._lock:
            return tuple(self._attempts[intent_id])

    def execution_attempts_for(self, intent_id: UUID) -> tuple[OrderAttempt, ...]:
        return self.attempts_for(intent_id)

    def finalize_execution(
        self,
        certificate: ExecutionCertificate,
        reconciliation: Reconciliation,
        requested_status: str,
    ) -> ExecutionCertificate:
        with self._lock:
            if certificate.certificate_id in self._certificates:
                raise ExecutionBlocked("CERTIFICATE_IMMUTABLE")
            if certificate.intent_id in self._intent_certificate_ids:
                raise ExecutionBlocked("INTENT_ALREADY_CERTIFIED")
            intent = self._intents[certificate.intent_id]
            _ensure_executable_role(intent.account_role)
            trusted_now = self._clock()
            normalized, has_fill = validate_finalization(
                intent,
                tuple(self._attempts[intent.intent_id]),
                certificate,
                reconciliation,
                requested_status,
                trusted_now,
            )
            lock_reason = execution_lock_reason(reconciliation)
            if has_fill and requested_status.startswith("PARTIAL_"):
                lock_reason = "UNMANAGED_PARTIAL_EXPOSURE"
            account = self._accounts[intent.account_role]
            locked_account = account
            if lock_reason is not None and not account.execution_locked:
                locked_account = replace(
                    account,
                    execution_locked=True,
                    execution_lock_reason=lock_reason,
                    execution_locked_at=trusted_now,
                )
            budget = self._budgets[intent.account_role]
            consumed = intent.first_fill_consumed
            if intent.envelope.action == ExecutionAction.ENTRY:
                if budget.reserved_intent_id != intent.intent_id:
                    raise ExecutionBlocked("ENTRY_RESERVATION_MISSING")
                if budget.reserved_risk != intent.envelope.approved_max_loss:
                    raise ExecutionBlocked("ENTRY_RESERVATION_RISK_MISMATCH")
                if has_fill:
                    _validate_entry_limits(
                        self._entry_limits,
                        policy_hash=intent.envelope.policy_hash,
                        equity=account.equity,
                        budget=budget,
                        proposed_risk=budget.reserved_risk,
                        proposed_quantity=intent.envelope.quantity,
                    )
                    budget = EntryBudget(
                        entries_used=budget.entries_used + 1,
                        gross_approved_risk=budget.gross_approved_risk + budget.reserved_risk,
                    )
                    consumed = True
                else:
                    budget = replace(budget, reserved_intent_id=None, reserved_risk=Decimal(0))
                self._budgets[intent.account_role] = budget
            elif has_fill:
                consumed = True
            self._accounts[intent.account_role] = locked_account
            self._certificates[normalized.certificate_id] = normalized
            self._intent_certificate_ids[certificate.intent_id] = certificate.certificate_id
            self._intents[certificate.intent_id] = replace(
                intent, state=IntentState.TERMINAL, first_fill_consumed=consumed
            )
            return normalized

    def get_execution_certificate(self, certificate_id: UUID) -> ExecutionCertificate:
        with self._lock:
            return self._certificates[certificate_id]

    def add_assessment_certificate(self, certificate: AssessmentCertificate) -> None:
        _ensure_executable_role(certificate.account_role)
        with self._lock:
            if certificate.certificate_id in self._assessment_certificates:
                raise ExecutionBlocked("CERTIFICATE_IMMUTABLE")
            if certificate.certificate_id in self._entry_approvals:
                raise ExecutionBlocked("AUTHORIZATION_ID_COLLISION")
            if certificate.assessment_id in self._assessment_ids:
                raise ExecutionBlocked("ASSESSMENT_ALREADY_CERTIFIED")
            self._assessment_certificates[certificate.certificate_id] = certificate
            self._assessment_ids[certificate.assessment_id] = certificate.certificate_id

    def add_entry_approval(self, approval: EntryApprovalAuthorization) -> None:
        _ensure_executable_role(approval.account_role)
        with self._lock:
            if approval.approval_id in self._entry_approvals:
                raise ExecutionBlocked("AUTHORIZATION_IMMUTABLE")
            if approval.approval_id in self._assessment_certificates:
                raise ExecutionBlocked("AUTHORIZATION_ID_COLLISION")
            self._entry_approvals[approval.approval_id] = approval

    def get_assessment_certificate(self, certificate_id: UUID) -> AssessmentCertificate:
        with self._lock:
            return self._assessment_certificates[certificate_id]

    def recover_entry_reservations(self) -> None:
        with self._lock:
            for intent in self._intents.values():
                if (
                    intent.envelope.action == ExecutionAction.ENTRY
                    and intent.state == IntentState.CLAIMED
                    and not intent.first_fill_consumed
                ):
                    budget = self._budgets[intent.account_role]
                    self._budgets[intent.account_role] = replace(
                        budget,
                        reserved_intent_id=intent.intent_id,
                        reserved_risk=intent.envelope.approved_max_loss,
                    )

    def get_entry_budget(self, role: AccountRole) -> EntryBudget:
        _ensure_executable_role(role)
        with self._lock:
            return self._budgets[role]

    def get_execution_lock(self, role: AccountRole) -> AccountExecutionLock:
        _ensure_executable_role(role)
        with self._lock:
            account = self._accounts[role]
            return AccountExecutionLock(
                locked=account.execution_locked,
                reason=account.execution_lock_reason,
                locked_at=account.execution_locked_at,
            )

    def get_intent(self, intent_id: UUID) -> ExecutionIntent:
        with self._lock:
            intent = self._intents[intent_id]
            _ensure_executable_role(intent.account_role)
            return intent


def _validate_entry_limits(
    limits: EntryBudgetLimits | None,
    *,
    policy_hash: str,
    equity: Decimal,
    budget: EntryBudget,
    proposed_risk: Decimal,
    proposed_quantity: int,
) -> None:
    if limits is None:
        raise ExecutionBlocked("ENTRY_LIMITS_REQUIRED")
    try:
        limits.validate_entry(
            policy_hash=policy_hash,
            equity=equity,
            entries_used=budget.entries_used,
            lifetime_risk=budget.gross_approved_risk,
            proposed_risk=proposed_risk,
            proposed_quantity=proposed_quantity,
        )
    except ValueError as error:
        raise ExecutionBlocked(str(error)) from error


def _ensure_executable_role(role: AccountRole) -> None:
    if role == AccountRole.REPLAY:
        raise ExecutionBlocked("REPLAY_EXECUTION_FORBIDDEN")
