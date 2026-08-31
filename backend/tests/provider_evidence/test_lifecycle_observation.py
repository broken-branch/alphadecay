from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.models import BarSet, OptionsSnapshot, Quote
from alpaca.trading.enums import AssetStatus, ContractType, ExerciseStyle
from alpaca.trading.models import Calendar, OptionContract

from backend.app.alpaca.execution_evidence import LifecycleAccountEvidence, LifecycleOptionEvidence
from backend.app.alpaca.market_data import (
    AlpacaLifecycleMarketDataCollector,
    LifecycleBoundaryAuthority,
    MarketDataError,
    NormalizedLifecycleMarketEvidence,
)
from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.execution import (
    ActivityItem,
    ActivityType,
    InventoryItem,
    InventoryKind,
    OpenOrderItem,
    OpenOrderLeg,
)
from backend.app.lifecycle.observation import (
    AlpacaLifecycleObservationAdapter,
    LifecycleObservationError,
)
from backend.app.services import (
    AlpacaMarketSession,
    AtmIvObservation,
    LifecycleBoundaryObservation,
    LifecycleOptionObservation,
    PriceConfirmationPoint,
    UnderlyingMarketObservation,
)
from backend.tests.runtime_composition.test_development_acquisition import (
    NOW,
    context,
    options,
    sweep,
)


class AccountCollector:
    def __init__(self, value: LifecycleAccountEvidence) -> None:
        self.value = value
        self.calls = 0

    def collect(self, **_kwargs: object) -> LifecycleAccountEvidence:
        self.calls += 1
        return self.value


class MarketCollector:
    def __init__(self, value: NormalizedLifecycleMarketEvidence) -> None:
        self.value = value
        self.calls = 0

    def collect(self, **_kwargs: object) -> NormalizedLifecycleMarketEvidence:
        self.calls += 1
        return self.value


class Sink:
    def __init__(self) -> None:
        self.accounts = []
        self.sessions = []

    def persist_account_observation(self, **values: object) -> None:
        self.accounts.append(values)

    def persist_market_session(self, **values: object) -> None:
        self.sessions.append(values)


def account_evidence() -> LifecycleAccountEvidence:
    option_evidence = tuple(
        LifecycleOptionEvidence(
            symbol=item.symbol,
            signed_quantity=item.signed_quantity,
            multiplier=item.multiplier,
            bid_price=item.bid_price,
            ask_price=item.ask_price,
            delta=item.delta,
            gamma=item.gamma,
            theta_per_day=item.theta_per_day,
            vega_per_iv_point=item.vega_per_iv_point,
            feed="indicative",
            source_timestamp=item.quote_observed_at,
            retrieved_at=item.retrieved_at,
            source_hash=item.source_hash,
        )
        for item in options()
    )
    return LifecycleAccountEvidence(sweep=sweep(), options=option_evidence)


def market_evidence() -> NormalizedLifecycleMarketEvidence:
    session = AlpacaMarketSession(
        market_session_id=UUID("00000000-0000-0000-0000-000000000401"),
        session_date=date(2026, 8, 31),
        open_at=NOW - timedelta(hours=2),
        close_at=NOW + timedelta(hours=5),
        source_hash="6" * 64,
        request_hash="1" * 64,
        retrieved_at=NOW - timedelta(seconds=2),
    )
    return NormalizedLifecycleMarketEvidence(
        underlying=UnderlyingMarketObservation(
            underlying="NVDA",
            bid_price=Decimal("180"),
            ask_price=Decimal("180.10"),
            quote_observed_at=NOW - timedelta(seconds=3),
            quote_retrieved_at=NOW - timedelta(seconds=2),
            quote_source_hash="3" * 64,
            completed_bar_at=NOW - timedelta(seconds=60),
            completed_bar_source_hash="5" * 64,
            request_hash="2" * 64,
            benchmark_symbol="QQQ",
            benchmark_completed_bar_at=NOW - timedelta(seconds=60),
            benchmark_completed_bar_source_hash="4" * 64,
        ),
        atm_iv=AtmIvObservation(
            underlying="NVDA",
            value=Decimal("0.40"),
            feed="indicative",
            observed_at=NOW - timedelta(seconds=3),
            retrieved_at=NOW - timedelta(seconds=2),
            source_hash="7" * 64,
            request_hash="8" * 64,
            call_source_hash="9" * 64,
            put_source_hash="a" * 64,
        ),
        boundaries=LifecycleBoundaryObservation(
            market_session=session,
            observed_at=NOW - timedelta(seconds=3),
            source_hash="b" * 64,
            price_confirmation=(
                PriceConfirmationPoint(
                    NOW - timedelta(seconds=110),
                    Decimal("0.01"),
                    Decimal("0.02"),
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                ),
                PriceConfirmationPoint(
                    NOW - timedelta(seconds=60),
                    Decimal("0.02"),
                    Decimal("0.03"),
                    "f" * 64,
                    "0" * 64,
                    "1" * 64,
                ),
            ),
            short_call_close_at=None,
            weekend_close_at=NOW + timedelta(days=1),
            contest_end_at=NOW + timedelta(days=2),
        ),
    )


def adapter(
    *,
    account: LifecycleAccountEvidence | None = None,
    market: NormalizedLifecycleMarketEvidence | None = None,
) -> tuple[AlpacaLifecycleObservationAdapter, AccountCollector, MarketCollector, Sink]:
    accounts = AccountCollector(account or account_evidence())
    markets = MarketCollector(market or market_evidence())
    sink = Sink()
    return AlpacaLifecycleObservationAdapter(accounts, markets, sink), accounts, markets, sink


def open_order() -> OpenOrderItem:
    return OpenOrderItem(
        provider_order_id="provider-1",
        client_order_id="client-1",
        state="NEW",
        quantity=1,
        filled_quantity=0,
        replaces_client_order_id=None,
        replaced_by_client_order_id=None,
        order_class="MLEG",
        legs=(
            OpenOrderLeg("NVDA260918C00170000", PositionIntent.SELL_TO_CLOSE, 1),
            OpenOrderLeg("NVDA260918C00180000", PositionIntent.BUY_TO_CLOSE, 1),
        ),
    )


def test_development_observation_assembles_and_persists_read_only_authority() -> None:
    target, accounts, markets, sink = adapter()

    result = target.observe(context(), NOW)

    assert result.options == tuple(
        LifecycleOptionObservation(
            symbol=item.symbol,
            signed_quantity=item.signed_quantity,
            multiplier=100,
            active=True,
            tradable=True,
            feed="indicative",
            bid_price=item.bid_price,
            ask_price=item.ask_price,
            delta=item.delta,
            gamma=item.gamma,
            theta_per_day=item.theta_per_day,
            vega_per_iv_point=item.vega_per_iv_point,
            quote_observed_at=item.quote_observed_at,
            greek_observed_at=item.quote_observed_at,
            retrieved_at=item.retrieved_at,
            greek_authority_id=context().greek_authority.authority_id,
            greek_timestamp_source_hash=context().greek_authority.timestamp_contract_hash,
            greek_units_source_hash=context().greek_authority.units_source_hash,
            source_hash=item.source_hash,
        )
        for item in options()
    )
    assert result.roll is None
    assert result.underlying == market_evidence().underlying
    assert accounts.calls == markets.calls == 1
    assert len(sink.accounts) == len(sink.sessions) == 1


def test_submission_is_rejected_before_any_read_or_persistence() -> None:
    target, accounts, markets, sink = adapter()

    with pytest.raises(LifecycleObservationError, match="DEVELOPMENT_AUTHORITY_REQUIRED"):
        target.observe(replace(context(), account_role=AccountRole.SUBMISSION), NOW)

    assert accounts.calls == markets.calls == 0
    assert not sink.accounts and not sink.sessions


@pytest.mark.parametrize(
    ("changed_sweep", "code"),
    (
        (
            replace(sweep(), final_account=replace(sweep().final_account, cash=Decimal("1"))),
            "ACCOUNT_BOOKEND_UNSTABLE",
        ),
        (replace(sweep(), final_open_orders=(open_order(),)), "OPEN_ORDER_EXISTS"),
        (
            replace(
                sweep(),
                first_positions=(InventoryItem(InventoryKind.EQUITY, "NVDA", Decimal("1"), 1),),
                final_positions=(InventoryItem(InventoryKind.EQUITY, "NVDA", Decimal("1"), 1),),
            ),
            "ACCOUNT_INVENTORY_INVALID",
        ),
        (
            replace(
                sweep(),
                activities=(
                    ActivityItem(
                        activity_id_hash="f" * 64,
                        activity_type=ActivityType.OPASN,
                        occurred_at=NOW - timedelta(seconds=3),
                        symbol="NVDA260918C00180000",
                        signed_quantity=Decimal("1"),
                    ),
                ),
            ),
            "ASSIGNMENT_ACTIVITY_PRESENT",
        ),
        (
            replace(
                sweep(),
                activity_pagination=replace(sweep().activity_pagination, terminal_page_seen=False),
            ),
            "ACTIVITY_PAGINATION_INCOMPLETE",
        ),
    ),
)
def test_unsafe_or_incomplete_account_evidence_fails_closed(changed_sweep, code: str) -> None:
    target, _, markets, sink = adapter(account=replace(account_evidence(), sweep=changed_sweep))

    with pytest.raises(LifecycleObservationError, match=code):
        target.observe(context(), NOW)

    assert markets.calls == 0
    assert not sink.accounts and not sink.sessions


@pytest.mark.parametrize(
    ("change", "code"),
    (
        ({"feed": "opra"}, "OPTION_FEED_NOT_INDICATIVE"),
        ({"source_timestamp": NOW - timedelta(seconds=31)}, "OPTION_EVIDENCE_STALE"),
    ),
)
def test_wrong_feed_or_stale_option_evidence_fails_closed(
    change: dict[str, object], code: str
) -> None:
    evidence = account_evidence()
    changed = (replace(evidence.options[0], **change), evidence.options[1])
    target, _, markets, sink = adapter(account=replace(evidence, options=changed))

    with pytest.raises(LifecycleObservationError, match=code):
        target.observe(context(), NOW)

    assert markets.calls == 0
    assert not sink.accounts and not sink.sessions


def test_missing_leg_fails_closed() -> None:
    evidence = account_evidence()
    target, _, markets, sink = adapter(account=replace(evidence, options=evidence.options[:1]))

    with pytest.raises(LifecycleObservationError, match="OPTION_EVIDENCE_INCOMPLETE"):
        target.observe(context(), NOW)

    assert markets.calls == 0
    assert not sink.accounts and not sink.sessions


def test_wrong_atm_feed_fails_before_market_session_persistence() -> None:
    evidence = market_evidence()
    target, _, _, sink = adapter(
        market=replace(evidence, atm_iv=replace(evidence.atm_iv, feed="opra"))
    )

    with pytest.raises(LifecycleObservationError, match="ATM_IV_FEED_NOT_INDICATIVE"):
        target.observe(context(), NOW)

    assert len(sink.accounts) == 1
    assert not sink.sessions


class OptionChain:
    def __init__(self, values: dict[str, OptionsSnapshot]) -> None:
        self.values = values
        self.request = None

    def get_option_chain(self, request_params):
        self.request = request_params
        return self.values


class NoContracts:
    def contracts_for(self, _symbols):
        raise AssertionError("no roll candidate should request contracts")


class RollChain:
    def __init__(self, current, replacement) -> None:
        self.current = current
        self.replacement = replacement
        self.requests = []

    def get_option_chain(self, request_params):
        self.requests.append(request_params)
        return self.current if request_params.expiration_date is not None else self.replacement


class RollContracts:
    def contracts_for(self, symbols):
        return {
            symbol: OptionContract(
                id=f"contract-{symbol}",
                symbol=symbol,
                name="fixture",
                status=AssetStatus.ACTIVE,
                tradable=True,
                expiration_date=datetime.strptime(symbol[4:10], "%y%m%d").date(),
                root_symbol="NVDA",
                underlying_symbol="NVDA",
                underlying_asset_id=UUID(int=9),
                type=ContractType.CALL,
                style=ExerciseStyle.AMERICAN,
                strike_price=Decimal("170") if "00170000" in symbol else Decimal("180"),
                size="100",
            )
            for symbol in symbols
        }


class Stocks:
    def __init__(self, quote: Quote, bars: BarSet) -> None:
        self.quote = quote
        self.bars = bars
        self.quote_request = None
        self.bars_request = None

    def get_stock_latest_quote(self, request_params):
        self.quote_request = request_params
        return {"NVDA": self.quote}

    def get_stock_bars(self, request_params):
        self.bars_request = request_params
        return self.bars


class Calendars:
    def get_calendar(self, _filters=None):
        return [
            Calendar(
                date=NOW.date().isoformat(),
                open="10:45",
                close="16:00",
            )
        ]


class Boundaries:
    def authority_for(self, **_kwargs: object) -> LifecycleBoundaryAuthority:
        return LifecycleBoundaryAuthority(None, NOW + timedelta(days=1), NOW + timedelta(days=2))


def quote(symbol: str = "NVDA") -> Quote:
    return Quote(
        symbol=symbol,
        raw_data={
            "t": NOW - timedelta(seconds=3),
            "bp": 174,
            "bs": 10,
            "ap": 176,
            "as": 10,
        },
    )


def option_snapshot(symbol: str, iv: str) -> OptionsSnapshot:
    return OptionsSnapshot(
        symbol,
        {
            "latestQuote": {
                "t": NOW - timedelta(seconds=3),
                "bp": 1,
                "bs": 10,
                "ap": 2,
                "as": 10,
            },
            "impliedVolatility": float(iv),
        },
    )


def roll_snapshot(
    symbol: str,
    *,
    delta: str,
    bid: float = 3,
    ask: float = 3.2,
) -> OptionsSnapshot:
    return OptionsSnapshot(
        symbol,
        {
            "latestQuote": {
                "t": NOW - timedelta(seconds=3),
                "bp": bid,
                "bs": 10,
                "ap": ask,
                "as": 10,
            },
            "impliedVolatility": 0.4,
            "greeks": {
                "delta": float(delta),
                "gamma": 0.02,
                "rho": 0.01,
                "theta": -0.03,
                "vega": 0.08,
            },
        },
    )


def bars(symbol: str, start: str) -> list[dict[str, object]]:
    base = Decimal(start)
    return [
        {
            "t": NOW - timedelta(minutes=15 - index * 5),
            "o": float(base),
            "h": float(base + 2 + index),
            "l": float(base - 1),
            "c": float(base + 1 + index),
            "v": 1000 + index * 100,
            "n": 100,
            "vw": float(base + Decimal("0.5") + index),
        }
        for index in range(3)
    ]


def test_market_collector_uses_explicit_feeds_and_deterministic_atm_tie_break() -> None:
    chain = OptionChain(
        {
            "NVDA260918C00170000": option_snapshot("NVDA260918C00170000", "0.30"),
            "NVDA260918C00180000": option_snapshot("NVDA260918C00180000", "0.90"),
            "NVDA260918P00170000": option_snapshot("NVDA260918P00170000", "0.50"),
            "NVDA260918P00180000": option_snapshot("NVDA260918P00180000", "0.80"),
        }
    )
    stocks = Stocks(
        quote(),
        BarSet({"NVDA": bars("NVDA", "170"), "QQQ": bars("QQQ", "400")}),
    )
    collector = AlpacaLifecycleMarketDataCollector(
        chain,
        NoContracts(),
        stocks,
        Calendars(),
        Boundaries(),
        clock=lambda: NOW - timedelta(seconds=2),
    )

    result = collector.collect(context=context(), trusted_at=NOW)

    assert result.atm_iv.value == Decimal("0.4")
    assert result.boundaries.price_confirmation[1].completed_bar_at == NOW
    assert result.underlying.benchmark_symbol == "QQQ"
    assert chain.request.feed is OptionsFeed.INDICATIVE
    assert stocks.quote_request.feed is DataFeed.IEX
    assert stocks.bars_request.feed is DataFeed.IEX


def test_market_collector_derives_one_bounded_same_structure_roll_candidate() -> None:
    current = {
        "NVDA260918C00170000": option_snapshot("NVDA260918C00170000", "0.30"),
        "NVDA260918C00180000": option_snapshot("NVDA260918C00180000", "0.90"),
        "NVDA260918P00170000": option_snapshot("NVDA260918P00170000", "0.50"),
        "NVDA260918P00180000": option_snapshot("NVDA260918P00180000", "0.80"),
    }
    replacement = {
        "NVDA261002C00170000": roll_snapshot("NVDA261002C00170000", delta="0.60"),
        "NVDA261002C00180000": roll_snapshot("NVDA261002C00180000", delta="0.30"),
    }
    chain = RollChain(current, replacement)
    collector = AlpacaLifecycleMarketDataCollector(
        chain,
        RollContracts(),
        Stocks(
            quote(),
            BarSet({"NVDA": bars("NVDA", "170"), "QQQ": bars("QQQ", "400")}),
        ),
        Calendars(),
        Boundaries(),
        clock=lambda: NOW - timedelta(seconds=2),
    )

    result = collector.collect(context=context(), trusted_at=NOW)

    assert result.roll is not None
    assert tuple(item.symbol for item in result.roll.positions) == tuple(replacement)
    assert tuple(item.signed_quantity for item in result.roll.positions) == (
        Decimal("1"),
        Decimal("-1"),
    )
    assert all(item.active and item.tradable for item in result.roll.options)
    assert chain.requests[1].expiration_date_gte == date(2026, 9, 25)
    assert chain.requests[1].expiration_date_lte == date(2026, 10, 23)


def test_mixed_roll_chain_keeps_complete_standard_candidate() -> None:
    current = {
        "NVDA260918C00170000": option_snapshot("NVDA260918C00170000", "0.30"),
        "NVDA260918C00180000": option_snapshot("NVDA260918C00180000", "0.90"),
        "NVDA260918P00170000": option_snapshot("NVDA260918P00170000", "0.50"),
        "NVDA260918P00180000": option_snapshot("NVDA260918P00180000", "0.80"),
    }
    replacement = {
        "NVDA1261002C00170000": roll_snapshot("NVDA1261002C00170000", delta="0.61"),
        "NVDA261002C00170000": roll_snapshot("NVDA261002C00170000", delta="0.60"),
        "NVDA261002C00180000": roll_snapshot("NVDA261002C00180000", delta="0.30"),
    }
    contracts = RollContracts()
    collector = AlpacaLifecycleMarketDataCollector(
        RollChain(current, replacement),
        contracts,
        Stocks(
            quote(),
            BarSet({"NVDA": bars("NVDA", "170"), "QQQ": bars("QQQ", "400")}),
        ),
        Calendars(),
        Boundaries(),
        clock=lambda: NOW - timedelta(seconds=2),
    )

    result = collector.collect(context=context(), trusted_at=NOW)

    assert tuple(item.symbol for item in result.roll.positions) == (
        "NVDA261002C00170000",
        "NVDA261002C00180000",
    )


def test_adjusted_only_roll_chain_preserves_unsupported_reason() -> None:
    current = {
        "NVDA260918C00170000": option_snapshot("NVDA260918C00170000", "0.30"),
        "NVDA260918C00180000": option_snapshot("NVDA260918C00180000", "0.90"),
        "NVDA260918P00170000": option_snapshot("NVDA260918P00170000", "0.50"),
        "NVDA260918P00180000": option_snapshot("NVDA260918P00180000", "0.80"),
    }
    replacement = {
        "NVDA1261002C00170000": roll_snapshot("NVDA1261002C00170000", delta="0.60"),
        "NVDA1261002C00180000": roll_snapshot("NVDA1261002C00180000", delta="0.30"),
    }
    collector = AlpacaLifecycleMarketDataCollector(
        RollChain(current, replacement),
        NoContracts(),
        Stocks(
            quote(),
            BarSet({"NVDA": bars("NVDA", "170"), "QQQ": bars("QQQ", "400")}),
        ),
        Calendars(),
        Boundaries(),
        clock=lambda: NOW - timedelta(seconds=2),
    )

    with pytest.raises(MarketDataError) as raised:
        collector.collect(context=context(), trusted_at=NOW)

    assert raised.value.code == "NON_STANDARD_CONTRACT_UNSUPPORTED"


def test_market_collector_skips_illiquid_early_expiry_for_later_candidate() -> None:
    current = {
        "NVDA260918C00170000": option_snapshot("NVDA260918C00170000", "0.30"),
        "NVDA260918C00180000": option_snapshot("NVDA260918C00180000", "0.90"),
        "NVDA260918P00170000": option_snapshot("NVDA260918P00170000", "0.50"),
        "NVDA260918P00180000": option_snapshot("NVDA260918P00180000", "0.80"),
    }
    replacement = {
        "NVDA260925C00170000": roll_snapshot("NVDA260925C00170000", delta="0.60", bid=1, ask=5),
        "NVDA260925C00180000": roll_snapshot("NVDA260925C00180000", delta="0.30", bid=1, ask=5),
        "NVDA261002C00170000": roll_snapshot("NVDA261002C00170000", delta="0.60"),
        "NVDA261002C00180000": roll_snapshot("NVDA261002C00180000", delta="0.30"),
    }
    collector = AlpacaLifecycleMarketDataCollector(
        RollChain(current, replacement),
        RollContracts(),
        Stocks(
            quote(),
            BarSet({"NVDA": bars("NVDA", "170"), "QQQ": bars("QQQ", "400")}),
        ),
        Calendars(),
        Boundaries(),
        clock=lambda: NOW - timedelta(seconds=2),
    )

    result = collector.collect(context=context(), trusted_at=NOW)

    assert len(result.roll_candidates) == 1
    assert result.roll_candidates[0].positions[0].symbol == "NVDA261002C00170000"
