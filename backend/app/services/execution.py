from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.app.alpaca.market_data import NormalizedOptionSnapshot
from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.domain.option_contract_symbol import (
    NON_STANDARD_CONTRACT_UNSUPPORTED,
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)
from backend.app.execution import (
    Actor,
    AmbiguousBrokerResponse,
    AttemptObservation,
    AttemptObservationSource,
    BrokerMutationPermit,
    BrokerMutationPlan,
    BrokerMutationPreparation,
    BrokerMutationSchedule,
    BrokerResult,
    ExecutionAction,
    ExecutionBlocked,
    ExecutionCertificate,
    ExecutionPending,
    ExecutionPendingCode,
    OrderEnvelope,
    ReconciliationPurpose,
    SweepObservation,
    client_order_id,
    replacement_request_hash,
)
from backend.app.execution.models import (
    ExecutionIntent,
    OrderAttempt,
    PositionGreekObservation,
)
from backend.app.execution.order_status import (
    FINALIZABLE_BROKER_ORDER_STATES,
    MUTATION_ELIGIBLE_BROKER_ORDER_STATES,
    PENDING_BROKER_ORDER_STATES,
    TERMINAL_BROKER_ORDER_STATES,
)
from backend.app.execution.reconciliation import (
    ReconciliationExpectation,
    WholeAccountReconciliation,
)


class BrokerExecutionPort(Protocol):
    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult: ...
    def replace(self, provider_order_id: str, client_id: str, limit: Decimal) -> BrokerResult: ...
    def cancel(self, provider_order_id: str) -> BrokerResult: ...
    def lookup(self, client_id: str) -> BrokerResult | None: ...


@dataclass(frozen=True)
class WholeAccountEvidence:
    sweep: SweepObservation
    position_greeks: tuple[PositionGreekObservation, ...] = ()


def evaluate_broker_mutation_preflight(
    plan: BrokerMutationPlan,
    sweep: SweepObservation,
    *,
    accepted_at: datetime,
) -> WholeAccountReconciliation:
    return WholeAccountReconciliation.evaluate(
        sweep,
        plan.expectation,
        accepted_at=accepted_at,
    )


class WholeAccountSweepPort(Protocol):
    def collect(self, expectation: ReconciliationExpectation) -> WholeAccountEvidence: ...


class ReplacementQuotePort(Protocol):
    def collect(self, symbols: tuple[str, ...]) -> tuple[NormalizedOptionSnapshot, ...]: ...


@dataclass(frozen=True)
class ExecutionAdvance:
    intent_id: UUID
    status: str
    mutation: ReconciliationPurpose | None
    attempt: OrderAttempt | None = None
    certificate: ExecutionCertificate | None = None


class ExecutionAuthorityRepository(Protocol):
    def claim_intent(
        self,
        intent_id: UUID,
        actor: Actor,
        *,
        now: datetime,
        account_role: AccountRole,
        account_fingerprint: str,
    ) -> ExecutionIntent: ...

    def get_intent(self, intent_id: UUID) -> ExecutionIntent: ...

    def next_broker_mutation(self, claim: ExecutionIntent) -> BrokerMutationSchedule | None: ...

    def trusted_execution_time(self, claim: ExecutionIntent) -> datetime: ...

    def plan_broker_mutation(
        self,
        claim: ExecutionIntent,
        purpose: ReconciliationPurpose,
        replacement: OrderAttempt | None = None,
    ) -> BrokerMutationPlan: ...

    def prepare_broker_mutation(
        self,
        reconciliation: WholeAccountReconciliation,
        attempt: OrderAttempt,
        *,
        claim: ExecutionIntent,
    ) -> BrokerMutationPreparation: ...

    def acquire_broker_dispatch(
        self, permit_id: UUID, *, claim: ExecutionIntent
    ) -> BrokerMutationPermit: ...

    def record_attempt_observation(
        self,
        permit_id: UUID,
        observed: OrderAttempt,
        *,
        source: AttemptObservationSource,
        claim: ExecutionIntent,
        dispatch_nonce: UUID | None = None,
    ) -> AttemptObservation: ...

    def mark_broker_dispatch_ambiguous(
        self,
        permit_id: UUID,
        *,
        dispatch_nonce: UUID,
        claim: ExecutionIntent,
    ) -> BrokerMutationPermit: ...

    def record_attempt_absence(
        self,
        permit_id: UUID,
        *,
        source: AttemptObservationSource,
        claim: ExecutionIntent,
    ) -> AttemptObservation: ...

    def final_reconciliation_expectation(
        self, claim: ExecutionIntent
    ) -> ReconciliationExpectation: ...

    def attempts_for(self, intent_id: UUID) -> tuple[OrderAttempt, ...]: ...

    def execution_attempts_for(self, intent_id: UUID) -> tuple[OrderAttempt, ...]: ...

    def targeted_lookup_authority(
        self, claim: ExecutionIntent
    ) -> tuple[UUID, OrderAttempt] | None: ...

    def record_attempt_lookup_failure(
        self, permit_id: UUID, *, claim: ExecutionIntent
    ) -> AttemptObservation: ...

    def get_execution_certificate(self, certificate_id: UUID) -> ExecutionCertificate: ...

    def finalize_execution_authorized(
        self,
        certificate: ExecutionCertificate,
        reconciliation: WholeAccountReconciliation,
        requested_status: str,
        *,
        claim: ExecutionIntent,
        position_greeks: tuple[PositionGreekObservation, ...] = (),
    ) -> ExecutionCertificate | None: ...


class ExecutionService:
    _PENDING_STATES = PENDING_BROKER_ORDER_STATES | {"PREPARED"}

    def __init__(
        self,
        repository: ExecutionAuthorityRepository,
        broker: BrokerExecutionPort,
        preflight: WholeAccountSweepPort,
        quotes: ReplacementQuotePort | None = None,
        *,
        account_role: AccountRole,
        account_fingerprint: str,
    ) -> None:
        self._repository = repository
        self._broker = broker
        self._preflight = preflight
        self._account_role = account_role
        self._account_fingerprint = account_fingerprint
        self._quotes = quotes

    def advance(
        self,
        intent_id: UUID,
        actor: Actor,
        *,
        account_role: AccountRole,
        account_fingerprint: str,
    ) -> ExecutionAdvance:
        if (
            account_role is not self._account_role
            or account_fingerprint != self._account_fingerprint
        ):
            raise ExecutionBlocked("OBSERVED_ACCOUNT_AUTHORITY_MISMATCH")
        claim = self._claim(intent_id, actor, account_role, account_fingerprint)
        if claim.state.value == "TERMINAL":
            return self._completed_advance(claim, actor)
        self._require_claim_actor(claim, actor)
        _validate_standard_contract_envelope(claim.envelope)
        if self._repository.execution_attempts_for(claim.intent_id):
            lookup = self._lookup_transitional(claim)
            if lookup is not None and (
                lookup.attempt is None
                or lookup.attempt.state not in MUTATION_ELIGIBLE_BROKER_ORDER_STATES
            ):
                return lookup
        try:
            schedule = self._repository.next_broker_mutation(claim)
        except ExecutionBlocked as error:
            if str(error) != "ACCOUNT_EXECUTION_LOCKED":
                raise
            return self._waiting_or_finalized_advance(claim)
        if schedule is None:
            return self._waiting_or_finalized_advance(claim)
        if schedule.purpose == ReconciliationPurpose.SUBMIT:
            observed = self._submit_active(claim, schedule.timing_authority_at)
            return ExecutionAdvance(
                intent_id,
                "SUBMITTED",
                ReconciliationPurpose.SUBMIT,
                observed,
            )
        if schedule.purpose == ReconciliationPurpose.REPLACE:
            return self._replace_due(claim)
        if schedule.purpose == ReconciliationPurpose.CANCEL:
            return self._cancel_advance(claim, schedule.timing_authority_at)
        raise ExecutionBlocked("EXECUTION_ADVANCE_PURPOSE_INVALID")

    def _claim(
        self,
        intent_id: UUID,
        actor: Actor,
        account_role: AccountRole,
        account_fingerprint: str,
    ) -> ExecutionIntent:
        try:
            return self._repository.claim_intent(
                intent_id,
                actor,
                now=datetime(1970, 1, 1, tzinfo=UTC),
                account_role=account_role,
                account_fingerprint=account_fingerprint,
            )
        except ExecutionBlocked as error:
            if str(error) not in {"INTENT_ALREADY_CLAIMED", "ACCOUNT_EXECUTION_LOCKED"}:
                raise
            existing = self._repository.get_intent(intent_id)
            if existing.state.value not in {"CLAIMED", "TERMINAL"}:
                raise
            return existing

    def _completed_advance(self, claim: ExecutionIntent, actor: Actor) -> ExecutionAdvance:
        self._require_claim_actor(claim, actor)
        certificate = self._repository.get_execution_certificate(
            uuid5(NAMESPACE_URL, f"alphadecay:execution:{claim.digest}")
        )
        attempts = self._repository.execution_attempts_for(claim.intent_id)
        return ExecutionAdvance(claim.intent_id, "FINALIZED", None, attempts[-1], certificate)

    @staticmethod
    def _require_claim_actor(claim: ExecutionIntent, actor: Actor) -> None:
        if claim.claimed_by != actor:
            raise ExecutionBlocked("INTENT_CLAIM_ACTOR_MISMATCH")

    def _waiting_or_finalized_advance(self, claim: ExecutionIntent) -> ExecutionAdvance:
        attempts = self._repository.execution_attempts_for(claim.intent_id)
        if not attempts:
            return ExecutionAdvance(claim.intent_id, "WAITING", None)
        if attempts[-1].state != "PREPARED" and _requires_finalization(attempts[-1]):
            certificate = self._finalize_active(
                claim,
                attempts[-1],
                self._repository.trusted_execution_time(claim),
            )
            return ExecutionAdvance(
                claim.intent_id,
                "FINALIZED",
                None,
                attempts[-1],
                certificate,
            )
        lookup = self._lookup_transitional(claim)
        if lookup is not None:
            return lookup
        if attempts[-1].state == "PREPARED":
            return ExecutionAdvance(claim.intent_id, "WAITING", None)
        if attempts[-1].state in self._PENDING_STATES:
            return ExecutionAdvance(claim.intent_id, "WAITING", None)
        certificate = self._finalize_active(
            claim,
            attempts[-1],
            self._repository.trusted_execution_time(claim),
        )
        return ExecutionAdvance(claim.intent_id, "FINALIZED", None, attempts[-1], certificate)

    def _lookup_transitional(self, claim: ExecutionIntent) -> ExecutionAdvance | None:
        authority = self._repository.targeted_lookup_authority(claim)
        if authority is None:
            return None
        permit_id, current = authority
        result = self._lookup_or_defer(claim, permit_id, current.client_order_id)
        observed = replace(
            current,
            state=result.state,
            provider_order_id=result.provider_order_id,
            filled_quantity=result.filled_quantity,
            quantity=result.quantity,
            fill_cash_flow=result.fill_cash_flow,
        )
        self._repository.record_attempt_observation(
            permit_id,
            observed,
            source=AttemptObservationSource.TARGETED_LOOKUP,
            claim=claim,
        )
        if observed.state in PENDING_BROKER_ORDER_STATES and not (
            observed.state == "CALCULATED" and observed.filled_quantity == observed.quantity
        ):
            return ExecutionAdvance(claim.intent_id, "WAITING", None, observed)
        certificate = self._finalize_active(
            claim,
            observed,
            self._repository.trusted_execution_time(claim),
        )
        return ExecutionAdvance(claim.intent_id, "FINALIZED", None, observed, certificate)

    def _replace_due(self, claim: ExecutionIntent) -> ExecutionAdvance:
        if self._quotes is None:
            raise ExecutionBlocked("REPLACEMENT_QUOTES_REQUIRED")
        attempts = self._repository.attempts_for(claim.intent_id)
        current = attempts[-1]
        replacement_target = attempts[-2] if current.state == "PREPARED" else current
        current_limit = (
            replacement_target.limit_price
            if replacement_target.limit_price is not None
            else claim.envelope.minimum_limit
        )
        if current_limit + Decimal("0.01") > claim.envelope.maximum_limit:
            schedule = self._repository.next_broker_mutation(claim)
            if schedule is None:
                return ExecutionAdvance(claim.intent_id, "WAITING", None)
            changed = self._changed_replacement_schedule(claim, schedule)
            if changed is not None:
                return changed
            return ExecutionAdvance(
                claim.intent_id,
                "REPLACEMENT_LIMIT_CAPPED",
                None,
                current,
            )

        snapshots = self._quotes.collect(tuple(leg.symbol for leg in claim.envelope.legs))
        schedule = self._repository.next_broker_mutation(claim)
        if schedule is None:
            return ExecutionAdvance(claim.intent_id, "WAITING", None)
        changed = self._changed_replacement_schedule(claim, schedule)
        if changed is not None:
            return changed
        replacement = _replacement_attempt(
            claim,
            replacement_target,
            snapshots,
            schedule.timing_authority_at,
        )
        plan = self._repository.plan_broker_mutation(
            claim,
            ReconciliationPurpose.REPLACE,
            replacement,
        )
        try:
            observed = self._replace_active(claim, schedule.timing_authority_at, plan)
        except ExecutionBlocked as error:
            if str(error) != "REPLACEMENT_CANCEL_DUE":
                raise
            return self._cancel_advance(
                claim,
                self._repository.trusted_execution_time(claim),
            )
        return ExecutionAdvance(
            claim.intent_id,
            "REPLACED",
            ReconciliationPurpose.REPLACE,
            observed,
        )

    def _changed_replacement_schedule(
        self,
        claim: ExecutionIntent,
        schedule: BrokerMutationSchedule,
    ) -> ExecutionAdvance | None:
        if schedule.purpose == ReconciliationPurpose.CANCEL:
            return self._cancel_advance(claim, schedule.timing_authority_at)
        if schedule.purpose != ReconciliationPurpose.REPLACE:
            raise ExecutionBlocked("EXECUTION_ADVANCE_PURPOSE_CHANGED")
        return None

    def _cancel_advance(
        self,
        claim: ExecutionIntent,
        timing_authority_at: datetime,
    ) -> ExecutionAdvance:
        observed, status = self._cancel_active(claim, timing_authority_at)
        return ExecutionAdvance(
            claim.intent_id,
            status,
            ReconciliationPurpose.CANCEL,
            observed,
        )

    def execute(
        self,
        intent_id: UUID,
        actor: Actor,
        now: datetime,
        *,
        account_role: AccountRole | None = None,
        account_fingerprint: str | None = None,
    ) -> ExecutionCertificate:
        if not callable(getattr(self._repository, "plan_broker_mutation", None)):
            raise ExecutionBlocked("BROKER_WRITE_AUTHORITY_NOT_INTEGRATED")
        if not callable(getattr(self._repository, "get_intent", None)):
            self._repository.claim_intent(
                intent_id,
                actor,
                now=now,
                account_role=self._account_role,
                account_fingerprint=self._account_fingerprint,
            )
            raise ExecutionBlocked("EXECUTION_ADVANCE_NOT_INTEGRATED")
        outcome = self.advance(
            intent_id,
            actor,
            account_role=account_role or self._account_role,
            account_fingerprint=account_fingerprint or self._account_fingerprint,
        )
        if outcome.certificate is not None:
            return outcome.certificate
        if outcome.attempt is not None and _requires_finalization(outcome.attempt):
            claim = self._repository.get_intent(intent_id)
            return self._finalize_active(
                claim,
                outcome.attempt,
                self._repository.trusted_execution_time(claim),
            )
        raise ExecutionPending(ExecutionPendingCode.ADVANCE)

    def _submit_active(self, claim: ExecutionIntent, now: datetime) -> OrderAttempt:
        if claim.envelope.action is ExecutionAction.ROLL:
            if self._quotes is None:
                raise ExecutionBlocked("ROLL_EXECUTION_QUOTES_REQUIRED")
            snapshots = self._quotes.collect(tuple(leg.symbol for leg in claim.envelope.legs))
            _validate_roll_quote_authority(
                claim,
                snapshots,
                self._repository.trusted_execution_time(claim),
            )
        plan = self._repository.plan_broker_mutation(claim, ReconciliationPurpose.SUBMIT)
        prepared = self._prepare(plan, claim, now)
        if prepared.permit is None or prepared.attempt is None:
            raise ExecutionBlocked("BROKER_PREFLIGHT_BLOCKED")
        dispatch = self._repository.acquire_broker_dispatch(
            prepared.permit.permit_id,
            claim=claim,
        )
        if dispatch.dispatch_nonce is None:
            raise ExecutionBlocked("BROKER_DISPATCH_NONCE_MISSING")
        source = AttemptObservationSource.DISPATCH_OUTCOME
        dispatch_nonce = dispatch.dispatch_nonce
        try:
            result = self._broker.submit(claim.envelope, plan.attempt.client_order_id)
        except AmbiguousBrokerResponse:
            self._repository.mark_broker_dispatch_ambiguous(
                prepared.permit.permit_id,
                dispatch_nonce=dispatch.dispatch_nonce,
                claim=claim,
            )
            result = self._lookup_or_defer(
                claim,
                prepared.permit.permit_id,
                plan.attempt.client_order_id,
            )
            source = AttemptObservationSource.TARGETED_LOOKUP
            dispatch_nonce = None
        observed = replace(
            plan.attempt,
            state=result.state,
            provider_order_id=result.provider_order_id,
            filled_quantity=result.filled_quantity,
            quantity=result.quantity,
            fill_cash_flow=result.fill_cash_flow,
        )
        self._repository.record_attempt_observation(
            prepared.permit.permit_id,
            observed,
            source=source,
            claim=claim,
            dispatch_nonce=dispatch_nonce,
        )
        return observed

    def _finalize_active(
        self,
        claim: ExecutionIntent,
        observed: OrderAttempt,
        now: datetime,
    ) -> ExecutionCertificate:
        status = _terminal_status(observed)
        final_expectation = self._repository.final_reconciliation_expectation(claim)
        certificate = _certificate_candidate(
            claim,
            self._repository.execution_attempts_for(claim.intent_id),
            status,
            now,
        )
        for visibility_attempt in range(2):
            final_evidence = self._preflight.collect(final_expectation)
            final_sweep = final_evidence.sweep
            final_reconciliation = WholeAccountReconciliation.evaluate(
                final_sweep,
                final_expectation,
                accepted_at=max(now, final_sweep.retrieval_completed_at),
            )
            try:
                finalized = self._repository.finalize_execution_authorized(
                    certificate,
                    final_reconciliation,
                    status,
                    claim=claim,
                    position_greeks=final_evidence.position_greeks,
                )
            except ExecutionBlocked as error:
                if str(error) == "ATTEMPT_ACTIVITY_EVIDENCE_PENDING" and visibility_attempt == 0:
                    continue
                raise
            if finalized is None:
                raise ExecutionBlocked("FINAL_RECONCILIATION_BLOCKED")
            return finalized
        raise ExecutionBlocked("ATTEMPT_ACTIVITY_EVIDENCE_PENDING")

    def _replace_active(
        self,
        claim: ExecutionIntent,
        now: datetime,
        plan: BrokerMutationPlan,
    ) -> OrderAttempt:
        prepared = self._prepare(plan, claim, now)
        if prepared.permit is None or prepared.attempt is None:
            raise ExecutionBlocked("REPLACE_PREFLIGHT_BLOCKED")
        dispatch = self._repository.acquire_broker_dispatch(
            prepared.permit.permit_id,
            claim=claim,
        )
        if dispatch.dispatch_nonce is None:
            raise ExecutionBlocked("BROKER_DISPATCH_NONCE_MISSING")
        if dispatch.target_provider_order_id is None:
            raise ExecutionBlocked("REPLACE_TARGET_PROVIDER_ORDER_ID_MISSING")
        source = AttemptObservationSource.DISPATCH_OUTCOME
        dispatch_nonce = dispatch.dispatch_nonce
        try:
            result = self._broker.replace(
                dispatch.target_provider_order_id,
                plan.attempt.client_order_id,
                (
                    prepared.attempt.limit_price
                    if prepared.attempt.limit_price is not None
                    else claim.envelope.minimum_limit
                ),
            )
        except AmbiguousBrokerResponse:
            self._repository.mark_broker_dispatch_ambiguous(
                prepared.permit.permit_id,
                dispatch_nonce=dispatch.dispatch_nonce,
                claim=claim,
            )
            result = self._lookup_or_defer(
                claim,
                prepared.permit.permit_id,
                plan.attempt.client_order_id,
            )
            source = AttemptObservationSource.TARGETED_LOOKUP
            dispatch_nonce = None
        observed = replace(
            prepared.attempt,
            state=result.state,
            provider_order_id=result.provider_order_id,
            filled_quantity=result.filled_quantity,
            quantity=result.quantity,
            fill_cash_flow=result.fill_cash_flow,
        )
        self._repository.record_attempt_observation(
            prepared.permit.permit_id,
            observed,
            source=source,
            claim=claim,
            dispatch_nonce=dispatch_nonce,
        )
        return observed

    def _cancel_active(
        self,
        claim: ExecutionIntent,
        now: datetime,
    ) -> tuple[OrderAttempt, str]:
        plan = self._repository.plan_broker_mutation(claim, ReconciliationPurpose.CANCEL)
        prepared = self._prepare(plan, claim, now)
        if prepared.permit is None or prepared.attempt is None:
            raise ExecutionBlocked("CANCEL_PREFLIGHT_BLOCKED")
        dispatch = self._repository.acquire_broker_dispatch(
            prepared.permit.permit_id,
            claim=claim,
        )
        if dispatch.dispatch_nonce is None:
            raise ExecutionBlocked("BROKER_DISPATCH_NONCE_MISSING")
        if dispatch.target_provider_order_id is None:
            raise ExecutionBlocked("CANCEL_TARGET_PROVIDER_ORDER_ID_MISSING")
        try:
            result = self._broker.cancel(dispatch.target_provider_order_id)
        except AmbiguousBrokerResponse as error:
            self._repository.mark_broker_dispatch_ambiguous(
                prepared.permit.permit_id,
                dispatch_nonce=dispatch.dispatch_nonce,
                claim=claim,
            )
            raise ExecutionPending(ExecutionPendingCode.CANCEL_LOOKUP_DEFERRED) from error
        observed = replace(
            plan.attempt,
            state=result.state,
            provider_order_id=result.provider_order_id,
            filled_quantity=result.filled_quantity,
            quantity=result.quantity,
            fill_cash_flow=result.fill_cash_flow,
        )
        self._repository.record_attempt_observation(
            prepared.permit.permit_id,
            observed,
            source=AttemptObservationSource.DISPATCH_OUTCOME,
            claim=claim,
            dispatch_nonce=dispatch.dispatch_nonce,
        )
        for _ in range(2):
            if observed.state != "PENDING_CANCEL":
                break
            result = self._lookup_or_defer(
                claim,
                prepared.permit.permit_id,
                plan.attempt.client_order_id,
            )
            observed = replace(
                plan.attempt,
                state=result.state,
                provider_order_id=result.provider_order_id,
                filled_quantity=result.filled_quantity,
                quantity=result.quantity,
                fill_cash_flow=result.fill_cash_flow,
            )
            self._repository.record_attempt_observation(
                prepared.permit.permit_id,
                observed,
                source=AttemptObservationSource.TARGETED_LOOKUP,
                claim=claim,
            )
        if observed.state == "PENDING_CANCEL":
            raise ExecutionPending(ExecutionPendingCode.CANCEL_PENDING)
        if observed.state == "FILLED":
            return observed, "FILLED"
        if observed.state != "CANCELED":
            raise ExecutionPending(ExecutionPendingCode.CANCEL_NOT_TERMINAL)
        if observed.filled_quantity == 0:
            return observed, "CANCELED"
        if observed.filled_quantity < observed.quantity:
            return observed, "PARTIAL_CANCELED_RECONCILED"
        raise ExecutionPending(ExecutionPendingCode.CANCEL_NOT_TERMINAL)

    def _lookup_or_defer(
        self,
        claim: ExecutionIntent,
        permit_id: UUID,
        client_order_id: str,
    ) -> BrokerResult:
        try:
            result = self._broker.lookup(client_order_id)
        except AmbiguousBrokerResponse:
            self._repository.record_attempt_lookup_failure(permit_id, claim=claim)
            raise ExecutionPending(ExecutionPendingCode.LOOKUP_DEFERRED) from None
        if result is None:
            self._repository.record_attempt_absence(
                permit_id,
                source=AttemptObservationSource.TARGETED_LOOKUP,
                claim=claim,
            )
            raise ExecutionPending(ExecutionPendingCode.LOOKUP_ABSENT)
        return result

    def _prepare(
        self,
        plan: BrokerMutationPlan,
        claim: ExecutionIntent,
        now: datetime,
    ) -> BrokerMutationPreparation:
        sweep = self._preflight.collect(plan.expectation).sweep
        reconciliation = evaluate_broker_mutation_preflight(
            plan,
            sweep,
            accepted_at=max(now, sweep.retrieval_completed_at),
        )
        return self._repository.prepare_broker_mutation(
            reconciliation,
            plan.attempt,
            claim=claim,
        )


def _certificate_candidate(
    claim: ExecutionIntent,
    attempts: tuple[OrderAttempt, ...],
    status: str,
    now: datetime,
) -> ExecutionCertificate:
    entry_approval_id = (
        claim.envelope.authorization_certificate_id
        if claim.envelope.action == ExecutionAction.ENTRY
        else None
    )
    assessment_certificate_id = (
        claim.envelope.authorization_certificate_id
        if claim.envelope.action != ExecutionAction.ENTRY
        else None
    )
    return ExecutionCertificate(
        certificate_id=uuid5(NAMESPACE_URL, f"alphadecay:execution:{claim.digest}"),
        intent_id=claim.intent_id,
        entry_approval_id=entry_approval_id,
        assessment_certificate_id=assessment_certificate_id,
        execution_status=status,
        attempt_ids=tuple(attempt.client_order_id for attempt in attempts),
        actual_exposure=None,
        reconciliation_checks=(
            "TERMINAL",
            "REMAINDER_ABSENT",
            "WHOLE_ACCOUNT_RECONCILED",
        ),
        created_at=now,
    )


def _terminal_status(attempt: OrderAttempt) -> str:
    if attempt.state in {"FILLED", "CALCULATED"} and (attempt.filled_quantity == attempt.quantity):
        return "FILLED"
    if (
        attempt.state in FINALIZABLE_BROKER_ORDER_STATES - {"FILLED", "CALCULATED"}
        and attempt.filled_quantity == 0
    ):
        return attempt.state
    if attempt.state == "CANCELED" and 0 < attempt.filled_quantity < attempt.quantity:
        return "PARTIAL_CANCELED_RECONCILED"
    if attempt.state == "EXPIRED" and 0 < attempt.filled_quantity < attempt.quantity:
        return "PARTIAL_EXPIRED_RECONCILED"
    if attempt.state == "REPLACED" and 0 < attempt.filled_quantity < attempt.quantity:
        return "PARTIAL_REPLACED_RECONCILED"
    raise ExecutionBlocked("EXECUTION_OUTCOME_NOT_SUPPORTED")


def _execution_outcome_ready(attempt: OrderAttempt) -> bool:
    try:
        _terminal_status(attempt)
    except ExecutionBlocked:
        return False
    return True


def _requires_finalization(attempt: OrderAttempt) -> bool:
    return attempt.state in TERMINAL_BROKER_ORDER_STATES or _execution_outcome_ready(attempt)


def _replacement_attempt(
    claim: ExecutionIntent,
    current: OrderAttempt,
    snapshots: tuple[NormalizedOptionSnapshot, ...],
    timing_authority_at: datetime,
) -> OrderAttempt:
    legs = claim.envelope.legs
    if len(snapshots) != len(legs) or tuple(item.symbol for item in snapshots) != tuple(
        leg.symbol for leg in legs
    ):
        raise ExecutionBlocked("REPLACEMENT_QUOTE_SYMBOL_MISMATCH")
    contracts = tuple(_option_contract(leg.symbol) for leg in legs)
    underlyings = tuple(contract[0] for contract in contracts)
    expiry_counts: dict[str, int] = {}
    for contract in contracts:
        expiry_counts[contract[1]] = expiry_counts.get(contract[1], 0) + 1
    if (
        len(set(underlyings)) != 1
        or sorted(expiry_counts.values()) != ([2] if len(legs) == 2 else [2, 2])
        or len({contract[2] for contract in contracts}) != 1
        or len({(contract[1], contract[2], contract[3]) for contract in contracts})
        != len(contracts)
        or any(
            len({contract[3] for contract in contracts if contract[1] == expiry}) != 2
            for expiry in expiry_counts
        )
        or any(leg.ratio != 1 for leg in legs)
        or any(
            item.underlying != expected
            for item, expected in zip(snapshots, underlyings, strict=True)
        )
    ):
        raise ExecutionBlocked("REPLACEMENT_QUOTE_STRUCTURE_MISMATCH")
    if any(
        item.quote_timestamp > timing_authority_at
        or item.retrieved_at > timing_authority_at
        or item.quote_timestamp > item.retrieved_at
        or timing_authority_at - item.quote_timestamp > timedelta(seconds=30)
        or timing_authority_at - item.retrieved_at > timedelta(seconds=30)
        or not item.bid_price.is_finite()
        or not item.ask_price.is_finite()
        or item.bid_price <= 0
        or item.ask_price <= 0
        or item.bid_price > item.ask_price
        or item.bid_size <= 0
        or item.ask_size <= 0
        for item in snapshots
    ):
        raise ExecutionBlocked("REPLACEMENT_QUOTE_INVALID")
    if claim.envelope.action is ExecutionAction.ROLL:
        _validate_roll_quote_authority(claim, snapshots, timing_authority_at)
    timestamps = tuple(item.quote_timestamp for item in snapshots)
    if max(timestamps) - min(timestamps) > timedelta(seconds=30):
        raise ExecutionBlocked("REPLACEMENT_QUOTE_UNSYNCHRONIZED")
    net_low = Decimal(0)
    net_high = Decimal(0)
    for leg, item in zip(legs, snapshots, strict=True):
        if leg.intent in {PositionIntent.BUY_TO_OPEN, PositionIntent.BUY_TO_CLOSE}:
            net_low += item.bid_price * leg.ratio
            net_high += item.ask_price * leg.ratio
        else:
            net_low -= item.ask_price * leg.ratio
            net_high -= item.bid_price * leg.ratio
    quoted_width = net_high - net_low
    payoff_width = sum(
        max(contract[3] for contract in contracts if contract[1] == expiry)
        - min(contract[3] for contract in contracts if contract[1] == expiry)
        for expiry in expiry_counts
    )
    if (
        quoted_width <= 0
        or payoff_width <= 0
        or abs(net_low) >= payoff_width
        or abs(net_high) >= payoff_width
    ):
        raise ExecutionBlocked("REPLACEMENT_QUOTED_WIDTH_INVALID")
    increment = max(Decimal("0.01"), quoted_width * Decimal("0.10")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    current_limit = (
        current.limit_price if current.limit_price is not None else claim.envelope.minimum_limit
    )
    next_limit = min(
        claim.envelope.maximum_limit,
        (current_limit + increment).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
    )
    if next_limit == 0:
        raise ExecutionBlocked("REPLACEMENT_LIMIT_ZERO")
    if next_limit <= current_limit:
        raise ExecutionBlocked("REPLACEMENT_LIMIT_CAPPED")
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
        for item in snapshots
    ]
    quote_hash = hashlib.sha256(
        json.dumps(quote_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    ordinal = current.ordinal + 1
    identifier = client_order_id(
        claim.envelope.trading_day,
        claim.envelope.action,
        claim.digest,
        ordinal,
    )
    retrieved_at = max(item.retrieved_at for item in snapshots)
    request_hash = replacement_request_hash(
        claim.digest,
        ordinal,
        identifier,
        next_limit,
        current.client_order_id,
        current.request_hash,
        quote_hash,
        timestamps,
        retrieved_at,
        timing_authority_at,
    )
    return OrderAttempt(
        intent_id=claim.intent_id,
        ordinal=ordinal,
        client_order_id=identifier,
        request_hash=request_hash,
        state="PREPARED",
        replaces_client_order_id=current.client_order_id,
        quantity=claim.envelope.quantity,
        limit_price=next_limit,
        quote_hash=quote_hash,
        quote_source_timestamps=timestamps,
        quote_retrieved_at=retrieved_at,
        timing_authority_at=timing_authority_at,
        prior_request_hash=current.request_hash,
    )


def _validate_roll_quote_authority(
    claim: ExecutionIntent,
    snapshots: tuple[NormalizedOptionSnapshot, ...],
    timing_authority_at: datetime,
) -> None:
    envelope = claim.envelope
    if (
        envelope.action is not ExecutionAction.ROLL
        or envelope.maximum_relative_spread is None
        or envelope.maximum_incremental_debit is None
        or len(snapshots) != 4
        or tuple(item.symbol for item in snapshots) != tuple(leg.symbol for leg in envelope.legs)
    ):
        raise ExecutionBlocked("ROLL_QUOTE_AUTHORITY_INVALID")
    if any(
        item.quote_timestamp > timing_authority_at
        or item.retrieved_at > timing_authority_at
        or item.quote_timestamp > item.retrieved_at
        or timing_authority_at - item.quote_timestamp > timedelta(seconds=30)
        or timing_authority_at - item.retrieved_at > timedelta(seconds=30)
        or item.bid_price <= 0
        or item.ask_price < item.bid_price
        or item.bid_size <= 0
        or item.ask_size <= 0
        for item in snapshots
    ):
        raise ExecutionBlocked("ROLL_QUOTE_AUTHORITY_INVALID")
    gross_midpoint = sum(
        ((item.bid_price + item.ask_price) / Decimal(2)) * leg.ratio
        for leg, item in zip(envelope.legs, snapshots, strict=True)
    )
    quoted_width = sum(
        (item.ask_price - item.bid_price) * leg.ratio
        for leg, item in zip(envelope.legs, snapshots, strict=True)
    )
    if gross_midpoint <= 0 or any(
        (item.ask_price - item.bid_price) / ((item.bid_price + item.ask_price) / Decimal(2))
        > envelope.maximum_relative_spread
        for item in snapshots
    ):
        raise ExecutionBlocked("ROLL_LIQUIDITY_DERIORATED")
    if quoted_width / gross_midpoint > envelope.maximum_relative_spread:
        raise ExecutionBlocked("ROLL_LIQUIDITY_DERIORATED")
    provider_debit = sum(
        (
            item.ask_price
            if leg.intent in {PositionIntent.BUY_TO_OPEN, PositionIntent.BUY_TO_CLOSE}
            else -item.bid_price
        )
        * leg.ratio
        for leg, item in zip(envelope.legs, snapshots, strict=True)
    )
    incremental_debit = max(
        Decimal(0),
        provider_debit * envelope.quantity * Decimal(100),
    )
    if incremental_debit > envelope.maximum_incremental_debit:
        raise ExecutionBlocked("ROLL_INCREMENTAL_DEBIT_DERIORATED")


def _option_contract(symbol: str) -> tuple[str, str, str, Decimal]:
    try:
        parsed = parse_standard_option_contract_symbol(symbol)
    except OptionContractSymbolError as error:
        if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
            raise ExecutionBlocked(error.code) from error
        raise ExecutionBlocked("REPLACEMENT_QUOTE_STRUCTURE_MISMATCH") from error
    return (
        parsed.root_symbol,
        parsed.expiration_date.strftime("%y%m%d"),
        parsed.right,
        parsed.strike_price,
    )


def _validate_standard_contract_envelope(envelope: OrderEnvelope) -> None:
    contracts = []
    for leg in envelope.legs:
        try:
            contracts.append(parse_standard_option_contract_symbol(leg.symbol))
        except OptionContractSymbolError as error:
            if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
                raise ExecutionBlocked(error.code) from error
            raise ExecutionBlocked("ORDER_CONTRACT_SYMBOL_INVALID") from error
    if len({contract.root_symbol for contract in contracts}) != 1:
        raise ExecutionBlocked("ORDER_CONTRACT_UNDERLYING_MISMATCH")
