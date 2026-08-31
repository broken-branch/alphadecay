from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.models import BarSet
from alpaca.data.timeframe import TimeFrameUnit
from alpaca.trading.models import Calendar

from backend.app.alpaca.opportunity_runtime import (
    OpportunityRuntimeAdapterError,
    OpportunitySignalRuntimeAdapter,
)
from backend.app.alpaca.opportunity_signals import (
    AlpacaOpportunitySignalCollector,
    OpportunitySignalCollectionError,
    OpportunitySignalRequest,
    opportunity_signal_daily_evidence_digest,
    opportunity_signal_intraday_evidence_digest,
)
from backend.app.contracts.v1 import AccountRole
from backend.app.policy import OpportunityDirection, OpportunityPolicy
from backend.app.services.opportunity_signals import (
    BENCHMARK_SYMBOL,
    signal_bar_digest,
    signal_calendar_digest,
    signal_daily_close_digest,
)

BOUNDARY = datetime(2026, 8, 28, 16, tzinfo=UTC)
OBSERVED_AT = BOUNDARY + timedelta(seconds=20)
SNAPSHOT_HASH = "a" * 64
NEW_YORK = ZoneInfo("America/New_York")


def _business_sessions(count: int, through: date) -> tuple[date, ...]:
    result = []
    current = through
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current -= timedelta(days=1)
    return tuple(reversed(result))


DAILY_SESSIONS = _business_sessions(61, date(2026, 8, 26))


def _request(**changes: object) -> OpportunitySignalRequest:
    values: dict[str, object] = {
        "account_role": AccountRole.DEVELOPMENT,
        "underlying": "NVDA",
        "benchmark": BENCHMARK_SYMBOL,
        "daily_start_session": DAILY_SESSIONS[0],
        "pre_event_cutoff": DAILY_SESSIONS[-1],
        "first_reaction_session": date(2026, 8, 27),
        "signal_session": date(2026, 8, 28),
        "signal_boundary": BOUNDARY,
    }
    values.update(changes)
    return OpportunitySignalRequest(**values)  # type: ignore[arg-type]


def _policy(**changes: object) -> OpportunityPolicy:
    values: dict[str, object] = {
        "version": "opportunity-v1",
        "opportunity_key": "NVDA-2026-08-28",
        "underlying": "NVDA",
        "selected_decision_boundary": BOUNDARY,
        "last_entry_boundary": BOUNDARY + timedelta(minutes=30),
        "maximum_decision_delay": timedelta(minutes=2),
        "maximum_underlying_age": timedelta(minutes=2),
        "maximum_catalyst_age": timedelta(hours=2),
        "maximum_option_quote_age": timedelta(seconds=30),
        "maximum_leg_quote_skew": timedelta(seconds=5),
        "minimum_vwap_distance": Decimal("0.003"),
        "maximum_vwap_distance": Decimal("0.03"),
        "minimum_relative_return": Decimal("0.0075"),
        "minimum_beta": Decimal("0"),
        "maximum_beta": Decimal("3"),
        "required_trend_hits": 3,
        "maximum_first_reaction": Decimal("0.12"),
        "minimum_catalyst_score": 70,
        "minimum_candidate_score": 70,
        "minimum_dte": 7,
        "maximum_dte": 35,
        "maximum_relative_spread": Decimal("0.30"),
        "minimum_debit_width_fraction": Decimal("0.20"),
        "maximum_debit_width_fraction": Decimal("0.80"),
        "minimum_credit_width_fraction": Decimal("0.15"),
        "maximum_position_loss": Decimal("500"),
        "maximum_equity_risk_fraction": Decimal("0.0125"),
        "maximum_lifetime_entries": 1,
        "maximum_lifetime_risk": Decimal("500"),
        "equity_floor": Decimal("95000"),
        "maximum_quantity": 4,
    }
    values.update(changes)
    return OpportunityPolicy(**values)  # type: ignore[arg-type]


class Calendars:
    def __init__(self) -> None:
        sessions = (*DAILY_SESSIONS, date(2026, 8, 27), date(2026, 8, 28))
        self.values = [
            Calendar(date=session.isoformat(), open="09:30", close="16:00") for session in sessions
        ]
        self.requests = []

    def get_calendar(self, filters=None):
        self.requests.append(filters)
        return self.values


def _daily_bar(symbol: str, session: date, close: Decimal) -> dict[str, object]:
    started_at = datetime.combine(session, time(0), tzinfo=NEW_YORK).astimezone(UTC)
    return {
        "t": started_at,
        "o": float(close),
        "h": float(close + 1),
        "l": float(close - 1),
        "c": float(close),
        "v": 1000,
        "n": 100,
        "vw": float(close),
    }


def _intraday_bar(
    symbol: str,
    started_at: datetime,
    *,
    high: Decimal = Decimal("101"),
    low: Decimal = Decimal("99"),
    close: Decimal = Decimal("100"),
) -> dict[str, object]:
    return {
        "t": started_at,
        "o": 100.0,
        "h": float(high),
        "l": float(low),
        "c": float(close),
        "v": 1000,
        "n": 100,
        "vw": 100.0,
    }


class Stocks:
    def __init__(self) -> None:
        benchmark_price = Decimal("100")
        underlying_price = Decimal("100")
        underlying_daily = []
        benchmark_daily = []
        for index, session in enumerate(DAILY_SESSIONS):
            if index:
                benchmark_return = Decimal("0.01") if index % 2 == 0 else Decimal("-0.01")
                benchmark_price *= Decimal(1) + benchmark_return
                underlying_price *= Decimal(1) + Decimal(2) * benchmark_return
            underlying_daily.append(_daily_bar("NVDA", session, underlying_price))
            benchmark_daily.append(_daily_bar("QQQ", session, benchmark_price))
        underlying_daily.append(
            _daily_bar("NVDA", date(2026, 8, 27), underlying_price * Decimal("1.08"))
        )
        starts = tuple(
            datetime(2026, 8, 28, 13, 30, tzinfo=UTC) + index * timedelta(minutes=5)
            for index in range(30)
        )
        underlying_intraday = [_intraday_bar("NVDA", started_at) for started_at in starts]
        for offset in range(4):
            index = 26 + offset
            underlying_intraday[index] = _intraday_bar(
                "NVDA",
                starts[index],
                high=Decimal("101") + Decimal(offset) / 10,
                low=Decimal("99") + Decimal(offset) / 10,
                close=Decimal("101") if offset == 3 else Decimal("100"),
            )
        self.daily = {
            "NVDA": underlying_daily,
            "QQQ": benchmark_daily,
        }
        self.intraday = {
            "NVDA": underlying_intraday,
            "QQQ": [_intraday_bar("QQQ", started_at) for started_at in starts],
        }
        self.requests = []

    def get_stock_bars(self, request_params):
        self.requests.append(request_params)
        symbol = request_params.symbol_or_symbols
        values = (
            self.daily[symbol]
            if request_params.timeframe.unit is TimeFrameUnit.Day
            else self.intraday[symbol]
        )
        return BarSet({symbol: values})


def _collector(*, calculator=None):
    calendars = Calendars()
    stocks = Stocks()
    target = (
        AlpacaOpportunitySignalCollector(calendars, stocks)
        if calculator is None
        else AlpacaOpportunitySignalCollector(calendars, stocks, calculator=calculator)
    )
    return target, calendars, stocks


def _collect(target: AlpacaOpportunitySignalCollector):
    return target.collect(
        _request(),
        policy=_policy(),
        snapshot_source_hash=SNAPSHOT_HASH,
        observed_at=OBSERVED_AT,
    )


def test_collects_complete_hash_bound_inputs_then_calculates() -> None:
    target, calendars, stocks = _collector()

    result = _collect(target)

    assert result.direction is OpportunityDirection.BULLISH
    assert result.snapshot_source_hash == SNAPSHOT_HASH
    assert len(calendars.requests) == 1
    assert calendars.requests[0].start == DAILY_SESSIONS[0]
    assert calendars.requests[0].end == date(2026, 8, 28)
    assert len(stocks.requests) == 4
    underlying_daily, benchmark_daily, underlying_intraday, benchmark_intraday = stocks.requests
    assert underlying_daily.symbol_or_symbols == "NVDA"
    assert benchmark_daily.symbol_or_symbols == "QQQ"
    assert all(item.adjustment is Adjustment.ALL for item in stocks.requests[:2])
    assert all(item.feed is DataFeed.IEX for item in stocks.requests)
    assert all(item.sort is Sort.ASC for item in stocks.requests)
    assert all(item.limit == 64 for item in stocks.requests[:2])
    assert all(item.adjustment is Adjustment.SPLIT for item in stocks.requests[2:])
    assert all(item.limit == 78 for item in stocks.requests[2:])
    assert underlying_intraday.symbol_or_symbols == "NVDA"
    assert benchmark_intraday.symbol_or_symbols == "QQQ"
    assert underlying_daily.start == datetime.combine(
        DAILY_SESSIONS[0], time(0), NEW_YORK
    ).astimezone(UTC).replace(tzinfo=None)
    assert underlying_daily.end == datetime(2026, 8, 28, 4)
    assert benchmark_daily.end == datetime(2026, 8, 27, 4)
    assert underlying_intraday.start == datetime(2026, 8, 28, 13, 30)
    assert underlying_intraday.end == BOUNDARY.replace(tzinfo=None)


def test_calculator_receives_only_recomputed_evidence_after_five_provider_calls() -> None:
    captured = {}

    def calculator(**values):
        captured.update(values)
        return "complete"

    target, calendars, stocks = _collector(calculator=calculator)

    result = _collect(target)

    assert result == "complete"
    assert len(calendars.requests) + len(stocks.requests) == 5
    calendar = captured["calendar"]
    assert calendar.source_hash == signal_calendar_digest(calendar)
    assert all(
        item.source_hash == signal_daily_close_digest(item)
        for item in (*captured["underlying_daily_closes"], *captured["benchmark_daily_closes"])
    )
    assert captured["first_reaction_close"].source_hash == signal_daily_close_digest(
        captured["first_reaction_close"]
    )
    assert all(
        item.source_hash == signal_bar_digest(item)
        for item in (*captured["underlying_bars"], *captured["benchmark_bars"])
    )


def test_runtime_adapter_preserves_collected_signal_component_authority() -> None:
    target, _, _ = _collector()
    signal_request = _request()
    policy = _policy()

    class RecordingCollector:
        evidence = None

        def collect_evidence(self, *args, **kwargs):
            self.evidence = target.collect_evidence(*args, **kwargs)
            return self.evidence

    recording = RecordingCollector()

    result = OpportunitySignalRuntimeAdapter(recording).collect(
        signal_request,
        policy=policy,
        snapshot_source_hash=SNAPSHOT_HASH,
        observed_at=OBSERVED_AT,
    )

    assert result.authority.snapshot_source_hash == SNAPSHOT_HASH
    assert recording.evidence is not None
    assert result.calendar_hash == recording.evidence.calendar.source_hash
    assert result.daily_hash == recording.evidence.daily_source_hash
    assert result.intraday_hash == recording.evidence.intraday_source_hash


def test_runtime_adapter_rejects_tampered_component_authority() -> None:
    target, _, _ = _collector()
    signal_request = _request()
    policy = _policy()
    collected = target.collect_evidence(
        signal_request,
        policy=policy,
        snapshot_source_hash=SNAPSHOT_HASH,
        observed_at=OBSERVED_AT,
    )

    class TamperedCollector:
        def collect_evidence(self, *_args, **_kwargs):
            return replace(collected, daily_source_hash="f" * 64)

    assert collected.daily_source_hash == opportunity_signal_daily_evidence_digest(
        collected.underlying_daily_closes,
        collected.benchmark_daily_closes,
        collected.first_reaction_close,
    )
    assert collected.intraday_source_hash == opportunity_signal_intraday_evidence_digest(
        collected.underlying_bars,
        collected.benchmark_bars,
    )
    with pytest.raises(
        OpportunityRuntimeAdapterError,
        match="OPPORTUNITY_SIGNAL_AUTHORITY_INVALID",
    ):
        OpportunitySignalRuntimeAdapter(TamperedCollector()).collect(
            signal_request,
            policy=policy,
            snapshot_source_hash=SNAPSHOT_HASH,
            observed_at=OBSERVED_AT,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"account_role": AccountRole.REPLAY},
        {"benchmark": "SPY"},
        {"underlying": "QQQ"},
        {"maximum_calendar_span_days": 121},
        {"maximum_daily_bars_per_symbol": 65},
        {"maximum_daily_bars_per_symbol": 61},
        {"maximum_intraday_bars_per_symbol": 79},
        {"maximum_provider_calls": 6},
        {"signal_boundary": BOUNDARY + timedelta(seconds=1)},
    ],
)
def test_invalid_request_is_rejected_before_provider_calls(changes: dict[str, object]) -> None:
    with pytest.raises(OpportunitySignalCollectionError, match="SIGNAL_REQUEST_INVALID"):
        _request(**changes)


@pytest.mark.parametrize(
    ("policy", "source_hash"),
    [
        (_policy(underlying="AMD"), SNAPSHOT_HASH),
        (_policy(selected_decision_boundary=BOUNDARY + timedelta(minutes=5)), SNAPSHOT_HASH),
        (_policy(), "not-a-hash"),
    ],
)
def test_mismatched_authority_makes_no_provider_calls(policy, source_hash: str) -> None:
    target, calendars, stocks = _collector()

    with pytest.raises(OpportunitySignalCollectionError, match="SIGNAL_AUTHORITY_MISMATCH"):
        target.collect(
            _request(),
            policy=policy,
            snapshot_source_hash=source_hash,
            observed_at=OBSERVED_AT,
        )

    assert calendars.requests == []
    assert stocks.requests == []


@pytest.mark.parametrize(
    "observed_at",
    [
        BOUNDARY - timedelta(microseconds=1),
        BOUNDARY + timedelta(minutes=2, microseconds=1),
        BOUNDARY.replace(tzinfo=None),
    ],
)
def test_invalid_observation_time_makes_no_provider_calls(observed_at: datetime) -> None:
    target, calendars, stocks = _collector()

    with pytest.raises(
        OpportunitySignalCollectionError,
        match="SIGNAL_OBSERVATION_TIME_INVALID",
    ):
        target.collect(
            _request(),
            policy=_policy(),
            snapshot_source_hash=SNAPSHOT_HASH,
            observed_at=observed_at,
        )

    assert calendars.requests == []
    assert stocks.requests == []


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "wrong_reaction", "extra"])
def test_calendar_gaps_duplicates_and_excess_fail_before_bar_calls(mutation: str) -> None:
    target, calendars, stocks = _collector()
    if mutation == "duplicate":
        calendars.values[2] = calendars.values[1]
    elif mutation == "missing":
        calendars.values.pop(2)
    elif mutation == "wrong_reaction":
        calendars.values[-2] = Calendar(date="2026-08-25", open="09:30", close="16:00")
    else:
        calendars.values.insert(1, Calendar(date="2026-01-02", open="09:30", close="16:00"))

    with pytest.raises(OpportunitySignalCollectionError, match="SIGNAL_CALENDAR"):
        _collect(target)

    assert stocks.requests == []


@pytest.mark.parametrize(
    "session",
    [
        Calendar(date="2026-08-28", open="09:31", close="16:00"),
        Calendar(date="2026-08-28", open="09:30", close="15:59"),
        Calendar(date="2026-08-28", open="16:00", close="13:00"),
    ],
)
def test_calendar_requires_exact_regular_or_declared_early_close(session: Calendar) -> None:
    target, calendars, stocks = _collector()
    calendars.values[-1] = session

    with pytest.raises(OpportunitySignalCollectionError, match="SIGNAL_CALENDAR_SESSION_INVALID"):
        _collect(target)

    assert stocks.requests == []


@pytest.mark.parametrize("field", ["open", "close"])
def test_calendar_rejects_sdk_model_with_mismatched_embedded_date(field: str) -> None:
    target, calendars, stocks = _collector()
    session = calendars.values[-1]
    calendars.values[-1] = session.model_copy(
        update={field: getattr(session, field) - timedelta(days=1)}
    )

    with pytest.raises(OpportunitySignalCollectionError, match="SIGNAL_CALENDAR_SESSION_INVALID"):
        _collect(target)

    assert stocks.requests == []


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "future", "excess"])
def test_daily_bar_count_and_chronology_fail_before_calculation(mutation: str) -> None:
    called = False

    def calculator(**_values):
        nonlocal called
        called = True

    target, _, stocks = _collector(calculator=calculator)
    values = stocks.daily["NVDA"]
    if mutation == "missing":
        values.pop(3)
    elif mutation == "duplicate":
        values[3] = values[2]
    elif mutation == "future":
        values[-1] = _daily_bar("NVDA", date(2026, 8, 28), Decimal("108"))
    else:
        values.append(_daily_bar("NVDA", date(2026, 8, 28), Decimal("109")))

    with pytest.raises(OpportunitySignalCollectionError, match="SIGNAL_DAILY_BAR"):
        _collect(target)

    assert called is False


def test_daily_timestamp_accepts_the_exact_session_midnight_in_an_offset_zone() -> None:
    target, _, stocks = _collector(calculator=lambda **values: values)
    raw = stocks.daily["NVDA"][0]
    expected = datetime.combine(DAILY_SESSIONS[0], time(0), NEW_YORK)
    raw["t"] = expected

    result = _collect(target)

    assert result["underlying_daily_closes"][0].session_date == DAILY_SESSIONS[0]


def test_daily_timestamp_rejects_utc_midnight_when_it_is_not_new_york_midnight() -> None:
    called = False

    def calculator(**_values):
        nonlocal called
        called = True

    target, _, stocks = _collector(calculator=calculator)
    stocks.daily["NVDA"][0]["t"] = datetime.combine(DAILY_SESSIONS[0], time(0), UTC)

    with pytest.raises(
        OpportunitySignalCollectionError,
        match="SIGNAL_DAILY_BAR_CHRONOLOGY_INVALID",
    ):
        _collect(target)

    assert called is False


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "gap", "post_boundary"])
def test_intraday_bar_count_gaps_and_future_data_fail_closed(mutation: str) -> None:
    called = False

    def calculator(**_values):
        nonlocal called
        called = True

    target, _, stocks = _collector(calculator=calculator)
    values = stocks.intraday["NVDA"]
    if mutation == "missing":
        values.pop(4)
    elif mutation == "duplicate":
        values[4] = values[3]
    elif mutation == "gap":
        values[4] = _intraday_bar("NVDA", values[4]["t"] + timedelta(minutes=5))
    else:
        values[-1] = _intraday_bar("NVDA", BOUNDARY)

    with pytest.raises(OpportunitySignalCollectionError, match="SIGNAL_INTRADAY_BAR"):
        _collect(target)

    assert called is False


def test_early_close_boundary_uses_declared_session_and_exact_count() -> None:
    request = _request(signal_boundary=datetime(2026, 8, 28, 17, tzinfo=UTC))
    policy = _policy(
        selected_decision_boundary=request.signal_boundary,
        last_entry_boundary=request.signal_boundary + timedelta(minutes=30),
    )
    target, calendars, stocks = _collector(calculator=lambda **values: values)
    calendars.values[-1] = Calendar(date="2026-08-28", open="09:30", close="13:00")
    for symbol in ("NVDA", "QQQ"):
        stocks.intraday[symbol] = [
            _intraday_bar(
                symbol,
                datetime(2026, 8, 28, 13, 30, tzinfo=UTC) + index * timedelta(minutes=5),
            )
            for index in range(42)
        ]

    result = target.collect(
        request,
        policy=policy,
        snapshot_source_hash=SNAPSHOT_HASH,
        observed_at=request.signal_boundary + timedelta(seconds=20),
    )

    assert result["calendar"].signal_close_at == datetime(2026, 8, 28, 17, tzinfo=UTC)
    assert len(result["underlying_bars"]) == 42


def test_raw_numeric_and_symbol_mismatches_are_rejected() -> None:
    target, _, stocks = _collector()
    stocks.intraday["QQQ"][0]["vw"] = 500.0

    with pytest.raises(OpportunitySignalCollectionError, match="SIGNAL_INTRADAY_BAR_INVALID"):
        _collect(target)


@pytest.mark.parametrize("failed_call", range(1, 6))
def test_client_exception_stops_the_exact_five_call_sequence(failed_call: int) -> None:
    calculator_called = False

    def calculator(**_values):
        nonlocal calculator_called
        calculator_called = True

    target, calendars, stocks = _collector(calculator=calculator)
    provider_calls = 0
    calendar_call = calendars.get_calendar
    stock_call = stocks.get_stock_bars

    def calendar(filters=None):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == failed_call:
            raise RuntimeError("provider unavailable")
        return calendar_call(filters)

    def stock(request_params):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == failed_call:
            raise RuntimeError("provider unavailable")
        return stock_call(request_params)

    calendars.get_calendar = calendar
    stocks.get_stock_bars = stock

    with pytest.raises(RuntimeError, match="provider unavailable"):
        _collect(target)

    assert provider_calls == failed_call
    assert calculator_called is False


@pytest.mark.parametrize("mutation", ["wrong_key", "extra_key", "reversed"])
def test_provider_response_keys_and_order_are_exact(mutation: str) -> None:
    called = False

    def calculator(**_values):
        nonlocal called
        called = True

    target, _, stocks = _collector(calculator=calculator)
    original = stocks.get_stock_bars
    response_number = 0

    def stock(request_params):
        nonlocal response_number
        response_number += 1
        raw = original(request_params)
        if response_number != 1:
            return raw
        values = stocks.daily["NVDA"]
        if mutation == "wrong_key":
            return BarSet({"AMD": values})
        if mutation == "extra_key":
            return BarSet({"NVDA": values, "AMD": []})
        return BarSet({"NVDA": list(reversed(values))})

    stocks.get_stock_bars = stock

    with pytest.raises(OpportunitySignalCollectionError, match="SIGNAL_DAILY"):
        _collect(target)

    assert called is False
