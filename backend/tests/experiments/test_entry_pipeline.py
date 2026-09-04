from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from backend.app.experiments import entry_pipeline
from backend.app.experiments.entry_pipeline import evaluate_experiment_entry_pipeline
from backend.app.experiments.models import (
    CompiledExperimentVersion,
    ExperimentAuthorizationStatus,
)
from backend.app.experiments.runtime_authority import (
    ExperimentRuntimeAuthorityBlocked,
    ExperimentRuntimeDisposition,
    ExperimentRuntimeReason,
)
from backend.app.strategy_briefs.decision import PositionPhase
from backend.app.strategy_briefs.selection import CompiledCandidateReason
from backend.app.strategy_briefs.tick import run_compiled_experiment_tick
from backend.tests.strategy_briefs.test_compiled_candidate_selection import (
    authority,
    option,
    protocol,
    snapshot,
    tick,
)
from backend.tests.strategy_briefs.test_protocol_observations import evidence

NOW = datetime(2026, 9, 2, 14, tzinfo=UTC)
SOURCE_DEFINITION_HASH = "d" * 64
EVENT_HASH = "a" * 64


def inputs(*, options=None):
    compiled_protocol = protocol()
    compiled = CompiledExperimentVersion(
        experiment_id=uuid4(),
        source_definition_hash=SOURCE_DEFINITION_HASH,
        protocol_hash=compiled_protocol.protocol_hash,
        compiled_protocol=compiled_protocol,
        created_at=NOW,
    )
    compiled_tick = tick(compiled_protocol)
    authorization = authorization_for(compiled)
    market = snapshot(
        compiled_protocol,
        options
        or (
            option("C", "500", "2", "2.02"),
            option("C", "504", "0.5", "0.52"),
        ),
    )
    return compiled, compiled_tick, authorization, market, authority(market)


def authorization_for(
    compiled: CompiledExperimentVersion,
    *,
    state: str = "ARMED",
    revision: int = 1,
) -> ExperimentAuthorizationStatus:
    if revision == 0:
        return ExperimentAuthorizationStatus(
            experiment_id=compiled.experiment_id,
            source_definition_hash=compiled.source_definition_hash,
            protocol_hash=compiled.protocol_hash,
            authorization_revision=0,
            authorization_state="NOT_ARMED",
            entry_authorized=False,
        )
    return ExperimentAuthorizationStatus(
        experiment_id=compiled.experiment_id,
        source_definition_hash=compiled.source_definition_hash,
        protocol_hash=compiled.protocol_hash,
        authorization_revision=revision,
        authorization_state=state,
        entry_authorized=state == "ARMED",
        authorization_event_hash=EVENT_HASH,
        updated_at=NOW,
    )


def run(values, *, expected_revision=1):
    compiled, compiled_tick, authorization, market, selection_authority = values
    return evaluate_experiment_entry_pipeline(
        compiled,
        compiled_tick,
        authorization,
        expected_authorization_revision=expected_revision,
        snapshot=market,
        candidate_selection_authority=selection_authority,
    )


def test_exact_allowed_path_reproduces_selection_and_preserves_provenance(monkeypatch) -> None:
    values = inputs()
    compiled, compiled_tick, _, market, selection_authority = values
    original = entry_pipeline.select_compiled_vertical_candidate
    calls = 0

    def observed(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(entry_pipeline, "select_compiled_vertical_candidate", observed)

    result = run(values)

    assert calls == 2
    assert (
        result.runtime_authority.disposition is ExperimentRuntimeDisposition.ENTRY_SELECTION_ALLOWED
    )
    assert result.runtime_authority.reason_codes == (
        ExperimentRuntimeReason.ENTRY_CANDIDATE_AUTHORIZED,
    )
    assert result.candidate_selection.reason is CompiledCandidateReason.SELECTED
    assert result.candidate_selection.protocol_hash == compiled.protocol_hash
    assert result.candidate_selection.protocol_source_hash == compiled.compiled_protocol.source_hash
    assert result.candidate_selection.tick_protocol_hash == compiled_tick.protocol_hash
    assert result.candidate_selection.snapshot_hash == market.source_hash
    assert result.candidate_selection.account_fingerprint == selection_authority.account_fingerprint
    assert result.candidate_selection.leg_source_hashes == tuple(
        item.source_hash for item in market.options
    )
    assert result.candidate_selection.candidate is not None


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        ("experiment", ExperimentRuntimeReason.EXPERIMENT_ID_MISMATCH),
        ("source_definition", ExperimentRuntimeReason.SOURCE_DEFINITION_HASH_MISMATCH),
        ("protocol_source", ExperimentRuntimeReason.PROTOCOL_SOURCE_HASH_MISMATCH),
        ("protocol", ExperimentRuntimeReason.PROTOCOL_HASH_MISMATCH),
    ),
)
def test_identity_mismatch_short_circuits_selection(monkeypatch, change, reason) -> None:
    compiled, compiled_tick, authorization, market, selection_authority = inputs()
    if change == "experiment":
        authorization = authorization.model_copy(update={"experiment_id": uuid4()})
    elif change == "source_definition":
        authorization = authorization.model_copy(update={"source_definition_hash": "b" * 64})
    elif change == "protocol_source":
        compiled_tick = compiled_tick.model_copy(update={"protocol_source_hash": "b" * 64})
    else:
        authorization = authorization.model_copy(update={"protocol_hash": "b" * 64})
    calls = 0

    def forbidden(*args):
        nonlocal calls
        calls += 1
        raise AssertionError("selection must not run")

    monkeypatch.setattr(entry_pipeline, "select_compiled_vertical_candidate", forbidden)

    result = run((compiled, compiled_tick, authorization, market, selection_authority))

    assert calls == 0
    assert result.runtime_authority.reason_codes == (reason,)
    assert result.candidate_selection is None


@pytest.mark.parametrize("case", ("stale", "disarmed", "open"))
def test_revision_disarm_and_phase_failures_short_circuit_selection(monkeypatch, case) -> None:
    compiled, compiled_tick, authorization, market, selection_authority = inputs()
    expected_revision = 1
    if case == "stale":
        expected_revision = 2
        expected_reason = ExperimentRuntimeReason.AUTHORIZATION_REVISION_STALE
    elif case == "disarmed":
        authorization = authorization_for(compiled, state="DISARMED", revision=2)
        expected_revision = 2
        expected_reason = ExperimentRuntimeReason.AUTHORIZATION_NOT_ARMED
    else:
        compiled_tick = run_compiled_experiment_tick(
            compiled.compiled_protocol,
            evidence(compiled.compiled_protocol, PositionPhase.OPEN),
            PositionPhase.OPEN,
        )
        expected_reason = ExperimentRuntimeReason.OPEN_CLOSE_DECISION_PRESERVED

    def forbidden(*args):
        raise AssertionError("selection must not run")

    monkeypatch.setattr(entry_pipeline, "select_compiled_vertical_candidate", forbidden)

    result = run(
        (compiled, compiled_tick, authorization, market, selection_authority),
        expected_revision=expected_revision,
    )

    assert result.runtime_authority.reason_codes[0] is expected_reason
    assert result.candidate_selection is None


def test_inconsistent_tick_phase_fails_before_selection(monkeypatch) -> None:
    compiled, compiled_tick, authorization, market, selection_authority = inputs()
    inconsistent = compiled_tick.model_copy(update={"position_phase": PositionPhase.OPEN})
    calls = 0

    def forbidden(*args):
        nonlocal calls
        calls += 1
        raise AssertionError("selection must not run")

    monkeypatch.setattr(entry_pipeline, "select_compiled_vertical_candidate", forbidden)

    with pytest.raises(
        ExperimentRuntimeAuthorityBlocked,
        match="EXPERIMENT_RUNTIME_TICK_INVALID",
    ):
        run((compiled, inconsistent, authorization, market, selection_authority))

    assert calls == 0


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("none", CompiledCandidateReason.NO_ELIGIBLE_CANDIDATE),
        ("risk", CompiledCandidateReason.RISK_AUTHORITY_INSUFFICIENT),
        ("tie", CompiledCandidateReason.UNRESOLVED_TIE),
    ),
)
def test_allowed_path_preserves_no_candidate_tie_and_risk_reasons(case, expected_reason) -> None:
    if case == "none":
        values = inputs(options=(option("P", "500", "1", "1.02"),))
    else:
        lower = option("C", "500", "2", "2.02")
        upper = option("C", "504", "0.5", "0.52")
        values = inputs(options=(lower, upper, lower, upper) if case == "tie" else (lower, upper))
        if case == "risk":
            compiled, compiled_tick, authorization, market, selection_authority = values
            selection_authority = authority(
                market,
                available_risk=Decimal("1"),
                available_buying_power=Decimal("1"),
            )
            values = compiled, compiled_tick, authorization, market, selection_authority

    result = run(values)

    assert (
        result.runtime_authority.disposition is ExperimentRuntimeDisposition.ENTRY_SELECTION_ALLOWED
    )
    assert result.candidate_selection.reason is expected_reason
    assert result.candidate_selection.candidate is None


def test_selected_output_grants_literal_false_side_effect_authority() -> None:
    result = run(inputs())

    assert result.authority_state == "NON_AUTHORITATIVE"
    assert result.automation_state == "OFF"
    assert result.schedule_authorized is False
    assert result.provider_access_authorized is False
    assert result.broker_access_authorized is False
    assert result.order_authorized is False
    assert result.execution_eligible is False
    assert result.candidate_selection.execution_eligible is False


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"authority_state": "AUTHORITATIVE"}, "cannot grant authority"),
        ({"arm_state": "ARMED"}, "cannot grant authority"),
        ({"automation_state": "ON"}, "cannot grant authority"),
        ({"execution_eligible": True}, "cannot grant authority"),
        ({"snapshot_hash": "b" * 64}, "lineage mismatch"),
        ({"account_fingerprint": "b" * 64}, "lineage mismatch"),
    ),
)
def test_result_rejects_forged_nested_selection(change, message) -> None:
    result = run(inputs())
    forged = replace(result.candidate_selection, **change)

    with pytest.raises(ValueError, match=message):
        result.model_copy(update={"candidate_selection": forged}).model_validate(
            {
                **result.model_dump(),
                "candidate_selection": forged,
                "validated_snapshot": result.validated_snapshot,
                "validated_candidate_authority": result.validated_candidate_authority,
                "validated_compiled": result.validated_compiled,
                "validated_tick": result.validated_tick,
                "validated_authorization": result.validated_authorization,
            }
        )


def test_result_rejects_paired_snapshot_and_account_identity_forgery() -> None:
    result = run(inputs())
    forged = replace(
        result.candidate_selection,
        snapshot_hash="b" * 64,
        account_fingerprint="c" * 64,
    )

    with pytest.raises(ValueError, match="pipeline input identity mismatch"):
        result.model_validate(
            {
                **result.model_dump(),
                "snapshot_hash": "b" * 64,
                "account_fingerprint": "c" * 64,
                "candidate_selection": forged,
                "validated_snapshot": result.validated_snapshot,
                "validated_candidate_authority": result.validated_candidate_authority,
                "validated_compiled": result.validated_compiled,
                "validated_tick": result.validated_tick,
                "validated_authorization": result.validated_authorization,
            }
        )


def test_result_rejects_selection_under_weakened_risk_authority() -> None:
    result = run(inputs())
    weak_authority = replace(
        result.validated_candidate_authority,
        available_risk=Decimal("0"),
        available_buying_power=Decimal("0"),
    )

    with pytest.raises(ValueError, match="does not reproduce from exact inputs"):
        result.model_validate(
            {
                **result.model_dump(),
                "validated_snapshot": result.validated_snapshot,
                "validated_candidate_authority": weak_authority,
                "validated_compiled": result.validated_compiled,
                "validated_tick": result.validated_tick,
                "validated_authorization": result.validated_authorization,
            }
        )


def test_result_rejects_forged_candidate_reason_under_exact_inputs() -> None:
    result = run(inputs())
    forged = replace(
        result.candidate_selection,
        reason=CompiledCandidateReason.NO_ELIGIBLE_CANDIDATE,
        candidate=None,
        selection_basis=None,
        leg_source_hashes=(),
    )

    with pytest.raises(ValueError, match="does not reproduce from exact inputs"):
        result.model_validate(
            {
                **result.model_dump(),
                "candidate_selection": forged,
                "validated_snapshot": result.validated_snapshot,
                "validated_candidate_authority": result.validated_candidate_authority,
                "validated_compiled": result.validated_compiled,
                "validated_tick": result.validated_tick,
                "validated_authorization": result.validated_authorization,
            }
        )
