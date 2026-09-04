from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.models import Bar, BarSet, OptionsSnapshot
from alpaca.data.requests import OptionChainRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import AssetStatus
from alpaca.trading.models import Calendar, Clock, OptionContract
from alpaca.trading.requests import GetCalendarRequest

from backend.app.alpaca.execution_evidence import ExecutionEvidenceError
from backend.app.alpaca.market_data import OptionContractSource
from backend.app.alpaca.trading import (
    AlpacaTradingReadAdapter,
    ProviderDataError,
    TradingFixtureClient,
)
from backend.app.contracts.v1 import (
    AccountResponse,
    AccountRole,
    DataQuality,
    PositionListResponse,
)
from backend.app.domain.option_contract_symbol import (
    NON_STANDARD_CONTRACT_UNSUPPORTED,
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)

MAX_OPPORTUNITY_CONTRACTS = 128
MAX_EXPIRY_WINDOW_DAYS = 45
MAX_STRIKE_WINDOW = Decimal("1000")
_BAR_DURATION = timedelta(minutes=5)
_NEW_YORK = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


class OpportunitySnapshotError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OpportunityTradingClient(TradingFixtureClient, Protocol):
    def get_clock(self) -> Clock: ...

    def get_calendar(self, filters: GetCalendarRequest | None = None) -> list[Calendar]: ...


class OpportunityStockClient(Protocol):
    def get_stock_bars(self, request_params: StockBarsRequest) -> BarSet: ...


class OpportunityOptionClient(Protocol):
    def get_option_chain(
        self, request_params: OptionChainRequest
    ) -> Mapping[str, OptionsSnapshot]: ...


@dataclass(frozen=True)
class OpportunitySnapshotRequest:
    expected_account_fingerprint: str
    underlying: str
    benchmark: str
    decision_boundary: datetime
    minimum_expiry: date
    maximum_expiry: date
    minimum_strike: Decimal
    maximum_strike: Decimal
    account_role: AccountRole = AccountRole.DEVELOPMENT
    maximum_contracts: int = 64
    maximum_quote_age: timedelta = timedelta(seconds=30)
    maximum_quote_skew: timedelta = timedelta(seconds=5)

    def __post_init__(self) -> None:
        strict_utc = (
            self.decision_boundary.tzinfo is not None
            and self.decision_boundary.utcoffset() == timedelta(0)
        )
        if (
            self.account_role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}
            or not re.fullmatch(r"[0-9a-f]{64}", self.expected_account_fingerprint)
            or not _SYMBOL.fullmatch(self.underlying)
            or not _SYMBOL.fullmatch(self.benchmark)
            or self.underlying == self.benchmark
            or not strict_utc
            or self.decision_boundary.second != 0
            or self.decision_boundary.microsecond != 0
            or self.decision_boundary.minute % 5 != 0
            or self.minimum_expiry < self.decision_boundary.date()
            or self.maximum_expiry < self.minimum_expiry
            or (self.maximum_expiry - self.minimum_expiry).days > MAX_EXPIRY_WINDOW_DAYS
            or not _finite(self.minimum_strike)
            or not _finite(self.maximum_strike)
            or self.minimum_strike <= 0
            or self.maximum_strike < self.minimum_strike
            or self.maximum_strike - self.minimum_strike > MAX_STRIKE_WINDOW
            or not 1 <= self.maximum_contracts <= MAX_OPPORTUNITY_CONTRACTS
            or not timedelta(0) < self.maximum_quote_age <= timedelta(minutes=2)
            or not timedelta(0) < self.maximum_quote_skew <= timedelta(seconds=30)
        ):
            raise OpportunitySnapshotError("OPPORTUNITY_REQUEST_INVALID")


@dataclass(frozen=True)
class OpportunityMarketSession:
    session_date: date
    open_at: datetime
    close_at: datetime
    clock_at: datetime
    market_open: bool
    next_open_at: datetime
    next_close_at: datetime
    source_hash: str


@dataclass(frozen=True)
class OpportunityBar:
    symbol: str
    started_at: datetime
    completed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal
    source_hash: str


@dataclass(frozen=True)
class OpportunityOption:
    symbol: str
    underlying: str
    expiry: date
    right: str
    strike: Decimal
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    quote_at: datetime
    retrieved_at: datetime
    implied_volatility: Decimal
    delta: Decimal
    gamma: Decimal
    theta_per_day: Decimal
    vega_per_iv_point: Decimal
    source_hash: str


@dataclass(frozen=True)
class OpportunityAccountBook:
    account: AccountResponse
    positions: PositionListResponse
    open_orders: tuple[dict[str, object], ...]
    account_fingerprint: str
    source_hash: str


@dataclass(frozen=True)
class OpportunityMarketSnapshot:
    trusted_at: datetime
    account_book: OpportunityAccountBook
    session: OpportunityMarketSession
    underlying_bar: OpportunityBar
    benchmark_bar: OpportunityBar
    options: tuple[OpportunityOption, ...]
    request_hash: str
    source_hash: str


class AlpacaOpportunitySnapshotCollector:
    def __init__(
        self,
        trading: OpportunityTradingClient,
        stocks: OpportunityStockClient,
        options: OpportunityOptionClient,
        contracts: OptionContractSource,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._trading = trading
        self._stocks = stocks
        self._options = options
        self._contracts = contracts
        self._clock = clock

    def collect(
        self, request: OpportunitySnapshotRequest, *, trusted_at: datetime
    ) -> OpportunityMarketSnapshot:
        if request.account_role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION):
            raise OpportunitySnapshotError("ACCOUNT_ROLE_NOT_DEVELOPMENT")
        trusted = _utc(trusted_at, "TRUSTED_TIME_INVALID")
        if trusted < request.decision_boundary:
            raise OpportunitySnapshotError("DECISION_BOUNDARY_INCOMPLETE")

        book = self._account_book(request)
        session = self._session(request, trusted)
        underlying_bar, benchmark_bar = self._bars(request, trusted)
        options = self._chain(request, trusted)
        request_hash = opportunity_snapshot_request_digest(request)
        snapshot = OpportunityMarketSnapshot(
            trusted,
            book,
            session,
            underlying_bar,
            benchmark_bar,
            options,
            request_hash,
            "",
        )
        return replace(snapshot, source_hash=opportunity_market_snapshot_digest(snapshot))

    def _account_book(self, request: OpportunitySnapshotRequest) -> OpportunityAccountBook:
        adapter = AlpacaTradingReadAdapter(
            self._trading,
            account_role=request.account_role,
            expected_account_fingerprint=request.expected_account_fingerprint,
            baseline_status=DataQuality.UNKNOWN,
            autonomous_enabled=False,
        )
        try:
            account = adapter.get_account()
            positions = adapter.list_positions()
            orders = adapter.list_open_orders()
        except ProviderDataError as error:
            if error.code == "ACCOUNT_FINGERPRINT_MISMATCH":
                raise OpportunitySnapshotError("ACCOUNT_AUTHORITY_MISMATCH") from error
            raise OpportunitySnapshotError("ACCOUNT_BOOK_INVALID") from error
        except ValueError as error:
            raise OpportunitySnapshotError("ACCOUNT_BOOK_INVALID") from error
        if (
            account.role is not request.account_role
            or not account.paper
            or any(item.role is not request.account_role for item in positions.positions)
        ):
            raise OpportunitySnapshotError("ACCOUNT_AUTHORITY_MISMATCH")
        book = OpportunityAccountBook(
            account,
            positions,
            orders,
            request.expected_account_fingerprint,
            "",
        )
        return replace(book, source_hash=opportunity_account_book_digest(book))

    def _session(
        self, request: OpportunitySnapshotRequest, trusted_at: datetime
    ) -> OpportunityMarketSession:
        raw_clock = self._trading.get_clock()
        if type(raw_clock) is not Clock:
            raise OpportunitySnapshotError("MARKET_CLOCK_INVALID")
        clock_at = _utc(raw_clock.timestamp, "MARKET_CLOCK_INVALID")
        next_open = _utc(raw_clock.next_open, "MARKET_CLOCK_INVALID")
        next_close = _utc(raw_clock.next_close, "MARKET_CLOCK_INVALID")
        if (
            clock_at - trusted_at > timedelta(seconds=30)
            or trusted_at - clock_at > timedelta(minutes=2)
            or next_open <= clock_at
            or next_close <= clock_at
        ):
            raise OpportunitySnapshotError("MARKET_CLOCK_INVALID")
        session_date = request.decision_boundary.astimezone(_NEW_YORK).date()
        calendars = self._trading.get_calendar(
            GetCalendarRequest(start=session_date, end=session_date)
        )
        if (
            type(calendars) is not list
            or len(calendars) != 1
            or type(calendars[0]) is not Calendar
            or calendars[0].date != session_date
        ):
            raise OpportunitySnapshotError("MARKET_SESSION_INVALID")
        open_at = _calendar_time(calendars[0].open)
        close_at = _calendar_time(calendars[0].close)
        expected_open = open_at <= clock_at < close_at
        if (
            open_at >= close_at
            or not open_at < request.decision_boundary <= close_at
            or raw_clock.is_open is not expected_open
        ):
            raise OpportunitySnapshotError("MARKET_SESSION_INVALID")
        session = OpportunityMarketSession(
            session_date,
            open_at,
            close_at,
            clock_at,
            raw_clock.is_open,
            next_open,
            next_close,
            "",
        )
        return replace(session, source_hash=opportunity_market_session_digest(session))

    def _bars(
        self, request: OpportunitySnapshotRequest, trusted_at: datetime
    ) -> tuple[OpportunityBar, OpportunityBar]:
        started_at = request.decision_boundary - _BAR_DURATION
        raw = self._stocks.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=[request.underlying, request.benchmark],
                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=started_at,
                end=request.decision_boundary,
                feed=DataFeed.IEX,
            )
        )
        retrieved_at = _utc(self._clock(), "PROVIDER_CLOCK_INVALID")
        if retrieved_at - trusted_at > timedelta(seconds=30) or not isinstance(raw, BarSet):
            raise OpportunitySnapshotError("DECISION_BAR_INVALID")
        if set(raw.data) != {request.underlying, request.benchmark}:
            raise OpportunitySnapshotError("DECISION_BAR_INCOMPLETE")
        results = tuple(
            _normalize_boundary_bar(raw.data[symbol], symbol, request.decision_boundary)
            for symbol in (request.underlying, request.benchmark)
        )
        return results[0], results[1]

    def _chain(
        self, request: OpportunitySnapshotRequest, trusted_at: datetime
    ) -> tuple[OpportunityOption, ...]:
        raw = self._options.get_option_chain(
            OptionChainRequest(
                underlying_symbol=request.underlying,
                feed=OptionsFeed.INDICATIVE,
                strike_price_gte=float(request.minimum_strike),
                strike_price_lte=float(request.maximum_strike),
                expiration_date_gte=request.minimum_expiry,
                expiration_date_lte=request.maximum_expiry,
            )
        )
        retrieved_at = _utc(self._clock(), "PROVIDER_CLOCK_INVALID")
        if retrieved_at - trusted_at > timedelta(seconds=30) or type(raw) is not dict:
            raise OpportunitySnapshotError("OPTION_CHAIN_INVALID")
        if not raw:
            raise OpportunitySnapshotError("OPTION_CHAIN_EMPTY")
        if len(raw) > request.maximum_contracts:
            raise OpportunitySnapshotError("OPTION_CHAIN_LIMIT_EXCEEDED")
        symbols = tuple(sorted(raw))
        if len(set(symbols)) != len(symbols):
            raise OpportunitySnapshotError("OPTION_CONTRACT_DUPLICATE")
        try:
            durable = self._contracts.contracts_for(symbols)
        except ExecutionEvidenceError as error:
            if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
                raise OpportunitySnapshotError(error.code) from error
            raise OpportunitySnapshotError("OPTION_CONTRACT_EVIDENCE_INVALID") from error
        except Exception as error:
            raise OpportunitySnapshotError("OPTION_CONTRACT_EVIDENCE_INVALID") from error
        if type(durable) is not dict or set(durable) != set(symbols):
            raise OpportunitySnapshotError("OPTION_CONTRACT_EVIDENCE_INCOMPLETE")
        normalized = tuple(
            _normalize_option(
                symbol,
                raw[symbol],
                durable[symbol],
                request,
                retrieved_at,
            )
            for symbol in symbols
        )
        fresh = tuple(
            item for item in normalized if retrieved_at - item.quote_at <= request.maximum_quote_age
        )
        if len(fresh) < 2:
            raise OpportunitySnapshotError("OPTION_QUOTE_STALE")
        return fresh


def _normalize_boundary_bar(
    bars: object, symbol: str, decision_boundary: datetime
) -> OpportunityBar:
    if type(bars) is not list or not bars or any(type(bar) is not Bar for bar in bars):
        raise OpportunitySnapshotError("DECISION_BAR_INCOMPLETE")
    matching = [
        bar
        for bar in bars
        if _utc(bar.timestamp, "DECISION_BAR_INVALID") + _BAR_DURATION == decision_boundary
    ]
    if len(matching) != 1:
        raise OpportunitySnapshotError("DECISION_BAR_INCOMPLETE")
    bar = matching[0]
    started_at = _utc(bar.timestamp, "DECISION_BAR_INVALID")
    values = tuple(
        _decimal(value, "DECISION_BAR_INVALID")
        for value in (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap)
    )
    open_, high, low, close, volume, vwap = values
    if (
        bar.symbol != symbol
        or started_at + _BAR_DURATION != decision_boundary
        or min(open_, high, low, close, volume, vwap) <= 0
        or low > high
        or not low <= open_ <= high
        or not low <= close <= high
    ):
        raise OpportunitySnapshotError("DECISION_BAR_INVALID")
    normalized = OpportunityBar(
        symbol,
        started_at,
        decision_boundary,
        open_,
        high,
        low,
        close,
        volume,
        vwap,
        "",
    )
    return replace(normalized, source_hash=opportunity_bar_digest(normalized))


def _normalize_option(
    symbol: str,
    snapshot: OptionsSnapshot,
    contract: OptionContract,
    request: OpportunitySnapshotRequest,
    retrieved_at: datetime,
) -> OpportunityOption:
    try:
        parsed = parse_standard_option_contract_symbol(
            symbol,
            underlying_symbol=request.underlying,
        )
    except OptionContractSymbolError as error:
        if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
            raise OpportunitySnapshotError(error.code) from error
        raise OpportunitySnapshotError("OPTION_CONTRACT_MALFORMED") from error
    expiry = parsed.expiration_date
    strike = parsed.strike_price
    right = parsed.right
    contract_type = "call" if right == "C" else "put"
    if (
        not request.minimum_expiry <= expiry <= request.maximum_expiry
        or not request.minimum_strike <= strike <= request.maximum_strike
        or type(snapshot) is not OptionsSnapshot
        or snapshot.symbol != symbol
        or type(contract) is not OptionContract
        or contract.symbol != symbol
        or contract.root_symbol != parsed.root_symbol
        or contract.underlying_symbol != request.underlying
        or parsed.root_symbol != request.underlying
        or contract.expiration_date != expiry
        or contract.type is None
        or contract.type.value != contract_type
        or _decimal(contract.strike_price, "OPTION_CONTRACT_INCONSISTENT") != strike
        or contract.size != "100"
        or contract.status is not AssetStatus.ACTIVE
        or contract.tradable is not True
    ):
        raise OpportunitySnapshotError("OPTION_CONTRACT_INCONSISTENT")
    quote = snapshot.latest_quote
    greeks = snapshot.greeks
    if quote is None or greeks is None:
        raise OpportunitySnapshotError("OPTION_GREEKS_MISSING")
    quote_at = _utc(quote.timestamp, "OPTION_QUOTE_INVALID")
    bid = _decimal(quote.bid_price, "OPTION_QUOTE_INVALID")
    ask = _decimal(quote.ask_price, "OPTION_QUOTE_INVALID")
    bid_size = _whole_positive(quote.bid_size, "OPTION_QUOTE_INVALID")
    ask_size = _whole_positive(quote.ask_size, "OPTION_QUOTE_INVALID")
    iv = _decimal(snapshot.implied_volatility, "OPTION_GREEKS_MISSING")
    delta = _decimal(greeks.delta, "OPTION_GREEKS_INVALID")
    gamma = _decimal(greeks.gamma, "OPTION_GREEKS_INVALID")
    theta = _decimal(greeks.theta, "OPTION_GREEKS_INVALID")
    vega = _decimal(greeks.vega, "OPTION_GREEKS_INVALID")
    if (
        min(bid, ask, iv) <= 0
        or bid > ask
        or quote_at > retrieved_at
        or not Decimal("-1") <= delta <= Decimal("1")
        or not Decimal(0) <= gamma <= Decimal("10")
        or abs(theta) > Decimal("1000")
        or not Decimal(0) <= vega <= Decimal("1000")
    ):
        raise OpportunitySnapshotError("OPTION_QUOTE_OR_GREEKS_INVALID")
    normalized = OpportunityOption(
        symbol,
        request.underlying,
        expiry,
        right,
        strike,
        bid,
        ask,
        bid_size,
        ask_size,
        quote_at,
        retrieved_at,
        iv,
        delta,
        gamma,
        theta,
        vega,
        "",
    )
    return replace(normalized, source_hash=opportunity_option_digest(normalized))


def opportunity_snapshot_request_digest(request: OpportunitySnapshotRequest) -> str:
    return _hash(
        {
            "account_role": request.account_role.value,
            "account_fingerprint": request.expected_account_fingerprint,
            "underlying": request.underlying,
            "benchmark": request.benchmark,
            "decision_boundary": request.decision_boundary.isoformat(),
            "minimum_expiry": request.minimum_expiry.isoformat(),
            "maximum_expiry": request.maximum_expiry.isoformat(),
            "minimum_strike": str(request.minimum_strike),
            "maximum_strike": str(request.maximum_strike),
            "maximum_contracts": request.maximum_contracts,
            "maximum_quote_age_seconds": request.maximum_quote_age.total_seconds(),
            "maximum_quote_skew_seconds": request.maximum_quote_skew.total_seconds(),
        }
    )


def opportunity_account_book_digest(book: OpportunityAccountBook) -> str:
    return _hash(
        {
            "account": book.account.model_dump(mode="json"),
            "positions": book.positions.model_dump(mode="json"),
            "orders": book.open_orders,
            "account_fingerprint": book.account_fingerprint,
        }
    )


def opportunity_market_session_digest(session: OpportunityMarketSession) -> str:
    return _hash(
        {
            "date": session.session_date.isoformat(),
            "open": session.open_at.isoformat(),
            "close": session.close_at.isoformat(),
            "clock": session.clock_at.isoformat(),
            "market_open": session.market_open,
            "next_open": session.next_open_at.isoformat(),
            "next_close": session.next_close_at.isoformat(),
        }
    )


def opportunity_bar_digest(bar: OpportunityBar) -> str:
    return _hash(
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


def opportunity_option_digest(option: OpportunityOption) -> str:
    return _hash(
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


def opportunity_market_snapshot_digest(snapshot: OpportunityMarketSnapshot) -> str:
    return _hash(
        {
            "request": snapshot.request_hash,
            "book": snapshot.account_book.source_hash,
            "session": snapshot.session.source_hash,
            "underlying_bar": snapshot.underlying_bar.source_hash,
            "benchmark_bar": snapshot.benchmark_bar.source_hash,
            "options": [item.source_hash for item in snapshot.options],
        }
    )


def _calendar_time(value: datetime) -> datetime:
    if value.tzinfo is not None or value.utcoffset() is not None:
        raise OpportunitySnapshotError("MARKET_SESSION_INVALID")
    return value.replace(tzinfo=_NEW_YORK).astimezone(UTC)


def _whole_positive(value: object, code: str) -> int:
    number = _decimal(value, code)
    if number <= 0 or number != number.to_integral_value():
        raise OpportunitySnapshotError(code)
    return int(number)


def _decimal(value: object, code: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise OpportunitySnapshotError(code) from error
    if not result.is_finite():
        raise OpportunitySnapshotError(code)
    return result


def _finite(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _utc(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OpportunitySnapshotError(code)
    return value.astimezone(UTC)


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
