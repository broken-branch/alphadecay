from __future__ import annotations

from decimal import Decimal

from backend.app.execution.models import ExecutionBlocked, OrderAttempt
from backend.app.execution.order_status import (
    KNOWN_BROKER_ORDER_STATES,
    TERMINAL_BROKER_ORDER_STATES,
    broker_state_matches_fill,
)

TERMINAL_ATTEMPT_STATES = TERMINAL_BROKER_ORDER_STATES


def validate_attempt_observation(existing: OrderAttempt, observed: OrderAttempt) -> None:
    if observed.state not in KNOWN_BROKER_ORDER_STATES:
        raise ExecutionBlocked("ATTEMPT_STATE_INVALID")
    if observed.provider_order_id is None:
        raise ExecutionBlocked("ATTEMPT_PROVIDER_ID_REQUIRED")
    if (
        not isinstance(observed.provider_order_id, str)
        or not observed.provider_order_id
        or observed.provider_order_id.strip() != observed.provider_order_id
        or len(observed.provider_order_id) > 128
    ):
        raise ExecutionBlocked("ATTEMPT_PROVIDER_ID_INVALID")
    if existing.provider_order_id is not None and (
        observed.provider_order_id != existing.provider_order_id
    ):
        raise ExecutionBlocked("ATTEMPT_PROVIDER_ID_MISMATCH")
    if (
        isinstance(existing.quantity, bool)
        or not isinstance(existing.quantity, int)
        or existing.quantity < 0
        or isinstance(observed.quantity, bool)
        or not isinstance(observed.quantity, int)
        or observed.quantity <= 0
        or (existing.quantity > 0 and observed.quantity != existing.quantity)
    ):
        raise ExecutionBlocked("ATTEMPT_QUANTITY_MISMATCH")
    if isinstance(observed.filled_quantity, bool) or not isinstance(observed.filled_quantity, int):
        raise ExecutionBlocked("ATTEMPT_FILL_INVALID")
    if observed.filled_quantity < existing.filled_quantity:
        raise ExecutionBlocked("ATTEMPT_FILL_REGRESSION")
    if not 0 <= observed.filled_quantity <= observed.quantity:
        raise ExecutionBlocked("ATTEMPT_FILL_INVALID")
    if observed.filled_quantity == 0:
        if observed.fill_cash_flow not in {None, Decimal(0)}:
            raise ExecutionBlocked("ATTEMPT_CASH_FLOW_INVALID")
    elif (
        not isinstance(observed.fill_cash_flow, Decimal)
        or not observed.fill_cash_flow.is_finite()
        or observed.fill_cash_flow == 0
    ):
        raise ExecutionBlocked("ATTEMPT_CASH_FLOW_REQUIRED")
    if existing.fill_cash_flow not in {None, Decimal(0)} and (
        observed.fill_cash_flow is None
        or existing.fill_cash_flow.is_signed() != observed.fill_cash_flow.is_signed()
        or abs(observed.fill_cash_flow) < abs(existing.fill_cash_flow)
    ):
        raise ExecutionBlocked("ATTEMPT_CASH_FLOW_REGRESSION")
    if existing.state in TERMINAL_ATTEMPT_STATES and observed.state != existing.state:
        raise ExecutionBlocked("ATTEMPT_TERMINAL_STATE_REGRESSION")
    if not _fill_matches_state(observed):
        raise ExecutionBlocked("ATTEMPT_STATE_FILL_INVALID")


def _fill_matches_state(observed: OrderAttempt) -> bool:
    return broker_state_matches_fill(
        observed.state,
        observed.filled_quantity,
        observed.quantity,
    )
