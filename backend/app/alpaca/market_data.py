from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.models import Bar, BarSet, OptionsSnapshot, Quote
from alpaca.data.requests import OptionChainRequest, StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import AssetStatus
from alpaca.trading.models import Calendar, OptionContract
from alpaca.trading.requests import GetCalendarRequest
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.app.domain.option_contract_symbol import (
    NON_STANDARD_CONTRACT_UNSUPPORTED,
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)

if TYPE_CHECKING:
    from backend.app.services.acquisition import (
        AtmIvObservation,
        LifecycleBoundaryObservation,
        LifecycleRollObservation,
        RetainedLifecycleContext,
        RetainedOptionPosition,
        UnderlyingMarketObservation,
    )


class MarketDataError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _QuotePayload(_ProviderModel):
    timestamp: datetime
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal


class _GreekPayload(_ProviderModel):
    delta: Decimal
    gamma: Decimal
    theta: Decimal
    vega: Decimal


class _OptionSnapshotPayload(_ProviderModel):
    symbol: str
    underlying: str
    feed: str
    retrieved_at: datetime
    quote: _QuotePayload
    greeks: _GreekPayload
    multiplier: Decimal


class NormalizedGreeks(_ProviderModel):
    delta_per_share: Decimal
    gamma_per_share_per_usd: Decimal
    theta_per_share_per_day: Decimal
    vega_per_share_per_iv_point: Decimal
    quality: Literal["DERIVED_UNTIMESTAMPED"] = "DERIVED_UNTIMESTAMPED"


class NormalizedOptionSnapshot(_ProviderModel):
    symbol: str
    underlying: str
    provenance: Literal["INDICATIVE_MODIFIED"] = "INDICATIVE_MODIFIED"
    retrieved_at: datetime
    quote_timestamp: datetime
    bid_price: Decimal
    ask_price: Decimal
    bid_size: int
    ask_size: int
    multiplier: Literal[100] = 100
    greeks: NormalizedGreeks


class _BarPayload(_ProviderModel):
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class NormalizedBar(_ProviderModel):
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class NormalizedLifecycleMarketEvidence:
    underlying: UnderlyingMarketObservation
    atm_iv: AtmIvObservation
    boundaries: LifecycleBoundaryObservation
    roll_candidates: tuple[LifecycleRollObservation, ...] = ()

    @property
    def roll(self) -> LifecycleRollObservation | None:
        return self.roll_candidates[0] if self.roll_candidates else None


@dataclass(frozen=True)
class LifecycleBoundaryAuthority:
    short_call_close_at: datetime | None
    weekend_close_at: datetime
    contest_end_at: datetime

    def __post_init__(self) -> None:
        values = (self.weekend_close_at, self.contest_end_at)
        if (
            any(item.tzinfo is None or item.utcoffset() != timedelta(0) for item in values)
            or self.weekend_close_at > self.contest_end_at
            or (
                self.short_call_close_at is not None
                and (
                    self.short_call_close_at.tzinfo is None
                    or self.short_call_close_at.utcoffset() != timedelta(0)
                )
            )
        ):
            raise ValueError("LIFECYCLE_BOUNDARY_AUTHORITY_INVALID")


class OptionChainClient(Protocol):
    def get_option_chain(
        self, request_params: OptionChainRequest
    ) -> Mapping[str, OptionsSnapshot]: ...


class OptionContractSource(Protocol):
    def contracts_for(self, symbols: tuple[str, ...]) -> Mapping[str, OptionContract]: ...


class StockMarketDataClient(Protocol):
    def get_stock_latest_quote(
        self, request_params: StockLatestQuoteRequest
    ) -> Mapping[str, Quote]: ...

    def get_stock_bars(self, request_params: StockBarsRequest) -> BarSet: ...


class CalendarClient(Protocol):
    def get_calendar(self, filters: GetCalendarRequest | None = None) -> list[Calendar]: ...


class LifecycleBoundaryAuthoritySource(Protocol):
    def authority_for(
        self, *, context: RetainedLifecycleContext, session: Calendar
    ) -> LifecycleBoundaryAuthority: ...


_NEW_YORK = ZoneInfo("America/New_York")
FRIDAY_WEEKEND_CLOSE_AT = datetime(2026, 8, 28, 19, 30, tzinfo=UTC)
COMPETITION_END_AT = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)


class FrozenCompetitionBoundaryAuthority:
    def authority_for(
        self, *, context: RetainedLifecycleContext, session: Calendar
    ) -> LifecycleBoundaryAuthority:
        from backend.app.contracts.v1 import AccountRole
        from backend.app.services.acquisition import RetainedLifecycleContext

        if (
            type(context) is not RetainedLifecycleContext
            or context.account_role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION)
            or not _strict_utc(context.lifecycle_origin_at)
            or not _strict_utc(context.thesis_frozen_at)
            or not _strict_utc(context.launch_authority.entry_boundary_at)
            or context.thesis_frozen_at > context.lifecycle_origin_at
            or not context.launch_authority.entry_boundary_at
            <= context.thesis_frozen_at
            <= context.lifecycle_origin_at
        ):
            raise MarketDataError("LIFECYCLE_BOUNDARY_CONTEXT_INVALID")
        if type(session) is not Calendar:
            raise MarketDataError("LIFECYCLE_BOUNDARY_SESSION_INVALID")
        try:
            open_at = _calendar_utc(session.open)
            close_at = _calendar_utc(session.close)
        except MarketDataError as error:
            raise MarketDataError("LIFECYCLE_BOUNDARY_SESSION_INVALID") from error
        if (
            not date(2026, 8, 28) <= session.date <= date(2026, 9, 4)
            or open_at.date() != session.date
            or close_at.date() != session.date
            or open_at >= close_at
        ):
            raise MarketDataError("LIFECYCLE_BOUNDARY_SESSION_INVALID")

        weekend_close_at = (
            FRIDAY_WEEKEND_CLOSE_AT
            if context.lifecycle_origin_at <= FRIDAY_WEEKEND_CLOSE_AT
            else COMPETITION_END_AT
        )
        return LifecycleBoundaryAuthority(
            short_call_close_at=None,
            weekend_close_at=weekend_close_at,
            contest_end_at=COMPETITION_END_AT,
        )


class AlpacaLifecycleMarketDataCollector:
    def __init__(
        self,
        options: OptionChainClient,
        contracts: OptionContractSource,
        stocks: StockMarketDataClient,
        calendar: CalendarClient,
        boundary_authority: LifecycleBoundaryAuthoritySource,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._options = options
        self._contracts = contracts
        self._stocks = stocks
        self._calendar = calendar
        self._boundary_authority = boundary_authority
        self._clock = clock

    def collect(
        self, *, context: RetainedLifecycleContext, trusted_at: datetime
    ) -> NormalizedLifecycleMarketEvidence:
        from backend.app.services.acquisition import (
            AlpacaMarketSession,
            AtmIvObservation,
            LifecycleBoundaryObservation,
            PriceConfirmationPoint,
            UnderlyingMarketObservation,
        )

        underlying = context.thesis.thesis.underlying
        parsed = tuple(_parse_occ(item.symbol) for item in context.expected_positions)
        if len({(item[0], item[1]) for item in parsed}) != 1 or parsed[0][0] != underlying:
            raise MarketDataError("OPTION_STRUCTURE_INVALID")
        expiry = parsed[0][1]
        session_date = trusted_at.astimezone(_NEW_YORK).date()
        session_request = GetCalendarRequest(start=session_date, end=session_date)
        calendars = self._calendar.get_calendar(session_request)
        calendar_retrieved_at = _utc(self._clock(), "PROVIDER_CLOCK_INVALID")
        if (
            type(calendars) is not list
            or len(calendars) != 1
            or type(calendars[0]) is not Calendar
            or calendars[0].date != session_date
        ):
            raise MarketDataError("MARKET_CALENDAR_INVALID")
        calendar = calendars[0]
        open_at = _calendar_utc(calendar.open)
        close_at = _calendar_utc(calendar.close)
        if not open_at <= trusted_at <= close_at or calendar_retrieved_at - trusted_at > timedelta(
            seconds=30
        ):
            raise MarketDataError("MARKET_CALENDAR_INVALID")

        quote_request = StockLatestQuoteRequest(
            symbol_or_symbols=[underlying],
            feed=DataFeed.IEX,
        )
        quotes = self._stocks.get_stock_latest_quote(quote_request)
        quote_retrieved_at = _utc(self._clock(), "PROVIDER_CLOCK_INVALID")
        if type(quotes) is not dict or set(quotes) != {underlying}:
            raise MarketDataError("UNDERLYING_QUOTE_INCOMPLETE")
        quote = quotes[underlying]
        if type(quote) is not Quote or quote.symbol != underlying:
            raise MarketDataError("UNDERLYING_QUOTE_INVALID")
        quote_at = _utc(quote.timestamp, "UNDERLYING_QUOTE_INVALID")
        bid = _finite(quote.bid_price, "UNDERLYING_QUOTE_INVALID")
        ask = _finite(quote.ask_price, "UNDERLYING_QUOTE_INVALID")
        if (
            bid <= 0
            or ask < bid
            or quote_at > quote_retrieved_at
            or quote_retrieved_at - trusted_at > timedelta(seconds=30)
        ):
            raise MarketDataError("UNDERLYING_QUOTE_INVALID")

        bars_request = StockBarsRequest(
            symbol_or_symbols=[underlying, context.launch_authority.benchmark_symbol],
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=open_at,
            end=trusted_at,
            feed=DataFeed.IEX,
        )
        bar_set = self._stocks.get_stock_bars(bars_request)
        bars_retrieved_at = _utc(self._clock(), "PROVIDER_CLOCK_INVALID")
        if not isinstance(bar_set, BarSet) or set(bar_set.data) != {
            underlying,
            context.launch_authority.benchmark_symbol,
        }:
            raise MarketDataError("BAR_EVIDENCE_INCOMPLETE")
        asset_bars = _valid_bars(bar_set.data[underlying], trusted_at)
        benchmark_bars = _valid_bars(
            bar_set.data[context.launch_authority.benchmark_symbol], trusted_at
        )
        if (
            _utc(asset_bars[0].timestamp, "BAR_EVIDENCE_INVALID") != open_at
            or _utc(benchmark_bars[0].timestamp, "BAR_EVIDENCE_INVALID") != open_at
        ):
            raise MarketDataError("BAR_EVIDENCE_INCOMPLETE")
        if bars_retrieved_at - trusted_at > timedelta(seconds=30):
            raise MarketDataError("PROVIDER_TIMESTAMP_FUTURE")
        confirmations = _confirmation_points(
            asset_bars,
            benchmark_bars,
            context.launch_authority.beta60,
            context.launch_authority.entry_boundary_at,
        )

        chain_request = OptionChainRequest(
            underlying_symbol=underlying,
            feed=OptionsFeed.INDICATIVE,
            expiration_date=expiry,
        )
        chain = self._options.get_option_chain(chain_request)
        chain_retrieved_at = _utc(self._clock(), "PROVIDER_CLOCK_INVALID")
        if chain_retrieved_at - trusted_at > timedelta(seconds=30):
            raise MarketDataError("PROVIDER_TIMESTAMP_FUTURE")
        midpoint = (bid + ask) / Decimal(2)
        call = _select_atm(chain, underlying, expiry, "C", midpoint, chain_retrieved_at)
        put = _select_atm(chain, underlying, expiry, "P", midpoint, chain_retrieved_at)
        call_iv = _finite(call[1].implied_volatility, "ATM_IV_EVIDENCE_INVALID")
        put_iv = _finite(put[1].implied_volatility, "ATM_IV_EVIDENCE_INVALID")
        if call_iv <= 0 or put_iv <= 0:
            raise MarketDataError("ATM_IV_EVIDENCE_INVALID")
        call_at = _utc(call[1].latest_quote.timestamp, "ATM_IV_EVIDENCE_INVALID")
        put_at = _utc(put[1].latest_quote.timestamp, "ATM_IV_EVIDENCE_INVALID")
        if abs(call_at - put_at) > timedelta(seconds=5):
            raise MarketDataError("ATM_IV_EVIDENCE_NOT_SYNCHRONIZED")
        call_hash = _option_iv_hash(call[0], call[1], call_iv)
        put_hash = _option_iv_hash(put[0], put[1], put_iv)
        chain_hash = _hash(
            {
                "underlying": underlying,
                "expiry": expiry.isoformat(),
                "feed": "indicative",
            }
        )
        try:
            roll_candidates = _select_roll(
                self._options,
                self._contracts,
                context,
                parsed,
                trusted_at,
                self._clock,
            )
        except MarketDataError as error:
            if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
                raise
            roll_candidates = ()
        except Exception:
            roll_candidates = ()

        latest_asset = asset_bars[-1]
        latest_benchmark = benchmark_bars[-1]
        quote_hash = _hash(
            {"symbol": underlying, "at": quote_at.isoformat(), "bid": str(bid), "ask": str(ask)}
        )
        asset_bar_hash = _bar_hash(latest_asset)
        benchmark_bar_hash = _bar_hash(latest_benchmark)
        session_material = {
            "date": calendar.date.isoformat(),
            "open": open_at.isoformat(),
            "close": close_at.isoformat(),
        }
        session_hash = _hash(session_material)
        session_request_hash = _hash(
            {"start": session_date.isoformat(), "end": session_date.isoformat()}
        )
        session = AlpacaMarketSession(
            market_session_id=uuid5(NAMESPACE_URL, f"alphadecay:alpaca-session:{session_hash}"),
            session_date=calendar.date,
            open_at=open_at,
            close_at=close_at,
            source_hash=session_hash,
            request_hash=session_request_hash,
            retrieved_at=calendar_retrieved_at,
        )
        boundary_authority = self._boundary_authority.authority_for(
            context=context,
            session=calendar,
        )
        points = tuple(
            PriceConfirmationPoint(
                completed_bar_at=item[0],
                vwap_side=item[1],
                relative_return_side=item[2],
                source_hash=item[3],
                underlying_bar_source_hash=item[4],
                benchmark_bar_source_hash=item[5],
            )
            for item in confirmations
        )
        return NormalizedLifecycleMarketEvidence(
            underlying=UnderlyingMarketObservation(
                underlying=underlying,
                bid_price=bid,
                ask_price=ask,
                quote_observed_at=quote_at,
                quote_retrieved_at=quote_retrieved_at,
                quote_source_hash=quote_hash,
                completed_bar_at=_completed_at(latest_asset),
                completed_bar_source_hash=asset_bar_hash,
                request_hash=_hash(
                    {
                        "quote_symbols": [underlying],
                        "bar_symbols": [underlying, context.launch_authority.benchmark_symbol],
                        "timeframe": "5Min",
                        "start": open_at.isoformat(),
                        "end": trusted_at.isoformat(),
                        "feed": "iex",
                    }
                ),
                benchmark_symbol=context.launch_authority.benchmark_symbol,
                benchmark_completed_bar_at=_completed_at(latest_benchmark),
                benchmark_completed_bar_source_hash=benchmark_bar_hash,
            ),
            atm_iv=AtmIvObservation(
                underlying=underlying,
                value=(call_iv + put_iv) / Decimal(2),
                feed="indicative",
                observed_at=min(call_at, put_at),
                retrieved_at=chain_retrieved_at,
                source_hash=_hash({"call": call_hash, "put": put_hash}),
                request_hash=chain_hash,
                call_source_hash=call_hash,
                put_source_hash=put_hash,
            ),
            boundaries=LifecycleBoundaryObservation(
                market_session=session,
                observed_at=points[-1].completed_bar_at,
                source_hash=_hash(
                    {
                        "session": session_hash,
                        "points": [p.source_hash for p in points],
                        "short_call_close_at": _iso_or_none(boundary_authority.short_call_close_at),
                        "weekend_close_at": boundary_authority.weekend_close_at.isoformat(),
                        "contest_end_at": boundary_authority.contest_end_at.isoformat(),
                    }
                ),
                price_confirmation=(points[0], points[1]),
                short_call_close_at=boundary_authority.short_call_close_at,
                weekend_close_at=boundary_authority.weekend_close_at,
                contest_end_at=boundary_authority.contest_end_at,
            ),
            roll_candidates=roll_candidates,
        )


def _select_roll(
    chains: OptionChainClient,
    contracts: OptionContractSource,
    context: RetainedLifecycleContext,
    current: tuple[tuple[str, date, str, Decimal], ...],
    trusted_at: datetime,
    clock: Callable[[], datetime],
) -> tuple[LifecycleRollObservation, ...]:
    from backend.app.services.acquisition import (
        LifecycleOptionObservation,
        LifecycleRollObservation,
        RetainedOptionPosition,
    )

    underlying = context.thesis.thesis.underlying
    current_expiry = current[0][1]
    strikes = tuple(item[3] for item in current)
    right = current[0][2]
    request = OptionChainRequest(
        underlying_symbol=underlying,
        feed=OptionsFeed.INDICATIVE,
        strike_price_gte=float(min(strikes)),
        strike_price_lte=float(max(strikes)),
        expiration_date_gte=current_expiry + timedelta(days=7),
        expiration_date_lte=current_expiry + timedelta(days=35),
    )
    chain = chains.get_option_chain(request)
    retrieved_at = _utc(clock(), "PROVIDER_CLOCK_INVALID")
    if retrieved_at - trusted_at > timedelta(seconds=30) or type(chain) is not dict:
        raise MarketDataError("ROLL_EVIDENCE_INVALID")
    grouped: dict[date, dict[Decimal, tuple[str, OptionsSnapshot]]] = {}
    # Adjusted listings are ineligible, but they do not erase a complete standard candidate.
    adjusted_listing_seen = False
    for symbol, snapshot in chain.items():
        try:
            root, expiry, candidate_right, strike = _parse_occ(symbol)
        except MarketDataError as error:
            if error.code != NON_STANDARD_CONTRACT_UNSUPPORTED:
                raise
            adjusted_listing_seen = True
            continue
        if (
            root != underlying
            or candidate_right != right
            or strike not in strikes
            or not current_expiry + timedelta(days=7)
            <= expiry
            <= current_expiry + timedelta(days=35)
        ):
            continue
        if type(snapshot) is not OptionsSnapshot or snapshot.symbol != symbol:
            raise MarketDataError("ROLL_EVIDENCE_INVALID")
        grouped.setdefault(expiry, {})[strike] = (symbol, snapshot)
    candidates = tuple(
        (expiry, values)
        for expiry, values in sorted(grouped.items())
        if set(values) == set(strikes)
    )
    if not candidates:
        if adjusted_listing_seen:
            raise MarketDataError(NON_STANDARD_CONTRACT_UNSUPPORTED)
        return ()
    if len(candidates) > 8:
        candidates = candidates[:8]
    all_symbols = tuple(values[strike][0] for _expiry, values in candidates for strike in strikes)
    durable_contracts = contracts.contracts_for(all_symbols)
    if set(durable_contracts) != set(all_symbols):
        raise MarketDataError("ROLL_CONTRACT_EVIDENCE_INVALID")
    signed_by_strike = {
        parsed[3]: position.signed_quantity
        for parsed, position in zip(current, context.expected_positions, strict=True)
    }
    eligible: list[tuple[Decimal, date, LifecycleRollObservation]] = []
    for candidate_expiry, selected in candidates:
        positions: list[RetainedOptionPosition] = []
        observations: list[LifecycleOptionObservation] = []
        timestamps: list[datetime] = []
        for strike in strikes:
            symbol, snapshot = selected[strike]
            contract = durable_contracts[symbol]
            quote = snapshot.latest_quote
            greeks = snapshot.greeks
            if quote is None or greeks is None:
                raise MarketDataError("ROLL_EVIDENCE_INVALID")
            quote_at = _utc(quote.timestamp, "ROLL_EVIDENCE_INVALID")
            bid = _finite(quote.bid_price, "ROLL_EVIDENCE_INVALID")
            ask = _finite(quote.ask_price, "ROLL_EVIDENCE_INVALID")
            bid_size = _finite(quote.bid_size, "ROLL_EVIDENCE_INVALID")
            ask_size = _finite(quote.ask_size, "ROLL_EVIDENCE_INVALID")
            delta = _finite(greeks.delta, "ROLL_EVIDENCE_INVALID")
            gamma = _finite(greeks.gamma, "ROLL_EVIDENCE_INVALID")
            theta = _finite(greeks.theta, "ROLL_EVIDENCE_INVALID")
            vega = _finite(greeks.vega, "ROLL_EVIDENCE_INVALID")
            root, expiry, candidate_right, parsed_strike = _parse_occ(symbol)
            contract_type = "call" if candidate_right == "C" else "put"
            if (
                contract.symbol != symbol
                or contract.status is not AssetStatus.ACTIVE
                or contract.tradable is not True
                or contract.root_symbol != root
                or contract.underlying_symbol != underlying
                or contract.expiration_date != expiry
                or contract.type is None
                or contract.type.value != contract_type
                or _finite(contract.strike_price, "ROLL_CONTRACT_EVIDENCE_INVALID") != parsed_strike
                or contract.size != "100"
                or min(bid, ask, bid_size, ask_size) <= 0
                or bid > ask
                or quote_at > retrieved_at
                or retrieved_at - quote_at > timedelta(seconds=30)
                or not Decimal("-1") <= delta <= Decimal("1")
                or not Decimal(0) <= gamma <= Decimal("10")
                or abs(theta) > Decimal("1000")
                or not Decimal(0) <= vega <= Decimal("1000")
            ):
                raise MarketDataError("ROLL_EVIDENCE_INVALID")
            midpoint = (bid + ask) / Decimal(2)
            if (ask - bid) / midpoint > context.maximum_relative_spread:
                observations = []
                break
            signed_quantity = signed_by_strike[strike]
            material = {
                "symbol": symbol,
                "timestamp": quote_at.isoformat(),
                "retrieved_at": retrieved_at.isoformat(),
                "bid": str(bid),
                "ask": str(ask),
                "bid_size": str(bid_size),
                "ask_size": str(ask_size),
                "delta": str(delta),
                "gamma": str(gamma),
                "theta": str(theta),
                "vega": str(vega),
                "contract_status": contract.status.value,
                "contract_tradable": contract.tradable,
            }
            positions.append(RetainedOptionPosition(symbol, signed_quantity, 100))
            observations.append(
                LifecycleOptionObservation(
                    symbol=symbol,
                    signed_quantity=signed_quantity,
                    multiplier=100,
                    active=True,
                    tradable=True,
                    feed="indicative",
                    bid_price=bid,
                    ask_price=ask,
                    delta=delta,
                    gamma=gamma,
                    theta_per_day=theta,
                    vega_per_iv_point=vega,
                    quote_observed_at=quote_at,
                    greek_observed_at=quote_at,
                    retrieved_at=retrieved_at,
                    greek_authority_id=context.greek_authority.authority_id,
                    greek_timestamp_source_hash=context.greek_authority.timestamp_contract_hash,
                    greek_units_source_hash=context.greek_authority.units_source_hash,
                    source_hash=_hash(material),
                )
            )
            timestamps.append(quote_at)
        if not observations:
            continue
        if max(timestamps) - min(timestamps) > timedelta(seconds=5):
            raise MarketDataError("ROLL_EVIDENCE_NOT_SYNCHRONIZED")
        package_spread = sum((item.ask_price - item.bid_price) for item in observations) / sum(
            (item.ask_price + item.bid_price) / Decimal(2) for item in observations
        )
        if package_spread > context.maximum_relative_spread:
            continue
        eligible.append(
            (
                package_spread,
                candidate_expiry,
                LifecycleRollObservation(tuple(positions), tuple(observations)),
            )
        )
    eligible.sort(
        key=lambda item: (
            item[0],
            item[1],
            tuple(position.symbol for position in item[2].positions),
        )
    )
    return tuple(item[2] for item in eligible)


def _parse_occ(symbol: str) -> tuple[str, date, str, Decimal]:
    try:
        parsed = parse_standard_option_contract_symbol(symbol)
    except OptionContractSymbolError as error:
        if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
            raise MarketDataError(error.code) from error
        raise MarketDataError("OPTION_STRUCTURE_INVALID") from error
    return (
        parsed.root_symbol,
        parsed.expiration_date,
        parsed.right,
        parsed.strike_price,
    )


def _select_atm(
    chain: Mapping[str, OptionsSnapshot],
    underlying: str,
    expiry: date,
    right: str,
    midpoint: Decimal,
    retrieved_at: datetime,
) -> tuple[str, OptionsSnapshot]:
    candidates: list[tuple[Decimal, Decimal, str, OptionsSnapshot]] = []
    if type(chain) is not dict:
        raise MarketDataError("ATM_IV_EVIDENCE_INVALID")
    for symbol, snapshot in chain.items():
        root, candidate_expiry, candidate_right, strike = _parse_occ(symbol)
        if root != underlying or candidate_expiry != expiry or candidate_right != right:
            continue
        if type(snapshot) is not OptionsSnapshot or snapshot.symbol != symbol:
            raise MarketDataError("ATM_IV_EVIDENCE_INVALID")
        # Deep in- or out-of-the-money contracts routinely carry stale quotes, zero bids,
        # or no implied volatility. They are not candidates; only the selected
        # at-the-money pair must be fresh and complete.
        if snapshot.latest_quote is None or snapshot.implied_volatility is None:
            continue
        quote = snapshot.latest_quote
        try:
            quote_at = _utc(snapshot.latest_quote.timestamp, "ATM_IV_EVIDENCE_INVALID")
            bid = _finite(quote.bid_price, "ATM_IV_EVIDENCE_INVALID")
            ask = _finite(quote.ask_price, "ATM_IV_EVIDENCE_INVALID")
            iv = _finite(snapshot.implied_volatility, "ATM_IV_EVIDENCE_INVALID")
        except MarketDataError:
            continue
        if quote_at > retrieved_at or retrieved_at - quote_at > timedelta(seconds=30):
            continue
        if bid <= 0 or ask < bid or iv <= 0:
            continue
        candidates.append((abs(strike - midpoint), strike, symbol, snapshot))
    if not candidates:
        raise MarketDataError("ATM_IV_EVIDENCE_INCOMPLETE")
    candidates.sort(key=lambda item: item[:3])
    selected = candidates[0]
    return selected[2], selected[3]


def _valid_bars(bars: list[Bar], trusted_at: datetime) -> tuple[Bar, ...]:
    if type(bars) is not list or len(bars) < 2 or any(type(item) is not Bar for item in bars):
        raise MarketDataError("BAR_EVIDENCE_INCOMPLETE")
    ordered = tuple(sorted(bars, key=lambda item: item.timestamp))
    times = tuple(_utc(item.timestamp, "BAR_EVIDENCE_INVALID") for item in ordered)
    if len(set(times)) != len(times) or any(
        time + timedelta(minutes=5) > trusted_at for time in times
    ):
        raise MarketDataError("BAR_EVIDENCE_INVALID")
    for item in ordered:
        values = (
            _finite(item.open, "BAR_EVIDENCE_INVALID"),
            _finite(item.high, "BAR_EVIDENCE_INVALID"),
            _finite(item.low, "BAR_EVIDENCE_INVALID"),
            _finite(item.close, "BAR_EVIDENCE_INVALID"),
            _finite(item.volume, "BAR_EVIDENCE_INVALID"),
            _finite(item.vwap, "BAR_EVIDENCE_INVALID"),
        )
        if (
            min(values) <= 0
            or values[2] > values[1]
            or not values[2] <= values[0] <= values[1]
            or not values[2] <= values[3] <= values[1]
        ):
            raise MarketDataError("BAR_EVIDENCE_INVALID")
    return ordered


def _confirmation_points(
    asset_bars: tuple[Bar, ...],
    benchmark_bars: tuple[Bar, ...],
    beta60: Decimal,
    entry_boundary_at: datetime,
) -> tuple[tuple[datetime, Decimal, Decimal, str, str, str], ...]:
    asset_times = {_utc(item.timestamp, "BAR_EVIDENCE_INVALID") for item in asset_bars}
    benchmark_times = {_utc(item.timestamp, "BAR_EVIDENCE_INVALID") for item in benchmark_bars}
    if asset_times != benchmark_times:
        raise MarketDataError("BAR_EVIDENCE_INCOMPLETE")
    by_benchmark = {_utc(item.timestamp, "BAR_EVIDENCE_INVALID"): item for item in benchmark_bars}
    common = tuple(
        item
        for item in asset_bars
        if _utc(item.timestamp, "BAR_EVIDENCE_INVALID") in by_benchmark
        and _utc(item.timestamp, "BAR_EVIDENCE_INVALID") > entry_boundary_at
    )
    if len(common) < 2:
        raise MarketDataError("PRICE_CONFIRMATION_INCOMPLETE")
    selected = common[-2:]
    times = tuple(_utc(item.timestamp, "BAR_EVIDENCE_INVALID") for item in selected)
    if times[1] - times[0] != timedelta(minutes=5):
        raise MarketDataError("PRICE_CONFIRMATION_NOT_ADJACENT")
    asset_open = _finite(asset_bars[0].open, "BAR_EVIDENCE_INVALID")
    benchmark_open = _finite(benchmark_bars[0].open, "BAR_EVIDENCE_INVALID")
    results = []
    for asset in selected:
        at = _utc(asset.timestamp, "BAR_EVIDENCE_INVALID")
        completed_at = _completed_at(asset)
        benchmark = by_benchmark[at]
        prefix = tuple(
            item for item in asset_bars if _utc(item.timestamp, "BAR_EVIDENCE_INVALID") <= at
        )
        volume = sum((_finite(item.volume, "BAR_EVIDENCE_INVALID") for item in prefix), Decimal(0))
        session_vwap = (
            sum(
                (
                    _finite(item.vwap, "BAR_EVIDENCE_INVALID")
                    * _finite(item.volume, "BAR_EVIDENCE_INVALID")
                    for item in prefix
                ),
                Decimal(0),
            )
            / volume
        )
        asset_close = _finite(asset.close, "BAR_EVIDENCE_INVALID")
        benchmark_close = _finite(benchmark.close, "BAR_EVIDENCE_INVALID")
        vwap_side = asset_close / session_vwap - Decimal(1)
        relative = (asset_close / asset_open - Decimal(1)) - beta60 * (
            benchmark_close / benchmark_open - Decimal(1)
        )
        asset_hash = _bar_hash(asset)
        benchmark_hash = _bar_hash(benchmark)
        point_hash = _hash(
            {
                "at": completed_at.isoformat(),
                "vwap_side": str(vwap_side),
                "relative_return_side": str(relative),
                "asset": asset_hash,
                "benchmark": benchmark_hash,
            }
        )
        results.append((completed_at, vwap_side, relative, point_hash, asset_hash, benchmark_hash))
    return tuple(results)


def _bar_hash(bar: Bar) -> str:
    return _hash(
        {
            "symbol": bar.symbol,
            "timestamp": _utc(bar.timestamp, "BAR_EVIDENCE_INVALID").isoformat(),
            "open": str(_finite(bar.open, "BAR_EVIDENCE_INVALID")),
            "high": str(_finite(bar.high, "BAR_EVIDENCE_INVALID")),
            "low": str(_finite(bar.low, "BAR_EVIDENCE_INVALID")),
            "close": str(_finite(bar.close, "BAR_EVIDENCE_INVALID")),
            "volume": str(_finite(bar.volume, "BAR_EVIDENCE_INVALID")),
            "vwap": str(_finite(bar.vwap, "BAR_EVIDENCE_INVALID")),
        }
    )


def _completed_at(bar: Bar) -> datetime:
    return _utc(bar.timestamp, "BAR_EVIDENCE_INVALID") + timedelta(minutes=5)


def _option_iv_hash(symbol: str, snapshot: OptionsSnapshot, iv: Decimal) -> str:
    quote = snapshot.latest_quote
    if quote is None:
        raise MarketDataError("ATM_IV_EVIDENCE_INVALID")
    return _hash(
        {
            "symbol": symbol,
            "iv": str(iv),
            "feed": "indicative",
            "quote_at": _utc(quote.timestamp, "ATM_IV_EVIDENCE_INVALID").isoformat(),
            "bid": str(_finite(quote.bid_price, "ATM_IV_EVIDENCE_INVALID")),
            "ask": str(_finite(quote.ask_price, "ATM_IV_EVIDENCE_INVALID")),
        }
    )


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _finite(value: object, code: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise MarketDataError(code) from error
    if not result.is_finite():
        raise MarketDataError(code)
    return result


def _utc(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketDataError(code)
    return value.astimezone(UTC)


def _strict_utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


def _calendar_utc(value: datetime) -> datetime:
    if value.tzinfo is not None or value.utcoffset() is not None:
        raise MarketDataError("MARKET_CALENDAR_INVALID")
    return value.replace(tzinfo=_NEW_YORK).astimezone(UTC)


def normalize_option_snapshot(
    raw: object,
    *,
    now: datetime,
    max_quote_age_seconds: int = 30,
) -> NormalizedOptionSnapshot:
    try:
        payload = _OptionSnapshotPayload.model_validate(raw)
    except ValidationError as exc:
        raise MarketDataError("OPTION_SCHEMA_INVALID") from exc
    _require_aware(now, "CLOCK_TIMEZONE_MISSING")
    _require_aware(payload.retrieved_at, "OPTION_SCHEMA_INVALID")
    _require_aware(payload.quote.timestamp, "OPTION_SCHEMA_INVALID")
    if payload.feed != "indicative":
        raise MarketDataError("OPTION_FEED_NOT_INDICATIVE")
    if payload.retrieved_at > now or payload.quote.timestamp > now:
        raise MarketDataError("PROVIDER_TIMESTAMP_FUTURE")
    if payload.quote.timestamp > payload.retrieved_at:
        raise MarketDataError("OPTION_TIMESTAMP_INCONSISTENT")
    if (now - payload.quote.timestamp).total_seconds() > max_quote_age_seconds:
        raise MarketDataError("OPTION_QUOTE_STALE")
    if payload.quote.bid_price <= 0 or payload.quote.ask_price <= 0:
        raise MarketDataError("OPTION_QUOTE_EMPTY")
    if payload.quote.bid_price > payload.quote.ask_price:
        raise MarketDataError("OPTION_QUOTE_CROSSED")
    if payload.multiplier != 100:
        raise MarketDataError("OPTION_MULTIPLIER_INVALID")
    bid_size = _whole_positive(payload.quote.bid_size, "OPTION_QUOTE_EMPTY")
    ask_size = _whole_positive(payload.quote.ask_size, "OPTION_QUOTE_EMPTY")
    return NormalizedOptionSnapshot(
        symbol=payload.symbol,
        underlying=payload.underlying,
        retrieved_at=payload.retrieved_at,
        quote_timestamp=payload.quote.timestamp,
        bid_price=payload.quote.bid_price,
        ask_price=payload.quote.ask_price,
        bid_size=bid_size,
        ask_size=ask_size,
        greeks=NormalizedGreeks(
            delta_per_share=payload.greeks.delta,
            gamma_per_share_per_usd=payload.greeks.gamma,
            theta_per_share_per_day=payload.greeks.theta,
            vega_per_share_per_iv_point=payload.greeks.vega,
        ),
    )


def normalize_bars(
    raw: object,
    *,
    now: datetime,
    max_age_seconds: int = 120,
) -> tuple[NormalizedBar, ...]:
    if not isinstance(raw, list) or not raw:
        raise MarketDataError("BAR_SCHEMA_INVALID")
    try:
        payloads = tuple(_BarPayload.model_validate(item) for item in raw)
    except ValidationError as exc:
        raise MarketDataError("BAR_SCHEMA_INVALID") from exc
    _require_aware(now, "CLOCK_TIMEZONE_MISSING")
    if any(item.timestamp.tzinfo is None for item in payloads):
        raise MarketDataError("BAR_SCHEMA_INVALID")
    ordered = tuple(sorted(payloads, key=lambda item: item.timestamp))
    timestamps = tuple(item.timestamp for item in ordered)
    if len(set(timestamps)) != len(timestamps):
        raise MarketDataError("BAR_TIMESTAMP_DUPLICATE")
    if timestamps[-1] > now:
        raise MarketDataError("PROVIDER_TIMESTAMP_FUTURE")
    if (now - timestamps[-1]).total_seconds() > max_age_seconds:
        raise MarketDataError("BAR_STALE")
    for item in ordered:
        if item.volume < 0 or item.low > item.high:
            raise MarketDataError("BAR_VALUE_INVALID")
        if not item.low <= item.open <= item.high or not item.low <= item.close <= item.high:
            raise MarketDataError("BAR_VALUE_INVALID")
    return tuple(NormalizedBar(**item.model_dump()) for item in ordered)


def _whole_positive(value: Decimal, code: str) -> int:
    integral = value.to_integral_value()
    if value != integral or integral <= 0:
        raise MarketDataError(code)
    return int(integral)


def _require_aware(value: datetime, code: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketDataError(code)
