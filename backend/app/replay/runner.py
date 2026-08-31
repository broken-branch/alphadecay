from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.app.contracts.v1 import (
    AccountRole,
    CertificateResponse,
    DataQuality,
    FixtureEnvelope,
    GreekExposure,
    ReplayPresentation,
    ReplayResponse,
    ReplayScenario,
    ThesisCreateRequest,
    ThesisResponse,
)
from backend.app.policy import (
    AssessmentInput,
    EvidenceClaim,
    EvidenceState,
    HardGateInput,
    RollCandidate,
    ScoreInput,
    ThesisStatus,
    VolatilityView,
    evaluate_assessment,
    score_evidence,
)

SCENARIOS = tuple(ReplayScenario)
FIXTURE_ROOT = Path(__file__).parents[3] / "fixtures" / "replay"
THESIS_IDENTITY = "POST_EVENT_CONTINUATION_V1"


class ReplayFixtureError(ValueError):
    pass


def available_scenarios() -> tuple[ReplayScenario, ...]:
    return SCENARIOS


def run_replay(scenario: ReplayScenario | str) -> ReplayResponse:
    try:
        replay_scenario = ReplayScenario(scenario)
    except ValueError as exc:
        raise ReplayFixtureError("UNKNOWN_REPLAY_SCENARIO") from exc
    envelope = FixtureEnvelope.model_validate_json(
        (FIXTURE_ROOT / f"{replay_scenario.value}.json").read_text()
    )
    if envelope.scenario != replay_scenario:
        raise ReplayFixtureError("REPLAY_SCENARIO_MISMATCH")
    input_hash = _hash(envelope.payload)
    if input_hash != envelope.input_hash:
        raise ReplayFixtureError("REPLAY_INPUT_HASH_MISMATCH")

    payload = envelope.payload
    presentation = ReplayPresentation.model_validate(payload["presentation"])
    actual_exposure = GreekExposure.model_validate(payload["actual_exposure"])
    roll = payload.get("roll_candidate")
    scores = _score_input(payload["scores"])
    _validate_presentation(payload, presentation, actual_exposure, scores, roll)
    policy_result = evaluate_assessment(
        AssessmentInput(
            assessment_id=_id(replay_scenario, "assessment"),
            run_id=_id(replay_scenario, "run"),
            policy_hash=payload["policy_hash"],
            quality=DataQuality(payload.get("quality", DataQuality.COMPLETE)),
            actual_exposure=actual_exposure,
            thesis_status=ThesisStatus(payload["thesis_status"]),
            evidence_state=EvidenceState(payload["evidence_state"]),
            scores=scores,
            hard_gates=HardGateInput(**payload.get("hard_gates", {})),
            roll_candidate=(
                RollCandidate(
                    **{
                        **roll,
                        "relative_spread": Decimal(roll["relative_spread"]),
                        "maximum_relative_spread": Decimal(roll["maximum_relative_spread"]),
                        "incremental_debit": Decimal(roll["incremental_debit"]),
                        "maximum_incremental_debit": Decimal(roll["maximum_incremental_debit"]),
                    }
                )
                if roll
                else None
            ),
        )
    )
    assessment_hash = _hash(policy_result.response.model_dump(mode="json"))
    if assessment_hash != envelope.expected_hash:
        raise ReplayFixtureError("REPLAY_EXPECTED_HASH_MISMATCH")

    thesis_request = ThesisCreateRequest.model_validate(payload["thesis"])
    thesis = ThesisResponse(
        thesis_id=_id(THESIS_IDENTITY, "thesis"),
        version=1,
        frozen=True,
        thesis_hash=_hash(payload["thesis"]),
        thesis=thesis_request,
    )
    certificate = CertificateResponse(
        certificate_id=_id(replay_scenario, "certificate"),
        account_role=AccountRole.REPLAY,
        thesis=thesis,
        assessment=policy_result.response,
        expected_after_exposure=(
            GreekExposure.model_validate(payload["expected_after_exposure"])
            if payload.get("expected_after_exposure") is not None
            else None
        ),
        actual_after_exposure=None,
        attempts=(),
        execution_state="NOT_REQUESTED",
        lineage_hash=hashlib.sha256(f"{input_hash}:{assessment_hash}".encode()).hexdigest(),
        published=True,
    )
    return ReplayResponse(
        scenario=replay_scenario,
        input_hash=input_hash,
        assessment_hash=assessment_hash,
        assessment=policy_result.response,
        certificate=certificate,
        presentation=presentation,
    )


def _id(scenario: ReplayScenario | str, kind: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"alphadecay:replay:{scenario}:{kind}")


def _score_input(raw: dict[str, object]) -> ScoreInput:
    integer_fields = {"dte", "minimum_dte", "maximum_dte"}
    values = {
        name: int(value) if name in integer_fields else Decimal(str(value))
        for name, value in raw.items()
        if name != "volatility_view"
    }
    return ScoreInput(volatility_view=VolatilityView(str(raw["volatility_view"])), **values)


def _validate_presentation(
    payload: dict[str, object],
    presentation: ReplayPresentation,
    actual_exposure: GreekExposure,
    scores: ScoreInput,
    roll: object,
) -> None:
    opening = presentation.opening
    market = presentation.market
    quality = DataQuality(payload.get("quality", DataQuality.COMPLETE))
    thesis = payload["thesis"]
    if not isinstance(thesis, dict):
        raise ReplayFixtureError("REPLAY_PRESENTATION_MISMATCH")
    if (
        opening.underlying != thesis.get("underlying")
        or actual_exposure.delta != scores.delta
        or actual_exposure.theta_per_day != scores.theta_per_day
        or actual_exposure.vega_per_iv_point != scores.vega
        or market.dte != scores.dte
        or opening.approved_risk_cap != scores.approved_max_loss
        or opening.delta_low != scores.delta_low
        or opening.delta_high != scores.delta_high
        or opening.vega_low != scores.vega_low
        or opening.vega_high != scores.vega_high
        or opening.maximum_daily_theta != scores.max_daily_theta
        or opening.minimum_dte != scores.minimum_dte
        or opening.maximum_dte != scores.maximum_dte
        or (market.open_pnl is not None and market.open_pnl != scores.liquidation_pnl)
        or (quality is DataQuality.COMPLETE) != (market.quote_status == "FRESH")
        or (
            "quote_age_seconds" in payload
            and market.quote_age_seconds != payload["quote_age_seconds"]
        )
    ):
        raise ReplayFixtureError("REPLAY_PRESENTATION_MISMATCH")
    if (roll is None) != (presentation.roll is None):
        raise ReplayFixtureError("REPLAY_PRESENTATION_MISMATCH")
    if roll is not None:
        if not isinstance(roll, dict) or presentation.roll is None:
            raise ReplayFixtureError("REPLAY_PRESENTATION_MISMATCH")
        extension = (presentation.roll.expiration_date - opening.expiration_date).days
        if extension != roll.get("expiry_extension_days"):
            raise ReplayFixtureError("REPLAY_PRESENTATION_MISMATCH")

    classifications = presentation.evidence.classifications
    if not classifications:
        if payload.get("evidence_state") != EvidenceState.NO_CHANGE.value:
            raise ReplayFixtureError("REPLAY_EVIDENCE_PRESENTATION_MISMATCH")
        return
    evidence = score_evidence(
        EvidenceState.ASSESSED,
        tuple(
            EvidenceClaim(
                cluster_id=item.source_id,
                relation=item.relation,
                materiality=item.materiality,
                relevance=item.relevance,
                confidence=item.confidence,
                source_tier=item.source_tier,
                invalidates=item.invalidates,
            )
            for item in classifications
        ),
    )
    if (
        payload.get("evidence_state") != EvidenceState.ASSESSED.value
        or evidence.evidence_drift != scores.evidence_drift
        or evidence.thesis_status.value != payload.get("thesis_status")
    ):
        raise ReplayFixtureError("REPLAY_EVIDENCE_PRESENTATION_MISMATCH")


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
