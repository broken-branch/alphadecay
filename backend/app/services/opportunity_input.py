from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

from backend.app.alpaca.opportunity import (
    OpportunityAccountBook,
    OpportunityBar,
    OpportunityMarketSession,
    OpportunityMarketSnapshot,
    OpportunityOption,
    OpportunitySnapshotRequest,
    opportunity_account_book_digest,
    opportunity_bar_digest,
    opportunity_market_session_digest,
    opportunity_market_snapshot_digest,
    opportunity_option_digest,
    opportunity_snapshot_request_digest,
)
from backend.app.contracts.v1 import AccountRole, DataQuality
from backend.app.policy import (
    AccountOpportunityState,
    CatalystQuality,
    OpportunityDirection,
    OpportunityInput,
    OpportunityOutcome,
    OpportunityPolicy,
    VerticalCandidate,
)
from backend.app.policy.opportunity import (
    TradingHaltState,
    derive_opportunity_direction,
    opportunity_policy_hash,
)
from backend.app.services.opportunity_selection import (
    CandidateSelectionAuthority,
    CandidateSelectionResult,
    SelectionReason,
    select_vertical_candidate,
)

_HASH = re.compile(r"[0-9a-f]{64}")
_BAR_DURATION = timedelta(minutes=5)


class OpportunityInputAuthorityError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DecimalSignalAuthority:
    value: Decimal
    observed_at: datetime
    source_hash: str


@dataclass(frozen=True)
class TrendSignalAuthority:
    bull_hits: int
    bear_hits: int
    observed_at: datetime
    source_hash: str


@dataclass(frozen=True)
class OpportunitySignalAuthority:
    snapshot_source_hash: str
    calculation_source_hash: str
    beta: DecimalSignalAuthority
    vwap_distance: DecimalSignalAuthority
    relative_return: DecimalSignalAuthority
    trend: TrendSignalAuthority
    absolute_first_reaction: DecimalSignalAuthority
    trading_halt_state: TradingHaltState
    trading_status_observed_at: datetime
    trading_status_source_hash: str


@dataclass(frozen=True)
class CatalystAuthority:
    opportunity_key: str
    quality: CatalystQuality
    score: int
    observed_at: datetime
    source_hash: str


@dataclass(frozen=True)
class AccountBudgetAuthority:
    account_role: AccountRole
    account_fingerprint: str
    snapshot_book_source_hash: str
    observed_at: datetime
    baseline_clean: bool
    baseline_source_hash: str
    book_fingerprint: str
    book_source_hash: str
    clean_equity: Decimal
    open_position_count: int
    open_order_count: int
    filled_entry_count: int
    lifetime_approved_risk: Decimal
    entry_reservation_active: bool
    reserved_approved_risk: Decimal
    event_already_attempted: bool
    history_source_hash: str


@dataclass(frozen=True)
class PriorDecisionAuthority:
    opportunity_key: str
    decision_boundary: datetime
    outcome: OpportunityOutcome | None
    observed_at: datetime
    source_hash: str


@dataclass(frozen=True)
class OpportunityInputAssembly:
    values: OpportunityInput
    policy_hash: str
    authority_hash: str


def assemble_opportunity_input(
    *,
    request: OpportunitySnapshotRequest,
    snapshot: OpportunityMarketSnapshot,
    policy: OpportunityPolicy,
    requested_maximum_quantity: int,
    selection_authority: CandidateSelectionAuthority | None,
    selection: CandidateSelectionResult | None,
    signals: OpportunitySignalAuthority,
    catalyst: CatalystAuthority,
    account: AccountBudgetAuthority,
    prior_decision: PriorDecisionAuthority,
) -> OpportunityInputAssembly:
    _validate_snapshot(request, snapshot, policy)
    _validate_signals(signals, snapshot, policy)
    _validate_catalyst(catalyst, snapshot, policy)
    _validate_account(account, snapshot)
    _validate_prior_decision(prior_decision, snapshot, policy)
    direction = derive_opportunity_direction(
        policy,
        vwap_distance=signals.vwap_distance.value,
        relative_return=signals.relative_return.value,
        bull_trend_hits=signals.trend.bull_hits,
        bear_trend_hits=signals.trend.bear_hits,
    )
    candidate = _validate_selection(
        snapshot,
        policy,
        direction,
        requested_maximum_quantity,
        selection_authority,
        selection,
        account,
    )

    values = OpportunityInput(
        opportunity_key=policy.opportunity_key,
        underlying=policy.underlying,
        observed_decision_boundary=policy.selected_decision_boundary,
        evaluated_at=snapshot.trusted_at,
        completed_bar_at=snapshot.underlying_bar.completed_at,
        decision_boundary_complete=True,
        prior_decision_outcome=prior_decision.outcome,
        data_quality=DataQuality.COMPLETE,
        market_open=snapshot.session.market_open,
        trading_halted=signals.trading_halt_state,
        underlying_observed_at=max(
            signals.beta.observed_at,
            signals.vwap_distance.observed_at,
            signals.relative_return.observed_at,
            signals.trend.observed_at,
            signals.absolute_first_reaction.observed_at,
            signals.trading_status_observed_at,
        ),
        catalyst_observed_at=catalyst.observed_at,
        catalyst_quality=catalyst.quality,
        catalyst_score=catalyst.score,
        vwap_distance=signals.vwap_distance.value,
        relative_return=signals.relative_return.value,
        beta=signals.beta.value,
        bull_trend_hits=signals.trend.bull_hits,
        bear_trend_hits=signals.trend.bear_hits,
        absolute_first_reaction=signals.absolute_first_reaction.value,
        candidate=candidate,
        account=AccountOpportunityState(
            account_role=account.account_role,
            book_fingerprint=account.book_fingerprint,
            baseline_clean=account.baseline_clean,
            clean_equity=account.clean_equity,
            open_position_count=account.open_position_count,
            open_order_count=account.open_order_count,
            filled_entry_count=account.filled_entry_count,
            lifetime_approved_risk=account.lifetime_approved_risk,
            entry_reservation_active=account.entry_reservation_active,
            reserved_approved_risk=account.reserved_approved_risk,
            event_already_attempted=account.event_already_attempted,
        ),
    )
    policy_hash = opportunity_policy_hash(policy)
    material = {
        "snapshot_source_hash": snapshot.source_hash,
        "snapshot_request": request,
        "policy_hash": policy_hash,
        "selection_direction": direction,
        "requested_maximum_quantity": requested_maximum_quantity,
        "selection_authority": selection_authority,
        "selection": selection,
        "signals": signals,
        "catalyst": catalyst,
        "account": account,
        "prior_decision": prior_decision,
        "values": values,
    }
    return OpportunityInputAssembly(
        values=values,
        policy_hash=policy_hash,
        authority_hash=_canonical_hash("alphadecay.opportunity.input-authority.v1", material),
    )


def _validate_snapshot(
    request: OpportunitySnapshotRequest,
    snapshot: OpportunityMarketSnapshot,
    policy: OpportunityPolicy,
) -> None:
    if (
        type(policy) is not OpportunityPolicy
        or type(snapshot) is not OpportunityMarketSnapshot
        or type(snapshot.account_book) is not OpportunityAccountBook
        or type(snapshot.session) is not OpportunityMarketSession
        or type(snapshot.underlying_bar) is not OpportunityBar
        or type(snapshot.benchmark_bar) is not OpportunityBar
        or any(type(option) is not OpportunityOption for option in snapshot.options)
    ):
        raise OpportunityInputAuthorityError("SNAPSHOT_INVALID")
    _validate_request(request, snapshot, policy)
    _utc(snapshot.trusted_at, "SNAPSHOT_TIME_INVALID")
    if not _valid_hash(snapshot.request_hash) or not _valid_hash(snapshot.source_hash):
        raise OpportunityInputAuthorityError("SNAPSHOT_HASH_INVALID")
    if (
        snapshot.account_book.account.role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}
        or snapshot.account_book.account.role is not request.account_role
        or snapshot.account_book.account.paper is not True
        or snapshot.underlying_bar.symbol != policy.underlying
        or snapshot.underlying_bar.completed_at != policy.selected_decision_boundary
        or snapshot.benchmark_bar.completed_at != policy.selected_decision_boundary
        or snapshot.underlying_bar.started_at + _BAR_DURATION
        != snapshot.underlying_bar.completed_at
        or snapshot.benchmark_bar.started_at + _BAR_DURATION != snapshot.benchmark_bar.completed_at
        or snapshot.session.session_date
        != policy.selected_decision_boundary.astimezone(ZoneInfo("America/New_York")).date()
        or not snapshot.session.open_at
        < policy.selected_decision_boundary
        <= snapshot.session.close_at
        or snapshot.session.market_open
        != (snapshot.session.open_at <= snapshot.session.clock_at < snapshot.session.close_at)
        or snapshot.session.clock_at > snapshot.trusted_at
        or snapshot.trusted_at - snapshot.session.clock_at > timedelta(minutes=2)
        or policy.selected_decision_boundary > snapshot.trusted_at
        or snapshot.trusted_at - policy.selected_decision_boundary > policy.maximum_decision_delay
        or snapshot.session.next_open_at <= snapshot.session.clock_at
        or snapshot.session.next_close_at <= snapshot.session.clock_at
    ):
        raise OpportunityInputAuthorityError("SNAPSHOT_SCOPE_OR_TIME_MISMATCH")
    if snapshot.underlying_bar.symbol == snapshot.benchmark_bar.symbol:
        raise OpportunityInputAuthorityError("SNAPSHOT_SYMBOL_INVALID")
    _validate_snapshot_hashes(snapshot)


def _validate_request(
    request: OpportunitySnapshotRequest,
    snapshot: OpportunityMarketSnapshot,
    policy: OpportunityPolicy,
) -> None:
    if type(request) is not OpportunitySnapshotRequest:
        raise OpportunityInputAuthorityError("SNAPSHOT_REQUEST_INVALID")
    expected_hash = opportunity_snapshot_request_digest(request)
    if (
        request.account_role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}
        or request.expected_account_fingerprint != snapshot.account_book.account_fingerprint
        or request.underlying != policy.underlying
        or request.underlying != snapshot.underlying_bar.symbol
        or request.benchmark != snapshot.benchmark_bar.symbol
        or request.decision_boundary != policy.selected_decision_boundary
        or not all(
            request.minimum_expiry <= option.expiry <= request.maximum_expiry
            and request.minimum_strike <= option.strike <= request.maximum_strike
            for option in snapshot.options
        )
        or len(snapshot.options) > request.maximum_contracts
        or request.maximum_quote_age > policy.maximum_option_quote_age
        or request.maximum_quote_skew > policy.maximum_leg_quote_skew
        or snapshot.request_hash != expected_hash
    ):
        raise OpportunityInputAuthorityError("SNAPSHOT_REQUEST_MISMATCH")


def _validate_snapshot_hashes(snapshot: OpportunityMarketSnapshot) -> None:
    book = snapshot.account_book
    expected_book = opportunity_account_book_digest(book)
    expected_session = opportunity_market_session_digest(snapshot.session)
    if book.source_hash != expected_book or snapshot.session.source_hash != expected_session:
        raise OpportunityInputAuthorityError("SNAPSHOT_COMPONENT_HASH_MISMATCH")
    for bar in (snapshot.underlying_bar, snapshot.benchmark_bar):
        bar_values = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap)
        if (
            bar.source_hash != opportunity_bar_digest(bar)
            or any(not value.is_finite() or value <= 0 for value in bar_values)
            or bar.low > bar.high
            or not bar.low <= bar.open <= bar.high
            or not bar.low <= bar.close <= bar.high
            or _utc(bar.started_at, "SNAPSHOT_BAR_TIME_INVALID") + _BAR_DURATION
            != _utc(bar.completed_at, "SNAPSHOT_BAR_TIME_INVALID")
        ):
            raise OpportunityInputAuthorityError("SNAPSHOT_COMPONENT_HASH_MISMATCH")
    for option in snapshot.options:
        option_values = (
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
            option.source_hash != opportunity_option_digest(option)
            or option.right not in {"C", "P"}
            or any(not value.is_finite() for value in option_values)
            or min(option.strike, option.bid, option.ask, option.implied_volatility) <= 0
            or option.bid > option.ask
            or option.bid_size <= 0
            or option.ask_size <= 0
            or _utc(option.quote_at, "SNAPSHOT_OPTION_TIME_INVALID")
            > _utc(option.retrieved_at, "SNAPSHOT_OPTION_TIME_INVALID")
            or option.retrieved_at > snapshot.trusted_at
        ):
            raise OpportunityInputAuthorityError("SNAPSHOT_COMPONENT_HASH_MISMATCH")
    expected_snapshot = opportunity_market_snapshot_digest(snapshot)
    if snapshot.source_hash != expected_snapshot:
        raise OpportunityInputAuthorityError("SNAPSHOT_HASH_MISMATCH")


def _validate_signals(
    signals: OpportunitySignalAuthority,
    snapshot: OpportunityMarketSnapshot,
    policy: OpportunityPolicy,
) -> None:
    if (
        type(signals) is not OpportunitySignalAuthority
        or type(signals.beta) is not DecimalSignalAuthority
        or type(signals.vwap_distance) is not DecimalSignalAuthority
        or type(signals.relative_return) is not DecimalSignalAuthority
        or type(signals.trend) is not TrendSignalAuthority
        or type(signals.absolute_first_reaction) is not DecimalSignalAuthority
        or type(signals.trading_halt_state) is not TradingHaltState
    ):
        raise OpportunityInputAuthorityError("SIGNAL_AUTHORITY_INVALID")
    if signals.snapshot_source_hash != snapshot.source_hash or not _valid_hash(
        signals.calculation_source_hash
    ):
        raise OpportunityInputAuthorityError("SIGNAL_SNAPSHOT_MISMATCH")
    decimal_signals = (
        signals.beta,
        signals.vwap_distance,
        signals.relative_return,
        signals.absolute_first_reaction,
    )
    if any(not item.value.is_finite() for item in decimal_signals):
        raise OpportunityInputAuthorityError("SIGNAL_VALUE_INVALID")
    if not 0 <= signals.trend.bull_hits <= 3 or not 0 <= signals.trend.bear_hits <= 3:
        raise OpportunityInputAuthorityError("SIGNAL_VALUE_INVALID")
    evidence = (*decimal_signals, signals.trend)
    if any(not _valid_hash(item.source_hash) for item in evidence) or not _valid_hash(
        signals.trading_status_source_hash
    ):
        raise OpportunityInputAuthorityError("SIGNAL_SOURCE_HASH_INVALID")
    times = tuple(item.observed_at for item in evidence) + (signals.trading_status_observed_at,)
    if any(
        _utc(item, "EVIDENCE_TIME_INVALID") < policy.selected_decision_boundary
        or not _fresh(item, snapshot.trusted_at, policy.maximum_underlying_age)
        for item in times
    ):
        raise OpportunityInputAuthorityError("SIGNAL_EVIDENCE_STALE_OR_FUTURE")


def _validate_catalyst(
    catalyst: CatalystAuthority,
    snapshot: OpportunityMarketSnapshot,
    policy: OpportunityPolicy,
) -> None:
    if (
        type(catalyst) is not CatalystAuthority
        or type(catalyst.quality) is not CatalystQuality
        or type(catalyst.score) is not int
    ):
        raise OpportunityInputAuthorityError("CATALYST_AUTHORITY_INVALID")
    if catalyst.opportunity_key != policy.opportunity_key:
        raise OpportunityInputAuthorityError("CATALYST_SCOPE_MISMATCH")
    if not 0 <= catalyst.score <= 100 or not _valid_hash(catalyst.source_hash):
        raise OpportunityInputAuthorityError("CATALYST_AUTHORITY_INVALID")
    if not _fresh(catalyst.observed_at, snapshot.trusted_at, policy.maximum_catalyst_age):
        raise OpportunityInputAuthorityError("CATALYST_EVIDENCE_STALE_OR_FUTURE")


def _validate_account(account: AccountBudgetAuthority, snapshot: OpportunityMarketSnapshot) -> None:
    book = snapshot.account_book
    if (
        type(account) is not AccountBudgetAuthority
        or type(account.baseline_clean) is not bool
        or type(account.entry_reservation_active) is not bool
        or type(account.event_already_attempted) is not bool
        or any(
            type(value) is not int
            for value in (
                account.open_position_count,
                account.open_order_count,
                account.filled_entry_count,
            )
        )
        or account.account_role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}
        or account.account_role is not book.account.role
        or account.account_fingerprint != book.account_fingerprint
        or account.snapshot_book_source_hash != book.source_hash
        or account.clean_equity != book.account.equity
        or account.open_position_count != len(book.positions.positions)
        or account.open_order_count != len(book.open_orders)
        or account.observed_at != snapshot.trusted_at
    ):
        raise OpportunityInputAuthorityError("ACCOUNT_AUTHORITY_MISMATCH")
    hashes = (
        account.account_fingerprint,
        account.snapshot_book_source_hash,
        account.baseline_source_hash,
        account.book_fingerprint,
        account.book_source_hash,
        account.history_source_hash,
    )
    if any(not _valid_hash(item) for item in hashes):
        raise OpportunityInputAuthorityError("ACCOUNT_SOURCE_HASH_INVALID")
    decimals = (
        account.clean_equity,
        account.lifetime_approved_risk,
        account.reserved_approved_risk,
    )
    if (
        any(not item.is_finite() for item in decimals)
        or account.clean_equity <= 0
        or min(
            account.open_position_count,
            account.open_order_count,
            account.filled_entry_count,
        )
        < 0
        or account.lifetime_approved_risk < 0
        or account.reserved_approved_risk < 0
        or (not account.entry_reservation_active and account.reserved_approved_risk != 0)
    ):
        raise OpportunityInputAuthorityError("ACCOUNT_BUDGET_INVALID")


def _validate_prior_decision(
    authority: PriorDecisionAuthority,
    snapshot: OpportunityMarketSnapshot,
    policy: OpportunityPolicy,
) -> None:
    if (
        type(authority) is not PriorDecisionAuthority
        or (
            authority.outcome is not None
            and type(authority.outcome) is not OpportunityOutcome
        )
        or authority.opportunity_key != policy.opportunity_key
        or authority.decision_boundary != policy.selected_decision_boundary
        or not _valid_hash(authority.source_hash)
        or _utc(authority.observed_at, "PRIOR_DECISION_TIME_INVALID")
        != snapshot.trusted_at
    ):
        raise OpportunityInputAuthorityError("PRIOR_DECISION_AUTHORITY_MISMATCH")


def _validate_selection(
    snapshot: OpportunityMarketSnapshot,
    policy: OpportunityPolicy,
    direction: OpportunityDirection | None,
    requested_maximum_quantity: int,
    authority: CandidateSelectionAuthority | None,
    selection: CandidateSelectionResult | None,
    account: AccountBudgetAuthority,
) -> VerticalCandidate | None:
    if (
        type(requested_maximum_quantity) is not int
        or requested_maximum_quantity < 1
    ):
        raise OpportunityInputAuthorityError("CANDIDATE_SELECTION_INVALID")
    if direction is None:
        if authority is not None or selection is not None:
            raise OpportunityInputAuthorityError("CANDIDATE_SELECTION_WITHOUT_DIRECTION")
        return None
    if (
        type(direction) is not OpportunityDirection
        or type(authority) is not CandidateSelectionAuthority
        or type(selection) is not CandidateSelectionResult
    ):
        raise OpportunityInputAuthorityError("CANDIDATE_SELECTION_INVALID")
    remaining_lifetime_risk = max(
        Decimal(0),
        policy.maximum_lifetime_risk
        - account.lifetime_approved_risk
        - account.reserved_approved_risk,
    )
    if (
        authority.snapshot_request_hash != snapshot.request_hash
        or authority.snapshot_source_hash != snapshot.source_hash
        or authority.account_fingerprint != account.account_fingerprint
        or authority.observed_equity != account.clean_equity
        or authority.observed_buying_power != snapshot.account_book.account.buying_power
        or authority.available_risk
        != min(policy.maximum_position_loss, remaining_lifetime_risk)
    ):
        raise OpportunityInputAuthorityError("CANDIDATE_AUTHORITY_MISMATCH")
    expected = select_vertical_candidate(
        snapshot,
        policy,
        direction,
        requested_maximum_quantity,
        authority,
    )
    if selection != expected:
        raise OpportunityInputAuthorityError("CANDIDATE_SELECTION_MISMATCH")
    if selection.reason not in {
        SelectionReason.SELECTED,
        SelectionReason.NO_ELIGIBLE_STRUCTURE,
        SelectionReason.RISK_AUTHORITY_INSUFFICIENT,
    }:
        raise OpportunityInputAuthorityError("CANDIDATE_SELECTION_INVALID")
    if (selection.reason is SelectionReason.SELECTED) is (selection.candidate is None):
        raise OpportunityInputAuthorityError("CANDIDATE_SELECTION_INVALID")
    return selection.candidate


def _fresh(observed_at: datetime, trusted_at: datetime, maximum_age: timedelta) -> bool:
    observed = _utc(observed_at, "EVIDENCE_TIME_INVALID")
    return timedelta(0) <= trusted_at - observed <= maximum_age


def _utc(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise OpportunityInputAuthorityError(code)
    return value.astimezone(UTC)


def _valid_hash(value: str) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _canonical_hash(domain: str, value: object) -> str:
    payload = json.dumps(
        {"domain": domain, "value": _canonical_value(value)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise OpportunityInputAuthorityError("NONFINITE_AUTHORITY_VALUE")
        fixed = format(value, "f")
        if "." in fixed:
            fixed = fixed.rstrip("0").rstrip(".")
        return "0" if fixed in {"-0", "+0"} else fixed
    if isinstance(value, datetime):
        return _utc(value, "AUTHORITY_TIME_INVALID").isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {key: _canonical_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, timedelta):
        return value.total_seconds()
    return value
