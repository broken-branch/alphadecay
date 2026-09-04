from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from backend.app.contracts.v1.models import ContractModel
from backend.app.strategy_briefs.models import (
    StrategyBriefRequest,
    StrategyCurationResponse,
    StrategyProtocolFields,
)
from backend.app.strategy_briefs.protocol import (
    CompiledStrategyProtocol,
    ReviewedProtocolDefinition,
    ReviewedProtocolRules,
)


class ReviewedStrategyCurationInput(StrategyCurationResponse):
    pass


class ReviewedExperimentCreateRequest(ContractModel):
    original_thesis: StrategyBriefRequest
    reviewed_protocol: StrategyProtocolFields
    curation: ReviewedStrategyCurationInput

    @model_validator(mode="after")
    def curation_matches_reviewed_source(self) -> ReviewedExperimentCreateRequest:
        if (
            self.curation.intake != self.original_thesis
            or self.curation.protocol_fields != self.reviewed_protocol
        ):
            raise ValueError("curation must match the reviewed source")
        return self


class ReviewedExperimentDefinition(ContractModel):
    experiment_id: UUID
    version: Literal[1] = 1
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle_state: Literal["REVIEWED"] = "REVIEWED"
    automation_state: Literal["OFF"] = "OFF"
    execution_eligible: Literal[False] = False
    paper_trading_only: Literal[True] = True
    original_thesis: StrategyBriefRequest
    reviewed_protocol: StrategyProtocolFields
    curation: StrategyCurationResponse
    created_at: datetime

    @model_validator(mode="after")
    def curation_matches_reviewed_source(self) -> ReviewedExperimentDefinition:
        if (
            self.curation.intake != self.original_thesis
            or self.curation.protocol_fields != self.reviewed_protocol
        ):
            raise ValueError("curation must match the reviewed source")
        return self


class ReviewedExperimentListResponse(ContractModel):
    experiments: tuple[ReviewedExperimentDefinition, ...]


class CompileExperimentRequest(ContractModel):
    source_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    definition: ReviewedProtocolDefinition
    rules: ReviewedProtocolRules


class CompiledExperimentVersion(ContractModel):
    experiment_id: UUID
    source_version: Literal[1] = 1
    compiled_version: Literal[1] = 1
    source_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle_state: Literal["COMPILED"] = "COMPILED"
    arm_state: Literal["NOT_ARMED"] = "NOT_ARMED"
    automation_state: Literal["OFF"] = "OFF"
    execution_eligible: Literal[False] = False
    paper_trading_only: Literal[True] = True
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_protocol: CompiledStrategyProtocol
    created_at: datetime

    @model_validator(mode="after")
    def protocol_hash_matches(self) -> CompiledExperimentVersion:
        if self.protocol_hash != self.compiled_protocol.protocol_hash:
            raise ValueError("compiled protocol hash mismatch")
        return self


class ExperimentAuthorizationRequest(ContractModel):
    source_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)


class ExperimentAuthorizationStatus(ContractModel):
    experiment_id: UUID
    source_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_revision: int = Field(ge=0)
    authorization_state: Literal["NOT_ARMED", "ARMED", "DISARMED"]
    entry_authorized: bool
    existing_position_risk_management_preserved: Literal[True] = True
    runtime_state: Literal["NOT_CONNECTED"] = "NOT_CONNECTED"
    execution_eligible: Literal[False] = False
    paper_trading_only: Literal[True] = True
    authorization_event_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def state_matches_entry_authority(self) -> ExperimentAuthorizationStatus:
        if self.entry_authorized != (self.authorization_state == "ARMED"):
            raise ValueError("authorization state does not match entry authority")
        if self.authorization_revision == 0:
            if self.authorization_state != "NOT_ARMED":
                raise ValueError("initial authorization state must not be armed")
            if self.authorization_event_hash is not None or self.updated_at is not None:
                raise ValueError("initial authorization state cannot have an event")
        elif self.authorization_event_hash is None or self.updated_at is None:
            raise ValueError("persisted authorization state requires an event")
        return self


class ExperimentPerformanceLineage(ContractModel):
    experiment_id: UUID
    source_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExperimentMoneyMetric(ContractModel):
    value: Decimal | None
    unavailable_reason: Literal["NO_OPENED_TRADES", "NO_CLOSED_TRADES"] | None

    @model_validator(mode="after")
    def availability_is_consistent(self) -> ExperimentMoneyMetric:
        if (self.value is None) == (self.unavailable_reason is None):
            raise ValueError("experiment metric availability is inconsistent")
        return self


class ExperimentCountMetric(ContractModel):
    value: int | None = Field(default=None, ge=0)
    unavailable_reason: Literal["NO_OPENED_TRADES", "NO_CLOSED_TRADES"] | None

    @model_validator(mode="after")
    def availability_is_consistent(self) -> ExperimentCountMetric:
        if (self.value is None) == (self.unavailable_reason is None):
            raise ValueError("experiment metric availability is inconsistent")
        return self


class ExperimentPerformanceResponse(ContractModel):
    lineage: ExperimentPerformanceLineage
    decision_count: int = Field(ge=0)
    opened_trade_count: int = Field(ge=0)
    closed_trade_count: int = Field(ge=0)
    terminal_state: Literal["NO_POSITION", "OPEN", "CLOSED"]
    total_defined_maximum_risk_at_entry: ExperimentMoneyMetric
    entry_cash_flow: ExperimentMoneyMetric
    management_cash_flow: ExperimentMoneyMetric
    exit_cash_flow: ExperimentMoneyMetric
    realized_strategy_pnl: ExperimentMoneyMetric
    win_count: ExperimentCountMetric
    loss_count: ExperimentCountMetric
    breakeven_count: ExperimentCountMetric
