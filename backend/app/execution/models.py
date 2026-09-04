from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from backend.app.contracts.v1 import AccountRole, GreekExposure, PositionIntent
from backend.app.experiment_lineage import ExperimentExecutionLineage
from backend.app.order_limits import (
    MAX_STRUCTURAL_APPROVED_RISK,
    MAX_STRUCTURAL_OPTION_QUANTITY,
)


class ExecutionAction(StrEnum):
    ENTRY = "ENTRY"
    CLOSE = "CLOSE"
    ROLL = "ROLL"


class Actor(StrEnum):
    OWNER = "OWNER"
    SCHEDULER = "SCHEDULER"


class IntentState(StrEnum):
    APPROVED = "APPROVED"
    CLAIMED = "CLAIMED"
    TERMINAL = "TERMINAL"


class ExecutionBlocked(ValueError):
    pass


class ExecutionPendingCode(StrEnum):
    ADVANCE = "EXECUTION_ADVANCE_PENDING"
    LOOKUP_DEFERRED = "AMBIGUOUS_BROKER_LOOKUP_DEFERRED"
    LOOKUP_ABSENT = "AMBIGUOUS_BROKER_LOOKUP_ABSENT"
    CANCEL_LOOKUP_DEFERRED = "CANCEL_OUTCOME_LOOKUP_DEFERRED"
    CANCEL_PENDING = "CANCEL_OUTCOME_PENDING"
    CANCEL_NOT_TERMINAL = "CANCEL_OUTCOME_NOT_TERMINAL"


class ExecutionPending(ExecutionBlocked):
    def __init__(self, code: ExecutionPendingCode) -> None:
        if not isinstance(code, ExecutionPendingCode):
            raise TypeError("EXECUTION_PENDING_CODE_INVALID")
        self.code = code
        super().__init__(code.value)


class AmbiguousBrokerResponse(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderLegIntent:
    symbol: str
    intent: PositionIntent
    ratio: int


@dataclass(frozen=True)
class OrderEnvelope:
    action: ExecutionAction
    authorization_certificate_id: UUID
    policy_hash: str
    account_fingerprint: str
    position_or_book_fingerprint: str
    legs: tuple[OrderLegIntent, ...]
    quantity: int
    minimum_limit: Decimal
    maximum_limit: Decimal
    approved_max_loss: Decimal
    event_key: str
    trading_day: date
    market_session_id: UUID | None = None
    quoted_relative_spread: Decimal | None = None
    maximum_relative_spread: Decimal | None = None
    incremental_debit: Decimal | None = None
    maximum_incremental_debit: Decimal | None = None

    def __post_init__(self) -> None:
        expected_legs = 4 if self.action == ExecutionAction.ROLL else 2
        if (
            len(self.legs) != expected_legs
            or not 1 <= self.quantity <= MAX_STRUCTURAL_OPTION_QUANTITY
        ):
            raise ValueError("ORDER_STRUCTURE_OUT_OF_BOUNDS")
        if self.minimum_limit > self.maximum_limit:
            raise ValueError("PRICE_ENVELOPE_INVALID")
        if not Decimal(0) < self.approved_max_loss <= MAX_STRUCTURAL_APPROVED_RISK:
            raise ValueError("APPROVED_RISK_OUT_OF_BOUNDS")
        roll_values = (
            self.quoted_relative_spread,
            self.maximum_relative_spread,
            self.incremental_debit,
            self.maximum_incremental_debit,
        )
        if self.action is ExecutionAction.ROLL:
            if (
                self.market_session_id is None
                or any(
                    not isinstance(value, Decimal) or not value.is_finite() for value in roll_values
                )
                or not Decimal(0)
                <= self.quoted_relative_spread
                <= self.maximum_relative_spread
                < Decimal(1)
                or self.quoted_relative_spread
                != self.quoted_relative_spread.quantize(Decimal("0.0000000001"))
                or self.maximum_relative_spread
                != self.maximum_relative_spread.quantize(Decimal("0.0000000001"))
                or not Decimal(0)
                <= self.incremental_debit
                <= self.maximum_incremental_debit
                <= self.approved_max_loss
            ):
                raise ValueError("ROLL_AUTHORITY_OUT_OF_BOUNDS")
        elif self.market_session_id is not None or any(value is not None for value in roll_values):
            raise ValueError("ROLL_AUTHORITY_UNEXPECTED")


@dataclass(frozen=True)
class ExecutionIntent:
    intent_id: UUID
    account_role: AccountRole
    envelope: OrderEnvelope
    digest: str
    state: IntentState
    claimed_by: Actor | None = None
    claimed_at: datetime | None = None
    first_fill_consumed: bool = False
    claim_token: UUID | None = None
    claim_generation: int = 0
    execution_epoch: int = 0
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None


@dataclass(frozen=True)
class AccountExecutionLock:
    locked: bool
    reason: str | None = None
    locked_at: datetime | None = None


@dataclass(frozen=True)
class OrderAttempt:
    intent_id: UUID
    ordinal: int
    client_order_id: str
    request_hash: str
    state: str
    replaces_client_order_id: str | None = None
    provider_order_id: str | None = None
    filled_quantity: int = 0
    quantity: int = 0
    fill_cash_flow: Decimal | None = None
    limit_price: Decimal | None = None
    quote_hash: str | None = None
    quote_source_timestamps: tuple[datetime, ...] = ()
    quote_retrieved_at: datetime | None = None
    timing_authority_at: datetime | None = None
    prior_request_hash: str | None = None


@dataclass(frozen=True)
class BrokerResult:
    provider_order_id: str
    state: str
    filled_quantity: int
    quantity: int
    fill_cash_flow: Decimal | None = None


@dataclass(frozen=True)
class PositionGreekObservation:
    symbol: str
    signed_quantity: Decimal
    multiplier: int
    delta: Decimal
    gamma: Decimal
    theta_per_day: Decimal
    vega_per_iv_point: Decimal
    feed: str
    source_timestamp: datetime
    retrieved_at: datetime
    source_hash: str

    def __post_init__(self) -> None:
        values = (
            self.signed_quantity,
            self.delta,
            self.gamma,
            self.theta_per_day,
            self.vega_per_iv_point,
        )
        if (
            not isinstance(self.symbol, str)
            or not self.symbol
            or len(self.symbol) > 64
            or any(character.isspace() for character in self.symbol)
            or any(not isinstance(value, Decimal) or not value.is_finite() for value in values)
            or self.signed_quantity == 0
            or isinstance(self.multiplier, bool)
            or not isinstance(self.multiplier, int)
            or self.multiplier != 100
        ):
            raise ValueError("POSITION_GREEK_OBSERVATION_INVALID")
        if self.feed != "indicative":
            raise ValueError("POSITION_GREEK_FEED_INVALID")
        if (
            self.source_timestamp.tzinfo is None
            or self.source_timestamp.utcoffset() is None
            or self.retrieved_at.tzinfo is None
            or self.retrieved_at.utcoffset() is None
            or self.source_timestamp > self.retrieved_at
        ):
            raise ValueError("POSITION_GREEK_TIMESTAMP_INVALID")
        if len(self.source_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_hash
        ):
            raise ValueError("POSITION_GREEK_SOURCE_HASH_INVALID")


@dataclass(frozen=True)
class Reconciliation:
    terminal: bool
    remainder_absent: bool
    matches_expected: bool
    assignment_suspected: bool
    actual_exposure: GreekExposure | None


@dataclass(frozen=True)
class FrozenThesisVersion:
    thesis_version_id: UUID
    thesis_id: UUID
    account_role: AccountRole
    version: int
    thesis_hash: str
    policy_hash: str
    underlying: str
    thesis_code: str
    frozen_at: datetime
    target_at: datetime
    intended_exposure: dict[str, object]
    exposure_limits: dict[str, object]
    volatility_view: str
    entry_atm_iv: Decimal
    approved_max_loss: Decimal
    portfolio_risk_cap: Decimal
    invalidation_codes: tuple[str, ...]
    thesis_payload: dict[str, object]
    created_at: datetime
    origin_hash: str | None = None


@dataclass(frozen=True)
class EntryApprovalAuthorization:
    approval_id: UUID
    thesis_version_id: UUID
    account_role: AccountRole
    policy_hash: str
    book_fingerprint: str
    envelope_hash: str
    approved_max_loss: Decimal
    quantity: int
    valid_from: datetime
    expires_at: datetime
    valid: bool = True
    experiment_lineage: ExperimentExecutionLineage | None = None


@dataclass(frozen=True)
class AssessmentCertificate:
    certificate_id: UUID
    assessment_id: UUID
    thesis_version_id: UUID
    account_role: AccountRole
    action: ExecutionAction
    position_fingerprint: str
    envelope_hash: str
    approved_max_loss: Decimal
    quantity: int
    expected_after_exposure: GreekExposure | None
    policy_hash: str
    created_at: datetime
    expires_at: datetime
    valid: bool = True
    experiment_lineage: ExperimentExecutionLineage | None = None


@dataclass(frozen=True)
class ExecutionCertificate:
    certificate_id: UUID
    intent_id: UUID
    entry_approval_id: UUID | None
    assessment_certificate_id: UUID | None
    execution_status: str
    attempt_ids: tuple[str, ...]
    actual_exposure: GreekExposure | None
    reconciliation_checks: tuple[str, ...]
    created_at: datetime
    reconciliation_id: UUID | None = None
    reconciliation_hash: str | None = None
    last_observation_hash: str | None = None

    def __post_init__(self) -> None:
        if (self.entry_approval_id is None) == (self.assessment_certificate_id is None):
            raise ValueError("EXACTLY_ONE_AUTHORIZATION_ORIGIN_REQUIRED")
        provenance = (
            self.reconciliation_id,
            self.reconciliation_hash,
            self.last_observation_hash,
        )
        if any(value is not None for value in provenance) and not all(
            value is not None for value in provenance
        ):
            raise ValueError("FINALIZATION_PROVENANCE_INCOMPLETE")
