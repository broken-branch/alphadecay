from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from backend.app.contracts.v1.models import ContractModel
from backend.app.strategy_briefs.decision import (
    PositionPhase,
    ProtocolDecisionBlocked,
    ProtocolDecisionClassification,
    classify_protocol_decision,
)
from backend.app.strategy_briefs.protocol import (
    ProtocolEvaluationBlocked,
    evaluate_compiled_protocol,
    verify_compiled_protocol,
)
from backend.app.strategy_briefs.tick import CompiledExperimentTick

from .models import CompiledExperimentVersion, ExperimentAuthorizationStatus


class ExperimentRuntimeAuthorityBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExperimentRuntimeDisposition(StrEnum):
    ENTRY_SELECTION_ALLOWED = "ENTRY_SELECTION_ALLOWED"
    ENTRY_BLOCKED = "ENTRY_BLOCKED"
    OPEN_CLOSE_PRESERVED = "OPEN_CLOSE_PRESERVED"
    OPEN_HOLD_PRESERVED = "OPEN_HOLD_PRESERVED"


class ExperimentRuntimeReason(StrEnum):
    ENTRY_CANDIDATE_AUTHORIZED = "ENTRY_CANDIDATE_AUTHORIZED"
    DECISION_NOT_ENTRY_CANDIDATE = "DECISION_NOT_ENTRY_CANDIDATE"
    EXPERIMENT_ID_MISMATCH = "EXPERIMENT_ID_MISMATCH"
    SOURCE_DEFINITION_HASH_MISMATCH = "SOURCE_DEFINITION_HASH_MISMATCH"
    PROTOCOL_SOURCE_HASH_MISMATCH = "PROTOCOL_SOURCE_HASH_MISMATCH"
    PROTOCOL_HASH_MISMATCH = "PROTOCOL_HASH_MISMATCH"
    AUTHORIZATION_REVISION_STALE = "AUTHORIZATION_REVISION_STALE"
    AUTHORIZATION_NOT_ARMED = "AUTHORIZATION_NOT_ARMED"
    OPEN_CLOSE_DECISION_PRESERVED = "OPEN_CLOSE_DECISION_PRESERVED"
    OPEN_HOLD_DECISION_PRESERVED = "OPEN_HOLD_DECISION_PRESERVED"


class ExperimentRuntimeAuthorityDecision(ContractModel):
    status: Literal["RUNTIME_AUTHORITY_EVALUATED"] = "RUNTIME_AUTHORITY_EVALUATED"
    authority_state: Literal["NON_ORDER_GATE"] = "NON_ORDER_GATE"
    experiment_id: UUID
    source_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_authorization_revision: int = Field(ge=0)
    observed_authorization_revision: int = Field(ge=0)
    authorization_state: Literal["NOT_ARMED", "ARMED", "DISARMED"]
    position_phase: PositionPhase
    protocol_decision: ProtocolDecisionClassification
    disposition: ExperimentRuntimeDisposition
    reason_codes: tuple[ExperimentRuntimeReason, ...] = Field(min_length=1)
    candidate_selection_allowed: bool
    open_position_management_preserved: bool
    schedule_authorized: Literal[False] = False
    provider_access_authorized: Literal[False] = False
    broker_access_authorized: Literal[False] = False
    order_authorized: Literal[False] = False
    execution_eligible: Literal[False] = False

    @model_validator(mode="after")
    def authority_is_consistent(self) -> ExperimentRuntimeAuthorityDecision:
        if any(
            (
                self.schedule_authorized,
                self.provider_access_authorized,
                self.broker_access_authorized,
                self.order_authorized,
                self.execution_eligible,
            )
        ):
            raise ValueError("runtime authority cannot grant side-effect authority")
        if self.position_phase is PositionPhase.FLAT:
            if self.disposition not in {
                ExperimentRuntimeDisposition.ENTRY_SELECTION_ALLOWED,
                ExperimentRuntimeDisposition.ENTRY_BLOCKED,
            }:
                raise ValueError("flat runtime disposition is invalid")
            allowed = self.disposition is ExperimentRuntimeDisposition.ENTRY_SELECTION_ALLOWED
            if (
                self.open_position_management_preserved
                or self.candidate_selection_allowed != allowed
                or allowed
                != (
                    self.protocol_decision is ProtocolDecisionClassification.ENTRY_CANDIDATE
                    and self.reason_codes == (ExperimentRuntimeReason.ENTRY_CANDIDATE_AUTHORIZED,)
                )
                or (
                    allowed
                    and (
                        self.authorization_state != "ARMED"
                        or self.expected_authorization_revision
                        != self.observed_authorization_revision
                    )
                )
            ):
                raise ValueError("flat runtime authority is inconsistent")
        else:
            expected = {
                ProtocolDecisionClassification.CLOSE_CANDIDATE: (
                    ExperimentRuntimeDisposition.OPEN_CLOSE_PRESERVED,
                    ExperimentRuntimeReason.OPEN_CLOSE_DECISION_PRESERVED,
                ),
                ProtocolDecisionClassification.HOLD: (
                    ExperimentRuntimeDisposition.OPEN_HOLD_PRESERVED,
                    ExperimentRuntimeReason.OPEN_HOLD_DECISION_PRESERVED,
                ),
            }.get(self.protocol_decision)
            if (
                expected is None
                or self.disposition is not expected[0]
                or self.reason_codes[0] is not expected[1]
                or self.candidate_selection_allowed
                or not self.open_position_management_preserved
            ):
                raise ValueError("open runtime authority is inconsistent")
        return self


def evaluate_experiment_runtime_authority(
    compiled: CompiledExperimentVersion,
    tick: CompiledExperimentTick,
    authorization: ExperimentAuthorizationStatus,
    *,
    expected_authorization_revision: int,
) -> ExperimentRuntimeAuthorityDecision:
    compiled, tick, authorization = _validated_inputs(compiled, tick, authorization)
    if type(expected_authorization_revision) is not int or expected_authorization_revision < 0:
        raise ExperimentRuntimeAuthorityBlocked("EXPERIMENT_RUNTIME_REVISION_INVALID")
    identity_reasons = _identity_reasons(compiled, tick, authorization)
    revision_reasons = (
        (ExperimentRuntimeReason.AUTHORIZATION_REVISION_STALE,)
        if authorization.authorization_revision != expected_authorization_revision
        else ()
    )
    common = {
        "experiment_id": compiled.experiment_id,
        "source_definition_hash": compiled.source_definition_hash,
        "protocol_source_hash": compiled.compiled_protocol.source_hash,
        "protocol_hash": compiled.protocol_hash,
        "expected_authorization_revision": expected_authorization_revision,
        "observed_authorization_revision": authorization.authorization_revision,
        "authorization_state": authorization.authorization_state,
        "position_phase": tick.position_phase,
        "protocol_decision": tick.decision.classification,
    }

    if tick.position_phase is PositionPhase.OPEN:
        if tick.decision.classification is ProtocolDecisionClassification.CLOSE_CANDIDATE:
            disposition = ExperimentRuntimeDisposition.OPEN_CLOSE_PRESERVED
            phase_reason = ExperimentRuntimeReason.OPEN_CLOSE_DECISION_PRESERVED
        else:
            disposition = ExperimentRuntimeDisposition.OPEN_HOLD_PRESERVED
            phase_reason = ExperimentRuntimeReason.OPEN_HOLD_DECISION_PRESERVED
        return ExperimentRuntimeAuthorityDecision(
            **common,
            disposition=disposition,
            reason_codes=(phase_reason, *identity_reasons, *revision_reasons),
            candidate_selection_allowed=False,
            open_position_management_preserved=True,
        )

    entry_reasons: tuple[ExperimentRuntimeReason, ...] = ()
    if tick.decision.classification is not ProtocolDecisionClassification.ENTRY_CANDIDATE:
        entry_reasons += (ExperimentRuntimeReason.DECISION_NOT_ENTRY_CANDIDATE,)
    entry_reasons += identity_reasons
    entry_reasons += revision_reasons
    if not authorization.entry_authorized or authorization.authorization_state != "ARMED":
        entry_reasons += (ExperimentRuntimeReason.AUTHORIZATION_NOT_ARMED,)
    if entry_reasons:
        return ExperimentRuntimeAuthorityDecision(
            **common,
            disposition=ExperimentRuntimeDisposition.ENTRY_BLOCKED,
            reason_codes=entry_reasons,
            candidate_selection_allowed=False,
            open_position_management_preserved=False,
        )
    return ExperimentRuntimeAuthorityDecision(
        **common,
        disposition=ExperimentRuntimeDisposition.ENTRY_SELECTION_ALLOWED,
        reason_codes=(ExperimentRuntimeReason.ENTRY_CANDIDATE_AUTHORIZED,),
        candidate_selection_allowed=True,
        open_position_management_preserved=False,
    )


def _validated_inputs(
    compiled: CompiledExperimentVersion,
    tick: CompiledExperimentTick,
    authorization: ExperimentAuthorizationStatus,
) -> tuple[
    CompiledExperimentVersion,
    CompiledExperimentTick,
    ExperimentAuthorizationStatus,
]:
    if (
        type(compiled) is not CompiledExperimentVersion
        or type(tick) is not CompiledExperimentTick
        or type(authorization) is not ExperimentAuthorizationStatus
    ):
        raise ExperimentRuntimeAuthorityBlocked("EXPERIMENT_RUNTIME_INPUT_INVALID")
    try:
        compiled = CompiledExperimentVersion.model_validate(compiled.model_dump(mode="python"))
        tick = CompiledExperimentTick.model_validate(tick.model_dump(mode="python"))
        authorization = ExperimentAuthorizationStatus.model_validate(
            authorization.model_dump(mode="python")
        )
    except ValidationError as error:
        raise ExperimentRuntimeAuthorityBlocked("EXPERIMENT_RUNTIME_INPUT_INVALID") from error
    if (
        not verify_compiled_protocol(compiled.compiled_protocol)
        or not _tick_is_consistent(tick)
        or not _matching_tick_is_exact(compiled, tick)
    ):
        raise ExperimentRuntimeAuthorityBlocked("EXPERIMENT_RUNTIME_TICK_INVALID")
    return compiled, tick, authorization


def _tick_is_consistent(tick: CompiledExperimentTick) -> bool:
    return (
        tick.protocol_hash
        == tick.observation.protocol_hash
        == tick.evaluation.protocol_hash
        == tick.decision.protocol_hash
        and tick.position_phase == tick.observation.position_phase == tick.decision.position_phase
        and tick.observation_source_hashes == tick.observation.source_hashes
        and (
            tick.decision.classification
            in {
                ProtocolDecisionClassification.BLOCKED,
                ProtocolDecisionClassification.ENTRY_CANDIDATE,
                ProtocolDecisionClassification.STAND_ASIDE,
            }
        )
        == (tick.position_phase is PositionPhase.FLAT)
    )


def _matching_tick_is_exact(
    compiled: CompiledExperimentVersion,
    tick: CompiledExperimentTick,
) -> bool:
    protocol = compiled.compiled_protocol
    if (
        tick.protocol_hash != compiled.protocol_hash
        or tick.protocol_source_hash != protocol.source_hash
    ):
        return True
    try:
        evaluation = evaluate_compiled_protocol(protocol, tick.observation.observations)
        decision = classify_protocol_decision(protocol, evaluation, tick.position_phase)
    except (ProtocolEvaluationBlocked, ProtocolDecisionBlocked):
        return False
    return tick.evaluation == evaluation and tick.decision == decision


def _identity_reasons(
    compiled: CompiledExperimentVersion,
    tick: CompiledExperimentTick,
    authorization: ExperimentAuthorizationStatus,
) -> tuple[ExperimentRuntimeReason, ...]:
    reasons: list[ExperimentRuntimeReason] = []
    if authorization.experiment_id != compiled.experiment_id:
        reasons.append(ExperimentRuntimeReason.EXPERIMENT_ID_MISMATCH)
    if authorization.source_definition_hash != compiled.source_definition_hash:
        reasons.append(ExperimentRuntimeReason.SOURCE_DEFINITION_HASH_MISMATCH)
    if tick.protocol_source_hash != compiled.compiled_protocol.source_hash:
        reasons.append(ExperimentRuntimeReason.PROTOCOL_SOURCE_HASH_MISMATCH)
    if (
        authorization.protocol_hash != compiled.protocol_hash
        or tick.protocol_hash != compiled.protocol_hash
    ):
        reasons.append(ExperimentRuntimeReason.PROTOCOL_HASH_MISMATCH)
    return tuple(reasons)
