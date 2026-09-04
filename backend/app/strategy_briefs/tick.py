from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, ValidationError

from .decision import (
    PositionPhase,
    ProtocolDecision,
    ProtocolDecisionBlocked,
    classify_protocol_decision,
)
from .observations import (
    AcquiredProtocolEvidence,
    ProtocolObservationBlocked,
    ProtocolObservationBundle,
    build_protocol_observations,
)
from .protocol import (
    CompiledStrategyProtocol,
    ProtocolEvaluation,
    ProtocolEvaluationBlocked,
    ProtocolModel,
    evaluate_compiled_protocol,
)


class ProtocolTickBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CompiledExperimentTick(ProtocolModel):
    status: Literal["TICK_CLASSIFIED"] = "TICK_CLASSIFIED"
    authority_state: Literal["NON_AUTHORITATIVE"] = "NON_AUTHORITATIVE"
    arm_state: Literal["NOT_ARMED"] = "NOT_ARMED"
    automation_state: Literal["OFF"] = "OFF"
    execution_eligible: Literal[False] = False
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    position_phase: PositionPhase
    observed_at: datetime
    session_date: date
    observation_source_hashes: tuple[str, ...]
    observation: ProtocolObservationBundle
    evaluation: ProtocolEvaluation
    decision: ProtocolDecision


def run_compiled_experiment_tick(
    protocol: CompiledStrategyProtocol,
    evidence: AcquiredProtocolEvidence,
    position_phase: PositionPhase,
) -> CompiledExperimentTick:
    try:
        observation = build_protocol_observations(protocol, evidence, position_phase)
        evaluation = evaluate_compiled_protocol(protocol, observation.observations)
        decision = classify_protocol_decision(protocol, evaluation, position_phase)
        return CompiledExperimentTick(
            protocol_hash=protocol.protocol_hash,
            protocol_source_hash=protocol.source_hash,
            position_phase=position_phase,
            observed_at=evidence.observed_at,
            session_date=evidence.session_date,
            observation_source_hashes=observation.source_hashes,
            observation=observation,
            evaluation=evaluation,
            decision=decision,
        )
    except (
        ProtocolObservationBlocked,
        ProtocolEvaluationBlocked,
        ProtocolDecisionBlocked,
    ) as error:
        raise ProtocolTickBlocked(error.code) from error
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise ProtocolTickBlocked("PROTOCOL_TICK_INVALID") from error
