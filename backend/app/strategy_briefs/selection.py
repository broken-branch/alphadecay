from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Literal

from backend.app.alpaca.opportunity import (
    OpportunityMarketSnapshot,
    OpportunitySnapshotRequest,
    opportunity_account_book_digest,
    opportunity_bar_digest,
    opportunity_market_session_digest,
    opportunity_market_snapshot_digest,
    opportunity_option_digest,
    opportunity_snapshot_request_digest,
)
from backend.app.contracts.v1 import AccountRole, DataQuality, OptionRight, PositionIntent
from backend.app.domain.option_contract_symbol import (
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)
from backend.app.policy.opportunity import (
    InstrumentKind,
    OptionFeed,
    OptionLeg,
    VerticalCandidate,
    VerticalStrategy,
)
from backend.app.services.opportunity_selection import (
    CandidateSelectionAuthority,
    GreekUnitConvention,
)

from .decision import PositionPhase, ProtocolDecisionClassification
from .models import CuratedStructure
from .protocol import CompiledStrategyProtocol, verify_compiled_protocol
from .tick import CompiledExperimentTick


class CompiledCandidateSelectionBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CompiledCandidateReason(StrEnum):
    SELECTED = "SELECTED"
    NO_ELIGIBLE_CANDIDATE = "NO_ELIGIBLE_CANDIDATE"
    RISK_AUTHORITY_INSUFFICIENT = "RISK_AUTHORITY_INSUFFICIENT"
    UNRESOLVED_TIE = "UNRESOLVED_TIE"


@dataclass(frozen=True)
class CompiledSelectionBasis:
    order: tuple[str, ...]
    target_dte_distance: int
    worst_leg_relative_spread_percent: Decimal
    quote_skew_seconds: Decimal
    debit_per_share: Decimal
    bought_strike: Decimal
    sold_strike: Decimal
    bought_symbol: str
    sold_symbol: str


@dataclass(frozen=True)
class CompiledCandidateSelection:
    status: Literal["CANDIDATE_REVIEW"]
    authority_state: Literal["NON_AUTHORITATIVE"]
    arm_state: Literal["NOT_ARMED"]
    automation_state: Literal["OFF"]
    execution_eligible: Literal[False]
    protocol_hash: str
    protocol_source_hash: str
    tick_protocol_hash: str
    snapshot_hash: str
    account_fingerprint: str
    reason: CompiledCandidateReason
    candidate: VerticalCandidate | None
    selection_basis: CompiledSelectionBasis | None
    leg_source_hashes: tuple[str, ...]


@dataclass(frozen=True)
class _Eligible:
    candidate: VerticalCandidate
    basis: CompiledSelectionBasis
    leg_hashes: tuple[str, str]

    @property
    def rank(self) -> tuple[object, ...]:
        basis = self.basis
        return (
            basis.target_dte_distance,
            basis.worst_leg_relative_spread_percent,
            basis.quote_skew_seconds,
            basis.debit_per_share,
            basis.bought_strike,
            basis.sold_strike,
            basis.bought_symbol,
            basis.sold_symbol,
        )


_ORDER = (
    "TARGET_DTE_DISTANCE",
    "WORST_LEG_RELATIVE_SPREAD_PERCENT",
    "QUOTE_SKEW_SECONDS",
    "DEBIT_PER_SHARE",
    "BOUGHT_STRIKE",
    "SOLD_STRIKE",
    "BOUGHT_SYMBOL",
    "SOLD_SYMBOL",
)


def select_compiled_vertical_candidate(
    protocol: CompiledStrategyProtocol,
    tick: CompiledExperimentTick,
    snapshot: OpportunityMarketSnapshot,
    authority: CandidateSelectionAuthority,
) -> CompiledCandidateSelection:
    _validate_inputs(protocol, tick, snapshot, authority)
    selection = protocol.definition.selection
    eligible: list[_Eligible] = []
    geometry_seen = False
    for first_index, first in enumerate(snapshot.options):
        for second in snapshot.options[first_index + 1 :]:
            structure = _structure(protocol, first, second)
            if structure is None:
                continue
            bought, sold, strategy = structure
            if not _geometry_eligible(protocol, snapshot, bought, sold):
                continue
            debit = bought.ask - sold.bid
            total_risk = debit * selection.quantity * Decimal(100)
            if (
                debit <= 0
                or debit > selection.maximum_debit_per_share
                or total_risk > selection.maximum_loss_dollars
            ):
                continue
            geometry_seen = True
            if not _risk_available(protocol, authority, total_risk):
                continue
            dte = (bought.expiry - protocol.definition.schedule.decision_boundary.date()).days
            bought_spread = _relative_spread_percent(bought)
            sold_spread = _relative_spread_percent(sold)
            skew = Decimal(str(abs((bought.quote_at - sold.quote_at).total_seconds())))
            worst_spread = max(bought_spread, sold_spread)
            score = int(
                (
                    Decimal(100)
                    * (
                        protocol.definition.market_quality.maximum_relative_spread_percent
                        - worst_spread
                    )
                    / protocol.definition.market_quality.maximum_relative_spread_percent
                ).to_integral_value(rounding=ROUND_FLOOR)
            )
            basis = CompiledSelectionBasis(
                order=_ORDER,
                target_dte_distance=abs(dte - selection.target_dte),
                worst_leg_relative_spread_percent=worst_spread,
                quote_skew_seconds=skew,
                debit_per_share=debit,
                bought_strike=bought.strike,
                sold_strike=sold.strike,
                bought_symbol=bought.symbol,
                sold_symbol=sold.symbol,
            )
            eligible.append(
                _Eligible(
                    candidate=VerticalCandidate(
                        strategy=strategy,
                        legs=(
                            _leg(bought, PositionIntent.BUY_TO_OPEN, authority),
                            _leg(sold, PositionIntent.SELL_TO_OPEN, authority),
                        ),
                        quantity=selection.quantity,
                        dte=dte,
                        approved_limit=debit,
                        candidate_score=score,
                        selection_rank=1,
                        buying_power_sufficient=True,
                    ),
                    basis=basis,
                    leg_hashes=(bought.source_hash, sold.source_hash),
                )
            )
    if not eligible:
        reason = (
            CompiledCandidateReason.RISK_AUTHORITY_INSUFFICIENT
            if geometry_seen
            else CompiledCandidateReason.NO_ELIGIBLE_CANDIDATE
        )
        return _result(protocol, tick, snapshot, authority, reason)
    eligible.sort(key=lambda item: item.rank)
    if len(eligible) > 1 and eligible[0].rank == eligible[1].rank:
        return _result(
            protocol,
            tick,
            snapshot,
            authority,
            CompiledCandidateReason.UNRESOLVED_TIE,
        )
    chosen = eligible[0]
    return _result(
        protocol,
        tick,
        snapshot,
        authority,
        CompiledCandidateReason.SELECTED,
        chosen,
    )


def _validate_inputs(protocol, tick, snapshot, authority) -> None:
    if type(protocol) is not CompiledStrategyProtocol or not verify_compiled_protocol(protocol):
        raise CompiledCandidateSelectionBlocked("COMPILED_SELECTION_PROTOCOL_INVALID")
    if (
        type(tick) is not CompiledExperimentTick
        or tick.protocol_hash != protocol.protocol_hash
        or tick.protocol_source_hash != protocol.source_hash
        or tick.position_phase is not PositionPhase.FLAT
        or tick.decision.classification is not ProtocolDecisionClassification.ENTRY_CANDIDATE
    ):
        raise CompiledCandidateSelectionBlocked("COMPILED_SELECTION_TICK_INVALID")
    if type(snapshot) is not OpportunityMarketSnapshot:
        raise CompiledCandidateSelectionBlocked("COMPILED_SELECTION_SNAPSHOT_INVALID")
    schedule = protocol.definition.schedule
    if (
        snapshot.trusted_at != tick.observed_at
        or snapshot.session.session_date != tick.session_date
        or snapshot.underlying_bar.symbol != protocol.symbol
        or snapshot.underlying_bar.completed_at != schedule.decision_boundary
        or snapshot.benchmark_bar.symbol != protocol.definition.benchmark_symbol
        or snapshot.benchmark_bar.completed_at != schedule.decision_boundary
        or not snapshot.session.market_open
        or not schedule.entry_window_start <= snapshot.trusted_at < schedule.entry_window_end
    ):
        raise CompiledCandidateSelectionBlocked("COMPILED_SELECTION_BINDING_MISMATCH")
    if len(snapshot.options) > protocol.definition.selection.maximum_contracts_considered:
        raise CompiledCandidateSelectionBlocked("COMPILED_SELECTION_CONTRACT_CAP_EXCEEDED")
    _validate_authority(snapshot, authority)
    _validate_snapshot_hashes(protocol, snapshot, authority)


def _validate_snapshot_hashes(protocol, snapshot, authority) -> None:
    selection = protocol.definition.selection
    quality = protocol.definition.market_quality
    request = OpportunitySnapshotRequest(
        expected_account_fingerprint=authority.account_fingerprint,
        underlying=protocol.symbol,
        benchmark=protocol.definition.benchmark_symbol,
        decision_boundary=protocol.definition.schedule.decision_boundary,
        minimum_expiry=selection.minimum_expiry,
        maximum_expiry=selection.maximum_expiry,
        minimum_strike=selection.minimum_strike,
        maximum_strike=selection.maximum_strike,
        account_role=snapshot.account_book.account.role,
        maximum_contracts=selection.maximum_contracts_considered,
        maximum_quote_age=timedelta(seconds=quality.maximum_option_quote_age_seconds),
        maximum_quote_skew=timedelta(seconds=quality.maximum_leg_quote_skew_seconds),
    )
    if (
        snapshot.request_hash != opportunity_snapshot_request_digest(request)
        or snapshot.source_hash != opportunity_market_snapshot_digest(snapshot)
        or snapshot.account_book.source_hash
        != opportunity_account_book_digest(snapshot.account_book)
        or snapshot.session.source_hash != opportunity_market_session_digest(snapshot.session)
        or snapshot.underlying_bar.source_hash != opportunity_bar_digest(snapshot.underlying_bar)
        or snapshot.benchmark_bar.source_hash != opportunity_bar_digest(snapshot.benchmark_bar)
        or any(item.source_hash != opportunity_option_digest(item) for item in snapshot.options)
    ):
        raise CompiledCandidateSelectionBlocked("COMPILED_SELECTION_SNAPSHOT_INVALID")


def _validate_authority(snapshot, authority) -> None:
    if type(authority) is not CandidateSelectionAuthority:
        raise CompiledCandidateSelectionBlocked("COMPILED_SELECTION_ACCOUNT_INVALID")
    account = snapshot.account_book.account
    amounts = (
        authority.observed_equity,
        authority.observed_buying_power,
        authority.available_risk,
        authority.available_buying_power,
    )
    if (
        account.role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}
        or not account.paper
        or account.baseline_status is not DataQuality.COMPLETE
        or snapshot.account_book.positions.positions
        or snapshot.account_book.open_orders
        or authority.snapshot_request_hash != snapshot.request_hash
        or authority.snapshot_source_hash != snapshot.source_hash
        or authority.account_fingerprint != snapshot.account_book.account_fingerprint
        or authority.observed_equity != account.equity
        or authority.observed_buying_power != account.buying_power
        or authority.available_buying_power > account.buying_power
        or any(type(item) is not Decimal or not item.is_finite() or item < 0 for item in amounts)
        or authority.greek_unit_convention is not GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1
        or not _hash(authority.greek_unit_evidence_hash)
    ):
        raise CompiledCandidateSelectionBlocked("COMPILED_SELECTION_ACCOUNT_INVALID")


def _structure(protocol, first, second):
    if (
        first.underlying != second.underlying
        or first.expiry != second.expiry
        or first.right != second.right
        or first.strike == second.strike
    ):
        return None
    lower, higher = sorted((first, second), key=lambda item: item.strike)
    if protocol.structure is CuratedStructure.BULL_CALL_DEBIT_SPREAD and first.right == "C":
        return lower, higher, VerticalStrategy.BULL_CALL_DEBIT
    if protocol.structure is CuratedStructure.BEAR_PUT_DEBIT_SPREAD and first.right == "P":
        return higher, lower, VerticalStrategy.BEAR_PUT_DEBIT
    return None


def _geometry_eligible(protocol, snapshot, bought, sold) -> bool:
    selection = protocol.definition.selection
    quality = protocol.definition.market_quality
    dte = (bought.expiry - protocol.definition.schedule.decision_boundary.date()).days
    return (
        selection.minimum_expiry <= bought.expiry <= selection.maximum_expiry
        and selection.minimum_dte <= dte <= selection.maximum_dte
        and selection.minimum_strike <= bought.strike <= selection.maximum_strike
        and selection.minimum_strike <= sold.strike <= selection.maximum_strike
        and abs(bought.strike - sold.strike) == selection.width_dollars
        and _option_eligible(protocol, snapshot, bought)
        and _option_eligible(protocol, snapshot, sold)
        and abs((bought.quote_at - sold.quote_at).total_seconds())
        <= quality.maximum_leg_quote_skew_seconds
    )


def _option_eligible(protocol, snapshot, option) -> bool:
    quality = protocol.definition.market_quality
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
    try:
        parsed = parse_standard_option_contract_symbol(
            option.symbol, underlying_symbol=protocol.symbol
        )
    except OptionContractSymbolError:
        return False
    return (
        option.underlying == protocol.symbol
        and parsed.expiration_date == option.expiry
        and parsed.right == option.right
        and parsed.strike_price == option.strike
        and all(type(item) is Decimal and item.is_finite() for item in decimals)
        and 0 < option.bid <= option.ask
        and option.bid_size >= quality.minimum_leg_bid_size
        and option.ask_size >= quality.minimum_leg_ask_size
        and option.quote_at.tzinfo is not None
        and option.quote_at.utcoffset() is not None
        and option.retrieved_at.tzinfo is not None
        and option.quote_at <= option.retrieved_at <= snapshot.trusted_at
        and timedelta(0)
        <= snapshot.trusted_at - option.quote_at
        <= timedelta(seconds=quality.maximum_option_quote_age_seconds)
        and _relative_spread_percent(option) <= quality.maximum_relative_spread_percent
    )


def _risk_available(protocol, authority, total_risk) -> bool:
    limit = protocol.definition.maximum_account_risk_percent
    return (
        total_risk <= authority.available_risk
        and total_risk <= authority.available_buying_power
        and (
            limit is None or total_risk <= authority.observed_equity * Decimal(limit) / Decimal(100)
        )
    )


def _leg(option, intent, authority) -> OptionLeg:
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
        quote_at=option.quote_at.astimezone(UTC),
        feed=OptionFeed.INDICATIVE_MODIFIED,
        greeks_complete=True,
        greek_units_verified=(
            authority.greek_unit_convention is GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1
            and _hash(authority.greek_unit_evidence_hash)
        ),
    )


def _relative_spread_percent(option) -> Decimal:
    return Decimal(100) * (option.ask - option.bid) / ((option.ask + option.bid) / Decimal(2))


def _result(protocol, tick, snapshot, authority, reason, chosen=None):
    return CompiledCandidateSelection(
        status="CANDIDATE_REVIEW",
        authority_state="NON_AUTHORITATIVE",
        arm_state="NOT_ARMED",
        automation_state="OFF",
        execution_eligible=False,
        protocol_hash=protocol.protocol_hash,
        protocol_source_hash=protocol.source_hash,
        tick_protocol_hash=tick.protocol_hash,
        snapshot_hash=snapshot.source_hash,
        account_fingerprint=authority.account_fingerprint,
        reason=reason,
        candidate=None if chosen is None else chosen.candidate,
        selection_basis=None if chosen is None else chosen.basis,
        leg_source_hashes=() if chosen is None else chosen.leg_hashes,
    )


def _hash(value: str) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )
