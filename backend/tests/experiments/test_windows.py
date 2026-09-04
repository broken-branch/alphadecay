from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.experiments.windows import SQLAlchemyExperimentWindowReader
from backend.app.persistence.sqlalchemy_models import (
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AgentTickRow,
    Base,
    DevelopmentOpportunityPlanRow,
    EntryApprovalCertificateRow,
    ExecutionCertificateRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
)
from backend.app.policy.opportunity import (
    STRUCTURAL_BEARISH_OTM_PILOT_ID,
    STRUCTURAL_BULLISH_OTM_PILOT_ID,
    STRUCTURAL_BULLISH_PILOT_ID,
)

BOUNDARY = datetime(2026, 9, 2, 13, 50, tzinfo=UTC)
FINGERPRINT = "a" * 64


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _plan(
    version: int = 1,
    opportunity_key: str = STRUCTURAL_BULLISH_PILOT_ID,
) -> DevelopmentOpportunityPlanRow:
    frozen_at = BOUNDARY - timedelta(days=version)
    policy_hash = f"{version:x}" * 64
    return DevelopmentOpportunityPlanRow(
        plan_id=uuid4(),
        opportunity_key=opportunity_key,
        version=version,
        account_role="SUBMISSION",
        underlying="SPY",
        benchmark_symbol="QQQ",
        event_session=date(2026, 8, 28),
        pre_event_session=date(2026, 8, 27),
        reaction_session=date(2026, 9, 1),
        signal_session=date(2026, 9, 2),
        daily_start_session=date(2026, 8, 1),
        allowed_event_codes=["MACRO"],
        evidence_window_start=frozen_at,
        evidence_window_end=BOUNDARY,
        policy_payload={
            "selected_decision_boundary": BOUNDARY.isoformat(),
            "last_entry_boundary": (BOUNDARY + timedelta(minutes=35)).isoformat(),
            "minimum_dte": 30,
            "maximum_dte": 45,
        },
        policy_hash=policy_hash,
        request_contract={},
        request_contract_hash="b" * 64,
        thesis_code=opportunity_key,
        thesis_target_contract={},
        thesis_target_hash="c" * 64,
        exposure_limit_contract={},
        exposure_limit_hash="d" * 64,
        invalidation_codes=["THESIS_BROKEN"],
        frozen_at=frozen_at,
        plan_material={},
        plan_hash=f"{version + 4:x}" * 64,
    )


def _decision(
    plan: DevelopmentOpportunityPlanRow,
    outcome: str,
    reason: str,
) -> AgentDecisionRow:
    approved = outcome == "ENTRY_APPROVED"
    suffix = {"NO_TRADE": "6", "PROVIDER_FAILURE_NO_TRADE": "7", "ENTRY_APPROVED": "8"}[outcome]
    return AgentDecisionRow(
        decision_id=uuid4(),
        thesis_version_id=uuid4() if approved else None,
        origin_tick_id=uuid4(),
        input_snapshot_id=uuid4(),
        account_role="SUBMISSION",
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        outcome=outcome,
        reason_code=reason,
        policy_hash=plan.policy_hash,
        experiment_id=None,
        experiment_source_definition_hash=None,
        experiment_protocol_hash=None,
        result_payload={"typed": {}},
        result_hash=suffix * 64,
        autonomy_authorized=approved,
        decision_boundary=BOUNDARY,
        created_at=BOUNDARY + timedelta(minutes=2),
    )


def _approval(decision: AgentDecisionRow) -> EntryApprovalCertificateRow:
    assert decision.thesis_version_id is not None
    return EntryApprovalCertificateRow(
        approval_id=uuid4(),
        thesis_version_id=decision.thesis_version_id,
        agent_decision_id=decision.decision_id,
        account_role="SUBMISSION",
        policy_hash=decision.policy_hash,
        experiment_id=None,
        experiment_source_definition_hash=None,
        experiment_protocol_hash=None,
        book_fingerprint="9" * 64,
        envelope_hash="a" * 64,
        approved_max_loss=Decimal("200"),
        quantity=1,
        valid_from=BOUNDARY,
        expires_at=BOUNDARY + timedelta(minutes=35),
        valid=True,
    )


def _tick(decision: AgentDecisionRow) -> AgentTickRow:
    return AgentTickRow(
        tick_id=decision.origin_tick_id,
        account_role="SUBMISSION",
        account_fingerprint=FINGERPRINT,
        tick_key=f"SUBMISSION:SCHEDULER:{decision.decision_id}",
        tick_boundary=BOUNDARY,
        actor="SCHEDULER",
        status="COMPLETED",
        reservation_token=uuid4(),
        terminal_code=decision.reason_code,
        decision_id=decision.decision_id,
        execution_certificate_id=None,
        proof_hash="e" * 64,
        created_at=BOUNDARY,
        completed_at=decision.created_at + timedelta(seconds=1),
    )


def _position(
    approval: EntryApprovalCertificateRow,
    *,
    closed: bool,
) -> tuple[ManagedLifecyclePositionRow, tuple[object, ...]]:
    position_id = uuid4()
    entry_certificate_id = uuid4()
    entry_reconciliation_id = uuid4()
    current_state_id = uuid4()
    current_snapshot_id = uuid4()
    closed_at = BOUNDARY + timedelta(days=1) if closed else None
    position = ManagedLifecyclePositionRow(
        managed_position_id=position_id,
        account_role="SUBMISSION",
        account_fingerprint=FINGERPRINT,
        entry_execution_certificate_id=entry_certificate_id,
        entry_intent_id=uuid4(),
        entry_approval_id=approval.approval_id,
        thesis_version_id=approval.thesis_version_id,
        experiment_id=None,
        experiment_source_definition_hash=None,
        experiment_protocol_hash=None,
        entry_reconciliation_id=entry_reconciliation_id,
        current_reconciliation_state_id=current_state_id,
        current_snapshot_id=current_snapshot_id,
        active_position_fingerprint="e" * 64,
        activated_at=BOUNDARY + timedelta(minutes=8),
        closed_at=closed_at,
    )
    if not closed:
        return position, ()

    transition_id = uuid4()
    close_reconciliation_id = uuid4()
    close_certificate_id = uuid4()
    transition = ManagedPositionTransitionRow(
        transition_id=transition_id,
        managed_position_id=position_id,
        predecessor_transition_id=None,
        transition_sequence=1,
        action="CLOSE",
        execution_intent_id=uuid4(),
        execution_certificate_id=close_certificate_id,
        post_reconciliation_id=close_reconciliation_id,
        fill_activity_manifest=[],
        fill_activity_manifest_hash="f" * 64,
        cashflow_contribution=Decimal("315.25"),
        resulting_position_fingerprint="0" * 64,
        occurred_at=closed_at,
        market_session_id=uuid4(),
        transition_hash="1" * 64,
    )
    snapshot = ManagedPositionSnapshotRow(
        snapshot_id=current_snapshot_id,
        managed_position_id=position_id,
        predecessor_snapshot_id=None,
        transition_id=transition_id,
        reconciliation_id=close_reconciliation_id,
        reconciliation_state_id=current_state_id,
        normalized_inventory=[],
        inventory_hash="2" * 64,
        activity_manifest=[],
        activity_manifest_hash="3" * 64,
        cumulative_cashflow=Decimal("115.25"),
        rolls_on_trading_day=0,
        market_session_id=transition.market_session_id,
        position_fingerprint="0" * 64,
        accepted_at=closed_at,
        snapshot_hash="4" * 64,
    )
    certificate = ExecutionCertificateRow(
        certificate_id=close_certificate_id,
        execution_intent_id=transition.execution_intent_id,
        entry_approval_id=None,
        assessment_certificate_id=uuid4(),
        execution_status="FILLED",
        attempt_ids=[],
        actual_exposure={},
        reconciliation_checks=[],
        created_at=closed_at,
        reconciliation_id=close_reconciliation_id,
        reconciliation_hash="5" * 64,
        last_observation_hash="6" * 64,
    )
    return position, (transition, snapshot, certificate)


def test_empty_database_has_no_windows(sessions: sessionmaker[Session]) -> None:
    assert SQLAlchemyExperimentWindowReader(sessions).list() == ()


@pytest.mark.parametrize(
    ("opportunity_key", "name", "summary_start"),
    (
        (
            STRUCTURAL_BULLISH_OTM_PILOT_ID,
            "SPY structural bullish OTM pilot",
            "Bullish direction fixed before the window",
        ),
        (
            STRUCTURAL_BEARISH_OTM_PILOT_ID,
            "SPY structural bearish OTM pilot",
            "Bearish direction fixed before the window",
        ),
    ),
)
def test_registered_protocol_windows_have_plain_distinct_summaries(
    sessions: sessionmaker[Session],
    opportunity_key: str,
    name: str,
    summary_start: str,
) -> None:
    with sessions.begin() as session:
        session.add(_plan(opportunity_key=opportunity_key))

    window = SQLAlchemyExperimentWindowReader(sessions).list()[0]

    assert window.protocol.name == name
    assert window.protocol.summary.startswith(summary_start)
    assert "30–45 days to expiry, $4 wide, with defined risk." in window.protocol.summary


@pytest.mark.parametrize(
    ("outcome", "reason", "plain_reason"),
    (
        ("NO_TRADE", "OPTION_QUOTE_STALE", "The option quote was too old."),
        (
            "PROVIDER_FAILURE_NO_TRADE",
            "PROVIDER_FAILURE_NO_TRADE",
            "A required data source failed, so no trade was allowed.",
        ),
    ),
)
def test_no_trade_windows_are_plain_and_redacted(
    sessions: sessionmaker[Session],
    outcome: str,
    reason: str,
    plain_reason: str,
) -> None:
    plan = _plan()
    decision = _decision(plan, outcome, reason)
    if outcome == "PROVIDER_FAILURE_NO_TRADE":
        decision.policy_hash = "9" * 64
        decision.decision_boundary = BOUNDARY + timedelta(minutes=2)
    with sessions.begin() as session:
        session.add_all((plan, decision, _tick(decision)))

    window = SQLAlchemyExperimentWindowReader(sessions).list()[0]

    assert window.terminal_decision is not None
    assert window.terminal_decision.outcome_code == outcome
    assert window.terminal_decision.reason == plain_reason
    assert window.lifecycle is None
    assert window.protocol.summary == (
        "Bullish direction fixed before the window; one bull call debit spread, "
        "30–45 days to expiry, $4 wide, with defined risk."
    )
    serialized = window.model_dump_json()
    assert str(plan.plan_id) not in serialized
    assert plan.policy_hash not in serialized
    assert FINGERPRINT not in serialized


def test_approved_closed_window_reports_only_certified_realized_pnl(
    sessions: sessionmaker[Session],
) -> None:
    plan = _plan()
    decision = _decision(plan, "ENTRY_APPROVED", "ENTRY_APPROVED")
    approval = _approval(decision)
    position, lifecycle_rows = _position(approval, closed=True)
    with sessions.begin() as session:
        session.add_all((plan, decision, _tick(decision), approval, position, *lifecycle_rows))

    window = SQLAlchemyExperimentWindowReader(sessions).list()[0]

    assert window.terminal_decision is not None
    assert window.terminal_decision.outcome_code == "ENTRY_APPROVED"
    assert window.lifecycle is not None
    assert window.lifecycle.status == "CLOSED"
    assert window.lifecycle.exit_reason == (
        "The certified lifecycle rules closed the paper position."
    )
    assert window.lifecycle.realized_paper_pnl == Decimal("115.25")


def test_calibration_no_trade_is_bound_to_the_exact_plan(
    sessions: sessionmaker[Session],
) -> None:
    plan = _plan()
    observed_at = BOUNDARY + timedelta(seconds=2)
    calibration_hash = "7" * 64
    snapshot_id = uuid4()
    decision = AgentDecisionRow(
        decision_id=uuid4(),
        thesis_version_id=None,
        origin_tick_id=uuid4(),
        input_snapshot_id=snapshot_id,
        account_role="SUBMISSION",
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        outcome="NO_TRADE",
        reason_code="CALIBRATION_BINDING_NO_TRADE",
        policy_hash=calibration_hash,
        experiment_id=None,
        experiment_source_definition_hash=None,
        experiment_protocol_hash=None,
        result_payload={},
        result_hash="0" * 64,
        autonomy_authorized=False,
        decision_boundary=BOUNDARY,
        created_at=observed_at,
    )
    material = json.dumps(
        {
            "domain": "alphadecay.calibration-machine-binding.v1",
            "account_role": "SUBMISSION",
            "account_fingerprint": FINGERPRINT,
            "decision_code": "CALIBRATION_BINDING_NO_TRADE",
            "policy_hash": plan.policy_hash,
            "calibration_hash": calibration_hash,
            "decision_boundary": BOUNDARY.isoformat(),
            "sealed_at": observed_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    snapshot = AgentInputSnapshotRow(
        snapshot_id=snapshot_id,
        thesis_version_id=None,
        account_role="SUBMISSION",
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=BOUNDARY,
        observed_at=observed_at,
        normalized_payload={
            "machine_binding_hash": hashlib.sha256(material).hexdigest(),
            "calibration_hash": calibration_hash,
        },
        input_hash="9" * 64,
        created_at=observed_at,
    )
    with sessions.begin() as session:
        session.add_all((plan, snapshot, decision, _tick(decision)))

    window = SQLAlchemyExperimentWindowReader(sessions).list()[0]

    assert window.terminal_decision is not None
    assert window.terminal_decision.reason == (
        "The frozen calibration required the agent to stop before entry."
    )


def test_approved_open_window_reports_open_without_realized_pnl(
    sessions: sessionmaker[Session],
) -> None:
    plan = _plan()
    decision = _decision(plan, "ENTRY_APPROVED", "ENTRY_APPROVED")
    approval = _approval(decision)
    position, lifecycle_rows = _position(approval, closed=False)
    with sessions.begin() as session:
        session.add_all((plan, decision, _tick(decision), approval, position, *lifecycle_rows))

    window = SQLAlchemyExperimentWindowReader(sessions).list()[0]

    assert window.lifecycle is not None
    assert window.lifecycle.status == "OPEN"
    assert window.lifecycle.closed_at is None
    assert window.lifecycle.exit_reason is None
    assert window.lifecycle.realized_paper_pnl is None


def test_reader_groups_protocols_collapses_aborted_bursts_and_reports_tick_outcomes(
    sessions: sessionmaker[Session],
) -> None:
    first = _plan(version=6, opportunity_key=STRUCTURAL_BULLISH_OTM_PILOT_ID)
    second = _plan(version=7, opportunity_key=STRUCTURAL_BULLISH_OTM_PILOT_ID)
    first.frozen_at = BOUNDARY - timedelta(days=1)
    second.frozen_at = first.frozen_at + timedelta(seconds=30)
    first.evidence_window_start = first.frozen_at
    second.evidence_window_start = second.frozen_at
    decided = _plan(version=1, opportunity_key=STRUCTURAL_BEARISH_OTM_PILOT_ID)
    decision = _decision(decided, "ENTRY_APPROVED", "ENTRY_APPROVED")
    tick = _tick(decision)
    tick.terminal_code = "EXECUTION_BLOCKED"
    with sessions.begin() as session:
        session.add_all((first, second, decided, decision, tick))

    windows = SQLAlchemyExperimentWindowReader(sessions).list()

    aborted = next(window for window in windows if window.status == "ABORTED")
    completed = next(window for window in windows if window.status == "DECIDED")
    assert aborted.protocol.name == "SPY structural bullish OTM pilot"
    assert aborted.collapsed_versions == (6, 7)
    assert aborted.aborted_reason == "runtime never started"
    assert completed.protocol.name == "SPY structural bearish OTM pilot"
    assert completed.tick_outcome_code == "EXECUTION_BLOCKED"
    assert completed.tick_outcome_text == (
        "Entry approved, then execution was blocked before the order was sent."
    )


def test_newest_frozen_window_is_first(sessions: sessionmaker[Session]) -> None:
    old = _plan(version=2)
    recent = _plan(version=1)
    with sessions.begin() as session:
        session.add_all((old, recent))

    versions = [window.plan_version for window in SQLAlchemyExperimentWindowReader(sessions).list()]
    assert versions == [
        1,
        2,
    ]
