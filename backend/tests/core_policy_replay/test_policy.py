from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from backend.app.contracts.v1 import Action, DataQuality, GreekExposure
from backend.app.policy import (
    AssessmentInput,
    EvidenceClaim,
    EvidenceRelation,
    EvidenceState,
    EvidenceTier,
    ExecutionDecision,
    FreshnessInput,
    FreshnessKind,
    HardGateInput,
    RollCandidate,
    ScoreInput,
    ThesisStatus,
    VolatilityView,
    check_freshness,
    evaluate_assessment,
    score_drift,
    score_evidence,
)

NOW = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)


def score_input(**changes: object) -> ScoreInput:
    values: dict[str, object] = {
        "evidence_drift": Decimal("0"),
        "delta": Decimal("50"),
        "delta_low": Decimal("40"),
        "delta_high": Decimal("60"),
        "vega": Decimal("4"),
        "vega_low": Decimal("2"),
        "vega_high": Decimal("6"),
        "theta_per_day": Decimal("-4"),
        "max_daily_theta": Decimal("8"),
        "dte": 21,
        "minimum_dte": 14,
        "maximum_dte": 35,
        "horizon_fraction": Decimal("0.25"),
        "volatility_view": VolatilityView.LONG,
        "entry_atm_iv": Decimal("0.50"),
        "current_atm_iv": Decimal("0.50"),
        "liquidation_pnl": Decimal("20"),
        "approved_max_loss": Decimal("500"),
    }
    values.update(changes)
    return ScoreInput(**values)


def assessment_input(**changes: object) -> AssessmentInput:
    values: dict[str, object] = {
        "assessment_id": UUID("00000000-0000-0000-0000-000000000101"),
        "run_id": UUID("00000000-0000-0000-0000-000000000102"),
        "policy_hash": "policy-v0.1",
        "quality": DataQuality.COMPLETE,
        "actual_exposure": GreekExposure(delta=50, gamma=2, theta_per_day=-4, vega_per_iv_point=4),
        "thesis_status": ThesisStatus.INTACT,
        "evidence_state": EvidenceState.NO_CHANGE,
        "scores": score_input(),
    }
    values.update(changes)
    return AssessmentInput(**values)


def test_drift_score_uses_frozen_component_weights() -> None:
    result = score_drift(
        score_input(
            evidence_drift=Decimal("25"),
            delta=Decimal("80"),
            theta_per_day=Decimal("-16"),
            dte=10,
            horizon_fraction=Decimal("0.75"),
            current_atm_iv=Decimal("0.40"),
            liquidation_pnl=Decimal("-150"),
        )
    )

    assert result.exposure_mismatch == Decimal("60.57142857142857142857142857")
    assert result.time_pressure == Decimal("57.14285714285714285714285714")
    assert result.volatility_mismatch == Decimal("100")
    assert result.risk_utilization == Decimal("50.0")
    assert result.display_score == 50


def test_freshness_accepts_boundaries_and_rejects_future_or_stale_data() -> None:
    result = check_freshness(
        NOW,
        (
            FreshnessInput(FreshnessKind.ACCOUNT, NOW - timedelta(seconds=15)),
            FreshnessInput(FreshnessKind.OPTION_SNAPSHOT, NOW - timedelta(seconds=30)),
            FreshnessInput(FreshnessKind.COMPLETED_BAR, NOW - timedelta(seconds=120)),
            FreshnessInput(FreshnessKind.NEWS, NOW - timedelta(minutes=15)),
            FreshnessInput(FreshnessKind.UNDERLYING_QUOTE, NOW + timedelta(seconds=1)),
        ),
    )

    assert not result.complete
    assert result.failures == ("FUTURE_UNDERLYING_QUOTE",)


def test_evidence_deduplicates_clusters_and_primary_invalidation_breaks_thesis() -> None:
    result = score_evidence(
        EvidenceState.ASSESSED,
        (
            EvidenceClaim(
                "shared",
                EvidenceRelation.SUPPORTS,
                3,
                Decimal("1"),
                Decimal("1"),
                EvidenceTier.SECONDARY,
            ),
            EvidenceClaim(
                "shared",
                EvidenceRelation.SUPPORTS,
                3,
                Decimal("1"),
                Decimal("1"),
                EvidenceTier.PRIMARY,
            ),
            EvidenceClaim(
                "filing",
                EvidenceRelation.CONTRADICTS,
                3,
                Decimal("1"),
                Decimal("1"),
                EvidenceTier.PRIMARY,
                invalidates=True,
            ),
        ),
    )

    assert result.evidence_drift == Decimal("100")
    assert result.thesis_status == ThesisStatus.BROKEN


def test_no_change_and_unknown_evidence_remain_distinct() -> None:
    quiet = score_evidence(EvidenceState.NO_CHANGE, ())
    unknown = score_evidence(EvidenceState.UNKNOWN, ())

    assert quiet.evidence_drift == 0
    assert quiet.thesis_status == ThesisStatus.INTACT
    assert unknown.evidence_drift is None
    assert unknown.thesis_status == ThesisStatus.UNKNOWN


def test_execution_critical_failure_precedes_mandatory_close() -> None:
    result = evaluate_assessment(
        assessment_input(
            execution_failures=("ASSIGNMENT_SUSPECTED",),
            hard_gates=HardGateInput(short_dte=7),
        )
    )

    assert result.response.action == Action.NO_ACTION
    assert result.execution_decision == ExecutionDecision.NO_ACTION
    assert result.response.rationale_code == "ASSIGNMENT_SUSPECTED"


def test_unknown_evidence_allows_only_independent_mandatory_close() -> None:
    mandatory = evaluate_assessment(
        assessment_input(
            thesis_status=ThesisStatus.UNKNOWN,
            evidence_state=EvidenceState.UNKNOWN,
            hard_gates=HardGateInput(short_dte=7),
        )
    )
    discretionary = evaluate_assessment(
        assessment_input(thesis_status=ThesisStatus.UNKNOWN, evidence_state=EvidenceState.UNKNOWN)
    )

    assert mandatory.response.action == Action.CLOSE
    assert mandatory.execution_decision == ExecutionDecision.CLOSE_RISK_ONLY
    assert discretionary.response.action == Action.NO_ACTION
    assert discretionary.execution_decision == ExecutionDecision.NO_ACTION


def test_missing_quality_blocks_without_a_separate_failure_hint() -> None:
    result = evaluate_assessment(
        assessment_input(quality=DataQuality.MISSING, actual_exposure=None, scores=None)
    )

    assert result.response.action == Action.NO_ACTION
    assert result.response.rationale_code == "EXECUTION_DATA_MISSING"
    assert result.response.drift_score is None


def test_loss_at_exact_half_of_approved_risk_requires_close() -> None:
    result = evaluate_assessment(
        assessment_input(scores=score_input(liquidation_pnl=Decimal("-250")))
    )

    assert result.response.action == Action.CLOSE
    assert result.execution_decision == ExecutionDecision.CLOSE_RISK_ONLY
    assert result.response.rationale_code == "LOSS_LIMIT_REACHED"


def test_broken_thesis_closes_before_roll() -> None:
    result = evaluate_assessment(
        assessment_input(
            thesis_status=ThesisStatus.BROKEN,
            evidence_state=EvidenceState.ASSESSED,
            roll_candidate=RollCandidate(
                valid=True,
                drift_reduction=30,
                expiry_extension_days=14,
                relative_spread=Decimal("0.05"),
                maximum_relative_spread=Decimal("0.25"),
                incremental_debit=Decimal("100"),
                maximum_incremental_debit=Decimal("500"),
            ),
        )
    )

    assert result.response.action == Action.CLOSE
    assert result.execution_decision == ExecutionDecision.CLOSE_APPROVED


def test_valid_roll_wins_only_in_middle_drift_band() -> None:
    result = evaluate_assessment(
        assessment_input(
            evidence_state=EvidenceState.ASSESSED,
            scores=score_input(
                evidence_drift=Decimal("50"),
                delta=Decimal("85"),
                theta_per_day=Decimal("-16"),
                dte=10,
                liquidation_pnl=Decimal("-150"),
            ),
            roll_candidate=RollCandidate(
                valid=True,
                drift_reduction=20,
                expiry_extension_days=7,
                relative_spread=Decimal("0.05"),
                maximum_relative_spread=Decimal("0.25"),
                incremental_debit=Decimal("100"),
                maximum_incremental_debit=Decimal("500"),
            ),
        )
    )

    assert 45 <= result.response.drift_score < 70
    assert result.response.action == Action.ROLL
    assert result.execution_decision == ExecutionDecision.ROLL_APPROVED
    assert tuple(item.action for item in result.response.alternatives) == (
        Action.HOLD,
        Action.CLOSE,
        Action.ROLL,
    )
