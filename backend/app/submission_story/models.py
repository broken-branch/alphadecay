from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.competition_archive.models import require_aware
from backend.app.experiment_lineage import ExperimentExecutionLineage


class StrictStoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategySummary(StrictStoryModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
    version: int = Field(gt=0)
    underlying: str = Field(pattern=r"^[A-Z]{1,6}$")
    frozen_at: datetime

    @field_validator("frozen_at")
    @classmethod
    def aware_frozen_time(cls, value: datetime) -> datetime:
        return require_aware(value)


class RiskLimitSummary(StrictStoryModel):
    description: Literal[
        "Defined-risk options limits stayed binding throughout this paper decision."
    ]
    maximum_loss_usd: Decimal | None = Field(default=None, gt=0)
    numeric_limit_status: Literal["RECORDED", "NOT_APPLICABLE", "NOT_RECORDED"]


class PriceSummary(StrictStoryModel):
    order_type: Literal["NET_DEBIT_LIMIT", "NET_CREDIT_LIMIT"]
    limit_per_share: Decimal = Field(gt=0)


class SelectedSpreadSummary(StrictStoryModel):
    structure: Literal["VERTICAL"] = "VERTICAL"
    option_type: Literal["CALL", "PUT"]
    expiration: date
    long_strike: Decimal = Field(gt=0)
    short_strike: Decimal = Field(gt=0)
    quantity: int = Field(gt=0)
    price: PriceSummary

    @model_validator(mode="after")
    def distinct_strikes(self) -> SelectedSpreadSummary:
        if self.long_strike == self.short_strike:
            raise ValueError("submission story spread strikes must be distinct")
        return self


class AccountImpactSummary(StrictStoryModel):
    status: Literal[
        "NO_MUTATION_AUTHORIZED",
        "BROKER_EFFECT_NOT_RECORDED",
        "TERMINAL_ZERO_FILL_RECONCILED",
        "PARTIAL_FILL_RECONCILED_UNSAFE",
        "RECONCILED_SIMULATED_POSITION_OPEN",
        "RECONCILED_SIMULATED_POSITION_CLOSED",
        "RECOVERY_PENDING",
        "PERMANENTLY_UNSAFE",
    ]
    description: str = Field(min_length=1, max_length=180)
    reconciled_cashflow_usd: Decimal | None = None
    cashflow_status: Literal["RECONCILED", "NOT_APPLICABLE", "NOT_RECORDED"]
    pnl_status: Literal["NOT_RECORDED", "REALIZED_RECONCILED"] = "NOT_RECORDED"
    realized_pnl_usd: Decimal | None = None
    realized_pnl_status: Literal["UNAVAILABLE", "CERTIFIED"] = "UNAVAILABLE"

    @model_validator(mode="after")
    def cashflow_consistency(self) -> AccountImpactSummary:
        if (self.reconciled_cashflow_usd is not None) != (self.cashflow_status == "RECONCILED"):
            raise ValueError("submission story cashflow authority is inconsistent")
        if (self.realized_pnl_usd is not None) != (self.realized_pnl_status == "CERTIFIED"):
            raise ValueError("submission story realized pnl authority is inconsistent")
        if (self.pnl_status == "REALIZED_RECONCILED") != (self.realized_pnl_status == "CERTIFIED"):
            raise ValueError("submission story pnl status is inconsistent")
        return self


class OrderAttemptSummary(StrictStoryModel):
    ordinal: int = Field(ge=0, le=3)
    state: str = Field(pattern=r"^[A-Z][A-Z_]{1,39}$")
    filled_quantity: int = Field(ge=0)
    quantity: int = Field(gt=0)

    @model_validator(mode="after")
    def fill_consistency(self) -> OrderAttemptSummary:
        if self.filled_quantity > self.quantity:
            raise ValueError("submission story attempt fill is inconsistent")
        return self


class OrderLifecycleSummary(StrictStoryModel):
    recording_status: Literal["RECORDED", "NOT_RECORDED", "NOT_APPLICABLE"]
    attempts: tuple[OrderAttemptSummary, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def recording_consistency(self) -> OrderLifecycleSummary:
        if (self.recording_status == "RECORDED") != bool(self.attempts):
            raise ValueError("submission story attempt recording is inconsistent")
        if tuple(item.ordinal for item in self.attempts) != tuple(range(len(self.attempts))):
            raise ValueError("submission story attempt ordinals are inconsistent")
        return self


class EntryExecutionSummary(StrictStoryModel):
    submitted_at: datetime
    filled_at: datetime
    reconciled_at: datetime
    reconciliation_sequence: int = Field(gt=0)

    @field_validator("submitted_at", "filled_at", "reconciled_at")
    @classmethod
    def aware_execution_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def chronological_execution(self) -> EntryExecutionSummary:
        if not self.submitted_at <= self.filled_at <= self.reconciled_at:
            raise ValueError("submission story entry execution is not chronological")
        return self


class LifecycleAssessmentSummary(StrictStoryModel):
    action: Literal["HOLD", "CLOSE", "ROLL", "NO_ACTION"]
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    assessed_at: datetime

    @field_validator("assessed_at")
    @classmethod
    def aware_assessment_time(cls, value: datetime) -> datetime:
        return require_aware(value)


class ManagementPolicySummary(StrictStoryModel):
    evaluation_interval_minutes: Literal[5] = 5
    profit_target_spread_value_pct: Literal[30] = 30
    stop_loss_spread_value_pct: Literal[-20] = -20
    mandatory_close_at: datetime

    @field_validator("mandatory_close_at")
    @classmethod
    def aware_mandatory_close_time(cls, value: datetime) -> datetime:
        return require_aware(value)


# Persisted status and reason codes that the judge export maps to public wording.
RETRY_NO_TRADE_STATUS = "PROVIDER_FAILURE_NO_TRADE"
LIFECYCLE_NO_ACTION_UNBOUND_CODE = "PROVIDER_FAILURE_NO_ACTION"


class ProviderRetryAuditSummary(StrictStoryModel):
    status: Literal["OPPORTUNITY_DECISION_PENDING", "PROVIDER_FAILURE_NO_TRADE"]
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def aware_recorded_time(cls, value: datetime) -> datetime:
        return require_aware(value)


class TerminalOutcomeSummary(StrictStoryModel):
    scope: Literal["DECISION", "ENTRY", "EXIT"]
    certificate_recording_status: Literal["RECORDED", "NOT_RECORDED", "NOT_APPLICABLE"]
    certificate_status: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{1,47}$",
    )
    certificate_time: datetime | None = None
    outcome_status: Literal[
        "NO_TRADE",
        "APPROVED_UNFILLED",
        "PARTIALLY_FILLED",
        "FILLED_OPEN",
        "EXIT_WORKING",
        "CLOSED",
        "RECOVERY",
        "PERMANENTLY_UNSAFE",
    ]
    outcome_time: datetime | None = None

    @field_validator("certificate_time", "outcome_time")
    @classmethod
    def aware_optional_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware(value)

    @model_validator(mode="after")
    def certificate_consistency(self) -> TerminalOutcomeSummary:
        recorded = self.certificate_recording_status == "RECORDED"
        if recorded != (self.certificate_status is not None and self.certificate_time is not None):
            raise ValueError("submission story terminal certificate authority is inconsistent")
        if not recorded and (
            self.certificate_status is not None or self.certificate_time is not None
        ):
            raise ValueError("submission story terminal certificate authority is inconsistent")
        return self


class SubmissionDecisionStory(StrictStoryModel):
    schema_version: Literal["v2"] = "v2"
    artifact_kind: Literal["SUBMISSION_DECISION_STORY"] = "SUBMISSION_DECISION_STORY"
    account_role: Literal["SUBMISSION"] = "SUBMISSION"
    trading_mode: Literal["PAPER_ONLY_APPLICATION_CONTRACT"] = "PAPER_ONLY_APPLICATION_CONTRACT"
    trading_mode_evidence: Literal["NOT_RECORDED_IN_LINEAGE"] = "NOT_RECORDED_IN_LINEAGE"
    strategy: StrategySummary
    decision_time: datetime
    decision_reason_codes: tuple[str, ...] = Field(min_length=1, max_length=4)
    why_selected: tuple[str, ...] = Field(min_length=1, max_length=4)
    alternatives_rejected: tuple[str, ...] = Field(min_length=1, max_length=4)
    alternatives_recording: Literal["NOT_RECORDED"] = "NOT_RECORDED"
    evidence_used: tuple[str, ...] = Field(min_length=1, max_length=6)
    experiment_execution_lineage: ExperimentExecutionLineage | None = None
    provider_retry_audit: tuple[ProviderRetryAuditSummary, ...] = Field(default=())
    risk_limits: RiskLimitSummary
    selected_spread: SelectedSpreadSummary | None
    entry_order_lifecycle: OrderLifecycleSummary
    entry_execution: EntryExecutionSummary | None = None
    lifecycle_assessments: tuple[LifecycleAssessmentSummary, ...] = Field(default=(), max_length=32)
    management_policy: ManagementPolicySummary | None = None
    exit_order_lifecycle: OrderLifecycleSummary = Field(
        default_factory=lambda: OrderLifecycleSummary(recording_status="NOT_APPLICABLE")
    )
    entry_execution_status: str = Field(pattern=r"^[A-Z][A-Z_]{1,47}$")
    order_lifecycle_status: Literal[
        "NO_ORDER_AUTHORIZED",
        "ORDER_ACTIVITY_NOT_RECORDED",
        "ORDER_WORKING",
        "ORDER_LOOKUP_ONLY",
        "ENTRY_REJECTED",
        "ENTRY_CANCELED",
        "ENTRY_EXPIRED",
        "ENTRY_REPLACED",
        "ENTRY_UNFILLED",
        "PARTIAL_FILL_UNRECONCILED",
        "PARTIAL_FILL_RECONCILED_UNSAFE",
        "FILLED_POSITION_OPEN",
        "EXIT_ACTIVITY_NOT_RECORDED",
        "EXIT_WORKING",
        "EXIT_LOOKUP_ONLY",
        "EXIT_REJECTED",
        "EXIT_CANCELED",
        "EXIT_EXPIRED",
        "EXIT_REPLACED",
        "EXIT_UNFILLED",
        "MANAGED_POSITION_CLOSED",
        "RECOVERY_PENDING",
        "PERMANENTLY_UNSAFE",
    ]
    account_impact: AccountImpactSummary
    outcome: Literal[
        "NO_TRADE",
        "APPROVED_UNFILLED",
        "PARTIALLY_FILLED",
        "FILLED_OPEN",
        "EXIT_WORKING",
        "CLOSED",
        "RECOVERY",
        "PERMANENTLY_UNSAFE",
    ]
    terminal: TerminalOutcomeSummary | None = None
    what_changed_next: tuple[str, ...] = Field(min_length=1, max_length=4)

    @field_validator("decision_time")
    @classmethod
    def aware_decision_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def lifecycle_consistency(self) -> SubmissionDecisionStory:
        without_spread = self.outcome == "NO_TRADE"
        if without_spread != (self.selected_spread is None):
            raise ValueError("submission story spread authority is inconsistent")
        expected_status = {
            "NO_TRADE": {"NO_ORDER_AUTHORIZED"},
            "APPROVED_UNFILLED": {
                "ORDER_ACTIVITY_NOT_RECORDED",
                "ORDER_WORKING",
                "ORDER_LOOKUP_ONLY",
                "ENTRY_REJECTED",
                "ENTRY_CANCELED",
                "ENTRY_EXPIRED",
                "ENTRY_REPLACED",
                "ENTRY_UNFILLED",
            },
            "PARTIALLY_FILLED": {
                "PARTIAL_FILL_UNRECONCILED",
                "PARTIAL_FILL_RECONCILED_UNSAFE",
            },
            "FILLED_OPEN": {
                "FILLED_POSITION_OPEN",
                "EXIT_ACTIVITY_NOT_RECORDED",
                "EXIT_REJECTED",
                "EXIT_CANCELED",
                "EXIT_EXPIRED",
                "EXIT_REPLACED",
                "EXIT_UNFILLED",
            },
            "EXIT_WORKING": {"EXIT_WORKING", "EXIT_LOOKUP_ONLY"},
            "CLOSED": {"MANAGED_POSITION_CLOSED"},
            "RECOVERY": {"RECOVERY_PENDING"},
            "PERMANENTLY_UNSAFE": {"PERMANENTLY_UNSAFE"},
        }[self.outcome]
        if self.order_lifecycle_status not in expected_status:
            raise ValueError("submission story lifecycle status is inconsistent")
        if self.outcome == "NO_TRADE" and (
            self.entry_execution_status != "NOT_APPLICABLE"
            or self.entry_order_lifecycle.recording_status != "NOT_APPLICABLE"
        ):
            raise ValueError("submission story no-trade order authority is inconsistent")
        if self.entry_execution_status not in {"NOT_APPLICABLE", "NOT_RECORDED"} and (
            self.entry_order_lifecycle.recording_status != "RECORDED"
        ):
            raise ValueError("submission story terminal order authority is incomplete")
        if self.entry_execution is not None and (
            self.entry_execution_status != "FILLED"
            or not self.entry_order_lifecycle.attempts
            or self.entry_order_lifecycle.attempts[-1].state not in {"FILLED", "CALCULATED"}
            or self.entry_order_lifecycle.attempts[-1].filled_quantity
            != self.entry_order_lifecycle.attempts[-1].quantity
            or self.outcome not in {"FILLED_OPEN", "EXIT_WORKING", "CLOSED"}
        ):
            raise ValueError("submission story entry execution authority is inconsistent")
        if self.outcome in {"FILLED_OPEN", "CLOSED"} and (
            self.account_impact.reconciled_cashflow_usd is None
        ):
            raise ValueError("filled story requires reconciled cashflow")
        if self.outcome in {"NO_TRADE", "APPROVED_UNFILLED", "PARTIALLY_FILLED"} and (
            self.account_impact.reconciled_cashflow_usd is not None
        ):
            raise ValueError("story cannot infer cashflow from order state")
        if tuple(item.assessed_at for item in self.lifecycle_assessments) != tuple(
            sorted(item.assessed_at for item in self.lifecycle_assessments)
        ):
            raise ValueError("submission story lifecycle assessments are not chronological")
        if tuple(item.recorded_at for item in self.provider_retry_audit) != tuple(
            sorted(item.recorded_at for item in self.provider_retry_audit)
        ):
            raise ValueError("submission story scheduled check audit is not chronological")
        if self.outcome == "NO_TRADE" and (
            self.lifecycle_assessments
            or self.exit_order_lifecycle.recording_status != "NOT_APPLICABLE"
        ):
            raise ValueError("submission story no-trade lifecycle authority is inconsistent")
        if self.terminal is not None and self.terminal.outcome_status != self.outcome:
            raise ValueError("submission story terminal outcome authority is inconsistent")
        return self


class PublicStrategySummary(StrictStoryModel):
    recording_status: Literal["RECORDED"] = "RECORDED"
    description: Literal["One frozen defined risk options strategy was evaluated."] = (
        "One frozen defined risk options strategy was evaluated."
    )


class PublicSpreadSummary(StrictStoryModel):
    structure: Literal["DEFINED_RISK_VERTICAL"] = "DEFINED_RISK_VERTICAL"
    price_summary: Literal["The spread could proceed only at its persisted bounded limit."] = (
        "The spread could proceed only at its persisted bounded limit."
    )


class PublicSubmissionStoryPreview(StrictStoryModel):
    schema_version: Literal["v2"] = "v2"
    artifact_kind: Literal["SUBMISSION_DECISION_STORY_PREVIEW"] = (
        "SUBMISSION_DECISION_STORY_PREVIEW"
    )
    trading_mode: Literal["PAPER_ONLY_APPLICATION_CONTRACT"] = "PAPER_ONLY_APPLICATION_CONTRACT"
    trading_mode_evidence: Literal["NOT_RECORDED_IN_LINEAGE"] = "NOT_RECORDED_IN_LINEAGE"
    broker_fill_model: Literal["PAPER_SIMULATION_IF_RECORDED"] = "PAPER_SIMULATION_IF_RECORDED"
    strategy: PublicStrategySummary
    decision_time: datetime
    summary: str = Field(min_length=1, max_length=220)
    why_selected: tuple[str, ...] = Field(min_length=1, max_length=4)
    alternatives_rejected: tuple[str, ...] = Field(min_length=1, max_length=4)
    alternatives_recording: Literal["NOT_RECORDED"] = "NOT_RECORDED"
    evidence_used: tuple[str, ...] = Field(min_length=1, max_length=6)
    risk_limits: Literal[
        "A defined risk options limit remained binding; private numeric entry parameters are "
        "omitted."
    ]
    selected_spread: PublicSpreadSummary | None
    order_lifecycle_status: str
    account_impact: str = Field(min_length=1, max_length=180)
    pnl_status: Literal["NOT_RECORDED"] = "NOT_RECORDED"
    outcome: str
    what_changed_next: tuple[str, ...] = Field(min_length=1, max_length=4)

    @field_validator("decision_time")
    @classmethod
    def aware_decision_time(cls, value: datetime) -> datetime:
        return require_aware(value)


class JudgeStrategySummary(StrictStoryModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
    version: int = Field(gt=0)
    underlying: str = Field(pattern=r"^[A-Z]{1,6}$")
    description: Literal[
        "One frozen defined risk options strategy was evaluated under the paper-only policy."
    ] = "One frozen defined risk options strategy was evaluated under the paper-only policy."


class JudgeTimelineEvent(StrictStoryModel):
    sequence: int = Field(ge=1)
    stage: Literal[
        "PLAN_FROZEN",
        "SCHEDULED_CHECK",
        "ENTRY_DECISION",
        "ENTRY_ORDER",
        "ENTRY_ORDER_SUBMITTED",
        "ENTRY_FILL",
        "ENTRY_RECONCILIATION",
        "POSITION_MATERIALIZED",
        "LIFECYCLE_TICK",
        "EXIT_ORDER",
        "CURRENT_STATE",
    ]
    occurred_at: datetime | None = None
    status: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason_codes: tuple[str, ...] = Field(default=(), max_length=4)
    attempt_ordinal: int | None = Field(default=None, ge=0, le=3)
    filled_quantity: int | None = Field(default=None, ge=0)
    ordered_quantity: int | None = Field(default=None, gt=0)
    description: str = Field(min_length=1, max_length=240)

    @field_validator("occurred_at")
    @classmethod
    def aware_optional_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware(value)

    @model_validator(mode="after")
    def attempt_consistency(self) -> JudgeTimelineEvent:
        counts = (self.filled_quantity, self.ordered_quantity)
        if (self.attempt_ordinal is None) != (counts == (None, None)):
            raise ValueError("submission judge story attempt fields are inconsistent")
        if (
            self.filled_quantity is not None
            and self.ordered_quantity is not None
            and self.filled_quantity > self.ordered_quantity
        ):
            raise ValueError("submission judge story fill is inconsistent")
        return self


class JudgeSubmissionStory(StrictStoryModel):
    schema_version: Literal["v2"] = "v2"
    artifact_kind: Literal["SUBMISSION_JUDGE_STORY"] = "SUBMISSION_JUDGE_STORY"
    trading_mode: Literal["PAPER_ONLY"] = "PAPER_ONLY"
    strategy: JudgeStrategySummary
    entry_decision: Literal["ENTRY_APPROVED", "NO_TRADE"]
    decision_time: datetime
    decision_reason_codes: tuple[str, ...] = Field(min_length=1, max_length=4)
    rationale: tuple[str, ...] = Field(min_length=1, max_length=4)
    evidence_used: tuple[str, ...] = Field(min_length=1, max_length=6)
    management_policy: ManagementPolicySummary | None = None
    risk_policy: Literal[
        "Defined-risk options limits stayed binding. Private numeric entry parameters are omitted."
    ] = "Defined-risk options limits stayed binding. Private numeric entry parameters are omitted."
    spread: Literal["DEFINED_RISK_VERTICAL"] | None
    expiration: date | None = None
    quantity: int | None = Field(default=None, gt=0)
    debit_paid_usd: Decimal | None = Field(default=None, gt=0)
    scheduled_cycle_count: int = Field(ge=1)
    approved_cycle_count: int = Field(ge=0)
    no_trade_cycle_count: int = Field(ge=0)
    post_fill_no_action_count: int = Field(ge=0)
    no_trade_reason_codes: tuple[str, ...] = Field(default=(), max_length=8)
    timeline: tuple[JudgeTimelineEvent, ...] = Field(min_length=2)
    outcome: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    final_reconciliation: str = Field(min_length=1, max_length=180)
    realized_pnl_status: Literal["UNAVAILABLE", "CERTIFIED"]
    realized_pnl_usd: Decimal | None = None

    @field_validator("decision_time")
    @classmethod
    def aware_judge_decision_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def story_consistency(self) -> JudgeSubmissionStory:
        if (self.entry_decision == "NO_TRADE") != (self.spread is None):
            raise ValueError("submission judge story spread is inconsistent")
        public_terms = (self.expiration, self.quantity)
        if (self.spread is None) != all(value is None for value in public_terms):
            raise ValueError("submission judge story public spread terms are inconsistent")
        if self.spread is not None and any(value is None for value in public_terms):
            raise ValueError("submission judge story public spread terms are incomplete")
        if self.outcome in {"FILLED_OPEN", "CLOSED"} and self.debit_paid_usd is None:
            raise ValueError("submission judge story filled spread debit is missing")
        if self.spread is None and self.debit_paid_usd is not None:
            raise ValueError("submission judge story debit is inconsistent")
        if self.approved_cycle_count > self.scheduled_cycle_count:
            raise ValueError("submission judge story approved cycle count is inconsistent")
        if tuple(item.sequence for item in self.timeline) != tuple(
            range(1, len(self.timeline) + 1)
        ):
            raise ValueError("submission judge story sequence is inconsistent")
        if (self.realized_pnl_usd is not None) != (self.realized_pnl_status == "CERTIFIED"):
            raise ValueError("submission judge story realized pnl is inconsistent")
        return self
