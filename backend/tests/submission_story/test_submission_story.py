import hashlib
import json
import stat
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.contracts.v1 import (
    AccountRole,
    DataQuality,
    OptionRight,
    PositionIntent,
)
from backend.app.experiment_lineage import ExperimentExecutionLineage
from backend.app.persistence.agent_authority import (
    agent_input_material,
    agent_result_material,
    canonical_agent_hash,
)
from backend.app.persistence.agent_codec import encode_agent_value
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    Base,
    DevelopmentOpportunityPlanRow,
    SubmissionBaselineRow,
)
from backend.app.policy.opportunity import (
    AccountOpportunityState,
    CatalystQuality,
    InstrumentKind,
    OpportunityDecisionRecord,
    OpportunityDirection,
    OpportunityInput,
    OpportunityOutcome,
    OpportunityReason,
    OptionFeed,
    OptionLeg,
    TradingHaltState,
    VerticalCandidate,
    VerticalStrategy,
)
from backend.app.services.acquisition import AcquisitionKind, CalibrationBinding
from backend.app.services.agent import AgentDecision
from backend.app.submission_story.export import build_judge_story, render_judge_markdown
from backend.app.submission_story.models import (
    EntryExecutionSummary,
    LifecycleAssessmentSummary,
    OrderLifecycleSummary,
    PriceSummary,
    ProviderRetryAuditSummary,
    SelectedSpreadSummary,
    StrategySummary,
    TerminalOutcomeSummary,
)
from backend.app.submission_story.repository import (
    SQLAlchemySubmissionStoryRepository,
    SubmissionStoryError,
    _approved_story,
    _assert_private_story_safe,
    _calibration_machine_hash,
    _calibration_no_trade_story,
    _closed_exit_evidence,
    _experiment_lineage,
    _filled_story,
    _lifecycle_assessment_summaries,
    _lifecycle_provider_failure_summaries,
    _no_trade_story,
    _provider_retry_audit,
    _terminal_entry_story,
    _unterminated_entry_story,
    _valid_entry_thesis_chronology,
    build_public_preview,
)
from ops.launch.submission_story import main

BOUNDARY = datetime(2026, 9, 1, 15, tzinfo=UTC)
FINGERPRINT = "a" * 64
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "judge_story_cases.json"


def _option(
    symbol: str,
    strike: str,
    intent: PositionIntent,
) -> OptionLeg:
    return OptionLeg(
        instrument_kind=InstrumentKind.OPTION,
        symbol=symbol,
        underlying="SPY",
        right=OptionRight.CALL,
        strike=Decimal(strike),
        expiry=date(2026, 9, 4),
        intent=intent,
        ratio=1,
        multiplier=100,
        active=True,
        tradable=True,
        bid=Decimal("1.00"),
        ask=Decimal("1.10"),
        bid_size=10,
        ask_size=10,
        quote_at=BOUNDARY,
        feed=OptionFeed.INDICATIVE_MODIFIED,
        greeks_complete=True,
        greek_units_verified=True,
    )


def _values(*, candidate: bool) -> OpportunityInput:
    selected = (
        VerticalCandidate(
            strategy=VerticalStrategy.BULL_CALL_DEBIT,
            legs=(
                _option("SPY260904C00400000", "400", PositionIntent.BUY_TO_OPEN),
                _option("SPY260904C00405000", "405", PositionIntent.SELL_TO_OPEN),
            ),
            quantity=1,
            dte=3,
            approved_limit=Decimal("1.25"),
            candidate_score=90,
            selection_rank=1,
            buying_power_sufficient=True,
        )
        if candidate
        else None
    )
    return OpportunityInput(
        opportunity_key="SPY_EVENT_V1",
        underlying="SPY",
        observed_decision_boundary=BOUNDARY,
        evaluated_at=BOUNDARY + timedelta(seconds=2),
        completed_bar_at=BOUNDARY,
        decision_boundary_complete=True,
        prior_decision_outcome=None,
        data_quality=DataQuality.COMPLETE,
        market_open=True,
        trading_halted=TradingHaltState.NOT_HALTED,
        underlying_observed_at=BOUNDARY,
        catalyst_observed_at=BOUNDARY,
        catalyst_quality=CatalystQuality.CLEAR,
        catalyst_score=90,
        vwap_distance=Decimal("0.01"),
        relative_return=Decimal("0.01"),
        beta=Decimal("1"),
        bull_trend_hits=3,
        bear_trend_hits=0,
        absolute_first_reaction=Decimal("0.01"),
        candidate=selected,
        account=AccountOpportunityState(
            account_role=AccountRole.SUBMISSION,
            book_fingerprint="b" * 64,
            baseline_clean=True,
            clean_equity=Decimal("100000"),
            open_position_count=0,
            open_order_count=0,
            filled_entry_count=0,
            lifetime_approved_risk=Decimal("0"),
            entry_reservation_active=False,
            reserved_approved_risk=Decimal("0"),
            event_already_attempted=False,
        ),
    )


def _agent(*, approved: bool) -> tuple[AgentDecisionRow, AgentDecision, OpportunityInput]:
    values = _values(candidate=approved)
    outcome = OpportunityOutcome.ENTRY_APPROVED if approved else OpportunityOutcome.NO_TRADE
    reasons = (
        (OpportunityReason.ENTRY_APPROVED,)
        if approved
        else (OpportunityReason.DIRECTION_NOT_CONFIRMED,)
    )
    record = OpportunityDecisionRecord(
        outcome=outcome,
        reason_codes=reasons,
        opportunity_key=values.opportunity_key,
        decision_boundary=BOUNDARY,
        direction=OpportunityDirection.BULLISH if approved else None,
        strategy=VerticalStrategy.BULL_CALL_DEBIT if approved else None,
        quantity=1 if approved else None,
        approved_max_loss=Decimal("240") if approved else None,
        book_fingerprint=values.account.book_fingerprint,
        candidate_hash="c" * 64 if approved else None,
        input_hash="d" * 64,
        policy_hash="e" * 64,
        result_hash="f" * 64,
    )
    agent = AgentDecision(
        code=outcome.value,
        decided_at=values.evaluated_at,
        thesis_version_id=uuid4() if approved else None,
        opportunity=record,
        normalized_input=values,
    )
    row = AgentDecisionRow(
        decision_id=uuid4(),
        thesis_version_id=agent.thesis_version_id,
        origin_tick_id=uuid4(),
        input_snapshot_id=uuid4(),
        account_role="SUBMISSION",
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        outcome=outcome.value,
        reason_code=reasons[0].value,
        policy_hash="e" * 64,
        result_payload={},
        result_hash="f" * 64,
        autonomy_authorized=approved,
        decision_boundary=BOUNDARY,
        created_at=values.evaluated_at,
    )
    return row, agent, values


def _spread() -> SelectedSpreadSummary:
    return SelectedSpreadSummary(
        option_type="CALL",
        expiration=date(2026, 9, 4),
        long_strike=Decimal("400"),
        short_strike=Decimal("405"),
        quantity=1,
        price=PriceSummary(order_type="NET_DEBIT_LIMIT", limit_per_share=Decimal("1.25")),
    )


def _strategy() -> StrategySummary:
    return StrategySummary(
        name="SPY_EVENT_V1",
        version=1,
        underlying="SPY",
        frozen_at=BOUNDARY - timedelta(days=1),
    )


def _approved() -> object:
    row, agent, values = _agent(approved=True)
    return _approved_story(
        _strategy(), row, agent, values, _spread(), "ORDER_ACTIVITY_NOT_RECORDED"
    )


def _recorded_fill_attempt() -> SimpleNamespace:
    return SimpleNamespace(
        attempt_ordinal=0,
        state="FILLED",
        filled_quantity=1,
        quantity=1,
    )


def test_no_trade_story_stops_without_spread_or_account_change() -> None:
    row, agent, values = _agent(approved=False)
    story = _no_trade_story(_strategy(), row, agent, values)

    assert story.outcome == "NO_TRADE"
    assert story.selected_spread is None
    assert story.order_lifecycle_status == "NO_ORDER_AUTHORIZED"
    assert story.account_impact.status == "NO_MUTATION_AUTHORIZED"
    assert story.account_impact.pnl_status == "NOT_RECORDED"
    assert story.trading_mode == "PAPER_ONLY_APPLICATION_CONTRACT"
    assert story.trading_mode_evidence == "NOT_RECORDED_IN_LINEAGE"


def test_calibration_no_trade_story_stops_before_market_acquisition() -> None:
    decision = _decision("SUBMISSION", BOUNDARY, 0)
    story = _calibration_no_trade_story(_strategy(), decision)

    assert story.outcome == "NO_TRADE"
    assert story.selected_spread is None
    assert story.account_impact.status == "NO_MUTATION_AUTHORIZED"
    assert "authorized no provider acquisition" in story.account_impact.description


def test_repository_derives_calibration_no_trade_from_persisted_authority() -> None:
    sessions = _sessions()
    plan_id = uuid4()
    baseline_id = uuid4()
    sealed_at = BOUNDARY + timedelta(seconds=3)
    plan = DevelopmentOpportunityPlanRow(
        plan_id=plan_id,
        opportunity_key="SPY_EVENT_V1",
        version=1,
        account_role="SUBMISSION",
        underlying="SPY",
        benchmark_symbol="QQQ",
        event_session=date(2026, 8, 28),
        pre_event_session=date(2026, 8, 27),
        reaction_session=date(2026, 8, 31),
        signal_session=date(2026, 9, 1),
        daily_start_session=date(2026, 8, 1),
        allowed_event_codes=["EARNINGS"],
        evidence_window_start=BOUNDARY - timedelta(days=1),
        evidence_window_end=BOUNDARY,
        policy_payload={},
        policy_hash="b" * 64,
        request_contract={},
        request_contract_hash="1" * 64,
        thesis_code="SPY_EVENT",
        thesis_target_contract={},
        thesis_target_hash="2" * 64,
        exposure_limit_contract={},
        exposure_limit_hash="3" * 64,
        invalidation_codes=["THESIS_BROKEN"],
        frozen_at=BOUNDARY - timedelta(days=2),
        plan_material={},
        plan_hash="4" * 64,
    )
    snapshot = AgentInputSnapshotRow(
        snapshot_id=uuid4(),
        thesis_version_id=None,
        account_role="SUBMISSION",
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=BOUNDARY,
        observed_at=sealed_at,
        normalized_payload={},
        input_hash="0" * 64,
        created_at=sealed_at,
    )
    decision = AgentDecisionRow(
        decision_id=uuid4(),
        thesis_version_id=None,
        origin_tick_id=uuid4(),
        input_snapshot_id=snapshot.snapshot_id,
        account_role="SUBMISSION",
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        outcome="NO_TRADE",
        reason_code="CALIBRATION_BINDING_NO_TRADE",
        policy_hash="c" * 64,
        result_payload={},
        result_hash="0" * 64,
        autonomy_authorized=False,
        decision_boundary=BOUNDARY,
        created_at=sealed_at,
    )
    snapshot.normalized_payload = {
        "machine_binding_hash": _calibration_machine_hash(plan.policy_hash, decision, snapshot),
        "calibration_hash": decision.policy_hash,
    }
    snapshot.input_hash = canonical_agent_hash(
        agent_input_material(
            account_role=snapshot.account_role,
            account_fingerprint=snapshot.account_fingerprint,
            decision_kind=snapshot.decision_kind,
            decision_boundary=BOUNDARY,
            observed_at=sealed_at,
            normalized_input=snapshot.normalized_payload,
            thesis_version_id=None,
        )
    )
    decision.result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash=snapshot.input_hash,
            outcome=decision.outcome,
            reason_code=decision.reason_code,
            policy_hash=decision.policy_hash,
            thesis_version_id=None,
            result_payload={},
            authorization_id=None,
            intent_id=None,
            intent_digest=None,
            autonomy_authorized=False,
        )
    )
    decision.decision_id = uuid5(NAMESPACE_URL, f"alphadecay:agent-decision:{decision.result_hash}")
    with sessions.begin() as session:
        session.add_all(
            (
                AccountRoleRow(
                    role="SUBMISSION",
                    account_fingerprint=FINGERPRINT,
                    equity=Decimal("100000"),
                    autonomous_enabled=False,
                ),
                SubmissionBaselineRow(
                    baseline_id=baseline_id,
                    account_role="SUBMISSION",
                    account_fingerprint=FINGERPRINT,
                    equity=Decimal("100000"),
                    captured_at=BOUNDARY - timedelta(days=3),
                    positions_hash="5" * 64,
                    orders_hash="6" * 64,
                    activities_hash="7" * 64,
                    contaminated=False,
                ),
                plan,
                snapshot,
                decision,
            )
        )

    repository = SQLAlchemySubmissionStoryRepository(sessions)
    repository._opportunity = SimpleNamespace(
        load_plan=lambda *_args, **_kwargs: SimpleNamespace(
            persisted=SimpleNamespace(plan_id=plan_id, policy_hash=plan.policy_hash),
            spec=SimpleNamespace(underlying="SPY"),
        ),
        load_baseline=lambda *_args, **_kwargs: SimpleNamespace(
            seal=SimpleNamespace(
                account_role=AccountRole.SUBMISSION,
                account_fingerprint=FINGERPRINT,
            ),
            persisted=SimpleNamespace(submission_baseline_id=baseline_id),
        ),
    )

    story = repository.latest()

    assert story.strategy == _strategy().model_copy(update={"frozen_at": plan.frozen_at})
    assert story.outcome == "NO_TRADE"
    assert story.order_lifecycle_status == "NO_ORDER_AUTHORIZED"


def test_approved_without_fill_preserves_private_limit_authority() -> None:
    story = _approved()

    assert story.outcome == "APPROVED_UNFILLED"
    assert story.selected_spread.price.limit_per_share == Decimal("1.25")
    assert story.risk_limits.maximum_loss_usd == Decimal("240")
    assert story.account_impact.reconciled_cashflow_usd is None


def test_entry_thesis_may_be_sealed_at_scheduler_tick_after_completed_boundary() -> None:
    decision = SimpleNamespace(
        decision_boundary=BOUNDARY,
        created_at=BOUNDARY + timedelta(minutes=5, seconds=2),
    )
    snapshot = SimpleNamespace(observed_at=BOUNDARY + timedelta(minutes=5))
    thesis = SimpleNamespace(
        frozen_at=BOUNDARY + timedelta(minutes=5),
        target_at=BOUNDARY + timedelta(days=1),
    )

    assert _valid_entry_thesis_chronology(decision, snapshot, thesis)


@pytest.mark.parametrize(
    ("frozen_at", "observed_at", "created_at", "target_at"),
    (
        (
            BOUNDARY - timedelta(microseconds=1),
            BOUNDARY + timedelta(minutes=5),
            BOUNDARY + timedelta(minutes=5, seconds=2),
            BOUNDARY + timedelta(days=1),
        ),
        (
            BOUNDARY + timedelta(minutes=5, microseconds=1),
            BOUNDARY + timedelta(minutes=5),
            BOUNDARY + timedelta(minutes=5, seconds=2),
            BOUNDARY + timedelta(days=1),
        ),
        (
            BOUNDARY + timedelta(minutes=5),
            BOUNDARY + timedelta(minutes=5),
            BOUNDARY + timedelta(minutes=4, seconds=59),
            BOUNDARY + timedelta(days=1),
        ),
        (
            BOUNDARY + timedelta(minutes=5),
            BOUNDARY + timedelta(minutes=5),
            BOUNDARY + timedelta(minutes=5, seconds=2),
            BOUNDARY + timedelta(minutes=5),
        ),
    ),
)
def test_entry_thesis_chronology_rejects_invalid_ordering(
    frozen_at: datetime,
    observed_at: datetime,
    created_at: datetime,
    target_at: datetime,
) -> None:
    decision = SimpleNamespace(decision_boundary=BOUNDARY, created_at=created_at)
    snapshot = SimpleNamespace(observed_at=observed_at)
    thesis = SimpleNamespace(frozen_at=frozen_at, target_at=target_at)

    assert not _valid_entry_thesis_chronology(decision, snapshot, thesis)


def test_filled_open_story_reports_reconciled_simulated_account_impact() -> None:
    row, agent, values = _agent(approved=True)
    story = _filled_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        closed=False,
        reconciled_cashflow=Decimal("-125"),
        assessment_present=True,
        attempts=(_recorded_fill_attempt(),),
    )

    assert story.outcome == "FILLED_OPEN"
    assert story.order_lifecycle_status == "FILLED_POSITION_OPEN"
    assert story.account_impact.status == "RECONCILED_SIMULATED_POSITION_OPEN"
    assert story.account_impact.reconciled_cashflow_usd == Decimal("-125")


def test_judge_story_narrates_submitted_filled_and_reconciled_entry() -> None:
    row, agent, values = _agent(approved=True)
    submitted_at = BOUNDARY + timedelta(minutes=5)
    filled_at = submitted_at + timedelta(milliseconds=200)
    reconciled_at = submitted_at + timedelta(hours=1)
    story = _filled_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        closed=False,
        reconciled_cashflow=Decimal("-125"),
        assessment_present=True,
        attempts=(_recorded_fill_attempt(),),
        entry_execution=EntryExecutionSummary(
            submitted_at=submitted_at,
            filled_at=filled_at,
            reconciled_at=reconciled_at,
            reconciliation_sequence=2,
        ),
        lifecycle_assessments=(
            LifecycleAssessmentSummary(
                action="NO_ACTION",
                reason_code="PROVIDER_FAILURE_NO_ACTION",
                assessed_at=reconciled_at + timedelta(minutes=15),
            ),
        ),
        terminal=TerminalOutcomeSummary(
            scope="ENTRY",
            certificate_recording_status="RECORDED",
            certificate_status="FILLED",
            certificate_time=reconciled_at,
            outcome_status="FILLED_OPEN",
            outcome_time=reconciled_at,
        ),
    )

    export = build_judge_story(story)
    timeline = {event.stage: event for event in export.timeline}

    assert timeline["ENTRY_ORDER_SUBMITTED"].occurred_at == submitted_at
    assert timeline["ENTRY_FILL"].occurred_at == filled_at
    assert timeline["ENTRY_RECONCILIATION"].occurred_at == reconciled_at
    assert "Reconciliation state 2" in timeline["ENTRY_RECONCILIATION"].description
    assert timeline["POSITION_MATERIALIZED"].status == "OPEN"
    lifecycle = next(event for event in export.timeline if event.stage == "LIFECYCLE_TICK")
    assert lifecycle.reason_codes == ("CHECK_AUTHORIZED_NO_ACTION",)
    assert export.outcome == "FILLED_OPEN"


def test_managed_closed_story_reports_terminal_lifecycle() -> None:
    row, agent, values = _agent(approved=True)
    story = _filled_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        closed=True,
        reconciled_cashflow=Decimal("40"),
        assessment_present=True,
        attempts=(_recorded_fill_attempt(),),
    )

    assert story.outcome == "CLOSED"
    assert story.order_lifecycle_status == "MANAGED_POSITION_CLOSED"
    assert "managed close" in story.what_changed_next[0]
    assert story.account_impact.realized_pnl_status == "CERTIFIED"
    assert story.account_impact.realized_pnl_usd == Decimal("40")


def test_filled_position_with_working_exit_is_not_reported_closed() -> None:
    row, agent, values = _agent(approved=True)
    story = _filled_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        closed=False,
        reconciled_cashflow=Decimal("-125"),
        assessment_present=True,
        lifecycle_status="EXIT_WORKING",
        lifecycle_outcome="EXIT_WORKING",
        impact_description=(
            "The simulated position is reconciled while its zero-fill paper exit remains working."
        ),
        attempts=(_recorded_fill_attempt(),),
    )

    assert story.outcome == "EXIT_WORKING"
    assert story.order_lifecycle_status == "EXIT_WORKING"
    assert story.account_impact.reconciled_cashflow_usd == Decimal("-125")


def test_private_story_carries_exact_optional_experiment_lineage_only() -> None:
    row, agent, values = _agent(approved=False)
    lineage = ExperimentExecutionLineage(
        experiment_id=uuid4(),
        source_definition_hash="1" * 64,
        protocol_hash="2" * 64,
    )
    row.experiment_id = lineage.experiment_id
    row.experiment_source_definition_hash = lineage.source_definition_hash
    row.experiment_protocol_hash = lineage.protocol_hash
    story = _no_trade_story(_strategy(), row, agent, values).model_copy(
        update={"experiment_execution_lineage": _experiment_lineage(row)}
    )

    _assert_private_story_safe(story)
    private_payload = story.model_dump_json()
    public_payload = build_public_preview(story).model_dump_json()

    assert story.experiment_execution_lineage == lineage
    assert str(lineage.experiment_id) in private_payload
    assert lineage.source_definition_hash in private_payload
    assert str(lineage.experiment_id) not in public_payload
    assert lineage.source_definition_hash not in public_payload
    assert "experiment_execution_lineage" not in public_payload


def test_filled_closing_story_keeps_assessments_exit_attempts_and_terminal_status() -> None:
    row, agent, values = _agent(approved=True)
    assessments = (
        LifecycleAssessmentSummary(
            action="HOLD",
            reason_code="HOLD_CERTIFIED",
            assessed_at=BOUNDARY + timedelta(minutes=5),
        ),
        LifecycleAssessmentSummary(
            action="CLOSE",
            reason_code="CLOSE_RISK_ONLY",
            assessed_at=BOUNDARY + timedelta(minutes=10),
        ),
    )
    exit_lifecycle = OrderLifecycleSummary(
        recording_status="RECORDED",
        attempts=(
            {
                "ordinal": 0,
                "state": "NEW",
                "filled_quantity": 0,
                "quantity": 1,
            },
        ),
    )
    terminal = TerminalOutcomeSummary(
        scope="EXIT",
        certificate_recording_status="NOT_RECORDED",
        outcome_status="EXIT_WORKING",
        outcome_time=BOUNDARY + timedelta(minutes=10),
    )

    story = _filled_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        closed=False,
        reconciled_cashflow=Decimal("-125"),
        assessment_present=True,
        lifecycle_status="EXIT_WORKING",
        lifecycle_outcome="EXIT_WORKING",
        attempts=(_recorded_fill_attempt(),),
        lifecycle_assessments=assessments,
        exit_order_lifecycle=exit_lifecycle,
        terminal=terminal,
    )

    assert story.lifecycle_assessments == assessments
    assert story.exit_order_lifecycle == exit_lifecycle
    assert story.terminal == terminal
    assert story.account_impact.realized_pnl_status == "UNAVAILABLE"
    assert story.account_impact.realized_pnl_usd is None


def test_lifecycle_assessments_must_be_chronological() -> None:
    row, agent, values = _agent(approved=True)
    assessments = (
        LifecycleAssessmentSummary(
            action="CLOSE",
            reason_code="CLOSE_APPROVED",
            assessed_at=BOUNDARY + timedelta(minutes=10),
        ),
        LifecycleAssessmentSummary(
            action="HOLD",
            reason_code="HOLD_CERTIFIED",
            assessed_at=BOUNDARY + timedelta(minutes=5),
        ),
    )

    with pytest.raises(ValueError, match="not chronological"):
        _filled_story(
            _strategy(),
            row,
            agent,
            values,
            _spread(),
            closed=False,
            reconciled_cashflow=Decimal("-125"),
            assessment_present=True,
            attempts=(_recorded_fill_attempt(),),
            lifecycle_assessments=assessments,
        )


def test_repository_maps_exact_lifecycle_assessment_action_reason_and_time() -> None:
    lineage = ExperimentExecutionLineage(
        experiment_id=uuid4(),
        source_definition_hash="1" * 64,
        protocol_hash="2" * 64,
    )
    rows = (
        SimpleNamespace(
            decision_boundary=BOUNDARY + timedelta(minutes=5),
            reason_code="HOLD_CERTIFIED",
            experiment_id=lineage.experiment_id,
            experiment_source_definition_hash=lineage.source_definition_hash,
            experiment_protocol_hash=lineage.protocol_hash,
        ),
        SimpleNamespace(
            decision_boundary=BOUNDARY + timedelta(minutes=10),
            reason_code="CLOSE_RISK_ONLY",
            experiment_id=lineage.experiment_id,
            experiment_source_definition_hash=lineage.source_definition_hash,
            experiment_protocol_hash=lineage.protocol_hash,
        ),
    )
    projections = (
        SimpleNamespace(
            action=SimpleNamespace(value="HOLD"),
            occurred_at=BOUNDARY + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action=SimpleNamespace(value="CLOSE"),
            occurred_at=BOUNDARY + timedelta(minutes=10),
        ),
    )
    session = SimpleNamespace(scalars=lambda _query: rows)

    assessments = _lifecycle_assessment_summaries(
        session,
        position=SimpleNamespace(
            account_fingerprint=FINGERPRINT,
            managed_position_id=uuid4(),
        ),
        thesis=SimpleNamespace(thesis_version_id=uuid4()),
        projections=projections,
        expected_lineage=lineage,
    )

    assert tuple(item.action for item in assessments) == ("HOLD", "CLOSE")
    assert tuple(item.reason_code for item in assessments) == (
        "HOLD_CERTIFIED",
        "CLOSE_RISK_ONLY",
    )
    assert tuple(item.assessed_at for item in assessments) == tuple(
        item.occurred_at for item in projections
    )


def test_repository_keeps_unbound_provider_failure_during_open_position(monkeypatch) -> None:
    row = SimpleNamespace(
        input_snapshot_id=uuid4(),
        reason_code="PROVIDER_FAILURE_NO_ACTION",
        created_at=BOUNDARY + timedelta(hours=1),
        autonomy_authorized=False,
        experiment_id=None,
        experiment_source_definition_hash=None,
        experiment_protocol_hash=None,
    )
    snapshot = SimpleNamespace()

    class FakeSession:
        def scalars(self, _query: object) -> tuple[SimpleNamespace, ...]:
            return (row,)

        def get(self, _model: object, identity: object) -> object | None:
            return snapshot if identity == row.input_snapshot_id else None

    monkeypatch.setattr(
        "backend.app.submission_story.repository._validate_decision_hashes",
        lambda *_args: None,
    )
    summaries = _lifecycle_provider_failure_summaries(
        FakeSession(),
        position=SimpleNamespace(
            account_fingerprint=FINGERPRINT,
            activated_at=BOUNDARY,
            closed_at=None,
        ),
    )

    assert summaries == (
        LifecycleAssessmentSummary(
            action="NO_ACTION",
            reason_code="PROVIDER_FAILURE_NO_ACTION",
            assessed_at=row.created_at,
        ),
    )


def test_closed_exit_requires_attempt_and_filled_terminal_certificate() -> None:
    intent_id = uuid4()
    authorization_id = uuid4()
    certificate_id = uuid4()
    client_order_id = "alphadecay-close-a0"
    intent = SimpleNamespace(
        intent_id=intent_id,
        assessment_certificate_id=authorization_id,
        action="CLOSE",
        state="TERMINAL",
        quantity=1,
    )
    authorization = SimpleNamespace(
        certificate_id=authorization_id,
        action="CLOSE",
        experiment_id=None,
        experiment_source_definition_hash=None,
        experiment_protocol_hash=None,
    )
    attempt = SimpleNamespace(
        attempt_id=uuid4(),
        execution_intent_id=intent_id,
        attempt_ordinal=0,
        client_order_id=client_order_id,
        provider_order_id="paper-order",
        state="FILLED",
        filled_quantity=1,
        quantity=1,
        replaces_attempt_id=None,
    )
    certificate = SimpleNamespace(
        certificate_id=certificate_id,
        execution_intent_id=intent_id,
        entry_approval_id=None,
        assessment_certificate_id=authorization_id,
        execution_status="FILLED",
        attempt_ids=[client_order_id],
        reconciliation_checks=[
            "TERMINAL",
            "REMAINDER_ABSENT",
            "WHOLE_ACCOUNT_RECONCILED",
        ],
        reconciliation_id=uuid4(),
        reconciliation_hash="3" * 64,
        last_observation_hash="4" * 64,
        actual_exposure={},
        created_at=BOUNDARY + timedelta(minutes=15),
    )
    transition = SimpleNamespace(
        action="CLOSE",
        execution_intent_id=intent_id,
        execution_certificate_id=certificate_id,
        occurred_at=BOUNDARY + timedelta(minutes=15),
    )

    class FakeSession:
        def get(self, model: object, identity: object) -> object | None:
            del identity
            return {
                "ExecutionIntentRow": intent,
                "ExecutionCertificateRow": certificate,
                "AssessmentCertificateRow": authorization,
            }.get(model.__name__)

        def scalars(self, _query: object) -> tuple[SimpleNamespace, ...]:
            return (attempt,)

    lifecycle, terminal = _closed_exit_evidence(
        FakeSession(),
        transition=transition,
        outcome_time=transition.occurred_at,
        expected_lineage=None,
    )

    assert lifecycle.recording_status == "RECORDED"
    assert lifecycle.attempts[0].state == "FILLED"
    assert terminal.certificate_status == "FILLED"
    assert terminal.outcome_status == "CLOSED"


def test_missing_order_activity_is_not_reported_as_no_fill() -> None:
    story = _approved()

    assert story.order_lifecycle_status == "ORDER_ACTIVITY_NOT_RECORDED"
    assert story.account_impact.status == "BROKER_EFFECT_NOT_RECORDED"
    assert story.account_impact.cashflow_status == "NOT_RECORDED"
    assert story.account_impact.pnl_status == "NOT_RECORDED"
    assert story.alternatives_recording == "NOT_RECORDED"
    assert story.alternatives_rejected == (
        "Named rejected alternatives were not recorded in the decision authority.",
    )


def test_working_partial_recovery_and_permanent_states_remain_distinct() -> None:
    row, agent, values = _agent(approved=True)
    intent = SimpleNamespace(state="CLAIMED")
    working = SimpleNamespace(attempt_ordinal=0, state="NEW", filled_quantity=0, quantity=2)
    partial = SimpleNamespace(
        attempt_ordinal=0, state="PARTIALLY_FILLED", filled_quantity=1, quantity=2
    )
    healthy = SimpleNamespace(recovery_pending=False, execution_locked=False)

    working_story = _unterminated_entry_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        intent=intent,
        attempts=(working,),
        account=healthy,
    )
    partial_story = _unterminated_entry_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        intent=intent,
        attempts=(partial,),
        account=healthy,
    )
    recovery_story = _unterminated_entry_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        intent=intent,
        attempts=(working,),
        account=SimpleNamespace(recovery_pending=True, execution_locked=True),
    )
    unsafe_story = _unterminated_entry_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        intent=intent,
        attempts=(working,),
        account=SimpleNamespace(recovery_pending=False, execution_locked=True),
    )

    assert working_story.order_lifecycle_status == "ORDER_WORKING"
    assert working_story.outcome == "APPROVED_UNFILLED"
    assert partial_story.order_lifecycle_status == "PARTIAL_FILL_UNRECONCILED"
    assert partial_story.outcome == "PARTIALLY_FILLED"
    assert recovery_story.outcome == "RECOVERY"
    assert unsafe_story.outcome == "PERMANENTLY_UNSAFE"


@pytest.mark.parametrize(
    ("certificate_status", "lifecycle_status"),
    (
        ("REJECTED", "ENTRY_REJECTED"),
        ("CANCELED", "ENTRY_CANCELED"),
        ("EXPIRED", "ENTRY_EXPIRED"),
        ("REPLACED", "ENTRY_REPLACED"),
        ("UNFILLED", "ENTRY_UNFILLED"),
    ),
)
def test_terminal_zero_fill_status_is_preserved(
    certificate_status: str, lifecycle_status: str
) -> None:
    row, agent, values = _agent(approved=True)
    story = _terminal_entry_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        certificate=SimpleNamespace(
            execution_status=certificate_status,
            created_at=BOUNDARY + timedelta(minutes=1),
        ),
        account=SimpleNamespace(recovery_pending=False, execution_locked=False),
        attempts=(
            SimpleNamespace(
                attempt_ordinal=0,
                state=(certificate_status if certificate_status != "UNFILLED" else "CANCELED"),
                filled_quantity=0,
                quantity=1,
            ),
        ),
    )

    assert story.order_lifecycle_status == lifecycle_status
    assert story.outcome == "APPROVED_UNFILLED"
    assert story.account_impact.status == "TERMINAL_ZERO_FILL_RECONCILED"
    assert story.account_impact.cashflow_status == "NOT_APPLICABLE"


def test_terminal_partial_fill_requires_permanent_unsafe_authority() -> None:
    row, agent, values = _agent(approved=True)
    certificate = SimpleNamespace(
        execution_status="PARTIAL_CANCELED_RECONCILED",
        created_at=BOUNDARY + timedelta(minutes=1),
    )
    attempts = (
        SimpleNamespace(
            attempt_ordinal=0,
            state="CANCELED",
            filled_quantity=1,
            quantity=2,
        ),
    )

    with pytest.raises(SubmissionStoryError, match="SUBMISSION_STORY_LINEAGE_INVALID"):
        _terminal_entry_story(
            _strategy(),
            row,
            agent,
            values,
            _spread(),
            certificate=certificate,
            account=SimpleNamespace(recovery_pending=False, execution_locked=False),
            attempts=attempts,
        )

    story = _terminal_entry_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        certificate=certificate,
        account=SimpleNamespace(recovery_pending=False, execution_locked=True),
        attempts=attempts,
    )
    assert story.order_lifecycle_status == "PARTIAL_FILL_RECONCILED_UNSAFE"
    assert story.outcome == "PARTIALLY_FILLED"


def _sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _decision(role: str, boundary: datetime, ordinal: int) -> AgentDecisionRow:
    return AgentDecisionRow(
        decision_id=uuid4(),
        thesis_version_id=None,
        origin_tick_id=uuid4(),
        input_snapshot_id=uuid4(),
        account_role=role,
        account_fingerprint=chr(ord("a") + ordinal) * 64,
        decision_kind="OPPORTUNITY",
        outcome="NO_TRADE",
        reason_code="CALIBRATION_BINDING_NO_TRADE",
        policy_hash="d" * 64,
        result_payload={},
        result_hash="e" * 63 + str(ordinal),
        autonomy_authorized=False,
        decision_boundary=boundary,
        created_at=boundary + timedelta(seconds=ordinal),
    )


def test_cross_role_is_missing_and_duplicate_policy_boundary_is_rejected() -> None:
    sessions = _sessions()
    repository = SQLAlchemySubmissionStoryRepository(sessions)
    with sessions.begin() as session:
        session.add(_decision("DEVELOPMENT", BOUNDARY, 0))
    with pytest.raises(SubmissionStoryError, match="SUBMISSION_STORY_MISSING"):
        repository.latest()

    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add_all(
            (
                _decision("SUBMISSION", BOUNDARY, 1),
                _decision("SUBMISSION", BOUNDARY, 2),
            )
        )


def test_newer_incomplete_submission_decision_is_not_skipped() -> None:
    sessions = _sessions()
    repository = SQLAlchemySubmissionStoryRepository(sessions)
    older = _decision("SUBMISSION", BOUNDARY - timedelta(minutes=5), 1)
    newer = _decision("SUBMISSION", BOUNDARY, 2)
    with sessions.begin() as session:
        session.add_all((older, newer))
    with sessions() as session:
        assert repository._latest_decision(session).decision_id == newer.decision_id


def test_materialized_position_keeps_its_entry_decision_authoritative() -> None:
    repository = SQLAlchemySubmissionStoryRepository(SimpleNamespace())
    approved = _decision("SUBMISSION", BOUNDARY, 0)
    approved.outcome = "ENTRY_APPROVED"
    position = SimpleNamespace(
        account_role="SUBMISSION",
        activated_at=BOUNDARY + timedelta(hours=1),
        managed_position_id=uuid4(),
        entry_approval_id=uuid4(),
    )
    approval = SimpleNamespace(agent_decision_id=approved.decision_id)

    class FakeSession:
        def scalars(self, _query: object) -> tuple[SimpleNamespace, ...]:
            return (position,)

        def get(self, model: object, identity: object) -> object | None:
            if (
                model.__name__ == "EntryApprovalCertificateRow"
                and identity == position.entry_approval_id
            ):
                return approval
            if model.__name__ == "AgentDecisionRow" and identity == approved.decision_id:
                return approved
            return None

    assert repository._latest_decision(FakeSession()).decision_id == approved.decision_id


def test_provider_retry_audit_does_not_hide_terminal_policy_decision() -> None:
    sessions = _sessions()
    repository = SQLAlchemySubmissionStoryRepository(sessions)
    terminal = _decision("SUBMISSION", BOUNDARY, 0)
    provider_failure = _decision("SUBMISSION", BOUNDARY, 1)
    provider_failure.account_fingerprint = terminal.account_fingerprint
    provider_failure.outcome = "PROVIDER_FAILURE_NO_TRADE"
    provider_failure.reason_code = "PROVIDER_FAILURE_NO_TRADE"
    provider_failure.created_at = terminal.created_at + timedelta(seconds=1)
    with sessions.begin() as session:
        session.add_all((terminal, provider_failure))

    with sessions() as session:
        assert repository._latest_decision(session).decision_id == terminal.decision_id


def test_provider_retry_audit_includes_only_safe_persisted_status_and_time() -> None:
    sessions = _sessions()
    terminal = _decision("SUBMISSION", BOUNDARY, 3)
    machine_binding_hash = "b" * 64
    calibration_hash = "c" * 64
    binding = CalibrationBinding(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=terminal.account_fingerprint,
        decision_code="CALIBRATION_BINDING_NO_TRADE",
        machine_binding_hash=machine_binding_hash,
        calibration_hash=calibration_hash,
        decision_boundary=BOUNDARY - timedelta(hours=1),
        sealed_at=BOUNDARY - timedelta(hours=1),
    )
    terminal_snapshot = AgentInputSnapshotRow(
        snapshot_id=terminal.input_snapshot_id,
        thesis_version_id=None,
        account_role="SUBMISSION",
        account_fingerprint=terminal.account_fingerprint,
        decision_kind="OPPORTUNITY",
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_payload={
            "machine_binding_hash": machine_binding_hash,
            "calibration_hash": calibration_hash,
        },
        input_hash="f" * 64,
        created_at=BOUNDARY,
    )
    rows: list[AgentDecisionRow | AgentInputSnapshotRow] = []
    expected_times: list[datetime] = []
    for index, status in enumerate(
        ("OPPORTUNITY_DECISION_PENDING", "PROVIDER_FAILURE_NO_TRADE"),
        start=1,
    ):
        observed_at = BOUNDARY - timedelta(minutes=10 - index)
        expected_times.append(observed_at)
        failure_material = {
            "code": status,
            "provider_failure_code": f"PERSISTED_FAILURE_{index}",
            "provider_failure_kind": "OPPORTUNITY",
        }
        normalized = {
            "typed": encode_agent_value(failure_material),
            "machine_binding_hash": machine_binding_hash,
            "calibration_hash": calibration_hash,
        }
        result_payload = {
            "typed": encode_agent_value(
                AgentDecision(
                    code=status,
                    decided_at=observed_at,
                    submission_authority=binding,
                    provider_failure_code=f"PERSISTED_FAILURE_{index}",
                    provider_failure_kind=AcquisitionKind.OPPORTUNITY,
                )
            )
        }
        policy_hash = hashlib.sha256(
            json.dumps(failure_material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        snapshot = AgentInputSnapshotRow(
            snapshot_id=uuid4(),
            thesis_version_id=None,
            account_role="SUBMISSION",
            account_fingerprint=terminal.account_fingerprint,
            decision_kind="OPPORTUNITY",
            decision_boundary=observed_at,
            observed_at=observed_at,
            normalized_payload=normalized,
            input_hash="0" * 64,
            created_at=observed_at,
        )
        snapshot.input_hash = canonical_agent_hash(
            agent_input_material(
                account_role=snapshot.account_role,
                account_fingerprint=snapshot.account_fingerprint,
                decision_kind=snapshot.decision_kind,
                decision_boundary=observed_at,
                observed_at=observed_at,
                normalized_input=normalized,
                thesis_version_id=None,
            )
        )
        result_hash = canonical_agent_hash(
            agent_result_material(
                input_hash=snapshot.input_hash,
                outcome=status,
                reason_code=status,
                policy_hash=policy_hash,
                thesis_version_id=None,
                result_payload=result_payload,
                authorization_id=None,
                intent_id=None,
                intent_digest=None,
                autonomy_authorized=False,
            )
        )
        audit = AgentDecisionRow(
            decision_id=uuid5(
                NAMESPACE_URL,
                f"alphadecay:agent-decision:{result_hash}",
            ),
            thesis_version_id=None,
            origin_tick_id=uuid4(),
            input_snapshot_id=snapshot.snapshot_id,
            account_role="SUBMISSION",
            account_fingerprint=terminal.account_fingerprint,
            decision_kind="OPPORTUNITY",
            outcome=status,
            reason_code=status,
            policy_hash=policy_hash,
            result_payload=result_payload,
            result_hash=result_hash,
            autonomy_authorized=False,
            decision_boundary=observed_at,
            created_at=observed_at,
        )
        rows.extend((snapshot, audit))
    with sessions.begin() as session:
        session.add(terminal_snapshot)
        session.add_all(rows)

    with sessions() as session:
        audit = _provider_retry_audit(session, terminal)

    assert tuple(item.status for item in audit) == (
        "OPPORTUNITY_DECISION_PENDING",
        "PROVIDER_FAILURE_NO_TRADE",
    )
    assert tuple(item.recorded_at for item in audit) == tuple(expected_times)
    payload = json.dumps([item.model_dump(mode="json") for item in audit])
    assert "provider_failure_kind" not in payload
    assert terminal.account_fingerprint not in payload


def test_contaminated_submission_baseline_fails_closed() -> None:
    sessions = _sessions()
    with sessions.begin() as session:
        session.add_all(
            (
                AccountRoleRow(
                    role="SUBMISSION",
                    account_fingerprint=FINGERPRINT,
                    equity=Decimal("100000"),
                    autonomous_enabled=False,
                ),
                SubmissionBaselineRow(
                    baseline_id=uuid4(),
                    account_role="SUBMISSION",
                    account_fingerprint=FINGERPRINT,
                    equity=Decimal("100000"),
                    captured_at=BOUNDARY - timedelta(days=1),
                    positions_hash="1" * 64,
                    orders_hash="2" * 64,
                    activities_hash="3" * 64,
                    contaminated=True,
                ),
                _decision("SUBMISSION", BOUNDARY, 0),
            )
        )

    with pytest.raises(SubmissionStoryError, match="SUBMISSION_STORY_LINEAGE_INVALID"):
        SQLAlchemySubmissionStoryRepository(sessions).latest()


def test_public_preview_redacts_prices_strikes_and_exact_private_limit() -> None:
    preview = build_public_preview(_approved())
    payload = preview.model_dump_json()

    assert "240" not in payload
    assert "400" not in payload
    assert "405" not in payload
    assert "1.25" not in payload
    assert "2026-09-04" not in payload
    assert "SPY_EVENT_V1" not in payload
    assert '"underlying":"SPY"' not in payload
    assert "private numeric entry parameters are omitted" in payload
    assert preview.selected_spread.structure == "DEFINED_RISK_VERTICAL"
    assert preview.pnl_status == "NOT_RECORDED"
    assert preview.alternatives_recording == "NOT_RECORDED"


def test_preview_is_redacted_and_writes_no_file(tmp_path: Path, capsys) -> None:
    story = _approved()
    before = tuple(tmp_path.iterdir())

    assert main([], loader=lambda: story) == 0

    assert tuple(tmp_path.iterdir()) == before
    payload = json.loads(capsys.readouterr().out)
    assert payload["artifact_kind"] == "SUBMISSION_DECISION_STORY_PREVIEW"
    assert "maximum_loss_usd" not in payload
    assert payload["trading_mode"] == "PAPER_ONLY_APPLICATION_CONTRACT"
    assert payload["trading_mode_evidence"] == "NOT_RECORDED_IN_LINEAGE"
    assert payload["broker_fill_model"] == "PAPER_SIMULATION_IF_RECORDED"


def test_private_output_is_atomic_0600_and_deterministic(tmp_path: Path, capsys) -> None:
    story = _approved()
    first = tmp_path / "story-one.json"
    second = tmp_path / "story-two.json"

    assert main(["--output", str(first)], loader=lambda: story) == 0
    capsys.readouterr()
    assert main(["--output", str(second)], loader=lambda: story) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert first.read_bytes() == second.read_bytes()
    assert first.with_suffix(".md").read_bytes() == second.with_suffix(".md").read_bytes()
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(second.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.with_suffix(".md").stat().st_mode) == 0o600
    assert stat.S_IMODE(second.with_suffix(".md").stat().st_mode) == 0o600
    assert json.loads(first.read_text())["artifact_kind"] == "SUBMISSION_JUDGE_STORY"
    assert "240" not in first.read_text()
    assert "240" not in first.with_suffix(".md").read_text()
    assert receipt == {
        "artifacts_written": ["JSON", "MARKDOWN"],
        "mode": "PRIVATE_0600_JUDGE_STORY_BUNDLE",
    }
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_private_output_refuses_existing_symlinked_and_env_targets(tmp_path: Path, capsys) -> None:
    story = _approved()
    existing = tmp_path / "existing.json"
    existing.write_text("keep")

    with pytest.raises(SystemExit):
        main(["--output", str(existing)], loader=lambda: story)
    assert existing.read_text() == "keep"
    assert "SUBMISSION_STORY_OUTPUT_EXISTS" in capsys.readouterr().err

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(SystemExit):
        main(["--output", str(linked_parent / "story.json")], loader=lambda: story)
    assert not (real_parent / "story.json").exists()
    assert "SUBMISSION_STORY_OUTPUT_INVALID" in capsys.readouterr().err

    occupied_markdown_json = tmp_path / "occupied-markdown.json"
    occupied_markdown = occupied_markdown_json.with_suffix(".md")
    occupied_markdown.write_text("keep")
    with pytest.raises(SystemExit):
        main(["--output", str(occupied_markdown_json)], loader=lambda: story)
    assert occupied_markdown.read_text() == "keep"
    assert not occupied_markdown_json.exists()
    assert "SUBMISSION_STORY_OUTPUT_EXISTS" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["--output", str(tmp_path / ".ENV.story")], loader=lambda: story)
    assert "SUBMISSION_STORY_OUTPUT_INVALID" in capsys.readouterr().err


def test_export_errors_do_not_echo_exception_details_or_output_path(tmp_path: Path, capsys) -> None:
    sensitive = tmp_path / "private-name.json"

    def fail() -> object:
        raise RuntimeError(f"provider payload at {sensitive}")

    with pytest.raises(SystemExit):
        main(["--output", str(sensitive)], loader=fail)

    error = capsys.readouterr().err
    assert "SUBMISSION_STORY_EXPORT_FAILED" in error
    assert "provider payload" not in error
    assert str(sensitive) not in error


def _judge_fixture_story(shape: str) -> object:
    if shape == "no_trade":
        row, agent, values = _agent(approved=False)
        return _no_trade_story(_strategy(), row, agent, values).model_copy(
            update={
                "provider_retry_audit": (
                    ProviderRetryAuditSummary(
                        status="PROVIDER_FAILURE_NO_TRADE",
                        recorded_at=BOUNDARY - timedelta(minutes=5),
                    ),
                )
            }
        )

    row, agent, values = _agent(approved=True)
    assessments = (
        LifecycleAssessmentSummary(
            action="HOLD",
            reason_code="HOLD_CERTIFIED",
            assessed_at=BOUNDARY + timedelta(minutes=5),
        ),
    )
    if shape == "open":
        return _filled_story(
            _strategy(),
            row,
            agent,
            values,
            _spread(),
            closed=False,
            reconciled_cashflow=Decimal("-125"),
            assessment_present=True,
            attempts=(_recorded_fill_attempt(),),
            lifecycle_assessments=assessments,
        )

    close_time = BOUNDARY + timedelta(minutes=15)
    return _filled_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        closed=True,
        reconciled_cashflow=Decimal("40"),
        assessment_present=True,
        attempts=(_recorded_fill_attempt(),),
        lifecycle_assessments=(
            *assessments,
            LifecycleAssessmentSummary(
                action="CLOSE",
                reason_code="FORCED_CLOSE_WINDOW",
                assessed_at=BOUNDARY + timedelta(minutes=10),
            ),
        ),
        exit_order_lifecycle=OrderLifecycleSummary(
            recording_status="RECORDED",
            attempts=(
                {
                    "ordinal": 0,
                    "state": "CANCELED",
                    "filled_quantity": 0,
                    "quantity": 1,
                },
                {
                    "ordinal": 1,
                    "state": "FILLED",
                    "filled_quantity": 1,
                    "quantity": 1,
                },
            ),
        ),
        terminal=TerminalOutcomeSummary(
            scope="EXIT",
            certificate_recording_status="RECORDED",
            certificate_status="FILLED",
            certificate_time=close_time,
            outcome_status="CLOSED",
            outcome_time=close_time,
        ),
    ).model_copy(
        update={
            "provider_retry_audit": (
                ProviderRetryAuditSummary(
                    status="OPPORTUNITY_DECISION_PENDING",
                    recorded_at=BOUNDARY - timedelta(minutes=5),
                ),
            )
        }
    )


@pytest.mark.parametrize(
    "case",
    json.loads(FIXTURE_PATH.read_text()),
    ids=lambda case: case["shape"],
)
def test_fixture_driven_judge_story_shapes(case: dict[str, object]) -> None:
    story = build_judge_story(_judge_fixture_story(str(case["shape"])))
    markdown = render_judge_markdown(story)
    payload = story.model_dump_json()

    assert story.entry_decision == case["expected_decision"]
    assert story.outcome == case["expected_outcome"]
    assert [event.stage for event in story.timeline] == case["expected_stages"]
    assert [event.status for event in story.timeline] == case["expected_statuses"]
    assert "account_fingerprint" not in payload
    assert "order_id" not in payload
    assert all(value not in payload for value in ("240", "1.25", "400", "405"))
    assert all(value not in markdown for value in ("240", "1.25", "400", "405"))
    assert "Scheduled checks shown above are audit history" in markdown

    if case["shape"] == "closed":
        assert story.realized_pnl_usd == Decimal("40")
        assert "scheduled forced close" in markdown
        assert "+$40.00" in markdown
    else:
        assert story.realized_pnl_usd is None


def test_judge_story_preserves_partial_fill_without_inventing_position_or_pnl() -> None:
    row, agent, values = _agent(approved=True)
    partial_story = _unterminated_entry_story(
        _strategy(),
        row,
        agent,
        values,
        _spread(),
        intent=SimpleNamespace(state="CLAIMED"),
        attempts=(
            SimpleNamespace(
                attempt_ordinal=0,
                state="PARTIALLY_FILLED",
                filled_quantity=1,
                quantity=2,
            ),
        ),
        account=SimpleNamespace(recovery_pending=False, execution_locked=False),
    )

    story = build_judge_story(partial_story)
    markdown = render_judge_markdown(story)

    attempt = next(event for event in story.timeline if event.stage == "ENTRY_ORDER")
    assert (attempt.status, attempt.filled_quantity, attempt.ordered_quantity) == (
        "PARTIALLY_FILLED",
        1,
        2,
    )
    assert story.outcome == "PARTIALLY_FILLED"
    assert story.realized_pnl_status == "UNAVAILABLE"
    assert "Filled 1 of 2" in markdown
