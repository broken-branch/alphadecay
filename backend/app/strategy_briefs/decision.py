from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from .protocol import (
    CompiledStrategyProtocol,
    ProtocolEvaluation,
    ProtocolModel,
    _compiled_protocol_hash,
)


class ProtocolDecisionBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PositionPhase(StrEnum):
    FLAT = "FLAT"
    OPEN = "OPEN"


class ProtocolDecisionClassification(StrEnum):
    BLOCKED = "BLOCKED"
    ENTRY_CANDIDATE = "ENTRY_CANDIDATE"
    STAND_ASIDE = "STAND_ASIDE"
    CLOSE_CANDIDATE = "CLOSE_CANDIDATE"
    HOLD = "HOLD"


class ProtocolDecisionReason(StrEnum):
    MANDATORY_SAFETY_GATE_FAILED = "MANDATORY_SAFETY_GATE_FAILED"
    INVALIDATION_RULE_MATCHED = "INVALIDATION_RULE_MATCHED"
    NO_TRADE_RULE_MATCHED = "NO_TRADE_RULE_MATCHED"
    ENTRY_RULE_MATCHED = "ENTRY_RULE_MATCHED"
    ENTRY_RULE_NOT_MATCHED = "ENTRY_RULE_NOT_MATCHED"
    LOSS_EXIT_RULE_MATCHED = "LOSS_EXIT_RULE_MATCHED"
    TIME_EXIT_RULE_MATCHED = "TIME_EXIT_RULE_MATCHED"
    PROFIT_EXIT_RULE_MATCHED = "PROFIT_EXIT_RULE_MATCHED"
    NO_EXIT_RULE_MATCHED = "NO_EXIT_RULE_MATCHED"


class ProtocolDecision(ProtocolModel):
    status: Literal["CLASSIFIED"] = "CLASSIFIED"
    authority_state: Literal["NON_AUTHORITATIVE"] = "NON_AUTHORITATIVE"
    arm_state: Literal["NOT_ARMED"] = "NOT_ARMED"
    automation_state: Literal["OFF"] = "OFF"
    execution_eligible: Literal[False] = False
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    position_phase: PositionPhase
    classification: ProtocolDecisionClassification
    reason_codes: tuple[ProtocolDecisionReason, ...] = Field(min_length=1, max_length=4)
    matched_invalidation_rule_numbers: tuple[int, ...] = Field(default=(), max_length=12)

    @model_validator(mode="after")
    def classification_is_consistent(self) -> ProtocolDecision:
        invalidation_reported = (
            ProtocolDecisionReason.INVALIDATION_RULE_MATCHED in self.reason_codes
        )
        allowed = {
            PositionPhase.FLAT: {
                ProtocolDecisionClassification.BLOCKED,
                ProtocolDecisionClassification.ENTRY_CANDIDATE,
                ProtocolDecisionClassification.STAND_ASIDE,
            },
            PositionPhase.OPEN: {
                ProtocolDecisionClassification.CLOSE_CANDIDATE,
                ProtocolDecisionClassification.HOLD,
            },
        }
        if (
            self.classification not in allowed[self.position_phase]
            or invalidation_reported != bool(self.matched_invalidation_rule_numbers)
            or tuple(sorted(set(self.matched_invalidation_rule_numbers)))
            != self.matched_invalidation_rule_numbers
            or any(number < 1 for number in self.matched_invalidation_rule_numbers)
        ):
            raise ValueError("protocol decision is inconsistent")
        return self


def classify_protocol_decision(
    protocol: CompiledStrategyProtocol,
    evaluation: ProtocolEvaluation,
    position_phase: PositionPhase,
) -> ProtocolDecision:
    if (
        type(protocol) is not CompiledStrategyProtocol
        or type(evaluation) is not ProtocolEvaluation
        or type(position_phase) is not PositionPhase
    ):
        raise ProtocolDecisionBlocked("PROTOCOL_DECISION_INPUT_INVALID")
    try:
        protocol = CompiledStrategyProtocol.model_validate(protocol.model_dump(mode="python"))
        evaluation = ProtocolEvaluation.model_validate(evaluation.model_dump(mode="python"))
    except ValidationError as exc:
        raise ProtocolDecisionBlocked("PROTOCOL_DECISION_INPUT_INVALID") from exc
    if protocol.protocol_hash != _compiled_protocol_hash(protocol):
        raise ProtocolDecisionBlocked("PROTOCOL_HASH_MISMATCH")
    if evaluation.protocol_hash != protocol.protocol_hash or len(
        evaluation.invalidation_rule_matches
    ) != len(protocol.rules.invalidation_rules):
        raise ProtocolDecisionBlocked("PROTOCOL_EVALUATION_MISMATCH")

    invalidation_numbers = tuple(
        index
        for index, matched in enumerate(evaluation.invalidation_rule_matches, start=1)
        if matched
    )
    if position_phase is PositionPhase.FLAT and not evaluation.safety_gates_passed:
        return _decision(
            protocol,
            position_phase,
            ProtocolDecisionClassification.BLOCKED,
            (ProtocolDecisionReason.MANDATORY_SAFETY_GATE_FAILED,),
        )
    if position_phase is PositionPhase.FLAT:
        reasons: list[ProtocolDecisionReason] = []
        if invalidation_numbers:
            reasons.append(ProtocolDecisionReason.INVALIDATION_RULE_MATCHED)
        if evaluation.no_trade_rule_matched:
            reasons.append(ProtocolDecisionReason.NO_TRADE_RULE_MATCHED)
        if reasons:
            return _decision(
                protocol,
                position_phase,
                ProtocolDecisionClassification.STAND_ASIDE,
                tuple(reasons),
                invalidation_numbers,
            )
        if evaluation.entry_rule_matched:
            return _decision(
                protocol,
                position_phase,
                ProtocolDecisionClassification.ENTRY_CANDIDATE,
                (ProtocolDecisionReason.ENTRY_RULE_MATCHED,),
            )
        return _decision(
            protocol,
            position_phase,
            ProtocolDecisionClassification.STAND_ASIDE,
            (ProtocolDecisionReason.ENTRY_RULE_NOT_MATCHED,),
        )

    reasons = []
    if invalidation_numbers:
        reasons.append(ProtocolDecisionReason.INVALIDATION_RULE_MATCHED)
    if evaluation.loss_exit_rule_matched:
        reasons.append(ProtocolDecisionReason.LOSS_EXIT_RULE_MATCHED)
    if evaluation.time_exit_rule_matched:
        reasons.append(ProtocolDecisionReason.TIME_EXIT_RULE_MATCHED)
    if evaluation.profit_exit_rule_matched:
        reasons.append(ProtocolDecisionReason.PROFIT_EXIT_RULE_MATCHED)
    if reasons:
        return _decision(
            protocol,
            position_phase,
            ProtocolDecisionClassification.CLOSE_CANDIDATE,
            tuple(reasons),
            invalidation_numbers,
        )
    return _decision(
        protocol,
        position_phase,
        ProtocolDecisionClassification.HOLD,
        (ProtocolDecisionReason.NO_EXIT_RULE_MATCHED,),
    )


def _decision(
    protocol: CompiledStrategyProtocol,
    position_phase: PositionPhase,
    classification: ProtocolDecisionClassification,
    reason_codes: tuple[ProtocolDecisionReason, ...],
    invalidation_numbers: tuple[int, ...] = (),
) -> ProtocolDecision:
    return ProtocolDecision(
        protocol_hash=protocol.protocol_hash,
        position_phase=position_phase,
        classification=classification,
        reason_codes=reason_codes,
        matched_invalidation_rule_numbers=invalidation_numbers,
    )
