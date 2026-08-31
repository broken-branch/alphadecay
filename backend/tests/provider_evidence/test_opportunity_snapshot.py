from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.models import BarSet, OptionsSnapshot
from alpaca.trading.enums import AssetStatus, ContractType, ExerciseStyle
from alpaca.trading.models import Calendar, Clock, OptionContract

from backend.app.alpaca.execution_evidence import (
    AlpacaOptionContractCollector,
    baseline_account_fingerprint,
)
from backend.app.alpaca.opportunity import (
    AlpacaOpportunitySnapshotCollector,
    OpportunitySnapshotError,
    OpportunitySnapshotRequest,
    opportunity_account_book_digest,
    opportunity_bar_digest,
    opportunity_market_session_digest,
    opportunity_market_snapshot_digest,
    opportunity_option_digest,
    opportunity_snapshot_request_digest,
)
from backend.app.alpaca.opportunity_runtime import (
    OpportunityRuntimeAdapterError,
    OpportunitySnapshotRuntimeAdapter,
)
from backend.app.contracts.v1 import AccountRole, DataQuality

TRUSTED_AT = datetime(2026, 8, 31, 15, 30, 20, tzinfo=UTC)
BOUNDARY = datetime(2026, 8, 31, 15, 30, tzinfo=UTC)
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000991")
ACCOUNT_FINGERPRINT = baseline_account_fingerprint(ACCOUNT_ID)
CALL = "NVDA260918C00175000"
PUT = "NVDA260918P00175000"


class Trading:
    def __init__(self) -> None:
        self.account_id = ACCOUNT_ID
        self.clock = Clock(
            timestamp=TRUSTED_AT - timedelta(seconds=2),
            is_open=True,
            next_open=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
            next_close=datetime(2026, 8, 31, 20, tzinfo=UTC),
        )
        self.calendar = Calendar(date="2026-08-31", open="09:30", close="16:00")
        self.calendar_request = None

    def get_account(self):
        return {
            "id": self.account_id,
            "status": "ACTIVE",
            "equity": "100000",
            "buying_power": "200000",
            "account_blocked": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "trade_suspended_by_user": False,
        }

    def get_all_positions(self):
        return []

    def get_orders(self, filter=None):
        assert filter.status.value == "open"
        assert filter.nested is True
        return []

    def get_clock(self):
        return self.clock

    def get_calendar(self, filters=None):
        self.calendar_request = filters
        return [self.calendar]


class Stocks:
    def __init__(self) -> None:
        self.request = None
        self.values = BarSet(
            {
                "NVDA": [bar("NVDA", "175", "176")],
                "QQQ": [bar("QQQ", "580", "581")],
            }
        )

    def get_stock_bars(self, request_params):
        self.request = request_params
        return self.values


class Options:
    def __init__(self) -> None:
        self.request = None
        self.values = {
            CALL: option(CALL, delta="0.55"),
            PUT: option(PUT, delta="-0.45"),
        }

    def get_option_chain(self, request_params):
        self.request = request_params
        return self.values


class Contracts:
    def __init__(self) -> None:
        self.values = {
            CALL: contract(CALL, ContractType.CALL),
            PUT: contract(PUT, ContractType.PUT),
        }
        self.symbols = None

    def contracts_for(self, symbols):
        self.symbols = symbols
        return {symbol: self.values[symbol] for symbol in symbols}


class ContractProviderTrap:
    def __init__(self) -> None:
        self.calls = 0

    def get_option_contracts(self, _request):
        self.calls += 1
        raise AssertionError("adjusted symbols must stop before contract provider work")


def bar(symbol: str, open_: str, close: str) -> dict[str, object]:
    start = BOUNDARY - timedelta(minutes=5)
    return {
        "t": start,
        "o": float(open_),
        "h": float(Decimal(close) + 1),
        "l": float(Decimal(open_) - 1),
        "c": float(close),
        "v": 1000,
        "n": 100,
        "vw": float((Decimal(open_) + Decimal(close)) / 2),
    }


def option(symbol: str, *, delta: str) -> OptionsSnapshot:
    return OptionsSnapshot(
        symbol,
        {
            "latestQuote": {
                "t": TRUSTED_AT - timedelta(seconds=8),
                "bp": 4.1,
                "bs": 5,
                "ap": 4.3,
                "as": 6,
            },
            "impliedVolatility": 0.35,
            "greeks": {
                "delta": float(delta),
                "gamma": 0.03,
                "rho": 0.02,
                "theta": -0.08,
                "vega": 0.17,
            },
        },
    )


def contract(symbol: str, kind: ContractType) -> OptionContract:
    return OptionContract(
        id=f"contract-{symbol}",
        symbol=symbol,
        name="fixture",
        status=AssetStatus.ACTIVE,
        tradable=True,
        expiration_date=date(2026, 9, 18),
        root_symbol="NVDA",
        underlying_symbol="NVDA",
        underlying_asset_id=UUID(int=7),
        type=kind,
        style=ExerciseStyle.AMERICAN,
        strike_price=Decimal("175"),
        size="100",
    )


def request(**changes: object) -> OpportunitySnapshotRequest:
    values = {
        "account_role": AccountRole.DEVELOPMENT,
        "expected_account_fingerprint": ACCOUNT_FINGERPRINT,
        "underlying": "NVDA",
        "benchmark": "QQQ",
        "decision_boundary": BOUNDARY,
        "minimum_expiry": date(2026, 9, 18),
        "maximum_expiry": date(2026, 9, 25),
        "minimum_strike": Decimal("165"),
        "maximum_strike": Decimal("185"),
        "maximum_contracts": 8,
    }
    values.update(changes)
    return OpportunitySnapshotRequest(**values)


def collector():
    trading = Trading()
    stocks = Stocks()
    options = Options()
    contracts = Contracts()
    target = AlpacaOpportunitySnapshotCollector(
        trading,
        stocks,
        options,
        contracts,
        clock=lambda: TRUSTED_AT - timedelta(seconds=1),
    )
    return target, trading, stocks, options, contracts


def test_collects_bounded_normalized_development_snapshot() -> None:
    target, trading, stocks, options, contracts = collector()

    result = target.collect(request(), trusted_at=TRUSTED_AT)

    assert result.account_book.account.role is AccountRole.DEVELOPMENT
    assert result.account_book.account.paper is True
    assert result.account_book.account.baseline_status is DataQuality.UNKNOWN
    assert result.account_book.account_fingerprint == ACCOUNT_FINGERPRINT
    assert result.session.market_open is True
    assert result.underlying_bar.completed_at == BOUNDARY
    assert result.benchmark_bar.symbol == "QQQ"
    assert [item.symbol for item in result.options] == [CALL, PUT]
    assert result.options[0].delta == Decimal("0.55")
    assert result.request_hash == opportunity_snapshot_request_digest(request())
    assert result.account_book.source_hash == opportunity_account_book_digest(result.account_book)
    assert result.session.source_hash == opportunity_market_session_digest(result.session)
    assert result.underlying_bar.source_hash == opportunity_bar_digest(result.underlying_bar)
    assert result.benchmark_bar.source_hash == opportunity_bar_digest(result.benchmark_bar)
    assert all(item.source_hash == opportunity_option_digest(item) for item in result.options)
    assert result.source_hash == opportunity_market_snapshot_digest(result)
    assert stocks.request.feed is DataFeed.IEX
    assert stocks.request.start == (BOUNDARY - timedelta(minutes=5)).replace(tzinfo=None)
    assert stocks.request.end == BOUNDARY.replace(tzinfo=None)
    assert options.request.feed is OptionsFeed.INDICATIVE
    assert options.request.strike_price_gte == 165.0
    assert options.request.strike_price_lte == 185.0
    assert options.request.expiration_date_gte == date(2026, 9, 18)
    assert options.request.expiration_date_lte == date(2026, 9, 25)
    assert contracts.symbols == (CALL, PUT)
    assert trading.calendar_request.start == date(2026, 8, 31)
    assert trading.calendar_request.end == date(2026, 8, 31)


def test_rejects_adjusted_contract_before_opportunity_selection() -> None:
    target, _trading, _stocks, options, contracts = collector()
    adjusted = "NVDA1260918C00175000"
    options.values = {adjusted: option(adjusted, delta="0.55")}
    contracts.values = {adjusted: contract(adjusted, ContractType.CALL)}

    with pytest.raises(OpportunitySnapshotError) as raised:
        target.collect(request(), trusted_at=TRUSTED_AT)

    assert raised.value.code == "NON_STANDARD_CONTRACT_UNSUPPORTED"


def test_real_contract_collector_preserves_adjusted_reason_without_provider_call() -> None:
    target, _trading, _stocks, options, _contracts = collector()
    adjusted = "NVDA1260918C00175000"
    options.values = {adjusted: option(adjusted, delta="0.55")}
    provider = ContractProviderTrap()
    target._contracts = AlpacaOptionContractCollector(provider)

    with pytest.raises(OpportunitySnapshotError) as raised:
        target.collect(request(), trusted_at=TRUSTED_AT)

    assert raised.value.code == "NON_STANDARD_CONTRACT_UNSUPPORTED"
    assert provider.calls == 0


def test_runtime_adapter_preserves_the_collectors_exact_snapshot() -> None:
    target, *_ = collector()
    snapshot_request = request()

    result = OpportunitySnapshotRuntimeAdapter(target).collect(
        snapshot_request,
        trusted_at=TRUSTED_AT,
    )

    assert result.source_hash == opportunity_market_snapshot_digest(result)
    assert result.request_hash == opportunity_snapshot_request_digest(snapshot_request)


def test_runtime_adapter_rejects_a_tampered_snapshot() -> None:
    target, *_ = collector()
    collected = target.collect(request(), trusted_at=TRUSTED_AT)

    class TamperedCollector:
        def collect(self, *_args, **_kwargs):
            return replace(collected, source_hash="f" * 64)

    with pytest.raises(
        OpportunityRuntimeAdapterError,
        match="OPPORTUNITY_SNAPSHOT_AUTHORITY_INVALID",
    ):
        OpportunitySnapshotRuntimeAdapter(TamperedCollector()).collect(
            request(),
            trusted_at=TRUSTED_AT,
        )


def test_request_accepts_executable_roles_and_rejects_replay_or_unbounded_ranges() -> None:
    assert request(account_role=AccountRole.SUBMISSION).account_role is AccountRole.SUBMISSION
    with pytest.raises(OpportunitySnapshotError, match="OPPORTUNITY_REQUEST_INVALID"):
        request(account_role=AccountRole.REPLAY)
    with pytest.raises(OpportunitySnapshotError, match="OPPORTUNITY_REQUEST_INVALID"):
        request(maximum_contracts=129)
    with pytest.raises(OpportunitySnapshotError, match="OPPORTUNITY_REQUEST_INVALID"):
        request(maximum_expiry=date(2026, 12, 1))


def test_request_defaults_to_development_role() -> None:
    values = request().__dict__.copy()
    values.pop("account_role")

    assert OpportunitySnapshotRequest(**values).account_role is AccountRole.DEVELOPMENT


def test_account_fingerprint_mismatch_fails_closed() -> None:
    target, trading, *_ = collector()
    trading.account_id = UUID(int=3)

    with pytest.raises(OpportunitySnapshotError, match="ACCOUNT_AUTHORITY_MISMATCH"):
        target.collect(request(), trusted_at=TRUSTED_AT)


def test_incomplete_or_current_decision_bar_fails_closed() -> None:
    target, _, stocks, *_ = collector()
    stocks.values = BarSet(
        {
            "NVDA": [bar("NVDA", "175", "176")],
            "QQQ": [
                {
                    **bar("QQQ", "580", "581"),
                    "t": BOUNDARY,
                }
            ],
        }
    )

    with pytest.raises(OpportunitySnapshotError, match="DECISION_BAR_INVALID"):
        target.collect(request(), trusted_at=TRUSTED_AT)


def test_chain_count_is_capped_before_contract_lookup() -> None:
    target, _, _, options, contracts = collector()
    options.values = {
        **options.values,
        "NVDA260918C00180000": option("NVDA260918C00180000", delta="0.40"),
    }

    with pytest.raises(OpportunitySnapshotError, match="OPTION_CHAIN_LIMIT_EXCEEDED"):
        target.collect(request(maximum_contracts=2), trusted_at=TRUSTED_AT)
    assert contracts.symbols is None


def test_stale_and_unsynchronized_option_quotes_fail_closed() -> None:
    target, _, _, options, _ = collector()
    options.values[CALL] = OptionsSnapshot(
        CALL,
        {
            "latestQuote": {
                "t": TRUSTED_AT - timedelta(seconds=45),
                "bp": 4.1,
                "bs": 5,
                "ap": 4.3,
                "as": 6,
            },
            "impliedVolatility": 0.35,
            "greeks": {"delta": 0.55, "gamma": 0.03, "rho": 0.02, "theta": -0.08, "vega": 0.17},
        },
    )

    with pytest.raises(
        OpportunitySnapshotError, match="OPTION_QUOTES_UNSYNCHRONIZED|OPTION_QUOTE_STALE"
    ):
        target.collect(request(), trusted_at=TRUSTED_AT)


def test_missing_greeks_and_malformed_occ_fail_closed() -> None:
    target, _, _, options, _ = collector()
    options.values[CALL] = OptionsSnapshot(
        CALL,
        {
            "latestQuote": {
                "t": TRUSTED_AT - timedelta(seconds=8),
                "bp": 4.1,
                "bs": 5,
                "ap": 4.3,
                "as": 6,
            },
            "impliedVolatility": 0.35,
        },
    )
    with pytest.raises(OpportunitySnapshotError, match="OPTION_GREEKS_MISSING"):
        target.collect(request(), trusted_at=TRUSTED_AT)

    target, _, _, options, _ = collector()
    options.values = {"NOT-OCC": option("NOT-OCC", delta="0.5")}
    with pytest.raises(OpportunitySnapshotError, match="OPTION_CONTRACT_EVIDENCE_INVALID"):
        target.collect(request(), trusted_at=TRUSTED_AT)


def test_inconsistent_contract_and_market_clock_fail_closed() -> None:
    target, trading, _, _, contracts = collector()
    contracts.values[CALL] = contract(CALL, ContractType.PUT)
    with pytest.raises(OpportunitySnapshotError, match="OPTION_CONTRACT_INCONSISTENT"):
        target.collect(request(), trusted_at=TRUSTED_AT)

    target, trading, *_ = collector()
    trading.clock = Clock(
        timestamp=TRUSTED_AT - timedelta(seconds=2),
        is_open=False,
        next_open=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
        next_close=datetime(2026, 8, 31, 20, tzinfo=UTC),
    )
    with pytest.raises(OpportunitySnapshotError, match="MARKET_SESSION_INVALID"):
        target.collect(request(), trusted_at=TRUSTED_AT)


@pytest.mark.parametrize(
    "changes",
    (
        {"root_symbol": "OTHER"},
        {"underlying_symbol": "OTHER"},
    ),
)
def test_contract_root_and_provider_underlying_are_validated_separately(
    changes: dict[str, object],
) -> None:
    target, _trading, _stocks, _options, contracts = collector()
    contracts.values[CALL] = contract(CALL, ContractType.CALL).model_copy(update=changes)

    with pytest.raises(OpportunitySnapshotError, match="OPTION_CONTRACT_INCONSISTENT"):
        target.collect(request(), trusted_at=TRUSTED_AT)
