from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from types import MappingProxyType

from backend.app.contracts.v1 import AccountRole, DataQuality, OptionRight, PositionIntent
from backend.app.order_limits import (
    MAX_STRUCTURAL_APPROVED_RISK,
    MAX_STRUCTURAL_LIFETIME_ENTRIES,
    MAX_STRUCTURAL_LIFETIME_RISK,
    MAX_STRUCTURAL_OPTION_QUANTITY,
    STRUCTURAL_PILOT_PER_CONTRACT_RISK,
    SUBMISSION_STRUCTURAL_OPTION_QUANTITY,
)


class OpportunityOutcome(StrEnum):
    ENTRY_APPROVED = "ENTRY_APPROVED"
    NO_TRADE = "NO_TRADE"


class OpportunityReason(StrEnum):
    ENTRY_APPROVED = "ENTRY_APPROVED"
    DECISION_BOUNDARY_INCOMPLETE = "DECISION_BOUNDARY_INCOMPLETE"
    DECISION_BOUNDARY_MISMATCH = "DECISION_BOUNDARY_MISMATCH"
    DECISION_BOUNDARY_NOT_REACHED = "DECISION_BOUNDARY_NOT_REACHED"
    DECISION_BOUNDARY_STALE = "DECISION_BOUNDARY_STALE"
    DATA_QUALITY_INCOMPLETE = "DATA_QUALITY_INCOMPLETE"
    NORMALIZED_INPUT_INVALID = "NORMALIZED_INPUT_INVALID"
    CATALYST_CONTRADICTED = "CATALYST_CONTRADICTED"
    CATALYST_DATA_MISSING = "CATALYST_DATA_MISSING"
    CATALYST_DATA_STALE = "CATALYST_DATA_STALE"
    CATALYST_SCORE_BELOW_MINIMUM = "CATALYST_SCORE_BELOW_MINIMUM"
    MARKET_CLOSED = "MARKET_CLOSED"
    TRADING_HALT_STATUS_UNKNOWN = "TRADING_HALT_STATUS_UNKNOWN"
    TRADING_HALTED = "TRADING_HALTED"
    UNDERLYING_DATA_STALE = "UNDERLYING_DATA_STALE"
    BETA_OUT_OF_BOUNDS = "BETA_OUT_OF_BOUNDS"
    FIRST_REACTION_OUT_OF_SUPPORT = "FIRST_REACTION_OUT_OF_SUPPORT"
    OPTION_CANDIDATE_MISSING = "OPTION_CANDIDATE_MISSING"
    OPTION_CANDIDATE_INVALID = "OPTION_CANDIDATE_INVALID"
    OPTION_ONLY_REQUIRED = "OPTION_ONLY_REQUIRED"
    VERTICAL_STRUCTURE_INVALID = "VERTICAL_STRUCTURE_INVALID"
    OPTION_FEED_NOT_INDICATIVE = "OPTION_FEED_NOT_INDICATIVE"
    OPTION_QUOTE_STALE = "OPTION_QUOTE_STALE"
    OPTION_QUOTES_UNSYNCHRONIZED = "OPTION_QUOTES_UNSYNCHRONIZED"
    OPTION_QUOTE_INVALID = "OPTION_QUOTE_INVALID"
    OPTION_QUOTE_TOO_WIDE = "OPTION_QUOTE_TOO_WIDE"
    OPTION_GREEKS_MISSING = "OPTION_GREEKS_MISSING"
    OPTION_DTE_OUT_OF_RANGE = "OPTION_DTE_OUT_OF_RANGE"
    OPTION_DTE_MISMATCH = "OPTION_DTE_MISMATCH"
    OPTION_CONTRACT_INELIGIBLE = "OPTION_CONTRACT_INELIGIBLE"
    OPTION_PAYOFF_INVALID = "OPTION_PAYOFF_INVALID"
    CANDIDATE_SCORE_BELOW_MINIMUM = "CANDIDATE_SCORE_BELOW_MINIMUM"
    CANDIDATE_FALLBACK_FORBIDDEN = "CANDIDATE_FALLBACK_FORBIDDEN"
    BUYING_POWER_INSUFFICIENT = "BUYING_POWER_INSUFFICIENT"
    QUANTITY_OUT_OF_BOUNDS = "QUANTITY_OUT_OF_BOUNDS"
    ACCOUNT_ROLE_NOT_EXECUTABLE = "ACCOUNT_ROLE_NOT_EXECUTABLE"
    BASELINE_NOT_CLEAN = "BASELINE_NOT_CLEAN"
    EQUITY_FLOOR_REACHED = "EQUITY_FLOOR_REACHED"
    ACCOUNT_NOT_FLAT = "ACCOUNT_NOT_FLAT"
    OPEN_ORDER_EXISTS = "OPEN_ORDER_EXISTS"
    LIFETIME_ENTRY_LIMIT_REACHED = "LIFETIME_ENTRY_LIMIT_REACHED"
    LIFETIME_RISK_LIMIT_REACHED = "LIFETIME_RISK_LIMIT_REACHED"
    EVENT_ALREADY_ATTEMPTED = "EVENT_ALREADY_ATTEMPTED"
    POSITION_RISK_LIMIT_EXCEEDED = "POSITION_RISK_LIMIT_EXCEEDED"
    ENTRY_WINDOW_CLOSED = "ENTRY_WINDOW_CLOSED"
    POLICY_SCOPE_MISMATCH = "POLICY_SCOPE_MISMATCH"
    BOOK_FINGERPRINT_INVALID = "BOOK_FINGERPRINT_INVALID"
    ACCOUNT_STATE_INVALID = "ACCOUNT_STATE_INVALID"
    ENTRY_RESERVATION_ACTIVE = "ENTRY_RESERVATION_ACTIVE"
    PRIOR_DECISION_BINDING = "PRIOR_DECISION_BINDING"
    DIRECTION_NOT_CONFIRMED = "DIRECTION_NOT_CONFIRMED"
    VERTICAL_DIRECTION_MISMATCH = "VERTICAL_DIRECTION_MISMATCH"


class OpportunityDirection(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class TradingHaltState(StrEnum):
    HALTED = "HALTED"
    NOT_HALTED = "NOT_HALTED"
    UNKNOWN = "UNKNOWN"


class CatalystQuality(StrEnum):
    CLEAR = "CLEAR"
    UNRESOLVED_RISK = "UNRESOLVED_RISK"
    AUTHORITATIVE_CONTRADICTION = "AUTHORITATIVE_CONTRADICTION"
    MISSING = "MISSING"


class InstrumentKind(StrEnum):
    OPTION = "OPTION"
    EQUITY = "EQUITY"


class OptionFeed(StrEnum):
    INDICATIVE_MODIFIED = "INDICATIVE_MODIFIED"
    OPRA = "OPRA"
    OTHER = "OTHER"


class VerticalStrategy(StrEnum):
    BULL_CALL_DEBIT = "BULL_CALL_DEBIT"
    BULL_PUT_CREDIT = "BULL_PUT_CREDIT"
    BEAR_PUT_DEBIT = "BEAR_PUT_DEBIT"
    BEAR_CALL_CREDIT = "BEAR_CALL_CREDIT"


STRUCTURAL_BULLISH_PILOT_ID = "SPY_STRUCTURAL_BULLISH_BETA_PILOT_V1"
STRUCTURAL_BULLISH_OTM_PILOT_ID = "SPY_STRUCTURAL_BULLISH_OTM_PILOT_V1"
STRUCTURAL_BEARISH_OTM_PILOT_ID = "SPY_STRUCTURAL_BEARISH_OTM_PILOT_V1"


@dataclass(frozen=True)
class StructuralBullishPilotProfile:
    direction: OpportunityDirection = OpportunityDirection.BULLISH
    target_dte: int = 38
    minimum_dte: int = 30
    maximum_dte: int = 45
    width: Decimal = Decimal("4")
    minimum_long_delta: Decimal = Decimal("0.55")
    maximum_long_delta: Decimal = Decimal("0.65")
    quantity: int = SUBMISSION_STRUCTURAL_OPTION_QUANTITY
    maximum_debit: Decimal = STRUCTURAL_PILOT_PER_CONTRACT_RISK / 100
    minimum_reward_to_risk: Decimal = Decimal("0.75")
    maximum_relative_spread: Decimal = Decimal("0.05")
    maximum_quote_age: timedelta = timedelta(seconds=20)
    maximum_quote_skew: timedelta = timedelta(seconds=3)


STRUCTURAL_BULLISH_PILOT = StructuralBullishPilotProfile()
STRUCTURAL_BULLISH_OTM_PILOT = StructuralBullishPilotProfile(
    minimum_long_delta=Decimal("0.35"),
    maximum_long_delta=Decimal("0.50"),
)
STRUCTURAL_BEARISH_OTM_PILOT = StructuralBullishPilotProfile(
    direction=OpportunityDirection.BEARISH,
    minimum_long_delta=Decimal("0.35"),
    maximum_long_delta=Decimal("0.50"),
)

STRUCTURAL_PILOT_PROFILES: Mapping[str, StructuralBullishPilotProfile] = MappingProxyType(
    {
        STRUCTURAL_BULLISH_PILOT_ID: STRUCTURAL_BULLISH_PILOT,
        STRUCTURAL_BULLISH_OTM_PILOT_ID: STRUCTURAL_BULLISH_OTM_PILOT,
        STRUCTURAL_BEARISH_OTM_PILOT_ID: STRUCTURAL_BEARISH_OTM_PILOT,
    }
)


def structural_pilot_profile(
    opportunity_key: str,
) -> StructuralBullishPilotProfile | None:
    return STRUCTURAL_PILOT_PROFILES.get(opportunity_key)


@dataclass(frozen=True)
class OpportunityPolicy:
    version: str
    opportunity_key: str
    underlying: str
    selected_decision_boundary: datetime
    last_entry_boundary: datetime
    maximum_decision_delay: timedelta
    maximum_underlying_age: timedelta
    maximum_catalyst_age: timedelta
    maximum_option_quote_age: timedelta
    maximum_leg_quote_skew: timedelta
    minimum_vwap_distance: Decimal
    maximum_vwap_distance: Decimal
    minimum_relative_return: Decimal
    minimum_beta: Decimal
    maximum_beta: Decimal
    required_trend_hits: int
    maximum_first_reaction: Decimal
    minimum_catalyst_score: int
    minimum_candidate_score: int
    minimum_dte: int
    maximum_dte: int
    maximum_relative_spread: Decimal
    minimum_debit_width_fraction: Decimal
    maximum_debit_width_fraction: Decimal
    minimum_credit_width_fraction: Decimal
    maximum_position_loss: Decimal
    maximum_equity_risk_fraction: Decimal
    maximum_lifetime_entries: int
    maximum_lifetime_risk: Decimal
    equity_floor: Decimal
    maximum_quantity: int

    def __post_init__(self) -> None:
        decimals = (
            self.minimum_vwap_distance,
            self.maximum_vwap_distance,
            self.minimum_relative_return,
            self.minimum_beta,
            self.maximum_beta,
            self.maximum_first_reaction,
            self.maximum_relative_spread,
            self.minimum_debit_width_fraction,
            self.maximum_debit_width_fraction,
            self.minimum_credit_width_fraction,
            self.maximum_position_loss,
            self.maximum_equity_risk_fraction,
            self.maximum_lifetime_risk,
            self.equity_floor,
        )
        valid = (
            bool(self.version and self.opportunity_key and self.underlying)
            and _is_utc(self.selected_decision_boundary)
            and _is_utc(self.last_entry_boundary)
            and all(
                duration > timedelta(0)
                for duration in (
                    self.maximum_decision_delay,
                    self.maximum_underlying_age,
                    self.maximum_catalyst_age,
                    self.maximum_option_quote_age,
                    self.maximum_leg_quote_skew,
                )
            )
            and all(value.is_finite() for value in decimals)
            and 0 <= self.minimum_vwap_distance < self.maximum_vwap_distance
            and self.minimum_relative_return >= 0
            and 0 <= self.minimum_beta < self.maximum_beta
            and 1 <= self.required_trend_hits <= 3
            and self.maximum_first_reaction > 0
            and 0 <= self.minimum_catalyst_score <= 100
            and 0 <= self.minimum_candidate_score <= 100
            and 1 <= self.minimum_dte <= self.maximum_dte
            and 0 < self.maximum_relative_spread < 1
            and 0 < self.minimum_debit_width_fraction <= self.maximum_debit_width_fraction < 1
            and 0 < self.minimum_credit_width_fraction < 1
            and 0 < self.maximum_position_loss <= MAX_STRUCTURAL_APPROVED_RISK
            and 0 < self.maximum_equity_risk_fraction <= 1
            and 0 < self.maximum_lifetime_entries <= MAX_STRUCTURAL_LIFETIME_ENTRIES
            and self.maximum_position_loss
            <= self.maximum_lifetime_risk
            <= MAX_STRUCTURAL_LIFETIME_RISK
            and self.equity_floor > 0
            and 0 < self.maximum_quantity <= MAX_STRUCTURAL_OPTION_QUANTITY
        )
        if not valid:
            raise ValueError("INVALID_OPPORTUNITY_POLICY")


@dataclass(frozen=True)
class OptionLeg:
    instrument_kind: InstrumentKind
    symbol: str
    underlying: str
    right: OptionRight
    strike: Decimal
    expiry: date
    intent: PositionIntent
    ratio: int
    multiplier: int
    active: bool
    tradable: bool
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    quote_at: datetime
    feed: OptionFeed
    greeks_complete: bool
    greek_units_verified: bool


@dataclass(frozen=True)
class VerticalCandidate:
    strategy: VerticalStrategy
    legs: tuple[OptionLeg, ...]
    quantity: int
    dte: int
    approved_limit: Decimal
    candidate_score: int
    selection_rank: int
    buying_power_sufficient: bool
    maximum_limit: Decimal | None = None


@dataclass(frozen=True)
class AccountOpportunityState:
    account_role: AccountRole
    book_fingerprint: str
    baseline_clean: bool
    clean_equity: Decimal
    open_position_count: int
    open_order_count: int
    filled_entry_count: int
    lifetime_approved_risk: Decimal
    entry_reservation_active: bool
    reserved_approved_risk: Decimal
    event_already_attempted: bool


@dataclass(frozen=True)
class OpportunityInput:
    opportunity_key: str
    underlying: str
    observed_decision_boundary: datetime
    evaluated_at: datetime
    completed_bar_at: datetime
    decision_boundary_complete: bool
    prior_decision_outcome: OpportunityOutcome | None
    data_quality: DataQuality
    market_open: bool
    trading_halted: TradingHaltState
    underlying_observed_at: datetime
    catalyst_observed_at: datetime
    catalyst_quality: CatalystQuality
    catalyst_score: int
    vwap_distance: Decimal
    relative_return: Decimal
    beta: Decimal
    bull_trend_hits: int
    bear_trend_hits: int
    absolute_first_reaction: Decimal
    candidate: VerticalCandidate | None
    account: AccountOpportunityState


@dataclass(frozen=True)
class OpportunityDecisionRecord:
    outcome: OpportunityOutcome
    reason_codes: tuple[OpportunityReason, ...]
    opportunity_key: str
    decision_boundary: datetime
    direction: OpportunityDirection | None
    strategy: VerticalStrategy | None
    quantity: int | None
    approved_max_loss: Decimal | None
    book_fingerprint: str
    candidate_hash: str | None
    input_hash: str
    policy_hash: str
    result_hash: str


def evaluate_opportunity(
    policy: OpportunityPolicy, values: OpportunityInput
) -> OpportunityDecisionRecord:
    policy_hash = opportunity_policy_hash(policy)
    input_hash = _canonical_hash("alphadecay.opportunity.input.v1", values)
    candidate = values.candidate
    candidate_hash = (
        _canonical_hash("alphadecay.opportunity.candidate.v1", candidate)
        if candidate is not None
        else None
    )
    structural_profile = structural_pilot_profile(policy.opportunity_key)
    structural_pilot = structural_profile is not None
    if (
        values.opportunity_key != policy.opportunity_key
        or values.underlying != policy.underlying
        or (structural_pilot and policy.underlying != "SPY")
    ):
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.POLICY_SCOPE_MISMATCH,
        )
    if values.prior_decision_outcome is not None:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.PRIOR_DECISION_BINDING,
        )
    if values.observed_decision_boundary != policy.selected_decision_boundary:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.DECISION_BOUNDARY_MISMATCH,
        )
    if (
        not values.decision_boundary_complete
        or values.completed_bar_at != values.observed_decision_boundary
    ):
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.DECISION_BOUNDARY_INCOMPLETE,
        )
    if values.evaluated_at < values.observed_decision_boundary:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.DECISION_BOUNDARY_NOT_REACHED,
        )
    if values.evaluated_at - values.observed_decision_boundary > policy.maximum_decision_delay:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.DECISION_BOUNDARY_STALE,
        )
    if values.data_quality != DataQuality.COMPLETE:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.DATA_QUALITY_INCOMPLETE,
        )
    if not _normalized_input_valid(values):
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.NORMALIZED_INPUT_INVALID,
        )
    if not values.market_open:
        return _rejected(
            policy_hash, input_hash, candidate_hash, values, OpportunityReason.MARKET_CLOSED
        )
    if values.trading_halted is TradingHaltState.UNKNOWN:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.TRADING_HALT_STATUS_UNKNOWN,
        )
    if values.trading_halted is TradingHaltState.HALTED:
        return _rejected(
            policy_hash, input_hash, candidate_hash, values, OpportunityReason.TRADING_HALTED
        )
    if not _fresh_at(
        values.underlying_observed_at, values.evaluated_at, policy.maximum_underlying_age
    ):
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.UNDERLYING_DATA_STALE,
        )
    if not _fresh_at(values.catalyst_observed_at, values.evaluated_at, policy.maximum_catalyst_age):
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.CATALYST_DATA_STALE,
        )
    if not structural_pilot and values.catalyst_quality == CatalystQuality.MISSING:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.CATALYST_DATA_MISSING,
        )
    if (
        not structural_pilot
        and values.catalyst_quality == CatalystQuality.AUTHORITATIVE_CONTRADICTION
    ):
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.CATALYST_CONTRADICTED,
        )
    if not structural_pilot and values.catalyst_score < policy.minimum_catalyst_score:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.CATALYST_SCORE_BELOW_MINIMUM,
        )
    if not structural_pilot and not policy.minimum_beta < values.beta <= policy.maximum_beta:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.BETA_OUT_OF_BOUNDS,
        )
    if not structural_pilot and values.absolute_first_reaction > policy.maximum_first_reaction:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.FIRST_REACTION_OUT_OF_SUPPORT,
        )
    direction = derive_opportunity_direction(
        policy,
        vwap_distance=values.vwap_distance,
        relative_return=values.relative_return,
        bull_trend_hits=values.bull_trend_hits,
        bear_trend_hits=values.bear_trend_hits,
    )
    if direction is None:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.DIRECTION_NOT_CONFIRMED,
        )
    if candidate is None:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.OPTION_CANDIDATE_MISSING,
            direction,
        )
    if candidate.strategy not in _DIRECTION_STRATEGIES[direction]:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            OpportunityReason.VERTICAL_DIRECTION_MISMATCH,
            direction,
        )
    candidate_failure = _option_candidate_failure(policy, values, candidate)
    if candidate_failure is not None:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            candidate_failure,
            direction,
        )
    approved_max_loss = _approved_max_loss(candidate)
    account_failure = _account_failure(policy, values, approved_max_loss)
    if account_failure is not None:
        return _rejected(
            policy_hash,
            input_hash,
            candidate_hash,
            values,
            account_failure,
            direction,
        )

    return _record(
        policy_hash=policy_hash,
        input_hash=input_hash,
        candidate_hash=candidate_hash,
        values=values,
        outcome=OpportunityOutcome.ENTRY_APPROVED,
        reason_codes=(OpportunityReason.ENTRY_APPROVED,),
        direction=direction,
        strategy=candidate.strategy,
        quantity=candidate.quantity,
        approved_max_loss=approved_max_loss,
    )


def derive_opportunity_direction(
    policy: OpportunityPolicy,
    *,
    vwap_distance: Decimal,
    relative_return: Decimal,
    bull_trend_hits: int,
    bear_trend_hits: int,
) -> OpportunityDirection | None:
    structural_profile = structural_pilot_profile(policy.opportunity_key)
    if structural_profile is not None and policy.underlying == "SPY":
        return structural_profile.direction
    vwap_size_passes = (
        abs(vwap_distance) > policy.minimum_vwap_distance
        and abs(vwap_distance) <= policy.maximum_vwap_distance
    )
    if (
        vwap_size_passes
        and vwap_distance > 0
        and relative_return > policy.minimum_relative_return
        and bull_trend_hits >= policy.required_trend_hits
    ):
        return OpportunityDirection.BULLISH
    if (
        vwap_size_passes
        and vwap_distance < 0
        and relative_return < -policy.minimum_relative_return
        and bear_trend_hits >= policy.required_trend_hits
    ):
        return OpportunityDirection.BEARISH
    return None


def opportunity_policy_hash(policy: OpportunityPolicy) -> str:
    material: object = policy
    structural_profile = structural_pilot_profile(policy.opportunity_key)
    if structural_profile is not None:
        material = {
            "policy": policy,
            "strategy_profile": replace(
                structural_profile,
                quantity=policy.maximum_quantity,
            ),
        }
    return _canonical_hash("alphadecay.opportunity.policy.v1", material)


_FRESHNESS_SKEW_TOLERANCE = timedelta(seconds=30)


def _fresh_at(observed_at: datetime, decision_at: datetime, maximum_age: timedelta) -> bool:
    """Evidence retrieved shortly after the trusted decision time is still fresh, not future."""
    age = decision_at - observed_at
    return -_FRESHNESS_SKEW_TOLERANCE <= age <= maximum_age


def _normalized_input_valid(values: OpportunityInput) -> bool:
    signals = (
        values.vwap_distance,
        values.relative_return,
        values.beta,
        values.absolute_first_reaction,
    )
    return (
        all(signal.is_finite() for signal in signals)
        and type(values.trading_halted) is TradingHaltState
        and values.absolute_first_reaction >= 0
        and 0 <= values.catalyst_score <= 100
        and 0 <= values.bull_trend_hits <= 3
        and 0 <= values.bear_trend_hits <= 3
    )


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _option_candidate_failure(
    policy: OpportunityPolicy,
    values: OpportunityInput,
    candidate: VerticalCandidate,
) -> OpportunityReason | None:
    maximum_limit = _candidate_maximum_limit(candidate)
    if (
        not candidate.approved_limit.is_finite()
        or candidate.approved_limit <= 0
        or not maximum_limit.is_finite()
        or maximum_limit < candidate.approved_limit
        or not 0 <= candidate.candidate_score <= 100
        or candidate.dte < 0
    ):
        return OpportunityReason.OPTION_CANDIDATE_INVALID
    if len(candidate.legs) != 2:
        return OpportunityReason.VERTICAL_STRUCTURE_INVALID
    if any(leg.instrument_kind != InstrumentKind.OPTION for leg in candidate.legs):
        return OpportunityReason.OPTION_ONLY_REQUIRED
    if candidate.selection_rank != 1:
        return OpportunityReason.CANDIDATE_FALLBACK_FORBIDDEN
    if not 1 <= candidate.quantity <= policy.maximum_quantity:
        return OpportunityReason.QUANTITY_OUT_OF_BOUNDS
    structural_profile = structural_pilot_profile(policy.opportunity_key)
    structural_pilot = structural_profile is not None
    if not structural_pilot and candidate.candidate_score < policy.minimum_candidate_score:
        return OpportunityReason.CANDIDATE_SCORE_BELOW_MINIMUM
    if not candidate.buying_power_sufficient:
        return OpportunityReason.BUYING_POWER_INSUFFICIENT
    minimum_dte = structural_profile.minimum_dte if structural_profile else policy.minimum_dte
    maximum_dte = structural_profile.maximum_dte if structural_profile else policy.maximum_dte
    if not minimum_dte <= candidate.dte <= maximum_dte:
        return OpportunityReason.OPTION_DTE_OUT_OF_RANGE

    if any(
        not leg.active or not leg.tradable or not leg.symbol or leg.underlying != values.underlying
        for leg in candidate.legs
    ):
        return OpportunityReason.OPTION_CONTRACT_INELIGIBLE
    if not _is_vertical(candidate):
        return OpportunityReason.VERTICAL_STRUCTURE_INVALID
    actual_dte = (candidate.legs[0].expiry - values.observed_decision_boundary.date()).days
    if candidate.dte != actual_dte:
        return OpportunityReason.OPTION_DTE_MISMATCH
    if any(leg.feed != OptionFeed.INDICATIVE_MODIFIED for leg in candidate.legs):
        return OpportunityReason.OPTION_FEED_NOT_INDICATIVE
    if any(not _quote_valid(leg) for leg in candidate.legs):
        return OpportunityReason.OPTION_QUOTE_INVALID
    maximum_quote_age = (
        structural_profile.maximum_quote_age
        if structural_profile
        else policy.maximum_option_quote_age
    )
    if any(
        not _fresh_at(leg.quote_at, values.evaluated_at, maximum_quote_age)
        for leg in candidate.legs
    ):
        return OpportunityReason.OPTION_QUOTE_STALE
    quote_times = tuple(leg.quote_at for leg in candidate.legs)
    maximum_quote_skew = (
        structural_profile.maximum_quote_skew
        if structural_profile
        else policy.maximum_leg_quote_skew
    )
    if max(quote_times) - min(quote_times) > maximum_quote_skew:
        return OpportunityReason.OPTION_QUOTES_UNSYNCHRONIZED
    maximum_relative_spread = (
        structural_profile.maximum_relative_spread
        if structural_profile
        else policy.maximum_relative_spread
    )
    if any(_relative_spread(leg) > maximum_relative_spread for leg in candidate.legs):
        return OpportunityReason.OPTION_QUOTE_TOO_WIDE
    if any(not leg.greeks_complete or not leg.greek_units_verified for leg in candidate.legs):
        return OpportunityReason.OPTION_GREEKS_MISSING
    payoff_valid = (
        _structural_pilot_payoff_valid(policy, candidate, structural_profile)
        if structural_profile
        else _payoff_geometry_valid(policy, candidate)
    )
    if not payoff_valid:
        return OpportunityReason.OPTION_PAYOFF_INVALID
    return None


def _is_vertical(candidate: VerticalCandidate) -> bool:
    first, second = candidate.legs
    if (
        first.symbol == second.symbol
        or first.underlying != second.underlying
        or first.right != second.right
        or first.expiry != second.expiry
        or first.strike == second.strike
        or first.multiplier != 100
        or second.multiplier != 100
        or first.ratio != 1
        or second.ratio != 1
    ):
        return False
    by_intent = {leg.intent: leg for leg in candidate.legs}
    if set(by_intent) != {PositionIntent.BUY_TO_OPEN, PositionIntent.SELL_TO_OPEN}:
        return False
    bought = by_intent[PositionIntent.BUY_TO_OPEN]
    sold = by_intent[PositionIntent.SELL_TO_OPEN]
    if candidate.strategy == VerticalStrategy.BULL_CALL_DEBIT:
        return bought.right == OptionRight.CALL and bought.strike < sold.strike
    if candidate.strategy == VerticalStrategy.BEAR_PUT_DEBIT:
        return bought.right == OptionRight.PUT and bought.strike > sold.strike
    if candidate.strategy == VerticalStrategy.BULL_PUT_CREDIT:
        return bought.right == OptionRight.PUT and bought.strike < sold.strike
    return bought.right == OptionRight.CALL and sold.strike < bought.strike


def _quote_valid(leg: OptionLeg) -> bool:
    return (
        leg.bid.is_finite()
        and leg.ask.is_finite()
        and 0 < leg.bid <= leg.ask
        and leg.bid_size > 0
        and leg.ask_size > 0
    )


def _relative_spread(leg: OptionLeg) -> Decimal:
    midpoint = (leg.bid + leg.ask) / 2
    return (leg.ask - leg.bid) / midpoint


def _payoff_geometry_valid(policy: OpportunityPolicy, candidate: VerticalCandidate) -> bool:
    by_intent = {leg.intent: leg for leg in candidate.legs}
    bought = by_intent[PositionIntent.BUY_TO_OPEN]
    sold = by_intent[PositionIntent.SELL_TO_OPEN]
    width = abs(bought.strike - sold.strike)
    if candidate.strategy in (
        VerticalStrategy.BULL_CALL_DEBIT,
        VerticalStrategy.BEAR_PUT_DEBIT,
    ):
        natural = bought.ask - sold.bid
        maximum_limit = _candidate_maximum_limit(candidate)
        fractions = (
            natural / width,
            candidate.approved_limit / width,
            maximum_limit / width,
        )
        return candidate.approved_limit <= maximum_limit <= natural and all(
            policy.minimum_debit_width_fraction <= fraction <= policy.maximum_debit_width_fraction
            for fraction in fractions
        )
    natural = sold.bid - bought.ask
    fractions = (natural / width, candidate.approved_limit / width)
    return candidate.approved_limit >= natural and all(
        policy.minimum_credit_width_fraction <= fraction < 1 for fraction in fractions
    )


def _structural_pilot_payoff_valid(
    policy: OpportunityPolicy,
    candidate: VerticalCandidate,
    profile: StructuralBullishPilotProfile,
) -> bool:
    expected_strategy = (
        VerticalStrategy.BULL_CALL_DEBIT
        if profile.direction is OpportunityDirection.BULLISH
        else VerticalStrategy.BEAR_PUT_DEBIT
    )
    expected_right = (
        OptionRight.CALL if profile.direction is OpportunityDirection.BULLISH else OptionRight.PUT
    )
    if candidate.strategy is not expected_strategy or candidate.quantity != policy.maximum_quantity:
        return False
    bought, sold = candidate.legs
    width = abs(sold.strike - bought.strike)
    natural = bought.ask - sold.bid
    maximum_limit = _candidate_maximum_limit(candidate)
    return (
        bought.right is expected_right
        and sold.right is expected_right
        and width == profile.width
        and Decimal(0) < candidate.approved_limit <= maximum_limit <= natural
        and maximum_limit <= profile.maximum_debit
        and width - maximum_limit >= profile.minimum_reward_to_risk * maximum_limit
    )


def _approved_max_loss(candidate: VerticalCandidate) -> Decimal:
    width = abs(candidate.legs[0].strike - candidate.legs[1].strike)
    if candidate.strategy in (
        VerticalStrategy.BULL_CALL_DEBIT,
        VerticalStrategy.BEAR_PUT_DEBIT,
    ):
        return _candidate_maximum_limit(candidate) * candidate.quantity * 100
    return (width - candidate.approved_limit) * candidate.quantity * 100


def _candidate_maximum_limit(candidate: VerticalCandidate) -> Decimal:
    return candidate.approved_limit if candidate.maximum_limit is None else candidate.maximum_limit


def _account_failure(
    policy: OpportunityPolicy,
    values: OpportunityInput,
    approved_max_loss: Decimal,
) -> OpportunityReason | None:
    account = values.account
    if len(account.book_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in account.book_fingerprint
    ):
        return OpportunityReason.BOOK_FINGERPRINT_INVALID
    if (
        not account.clean_equity.is_finite()
        or account.clean_equity <= 0
        or account.open_position_count < 0
        or account.open_order_count < 0
        or account.filled_entry_count < 0
        or not account.lifetime_approved_risk.is_finite()
        or account.lifetime_approved_risk < 0
        or not account.reserved_approved_risk.is_finite()
        or account.reserved_approved_risk < 0
        or (not account.entry_reservation_active and account.reserved_approved_risk != 0)
    ):
        return OpportunityReason.ACCOUNT_STATE_INVALID
    if account.account_role == AccountRole.REPLAY:
        return OpportunityReason.ACCOUNT_ROLE_NOT_EXECUTABLE
    # A book bound to the reconciled state may hold positions; each spread is entered and
    # managed independently. An open order still blocks a new entry.
    if account.clean_equity <= policy.equity_floor:
        return OpportunityReason.EQUITY_FLOOR_REACHED
    if account.open_order_count != 0:
        return OpportunityReason.OPEN_ORDER_EXISTS
    if account.entry_reservation_active:
        return OpportunityReason.ENTRY_RESERVATION_ACTIVE
    if account.filled_entry_count >= policy.maximum_lifetime_entries:
        return OpportunityReason.LIFETIME_ENTRY_LIMIT_REACHED
    if account.lifetime_approved_risk + approved_max_loss > policy.maximum_lifetime_risk:
        return OpportunityReason.LIFETIME_RISK_LIMIT_REACHED
    # Re-entry on the same event key within a trading day is allowed; the lifetime
    # entry count, lifetime risk, and per-position risk caps above still bind.
    position_risk_cap = min(
        policy.maximum_position_loss,
        account.clean_equity * policy.maximum_equity_risk_fraction,
    )
    if approved_max_loss > position_risk_cap:
        return OpportunityReason.POSITION_RISK_LIMIT_EXCEEDED
    if values.observed_decision_boundary > policy.last_entry_boundary:
        return OpportunityReason.ENTRY_WINDOW_CLOSED
    return None


def _rejected(
    policy_hash: str,
    input_hash: str,
    candidate_hash: str | None,
    values: OpportunityInput,
    reason: OpportunityReason,
    direction: OpportunityDirection | None = None,
) -> OpportunityDecisionRecord:
    return _record(
        policy_hash=policy_hash,
        input_hash=input_hash,
        candidate_hash=candidate_hash,
        values=values,
        outcome=OpportunityOutcome.NO_TRADE,
        reason_codes=(reason,),
        direction=direction,
    )


def _record(
    *,
    policy_hash: str,
    input_hash: str,
    candidate_hash: str | None,
    values: OpportunityInput,
    outcome: OpportunityOutcome,
    reason_codes: tuple[OpportunityReason, ...],
    direction: OpportunityDirection | None = None,
    strategy: VerticalStrategy | None = None,
    quantity: int | None = None,
    approved_max_loss: Decimal | None = None,
) -> OpportunityDecisionRecord:
    material = {
        "outcome": outcome,
        "reason_codes": reason_codes,
        "opportunity_key": values.opportunity_key,
        "decision_boundary": values.observed_decision_boundary,
        "direction": direction,
        "strategy": strategy,
        "quantity": quantity,
        "approved_max_loss": approved_max_loss,
        "book_fingerprint": values.account.book_fingerprint,
        "candidate_hash": candidate_hash,
        "input_hash": input_hash,
        "policy_hash": policy_hash,
    }
    return OpportunityDecisionRecord(
        **material,
        result_hash=_canonical_hash("alphadecay.opportunity.result.v1", material),
    )


_DIRECTION_STRATEGIES = {
    OpportunityDirection.BULLISH: {
        VerticalStrategy.BULL_CALL_DEBIT,
        VerticalStrategy.BULL_PUT_CREDIT,
    },
    OpportunityDirection.BEARISH: {
        VerticalStrategy.BEAR_PUT_DEBIT,
        VerticalStrategy.BEAR_CALL_CREDIT,
    },
}


def _canonical_hash(domain: str, value: object) -> str:
    payload = json.dumps(
        {"domain": domain, "value": _canonical_value(value)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _canonical_value(value: object) -> object:
    if value is TradingHaltState.HALTED:
        return True
    if value is TradingHaltState.NOT_HALTED:
        return False
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("NONFINITE_DECIMAL")
        fixed = format(value, "f")
        if "." in fixed:
            fixed = fixed.rstrip("0").rstrip(".")
        return "0" if fixed in {"-0", "+0"} else fixed
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("NAIVE_DATETIME")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {
            "days": value.days,
            "seconds": value.seconds,
            "microseconds": value.microseconds,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
            if not (field.name == "maximum_limit" and getattr(value, field.name) is None)
        }
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    return value
