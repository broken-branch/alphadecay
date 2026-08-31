from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from backend.app.execution.models import (
    ExecutionAction,
    ExecutionBlocked,
    ExecutionCertificate,
    ExecutionIntent,
    IntentState,
    OrderAttempt,
    Reconciliation,
)
from backend.app.execution.order_status import (
    FINALIZABLE_BROKER_ORDER_STATES,
    broker_state_matches_fill,
)

RECONCILIATION_CHECKS = (
    "TERMINAL",
    "REMAINDER_ABSENT",
    "WHOLE_ACCOUNT_RECONCILED",
)
TERMINAL_ATTEMPT_STATES = FINALIZABLE_BROKER_ORDER_STATES | {"ASSIGNMENT_LOCKED"}


def execution_lock_reason(reconciliation: Reconciliation) -> str | None:
    if reconciliation.assignment_suspected:
        return "ASSIGNMENT_SUSPECTED"
    if not reconciliation.matches_expected:
        return "RECONCILIATION_MISMATCH"
    return None


def validate_finalization(
    intent: ExecutionIntent,
    attempts: tuple[OrderAttempt, ...],
    certificate: ExecutionCertificate,
    reconciliation: Reconciliation,
    requested_status: str,
    trusted_now: datetime,
) -> tuple[ExecutionCertificate, bool]:
    if intent.state != IntentState.CLAIMED:
        raise ExecutionBlocked("INTENT_NOT_CLAIMED")
    if not attempts or tuple(attempt.ordinal for attempt in attempts) != tuple(
        range(len(attempts))
    ):
        raise ExecutionBlocked("ATTEMPT_LINEAGE_INVALID")
    if len(attempts) > 4 or attempts[-1].state not in TERMINAL_ATTEMPT_STATES:
        raise ExecutionBlocked("ATTEMPT_NOT_TERMINAL")
    if intent.first_fill_consumed:
        raise ExecutionBlocked("EXECUTION_ALREADY_CONSUMED")

    quantity = intent.envelope.quantity
    for index, attempt in enumerate(attempts):
        if attempt.intent_id != intent.intent_id or attempt.quantity != quantity:
            raise ExecutionBlocked("ATTEMPT_QUANTITY_MISMATCH")
        if not 0 <= attempt.filled_quantity <= attempt.quantity:
            raise ExecutionBlocked("ATTEMPT_FILL_INVALID")
        if attempt.state != "ASSIGNMENT_LOCKED" and not broker_state_matches_fill(
            attempt.state,
            attempt.filled_quantity,
            attempt.quantity,
        ):
            raise ExecutionBlocked("ATTEMPT_STATE_FILL_INVALID")
        if index < len(attempts) - 1 and attempt.filled_quantity:
            raise ExecutionBlocked("ATTEMPT_FILL_LINEAGE_INVALID")
        expected_replaced_id = attempts[index - 1].client_order_id if index else None
        if attempt.replaces_client_order_id != expected_replaced_id:
            raise ExecutionBlocked("ATTEMPT_LINEAGE_INVALID")
    filled_quantity = attempts[-1].filled_quantity

    if not reconciliation.terminal or not reconciliation.remainder_absent:
        raise ExecutionBlocked("RECONCILIATION_INCOMPLETE")
    _validate_requested_status(requested_status, attempts[-1], filled_quantity, quantity)

    if reconciliation.assignment_suspected:
        execution_status = "ASSIGNMENT_LOCKED"
    elif not reconciliation.matches_expected and requested_status != "ASSIGNMENT_LOCKED":
        execution_status = "RECONCILIATION_MISMATCH"
    else:
        execution_status = requested_status

    expected_certificate_id = uuid5(NAMESPACE_URL, f"alphadecay:execution:{intent.digest}")
    if certificate.certificate_id != expected_certificate_id:
        raise ExecutionBlocked("CERTIFICATE_ID_MISMATCH")
    entry_approval_id = (
        intent.envelope.authorization_certificate_id
        if intent.envelope.action == ExecutionAction.ENTRY
        else None
    )
    assessment_certificate_id = (
        intent.envelope.authorization_certificate_id
        if intent.envelope.action != ExecutionAction.ENTRY
        else None
    )
    if (
        certificate.intent_id != intent.intent_id
        or certificate.entry_approval_id != entry_approval_id
        or certificate.assessment_certificate_id != assessment_certificate_id
        or certificate.attempt_ids != tuple(attempt.client_order_id for attempt in attempts)
    ):
        raise ExecutionBlocked("CERTIFICATE_LINEAGE_MISMATCH")
    if certificate.execution_status != execution_status:
        raise ExecutionBlocked("CERTIFICATE_STATUS_MISMATCH")
    if certificate.actual_exposure != reconciliation.actual_exposure:
        raise ExecutionBlocked("CERTIFICATE_EXPOSURE_MISMATCH")
    if certificate.reconciliation_checks != RECONCILIATION_CHECKS:
        raise ExecutionBlocked("CERTIFICATE_CHECKS_MISMATCH")

    return replace(certificate, created_at=trusted_now), filled_quantity > 0


def _validate_requested_status(
    requested_status: str,
    terminal_attempt: OrderAttempt,
    filled_quantity: int,
    quantity: int,
) -> None:
    state = terminal_attempt.state
    if requested_status == "FILLED":
        valid = state in {"FILLED", "CALCULATED"} and filled_quantity == quantity
    elif requested_status == "PARTIAL_CANCELED_RECONCILED":
        valid = state == "CANCELED" and 0 < filled_quantity < quantity
    elif requested_status == "PARTIAL_EXPIRED_RECONCILED":
        valid = state == "EXPIRED" and 0 < filled_quantity < quantity
    elif requested_status == "PARTIAL_REPLACED_RECONCILED":
        valid = state == "REPLACED" and 0 < filled_quantity < quantity
    elif requested_status == "ASSIGNMENT_LOCKED":
        valid = state == "ASSIGNMENT_LOCKED"
    elif requested_status in {"REJECTED", "CANCELED", "EXPIRED", "REPLACED", "UNFILLED"}:
        valid = state in {"REJECTED", "CANCELED", "EXPIRED", "REPLACED"} and filled_quantity == 0
    else:
        valid = False
    if not valid:
        raise ExecutionBlocked("EXECUTION_STATUS_ATTEMPT_MISMATCH")
