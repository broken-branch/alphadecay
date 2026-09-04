from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from backend.app.contracts.v1 import (
    Action,
    Alternative,
    AssessmentResponse,
    DataQuality,
    DriftComponents,
    EvidenceRelation,
    EvidenceState,
    EvidenceTier,
    ExecutionDecision,
    GreekExposure,
    ThesisStatus,
)

ZERO = Decimal(0)
HUNDRED = Decimal(100)


SOURCE_WEIGHTS = {
    EvidenceTier.PRIMARY: Decimal("1.0"),
    EvidenceTier.ORIGINAL_REPORTING: Decimal("0.8"),
    EvidenceTier.SECONDARY: Decimal("0.5"),
}


@dataclass(frozen=True)
class EvidenceClaim:
    cluster_id: str
    relation: EvidenceRelation
    materiality: int
    relevance: Decimal
    confidence: Decimal
    source_tier: EvidenceTier
    invalidates: bool = False
    independent_reporting_group: str | None = None


@dataclass(frozen=True)
class EvidenceResult:
    state: EvidenceState
    net_evidence: Decimal | None
    evidence_drift: Decimal | None
    thesis_status: ThesisStatus


class VolatilityView(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class FreshnessKind(StrEnum):
    ACCOUNT = "ACCOUNT"
    POSITIONS = "POSITIONS"
    OPEN_ORDERS = "OPEN_ORDERS"
    UNDERLYING_QUOTE = "UNDERLYING_QUOTE"
    COMPLETED_BAR = "COMPLETED_BAR"
    OPTION_SNAPSHOT = "OPTION_SNAPSHOT"
    NEWS = "NEWS"


FRESHNESS_LIMITS = {
    FreshnessKind.ACCOUNT: timedelta(seconds=15),
    FreshnessKind.POSITIONS: timedelta(seconds=15),
    FreshnessKind.OPEN_ORDERS: timedelta(seconds=15),
    FreshnessKind.UNDERLYING_QUOTE: timedelta(seconds=30),
    FreshnessKind.COMPLETED_BAR: timedelta(seconds=120),
    FreshnessKind.OPTION_SNAPSHOT: timedelta(seconds=30),
    FreshnessKind.NEWS: timedelta(minutes=15),
}


@dataclass(frozen=True)
class FreshnessInput:
    kind: FreshnessKind
    observed_at: datetime
    retrieval_time_only: bool = False


@dataclass(frozen=True)
class FreshnessResult:
    complete: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class ScoreInput:
    evidence_drift: Decimal
    delta: Decimal
    delta_low: Decimal
    delta_high: Decimal
    vega: Decimal
    vega_low: Decimal
    vega_high: Decimal
    theta_per_day: Decimal
    max_daily_theta: Decimal
    dte: int
    minimum_dte: int
    maximum_dte: int
    horizon_fraction: Decimal
    volatility_view: VolatilityView
    entry_atm_iv: Decimal
    current_atm_iv: Decimal
    liquidation_pnl: Decimal
    approved_max_loss: Decimal


@dataclass(frozen=True)
class ScoreResult:
    evidence_drift: Decimal
    exposure_mismatch: Decimal
    time_pressure: Decimal
    volatility_mismatch: Decimal
    risk_utilization: Decimal
    unrounded_score: Decimal
    display_score: int
    dominant_non_evidence_component: str


@dataclass(frozen=True)
class RollCandidate:
    valid: bool
    drift_reduction: int
    expiry_extension_days: int
    relative_spread: Decimal
    maximum_relative_spread: Decimal
    incremental_debit: Decimal
    maximum_incremental_debit: Decimal
    within_loss_budget: bool = True
    covered_verticals: bool = True
    no_prior_roll_today: bool = True
    expected_after_exposure: GreekExposure | None = None


@dataclass(frozen=True)
class HardGateInput:
    verified_invalidation: bool = False
    price_confirmation_broken: bool = False
    short_dte: int | None = None
    short_call_ex_dividend_boundary: bool = False
    bounded_as_approved: bool = True
    risk_cap_exceeded: bool = False
    weekend_gate_failed: bool = False
    contest_end_window: bool = False
    strategy_close_reason: str | None = None


@dataclass(frozen=True)
class AssessmentInput:
    assessment_id: UUID
    run_id: UUID
    policy_hash: str
    quality: DataQuality
    actual_exposure: GreekExposure | None
    thesis_status: ThesisStatus
    evidence_state: EvidenceState
    scores: ScoreInput | None
    execution_failures: tuple[str, ...] = ()
    hard_gates: HardGateInput = HardGateInput()
    roll_candidate: RollCandidate | None = None
    acquisition_manifest_id: UUID | None = None
    acquisition_manifest_hash: str | None = None


@dataclass(frozen=True)
class PolicyResult:
    response: AssessmentResponse
    execution_decision: ExecutionDecision
    components: ScoreResult | None


def clamp(value: Decimal) -> Decimal:
    return min(HUNDRED, max(ZERO, value))


def band_deviation(value: Decimal, low: Decimal, high: Decimal, tolerance: Decimal) -> Decimal:
    if low > high or tolerance <= 0:
        raise ValueError("INVALID_BAND")
    if low <= value <= high:
        return ZERO
    distance = low - value if value < low else value - high
    return clamp(HUNDRED * distance / tolerance)


def score_drift(values: ScoreInput) -> ScoreResult:
    if (
        values.max_daily_theta <= 0
        or values.vega_low > values.vega_high
        or values.delta_low > values.delta_high
        or values.entry_atm_iv <= 0
        or values.approved_max_loss <= 0
    ):
        raise ValueError("INVALID_SCORE_INPUT")

    vega_tolerance = max(Decimal(1), values.vega_high - values.vega_low)
    delta_mismatch = band_deviation(values.delta, values.delta_low, values.delta_high, Decimal(25))
    vega_mismatch = band_deviation(values.vega, values.vega_low, values.vega_high, vega_tolerance)
    theta_mismatch = clamp(
        HUNDRED
        * (abs(min(values.theta_per_day, ZERO)) - values.max_daily_theta)
        / values.max_daily_theta
    )
    dte_mismatch = band_deviation(
        Decimal(values.dte),
        Decimal(values.minimum_dte),
        Decimal(values.maximum_dte),
        Decimal(7),
    )
    exposure_mismatch = (
        Decimal("0.40") * delta_mismatch
        + Decimal("0.25") * vega_mismatch
        + Decimal("0.20") * theta_mismatch
        + Decimal("0.15") * dte_mismatch
    )

    horizon_pressure = clamp(Decimal(200) * (values.horizon_fraction - Decimal("0.50")))
    expiry_pressure = clamp(HUNDRED * (Decimal(14) - values.dte) / Decimal(7))
    time_pressure = max(horizon_pressure, expiry_pressure)

    iv_change = (values.current_atm_iv - values.entry_atm_iv) / values.entry_atm_iv
    if values.volatility_view == VolatilityView.LONG:
        volatility_mismatch = clamp(-iv_change / Decimal("0.20") * HUNDRED)
    elif values.volatility_view == VolatilityView.SHORT:
        volatility_mismatch = clamp(iv_change / Decimal("0.20") * HUNDRED)
    else:
        volatility_mismatch = vega_mismatch

    loss_fraction = max(ZERO, -values.liquidation_pnl) / values.approved_max_loss
    risk_utilization = clamp((loss_fraction - Decimal("0.10")) / Decimal("0.40") * HUNDRED)
    unrounded = (
        Decimal("0.35") * clamp(values.evidence_drift)
        + Decimal("0.25") * exposure_mismatch
        + Decimal("0.15") * time_pressure
        + Decimal("0.10") * volatility_mismatch
        + Decimal("0.15") * risk_utilization
    )
    non_evidence = {
        "EXPOSURE": exposure_mismatch,
        "TIME": time_pressure,
        "VOLATILITY": volatility_mismatch,
        "RISK": risk_utilization,
    }
    dominant = max(non_evidence, key=non_evidence.__getitem__)
    return ScoreResult(
        evidence_drift=clamp(values.evidence_drift),
        exposure_mismatch=exposure_mismatch,
        time_pressure=time_pressure,
        volatility_mismatch=volatility_mismatch,
        risk_utilization=risk_utilization,
        unrounded_score=unrounded,
        display_score=int(round(unrounded)),
        dominant_non_evidence_component=dominant,
    )


def check_freshness(decision_time: datetime, inputs: tuple[FreshnessInput, ...]) -> FreshnessResult:
    failures: list[str] = []
    for item in inputs:
        age = decision_time - item.observed_at
        if age < timedelta(0):
            failures.append(f"FUTURE_{item.kind}")
        elif age > FRESHNESS_LIMITS[item.kind]:
            failures.append(f"STALE_{item.kind}")
        elif item.kind == FreshnessKind.OPTION_SNAPSHOT and item.retrieval_time_only:
            failures.append("UNVERIFIED_GREEK_TIMESTAMP")
    return FreshnessResult(not failures, tuple(failures))


def score_evidence(state: EvidenceState, claims: tuple[EvidenceClaim, ...]) -> EvidenceResult:
    if state == EvidenceState.UNKNOWN:
        return EvidenceResult(state, None, None, ThesisStatus.UNKNOWN)
    if state == EvidenceState.NO_CHANGE:
        return EvidenceResult(state, None, ZERO, ThesisStatus.INTACT)

    clusters: dict[str, EvidenceClaim] = {}
    for claim in claims:
        current = clusters.get(claim.cluster_id)
        if (
            current is None
            or SOURCE_WEIGHTS[claim.source_tier] > SOURCE_WEIGHTS[current.source_tier]
        ):
            clusters[claim.cluster_id] = claim

    primary_invalidation = any(
        claim.invalidates and claim.source_tier == EvidenceTier.PRIMARY
        for claim in clusters.values()
    )
    reporting_invalidations = {
        claim.independent_reporting_group
        for claim in clusters.values()
        if claim.invalidates
        and claim.source_tier == EvidenceTier.ORIGINAL_REPORTING
        and claim.independent_reporting_group
    }
    if primary_invalidation or len(reporting_invalidations) >= 2:
        return EvidenceResult(state, Decimal("-1"), HUNDRED, ThesisStatus.BROKEN)

    weighted: list[tuple[Decimal, int]] = []
    for claim in clusters.values():
        if (
            claim.relevance < Decimal("0.60")
            or claim.confidence < Decimal("0.60")
            or claim.relation == EvidenceRelation.NEUTRAL
        ):
            continue
        if not 1 <= claim.materiality <= 3:
            raise ValueError("INVALID_MATERIALITY")
        weight = (
            Decimal(claim.materiality)
            / Decimal(3)
            * claim.relevance
            * claim.confidence
            * SOURCE_WEIGHTS[claim.source_tier]
        )
        weighted.append((weight, 1 if claim.relation == EvidenceRelation.SUPPORTS else -1))

    if not weighted:
        return EvidenceResult(EvidenceState.NO_CHANGE, None, ZERO, ThesisStatus.INTACT)
    total = sum((weight for weight, _ in weighted), ZERO)
    net = sum((weight * sign for weight, sign in weighted), ZERO) / total
    drift = clamp(Decimal(50) - Decimal(50) * net)
    if net >= Decimal("0.25"):
        thesis_status = ThesisStatus.INTACT
    elif net <= Decimal("-0.25"):
        thesis_status = ThesisStatus.BROKEN
    else:
        thesis_status = ThesisStatus.WEAKENING
    return EvidenceResult(state, net, drift, thesis_status)


def evaluate_assessment(values: AssessmentInput) -> PolicyResult:
    if values.execution_failures:
        action = Action.NO_ACTION
        decision = ExecutionDecision.NO_ACTION
        rationale = values.execution_failures[0]
        components = None
    elif (
        values.quality != DataQuality.COMPLETE
        or values.actual_exposure is None
        or values.scores is None
    ):
        action = Action.NO_ACTION
        decision = ExecutionDecision.NO_ACTION
        rationale = "EXECUTION_DATA_MISSING"
        components = None
    else:
        components = score_drift(values.scores)
        mandatory_close_code = _mandatory_close_code(values.hard_gates, values.scores)
        if mandatory_close_code:
            action = Action.CLOSE
            decision = ExecutionDecision.CLOSE_RISK_ONLY
            rationale = mandatory_close_code
        elif values.evidence_state == EvidenceState.UNKNOWN:
            action = Action.NO_ACTION
            decision = ExecutionDecision.NO_ACTION
            rationale = "EVIDENCE_UNKNOWN"
        elif values.thesis_status == ThesisStatus.BROKEN or components.display_score >= 70:
            action = Action.CLOSE
            decision = ExecutionDecision.CLOSE_APPROVED
            rationale = (
                "THESIS_BROKEN" if values.thesis_status == ThesisStatus.BROKEN else "DRIFT_CLOSE"
            )
        elif _roll_is_eligible(values, components):
            action = Action.ROLL
            decision = ExecutionDecision.ROLL_APPROVED
            rationale = "EXPOSURE_ROLL"
        else:
            action = Action.HOLD
            decision = ExecutionDecision.HOLD_CERTIFIED
            rationale = "THESIS_ALIGNED" if components.display_score < 45 else "NO_VALID_ROLL"

    alternatives = tuple(
        Alternative(
            action=candidate,
            eligible=candidate == action,
            rationale_code=rationale if candidate == action else f"REJECTED_{candidate}",
            expected_exposure=(
                values.roll_candidate.expected_after_exposure
                if candidate == Action.ROLL and values.roll_candidate is not None
                else None
            ),
        )
        for candidate in (Action.HOLD, Action.CLOSE, Action.ROLL)
    )
    response = AssessmentResponse(
        assessment_id=values.assessment_id,
        run_id=values.run_id,
        action=action,
        rationale_code=rationale,
        quality=values.quality,
        thesis_status=values.thesis_status,
        evidence_state=values.evidence_state,
        execution_decision=decision,
        actual_exposure=values.actual_exposure,
        drift_score=Decimal(components.display_score) if components else None,
        components=DriftComponents(**asdict(components)) if components else None,
        alternatives=alternatives,
        evidence=(),
        policy_hash=values.policy_hash,
    )
    return PolicyResult(response=response, execution_decision=decision, components=components)


def _roll_is_eligible(values: AssessmentInput, components: ScoreResult) -> bool:
    candidate = values.roll_candidate
    return bool(
        candidate
        and 45 <= components.display_score < 70
        and values.thesis_status in (ThesisStatus.INTACT, ThesisStatus.WEAKENING)
        and components.dominant_non_evidence_component in ("EXPOSURE", "TIME")
        and candidate.valid
        and candidate.drift_reduction >= 20
        and 7 <= candidate.expiry_extension_days <= 35
        and ZERO <= candidate.relative_spread <= candidate.maximum_relative_spread < 1
        and ZERO <= candidate.incremental_debit <= candidate.maximum_incremental_debit
        and candidate.within_loss_budget
        and candidate.covered_verticals
        and candidate.no_prior_roll_today
    )


def _mandatory_close_code(gates: HardGateInput, scores: ScoreInput) -> str | None:
    if gates.strategy_close_reason is not None:
        return gates.strategy_close_reason
    if gates.verified_invalidation:
        return "VERIFIED_INVALIDATION"
    if gates.price_confirmation_broken:
        return "PRICE_CONFIRMATION_BROKEN"
    loss_fraction = max(ZERO, -scores.liquidation_pnl) / scores.approved_max_loss
    if loss_fraction >= Decimal("0.50"):
        return "LOSS_LIMIT_REACHED"
    if gates.short_dte is not None and gates.short_dte <= 7:
        return "SHORT_DTE_LIMIT"
    if gates.short_call_ex_dividend_boundary:
        return "SHORT_CALL_EX_DIVIDEND"
    if not gates.bounded_as_approved:
        return "DEFINED_RISK_BROKEN"
    if gates.risk_cap_exceeded:
        return "RISK_CAP_EXCEEDED"
    if gates.contest_end_window:
        return "CONTEST_END_CLOSE"
    if gates.weekend_gate_failed:
        return "WEEKEND_GATE_FAILED"
    return None
