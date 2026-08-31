from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    WithJsonSchema,
    model_validator,
)

from backend.app.order_limits import MAX_STRUCTURAL_OPTION_QUANTITY

SchemaVersion = Literal["v1"]


def canonical_decimal(value: Decimal) -> str:
    fixed = format(value, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return "0" if fixed in {"-0", "+0"} else fixed


Money = Annotated[
    Decimal,
    Field(max_digits=18, decimal_places=6),
    PlainSerializer(canonical_decimal, return_type=str, when_used="json"),
    WithJsonSchema(
        {"type": "string", "pattern": r"^[+-]?\d{1,12}(?:\.\d{1,6})?$"},
        mode="serialization",
    ),
]
Percent = Annotated[
    Decimal,
    Field(max_digits=18, decimal_places=9),
    PlainSerializer(canonical_decimal, return_type=str, when_used="json"),
    WithJsonSchema(
        {"type": "string", "pattern": r"^[+-]?\d{1,9}(?:\.\d{1,9})?$"},
        mode="serialization",
    ),
]
ExactDecimal = Annotated[
    Decimal,
    Field(max_digits=21, decimal_places=9),
    PlainSerializer(canonical_decimal, return_type=str, when_used="json"),
    WithJsonSchema(
        {"type": "string", "pattern": r"^[+-]?\d{1,12}(?:\.\d{1,9})?$"},
        mode="serialization",
    ),
]
HashBoundDecimal = Annotated[
    Decimal,
    Field(max_digits=21, decimal_places=9),
    WithJsonSchema(
        {"type": "string", "pattern": r"^[+-]?\d{1,12}(?:\.\d{1,9})?$"},
        mode="serialization",
    ),
]


UTC_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
UTC_TIMESTAMP_RE = re.compile(UTC_TIMESTAMP_PATTERN)


def require_canonical_utc_text(value: Any) -> Any:
    if isinstance(value, str) and UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError("timestamp text must use uppercase Z and at most 6 fractional digits")
    return value


def require_utc_datetime(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return value


UtcDateTime = Annotated[
    datetime,
    BeforeValidator(require_canonical_utc_text),
    AfterValidator(require_utc_datetime),
    WithJsonSchema(
        {
            "type": "string",
            "format": "date-time",
            "pattern": UTC_TIMESTAMP_PATTERN,
        },
        mode="serialization",
    ),
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: SchemaVersion = "v1"


class AccountRole(StrEnum):
    SUBMISSION = "SUBMISSION"
    DEVELOPMENT = "DEVELOPMENT"
    REPLAY = "REPLAY"


class ReplayScenario(StrEnum):
    THESIS_INTACT = "THESIS_INTACT"
    THETA_TAKEOVER = "THETA_TAKEOVER"
    CATALYST_BROKEN = "CATALYST_BROKEN"
    STALE_QUOTE = "STALE_QUOTE"


class DataQuality(StrEnum):
    COMPLETE = "COMPLETE"
    MISSING = "MISSING"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class MeasurementStatus(StrEnum):
    COMPLETE = "COMPLETE"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class BaselineStatus(StrEnum):
    NOT_CAPTURED = "BASELINE_NOT_CAPTURED"
    UNKNOWN = "BASELINE_UNKNOWN"
    CLEAN = "BASELINE_CLEAN"
    CONTAMINATED = "BASELINE_CONTAMINATED"


class PerformanceFailureCode(StrEnum):
    CAPTURE_NOT_STARTED = "CAPTURE_NOT_STARTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    ACCOUNT_STATE_INCOMPLETE = "ACCOUNT_STATE_INCOMPLETE"
    BASELINE_UNAVAILABLE = "BASELINE_UNAVAILABLE"
    SCHEMA_INVALID = "SCHEMA_INVALID"


class Action(StrEnum):
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    ROLL = "ROLL"
    NO_ACTION = "NO_ACTION"


class RunState(StrEnum):
    QUEUED = "QUEUED"
    OBSERVING = "OBSERVING"
    VALIDATING_DATA = "VALIDATING_DATA"
    EXTRACTING_EVIDENCE = "EXTRACTING_EVIDENCE"
    COMPUTING_EXPOSURE = "COMPUTING_EXPOSURE"
    DECIDING = "DECIDING"
    POLICY_CHECKING = "POLICY_CHECKING"
    EXECUTING = "EXECUTING"
    RECONCILING = "RECONCILING"
    TERMINAL = "TERMINAL"


class OptionRight(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


class PositionIntent(StrEnum):
    BUY_TO_OPEN = "BUY_TO_OPEN"
    SELL_TO_OPEN = "SELL_TO_OPEN"
    BUY_TO_CLOSE = "BUY_TO_CLOSE"
    SELL_TO_CLOSE = "SELL_TO_CLOSE"


class EvidenceRelation(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    NEUTRAL = "NEUTRAL"


class EvidenceTier(StrEnum):
    PRIMARY = "PRIMARY"
    ORIGINAL_REPORTING = "ORIGINAL_REPORTING"
    SECONDARY = "SECONDARY"


class ThesisStatus(StrEnum):
    INTACT = "INTACT"
    WEAKENING = "WEAKENING"
    BROKEN = "BROKEN"
    UNKNOWN = "UNKNOWN"


class EvidenceState(StrEnum):
    ASSESSED = "ASSESSED"
    NO_CHANGE = "NO_CHANGE"
    UNKNOWN = "UNKNOWN"


class ExecutionDecision(StrEnum):
    NO_ACTION = "NO_ACTION"
    HOLD_CERTIFIED = "HOLD_CERTIFIED"
    CLOSE_APPROVED = "CLOSE_APPROVED"
    CLOSE_RISK_ONLY = "CLOSE_RISK_ONLY"
    ROLL_APPROVED = "ROLL_APPROVED"


class OwnerModelProvider(StrEnum):
    GEMINI = "GEMINI"
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"


class HealthResponse(ContractModel):
    status: Literal["ok"] = "ok"
    build: str
    runtime_mode: Literal["CONNECTED", "REPLAY_ONLY"]


class ErrorDetail(ContractModel):
    code: str
    field: str | None = None


class ErrorResponse(ContractModel):
    error: ErrorDetail
    request_id: str


class SessionCreateRequest(ContractModel):
    access_code: str = Field(min_length=16, max_length=256)


class SessionResponse(ContractModel):
    authenticated: bool
    expires_at: datetime | None = None


class ProviderSettingsUpdateRequest(ContractModel):
    provider: OwnerModelProvider
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretStr = Field(min_length=1, max_length=16_384)
    endpoint: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def provider_fields_match(self) -> ProviderSettingsUpdateRequest:
        if self.provider is OwnerModelProvider.GEMINI:
            if self.endpoint is not None:
                raise ValueError("Gemini does not accept a custom endpoint")
        elif self.endpoint is None:
            raise ValueError("OpenAI-compatible providers require an endpoint")
        return self


class ProviderSettingsResponse(ContractModel):
    configured: bool
    provider: OwnerModelProvider | None = None
    endpoint: str | None = None
    model: str | None = None
    generation: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def configuration_is_complete(self) -> ProviderSettingsResponse:
        fields = (self.provider, self.endpoint, self.model, self.generation)
        if (self.configured and not all(value is not None for value in fields)) or (
            not self.configured and any(value is not None for value in fields)
        ):
            raise ValueError("provider settings response is inconsistent")
        return self


class AccountResponse(ContractModel):
    role: AccountRole
    paper: Literal[True] = True
    equity: Money
    buying_power: Money
    baseline_status: DataQuality
    autonomous_enabled: bool


class OptionLeg(ContractModel):
    symbol: str
    underlying: str
    expiry: date
    strike: Money
    right: OptionRight
    intent: PositionIntent
    ratio: int = Field(gt=0, le=10)
    quantity: int = Field(gt=0)
    multiplier: Literal[100] = 100


class GreekExposure(ContractModel):
    delta: ExactDecimal = Field(
        description="Position delta in equivalent shares of the underlying."
    )
    gamma: ExactDecimal = Field(
        description="Change in position delta for a $1 move in the underlying."
    )
    theta_per_day: ExactDecimal = Field(
        description="Estimated position value change in US dollars for one calendar day."
    )
    vega_per_iv_point: ExactDecimal = Field(
        description=(
            "Estimated position value change in US dollars for a one percentage-point "
            "change in implied volatility."
        )
    )


class PositionResponse(ContractModel):
    position_id: UUID
    role: AccountRole
    underlying: str
    legs: tuple[OptionLeg, ...] = Field(min_length=1, max_length=4)
    current_exposure: GreekExposure | None
    quality: DataQuality
    fingerprint: str


class PositionListResponse(ContractModel):
    positions: tuple[PositionResponse, ...]


class ThesisCreateRequest(ContractModel):
    underlying: str
    thesis_code: str
    invalidation_codes: tuple[str, ...] = Field(min_length=1)
    intended_exposure: GreekExposure
    source_policy_hash: str


class ThesisResponse(ContractModel):
    thesis_id: UUID
    version: int = Field(gt=0)
    frozen: bool
    thesis_hash: str
    thesis: ThesisCreateRequest


class ThesisFreezeResponse(ContractModel):
    thesis_id: UUID
    version: int
    thesis_hash: str
    frozen_at: datetime


class RunCreateRequest(ContractModel):
    thesis_id: UUID
    position_id: UUID


class RunEvent(ContractModel):
    sequence: int = Field(ge=0)
    state: RunState
    code: str
    occurred_at: datetime


class RunResponse(ContractModel):
    run_id: UUID
    state: RunState
    terminal_code: str | None = None


class SourceCluster(ContractModel):
    cluster_id: str
    source_ids: tuple[str, ...] = Field(min_length=1)
    headline: str
    observed_at: datetime
    source_tier: EvidenceTier
    independent_reporting_group: str | None = None


class EvidenceClassification(ContractModel):
    cluster_id: str
    source_ids: tuple[str, ...] = Field(min_length=1)
    event_code: str
    relation: EvidenceRelation
    materiality: int = Field(ge=1, le=3)
    relevance: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    source_tier: EvidenceTier
    invalidates: bool = False
    independent_reporting_group: str | None = None
    invalidation_condition_id: str | None = None


class Alternative(ContractModel):
    action: Action
    eligible: bool
    rationale_code: str
    expected_exposure: GreekExposure | None = None


class DriftComponents(ContractModel):
    evidence_drift: Decimal
    exposure_mismatch: Decimal
    time_pressure: Decimal
    volatility_mismatch: Decimal
    risk_utilization: Decimal
    unrounded_score: Decimal
    display_score: int = Field(ge=0, le=100)
    dominant_non_evidence_component: str


class AssessmentResponse(ContractModel):
    assessment_id: UUID
    run_id: UUID
    action: Action
    rationale_code: str
    quality: DataQuality
    thesis_status: ThesisStatus
    evidence_state: EvidenceState
    execution_decision: ExecutionDecision
    actual_exposure: GreekExposure | None
    drift_score: Decimal | None
    components: DriftComponents | None
    alternatives: tuple[Alternative, ...] = Field(min_length=3)
    evidence: tuple[EvidenceClassification, ...]
    policy_hash: str


class ExecutionClaimResponse(ContractModel):
    execution_id: UUID
    state: str
    intent_digest: str


class ExecutionAttempt(ContractModel):
    ordinal: int = Field(ge=0, le=3)
    client_order_id_hash: str
    state: str


class CertificateResponse(ContractModel):
    certificate_id: UUID
    account_role: AccountRole
    thesis: ThesisResponse
    assessment: AssessmentResponse
    expected_after_exposure: GreekExposure | None
    actual_after_exposure: GreekExposure | None
    attempts: tuple[ExecutionAttempt, ...]
    execution_state: str
    lineage_hash: str
    published: bool = False


class PerformancePoint(ContractModel):
    scheduled_for: UtcDateTime
    attempted_at: UtcDateTime
    measured_at: UtcDateTime | None
    status: MeasurementStatus
    failure_code: PerformanceFailureCode | None
    current_equity_usd: Money | None
    account_equity_change_usd: Money | None
    account_equity_return_pct: Percent | None = Field(
        description="Account equity return in percentage points; 1 means 1%, not 0.01%."
    )
    reconciled_lifecycle_cashflow_usd: Money | None
    open_position_liquidation_pnl_usd: Money | None
    simulator_limitations_code: Literal["ALPACA_PAPER_SIMULATION"]

    @model_validator(mode="after")
    def measurement_state_is_coherent(self) -> PerformancePoint:
        times = (self.scheduled_for, self.attempted_at, self.measured_at)
        if any(value is not None and value.utcoffset() != UTC.utcoffset(value) for value in times):
            raise ValueError("performance times must use UTC")
        if self.attempted_at < self.scheduled_for or (
            self.measured_at is not None and self.measured_at < self.attempted_at
        ):
            raise ValueError("performance time order is invalid")
        normalized = (self.account_equity_change_usd, self.account_equity_return_pct)
        measured_values = (
            self.current_equity_usd,
            *normalized,
            self.reconciled_lifecycle_cashflow_usd,
            self.open_position_liquidation_pnl_usd,
        )
        if self.status == MeasurementStatus.COMPLETE:
            if self.measured_at is None or self.current_equity_usd is None:
                raise ValueError("complete point requires measured account equity")
            if self.failure_code is not None:
                raise ValueError("complete point cannot have a failure code")
            if (normalized[0] is None) != (normalized[1] is None):
                raise ValueError("normalized values must be present or absent together")
        elif self.failure_code is None:
            raise ValueError("incomplete point requires a failure code")
        elif self.measured_at is not None or any(value is not None for value in measured_values):
            raise ValueError("incomplete point cannot expose values")
        return self


class CompetitionPerformanceProofResponse(ContractModel):
    publication_status: Literal["NOT_PUBLISHED", "PUBLISHED"]
    baseline_status: BaselineStatus | None
    published_at: UtcDateTime | None
    point: PerformancePoint | None
    linked_certificate_ids: Annotated[
        tuple[UUID, ...], Field(json_schema_extra={"uniqueItems": True})
    ]
    publication_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")] | None
    predecessor_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")] | None

    @model_validator(mode="after")
    def publication_state_is_coherent(self) -> CompetitionPerformanceProofResponse:
        if self.publication_status == "NOT_PUBLISHED":
            if (
                any(
                    value is not None
                    for value in (
                        self.baseline_status,
                        self.published_at,
                        self.point,
                        self.publication_hash,
                        self.predecessor_hash,
                    )
                )
                or self.linked_certificate_ids
            ):
                raise ValueError("unpublished proof cannot expose values")
            return self
        if any(
            value is None
            for value in (
                self.baseline_status,
                self.published_at,
                self.point,
                self.publication_hash,
            )
        ):
            raise ValueError("published proof is incomplete")
        assert self.point is not None
        assert self.published_at is not None
        if self.published_at.utcoffset() != UTC.utcoffset(self.published_at):
            raise ValueError("publication time must use UTC")
        latest_measurement_time = self.point.measured_at or self.point.attempted_at
        if self.published_at < latest_measurement_time:
            raise ValueError("publication time order is invalid")
        if len(set(self.linked_certificate_ids)) != len(self.linked_certificate_ids):
            raise ValueError("linked certificate IDs must be unique")
        if self.linked_certificate_ids != tuple(sorted(self.linked_certificate_ids)):
            raise ValueError("linked certificate IDs must be sorted")
        normalized = (
            self.point.account_equity_change_usd,
            self.point.account_equity_return_pct,
        )
        if self.baseline_status == BaselineStatus.CLEAN:
            if self.point.status == MeasurementStatus.COMPLETE and any(
                value is None for value in normalized
            ):
                raise ValueError("clean complete proof requires normalized values")
            if self.point.status == MeasurementStatus.COMPLETE:
                assert self.point.current_equity_usd is not None
                assert normalized[0] is not None
                assert normalized[1] is not None
                expected_change = self.point.current_equity_usd - Decimal("100000")
                if (
                    normalized[0] != expected_change
                    or normalized[1] * Decimal("1000") != expected_change
                ):
                    raise ValueError("clean proof normalized values are inconsistent")
        elif any(value is not None for value in normalized):
            raise ValueError("nonclean proof cannot expose normalized values")
        return self


class CompetitionSpreadProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    structure: Literal["VERTICAL"]
    underlying: str = Field(pattern=r"^[A-Z]{1,6}$")
    option_type: Literal["CALL", "PUT"] = Field(
        description=(
            "Normalized product value. Alpaca's corresponding contract type is lowercase "
            "call or put."
        )
    )
    expiration: date = Field(
        description="Normalized product field corresponding to Alpaca's expiration_date."
    )
    long_strike: HashBoundDecimal = Field(gt=0, description="Long-leg strike price in US dollars.")
    short_strike: HashBoundDecimal = Field(
        gt=0, description="Short-leg strike price in US dollars."
    )
    quantity: int = Field(gt=0, description="Number of vertical-spread strategy units.")

    @model_validator(mode="after")
    def distinct_strikes(self) -> CompetitionSpreadProjection:
        if self.long_strike == self.short_strike:
            raise ValueError("vertical strikes must be distinct")
        return self


class CompetitionExposureProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    delta: HashBoundDecimal | None = Field(
        default=None,
        description="Position delta in equivalent shares of the underlying.",
    )
    gamma: HashBoundDecimal | None = Field(
        default=None,
        description="Change in position delta for a $1 move in the underlying.",
    )
    theta_per_day: HashBoundDecimal | None = Field(
        default=None,
        description="Estimated position value change in US dollars for one calendar day.",
    )
    vega_per_iv_point: HashBoundDecimal | None = Field(
        default=None,
        description=(
            "Estimated position value change in US dollars for a one percentage-point "
            "change in implied volatility."
        ),
    )

    @model_validator(mode="after")
    def at_least_one_measurement(self) -> CompetitionExposureProjection:
        values = (self.delta, self.gamma, self.theta_per_day, self.vega_per_iv_point)
        if all(value is None for value in values):
            raise ValueError("exposure must contain a measured value")
        if any(value is not None and not value.is_finite() for value in values):
            raise ValueError("exposure values must be finite")
        return self


class CompetitionThesisProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    direction: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    volatility_view: Literal["LONG", "SHORT", "NEUTRAL"]
    target_at: UtcDateTime


class CompetitionExecutionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_kind: Literal["EXECUTION"]
    action: Literal["ENTRY", "ROLL", "CLOSE"]
    occurred_at: UtcDateTime
    reason_category: Literal["POSITION_OPENED", "POSITION_ROLLED", "POSITION_CLOSED"]
    cashflow_usd: HashBoundDecimal = Field(
        description=(
            "Signed whole-position cash flow in US dollars; debits are negative and "
            "credits are positive."
        )
    )
    execution_status: Literal["FILLED"] = Field(
        description=(
            "Product-normalized terminal state after broker reconciliation; not a raw "
            "Alpaca order status."
        )
    )
    resulting_state: Literal["OPEN", "CLOSED"]
    spread_after: CompetitionSpreadProjection | None

    @model_validator(mode="after")
    def action_matches_result(self) -> CompetitionExecutionEvent:
        expected_reason = {
            "ENTRY": "POSITION_OPENED",
            "ROLL": "POSITION_ROLLED",
            "CLOSE": "POSITION_CLOSED",
        }[self.action]
        if self.reason_category != expected_reason:
            raise ValueError("execution event reason does not match its action")
        if self.action == "CLOSE":
            if self.resulting_state != "CLOSED" or self.spread_after is not None:
                raise ValueError("close event must leave a closed position")
        elif self.resulting_state != "OPEN" or self.spread_after is None:
            raise ValueError("entry and roll events must leave an open spread")
        return self


class CompetitionAssessmentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_kind: Literal["ASSESSMENT"]
    action: Literal["HOLD", "CLOSE", "ROLL", "NO_ACTION"]
    occurred_at: UtcDateTime
    reason_category: Literal[
        "POSITION_REVIEWED",
        "RISK_REDUCTION",
        "THESIS_CHANGED",
        "POSITION_ADJUSTMENT",
        "DATA_INCOMPLETE",
    ]

    @model_validator(mode="after")
    def action_matches_reason(self) -> CompetitionAssessmentEvent:
        expected_reasons = {
            "HOLD": {"POSITION_REVIEWED"},
            "CLOSE": {"RISK_REDUCTION", "THESIS_CHANGED"},
            "ROLL": {"POSITION_ADJUSTMENT"},
            "NO_ACTION": {"DATA_INCOMPLETE"},
        }
        if self.reason_category not in expected_reasons[self.action]:
            raise ValueError("assessment event reason does not match its action")
        return self


CompetitionEvent = Annotated[
    CompetitionExecutionEvent | CompetitionAssessmentEvent,
    Field(discriminator="event_kind"),
]


class CompetitionNoTradeProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["v1"]
    record_kind: Literal["NO_TRADE"]
    public_record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["NO_TRADE"]
    reason_category: Literal["STRATEGY_NOT_READY"]
    decided_at: UtcDateTime
    observed_at: UtcDateTime
    paper_trading: Literal[True]

    @model_validator(mode="after")
    def chronology(self) -> CompetitionNoTradeProjection:
        if self.observed_at < self.decided_at:
            raise ValueError("no-trade observation precedes its decision boundary")
        return self


class CompetitionPositionProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["v1"]
    record_kind: Literal["POSITION"]
    public_record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["OPEN", "CLOSED"]
    underlying: str = Field(pattern=r"^[A-Z]{1,6}$")
    opening_spread: CompetitionSpreadProjection
    current_spread: CompetitionSpreadProjection | None
    opened_at: UtcDateTime
    as_of: UtcDateTime
    closed_at: UtcDateTime | None
    thesis: CompetitionThesisProjection
    events: tuple[CompetitionEvent, ...] = Field(min_length=1)
    current_exposure: CompetitionExposureProjection | None
    execution_status: Literal["FILLED"] = Field(
        description=(
            "Product-normalized terminal state after broker reconciliation; not a raw "
            "Alpaca order status."
        )
    )
    paper_trading: Literal[True]

    @model_validator(mode="after")
    def lifecycle_is_coherent(self) -> CompetitionPositionProjection:
        if not self.events or not isinstance(self.events[0], CompetitionExecutionEvent):
            raise ValueError("position requires an opening execution event")
        if self.events[0].action != "ENTRY" or self.opened_at != self.events[0].occurred_at:
            raise ValueError("position opening event is inconsistent")
        if tuple(event.occurred_at for event in self.events) != tuple(
            sorted(event.occurred_at for event in self.events)
        ):
            raise ValueError("position events are not chronological")
        if self.as_of < self.opened_at or self.events[-1].occurred_at > self.as_of:
            raise ValueError("position as-of time is inconsistent")
        executions = tuple(
            event for event in self.events if isinstance(event, CompetitionExecutionEvent)
        )
        lifecycle_state: Literal["OPEN", "CLOSED"] | None = None
        for event in self.events:
            if isinstance(event, CompetitionAssessmentEvent):
                if lifecycle_state != "OPEN":
                    raise ValueError("assessment event requires an open position")
                continue
            if event.action == "ENTRY":
                if lifecycle_state is not None:
                    raise ValueError("position cannot contain more than one entry")
            elif lifecycle_state != "OPEN":
                raise ValueError("position execution sequence is inconsistent")
            lifecycle_state = event.resulting_state
        spreads = (self.opening_spread, self.current_spread) + tuple(
            event.spread_after for event in executions
        )
        if any(spread is not None and spread.underlying != self.underlying for spread in spreads):
            raise ValueError("position spread underlying is inconsistent")
        if executions[0].spread_after != self.opening_spread:
            raise ValueError("position opening spread is inconsistent")
        if executions[-1].resulting_state != self.state:
            raise ValueError("position state does not match its latest execution")
        if executions[-1].spread_after != self.current_spread:
            raise ValueError("position current spread does not match its latest execution")
        if self.state == "OPEN":
            if self.current_spread is None or self.closed_at is not None:
                raise ValueError("open position state is inconsistent")
        elif (
            self.current_spread is not None
            or self.closed_at is None
            or executions[-1].action != "CLOSE"
            or executions[-1].occurred_at != self.closed_at
        ):
            raise ValueError("closed position state is inconsistent")
        return self


CompetitionRecordPayload = Annotated[
    CompetitionNoTradeProjection | CompetitionPositionProjection,
    Field(discriminator="record_kind"),
]


class CompetitionRecordItem(ContractModel):
    kind: Literal["NO_TRADE", "POSITION"]
    public_record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: UtcDateTime
    published_at: UtcDateTime
    payload: CompetitionRecordPayload
    projection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def record_is_coherent(self) -> CompetitionRecordItem:
        if (
            self.kind != self.payload.record_kind
            or self.public_record_id != self.payload.public_record_id
            or self.occurred_at.utcoffset() != UTC.utcoffset(self.occurred_at)
            or self.published_at.utcoffset() != UTC.utcoffset(self.published_at)
            or self.published_at < self.occurred_at
        ):
            raise ValueError("competition record is inconsistent")
        return self


class CompetitionRecordResponse(ContractModel):
    publication_status: Literal["NOT_PUBLISHED", "PUBLISHED"]
    records: tuple[CompetitionRecordItem, ...]

    @model_validator(mode="after")
    def publication_state_is_coherent(self) -> CompetitionRecordResponse:
        if self.publication_status == "NOT_PUBLISHED":
            if self.records:
                raise ValueError("unpublished competition record cannot contain records")
            return self
        if not self.records:
            raise ValueError("published competition record requires a record")
        if tuple(record.published_at for record in self.records) != tuple(
            sorted(record.published_at for record in self.records)
        ):
            raise ValueError("competition records are not chronological")
        for index, record in enumerate(self.records):
            expected = None if index == 0 else self.records[index - 1].publication_hash
            if record.predecessor_hash != expected:
                raise ValueError("competition record chain is inconsistent")
        return self


class ReplayOpeningPresentation(ContractModel):
    underlying: str
    reference_spot: Money
    spread_kind: Literal["BULL_CALL_SPREAD"]
    long_strike: Money
    short_strike: Money
    expiration_date: date
    quantity: int = Field(
        gt=0,
        le=MAX_STRUCTURAL_OPTION_QUANTITY,
        description="Number of vertical-spread strategy units.",
    )
    contract_multiplier: Literal[100] = Field(
        description="Shares represented by each standard US equity option contract.",
    )
    entry_net_debit_per_share_usd: Money = Field(
        description=(
            "Net option premium quoted in US dollars per underlying share. Multiply by "
            "quantity and contract_multiplier for total debit."
        )
    )
    maximum_loss: Money = Field(description="Maximum loss for the whole position in US dollars.")
    approved_risk_cap: Money = Field(description="Approved whole-position risk cap in US dollars.")
    delta_low: ExactDecimal
    delta_high: ExactDecimal
    vega_low: ExactDecimal
    vega_high: ExactDecimal
    maximum_daily_theta: ExactDecimal
    minimum_dte: int = Field(ge=0)
    maximum_dte: int = Field(ge=0)
    selection_state: Literal["PRESELECTED_SAMPLE"]

    @model_validator(mode="after")
    def validate_opening(self) -> ReplayOpeningPresentation:
        expected_loss = (
            self.entry_net_debit_per_share_usd * self.quantity * self.contract_multiplier
        )
        if (
            not self.underlying
            or self.reference_spot <= 0
            or self.long_strike <= 0
            or self.short_strike <= self.long_strike
            or self.entry_net_debit_per_share_usd <= 0
            or self.maximum_loss != expected_loss
            or self.maximum_loss > self.approved_risk_cap
            or self.delta_low > self.delta_high
            or self.vega_low > self.vega_high
            or self.maximum_daily_theta <= 0
            or self.minimum_dte > self.maximum_dte
        ):
            raise ValueError("invalid Replay opening record")
        return self


class ReplayMarketPresentation(ContractModel):
    assessed_at: UtcDateTime
    review_by: UtcDateTime | None
    urgency: Literal["ROUTINE", "SOON", "IMMEDIATE", "WAITING"]
    quote_status: Literal["FRESH", "STALE"]
    quote_age_seconds: int = Field(ge=0)
    dte: int = Field(ge=0)
    mark: Money | None = Field(description="Mid-market spread premium in US dollars per share.")
    bid: Money | None = Field(description="Executable liquidation premium in US dollars per share.")
    ask: Money | None = Field(description="Offer premium in US dollars per share.")
    liquidation_value: Money | None = Field(
        description="Estimated liquidation value for the whole position in US dollars."
    )
    open_pnl: Money | None = Field(
        description="Open profit or loss for the whole position in US dollars."
    )
    implied_volatility: Percent | None = Field(
        description="Implied volatility as a fraction; 0.50 means 50%."
    )
    iv_change_points: Percent | None = Field(
        description="Change in implied volatility in percentage points."
    )


class ReplayClassificationPresentation(ContractModel):
    source_id: str
    headline: str
    observed_at: UtcDateTime
    event_code: str
    relation: EvidenceRelation
    materiality: int = Field(ge=1, le=3)
    relevance: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    source_tier: EvidenceTier
    invalidates: bool = False


class ReplayEvidencePresentation(ContractModel):
    status: Literal["CLASSIFIED", "NOT_RUN"]
    classifications: tuple[ReplayClassificationPresentation, ...]

    @model_validator(mode="after")
    def validate_status(self) -> ReplayEvidencePresentation:
        if (self.status == "CLASSIFIED") != bool(self.classifications):
            raise ValueError("invalid Replay evidence status")
        if len({item.source_id for item in self.classifications}) != len(self.classifications):
            raise ValueError("duplicate Replay evidence source")
        return self


class ReplayRollPresentation(ContractModel):
    expiration_date: date
    long_strike: Money
    short_strike: Money
    quantity: int = Field(
        gt=0,
        le=MAX_STRUCTURAL_OPTION_QUANTITY,
        description="Number of vertical-spread strategy units.",
    )
    contract_multiplier: Literal[100] = Field(
        description="Shares represented by each standard US equity option contract."
    )
    estimated_net_debit_per_share_usd: Money = Field(
        ge=0, description="Estimated incremental net debit in US dollars per underlying share."
    )
    resulting_maximum_loss: Money = Field(
        description="Resulting maximum loss for the whole position in US dollars."
    )


class ReplayIntegrationPresentation(ContractModel):
    fixture_validation: Literal["COMPLETE"]
    deterministic_policy: Literal["COMPLETE"]
    trading_api: Literal["NOT_RUN"]
    mcp: Literal["NOT_RUN"]
    model: Literal["NOT_RUN"]
    cli: Literal["NOT_RUN"]
    order_entry: Literal["DISABLED"]


class ReplayPresentation(ContractModel):
    opening: ReplayOpeningPresentation
    market: ReplayMarketPresentation
    evidence: ReplayEvidencePresentation
    integration: ReplayIntegrationPresentation
    roll: ReplayRollPresentation | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> ReplayPresentation:
        opening = self.opening
        market = self.market
        if market.assessed_at.utcoffset() != timedelta(0):
            raise ValueError("Replay assessment time must be UTC")
        if market.dte != (opening.expiration_date - market.assessed_at.date()).days:
            raise ValueError("Replay DTE does not match assessment date")
        if market.review_by is not None and market.review_by <= market.assessed_at:
            raise ValueError("Replay review time must follow assessment")
        if market.review_by is not None and market.review_by.utcoffset() != timedelta(0):
            raise ValueError("Replay review time must be UTC")
        if any(item.observed_at > market.assessed_at for item in self.evidence.classifications):
            raise ValueError("Replay evidence cannot follow assessment")
        market_values = (
            market.mark,
            market.bid,
            market.ask,
            market.liquidation_value,
            market.open_pnl,
            market.implied_volatility,
            market.iv_change_points,
        )
        if market.quote_status == "STALE":
            if any(value is not None for value in market_values) or market.urgency != "WAITING":
                raise ValueError("stale Replay market must not expose current values")
        else:
            if (
                market.bid is None
                or market.mark is None
                or market.ask is None
                or market.liquidation_value is None
                or market.open_pnl is None
                or market.implied_volatility is None
                or market.iv_change_points is None
            ):
                raise ValueError("fresh Replay market is incomplete")
            if not market.bid <= market.mark <= market.ask:
                raise ValueError("Replay mark is outside the displayed market")
            expected_value = market.bid * opening.quantity * opening.contract_multiplier
            if (
                market.liquidation_value != expected_value
                or market.open_pnl != expected_value - opening.maximum_loss
            ):
                raise ValueError("Replay position arithmetic is inconsistent")
        if self.roll is not None:
            roll = self.roll
            expected_loss = (
                opening.maximum_loss
                + roll.estimated_net_debit_per_share_usd * roll.quantity * roll.contract_multiplier
            )
            if (
                roll.expiration_date <= opening.expiration_date
                or roll.long_strike <= 0
                or roll.short_strike <= roll.long_strike
                or roll.short_strike - roll.long_strike
                != opening.short_strike - opening.long_strike
                or roll.quantity != opening.quantity
                or roll.contract_multiplier != opening.contract_multiplier
                or roll.resulting_maximum_loss != expected_loss
                or roll.resulting_maximum_loss > opening.approved_risk_cap
            ):
                raise ValueError("invalid Replay roll record")
        return self


class ReplayResponse(ContractModel):
    scenario: ReplayScenario
    provenance_label: Literal["REPLAY / FIXTURE DATA"] = "REPLAY / FIXTURE DATA"
    input_hash: str
    assessment_hash: str
    assessment: AssessmentResponse
    certificate: CertificateResponse
    presentation: ReplayPresentation
    execution_enabled: Literal[False] = False


class SchedulerTickResponse(ContractModel):
    tick_id: UUID
    accepted: bool
    code: str


class AutonomyStatusResponse(ContractModel):
    role: AccountRole
    server_enabled: bool
    account_enabled: bool
    effective: bool


class SubmissionBaseline(ContractModel):
    role: Literal[AccountRole.SUBMISSION]
    equity: Money
    captured_at: datetime
    account_fingerprint: str
    positions_hash: str
    orders_hash: str
    activities_hash: str
    clean: bool


class EntryApproval(ContractModel):
    approval_id: UUID
    account_role: AccountRole
    decision_boundary: datetime
    book_fingerprint: str
    policy_hash: str
    selector_policy_hash: str
    max_loss: Money
    quantity: int = Field(gt=0, le=MAX_STRUCTURAL_OPTION_QUANTITY)
    envelope_hash: str
    expires_at: datetime


class FixtureEnvelope(ContractModel):
    scenario: ReplayScenario
    provenance: str
    input_hash: str
    expected_hash: str
    payload: dict[str, Any]
