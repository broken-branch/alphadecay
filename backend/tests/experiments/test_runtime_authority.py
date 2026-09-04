from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from backend.app.experiments.models import (
    CompiledExperimentVersion,
    ExperimentAuthorizationStatus,
)
from backend.app.experiments.runtime_authority import (
    ExperimentRuntimeAuthorityBlocked,
    ExperimentRuntimeDisposition,
    ExperimentRuntimeReason,
    evaluate_experiment_runtime_authority,
)
from backend.app.strategy_briefs.decision import (
    PositionPhase,
    ProtocolDecisionClassification,
    ProtocolDecisionReason,
)
from backend.app.strategy_briefs.protocol import (
    ProtocolFact,
    ProtocolMetric,
    compile_reviewed_protocol,
)
from backend.app.strategy_briefs.tick import (
    CompiledExperimentTick,
    run_compiled_experiment_tick,
)
from backend.tests.strategy_briefs.test_executable_protocol import request
from backend.tests.strategy_briefs.test_protocol_observations import evidence

NOW = datetime(2026, 9, 1, 18, tzinfo=UTC)
SOURCE_DEFINITION_HASH = "d" * 64
EVENT_HASH = "a" * 64


def runtime_inputs(
    phase: PositionPhase = PositionPhase.FLAT,
) -> tuple[
    CompiledExperimentVersion,
    CompiledExperimentTick,
    ExperimentAuthorizationStatus,
]:
    protocol = compile_reviewed_protocol(request())
    compiled = CompiledExperimentVersion(
        experiment_id=uuid4(),
        source_definition_hash=SOURCE_DEFINITION_HASH,
        protocol_hash=protocol.protocol_hash,
        compiled_protocol=protocol,
        created_at=NOW,
    )
    tick = run_compiled_experiment_tick(protocol, evidence(protocol, phase), phase)
    authorization = authorization_for(compiled)
    return compiled, tick, authorization


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


def test_exact_current_arm_allows_only_later_candidate_selection() -> None:
    compiled, tick, authorization = runtime_inputs()

    result = evaluate_experiment_runtime_authority(
        compiled,
        tick,
        authorization,
        expected_authorization_revision=1,
    )

    assert result.disposition is ExperimentRuntimeDisposition.ENTRY_SELECTION_ALLOWED
    assert result.reason_codes == (ExperimentRuntimeReason.ENTRY_CANDIDATE_AUTHORIZED,)
    assert result.candidate_selection_allowed is True
    assert result.open_position_management_preserved is False
    assert result.schedule_authorized is False
    assert result.provider_access_authorized is False
    assert result.broker_access_authorized is False
    assert result.order_authorized is False
    assert result.execution_eligible is False


@pytest.mark.parametrize(
    ("state", "revision"),
    (("NOT_ARMED", 0), ("DISARMED", 2)),
)
def test_unarmed_or_disarmed_state_blocks_future_entry(
    state: str,
    revision: int,
) -> None:
    compiled, tick, _ = runtime_inputs()
    authorization = authorization_for(compiled, state=state, revision=revision)

    result = evaluate_experiment_runtime_authority(
        compiled,
        tick,
        authorization,
        expected_authorization_revision=revision,
    )

    assert result.disposition is ExperimentRuntimeDisposition.ENTRY_BLOCKED
    assert result.reason_codes == (ExperimentRuntimeReason.AUTHORIZATION_NOT_ARMED,)
    assert result.candidate_selection_allowed is False


def test_stale_authorization_revision_blocks_future_entry() -> None:
    compiled, tick, authorization = runtime_inputs()

    result = evaluate_experiment_runtime_authority(
        compiled,
        tick,
        authorization,
        expected_authorization_revision=2,
    )

    assert result.reason_codes == (ExperimentRuntimeReason.AUTHORIZATION_REVISION_STALE,)
    assert result.disposition is ExperimentRuntimeDisposition.ENTRY_BLOCKED


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        ("experiment", ExperimentRuntimeReason.EXPERIMENT_ID_MISMATCH),
        ("source_definition", ExperimentRuntimeReason.SOURCE_DEFINITION_HASH_MISMATCH),
        ("protocol_source", ExperimentRuntimeReason.PROTOCOL_SOURCE_HASH_MISMATCH),
        ("authorization_protocol", ExperimentRuntimeReason.PROTOCOL_HASH_MISMATCH),
    ),
)
def test_identity_mismatch_blocks_future_entry(
    change: str,
    reason: ExperimentRuntimeReason,
) -> None:
    compiled, tick, authorization = runtime_inputs()
    if change == "experiment":
        authorization = authorization.model_copy(update={"experiment_id": uuid4()})
    elif change == "source_definition":
        authorization = authorization.model_copy(update={"source_definition_hash": "b" * 64})
    elif change == "protocol_source":
        tick = tick.model_copy(update={"protocol_source_hash": "b" * 64})
    else:
        authorization = authorization.model_copy(update={"protocol_hash": "b" * 64})

    result = evaluate_experiment_runtime_authority(
        compiled,
        tick,
        authorization,
        expected_authorization_revision=1,
    )

    assert result.reason_codes == (reason,)
    assert result.disposition is ExperimentRuntimeDisposition.ENTRY_BLOCKED


def test_internally_exact_tick_for_another_protocol_cannot_enter() -> None:
    compiled, _, authorization = runtime_inputs()
    changed_request = request()
    changed_market_quality = changed_request.definition.market_quality.model_copy(
        update={"maximum_underlying_age_seconds": 299}
    )
    changed_definition = changed_request.definition.model_copy(
        update={"market_quality": changed_market_quality}
    )
    changed_protocol = compile_reviewed_protocol(
        changed_request.model_copy(update={"definition": changed_definition})
    )
    changed_tick = run_compiled_experiment_tick(
        changed_protocol,
        evidence(changed_protocol, PositionPhase.FLAT),
        PositionPhase.FLAT,
    )

    result = evaluate_experiment_runtime_authority(
        compiled,
        changed_tick,
        authorization,
        expected_authorization_revision=1,
    )

    assert result.reason_codes == (ExperimentRuntimeReason.PROTOCOL_HASH_MISMATCH,)
    assert result.disposition is ExperimentRuntimeDisposition.ENTRY_BLOCKED


def test_non_entry_flat_decision_outranks_an_exact_arm() -> None:
    compiled, _, authorization = runtime_inputs()
    protocol = compiled.compiled_protocol
    acquired = evidence(protocol, PositionPhase.FLAT)
    facts = {**acquired.fact_values, ProtocolFact.PAPER_ACCOUNT_CONFIRMED: False}
    blocked_tick = run_compiled_experiment_tick(
        protocol,
        acquired.model_copy(update={"fact_values": facts}),
        PositionPhase.FLAT,
    )

    result = evaluate_experiment_runtime_authority(
        compiled,
        blocked_tick,
        authorization,
        expected_authorization_revision=1,
    )

    assert result.protocol_decision is ProtocolDecisionClassification.BLOCKED
    assert result.reason_codes == (ExperimentRuntimeReason.DECISION_NOT_ENTRY_CANDIDATE,)
    assert result.disposition is ExperimentRuntimeDisposition.ENTRY_BLOCKED


@pytest.mark.parametrize(
    "change",
    (
        "disarmed",
        "stale",
        "experiment",
        "source_definition",
        "protocol_source",
        "protocol",
    ),
)
def test_open_close_management_outranks_entry_authorization_failures(
    change: str,
) -> None:
    compiled, tick, authorization = runtime_inputs(PositionPhase.OPEN)
    expected_revision = 1
    if change == "disarmed":
        authorization = authorization_for(compiled, state="DISARMED", revision=2)
        expected_revision = 2
    elif change == "stale":
        expected_revision = 2
    elif change == "experiment":
        authorization = authorization.model_copy(update={"experiment_id": uuid4()})
    elif change == "source_definition":
        authorization = authorization.model_copy(update={"source_definition_hash": "b" * 64})
    elif change == "protocol_source":
        tick = tick.model_copy(update={"protocol_source_hash": "b" * 64})
    elif change == "protocol":
        authorization = authorization.model_copy(update={"protocol_hash": "b" * 64})

    result = evaluate_experiment_runtime_authority(
        compiled,
        tick,
        authorization,
        expected_authorization_revision=expected_revision,
    )

    assert result.protocol_decision is ProtocolDecisionClassification.CLOSE_CANDIDATE
    assert result.disposition is ExperimentRuntimeDisposition.OPEN_CLOSE_PRESERVED
    assert result.reason_codes[0] is ExperimentRuntimeReason.OPEN_CLOSE_DECISION_PRESERVED
    assert result.candidate_selection_allowed is False
    assert result.open_position_management_preserved is True
    assert result.order_authorized is False


def test_open_hold_management_is_preserved_after_disarm() -> None:
    compiled, _, _ = runtime_inputs(PositionPhase.OPEN)
    protocol = compiled.compiled_protocol
    acquired = evidence(protocol, PositionPhase.OPEN)
    numeric = {
        **acquired.numeric_values,
        ProtocolMetric.POSITION_RETURN_ON_MAX_RISK_PERCENT: Decimal("0"),
    }
    hold_tick = run_compiled_experiment_tick(
        protocol,
        acquired.model_copy(update={"numeric_values": numeric}),
        PositionPhase.OPEN,
    )
    authorization = authorization_for(compiled, state="DISARMED", revision=2)

    result = evaluate_experiment_runtime_authority(
        compiled,
        hold_tick,
        authorization,
        expected_authorization_revision=2,
    )

    assert result.protocol_decision is ProtocolDecisionClassification.HOLD
    assert result.disposition is ExperimentRuntimeDisposition.OPEN_HOLD_PRESERVED
    assert result.reason_codes == (ExperimentRuntimeReason.OPEN_HOLD_DECISION_PRESERVED,)
    assert result.open_position_management_preserved is True


def test_inconsistent_tick_phase_fails_closed() -> None:
    compiled, tick, authorization = runtime_inputs()
    inconsistent = tick.model_copy(update={"position_phase": PositionPhase.OPEN})

    with pytest.raises(
        ExperimentRuntimeAuthorityBlocked,
        match="EXPERIMENT_RUNTIME_TICK_INVALID",
    ):
        evaluate_experiment_runtime_authority(
            compiled,
            inconsistent,
            authorization,
            expected_authorization_revision=1,
        )


def test_reclassified_tick_fails_closed() -> None:
    compiled, tick, authorization = runtime_inputs()
    changed_decision = tick.decision.model_copy(
        update={
            "classification": ProtocolDecisionClassification.STAND_ASIDE,
            "reason_codes": (ProtocolDecisionReason.ENTRY_RULE_NOT_MATCHED,),
        }
    )
    inconsistent = tick.model_copy(update={"decision": changed_decision})

    with pytest.raises(
        ExperimentRuntimeAuthorityBlocked,
        match="EXPERIMENT_RUNTIME_TICK_INVALID",
    ):
        evaluate_experiment_runtime_authority(
            compiled,
            inconsistent,
            authorization,
            expected_authorization_revision=1,
        )


def test_invalid_revision_or_input_fails_closed() -> None:
    compiled, tick, authorization = runtime_inputs()

    with pytest.raises(
        ExperimentRuntimeAuthorityBlocked,
        match="EXPERIMENT_RUNTIME_REVISION_INVALID",
    ):
        evaluate_experiment_runtime_authority(
            compiled,
            tick,
            authorization,
            expected_authorization_revision=-1,
        )
    with pytest.raises(
        ExperimentRuntimeAuthorityBlocked,
        match="EXPERIMENT_RUNTIME_REVISION_INVALID",
    ):
        evaluate_experiment_runtime_authority(
            compiled,
            tick,
            authorization,
            expected_authorization_revision=True,
        )
    with pytest.raises(
        ExperimentRuntimeAuthorityBlocked,
        match="EXPERIMENT_RUNTIME_INPUT_INVALID",
    ):
        evaluate_experiment_runtime_authority(
            compiled,
            object(),
            authorization,
            expected_authorization_revision=1,
        )
