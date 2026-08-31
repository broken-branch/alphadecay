from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class CompetitionRecordKind(StrEnum):
    NO_TRADE = "NO_TRADE"
    POSITION = "POSITION"


class PositionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class PositionDirection(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class PublicAssessmentAction(StrEnum):
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    ROLL = "ROLL"
    NO_ACTION = "NO_ACTION"


class PublicAssessmentReason(StrEnum):
    POSITION_REVIEWED = "POSITION_REVIEWED"
    RISK_REDUCTION = "RISK_REDUCTION"
    THESIS_CHANGED = "THESIS_CHANGED"
    POSITION_ADJUSTMENT = "POSITION_ADJUSTMENT"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"


class StrictProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpreadProjection(StrictProjection):
    structure: Literal["VERTICAL"] = "VERTICAL"
    underlying: str = Field(pattern=r"^[A-Z]{1,6}$")
    option_type: Literal["CALL", "PUT"]
    expiration: date
    long_strike: Decimal = Field(gt=0)
    short_strike: Decimal = Field(gt=0)
    quantity: int = Field(gt=0)

    @model_validator(mode="after")
    def distinct_strikes(self) -> SpreadProjection:
        if self.long_strike == self.short_strike:
            raise ValueError("vertical strikes must be distinct")
        return self


class ExposureProjection(StrictProjection):
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta_per_day: Decimal | None = None
    vega_per_iv_point: Decimal | None = None

    @field_validator("delta", "gamma", "theta_per_day", "vega_per_iv_point")
    @classmethod
    def finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("exposure values must be finite")
        return value

    @model_validator(mode="after")
    def at_least_one_value(self) -> ExposureProjection:
        if all(getattr(self, name) is None for name in self.__class__.model_fields):
            raise ValueError("exposure must contain a measured value")
        return self


class ThesisProjection(StrictProjection):
    direction: PositionDirection
    volatility_view: Literal["LONG", "SHORT", "NEUTRAL"]
    target_at: datetime

    @field_validator("target_at")
    @classmethod
    def aware_target(cls, value: datetime) -> datetime:
        return require_aware(value)


class ExecutionEventProjection(StrictProjection):
    event_kind: Literal["EXECUTION"] = "EXECUTION"
    action: Literal["ENTRY", "ROLL", "CLOSE"]
    occurred_at: datetime
    reason_category: Literal["POSITION_OPENED", "POSITION_ROLLED", "POSITION_CLOSED"]
    cashflow_usd: Decimal
    execution_status: Literal["FILLED"]
    resulting_state: PositionState
    spread_after: SpreadProjection | None

    @field_validator("occurred_at")
    @classmethod
    def aware_occurrence(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def action_matches_state(self) -> ExecutionEventProjection:
        expected_reason = {
            "ENTRY": "POSITION_OPENED",
            "ROLL": "POSITION_ROLLED",
            "CLOSE": "POSITION_CLOSED",
        }[self.action]
        if self.reason_category != expected_reason:
            raise ValueError("execution event reason does not match its action")
        if self.action == "CLOSE":
            if self.resulting_state is not PositionState.CLOSED or self.spread_after is not None:
                raise ValueError("close event must leave a closed position")
        elif self.resulting_state is not PositionState.OPEN or self.spread_after is None:
            raise ValueError("entry and roll events must leave an open spread")
        return self


class AssessmentEventProjection(StrictProjection):
    event_kind: Literal["ASSESSMENT"] = "ASSESSMENT"
    action: PublicAssessmentAction
    occurred_at: datetime
    reason_category: PublicAssessmentReason

    @field_validator("occurred_at")
    @classmethod
    def aware_occurrence(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def action_matches_reason(self) -> AssessmentEventProjection:
        expected_reasons = {
            PublicAssessmentAction.HOLD: {PublicAssessmentReason.POSITION_REVIEWED},
            PublicAssessmentAction.CLOSE: {
                PublicAssessmentReason.RISK_REDUCTION,
                PublicAssessmentReason.THESIS_CHANGED,
            },
            PublicAssessmentAction.ROLL: {PublicAssessmentReason.POSITION_ADJUSTMENT},
            PublicAssessmentAction.NO_ACTION: {PublicAssessmentReason.DATA_INCOMPLETE},
        }
        if self.reason_category not in expected_reasons[self.action]:
            raise ValueError("assessment event reason does not match its action")
        return self


CompetitionEventProjection = Annotated[
    ExecutionEventProjection | AssessmentEventProjection,
    Field(discriminator="event_kind"),
]


class NoTradeProjection(StrictProjection):
    schema_version: Literal["v1"] = "v1"
    record_kind: Literal[CompetitionRecordKind.NO_TRADE] = CompetitionRecordKind.NO_TRADE
    public_record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["NO_TRADE"] = "NO_TRADE"
    reason_category: Literal["STRATEGY_NOT_READY"] = "STRATEGY_NOT_READY"
    decided_at: datetime
    observed_at: datetime
    paper_trading: Literal[True] = True

    @field_validator("decided_at", "observed_at")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def chronology(self) -> NoTradeProjection:
        if self.observed_at < self.decided_at:
            raise ValueError("no-trade observation precedes its decision boundary")
        return self


class PositionProjection(StrictProjection):
    schema_version: Literal["v1"] = "v1"
    record_kind: Literal[CompetitionRecordKind.POSITION] = CompetitionRecordKind.POSITION
    public_record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: PositionState
    underlying: str = Field(pattern=r"^[A-Z]{1,6}$")
    opening_spread: SpreadProjection
    current_spread: SpreadProjection | None
    opened_at: datetime
    as_of: datetime
    closed_at: datetime | None
    thesis: ThesisProjection
    events: tuple[CompetitionEventProjection, ...]
    current_exposure: ExposureProjection | None
    execution_status: Literal["FILLED"]
    paper_trading: Literal[True] = True

    @field_validator("opened_at", "as_of", "closed_at")
    @classmethod
    def aware_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware(value)

    @model_validator(mode="after")
    def lifecycle_consistency(self) -> PositionProjection:
        if not self.events or not isinstance(self.events[0], ExecutionEventProjection):
            raise ValueError("position requires an opening execution event")
        if self.events[0].action != "ENTRY" or self.opened_at != self.events[0].occurred_at:
            raise ValueError("position opening event is inconsistent")
        if tuple(event.occurred_at for event in self.events) != tuple(
            sorted(event.occurred_at for event in self.events)
        ):
            raise ValueError("position events are not chronological")
        if self.events[-1].occurred_at > self.as_of:
            raise ValueError("position as-of time precedes its latest event")
        execution_events = tuple(
            event for event in self.events if isinstance(event, ExecutionEventProjection)
        )
        lifecycle_state: PositionState | None = None
        for event in self.events:
            if isinstance(event, AssessmentEventProjection):
                if lifecycle_state is not PositionState.OPEN:
                    raise ValueError("assessment event requires an open position")
                continue
            if event.action == "ENTRY":
                if lifecycle_state is not None:
                    raise ValueError("position cannot contain more than one entry")
            elif lifecycle_state is not PositionState.OPEN:
                raise ValueError("position execution sequence is inconsistent")
            lifecycle_state = event.resulting_state
        spreads = (self.opening_spread, self.current_spread) + tuple(
            event.spread_after for event in execution_events
        )
        if any(spread is not None and spread.underlying != self.underlying for spread in spreads):
            raise ValueError("position spread underlying is inconsistent")
        if execution_events[0].spread_after != self.opening_spread:
            raise ValueError("position opening spread is inconsistent")
        if execution_events[-1].resulting_state is not self.state:
            raise ValueError("position state does not match its latest execution")
        if execution_events[-1].spread_after != self.current_spread:
            raise ValueError("position current spread does not match its latest execution")
        if self.state is PositionState.OPEN:
            if self.current_spread is None or self.closed_at is not None:
                raise ValueError("open position state is inconsistent")
        else:
            if self.current_spread is not None or self.closed_at is None:
                raise ValueError("closed position state is inconsistent")
            if (
                execution_events[-1].action != "CLOSE"
                or execution_events[-1].occurred_at != self.closed_at
            ):
                raise ValueError("position closing event is inconsistent")
        return self


Projection = NoTradeProjection | PositionProjection
PROJECTION_ADAPTER = TypeAdapter(Annotated[Projection, Field(discriminator="record_kind")])


class CompetitionRecord(StrictProjection):
    kind: CompetitionRecordKind
    public_record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: datetime
    published_at: datetime
    payload: dict[str, object]
    projection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("occurred_at", "published_at")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        return require_aware(value)

    @property
    def payload_text(self) -> str:
        return canonical_json(self.payload)


def validate_projection(value: object) -> Projection:
    return PROJECTION_ADAPTER.validate_python(value)


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


def canonical_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return canonical_value(value.model_dump(mode="json"))
    if isinstance(value, datetime):
        return utc_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical JSON cannot contain non-finite decimals")
        return format(value, "f")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical JSON keys must be strings")
        return {key: canonical_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [canonical_value(item) for item in value]
    if value is None or isinstance(value, str | int | bool):
        return value
    raise ValueError("value is not canonical JSON")


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(
        canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
