from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, DecimalException
from enum import Enum
from zoneinfo import ZoneInfo

from backend.app.contracts.v1 import AccountRole
from backend.app.policy import OpportunityDirection, OpportunityPolicy
from backend.app.policy.opportunity import derive_opportunity_direction
from backend.app.services.opportunity_input import (
    DecimalSignalAuthority,
    TrendSignalAuthority,
)

BENCHMARK_SYMBOL = "QQQ"
DAILY_CLOSE_COUNT = 61
RETURN_COUNT = 60
MAX_REGULAR_SESSION_BARS = 78
MAX_PRICE = Decimal("1000000000")
MAX_VOLUME = Decimal("1000000000000000")
MAX_BETA = Decimal("3")

_BAR_DURATION = timedelta(minutes=5)
_HASH = re.compile(r"[0-9a-f]{64}")
_NEW_YORK = ZoneInfo("America/New_York")


class OpportunitySignalCalculationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SignalPriceAdjustment(str, Enum):
    ALL = "ALL"
    RAW = "RAW"
    SPLIT = "SPLIT"


@dataclass(frozen=True)
class SignalCalendar:
    daily_sessions: tuple[date, ...]
    pre_event_cutoff: date
    first_reaction_session: date
    signal_session: date
    signal_open_at: datetime
    signal_close_at: datetime
    source_hash: str


@dataclass(frozen=True)
class SignalDailyClose:
    symbol: str
    session_date: date
    close: Decimal
    adjustment: SignalPriceAdjustment
    source_hash: str


@dataclass(frozen=True)
class SignalBar:
    symbol: str
    started_at: datetime
    completed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal
    adjustment: SignalPriceAdjustment
    source_hash: str


@dataclass(frozen=True)
class OpportunityDirectionalSignalAuthority:
    snapshot_source_hash: str
    beta: DecimalSignalAuthority
    vwap_distance: DecimalSignalAuthority
    relative_return: DecimalSignalAuthority
    trend: TrendSignalAuthority
    absolute_first_reaction: DecimalSignalAuthority
    direction: OpportunityDirection | None
    pre_event_cutoff: date
    source_hash: str


def signal_calendar_digest(calendar: SignalCalendar) -> str:
    return _canonical_hash(
        "alphadecay.opportunity.signal-calendar.v1",
        {
            "daily_sessions": calendar.daily_sessions,
            "pre_event_cutoff": calendar.pre_event_cutoff,
            "first_reaction_session": calendar.first_reaction_session,
            "signal_session": calendar.signal_session,
            "signal_open_at": calendar.signal_open_at,
            "signal_close_at": calendar.signal_close_at,
        },
    )


def signal_daily_close_digest(value: SignalDailyClose) -> str:
    return _canonical_hash(
        "alphadecay.opportunity.signal-daily-close.v1",
        {
            "symbol": value.symbol,
            "session_date": value.session_date,
            "close": value.close,
            "adjustment": value.adjustment,
        },
    )


def signal_bar_digest(value: SignalBar) -> str:
    return _canonical_hash(
        "alphadecay.opportunity.signal-bar.v1",
        {
            "symbol": value.symbol,
            "started_at": value.started_at,
            "completed_at": value.completed_at,
            "open": value.open,
            "high": value.high,
            "low": value.low,
            "close": value.close,
            "volume": value.volume,
            "vwap": value.vwap,
            "adjustment": value.adjustment,
        },
    )


def calculate_opportunity_signals(
    *,
    account_role: AccountRole,
    policy: OpportunityPolicy,
    snapshot_source_hash: str,
    observed_at: datetime,
    calendar: SignalCalendar,
    underlying_daily_closes: tuple[SignalDailyClose, ...],
    benchmark_daily_closes: tuple[SignalDailyClose, ...],
    first_reaction_close: SignalDailyClose,
    underlying_bars: tuple[SignalBar, ...],
    benchmark_bars: tuple[SignalBar, ...],
) -> OpportunityDirectionalSignalAuthority:
    boundary = _validate_scope(
        account_role,
        policy,
        snapshot_source_hash,
        observed_at,
        calendar,
    )
    _validate_daily_closes(
        calendar,
        policy.underlying,
        underlying_daily_closes,
        BENCHMARK_SYMBOL,
        benchmark_daily_closes,
        first_reaction_close,
    )
    _validate_bars(
        calendar.signal_open_at,
        calendar.signal_close_at,
        boundary,
        policy.underlying,
        underlying_bars,
        BENCHMARK_SYMBOL,
        benchmark_bars,
    )

    try:
        beta = _beta60(underlying_daily_closes, benchmark_daily_closes)
        underlying_vwap = sum((bar.vwap * bar.volume for bar in underlying_bars), Decimal(0)) / sum(
            (bar.volume for bar in underlying_bars), Decimal(0)
        )
        latest = underlying_bars[-1]
        vwap_distance = latest.close / underlying_vwap - Decimal(1)
        underlying_return = latest.close / underlying_bars[0].open - Decimal(1)
        benchmark_return = benchmark_bars[-1].close / benchmark_bars[0].open - Decimal(1)
        relative_return = underlying_return - beta * benchmark_return
        cutoff_close = underlying_daily_closes[-1]
        absolute_first_reaction = abs(first_reaction_close.close / cutoff_close.close - Decimal(1))
    except DecimalException as exc:
        raise OpportunitySignalCalculationError("SIGNAL_NUMERIC_RESULT_INVALID") from exc
    if not all(
        value.is_finite()
        for value in (
            beta,
            underlying_vwap,
            vwap_distance,
            underlying_return,
            benchmark_return,
            relative_return,
            absolute_first_reaction,
        )
    ):
        raise OpportunitySignalCalculationError("SIGNAL_NUMERIC_RESULT_INVALID")
    recent = underlying_bars[-4:]
    bull_hits = sum(recent[index].low >= recent[index - 1].low for index in range(1, 4))
    bear_hits = sum(recent[index].high <= recent[index - 1].high for index in range(1, 4))

    beta_hash = _canonical_hash(
        "alphadecay.opportunity.signal-beta60.v1",
        {
            "pre_event_cutoff": calendar.pre_event_cutoff,
            "daily_sessions": calendar.daily_sessions,
            "underlying": underlying_daily_closes,
            "benchmark": benchmark_daily_closes,
            "return_count": RETURN_COUNT,
            "value": beta,
        },
    )
    vwap_hash = _canonical_hash(
        "alphadecay.opportunity.signal-vwap-distance.v1",
        {
            "calendar_source_hash": calendar.source_hash,
            "decision_boundary": boundary,
            "bars": underlying_bars,
            "session_vwap": underlying_vwap,
            "value": vwap_distance,
        },
    )
    relative_hash = _canonical_hash(
        "alphadecay.opportunity.signal-relative-return.v1",
        {
            "calendar_source_hash": calendar.source_hash,
            "decision_boundary": boundary,
            "beta_source_hash": beta_hash,
            "underlying_bars": underlying_bars,
            "benchmark_bars": benchmark_bars,
            "value": relative_return,
        },
    )
    trend_hash = _canonical_hash(
        "alphadecay.opportunity.signal-trend.v1",
        {
            "calendar_source_hash": calendar.source_hash,
            "pre_event_cutoff": calendar.pre_event_cutoff,
            "decision_boundary": boundary,
            "bars": recent,
            "bull_hits": bull_hits,
            "bear_hits": bear_hits,
        },
    )
    reaction_hash = _canonical_hash(
        "alphadecay.opportunity.signal-first-reaction.v1",
        {
            "calendar_source_hash": calendar.source_hash,
            "pre_event_cutoff": calendar.pre_event_cutoff,
            "cutoff_close": cutoff_close,
            "first_reaction_close": first_reaction_close,
            "value": absolute_first_reaction,
        },
    )
    authority = OpportunityDirectionalSignalAuthority(
        snapshot_source_hash=snapshot_source_hash,
        beta=DecimalSignalAuthority(beta, observed_at, beta_hash),
        vwap_distance=DecimalSignalAuthority(vwap_distance, observed_at, vwap_hash),
        relative_return=DecimalSignalAuthority(relative_return, observed_at, relative_hash),
        trend=TrendSignalAuthority(bull_hits, bear_hits, observed_at, trend_hash),
        absolute_first_reaction=DecimalSignalAuthority(
            absolute_first_reaction,
            observed_at,
            reaction_hash,
        ),
        direction=None,
        pre_event_cutoff=calendar.pre_event_cutoff,
        source_hash="",
    )
    direction = derive_opportunity_direction(
        policy,
        vwap_distance=vwap_distance,
        relative_return=relative_return,
        bull_trend_hits=bull_hits,
        bear_trend_hits=bear_hits,
    )
    calculation_hash = _canonical_hash(
        "alphadecay.opportunity.signal-calculation.v1",
        {
            "account_role": account_role,
            "policy": policy,
            "snapshot_source_hash": snapshot_source_hash,
            "observed_at": observed_at,
            "calendar": calendar,
            "signals": authority,
            "direction": direction,
        },
    )
    return OpportunityDirectionalSignalAuthority(
        snapshot_source_hash=authority.snapshot_source_hash,
        beta=authority.beta,
        vwap_distance=authority.vwap_distance,
        relative_return=authority.relative_return,
        trend=authority.trend,
        absolute_first_reaction=authority.absolute_first_reaction,
        direction=direction,
        pre_event_cutoff=calendar.pre_event_cutoff,
        source_hash=calculation_hash,
    )


def _validate_scope(
    account_role: AccountRole,
    policy: OpportunityPolicy,
    snapshot_source_hash: str,
    observed_at: datetime,
    calendar: SignalCalendar,
) -> datetime:
    if account_role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION):
        raise OpportunitySignalCalculationError("ACCOUNT_ROLE_NOT_DEVELOPMENT")
    if type(policy) is not OpportunityPolicy or not _valid_hash(snapshot_source_hash):
        raise OpportunitySignalCalculationError("SIGNAL_SCOPE_INVALID")
    observed = _utc(observed_at, "OBSERVED_TIME_INVALID")
    boundary = _utc(policy.selected_decision_boundary, "DECISION_BOUNDARY_INVALID")
    if observed < boundary or observed - boundary > policy.maximum_underlying_age:
        raise OpportunitySignalCalculationError("OBSERVATION_TIME_INVALID")
    if type(calendar) is not SignalCalendar or calendar.source_hash != signal_calendar_digest(
        calendar
    ):
        raise OpportunitySignalCalculationError("CALENDAR_EVIDENCE_INVALID")
    sessions = calendar.daily_sessions
    local_boundary = boundary.astimezone(_NEW_YORK)
    open_at = _utc(calendar.signal_open_at, "CALENDAR_SESSION_TIME_INVALID")
    close_at = _utc(calendar.signal_close_at, "CALENDAR_SESSION_TIME_INVALID")
    local_open = open_at.astimezone(_NEW_YORK)
    local_close = close_at.astimezone(_NEW_YORK)
    if (
        type(sessions) is not tuple
        or len(sessions) != DAILY_CLOSE_COUNT
        or any(type(session) is not date for session in sessions)
        or type(calendar.pre_event_cutoff) is not date
        or type(calendar.first_reaction_session) is not date
        or type(calendar.signal_session) is not date
        or len(set(sessions)) != DAILY_CLOSE_COUNT
        or tuple(sorted(sessions)) != sessions
        or any(session.weekday() >= 5 for session in sessions)
        or calendar.first_reaction_session.weekday() >= 5
        or calendar.signal_session.weekday() >= 5
        or sessions[-1] != calendar.pre_event_cutoff
        or not calendar.pre_event_cutoff < calendar.first_reaction_session < calendar.signal_session
        or calendar.signal_session != local_boundary.date()
        or local_open.date() != calendar.signal_session
        or local_open.time() != time(9, 30)
        or local_close.date() != calendar.signal_session
        or local_close.time() not in {time(13), time(16)}
        or not open_at < boundary <= close_at
        or local_boundary.second != 0
        or local_boundary.microsecond != 0
        or local_boundary.minute % 5 != 0
    ):
        raise OpportunitySignalCalculationError("CALENDAR_CHRONOLOGY_INVALID")
    return boundary


def _validate_daily_closes(
    calendar: SignalCalendar,
    underlying: str,
    underlying_closes: tuple[SignalDailyClose, ...],
    benchmark: str,
    benchmark_closes: tuple[SignalDailyClose, ...],
    first_reaction_close: SignalDailyClose,
) -> None:
    if (
        type(underlying_closes) is not tuple
        or type(benchmark_closes) is not tuple
        or len(underlying_closes) != DAILY_CLOSE_COUNT
        or len(benchmark_closes) != DAILY_CLOSE_COUNT
        or type(first_reaction_close) is not SignalDailyClose
    ):
        raise OpportunitySignalCalculationError("DAILY_CLOSE_COUNT_INVALID")
    for symbol, values in ((underlying, underlying_closes), (benchmark, benchmark_closes)):
        if any(type(item) is not SignalDailyClose for item in values):
            raise OpportunitySignalCalculationError("DAILY_CLOSE_EVIDENCE_INVALID")
        if tuple(item.session_date for item in values) != calendar.daily_sessions:
            raise OpportunitySignalCalculationError("DAILY_CLOSE_CALENDAR_MISMATCH")
        if any(
            item.symbol != symbol
            or item.adjustment is not SignalPriceAdjustment.ALL
            or not _price_valid(item.close)
            or item.source_hash != signal_daily_close_digest(item)
            for item in values
        ):
            raise OpportunitySignalCalculationError("DAILY_CLOSE_EVIDENCE_INVALID")
    if (
        first_reaction_close.symbol != underlying
        or first_reaction_close.session_date != calendar.first_reaction_session
        or first_reaction_close.adjustment is not SignalPriceAdjustment.ALL
        or not _price_valid(first_reaction_close.close)
        or first_reaction_close.source_hash != signal_daily_close_digest(first_reaction_close)
    ):
        raise OpportunitySignalCalculationError("FIRST_REACTION_EVIDENCE_INVALID")


def _validate_bars(
    session_open_at: datetime,
    session_close_at: datetime,
    boundary: datetime,
    underlying: str,
    underlying_bars: tuple[SignalBar, ...],
    benchmark: str,
    benchmark_bars: tuple[SignalBar, ...],
) -> None:
    local_boundary = boundary.astimezone(_NEW_YORK)
    open_at = _utc(session_open_at, "CALENDAR_SESSION_TIME_INVALID")
    close_at = _utc(session_close_at, "CALENDAR_SESSION_TIME_INVALID")
    expected_count = int((boundary - open_at) / _BAR_DURATION)
    if (
        type(underlying_bars) is not tuple
        or type(benchmark_bars) is not tuple
        or not 4 <= expected_count <= MAX_REGULAR_SESSION_BARS
        or len(underlying_bars) != expected_count
        or len(benchmark_bars) != expected_count
        or local_boundary.date() != open_at.astimezone(_NEW_YORK).date()
        or boundary > close_at
    ):
        raise OpportunitySignalCalculationError("INTRADAY_BAR_COUNT_INVALID")
    expected_starts = tuple(open_at + index * _BAR_DURATION for index in range(expected_count))
    for symbol, values in ((underlying, underlying_bars), (benchmark, benchmark_bars)):
        if any(type(item) is not SignalBar for item in values):
            raise OpportunitySignalCalculationError("INTRADAY_BAR_EVIDENCE_INVALID")
        if tuple(item.started_at for item in values) != expected_starts:
            raise OpportunitySignalCalculationError("INTRADAY_BAR_CHRONOLOGY_INVALID")
        if any(not _bar_valid(item, symbol, boundary) for item in values):
            raise OpportunitySignalCalculationError("INTRADAY_BAR_EVIDENCE_INVALID")
    if tuple(item.started_at for item in underlying_bars) != tuple(
        item.started_at for item in benchmark_bars
    ):
        raise OpportunitySignalCalculationError("INTRADAY_BAR_ALIGNMENT_INVALID")


def _bar_valid(bar: SignalBar, symbol: str, boundary: datetime) -> bool:
    prices = (bar.open, bar.high, bar.low, bar.close, bar.vwap)
    return (
        type(bar) is SignalBar
        and bar.symbol == symbol
        and _utc(bar.started_at, "BAR_TIME_INVALID") + _BAR_DURATION
        == _utc(bar.completed_at, "BAR_TIME_INVALID")
        and bar.completed_at <= boundary
        and all(_price_valid(value) for value in prices)
        and bar.adjustment is SignalPriceAdjustment.SPLIT
        and bar.low <= bar.open <= bar.high
        and bar.low <= bar.close <= bar.high
        and bar.low <= bar.vwap <= bar.high
        and isinstance(bar.volume, Decimal)
        and bar.volume.is_finite()
        and Decimal(0) < bar.volume <= MAX_VOLUME
        and bar.source_hash == signal_bar_digest(bar)
    )


def _beta60(
    underlying_closes: tuple[SignalDailyClose, ...],
    benchmark_closes: tuple[SignalDailyClose, ...],
) -> Decimal:
    underlying_returns = tuple(
        underlying_closes[index].close / underlying_closes[index - 1].close - Decimal(1)
        for index in range(1, DAILY_CLOSE_COUNT)
    )
    benchmark_returns = tuple(
        benchmark_closes[index].close / benchmark_closes[index - 1].close - Decimal(1)
        for index in range(1, DAILY_CLOSE_COUNT)
    )
    underlying_mean = sum(underlying_returns, Decimal(0)) / RETURN_COUNT
    benchmark_mean = sum(benchmark_returns, Decimal(0)) / RETURN_COUNT
    covariance_numerator = sum(
        (
            (underlying_return - underlying_mean) * (benchmark_return - benchmark_mean)
            for underlying_return, benchmark_return in zip(
                underlying_returns,
                benchmark_returns,
                strict=True,
            )
        ),
        Decimal(0),
    )
    variance_numerator = sum(
        ((item - benchmark_mean) ** 2 for item in benchmark_returns),
        Decimal(0),
    )
    if variance_numerator <= 0:
        raise OpportunitySignalCalculationError("BENCHMARK_VARIANCE_NOT_POSITIVE")
    beta = covariance_numerator / variance_numerator
    if not beta.is_finite() or not Decimal(0) < beta <= MAX_BETA:
        raise OpportunitySignalCalculationError("BETA_OUT_OF_BOUNDS")
    return beta


def _price_valid(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and Decimal(0) < value <= MAX_PRICE


def _utc(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise OpportunitySignalCalculationError(code)
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
            raise OpportunitySignalCalculationError("NONFINITE_EVIDENCE_VALUE")
        fixed = format(value, "f")
        if "." in fixed:
            fixed = fixed.rstrip("0").rstrip(".")
        return "0" if fixed in {"-0", "+0"} else fixed
    if isinstance(value, datetime):
        return _utc(value, "EVIDENCE_TIME_INVALID").isoformat(timespec="microseconds")
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
