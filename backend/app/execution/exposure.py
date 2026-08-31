from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from backend.app.contracts.v1 import GreekExposure

from .models import ExecutionBlocked, PositionGreekObservation
from .reconciliation import InventoryItem, InventoryKind


def reconcile_actual_exposure(
    positions: tuple[InventoryItem, ...],
    observations: tuple[PositionGreekObservation, ...],
    *,
    accepted_at: datetime,
) -> GreekExposure | None:
    if any(position.kind == InventoryKind.EQUITY for position in positions):
        raise ExecutionBlocked("ASSIGNMENT_SUSPECTED")
    option_positions = tuple(
        position for position in positions if position.kind == InventoryKind.OPTION
    )
    if tuple(item.symbol for item in observations) != tuple(
        sorted(item.symbol for item in observations)
    ) or len({item.symbol for item in observations}) != len(observations):
        raise ExecutionBlocked("POSITION_GREEK_EVIDENCE_NOT_CANONICAL")
    expected = {
        position.symbol: (position.signed_quantity, position.multiplier)
        for position in option_positions
    }
    observed = {item.symbol: (item.signed_quantity, item.multiplier) for item in observations}
    if observed != expected:
        raise ExecutionBlocked("POSITION_GREEK_EVIDENCE_MISMATCH")
    if not observations:
        return None
    retrieved_at = {item.retrieved_at for item in observations}
    source_timestamps = tuple(item.source_timestamp for item in observations)
    if len(retrieved_at) != 1 or max(source_timestamps) - min(source_timestamps) > timedelta(
        seconds=1
    ):
        raise ExecutionBlocked("POSITION_GREEK_EVIDENCE_UNSYNCHRONIZED")
    retrieval_time = next(iter(retrieved_at))
    if (
        retrieval_time > accepted_at
        or accepted_at - retrieval_time > timedelta(seconds=15)
        or any(
            observation.retrieved_at - observation.source_timestamp > timedelta(seconds=15)
            for observation in observations
        )
    ):
        raise ExecutionBlocked("POSITION_GREEK_EVIDENCE_STALE")

    def total(field: str) -> Decimal:
        return sum(
            (
                item.signed_quantity * item.multiplier * getattr(item, field)
                for item in observations
            ),
            start=Decimal(0),
        )

    return GreekExposure(
        delta=total("delta"),
        gamma=total("gamma"),
        theta_per_day=total("theta_per_day"),
        vega_per_iv_point=total("vega_per_iv_point"),
    )
