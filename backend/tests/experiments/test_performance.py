from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from backend.app.experiment_lineage import ExperimentExecutionLineage
from backend.app.experiments.performance import (
    ContractQuantity,
    ExperimentDecisionEvidence,
    ExperimentPerformanceEvidenceError,
    ExperimentTerminalState,
    FillAttemptEvidence,
    LifecycleTransitionEvidence,
    MetricUnavailableReason,
    PositionLifecycleEvidence,
    project_experiment_performance,
)

BOUNDARY = datetime(2026, 8, 30, 15, tzinfo=UTC)
LINEAGE = ExperimentExecutionLineage(
    experiment_id=UUID("00000000-0000-0000-0000-000000000001"),
    source_definition_hash="1" * 64,
    protocol_hash="2" * 64,
)


def test_projects_certified_closed_lifecycle_cash_flows_and_outcome() -> None:
    decisions, position = _closed_evidence()

    projection = project_experiment_performance(
        lineage=LINEAGE,
        decisions=decisions,
        positions=(position,),
    )

    assert projection.lineage == LINEAGE
    assert projection.decision_count == 3
    assert projection.opened_trade_count == 1
    assert projection.closed_trade_count == 1
    assert projection.terminal_state == ExperimentTerminalState.CLOSED
    assert projection.total_defined_maximum_risk_at_entry.value == Decimal("500")
    assert projection.entry_cash_flow.value == Decimal("-100")
    assert projection.management_cash_flow.value == Decimal("20")
    assert projection.exit_cash_flow.value == Decimal("130")
    assert projection.realized_strategy_pnl.value == Decimal("50")
    assert projection.win_count.value == 1
    assert projection.loss_count.value == 0
    assert projection.breakeven_count.value == 0


def test_open_position_never_reports_partial_cash_flow_as_realized_pnl() -> None:
    decisions, closed = _closed_evidence()
    opened = replace(closed, closed_at=None, transitions=closed.transitions[:2])

    projection = project_experiment_performance(
        lineage=LINEAGE,
        decisions=decisions[:2],
        positions=(opened,),
    )

    assert projection.terminal_state == ExperimentTerminalState.OPEN
    assert projection.opened_trade_count == 1
    assert projection.closed_trade_count == 0
    assert projection.entry_cash_flow.value == Decimal("-100")
    assert projection.management_cash_flow.value == Decimal("20")
    assert projection.exit_cash_flow.value is None
    assert (
        projection.realized_strategy_pnl.unavailable_reason
        == MetricUnavailableReason.NO_CLOSED_TRADES
    )
    assert projection.win_count.value is None


def test_no_trade_decision_reports_values_as_unavailable_instead_of_zero_return() -> None:
    decision = _decision(1, 0)

    projection = project_experiment_performance(
        lineage=LINEAGE,
        decisions=(decision,),
        positions=(),
    )

    assert projection.decision_count == 1
    assert projection.opened_trade_count == 0
    assert projection.terminal_state == ExperimentTerminalState.NO_POSITION
    assert (
        projection.total_defined_maximum_risk_at_entry.unavailable_reason
        == MetricUnavailableReason.NO_OPENED_TRADES
    )
    assert projection.entry_cash_flow.value is None
    assert (
        projection.realized_strategy_pnl.unavailable_reason
        == MetricUnavailableReason.NO_CLOSED_TRADES
    )


def test_rejects_mixed_experiment_identity_instead_of_filtering_it() -> None:
    decisions, position = _closed_evidence()
    other_lineage = ExperimentExecutionLineage(
        experiment_id=UUID("00000000-0000-0000-0000-000000000099"),
        source_definition_hash="1" * 64,
        protocol_hash="2" * 64,
    )

    with pytest.raises(
        ExperimentPerformanceEvidenceError,
        match="EXPERIMENT_PERFORMANCE_LINEAGE_MISMATCH",
    ):
        project_experiment_performance(
            lineage=LINEAGE,
            decisions=decisions,
            positions=(replace(position, lineage=other_lineage),),
        )


def test_rejects_mixed_assessment_authorization_lineage() -> None:
    decisions, position = _closed_evidence()
    other_lineage = ExperimentExecutionLineage(
        experiment_id=LINEAGE.experiment_id,
        source_definition_hash=LINEAGE.source_definition_hash,
        protocol_hash="9" * 64,
    )
    corrupted_close = replace(position.transitions[-1], authorization_lineage=other_lineage)

    with pytest.raises(
        ExperimentPerformanceEvidenceError,
        match="EXPERIMENT_PERFORMANCE_LINEAGE_MISMATCH",
    ):
        project_experiment_performance(
            lineage=LINEAGE,
            decisions=decisions,
            positions=(
                replace(
                    position,
                    transitions=position.transitions[:-1] + (corrupted_close,),
                ),
            ),
        )


@pytest.mark.parametrize(
    ("corrupt_transition", "error_code"),
    [
        (
            lambda step: replace(step, snapshot_cumulative_cash_flow=Decimal("999")),
            "EXPERIMENT_PERFORMANCE_CASH_FLOW_LINEAGE_INCOMPLETE",
        ),
        (
            lambda step: replace(
                step,
                resulting_inventory=(ContractQuantity("SPY260911C00410000", 1),),
            ),
            "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE",
        ),
        (
            lambda step: replace(
                step,
                attempts=(replace(step.attempts[0], filled_quantity=0),),
            ),
            "EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE",
        ),
    ],
)
def test_rejects_incomplete_certification_before_projecting_realized_pnl(
    corrupt_transition,
    error_code: str,
) -> None:
    decisions, position = _closed_evidence()
    corrupted_close = corrupt_transition(position.transitions[-1])

    with pytest.raises(ExperimentPerformanceEvidenceError, match=error_code):
        project_experiment_performance(
            lineage=LINEAGE,
            decisions=decisions,
            positions=(
                replace(
                    position,
                    transitions=position.transitions[:-1] + (corrupted_close,),
                ),
            ),
        )


def _closed_evidence() -> tuple[tuple[ExperimentDecisionEvidence, ...], PositionLifecycleEvidence]:
    decisions = tuple(_decision(index, index) for index in range(1, 4))
    entry = _transition(
        number=1,
        predecessor=None,
        action="ENTRY",
        minute=1,
        activity=(
            ContractQuantity("SPY260904C00400000", 1),
            ContractQuantity("SPY260904C00405000", -1),
        ),
        inventory=(
            ContractQuantity("SPY260904C00400000", 1),
            ContractQuantity("SPY260904C00405000", -1),
        ),
        cash_flow=Decimal("-100"),
        cumulative=Decimal("-100"),
    )
    roll = _transition(
        number=2,
        predecessor=entry.transition_id,
        action="ROLL",
        minute=2,
        activity=(
            ContractQuantity("SPY260904C00400000", -1),
            ContractQuantity("SPY260904C00405000", 1),
            ContractQuantity("SPY260911C00410000", 1),
            ContractQuantity("SPY260911C00415000", -1),
        ),
        inventory=(
            ContractQuantity("SPY260911C00410000", 1),
            ContractQuantity("SPY260911C00415000", -1),
        ),
        cash_flow=Decimal("20"),
        cumulative=Decimal("-80"),
    )
    close = _transition(
        number=3,
        predecessor=roll.transition_id,
        action="CLOSE",
        minute=3,
        activity=(
            ContractQuantity("SPY260911C00410000", -1),
            ContractQuantity("SPY260911C00415000", 1),
        ),
        inventory=(),
        cash_flow=Decimal("130"),
        cumulative=Decimal("50"),
    )
    return decisions, PositionLifecycleEvidence(
        managed_position_id=_uuid(50),
        lineage=LINEAGE,
        entry_decision_id=decisions[0].decision_id,
        entry_approval_id=_uuid(60),
        entry_approval_lineage=LINEAGE,
        defined_maximum_risk=Decimal("500"),
        activated_at=entry.occurred_at,
        closed_at=close.occurred_at,
        transitions=(entry, roll, close),
    )


def _transition(
    *,
    number: int,
    predecessor: UUID | None,
    action: str,
    minute: int,
    activity: tuple[ContractQuantity, ...],
    inventory: tuple[ContractQuantity, ...],
    cash_flow: Decimal,
    cumulative: Decimal,
) -> LifecycleTransitionEvidence:
    attempt_id = _uuid(100 + number)
    return LifecycleTransitionEvidence(
        transition_id=_uuid(10 + number),
        predecessor_transition_id=predecessor,
        sequence=number - 1,
        action=action,
        decision_id=_uuid(number),
        authorization_id=_uuid(60 if action == "ENTRY" else 60 + number),
        authorization_lineage=LINEAGE,
        occurred_at=BOUNDARY + timedelta(minutes=minute),
        intent_action=action,
        authorized_quantity=1,
        execution_status="FILLED",
        certificate_attempt_ids=(attempt_id,),
        attempts=(
            FillAttemptEvidence(
                attempt_id=attempt_id,
                attempt_ordinal=0,
                requested_quantity=1,
                filled_quantity=1,
                fill_cash_flow=cash_flow,
            ),
        ),
        reconciled_contract_activity=activity,
        resulting_inventory=inventory,
        cash_flow=cash_flow,
        snapshot_cumulative_cash_flow=cumulative,
    )


def _decision(number: int, minute: int) -> ExperimentDecisionEvidence:
    return ExperimentDecisionEvidence(
        decision_id=_uuid(number),
        lineage=LINEAGE,
        occurred_at=BOUNDARY + timedelta(minutes=minute),
    )


def _uuid(number: int) -> UUID:
    return UUID(int=number)
