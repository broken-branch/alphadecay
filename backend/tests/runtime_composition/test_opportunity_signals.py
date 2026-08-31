from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.contracts.v1 import AccountRole
from backend.app.policy import OpportunityDirection, OpportunityPolicy
from backend.app.services.opportunity_signals import (
    BENCHMARK_SYMBOL,
    OpportunitySignalCalculationError,
    SignalBar,
    SignalCalendar,
    SignalDailyClose,
    SignalPriceAdjustment,
    calculate_opportunity_signals,
    signal_bar_digest,
    signal_calendar_digest,
    signal_daily_close_digest,
)

BOUNDARY = datetime(2026, 8, 28, 16, tzinfo=UTC)
OBSERVED_AT = BOUNDARY + timedelta(seconds=20)
SNAPSHOT_HASH = "a" * 64


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


def _business_sessions(count: int, through: date) -> tuple[date, ...]:
    result = []
    current = through
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current -= timedelta(days=1)
    return tuple(reversed(result))


def _calendar() -> SignalCalendar:
    value = SignalCalendar(
        daily_sessions=_business_sessions(61, date(2026, 8, 26)),
        pre_event_cutoff=date(2026, 8, 26),
        first_reaction_session=date(2026, 8, 27),
        signal_session=date(2026, 8, 28),
        signal_open_at=datetime(2026, 8, 28, 13, 30, tzinfo=UTC),
        signal_close_at=datetime(2026, 8, 28, 20, tzinfo=UTC),
        source_hash="",
    )
    return replace(value, source_hash=signal_calendar_digest(value))


def _close(symbol: str, session: date, close: Decimal) -> SignalDailyClose:
    value = SignalDailyClose(symbol, session, close, SignalPriceAdjustment.ALL, "")
    return replace(value, source_hash=signal_daily_close_digest(value))


def _paired_closes(
    *, underlying_scale: Decimal = Decimal("2"), benchmark_constant: bool = False
) -> tuple[tuple[SignalDailyClose, ...], tuple[SignalDailyClose, ...]]:
    underlying_price = Decimal("100")
    benchmark_price = Decimal("100")
    underlying = []
    benchmark = []
    for index, session in enumerate(_calendar().daily_sessions):
        if index:
            benchmark_return = (
                Decimal(0)
                if benchmark_constant
                else Decimal("0.01")
                if index % 2 == 0
                else Decimal("-0.01")
            )
            benchmark_price *= Decimal(1) + benchmark_return
            underlying_price *= Decimal(1) + underlying_scale * benchmark_return
        underlying.append(_close("NVDA", session, underlying_price))
        benchmark.append(_close(BENCHMARK_SYMBOL, session, benchmark_price))
    return tuple(underlying), tuple(benchmark)


def _bar(
    symbol: str,
    started_at: datetime,
    *,
    open_: Decimal = Decimal("100"),
    high: Decimal = Decimal("101"),
    low: Decimal = Decimal("99"),
    close: Decimal = Decimal("100"),
    volume: Decimal = Decimal("1"),
    vwap: Decimal = Decimal("100"),
) -> SignalBar:
    value = SignalBar(
        symbol,
        started_at,
        started_at + timedelta(minutes=5),
        open_,
        high,
        low,
        close,
        volume,
        vwap,
        SignalPriceAdjustment.SPLIT,
        "",
    )
    return replace(value, source_hash=signal_bar_digest(value))


def _bars(
    symbol: str,
    *,
    direction: OpportunityDirection | None = OpportunityDirection.BULLISH,
    benchmark_move: Decimal = Decimal(0),
) -> tuple[SignalBar, ...]:
    start = datetime(2026, 8, 28, 13, 30, tzinfo=UTC)
    bars = [_bar(symbol, start + index * timedelta(minutes=5)) for index in range(30)]
    if symbol == BENCHMARK_SYMBOL:
        bars[-1] = _bar(
            symbol,
            bars[-1].started_at,
            high=max(Decimal("101"), Decimal("100") * (Decimal(1) + benchmark_move)),
            low=min(Decimal("99"), Decimal("100") * (Decimal(1) + benchmark_move)),
            close=Decimal("100") * (Decimal(1) + benchmark_move),
        )
        return tuple(bars)
    if direction is OpportunityDirection.BULLISH:
        for offset in range(4):
            index = 26 + offset
            bars[index] = _bar(
                symbol,
                bars[index].started_at,
                high=Decimal("101") + Decimal(offset) / 10,
                low=Decimal("99") + Decimal(offset) / 10,
                close=Decimal("101") if offset == 3 else Decimal("100"),
            )
    elif direction is OpportunityDirection.BEARISH:
        for offset in range(4):
            index = 26 + offset
            bars[index] = _bar(
                symbol,
                bars[index].started_at,
                high=Decimal("101") - Decimal(offset) / 10,
                low=Decimal("99") - Decimal(offset) / 10,
                close=Decimal("99") if offset == 3 else Decimal("100"),
            )
    return tuple(bars)


def _calculate(**changes: object):
    underlying_daily, benchmark_daily = _paired_closes()
    reaction = _close(
        "NVDA", _calendar().first_reaction_session, underlying_daily[-1].close * Decimal("1.08")
    )
    values: dict[str, object] = {
        "account_role": AccountRole.DEVELOPMENT,
        "policy": _policy(),
        "snapshot_source_hash": SNAPSHOT_HASH,
        "observed_at": OBSERVED_AT,
        "calendar": _calendar(),
        "underlying_daily_closes": underlying_daily,
        "benchmark_daily_closes": benchmark_daily,
        "first_reaction_close": reaction,
        "underlying_bars": _bars("NVDA"),
        "benchmark_bars": _bars(BENCHMARK_SYMBOL),
    }
    values.update(changes)
    return calculate_opportunity_signals(**values)  # type: ignore[arg-type]


def test_hand_calculated_bullish_signals_and_cutoff_authority() -> None:
    result = _calculate()
    underlying, benchmark = _paired_closes()
    underlying_returns = tuple(
        underlying[index].close / underlying[index - 1].close - Decimal(1) for index in range(1, 61)
    )
    benchmark_returns = tuple(
        benchmark[index].close / benchmark[index - 1].close - Decimal(1) for index in range(1, 61)
    )
    underlying_mean = sum(underlying_returns, Decimal(0)) / 60
    benchmark_mean = sum(benchmark_returns, Decimal(0)) / 60
    sample_covariance = (
        sum(
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
        / 59
    )
    sample_variance = (
        sum(
            ((value - benchmark_mean) ** 2 for value in benchmark_returns),
            Decimal(0),
        )
        / 59
    )

    assert result.beta.value == Decimal("2")
    assert abs(result.beta.value - sample_covariance / sample_variance) < Decimal("1e-24")
    assert result.vwap_distance.value == Decimal("0.01")
    assert result.relative_return.value == Decimal("0.01")
    assert (result.trend.bull_hits, result.trend.bear_hits) == (3, 0)
    assert result.absolute_first_reaction.value == Decimal("0.08")
    assert result.direction is OpportunityDirection.BULLISH
    assert result.pre_event_cutoff == date(2026, 8, 26)
    assert all(
        len(value.source_hash) == 64
        for value in (
            result.beta,
            result.vwap_distance,
            result.relative_return,
            result.trend,
            result.absolute_first_reaction,
        )
    )


def test_session_vwap_is_bar_vwap_weighted_by_bar_volume() -> None:
    bars = list(_bars("NVDA"))
    bars[-1] = _bar(
        "NVDA",
        bars[-1].started_at,
        high=Decimal("102"),
        low=Decimal("99.3"),
        close=Decimal("101"),
        volume=Decimal("2"),
        vwap=Decimal("102"),
    )

    result = _calculate(underlying_bars=tuple(bars))

    expected_session_vwap = Decimal(3104) / Decimal(31)
    assert result.vwap_distance.value == Decimal(101) / expected_session_vwap - Decimal(1)


def test_equal_lows_and_highs_count_for_both_four_bar_trends() -> None:
    bars = list(_bars("NVDA", direction=None))
    bars[-1] = _bar("NVDA", bars[-1].started_at, close=Decimal("101"))

    result = _calculate(underlying_bars=tuple(bars))

    assert (result.trend.bull_hits, result.trend.bear_hits) == (3, 3)
    assert result.direction is OpportunityDirection.BULLISH


def test_hand_calculated_bearish_signals() -> None:
    result = _calculate(underlying_bars=_bars("NVDA", direction=OpportunityDirection.BEARISH))

    assert result.vwap_distance.value == Decimal("-0.01")
    assert result.relative_return.value == Decimal("-0.01")
    assert (result.trend.bull_hits, result.trend.bear_hits) == (0, 3)
    assert result.direction is OpportunityDirection.BEARISH


def test_direction_is_derived_as_none_when_relative_return_does_not_confirm() -> None:
    result = _calculate(benchmark_bars=_bars(BENCHMARK_SYMBOL, benchmark_move=Decimal("0.005")))

    assert result.vwap_distance.value == Decimal("0.01")
    assert result.relative_return.value == Decimal("0.000")
    assert result.direction is None


def test_cutoff_change_changes_beta_and_calculation_authority_hashes() -> None:
    original = _calculate()
    calendar = _calendar()
    shifted_sessions = _business_sessions(61, date(2026, 8, 25))
    shifted = SignalCalendar(
        shifted_sessions,
        shifted_sessions[-1],
        calendar.first_reaction_session,
        calendar.signal_session,
        calendar.signal_open_at,
        calendar.signal_close_at,
        "",
    )
    shifted = replace(shifted, source_hash=signal_calendar_digest(shifted))
    underlying, benchmark = _paired_closes()
    shifted_underlying = tuple(
        _close(item.symbol, session, item.close)
        for item, session in zip(underlying, shifted_sessions, strict=True)
    )
    shifted_benchmark = tuple(
        _close(item.symbol, session, item.close)
        for item, session in zip(benchmark, shifted_sessions, strict=True)
    )
    reaction = _close(
        "NVDA", shifted.first_reaction_session, shifted_underlying[-1].close * Decimal("1.08")
    )

    changed = _calculate(
        calendar=shifted,
        underlying_daily_closes=shifted_underlying,
        benchmark_daily_closes=shifted_benchmark,
        first_reaction_close=reaction,
    )

    assert changed.beta.value == original.beta.value
    assert changed.beta.source_hash != original.beta.source_hash
    assert changed.trend.source_hash != original.trend.source_hash
    assert changed.source_hash != original.source_hash


def test_post_cutoff_price_mutation_cannot_change_beta_authority() -> None:
    original = _calculate()
    underlying, _ = _paired_closes()
    changed_reaction = _close(
        "NVDA",
        _calendar().first_reaction_session,
        underlying[-1].close * Decimal("1.09"),
    )
    changed_bars = list(_bars("NVDA"))
    changed_bars[-1] = _bar(
        "NVDA",
        changed_bars[-1].started_at,
        high=Decimal("102"),
        low=Decimal("99.3"),
        close=Decimal("101.5"),
    )

    changed = _calculate(
        first_reaction_close=changed_reaction,
        underlying_bars=tuple(changed_bars),
    )

    assert changed.beta == original.beta
    assert changed.absolute_first_reaction.value == Decimal("0.09")
    assert changed.source_hash != original.source_hash


def test_declared_cutoff_must_equal_last_beta_close_session() -> None:
    calendar = replace(
        _calendar(),
        pre_event_cutoff=date(2026, 8, 25),
        source_hash="",
    )
    calendar = replace(calendar, source_hash=signal_calendar_digest(calendar))

    with pytest.raises(OpportunitySignalCalculationError, match="CALENDAR_CHRONOLOGY_INVALID"):
        _calculate(calendar=calendar)


def test_explicit_half_day_close_bounds_the_decision_session() -> None:
    calendar = replace(
        _calendar(),
        signal_close_at=datetime(2026, 8, 28, 17, tzinfo=UTC),
        source_hash="",
    )
    calendar = replace(calendar, source_hash=signal_calendar_digest(calendar))

    result = _calculate(calendar=calendar)

    assert result.direction is OpportunityDirection.BULLISH


def test_decision_boundary_after_declared_half_day_close_is_rejected() -> None:
    calendar = replace(
        _calendar(),
        signal_close_at=datetime(2026, 8, 28, 17, tzinfo=UTC),
        source_hash="",
    )
    calendar = replace(calendar, source_hash=signal_calendar_digest(calendar))
    boundary = datetime(2026, 8, 28, 18, tzinfo=UTC)

    with pytest.raises(OpportunitySignalCalculationError, match="CALENDAR_CHRONOLOGY_INVALID"):
        _calculate(
            calendar=calendar,
            policy=_policy(
                selected_decision_boundary=boundary,
                last_entry_boundary=boundary + timedelta(minutes=30),
            ),
            observed_at=boundary + timedelta(seconds=20),
        )


@pytest.mark.parametrize("role", [AccountRole.REPLAY])
def test_replay_role_is_rejected(role: AccountRole) -> None:
    with pytest.raises(OpportunitySignalCalculationError, match="ACCOUNT_ROLE_NOT_DEVELOPMENT"):
        _calculate(account_role=role)


def test_calendar_hash_tamper_is_rejected() -> None:
    with pytest.raises(OpportunitySignalCalculationError, match="CALENDAR_EVIDENCE_INVALID"):
        _calculate(calendar=replace(_calendar(), source_hash="f" * 64))


def test_duplicate_or_missing_daily_session_is_rejected() -> None:
    calendar = _calendar()
    duplicated = replace(
        calendar,
        daily_sessions=(*calendar.daily_sessions[:-1], calendar.daily_sessions[-2]),
        source_hash="",
    )
    duplicated = replace(duplicated, source_hash=signal_calendar_digest(duplicated))
    with pytest.raises(OpportunitySignalCalculationError, match="CALENDAR_CHRONOLOGY_INVALID"):
        _calculate(calendar=duplicated)


def test_weekend_signal_session_is_rejected_before_bar_calculation() -> None:
    calendar = replace(
        _calendar(),
        signal_session=date(2026, 8, 29),
        signal_open_at=datetime(2026, 8, 29, 13, 30, tzinfo=UTC),
        signal_close_at=datetime(2026, 8, 29, 20, tzinfo=UTC),
        source_hash="",
    )
    calendar = replace(calendar, source_hash=signal_calendar_digest(calendar))
    boundary = datetime(2026, 8, 29, 16, tzinfo=UTC)

    with pytest.raises(OpportunitySignalCalculationError, match="CALENDAR_CHRONOLOGY_INVALID"):
        _calculate(
            calendar=calendar,
            policy=_policy(
                selected_decision_boundary=boundary,
                last_entry_boundary=boundary + timedelta(minutes=30),
            ),
            observed_at=boundary + timedelta(seconds=20),
        )


def test_daily_close_gap_against_calendar_is_rejected() -> None:
    underlying, _ = _paired_closes()
    missing = underlying[:-1]
    with pytest.raises(OpportunitySignalCalculationError, match="DAILY_CLOSE_COUNT_INVALID"):
        _calculate(underlying_daily_closes=missing)


def test_daily_close_date_substitution_is_rejected() -> None:
    underlying, _ = _paired_closes()
    changed = list(underlying)
    changed[20] = _close("NVDA", changed[20].session_date + timedelta(days=1), changed[20].close)
    with pytest.raises(OpportunitySignalCalculationError, match="DAILY_CLOSE_CALENDAR_MISMATCH"):
        _calculate(underlying_daily_closes=tuple(changed))


def test_daily_close_value_tamper_without_new_hash_is_rejected() -> None:
    underlying, _ = _paired_closes()
    changed = list(underlying)
    changed[20] = replace(changed[20], close=changed[20].close + Decimal("1"))
    with pytest.raises(OpportunitySignalCalculationError, match="DAILY_CLOSE_EVIDENCE_INVALID"):
        _calculate(underlying_daily_closes=tuple(changed))


def test_unadjusted_daily_close_substitution_is_rejected() -> None:
    underlying, _ = _paired_closes()
    changed = list(underlying)
    raw = replace(
        changed[20],
        adjustment=SignalPriceAdjustment.RAW,
        source_hash="",
    )
    changed[20] = replace(raw, source_hash=signal_daily_close_digest(raw))

    with pytest.raises(OpportunitySignalCalculationError, match="DAILY_CLOSE_EVIDENCE_INVALID"):
        _calculate(underlying_daily_closes=tuple(changed))


@pytest.mark.parametrize("close", [Decimal("Infinity"), Decimal("1000000000.01")])
def test_daily_close_nonfinite_or_above_cap_is_rejected(close: Decimal) -> None:
    underlying, _ = _paired_closes()
    changed = list(underlying)
    changed[20] = SignalDailyClose(
        "NVDA",
        changed[20].session_date,
        close,
        SignalPriceAdjustment.ALL,
        "",
    )

    with pytest.raises(OpportunitySignalCalculationError, match="DAILY_CLOSE_EVIDENCE_INVALID"):
        _calculate(underlying_daily_closes=tuple(changed))


def test_benchmark_must_be_qqq() -> None:
    _, benchmark = _paired_closes()
    wrong = tuple(_close("SPY", item.session_date, item.close) for item in benchmark)
    with pytest.raises(OpportunitySignalCalculationError, match="DAILY_CLOSE_EVIDENCE_INVALID"):
        _calculate(benchmark_daily_closes=wrong)


def test_zero_benchmark_variance_is_rejected() -> None:
    underlying, benchmark = _paired_closes(benchmark_constant=True)
    reaction = _close(
        "NVDA", _calendar().first_reaction_session, underlying[-1].close * Decimal("1.08")
    )
    with pytest.raises(OpportunitySignalCalculationError, match="BENCHMARK_VARIANCE_NOT_POSITIVE"):
        _calculate(
            underlying_daily_closes=underlying,
            benchmark_daily_closes=benchmark,
            first_reaction_close=reaction,
        )


def test_beta_above_hard_cap_is_rejected() -> None:
    underlying, benchmark = _paired_closes(underlying_scale=Decimal("4"))
    reaction = _close(
        "NVDA", _calendar().first_reaction_session, underlying[-1].close * Decimal("1.08")
    )
    with pytest.raises(OpportunitySignalCalculationError, match="BETA_OUT_OF_BOUNDS"):
        _calculate(
            underlying_daily_closes=underlying,
            benchmark_daily_closes=benchmark,
            first_reaction_close=reaction,
        )


def test_first_reaction_must_be_exact_declared_session() -> None:
    underlying, _ = _paired_closes()
    reaction = _close("NVDA", date(2026, 8, 28), underlying[-1].close * Decimal("1.08"))
    with pytest.raises(OpportunitySignalCalculationError, match="FIRST_REACTION_EVIDENCE_INVALID"):
        _calculate(first_reaction_close=reaction)


def test_intraday_duplicate_or_gap_is_rejected() -> None:
    bars = list(_bars("NVDA"))
    bars[10] = bars[9]
    with pytest.raises(OpportunitySignalCalculationError, match="INTRADAY_BAR_CHRONOLOGY_INVALID"):
        _calculate(underlying_bars=tuple(bars))


def test_post_boundary_bar_is_rejected() -> None:
    bars = (*_bars("NVDA")[1:], _bar("NVDA", BOUNDARY))
    with pytest.raises(OpportunitySignalCalculationError, match="INTRADAY_BAR_CHRONOLOGY_INVALID"):
        _calculate(underlying_bars=bars)


def test_bar_completed_after_boundary_is_rejected() -> None:
    bars = list(_bars("NVDA"))
    changed = replace(bars[-1], completed_at=BOUNDARY + timedelta(minutes=5), source_hash="")
    bars[-1] = replace(changed, source_hash=signal_bar_digest(changed))

    with pytest.raises(OpportunitySignalCalculationError, match="INTRADAY_BAR_EVIDENCE_INVALID"):
        _calculate(underlying_bars=tuple(bars))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("volume", Decimal(0)),
        ("volume", Decimal("1000000000000001")),
        ("vwap", Decimal(0)),
        ("vwap", Decimal("1000000000.01")),
        ("close", Decimal("Infinity")),
    ],
)
def test_invalid_intraday_numeric_evidence_is_rejected(field: str, value: Decimal) -> None:
    bars = list(_bars("NVDA"))
    bars[4] = replace(bars[4], **{field: value})
    if value.is_finite():
        bars[4] = replace(bars[4], source_hash=signal_bar_digest(bars[4]))
    with pytest.raises(OpportunitySignalCalculationError, match="INTRADAY_BAR_EVIDENCE_INVALID"):
        _calculate(underlying_bars=tuple(bars))


def test_intraday_value_tamper_without_new_hash_is_rejected() -> None:
    bars = list(_bars("NVDA"))
    bars[4] = replace(bars[4], volume=Decimal("2"))
    with pytest.raises(OpportunitySignalCalculationError, match="INTRADAY_BAR_EVIDENCE_INVALID"):
        _calculate(underlying_bars=tuple(bars))


def test_non_split_adjusted_intraday_bar_substitution_is_rejected() -> None:
    bars = list(_bars("NVDA"))
    all_adjusted = replace(
        bars[4],
        adjustment=SignalPriceAdjustment.ALL,
        source_hash="",
    )
    bars[4] = replace(all_adjusted, source_hash=signal_bar_digest(all_adjusted))

    with pytest.raises(OpportunitySignalCalculationError, match="INTRADAY_BAR_EVIDENCE_INVALID"):
        _calculate(underlying_bars=tuple(bars))


def test_bar_vwap_outside_bar_range_is_rejected() -> None:
    bars = list(_bars("NVDA"))
    changed = replace(bars[4], vwap=Decimal("102"), source_hash="")
    bars[4] = replace(changed, source_hash=signal_bar_digest(changed))

    with pytest.raises(OpportunitySignalCalculationError, match="INTRADAY_BAR_EVIDENCE_INVALID"):
        _calculate(underlying_bars=tuple(bars))


def test_future_observation_outside_freshness_cap_is_rejected() -> None:
    with pytest.raises(OpportunitySignalCalculationError, match="OBSERVATION_TIME_INVALID"):
        _calculate(observed_at=BOUNDARY + timedelta(minutes=2, microseconds=1))
