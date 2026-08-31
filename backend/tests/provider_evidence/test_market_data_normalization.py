from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.app.alpaca.market_data import (
    MarketDataError,
    normalize_bars,
    normalize_option_snapshot,
)

NOW = datetime(2026, 8, 28, 15, 30, 20, tzinfo=UTC)


def option_fixture() -> dict[str, object]:
    return {
        "symbol": "NVDA260918C00230000",
        "underlying": "NVDA",
        "feed": "indicative",
        "retrieved_at": "2026-08-28T15:30:20Z",
        "quote": {
            "timestamp": "2026-08-28T15:30:10Z",
            "bid_price": "8.20",
            "ask_price": "8.40",
            "bid_size": "4",
            "ask_size": "7",
        },
        "greeks": {
            "delta": "0.55",
            "gamma": "0.031",
            "theta": "-0.18",
            "vega": "0.42",
        },
        "multiplier": "100",
    }


def test_option_snapshot_is_strict_fresh_and_keeps_raw_greek_units() -> None:
    snapshot = normalize_option_snapshot(option_fixture(), now=NOW)

    assert snapshot.provenance == "INDICATIVE_MODIFIED"
    assert snapshot.bid_price == Decimal("8.20")
    assert snapshot.ask_price == Decimal("8.40")
    assert snapshot.greeks.delta_per_share == Decimal("0.55")
    assert snapshot.greeks.theta_per_share_per_day == Decimal("-0.18")
    assert snapshot.greeks.quality == "DERIVED_UNTIMESTAMPED"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("feed", "opra", "OPTION_FEED_NOT_INDICATIVE"),
        ("quote.timestamp", "2026-08-28T15:29:40Z", "OPTION_QUOTE_STALE"),
        ("quote.timestamp", "2026-08-28T15:30:30Z", "PROVIDER_TIMESTAMP_FUTURE"),
        ("quote.bid_price", "8.50", "OPTION_QUOTE_CROSSED"),
    ],
)
def test_option_quality_failures_are_specific(field: str, value: str, code: str) -> None:
    payload = option_fixture()
    target: dict[str, object] = payload
    key = field
    if "." in field:
        parent, key = field.split(".")
        nested = payload[parent]
        assert isinstance(nested, dict)
        target = nested
    target[key] = value

    with pytest.raises(MarketDataError, match=code):
        normalize_option_snapshot(payload, now=NOW)


def test_completed_bars_are_sorted_and_reject_unknown_fields() -> None:
    raw = [
        {
            "timestamp": "2026-08-28T15:30:00Z",
            "open": "228.1",
            "high": "229.0",
            "low": "227.8",
            "close": "228.7",
            "volume": "4000",
        },
        {
            "timestamp": "2026-08-28T15:25:00Z",
            "open": "227.5",
            "high": "228.5",
            "low": "227.4",
            "close": "228.1",
            "volume": "3000",
        },
    ]

    bars = normalize_bars(raw, now=NOW)

    assert [bar.timestamp.minute for bar in bars] == [25, 30]
    raw[0]["trade_count"] = 99
    with pytest.raises(MarketDataError, match="BAR_SCHEMA_INVALID"):
        normalize_bars(raw, now=NOW)


def test_quote_cannot_be_newer_than_its_retrieval() -> None:
    payload = option_fixture()
    payload["retrieved_at"] = "2026-08-28T15:30:05Z"

    with pytest.raises(MarketDataError, match="OPTION_TIMESTAMP_INCONSISTENT"):
        normalize_option_snapshot(payload, now=NOW)
