from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from backend.app.alpaca.opportunity import (
    OpportunityAccountBook,
    OpportunityBar,
    OpportunityMarketSession,
    OpportunityMarketSnapshot,
    OpportunityOption,
)
from backend.app.contracts.v1 import AccountRole, OptionRight, PositionIntent
from backend.app.domain.option_contract_symbol import (
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)
from backend.app.policy.opportunity import (
    InstrumentKind,
    OpportunityDirection,
    OpportunityPolicy,
    OptionFeed,
    OptionLeg,
    VerticalCandidate,
    VerticalStrategy,
)


class SelectionReason(StrEnum):
    SELECTED = "SELECTED"
    INPUT_INVALID = "INPUT_INVALID"
    SNAPSHOT_INVALID = "SNAPSHOT_INVALID"
    NO_ELIGIBLE_STRUCTURE = "NO_ELIGIBLE_STRUCTURE"
    RISK_AUTHORITY_INSUFFICIENT = "RISK_AUTHORITY_INSUFFICIENT"


class GreekUnitConvention(StrEnum):
    ALPACA_GOPRICEOPTIONS_RAW_V1 = "ALPACA_GOPRICEOPTIONS_RAW_V1"


@dataclass(frozen=True)
class CandidateSelectionAuthority:
    snapshot_request_hash: str
    snapshot_source_hash: str
    account_fingerprint: str
    observed_equity: Decimal
    observed_buying_power: Decimal
    available_risk: Decimal
    available_buying_power: Decimal
    greek_unit_convention: GreekUnitConvention
    greek_unit_evidence_hash: str


@dataclass(frozen=True)
class CandidateSelectionResult:
    candidate: VerticalCandidate | None
    reason: SelectionReason


@dataclass(frozen=True)
class _RankedCandidate:
    candidate: VerticalCandidate
    risk_per_contract: Decimal


def select_vertical_candidate(
    snapshot: OpportunityMarketSnapshot,
    policy: OpportunityPolicy,
    direction: OpportunityDirection,
    requested_maximum_quantity: int,
    authority: CandidateSelectionAuthority,
) -> CandidateSelectionResult:
    if not _inputs_valid(policy, direction, requested_maximum_quantity, authority):
        return CandidateSelectionResult(None, SelectionReason.INPUT_INVALID)
    if not _snapshot_valid(snapshot, policy):
        return CandidateSelectionResult(None, SelectionReason.SNAPSHOT_INVALID)
    if (
        authority.snapshot_request_hash != snapshot.request_hash
        or authority.snapshot_source_hash != snapshot.source_hash
        or authority.account_fingerprint != snapshot.account_book.account_fingerprint
        or authority.observed_equity != snapshot.account_book.account.equity
        or authority.observed_buying_power != snapshot.account_book.account.buying_power
        or authority.available_buying_power > snapshot.account_book.account.buying_power
    ):
        return CandidateSelectionResult(None, SelectionReason.INPUT_INVALID)

    ranked: list[_RankedCandidate] = []
    had_eligible_geometry = False
    for first_index, first in enumerate(snapshot.options):
        for second in snapshot.options[first_index + 1 :]:
            structure = _build_structure(first, second, direction)
            if structure is None:
                continue
            strategy, bought, sold = structure
            terms = _candidate_terms(policy, bought, sold, strategy)
            if terms is None:
                continue
            approved_limit, risk_per_contract, score = terms
            if score < policy.minimum_candidate_score:
                continue
            had_eligible_geometry = True
            quantity = _quantity(
                policy,
                requested_maximum_quantity,
                authority,
                risk_per_contract,
            )
            if quantity < 1:
                continue
            ranked.append(
                _RankedCandidate(
                    VerticalCandidate(
                        strategy=strategy,
                        legs=(
                            _option_leg(bought, PositionIntent.BUY_TO_OPEN, authority),
                            _option_leg(sold, PositionIntent.SELL_TO_OPEN, authority),
                        ),
                        quantity=quantity,
                        dte=(bought.expiry - policy.selected_decision_boundary.date()).days,
                        approved_limit=approved_limit,
                        candidate_score=score,
                        selection_rank=1,
                        buying_power_sufficient=True,
                    ),
                    risk_per_contract,
                )
            )

    if not ranked:
        reason = (
            SelectionReason.RISK_AUTHORITY_INSUFFICIENT
            if had_eligible_geometry
            else SelectionReason.NO_ELIGIBLE_STRUCTURE
        )
        return CandidateSelectionResult(None, reason)

    ranked.sort(key=_rank_key)
    if len(ranked) > 1 and _rank_key(ranked[0]) == _rank_key(ranked[1]):
        return CandidateSelectionResult(None, SelectionReason.SNAPSHOT_INVALID)
    return CandidateSelectionResult(ranked[0].candidate, SelectionReason.SELECTED)


def _inputs_valid(
    policy: OpportunityPolicy,
    direction: OpportunityDirection,
    requested_maximum_quantity: int,
    authority: CandidateSelectionAuthority,
) -> bool:
    if type(authority) is not CandidateSelectionAuthority:
        return False
    amounts = (
        authority.observed_equity,
        authority.observed_buying_power,
        authority.available_risk,
        authority.available_buying_power,
    )
    return (
        type(policy) is OpportunityPolicy
        and type(direction) is OpportunityDirection
        and type(requested_maximum_quantity) is int
        and requested_maximum_quantity > 0
        and all(type(value) is Decimal for value in amounts)
        and all(value.is_finite() and value >= 0 for value in amounts)
        and authority.observed_equity > 0
        and authority.greek_unit_convention is GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1
        and all(
            _digest(value)
            for value in (
                authority.snapshot_request_hash,
                authority.snapshot_source_hash,
                authority.account_fingerprint,
                authority.greek_unit_evidence_hash,
            )
        )
    )


def _snapshot_valid(snapshot: OpportunityMarketSnapshot, policy: OpportunityPolicy) -> bool:
    if (
        type(snapshot) is not OpportunityMarketSnapshot
        or type(snapshot.account_book) is not OpportunityAccountBook
        or type(snapshot.session) is not OpportunityMarketSession
        or type(snapshot.underlying_bar) is not OpportunityBar
        or type(snapshot.benchmark_bar) is not OpportunityBar
        or not _snapshot_times_valid(snapshot)
        or snapshot.underlying_bar.symbol != policy.underlying
        or snapshot.underlying_bar.completed_at != policy.selected_decision_boundary
        or snapshot.benchmark_bar.symbol == policy.underlying
        or snapshot.benchmark_bar.completed_at != policy.selected_decision_boundary
        or snapshot.underlying_bar.started_at
        != policy.selected_decision_boundary - timedelta(minutes=5)
        or snapshot.benchmark_bar.started_at
        != policy.selected_decision_boundary - timedelta(minutes=5)
        or snapshot.trusted_at < policy.selected_decision_boundary
        or snapshot.trusted_at - policy.selected_decision_boundary > policy.maximum_decision_delay
        or not snapshot.session.market_open
        or snapshot.session.clock_at > snapshot.trusted_at
        or not snapshot.session.open_at <= snapshot.session.clock_at < snapshot.session.close_at
        or not snapshot.session.open_at
        < policy.selected_decision_boundary
        <= snapshot.session.close_at
        or snapshot.session.next_open_at <= snapshot.session.clock_at
        or snapshot.session.next_close_at <= snapshot.session.clock_at
        or snapshot.account_book.account.role
        not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}
        or not snapshot.account_book.account.paper
        or snapshot.account_book.positions.positions
        or snapshot.account_book.open_orders
        or not _snapshot_hashes_valid(snapshot)
        or not snapshot.options
    ):
        return False
    identities: set[tuple[object, ...]] = set()
    symbols: set[str] = set()
    for option in snapshot.options:
        if type(option) is not OpportunityOption:
            return False
        identity = (option.underlying, option.right, option.expiry, option.strike)
        if (
            option.symbol in symbols
            or identity in identities
            or not _option_valid(option, snapshot, policy)
        ):
            return False
        symbols.add(option.symbol)
        identities.add(identity)
    quote_times = tuple(option.quote_at for option in snapshot.options)
    return max(quote_times) - min(quote_times) <= policy.maximum_leg_quote_skew


def _option_valid(
    option: OpportunityOption,
    snapshot: OpportunityMarketSnapshot,
    policy: OpportunityPolicy,
) -> bool:
    decimals = (
        option.strike,
        option.bid,
        option.ask,
        option.implied_volatility,
        option.delta,
        option.gamma,
        option.theta_per_day,
        option.vega_per_iv_point,
    )
    if (
        not all(type(value) is Decimal for value in decimals)
        or type(option.bid_size) is not int
        or type(option.ask_size) is not int
        or not _strict_utc(option.quote_at)
        or not _strict_utc(option.retrieved_at)
    ):
        return False
    age = snapshot.trusted_at - option.quote_at
    return (
        option.underlying == policy.underlying
        and option.right in {"C", "P"}
        and _occ_matches(option)
        and option.strike > 0
        and all(value.is_finite() for value in decimals)
        and 0 < option.bid <= option.ask
        and option.bid_size > 0
        and option.ask_size > 0
        and option.implied_volatility > 0
        and Decimal("-1") <= option.delta <= Decimal("1")
        and Decimal(0) <= option.gamma <= Decimal(10)
        and abs(option.theta_per_day) <= Decimal(1000)
        and Decimal(0) <= option.vega_per_iv_point <= Decimal(1000)
        and option.quote_at <= option.retrieved_at <= snapshot.trusted_at
        and timedelta(0) <= age <= policy.maximum_option_quote_age
        and policy.minimum_dte
        <= (option.expiry - policy.selected_decision_boundary.date()).days
        <= policy.maximum_dte
        and _relative_spread(option) <= policy.maximum_relative_spread
        and _digest(option.source_hash)
    )


def _build_structure(
    first: OpportunityOption,
    second: OpportunityOption,
    direction: OpportunityDirection,
) -> tuple[VerticalStrategy, OpportunityOption, OpportunityOption] | None:
    if (
        first.underlying != second.underlying
        or first.right != second.right
        or first.expiry != second.expiry
        or first.strike == second.strike
    ):
        return None
    lower, higher = sorted((first, second), key=lambda option: option.strike)
    if direction is OpportunityDirection.BULLISH:
        if first.right == "C":
            return VerticalStrategy.BULL_CALL_DEBIT, lower, higher
        return VerticalStrategy.BULL_PUT_CREDIT, lower, higher
    if first.right == "P":
        return VerticalStrategy.BEAR_PUT_DEBIT, higher, lower
    return VerticalStrategy.BEAR_CALL_CREDIT, higher, lower


def _candidate_terms(
    policy: OpportunityPolicy,
    bought: OpportunityOption,
    sold: OpportunityOption,
    strategy: VerticalStrategy,
) -> tuple[Decimal, Decimal, int] | None:
    if abs(bought.quote_at - sold.quote_at) > policy.maximum_leg_quote_skew:
        return None
    width = abs(bought.strike - sold.strike)
    debit = strategy in {VerticalStrategy.BULL_CALL_DEBIT, VerticalStrategy.BEAR_PUT_DEBIT}
    natural = bought.ask - sold.bid if debit else sold.bid - bought.ask
    if natural <= 0 or width <= 0:
        return None
    fraction = natural / width
    if debit:
        if not (
            policy.minimum_debit_width_fraction <= fraction <= policy.maximum_debit_width_fraction
        ):
            return None
        risk = natural * 100
    else:
        if not policy.minimum_credit_width_fraction <= fraction < 1:
            return None
        risk = (width - natural) * 100
    if risk <= 0:
        return None
    worst_spread = max(_relative_spread(bought), _relative_spread(sold))
    score = int(
        (
            Decimal(100)
            * (policy.maximum_relative_spread - worst_spread)
            / policy.maximum_relative_spread
        ).to_integral_value(rounding=ROUND_FLOOR)
    )
    return natural, risk, score


def _quantity(
    policy: OpportunityPolicy,
    requested: int,
    authority: CandidateSelectionAuthority,
    risk_per_contract: Decimal,
) -> int:
    position_cap = min(
        policy.maximum_position_loss,
        authority.observed_equity * policy.maximum_equity_risk_fraction,
        authority.available_risk,
        authority.available_buying_power,
    )
    affordable = int((position_cap / risk_per_contract).to_integral_value(rounding=ROUND_FLOOR))
    return min(requested, policy.maximum_quantity, affordable)


def _option_leg(
    option: OpportunityOption,
    intent: PositionIntent,
    authority: CandidateSelectionAuthority,
) -> OptionLeg:
    return OptionLeg(
        instrument_kind=InstrumentKind.OPTION,
        symbol=option.symbol,
        underlying=option.underlying,
        right=OptionRight.CALL if option.right == "C" else OptionRight.PUT,
        strike=option.strike,
        expiry=option.expiry,
        intent=intent,
        ratio=1,
        multiplier=100,
        active=True,
        tradable=True,
        bid=option.bid,
        ask=option.ask,
        bid_size=option.bid_size,
        ask_size=option.ask_size,
        quote_at=option.quote_at,
        feed=OptionFeed.INDICATIVE_MODIFIED,
        greeks_complete=True,
        greek_units_verified=(
            authority.greek_unit_convention is GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1
            and _digest(authority.greek_unit_evidence_hash)
        ),
    )


def _relative_spread(option: OpportunityOption) -> Decimal:
    return (option.ask - option.bid) / ((option.ask + option.bid) / 2)


def _rank_key(item: _RankedCandidate) -> tuple[object, ...]:
    candidate = item.candidate
    bought, sold = candidate.legs
    return (
        -candidate.candidate_score,
        item.risk_per_contract,
        candidate.strategy.value,
        bought.expiry,
        bought.strike,
        sold.strike,
        bought.symbol,
        sold.symbol,
    )


def _digest(value: str) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _occ_matches(option: OpportunityOption) -> bool:
    try:
        parsed = parse_standard_option_contract_symbol(
            option.symbol,
            underlying_symbol=option.underlying,
        )
    except OptionContractSymbolError:
        return False
    return (
        parsed.expiration_date == option.expiry
        and parsed.right == option.right
        and parsed.strike_price == option.strike
    )


def _snapshot_hashes_valid(snapshot: OpportunityMarketSnapshot) -> bool:
    if not all(
        _digest(value)
        for value in (
            snapshot.request_hash,
            snapshot.source_hash,
            snapshot.account_book.account_fingerprint,
            snapshot.account_book.source_hash,
            snapshot.session.source_hash,
            snapshot.underlying_bar.source_hash,
            snapshot.benchmark_bar.source_hash,
            *(option.source_hash for option in snapshot.options),
        )
    ):
        return False
    if snapshot.account_book.source_hash != _canonical_hash(
        {
            "account": snapshot.account_book.account.model_dump(mode="json"),
            "positions": snapshot.account_book.positions.model_dump(mode="json"),
            "orders": snapshot.account_book.open_orders,
            "account_fingerprint": snapshot.account_book.account_fingerprint,
        }
    ):
        return False
    session = snapshot.session
    if session.source_hash != _canonical_hash(
        {
            "date": session.session_date.isoformat(),
            "open": session.open_at.isoformat(),
            "close": session.close_at.isoformat(),
            "clock": session.clock_at.isoformat(),
            "market_open": session.market_open,
            "next_open": session.next_open_at.isoformat(),
            "next_close": session.next_close_at.isoformat(),
        }
    ):
        return False
    if any(
        bar.source_hash
        != _canonical_hash(
            {
                "symbol": bar.symbol,
                "started_at": bar.started_at.isoformat(),
                "completed_at": bar.completed_at.isoformat(),
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume),
                "vwap": str(bar.vwap),
            }
        )
        for bar in (snapshot.underlying_bar, snapshot.benchmark_bar)
    ):
        return False
    if any(
        option.source_hash
        != _canonical_hash(
            {
                "symbol": option.symbol,
                "expiry": option.expiry.isoformat(),
                "right": option.right,
                "strike": str(option.strike),
                "bid": str(option.bid),
                "ask": str(option.ask),
                "bid_size": option.bid_size,
                "ask_size": option.ask_size,
                "quote_at": option.quote_at.isoformat(),
                "retrieved_at": option.retrieved_at.isoformat(),
                "implied_volatility": str(option.implied_volatility),
                "delta": str(option.delta),
                "gamma": str(option.gamma),
                "theta": str(option.theta_per_day),
                "vega": str(option.vega_per_iv_point),
            }
        )
        for option in snapshot.options
    ):
        return False
    return snapshot.source_hash == _canonical_hash(
        {
            "request": snapshot.request_hash,
            "book": snapshot.account_book.source_hash,
            "session": snapshot.session.source_hash,
            "underlying_bar": snapshot.underlying_bar.source_hash,
            "benchmark_bar": snapshot.benchmark_bar.source_hash,
            "options": [option.source_hash for option in snapshot.options],
        }
    )


def _canonical_hash(value: object) -> str | None:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_times_valid(snapshot: OpportunityMarketSnapshot) -> bool:
    values = (
        snapshot.trusted_at,
        snapshot.session.open_at,
        snapshot.session.close_at,
        snapshot.session.clock_at,
        snapshot.session.next_open_at,
        snapshot.session.next_close_at,
        snapshot.underlying_bar.started_at,
        snapshot.underlying_bar.completed_at,
        snapshot.benchmark_bar.started_at,
        snapshot.benchmark_bar.completed_at,
    )
    return all(_strict_utc(value) for value in values)


def _strict_utc(value: object) -> bool:
    return (
        type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0)
    )
