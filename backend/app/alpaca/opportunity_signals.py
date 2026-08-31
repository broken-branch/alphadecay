"""Bounded signal evidence collection over caller-owned Alpaca resources.

Production composition must inject the retained, already budgeted calendar and stock
clients. This collector adds no budget counter and makes exactly five read calls on a
complete run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol
from zoneinfo import ZoneInfo

from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.models import Bar, BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.models import Calendar
from alpaca.trading.requests import GetCalendarRequest

from backend.app.contracts.v1 import AccountRole
from backend.app.policy import OpportunityPolicy
from backend.app.services.opportunity_signals import (
    BENCHMARK_SYMBOL,
    DAILY_CLOSE_COUNT,
    MAX_REGULAR_SESSION_BARS,
    OpportunityDirectionalSignalAuthority,
    SignalBar,
    SignalCalendar,
    SignalDailyClose,
    SignalPriceAdjustment,
    calculate_opportunity_signals,
    signal_bar_digest,
    signal_calendar_digest,
    signal_daily_close_digest,
)

MAX_CALENDAR_SPAN_DAYS = 120
MAX_DAILY_BARS_PER_SYMBOL = 64
PROVIDER_CALL_COUNT = 5

_BAR_DURATION = timedelta(minutes=5)
_HASH = re.compile(r"[0-9a-f]{64}")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,9}")
_NEW_YORK = ZoneInfo("America/New_York")


class OpportunitySignalCollectionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OpportunitySignalCalendarClient(Protocol):
    def get_calendar(self, filters: GetCalendarRequest | None = None) -> list[Calendar]: ...


class OpportunitySignalStockClient(Protocol):
    def get_stock_bars(self, request_params: StockBarsRequest) -> BarSet: ...


@dataclass(frozen=True)
class OpportunitySignalRequest:
    account_role: AccountRole
    underlying: str
    benchmark: str
    daily_start_session: date
    pre_event_cutoff: date
    first_reaction_session: date
    signal_session: date
    signal_boundary: datetime
    maximum_calendar_span_days: int = MAX_CALENDAR_SPAN_DAYS
    maximum_daily_bars_per_symbol: int = MAX_DAILY_BARS_PER_SYMBOL
    maximum_intraday_bars_per_symbol: int = MAX_REGULAR_SESSION_BARS
    maximum_provider_calls: int = PROVIDER_CALL_COUNT

    def __post_init__(self) -> None:
        dates = (
            self.daily_start_session,
            self.pre_event_cutoff,
            self.first_reaction_session,
            self.signal_session,
        )
        strict_utc = (
            type(self.signal_boundary) is datetime
            and self.signal_boundary.tzinfo is not None
            and self.signal_boundary.utcoffset() == timedelta(0)
        )
        local_boundary = self.signal_boundary.astimezone(_NEW_YORK) if strict_utc else None
        if (
            self.account_role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION)
            or not _SYMBOL.fullmatch(self.underlying)
            or self.benchmark != BENCHMARK_SYMBOL
            or self.underlying == self.benchmark
            or any(type(value) is not date for value in dates)
            or not self.daily_start_session
            < self.pre_event_cutoff
            < self.first_reaction_session
            < self.signal_session
            or any(value.weekday() >= 5 for value in dates)
            or not strict_utc
            or local_boundary is None
            or local_boundary.date() != self.signal_session
            or local_boundary.second != 0
            or local_boundary.microsecond != 0
            or local_boundary.minute % 5 != 0
            or not 1
            <= (self.signal_session - self.daily_start_session).days
            <= self.maximum_calendar_span_days
            or not 1 <= self.maximum_calendar_span_days <= MAX_CALENDAR_SPAN_DAYS
            or not DAILY_CLOSE_COUNT + 1
            <= self.maximum_daily_bars_per_symbol
            <= MAX_DAILY_BARS_PER_SYMBOL
            or not 4 <= self.maximum_intraday_bars_per_symbol <= MAX_REGULAR_SESSION_BARS
            or self.maximum_provider_calls != PROVIDER_CALL_COUNT
        ):
            raise OpportunitySignalCollectionError("SIGNAL_REQUEST_INVALID")


@dataclass(frozen=True)
class CollectedOpportunitySignalEvidence:
    authority: OpportunityDirectionalSignalAuthority
    calendar: SignalCalendar
    underlying_daily_closes: tuple[SignalDailyClose, ...]
    benchmark_daily_closes: tuple[SignalDailyClose, ...]
    first_reaction_close: SignalDailyClose
    underlying_bars: tuple[SignalBar, ...]
    benchmark_bars: tuple[SignalBar, ...]
    daily_source_hash: str
    intraday_source_hash: str


class AlpacaOpportunitySignalCollector:
    def __init__(
        self,
        calendar: OpportunitySignalCalendarClient,
        stocks: OpportunitySignalStockClient,
        *,
        calculator: Callable[..., OpportunityDirectionalSignalAuthority] = (
            calculate_opportunity_signals
        ),
    ) -> None:
        self._calendar = calendar
        self._stocks = stocks
        self._calculator = calculator

    def collect(
        self,
        request: OpportunitySignalRequest,
        *,
        policy: OpportunityPolicy,
        snapshot_source_hash: str,
        observed_at: datetime,
    ) -> OpportunityDirectionalSignalAuthority:
        return self.collect_evidence(
            request,
            policy=policy,
            snapshot_source_hash=snapshot_source_hash,
            observed_at=observed_at,
        ).authority

    def collect_evidence(
        self,
        request: OpportunitySignalRequest,
        *,
        policy: OpportunityPolicy,
        snapshot_source_hash: str,
        observed_at: datetime,
    ) -> CollectedOpportunitySignalEvidence:
        self._validate_authority(request, policy, snapshot_source_hash, observed_at)
        calendar = self._collect_calendar(request)
        underlying_daily, first_reaction = self._collect_underlying_daily(request, calendar)
        benchmark_daily = self._collect_daily(
            request,
            symbol=request.benchmark,
            expected_sessions=calendar.daily_sessions,
        )
        underlying_bars = self._collect_intraday(request, calendar, request.underlying)
        benchmark_bars = self._collect_intraday(request, calendar, request.benchmark)
        authority = self._calculator(
            account_role=request.account_role,
            policy=policy,
            snapshot_source_hash=snapshot_source_hash,
            observed_at=observed_at,
            calendar=calendar,
            underlying_daily_closes=underlying_daily,
            benchmark_daily_closes=benchmark_daily,
            first_reaction_close=first_reaction,
            underlying_bars=underlying_bars,
            benchmark_bars=benchmark_bars,
        )
        daily_source_hash = opportunity_signal_daily_evidence_digest(
            underlying_daily,
            benchmark_daily,
            first_reaction,
        )
        intraday_source_hash = opportunity_signal_intraday_evidence_digest(
            underlying_bars,
            benchmark_bars,
        )
        return CollectedOpportunitySignalEvidence(
            authority=authority,
            calendar=calendar,
            underlying_daily_closes=underlying_daily,
            benchmark_daily_closes=benchmark_daily,
            first_reaction_close=first_reaction,
            underlying_bars=underlying_bars,
            benchmark_bars=benchmark_bars,
            daily_source_hash=daily_source_hash,
            intraday_source_hash=intraday_source_hash,
        )

    @staticmethod
    def _validate_authority(
        request: OpportunitySignalRequest,
        policy: OpportunityPolicy,
        snapshot_source_hash: str,
        observed_at: datetime,
    ) -> None:
        if type(request) is not OpportunitySignalRequest:
            raise OpportunitySignalCollectionError("SIGNAL_REQUEST_INVALID")
        if (
            type(policy) is not OpportunityPolicy
            or policy.underlying != request.underlying
            or policy.selected_decision_boundary != request.signal_boundary
            or not isinstance(snapshot_source_hash, str)
            or _HASH.fullmatch(snapshot_source_hash) is None
        ):
            raise OpportunitySignalCollectionError("SIGNAL_AUTHORITY_MISMATCH")
        if (
            type(observed_at) is not datetime
            or observed_at.tzinfo is None
            or observed_at.utcoffset() != timedelta(0)
            or observed_at < request.signal_boundary
            or observed_at - request.signal_boundary > policy.maximum_underlying_age
        ):
            raise OpportunitySignalCollectionError("SIGNAL_OBSERVATION_TIME_INVALID")

    def _collect_calendar(self, request: OpportunitySignalRequest) -> SignalCalendar:
        raw = self._calendar.get_calendar(
            GetCalendarRequest(
                start=request.daily_start_session,
                end=request.signal_session,
            )
        )
        if type(raw) is not list or not raw or any(type(item) is not Calendar for item in raw):
            raise OpportunitySignalCollectionError("SIGNAL_CALENDAR_INVALID")
        dates = tuple(item.date for item in raw)
        if (
            len(raw) > request.maximum_calendar_span_days
            or len(set(dates)) != len(dates)
            or dates != tuple(sorted(dates))
            or dates[0] != request.daily_start_session
            or dates[-1] != request.signal_session
            or any(day.weekday() >= 5 for day in dates)
        ):
            raise OpportunitySignalCollectionError("SIGNAL_CALENDAR_INVALID")
        sessions_through_cutoff = tuple(day for day in dates if day <= request.pre_event_cutoff)
        later_sessions = tuple(day for day in dates if day > request.pre_event_cutoff)
        if (
            len(sessions_through_cutoff) != DAILY_CLOSE_COUNT
            or sessions_through_cutoff[-1] != request.pre_event_cutoff
            or not later_sessions
            or later_sessions[0] != request.first_reaction_session
            or request.signal_session not in later_sessions
        ):
            raise OpportunitySignalCollectionError("SIGNAL_CALENDAR_CHRONOLOGY_INVALID")
        session_times = tuple(_calendar_times(item) for item in raw)
        if any(
            open_at.astimezone(_NEW_YORK).time() != time(9, 30)
            or close_at.astimezone(_NEW_YORK).time() not in {time(13), time(16)}
            or open_at >= close_at
            for open_at, close_at in session_times
        ):
            raise OpportunitySignalCollectionError("SIGNAL_CALENDAR_SESSION_INVALID")
        signal_index = dates.index(request.signal_session)
        signal_open, signal_close = session_times[signal_index]
        expected_bars = int((request.signal_boundary - signal_open) / _BAR_DURATION)
        if (
            not signal_open < request.signal_boundary <= signal_close
            or not 4 <= expected_bars <= request.maximum_intraday_bars_per_symbol
            or signal_open + expected_bars * _BAR_DURATION != request.signal_boundary
        ):
            raise OpportunitySignalCollectionError("SIGNAL_BOUNDARY_INVALID")
        normalized = SignalCalendar(
            daily_sessions=sessions_through_cutoff,
            pre_event_cutoff=request.pre_event_cutoff,
            first_reaction_session=request.first_reaction_session,
            signal_session=request.signal_session,
            signal_open_at=signal_open,
            signal_close_at=signal_close,
            source_hash="",
        )
        return replace(normalized, source_hash=signal_calendar_digest(normalized))

    def _collect_underlying_daily(
        self,
        request: OpportunitySignalRequest,
        calendar: SignalCalendar,
    ) -> tuple[tuple[SignalDailyClose, ...], SignalDailyClose]:
        expected = (*calendar.daily_sessions, calendar.first_reaction_session)
        values = self._collect_daily(
            request,
            symbol=request.underlying,
            expected_sessions=expected,
        )
        return values[:-1], values[-1]

    def _collect_daily(
        self,
        request: OpportunitySignalRequest,
        *,
        symbol: str,
        expected_sessions: tuple[date, ...],
    ) -> tuple[SignalDailyClose, ...]:
        end_session = expected_sessions[-1] + timedelta(days=1)
        raw = self._stocks.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=_session_midnight(expected_sessions[0]),
                end=_session_midnight(end_session),
                limit=request.maximum_daily_bars_per_symbol,
                adjustment=Adjustment.ALL,
                feed=DataFeed.IEX,
                sort=Sort.ASC,
            )
        )
        bars = _single_symbol_bars(raw, symbol, "SIGNAL_DAILY_BARS_INVALID")
        if len(bars) > request.maximum_daily_bars_per_symbol or len(bars) != len(expected_sessions):
            raise OpportunitySignalCollectionError("SIGNAL_DAILY_BAR_COUNT_INVALID")
        normalized = tuple(
            _normalize_daily_close(bar, symbol, session)
            for bar, session in zip(bars, expected_sessions, strict=True)
        )
        if tuple(item.session_date for item in normalized) != expected_sessions:
            raise OpportunitySignalCollectionError("SIGNAL_DAILY_BAR_CHRONOLOGY_INVALID")
        return normalized

    def _collect_intraday(
        self,
        request: OpportunitySignalRequest,
        calendar: SignalCalendar,
        symbol: str,
    ) -> tuple[SignalBar, ...]:
        expected_count = int((request.signal_boundary - calendar.signal_open_at) / _BAR_DURATION)
        raw = self._stocks.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=calendar.signal_open_at,
                end=request.signal_boundary,
                limit=request.maximum_intraday_bars_per_symbol,
                adjustment=Adjustment.SPLIT,
                feed=DataFeed.IEX,
                sort=Sort.ASC,
            )
        )
        bars = _single_symbol_bars(raw, symbol, "SIGNAL_INTRADAY_BARS_INVALID")
        if len(bars) != expected_count or len(bars) > request.maximum_intraday_bars_per_symbol:
            raise OpportunitySignalCollectionError("SIGNAL_INTRADAY_BAR_COUNT_INVALID")
        expected_starts = tuple(
            calendar.signal_open_at + index * _BAR_DURATION for index in range(expected_count)
        )
        normalized = tuple(
            _normalize_intraday_bar(bar, symbol, started_at, request.signal_boundary)
            for bar, started_at in zip(bars, expected_starts, strict=True)
        )
        if tuple(item.started_at for item in normalized) != expected_starts:
            raise OpportunitySignalCollectionError("SIGNAL_INTRADAY_BAR_CHRONOLOGY_INVALID")
        return normalized


def _single_symbol_bars(raw: object, symbol: str, code: str) -> list[Bar]:
    if not isinstance(raw, BarSet) or set(raw.data) != {symbol}:
        raise OpportunitySignalCollectionError(code)
    bars = raw.data[symbol]
    if type(bars) is not list or any(type(item) is not Bar for item in bars):
        raise OpportunitySignalCollectionError(code)
    return bars


def opportunity_signal_daily_evidence_digest(
    underlying: tuple[SignalDailyClose, ...],
    benchmark: tuple[SignalDailyClose, ...],
    first_reaction: SignalDailyClose,
) -> str:
    return _evidence_hash(
        "alphadecay.opportunity.signal-daily-evidence.v1",
        {
            "underlying": [item.source_hash for item in underlying],
            "benchmark": [item.source_hash for item in benchmark],
            "first_reaction": first_reaction.source_hash,
        },
    )


def opportunity_signal_intraday_evidence_digest(
    underlying: tuple[SignalBar, ...],
    benchmark: tuple[SignalBar, ...],
) -> str:
    return _evidence_hash(
        "alphadecay.opportunity.signal-intraday-evidence.v1",
        {
            "underlying": [item.source_hash for item in underlying],
            "benchmark": [item.source_hash for item in benchmark],
        },
    )


def _normalize_daily_close(bar: Bar, symbol: str, session: date) -> SignalDailyClose:
    started_at = _utc(bar.timestamp, "SIGNAL_DAILY_BAR_INVALID")
    if (
        bar.symbol != symbol
        or started_at.astimezone(_NEW_YORK).date() != session
        or started_at.astimezone(_NEW_YORK).time() != time(0)
    ):
        raise OpportunitySignalCollectionError("SIGNAL_DAILY_BAR_CHRONOLOGY_INVALID")
    close = _positive_decimal(bar.close, "SIGNAL_DAILY_BAR_INVALID")
    normalized = SignalDailyClose(symbol, session, close, SignalPriceAdjustment.ALL, "")
    return replace(normalized, source_hash=signal_daily_close_digest(normalized))


def _normalize_intraday_bar(
    bar: Bar,
    symbol: str,
    expected_start: datetime,
    boundary: datetime,
) -> SignalBar:
    started_at = _utc(bar.timestamp, "SIGNAL_INTRADAY_BAR_INVALID")
    if (
        bar.symbol != symbol
        or started_at != expected_start
        or started_at + _BAR_DURATION > boundary
    ):
        raise OpportunitySignalCollectionError("SIGNAL_INTRADAY_BAR_CHRONOLOGY_INVALID")
    open_, high, low, close, volume, vwap = tuple(
        _positive_decimal(value, "SIGNAL_INTRADAY_BAR_INVALID")
        for value in (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap)
    )
    if (
        low > high
        or not low <= open_ <= high
        or not low <= close <= high
        or not low <= vwap <= high
    ):
        raise OpportunitySignalCollectionError("SIGNAL_INTRADAY_BAR_INVALID")
    normalized = SignalBar(
        symbol=symbol,
        started_at=started_at,
        completed_at=started_at + _BAR_DURATION,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        vwap=vwap,
        adjustment=SignalPriceAdjustment.SPLIT,
        source_hash="",
    )
    return replace(normalized, source_hash=signal_bar_digest(normalized))


def _calendar_times(session: Calendar) -> tuple[datetime, datetime]:
    if (
        type(session.date) is not date
        or type(session.open) is not datetime
        or type(session.close) is not datetime
        or session.open.tzinfo is not None
        or session.close.tzinfo is not None
        or session.open.date() != session.date
        or session.close.date() != session.date
        or session.open.second != 0
        or session.close.second != 0
        or session.open.microsecond != 0
        or session.close.microsecond != 0
    ):
        raise OpportunitySignalCollectionError("SIGNAL_CALENDAR_SESSION_INVALID")
    open_at = datetime.combine(session.date, session.open.time(), _NEW_YORK).astimezone(UTC)
    close_at = datetime.combine(session.date, session.close.time(), _NEW_YORK).astimezone(UTC)
    return open_at, close_at


def _session_midnight(session: date) -> datetime:
    return datetime.combine(session, time(0), _NEW_YORK).astimezone(UTC)


def _positive_decimal(value: object, code: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise OpportunitySignalCollectionError(code) from error
    if not number.is_finite() or number <= 0:
        raise OpportunitySignalCollectionError(code)
    return number


def _utc(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OpportunitySignalCollectionError(code)
    return value.astimezone(UTC)


def _evidence_hash(domain: str, value: object) -> str:
    payload = json.dumps(
        {"domain": domain, "value": value},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
