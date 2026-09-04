from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from backend.app.experiments import runtime_bridge
from backend.app.experiments.models import (
    CompiledExperimentVersion,
    ExperimentAuthorizationStatus,
)
from backend.app.experiments.runtime_authority import ExperimentRuntimeReason
from backend.app.experiments.runtime_bridge import evaluate_experiment_runtime_bridge
from backend.app.strategy_briefs.decision import PositionPhase
from backend.app.strategy_briefs.selection import (
    CompiledCandidateReason,
    CompiledCandidateSelectionBlocked,
)
from backend.app.strategy_briefs.tick import ProtocolTickBlocked
from backend.tests.strategy_briefs.test_compiled_candidate_selection import (
    authority,
    option,
    protocol,
    snapshot,
)
from backend.tests.strategy_briefs.test_protocol_observations import evidence

NOW = datetime(2026, 9, 2, 14, tzinfo=UTC)
SOURCE_DEFINITION_HASH = "d" * 64
EVENT_HASH = "a" * 64


def inputs():
    compiled_protocol = protocol()
    compiled = CompiledExperimentVersion(
        experiment_id=uuid4(),
        source_definition_hash=SOURCE_DEFINITION_HASH,
        protocol_hash=compiled_protocol.protocol_hash,
        compiled_protocol=compiled_protocol,
        created_at=NOW,
    )
    authorization = ExperimentAuthorizationStatus(
        experiment_id=compiled.experiment_id,
        source_definition_hash=compiled.source_definition_hash,
        protocol_hash=compiled.protocol_hash,
        authorization_revision=1,
        authorization_state="ARMED",
        entry_authorized=True,
        authorization_event_hash=EVENT_HASH,
        updated_at=NOW,
    )
    market = snapshot(
        compiled_protocol,
        (
            option("C", "500", "2", "2.02"),
            option("C", "504", "0.5", "0.52"),
        ),
    )
    acquired = evidence(compiled_protocol, PositionPhase.FLAT)
    return compiled, acquired, authorization, market, authority(market)


def run(values, *, expected_revision=1):
    compiled, acquired, authorization, market, selection_authority = values
    return evaluate_experiment_runtime_bridge(
        compiled,
        acquired,
        authorization,
        expected_authorization_revision=expected_revision,
        snapshot=market,
        candidate_selection_authority=selection_authority,
    )


def validation_payload(result, **updates):
    return {
        **result.model_dump(),
        "validated_compiled": result.validated_compiled,
        "validated_evidence": result.validated_evidence,
        "validated_authorization": result.validated_authorization,
        "validated_snapshot": result.validated_snapshot,
        "validated_candidate_authority": result.validated_candidate_authority,
        **updates,
    }


def test_runs_tick_then_full_pipeline_and_reproduces_both(monkeypatch) -> None:
    values = inputs()
    calls: list[str] = []
    original_tick = runtime_bridge.run_compiled_experiment_tick
    original_pipeline = runtime_bridge.evaluate_experiment_entry_pipeline

    def observed_tick(*args):
        calls.append("tick")
        return original_tick(*args)

    def observed_pipeline(*args, **kwargs):
        calls.append("pipeline")
        return original_pipeline(*args, **kwargs)

    monkeypatch.setattr(runtime_bridge, "run_compiled_experiment_tick", observed_tick)
    monkeypatch.setattr(
        runtime_bridge,
        "evaluate_experiment_entry_pipeline",
        observed_pipeline,
    )

    result = run(values)

    assert calls == ["tick", "pipeline", "tick", "pipeline"]
    assert result.entry_pipeline.runtime_authority.reason_codes == (
        ExperimentRuntimeReason.ENTRY_CANDIDATE_AUTHORIZED,
    )
    assert result.entry_pipeline.candidate_selection.reason is CompiledCandidateReason.SELECTED
    assert result.entry_pipeline.candidate_selection.candidate is not None
    assert result.observation_source_hashes == result.tick.observation_source_hashes
    assert result.snapshot_hash == result.validated_snapshot.source_hash


@pytest.mark.parametrize(
    ("change", "code"),
    (
        ({"protocol_hash": "b" * 64}, "PROTOCOL_SOURCE_MISMATCH"),
        ({"protocol_source_hash": "c" * 64}, "PROTOCOL_SOURCE_MISMATCH"),
        ({"symbol": "QQQ"}, "PROTOCOL_SYMBOL_MISMATCH"),
        ({"numeric_values": {}}, "PROTOCOL_METRIC_MISSING"),
    ),
)
def test_invalid_evidence_stops_before_pipeline(monkeypatch, change, code) -> None:
    compiled, acquired, authorization, market, selection_authority = inputs()
    acquired = acquired.model_copy(update=change)
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("entry pipeline must not run")

    monkeypatch.setattr(runtime_bridge, "evaluate_experiment_entry_pipeline", forbidden)

    with pytest.raises(ProtocolTickBlocked, match=code) as caught:
        run((compiled, acquired, authorization, market, selection_authority))

    assert caught.value.code == code
    assert calls == 0


def test_stale_revision_preserves_exact_reason_and_skips_candidate() -> None:
    result = run(inputs(), expected_revision=2)

    assert result.expected_authorization_revision == 2
    assert result.observed_authorization_revision == 1
    assert result.entry_pipeline.runtime_authority.reason_codes == (
        ExperimentRuntimeReason.AUTHORIZATION_REVISION_STALE,
    )
    assert result.entry_pipeline.candidate_selection is None


def test_snapshot_and_candidate_authority_mismatch_fails_closed() -> None:
    compiled, acquired, authorization, market, selection_authority = inputs()
    mismatched = replace(selection_authority, snapshot_source_hash="b" * 64)

    with pytest.raises(
        CompiledCandidateSelectionBlocked,
        match="COMPILED_SELECTION_ACCOUNT_INVALID",
    ):
        run((compiled, acquired, authorization, market, mismatched))


def test_every_composed_layer_keeps_side_effect_authority_false() -> None:
    result = run(inputs())
    runtime = result.entry_pipeline.runtime_authority
    candidate = result.entry_pipeline.candidate_selection
    serialized = result.model_dump()

    assert result.authority_state == "NON_AUTHORITATIVE"
    assert result.automation_state == "OFF"
    assert result.schedule_authorized is False
    assert result.provider_access_authorized is False
    assert result.broker_access_authorized is False
    assert result.order_authorized is False
    assert result.execution_eligible is False
    assert result.tick.authority_state == "NON_AUTHORITATIVE"
    assert result.tick.arm_state == "NOT_ARMED"
    assert result.tick.automation_state == "OFF"
    assert result.tick.execution_eligible is False
    assert runtime.schedule_authorized is False
    assert runtime.provider_access_authorized is False
    assert runtime.broker_access_authorized is False
    assert runtime.order_authorized is False
    assert runtime.execution_eligible is False
    assert candidate is not None
    assert candidate.execution_eligible is False
    assert not any(name.startswith("validated_") for name in serialized)
    assert not any(name.startswith("validated_") for name in serialized["entry_pipeline"])


def test_result_rejects_forged_evidence_anchor() -> None:
    result = run(inputs())
    numeric_values = dict(result.validated_evidence.numeric_values)
    numeric_values[next(iter(numeric_values))] += Decimal("1")
    forged = result.validated_evidence.model_copy(update={"numeric_values": numeric_values})

    with pytest.raises(ValueError, match="tick does not reproduce from exact inputs"):
        result.model_validate(validation_payload(result, validated_evidence=forged))


def test_result_rejects_weakened_risk_authority() -> None:
    result = run(inputs())
    weak = replace(
        result.validated_candidate_authority,
        available_risk=Decimal("0"),
        available_buying_power=Decimal("0"),
    )

    with pytest.raises(ValueError, match="does not reproduce from exact inputs"):
        result.model_validate(validation_payload(result, validated_candidate_authority=weak))


def test_result_rejects_forged_candidate_reason() -> None:
    result = run(inputs())
    candidate = replace(
        result.entry_pipeline.candidate_selection,
        reason=CompiledCandidateReason.NO_ELIGIBLE_CANDIDATE,
        candidate=None,
        selection_basis=None,
        leg_source_hashes=(),
    )
    pipeline = result.entry_pipeline.model_copy(update={"candidate_selection": candidate})

    with pytest.raises(ValueError, match="does not reproduce from exact inputs"):
        result.model_validate(validation_payload(result, entry_pipeline=pipeline))


@pytest.mark.parametrize(
    "updates",
    (
        {"protocol_hash": "b" * 64},
        {"source_definition_hash": "c" * 64},
        {"snapshot_hash": "d" * 64},
        {"account_fingerprint": "e" * 64},
    ),
)
def test_result_rejects_forged_reported_lineage(updates) -> None:
    result = run(inputs())

    with pytest.raises(ValueError, match="runtime bridge lineage mismatch"):
        result.model_validate(validation_payload(result, **updates))
