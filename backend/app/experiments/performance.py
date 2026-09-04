from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from backend.app.experiment_lineage import ExperimentExecutionLineage


class ExperimentPerformanceEvidenceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExperimentTerminalState(StrEnum):
    NO_POSITION = "NO_POSITION"
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class MetricUnavailableReason(StrEnum):
    NO_OPENED_TRADES = "NO_OPENED_TRADES"
    NO_CLOSED_TRADES = "NO_CLOSED_TRADES"


@dataclass(frozen=True)
class ProjectedMetric:
    value: Decimal | int | None
    unavailable_reason: MetricUnavailableReason | None = None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.unavailable_reason is None):
            raise ValueError("EXPERIMENT_PERFORMANCE_METRIC_AVAILABILITY_INVALID")

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None


@dataclass(frozen=True)
class ExperimentDecisionEvidence:
    decision_id: UUID
    lineage: ExperimentExecutionLineage
    occurred_at: datetime


@dataclass(frozen=True)
class ContractQuantity:
    symbol: str
    signed_quantity: int
    multiplier: int = 100


@dataclass(frozen=True)
class FillAttemptEvidence:
    attempt_id: UUID
    attempt_ordinal: int
    requested_quantity: int
    filled_quantity: int
    fill_cash_flow: Decimal | None


@dataclass(frozen=True)
class LifecycleTransitionEvidence:
    transition_id: UUID
    predecessor_transition_id: UUID | None
    sequence: int
    action: str
    decision_id: UUID
    authorization_id: UUID
    authorization_lineage: ExperimentExecutionLineage
    occurred_at: datetime
    intent_action: str
    authorized_quantity: int
    execution_status: str
    certificate_attempt_ids: tuple[UUID, ...]
    attempts: tuple[FillAttemptEvidence, ...]
    reconciled_contract_activity: tuple[ContractQuantity, ...]
    resulting_inventory: tuple[ContractQuantity, ...]
    cash_flow: Decimal
    snapshot_cumulative_cash_flow: Decimal


@dataclass(frozen=True)
class PositionLifecycleEvidence:
    managed_position_id: UUID
    lineage: ExperimentExecutionLineage
    entry_decision_id: UUID
    entry_approval_id: UUID
    entry_approval_lineage: ExperimentExecutionLineage
    defined_maximum_risk: Decimal
    activated_at: datetime
    closed_at: datetime | None
    transitions: tuple[LifecycleTransitionEvidence, ...]


@dataclass(frozen=True)
class ExperimentPerformanceProjection:
    lineage: ExperimentExecutionLineage
    decision_count: int
    opened_trade_count: int
    closed_trade_count: int
    terminal_state: ExperimentTerminalState
    total_defined_maximum_risk_at_entry: ProjectedMetric
    entry_cash_flow: ProjectedMetric
    management_cash_flow: ProjectedMetric
    exit_cash_flow: ProjectedMetric
    realized_strategy_pnl: ProjectedMetric
    win_count: ProjectedMetric
    loss_count: ProjectedMetric
    breakeven_count: ProjectedMetric


def project_experiment_performance(
    *,
    lineage: ExperimentExecutionLineage,
    decisions: tuple[ExperimentDecisionEvidence, ...],
    positions: tuple[PositionLifecycleEvidence, ...],
) -> ExperimentPerformanceProjection:
    decision_by_id = _validate_decisions(lineage, decisions)
    ordered_positions = sorted(positions, key=lambda item: _utc(item.activated_at))
    _validate_position_order(ordered_positions)

    entry_cash_flow = Decimal(0)
    management_cash_flow = Decimal(0)
    exit_cash_flow = Decimal(0)
    total_maximum_risk = Decimal(0)
    closed_pnl: list[Decimal] = []

    for position in ordered_positions:
        if position.lineage != lineage:
            raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LINEAGE_MISMATCH")
        transitions = _validate_position(position, decision_by_id)
        total_maximum_risk += position.defined_maximum_risk
        entry_cash_flow += transitions[0].cash_flow
        management_cash_flow += sum(
            (item.cash_flow for item in transitions if item.action == "ROLL"),
            start=Decimal(0),
        )
        if transitions[-1].action == "CLOSE":
            exit_cash_flow += transitions[-1].cash_flow
            closed_pnl.append(transitions[-1].snapshot_cumulative_cash_flow)

    opened_count = len(ordered_positions)
    closed_count = len(closed_pnl)
    terminal_state = _terminal_state(ordered_positions)
    no_opened = _unavailable(MetricUnavailableReason.NO_OPENED_TRADES)
    no_closed = _unavailable(MetricUnavailableReason.NO_CLOSED_TRADES)

    return ExperimentPerformanceProjection(
        lineage=lineage,
        decision_count=len(decisions),
        opened_trade_count=opened_count,
        closed_trade_count=closed_count,
        terminal_state=terminal_state,
        total_defined_maximum_risk_at_entry=(
            _available(total_maximum_risk) if opened_count else no_opened
        ),
        entry_cash_flow=_available(entry_cash_flow) if opened_count else no_opened,
        management_cash_flow=(_available(management_cash_flow) if opened_count else no_opened),
        exit_cash_flow=_available(exit_cash_flow) if closed_count else no_closed,
        realized_strategy_pnl=(
            _available(sum(closed_pnl, start=Decimal(0))) if closed_count else no_closed
        ),
        win_count=(
            _available(sum(value > 0 for value in closed_pnl)) if closed_count else no_closed
        ),
        loss_count=(
            _available(sum(value < 0 for value in closed_pnl)) if closed_count else no_closed
        ),
        breakeven_count=(
            _available(sum(value == 0 for value in closed_pnl)) if closed_count else no_closed
        ),
    )


def _validate_decisions(
    lineage: ExperimentExecutionLineage,
    decisions: tuple[ExperimentDecisionEvidence, ...],
) -> dict[UUID, ExperimentDecisionEvidence]:
    decision_by_id: dict[UUID, ExperimentDecisionEvidence] = {}
    for decision in decisions:
        if decision.lineage != lineage:
            raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LINEAGE_MISMATCH")
        if decision.decision_id in decision_by_id:
            raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_DECISION_DUPLICATE")
        decision_by_id[decision.decision_id] = decision
    return decision_by_id


def _validate_position_order(positions: list[PositionLifecycleEvidence]) -> None:
    position_ids: set[UUID] = set()
    previous: PositionLifecycleEvidence | None = None
    for position in positions:
        if position.managed_position_id in position_ids:
            raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_POSITION_DUPLICATE")
        position_ids.add(position.managed_position_id)
        if previous is not None and (
            previous.closed_at is None or _utc(previous.closed_at) > _utc(position.activated_at)
        ):
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_POSITION_CHRONOLOGY_INVALID"
            )
        previous = position


def _validate_position(
    position: PositionLifecycleEvidence,
    decision_by_id: dict[UUID, ExperimentDecisionEvidence],
) -> tuple[LifecycleTransitionEvidence, ...]:
    if position.defined_maximum_risk <= 0 or not position.transitions:
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LIFECYCLE_INCOMPLETE")
    if position.entry_approval_lineage != position.lineage:
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LINEAGE_MISMATCH")
    transitions = tuple(sorted(position.transitions, key=lambda item: item.sequence))
    cumulative_cash_flow = Decimal(0)
    inventory: dict[tuple[str, int], int] = {}
    previous: LifecycleTransitionEvidence | None = None

    for index, transition in enumerate(transitions):
        decision = decision_by_id.get(transition.decision_id)
        if decision is None or _utc(decision.occurred_at) > _utc(transition.occurred_at):
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_DECISION_LINEAGE_INCOMPLETE"
            )
        if transition.authorization_lineage != position.lineage:
            raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LINEAGE_MISMATCH")
        expected_action = "ENTRY" if index == 0 else transition.action
        if (
            transition.sequence != index
            or transition.action != expected_action
            or transition.intent_action != transition.action
            or transition.execution_status != "FILLED"
            or transition.authorized_quantity <= 0
            or (index == 0 and transition.authorization_id != position.entry_approval_id)
            or (index == 0 and transition.decision_id != position.entry_decision_id)
            or (index > 0 and transition.action not in {"ROLL", "CLOSE"})
            or (previous is None and transition.predecessor_transition_id is not None)
            or (
                previous is not None
                and transition.predecessor_transition_id != previous.transition_id
            )
            or (previous is not None and _utc(transition.occurred_at) < _utc(previous.occurred_at))
            or (previous is not None and previous.action == "CLOSE")
        ):
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_LIFECYCLE_CHRONOLOGY_INVALID"
            )
        _validate_execution(transition)
        inventory = _apply_activity(inventory, transition.reconciled_contract_activity)
        if inventory != _inventory(transition.resulting_inventory):
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
            )
        if transition.action != "CLOSE" and not inventory:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
            )
        if transition.action == "CLOSE" and inventory:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
            )
        cumulative_cash_flow += transition.cash_flow
        if transition.snapshot_cumulative_cash_flow != cumulative_cash_flow:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_CASH_FLOW_LINEAGE_INCOMPLETE"
            )
        previous = transition

    first, last = transitions[0], transitions[-1]
    if _utc(position.activated_at) != _utc(first.occurred_at):
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_LIFECYCLE_CHRONOLOGY_INVALID"
        )
    if (
        (position.closed_at is None and last.action == "CLOSE")
        or (position.closed_at is not None and last.action != "CLOSE")
        or (position.closed_at is not None and _utc(position.closed_at) != _utc(last.occurred_at))
    ):
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_TERMINAL_STATE_INVALID")
    return transitions


def _validate_execution(transition: LifecycleTransitionEvidence) -> None:
    attempts = sorted(transition.attempts, key=lambda item: item.attempt_ordinal)
    if (
        not attempts
        or [item.attempt_ordinal for item in attempts] != list(range(len(attempts)))
        or tuple(item.attempt_id for item in attempts) != transition.certificate_attempt_ids
        or sum(item.filled_quantity for item in attempts) != transition.authorized_quantity
    ):
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE")
    cash_flow = Decimal(0)
    for attempt in attempts:
        if (
            attempt.requested_quantity <= 0
            or not 0 <= attempt.filled_quantity <= attempt.requested_quantity
            or (attempt.filled_quantity > 0) != (attempt.fill_cash_flow is not None)
        ):
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE"
            )
        if attempt.fill_cash_flow is not None:
            cash_flow += attempt.fill_cash_flow
    if cash_flow != transition.cash_flow or not transition.reconciled_contract_activity:
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE")


def _inventory(items: tuple[ContractQuantity, ...]) -> dict[tuple[str, int], int]:
    inventory: dict[tuple[str, int], int] = {}
    for item in items:
        if (
            not item.symbol
            or item.multiplier != 100
            or item.signed_quantity == 0
            or (item.symbol, item.multiplier) in inventory
        ):
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
            )
        inventory[(item.symbol, item.multiplier)] = item.signed_quantity
    return inventory


def _apply_activity(
    inventory: dict[tuple[str, int], int],
    activity: tuple[ContractQuantity, ...],
) -> dict[tuple[str, int], int]:
    result = dict(inventory)
    for item in activity:
        if not item.symbol or item.multiplier != 100 or item.signed_quantity == 0:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
            )
        key = (item.symbol, item.multiplier)
        result[key] = result.get(key, 0) + item.signed_quantity
        if result[key] == 0:
            result.pop(key)
    return result


def _terminal_state(
    positions: list[PositionLifecycleEvidence],
) -> ExperimentTerminalState:
    if not positions:
        return ExperimentTerminalState.NO_POSITION
    return (
        ExperimentTerminalState.OPEN
        if positions[-1].closed_at is None
        else ExperimentTerminalState.CLOSED
    )


def _available(value: Decimal | int) -> ProjectedMetric:
    return ProjectedMetric(value=value)


def _unavailable(reason: MetricUnavailableReason) -> ProjectedMetric:
    return ProjectedMetric(value=None, unavailable_reason=reason)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
