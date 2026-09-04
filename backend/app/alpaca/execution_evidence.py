from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from alpaca.data.enums import OptionsFeed
from alpaca.data.models import OptionsSnapshot
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.enums import AssetClass, AssetStatus, OrderClass, QueryOrderStatus
from alpaca.trading.models import (
    OptionContract,
    OptionContractsResponse,
    Order,
    Position,
    TradeAccount,
)
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    GetOrderByIdRequest,
    GetOrdersRequest,
)

from backend.app.alpaca.activities import InitialFundingContext
from backend.app.alpaca.market_data import NormalizedGreeks, NormalizedOptionSnapshot
from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.domain.option_contract_symbol import (
    NON_STANDARD_CONTRACT_UNSUPPORTED,
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)
from backend.app.execution import (
    AccountObservation,
    ActivityItem,
    ActivityPaginationEvidence,
    ActivityType,
    InventoryItem,
    InventoryKind,
    OpenOrderItem,
    OpenOrderLeg,
    SweepObservation,
)
from backend.app.execution.models import OrderAttempt, PositionGreekObservation
from backend.app.execution.order_status import normalize_broker_order_state
from backend.app.execution.reconciliation import ReconciliationExpectation
from backend.app.services.execution import WholeAccountEvidence

if TYPE_CHECKING:
    from backend.app.services.acquisition import RetainedLifecycleContext


class ExecutionEvidenceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LifecycleAccountEvidence:
    sweep: SweepObservation
    options: tuple[LifecycleOptionEvidence, ...]


@dataclass(frozen=True)
class LifecycleOptionEvidence:
    symbol: str
    signed_quantity: Decimal
    multiplier: int
    bid_price: Decimal
    ask_price: Decimal
    delta: Decimal
    gamma: Decimal
    theta_per_day: Decimal
    vega_per_iv_point: Decimal
    feed: str
    source_timestamp: datetime
    retrieved_at: datetime
    source_hash: str


def baseline_account_fingerprint(account_id: UUID) -> str:
    """Match the sealed baseline's SHA-256 of the UUID text and one LF byte."""
    if not isinstance(account_id, UUID):
        raise ExecutionEvidenceError("ACCOUNT_ID_INVALID")
    return hashlib.sha256(f"{account_id}\n".encode()).hexdigest()


class TradingReadClient(Protocol):
    def get_account(self) -> TradeAccount: ...
    def get_all_positions(self) -> list[Position]: ...
    def get_orders(self, filter: GetOrdersRequest | None = None) -> list[Order]: ...
    def get_order_by_id(
        self, order_id: UUID | str, filter: GetOrderByIdRequest | None = None
    ) -> Order: ...


class OptionSnapshotClient(Protocol):
    def get_option_snapshot(
        self, request_params: OptionSnapshotRequest
    ) -> Mapping[str, OptionsSnapshot]: ...


class AlpacaReplacementQuoteCollector:
    def __init__(
        self,
        snapshots: OptionSnapshotClient,
        contracts: OptionContractSource,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._snapshots = snapshots
        self._contracts = contracts
        self._clock = clock

    def collect(self, symbols: tuple[str, ...]) -> tuple[NormalizedOptionSnapshot, ...]:
        if not symbols or len(set(symbols)) != len(symbols):
            raise ExecutionEvidenceError("REPLACEMENT_QUOTE_SYMBOLS_INVALID")
        parsed = {}
        for symbol in symbols:
            try:
                parsed[symbol] = parse_standard_option_contract_symbol(symbol)
            except OptionContractSymbolError as error:
                if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
                    raise ExecutionEvidenceError(error.code) from error
                raise ExecutionEvidenceError("REPLACEMENT_QUOTE_EVIDENCE_INVALID") from error
        raw = self._snapshots.get_option_snapshot(
            OptionSnapshotRequest(
                symbol_or_symbols=list(symbols),
                feed=OptionsFeed.INDICATIVE,
            )
        )
        retrieved_at = _utc(self._clock())
        if set(raw) != set(symbols):
            raise ExecutionEvidenceError("REPLACEMENT_QUOTE_EVIDENCE_INCOMPLETE")
        try:
            contracts = self._contracts.contracts_for(symbols)
        except ExecutionEvidenceError:
            raise
        except Exception as error:
            raise ExecutionEvidenceError("OPTION_CONTRACT_EVIDENCE_INVALID") from error
        if set(contracts) != set(symbols):
            raise ExecutionEvidenceError("OPTION_CONTRACT_EVIDENCE_INCOMPLETE")
        normalized: list[NormalizedOptionSnapshot] = []
        for symbol in symbols:
            snapshot = raw[symbol]
            contract = parsed[symbol]
            durable = contracts[symbol]
            _contract_material(durable)
            if durable.root_symbol != contract.root_symbol:
                raise ExecutionEvidenceError("OPTION_CONTRACT_METADATA_INVALID")
            if snapshot.latest_quote is None or snapshot.greeks is None:
                raise ExecutionEvidenceError("REPLACEMENT_QUOTE_EVIDENCE_INVALID")
            quote = snapshot.latest_quote
            bid_size = _finite_decimal(quote.bid_size, "REPLACEMENT_QUOTE_EVIDENCE_INVALID")
            ask_size = _finite_decimal(quote.ask_size, "REPLACEMENT_QUOTE_EVIDENCE_INVALID")
            if bid_size != bid_size.to_integral_value() or ask_size != ask_size.to_integral_value():
                raise ExecutionEvidenceError("REPLACEMENT_QUOTE_EVIDENCE_INVALID")
            normalized.append(
                NormalizedOptionSnapshot(
                    symbol=symbol,
                    underlying=durable.underlying_symbol,
                    retrieved_at=retrieved_at,
                    quote_timestamp=_utc(quote.timestamp),
                    bid_price=_finite_decimal(
                        quote.bid_price, "REPLACEMENT_QUOTE_EVIDENCE_INVALID"
                    ),
                    ask_price=_finite_decimal(
                        quote.ask_price, "REPLACEMENT_QUOTE_EVIDENCE_INVALID"
                    ),
                    bid_size=int(bid_size),
                    ask_size=int(ask_size),
                    greeks=NormalizedGreeks(
                        delta_per_share=_finite_decimal(
                            snapshot.greeks.delta,
                            "REPLACEMENT_QUOTE_EVIDENCE_INVALID",
                        ),
                        gamma_per_share_per_usd=_finite_decimal(
                            snapshot.greeks.gamma,
                            "REPLACEMENT_QUOTE_EVIDENCE_INVALID",
                        ),
                        theta_per_share_per_day=_finite_decimal(
                            snapshot.greeks.theta,
                            "REPLACEMENT_QUOTE_EVIDENCE_INVALID",
                        ),
                        vega_per_share_per_iv_point=_finite_decimal(
                            snapshot.greeks.vega,
                            "REPLACEMENT_QUOTE_EVIDENCE_INVALID",
                        ),
                    ),
                )
            )
        return tuple(normalized)


class OptionContractSource(Protocol):
    def contracts_for(self, symbols: tuple[str, ...]) -> Mapping[str, OptionContract]: ...


class OptionContractClient(Protocol):
    def get_option_contracts(
        self, request: GetOptionContractsRequest
    ) -> OptionContractsResponse: ...


class ActivitySource(Protocol):
    def collect(
        self,
        *,
        since: datetime,
        until: datetime,
        provider_to_client: Mapping[str, str],
        initial_funding: InitialFundingContext,
        observed_account_fingerprint: str,
    ) -> tuple[tuple[ActivityItem, ...], ActivityPaginationEvidence]: ...


class LifecycleActivitySource(Protocol):
    def collect_lifecycle(
        self,
        *,
        since: datetime,
        until: datetime,
        observed_account_fingerprint: str,
        known_activity_hashes: tuple[str, ...],
    ) -> tuple[tuple[ActivityItem, ...], ActivityPaginationEvidence]: ...


class AttemptLineageSource(Protocol):
    def attempts_for(self, intent_id: UUID) -> tuple[OrderAttempt, ...]: ...


class AlpacaExecutionReadCollector:
    def __init__(
        self,
        client: TradingReadClient,
        *,
        account_role: AccountRole,
        expected_account_fingerprint: str,
        paper: bool,
        clock: Callable[[], datetime],
    ) -> None:
        if paper is not True:
            raise ExecutionEvidenceError("PAPER_TRADING_REQUIRED")
        self._client = client
        self._role = account_role
        self._expected_fingerprint = expected_account_fingerprint
        self._paper = paper
        self._clock = clock

    def account(self) -> AccountObservation:
        raw = self._client.get_account()
        fingerprint = baseline_account_fingerprint(raw.id)
        if fingerprint != self._expected_fingerprint:
            raise ExecutionEvidenceError("ACCOUNT_FINGERPRINT_MISMATCH")
        observed_at = _utc(self._clock())
        required = (
            raw.equity,
            raw.buying_power,
            raw.cash,
            raw.account_blocked,
            raw.trading_blocked,
            raw.trade_suspended_by_user,
            raw.options_trading_level,
            raw.status,
        )
        if any(value is None for value in required):
            raise ExecutionEvidenceError("ACCOUNT_EVIDENCE_INCOMPLETE")
        return AccountObservation(
            role=self._role,
            account_fingerprint=fingerprint,
            paper=self._paper,
            status=raw.status.value,
            account_blocked=bool(raw.account_blocked),
            trading_blocked=bool(raw.trading_blocked or raw.trade_suspended_by_user),
            options_trading_blocked=int(raw.options_trading_level) < 3,
            equity=_finite_decimal(raw.equity, "ACCOUNT_EVIDENCE_INVALID"),
            buying_power=_finite_decimal(raw.buying_power, "ACCOUNT_EVIDENCE_INVALID"),
            cash=_finite_decimal(raw.cash, "ACCOUNT_EVIDENCE_INVALID"),
            observed_at=observed_at,
            time_quality="RETRIEVAL_TIME_ONLY",
        )

    def positions(self) -> tuple[InventoryItem, ...]:
        raw_positions = self._client.get_all_positions()
        if not isinstance(raw_positions, list):
            raise ExecutionEvidenceError("POSITION_RESPONSE_INCOMPLETE")
        items: list[InventoryItem] = []
        for raw in raw_positions:
            quantity = _signed_quantity(raw)
            if raw.asset_class == AssetClass.US_OPTION:
                try:
                    parse_standard_option_contract_symbol(raw.symbol)
                except OptionContractSymbolError as error:
                    if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
                        raise ExecutionEvidenceError(error.code) from error
                    raise ExecutionEvidenceError("OPTION_POSITION_METADATA_INVALID") from error
                items.append(InventoryItem(InventoryKind.OPTION, raw.symbol, quantity, 100))
            elif raw.asset_class == AssetClass.US_EQUITY:
                items.append(InventoryItem(InventoryKind.EQUITY, raw.symbol, quantity, 1))
            else:
                raise ExecutionEvidenceError("POSITION_ASSET_CLASS_UNSUPPORTED")
        ordered = tuple(sorted(items, key=lambda item: (item.kind.value, item.symbol)))
        if len({(item.kind, item.symbol) for item in ordered}) != len(ordered):
            raise ExecutionEvidenceError("POSITION_DUPLICATE")
        return ordered

    def open_orders(self) -> tuple[OpenOrderItem, ...]:
        raw_orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=True)
        )
        if not isinstance(raw_orders, list):
            raise ExecutionEvidenceError("OPEN_ORDER_RESPONSE_INCOMPLETE")
        if len(raw_orders) >= 500:
            raise ExecutionEvidenceError("OPEN_ORDER_WINDOW_SATURATED")
        by_provider = {str(order.id): order for order in raw_orders}
        if len(by_provider) != len(raw_orders):
            raise ExecutionEvidenceError("ORDER_PROVIDER_ID_DUPLICATE")
        for provider_id in sorted(
            {
                str(linked_id)
                for order in raw_orders
                for linked_id in (order.replaces, order.replaced_by)
                if linked_id is not None and str(linked_id) not in by_provider
            }
        ):
            linked = self._client.get_order_by_id(provider_id)
            if str(linked.id) != provider_id or not linked.client_order_id:
                raise ExecutionEvidenceError("ORDER_REPLACEMENT_LINEAGE_INCOMPLETE")
            by_provider[provider_id] = linked
        if len({order.client_order_id for order in by_provider.values()}) != len(by_provider):
            raise ExecutionEvidenceError("ORDER_CLIENT_ID_DUPLICATE")
        for order in by_provider.values():
            if order.replaces is not None:
                predecessor = by_provider.get(str(order.replaces))
                if predecessor is None or predecessor.replaced_by != order.id:
                    raise ExecutionEvidenceError("ORDER_REPLACEMENT_LINEAGE_INVALID")
            if order.replaced_by is not None:
                successor = by_provider.get(str(order.replaced_by))
                if successor is None or successor.replaces != order.id:
                    raise ExecutionEvidenceError("ORDER_REPLACEMENT_LINEAGE_INVALID")
        normalized = tuple(
            sorted(
                (self._open_order(order, by_provider) for order in raw_orders),
                key=lambda item: item.client_order_id,
            )
        )
        if len({item.client_order_id for item in normalized}) != len(normalized):
            raise ExecutionEvidenceError("ORDER_CLIENT_ID_DUPLICATE")
        return normalized

    @staticmethod
    def _open_order(order: Order, by_provider: Mapping[str, Order]) -> OpenOrderItem:
        if order.order_class != OrderClass.MLEG or order.legs is None:
            raise ExecutionEvidenceError("OPEN_ORDER_NOT_MLEG")
        quantity = _whole_positive(order.qty, "OPEN_ORDER_QUANTITY_INVALID")
        filled = _whole_nonnegative(order.filled_qty, "OPEN_ORDER_QUANTITY_INVALID")
        if filled > quantity or len(order.legs) not in {2, 4}:
            raise ExecutionEvidenceError("OPEN_ORDER_STRUCTURE_INVALID")
        legs: list[OpenOrderLeg] = []
        for leg in order.legs:
            if leg.symbol is None or leg.position_intent is None or leg.ratio_qty is None:
                raise ExecutionEvidenceError("OPEN_ORDER_LEG_INCOMPLETE")
            legs.append(
                OpenOrderLeg(
                    symbol=leg.symbol,
                    intent=PositionIntent(leg.position_intent.value.upper()),
                    ratio=_whole_positive(leg.ratio_qty, "OPEN_ORDER_RATIO_INVALID"),
                )
            )
        replaces = _client_id_for_provider(order.replaces, by_provider)
        replaced_by = _client_id_for_provider(order.replaced_by, by_provider)
        return OpenOrderItem(
            provider_order_id=str(order.id),
            client_order_id=order.client_order_id,
            state=normalize_broker_order_state(order.status.value),
            quantity=quantity,
            filled_quantity=filled,
            replaces_client_order_id=replaces,
            replaced_by_client_order_id=replaced_by,
            order_class="MLEG",
            legs=tuple(sorted(legs, key=lambda item: item.symbol)),
        )


class AlpacaOptionContractCollector:
    def __init__(self, client: OptionContractClient, *, max_pages: int = 100) -> None:
        if not 1 <= max_pages <= 100:
            raise ExecutionEvidenceError("OPTION_CONTRACT_PAGE_LIMIT_INVALID")
        self._client = client
        self._max_pages = max_pages

    def contracts_for(self, symbols: tuple[str, ...]) -> Mapping[str, OptionContract]:
        roots: dict[str, set[str]] = {}
        expirations: dict[str, tuple[date, date]] = {}
        for symbol in symbols:
            try:
                contract = parse_standard_option_contract_symbol(symbol)
            except OptionContractSymbolError as error:
                if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
                    raise ExecutionEvidenceError(error.code) from error
                raise ExecutionEvidenceError("OPTION_CONTRACT_SYMBOL_INVALID") from error
            roots.setdefault(contract.root_symbol, set()).add(symbol)
            window = expirations.get(contract.root_symbol)
            expirations[contract.root_symbol] = (
                min(window[0], contract.expiration_date) if window else contract.expiration_date,
                max(window[1], contract.expiration_date) if window else contract.expiration_date,
            )
        collected: dict[str, OptionContract] = {}
        for root in sorted(roots):
            page_token: str | None = None
            seen_tokens: set[str] = set()
            earliest, latest = expirations[root]
            for _ in range(self._max_pages):
                response = self._client.get_option_contracts(
                    GetOptionContractsRequest(
                        root_symbol=root,
                        expiration_date_gte=earliest,
                        expiration_date_lte=latest,
                        limit=1000,
                        page_token=page_token,
                    )
                )
                if not isinstance(response, OptionContractsResponse):
                    raise ExecutionEvidenceError("OPTION_CONTRACT_RESPONSE_INVALID")
                contracts = response.option_contracts
                if contracts is None:
                    raise ExecutionEvidenceError("OPTION_CONTRACT_RESPONSE_INCOMPLETE")
                for contract in contracts:
                    if contract.root_symbol != root or contract.symbol in collected:
                        raise ExecutionEvidenceError("OPTION_CONTRACT_RESPONSE_INVALID")
                    if contract.symbol in roots[root]:
                        collected[contract.symbol] = contract
                next_token = response.next_page_token
                if next_token is None:
                    break
                if not contracts or not next_token or next_token in seen_tokens:
                    raise ExecutionEvidenceError("OPTION_CONTRACT_PAGINATION_INVALID")
                seen_tokens.add(next_token)
                page_token = next_token
            else:
                raise ExecutionEvidenceError("OPTION_CONTRACT_PAGE_LIMIT_EXCEEDED")
        if set(collected) != set(symbols):
            raise ExecutionEvidenceError("OPTION_CONTRACT_EVIDENCE_INCOMPLETE")
        return collected


class IndicativeGreekCollector:
    def __init__(
        self,
        snapshots: OptionSnapshotClient,
        contracts: OptionContractSource,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._snapshots = snapshots
        self._contracts = contracts
        self._clock = clock

    def collect(self, positions: tuple[InventoryItem, ...]) -> tuple[PositionGreekObservation, ...]:
        return tuple(
            PositionGreekObservation(
                symbol=item.symbol,
                signed_quantity=item.signed_quantity,
                multiplier=item.multiplier,
                delta=item.delta,
                gamma=item.gamma,
                theta_per_day=item.theta_per_day,
                vega_per_iv_point=item.vega_per_iv_point,
                feed=item.feed,
                source_timestamp=item.source_timestamp,
                retrieved_at=item.retrieved_at,
                source_hash=item.source_hash,
            )
            for item in self.collect_lifecycle(positions)
        )

    def collect_lifecycle(
        self,
        positions: tuple[InventoryItem, ...],
    ) -> tuple[LifecycleOptionEvidence, ...]:
        option_positions = tuple(item for item in positions if item.kind == InventoryKind.OPTION)
        if not option_positions:
            return ()
        symbols = tuple(item.symbol for item in option_positions)
        raw = self._snapshots.get_option_snapshot(
            OptionSnapshotRequest(
                symbol_or_symbols=list(symbols),
                feed=OptionsFeed.INDICATIVE,
            )
        )
        contracts = self._contracts.contracts_for(symbols)
        retrieved_at = _utc(self._clock())
        if set(raw) != set(symbols) or set(contracts) != set(symbols):
            raise ExecutionEvidenceError("OPTION_GREEK_EVIDENCE_INCOMPLETE")
        observations: list[LifecycleOptionEvidence] = []
        for position in option_positions:
            snapshot = raw[position.symbol]
            contract = contracts[position.symbol]
            if (
                snapshot.latest_quote is None
                or snapshot.greeks is None
                or contract.symbol != position.symbol
            ):
                raise ExecutionEvidenceError("OPTION_GREEK_EVIDENCE_INVALID")
            quote = snapshot.latest_quote
            source_timestamp = _utc(quote.timestamp)
            if source_timestamp > retrieved_at or retrieved_at - source_timestamp > timedelta(
                seconds=30
            ):
                raise ExecutionEvidenceError("OPTION_GREEK_EVIDENCE_INVALID")
            contract_material = _contract_material(contract)
            bid_price = _finite_decimal(quote.bid_price, "OPTION_GREEK_EVIDENCE_INVALID")
            ask_price = _finite_decimal(quote.ask_price, "OPTION_GREEK_EVIDENCE_INVALID")
            bid_size = _finite_decimal(quote.bid_size, "OPTION_GREEK_EVIDENCE_INVALID")
            ask_size = _finite_decimal(quote.ask_size, "OPTION_GREEK_EVIDENCE_INVALID")
            delta = _finite_decimal(snapshot.greeks.delta, "OPTION_GREEK_EVIDENCE_INVALID")
            gamma = _finite_decimal(snapshot.greeks.gamma, "OPTION_GREEK_EVIDENCE_INVALID")
            theta = _finite_decimal(snapshot.greeks.theta, "OPTION_GREEK_EVIDENCE_INVALID")
            vega = _finite_decimal(snapshot.greeks.vega, "OPTION_GREEK_EVIDENCE_INVALID")
            if (
                min(bid_price, ask_price, bid_size, ask_size) <= 0
                or bid_price > ask_price
                or not Decimal("-1") <= delta <= Decimal("1")
                or not Decimal("0") <= gamma <= Decimal("10")
                or abs(theta) > Decimal("1000")
                or not Decimal("0") <= vega <= Decimal("1000")
            ):
                raise ExecutionEvidenceError("OPTION_GREEK_EVIDENCE_INVALID")
            material = {
                "symbol": position.symbol,
                "timestamp": source_timestamp.isoformat(),
                "bid_price": str(bid_price),
                "ask_price": str(ask_price),
                "bid_size": str(bid_size),
                "ask_size": str(ask_size),
                "delta": str(delta),
                "gamma": str(gamma),
                "theta": str(theta),
                "vega": str(vega),
                "multiplier": 100,
                "feed": "indicative",
                "contract": contract_material,
            }
            source_hash = hashlib.sha256(
                json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            observations.append(
                LifecycleOptionEvidence(
                    symbol=position.symbol,
                    signed_quantity=position.signed_quantity,
                    multiplier=100,
                    bid_price=bid_price,
                    ask_price=ask_price,
                    delta=delta,
                    gamma=gamma,
                    theta_per_day=theta,
                    vega_per_iv_point=vega,
                    feed="indicative",
                    source_timestamp=source_timestamp,
                    retrieved_at=retrieved_at,
                    source_hash=source_hash,
                )
            )
        return tuple(observations)


class AlpacaLifecycleAccountCollector:
    def __init__(
        self,
        trading: AlpacaExecutionReadCollector,
        activities: LifecycleActivitySource,
        greeks: IndicativeGreekCollector,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._trading = trading
        self._activities = activities
        self._greeks = greeks
        self._clock = clock

    def collect(
        self,
        *,
        context: RetainedLifecycleContext,
        trusted_at: datetime,
    ) -> LifecycleAccountEvidence:
        started = _utc(self._clock())
        if started - trusted_at > timedelta(seconds=30):
            raise ExecutionEvidenceError("PROVIDER_TIMESTAMP_FUTURE")
        first_account = self._trading.account()
        first_positions = self._trading.positions()
        first_orders = self._trading.open_orders()
        known_hashes = context.account_activity_hashes
        activities, pagination = self._activities.collect_lifecycle(
            since=context.account_lifecycle_origin_at,
            until=first_account.observed_at,
            observed_account_fingerprint=context.account_fingerprint,
            known_activity_hashes=known_hashes,
        )
        options = self._greeks.collect_lifecycle(first_positions)
        final_account = self._trading.account()
        final_positions = self._trading.positions()
        final_orders = self._trading.open_orders()
        completed = _utc(self._clock())
        if completed - trusted_at > timedelta(seconds=30):
            raise ExecutionEvidenceError("PROVIDER_TIMESTAMP_FUTURE")
        return LifecycleAccountEvidence(
            sweep=SweepObservation(
                retrieval_started_at=started,
                retrieval_completed_at=completed,
                activity_pagination=pagination,
                first_account=first_account,
                final_account=final_account,
                first_positions=first_positions,
                final_positions=final_positions,
                first_open_orders=first_orders,
                final_open_orders=final_orders,
                activities=activities,
                positions_complete=True,
                orders_complete=True,
            ),
            options=options,
        )


class AlpacaWholeAccountSweepPort:
    def __init__(
        self,
        trading: AlpacaExecutionReadCollector,
        activities: ActivitySource,
        greeks: IndicativeGreekCollector,
        lineage: AttemptLineageSource,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._trading = trading
        self._activities = activities
        self._greeks = greeks
        self._lineage = lineage
        self._clock = clock

    def collect(self, expectation: ReconciliationExpectation) -> WholeAccountEvidence:
        started = _utc(self._clock())
        first_account = self._trading.account()
        first_positions = self._trading.positions()
        first_orders = self._trading.open_orders()
        provider_to_client: dict[str, str] = {}
        for attempt in self._lineage.attempts_for(expectation.intent_id):
            if attempt.provider_order_id is None:
                continue
            known_client = provider_to_client.get(attempt.provider_order_id)
            if known_client is not None and known_client != attempt.client_order_id:
                raise ExecutionEvidenceError("ORDER_LINEAGE_CONFLICT")
            provider_to_client[attempt.provider_order_id] = attempt.client_order_id
        for order in first_orders:
            known_client = provider_to_client.get(order.provider_order_id)
            if known_client is not None and known_client != order.client_order_id:
                raise ExecutionEvidenceError("ORDER_LINEAGE_CONFLICT")
            provider_to_client[order.provider_order_id] = order.client_order_id
        activities, pagination = self._activities.collect(
            since=expectation.required_activity_window_start,
            until=first_account.observed_at,
            provider_to_client=provider_to_client,
            initial_funding=_initial_funding_context(expectation),
            observed_account_fingerprint=first_account.account_fingerprint,
        )
        greek_observations = self._greeks.collect(first_positions)
        final_account = self._trading.account()
        final_positions = self._trading.positions()
        final_orders = self._trading.open_orders()
        completed = _utc(self._clock())
        sweep = SweepObservation(
            retrieval_started_at=started,
            retrieval_completed_at=completed,
            activity_pagination=pagination,
            first_account=first_account,
            final_account=final_account,
            first_positions=first_positions,
            final_positions=final_positions,
            first_open_orders=first_orders,
            final_open_orders=final_orders,
            activities=activities,
            positions_complete=True,
            orders_complete=True,
        )
        return WholeAccountEvidence(sweep, greek_observations)


def _initial_funding_context(
    expectation: ReconciliationExpectation,
) -> InitialFundingContext | None:
    funding = tuple(
        activity
        for activity in expectation.known_activities
        if activity.activity_type == ActivityType.INITIAL_FUNDING
    )
    if len(funding) > 1:
        raise ExecutionEvidenceError("INITIAL_FUNDING_EXPECTATION_INVALID")
    if not funding:
        # Successor reconciliation states carry only in-window activities; the funding
        # journal predates every window and needs no classification context.
        return None
    item = funding[0]
    if (
        item.symbol is not None
        or item.signed_quantity is None
        or item.signed_quantity <= 0
        or item.provider_order_id is not None
        or item.client_order_id is not None
        or item.occurred_at > expectation.baseline_captured_at
    ):
        raise ExecutionEvidenceError("INITIAL_FUNDING_EXPECTATION_INVALID")
    return InitialFundingContext(
        captured_at=expectation.baseline_captured_at,
        equity=item.signed_quantity,
        account_fingerprint=expectation.account_fingerprint,
        activity_id_hash=item.activity_id_hash,
    )


def _signed_quantity(position: Position) -> Decimal:
    """Alpaca reports a short position with a negative quantity; the sign must match the side."""
    quantity = _finite_decimal(position.qty, "POSITION_QUANTITY_INVALID")
    side = position.side.value
    if quantity == 0 or (side == "long" and quantity < 0) or (side == "short" and quantity > 0):
        raise ExecutionEvidenceError("POSITION_QUANTITY_INVALID")
    if side not in {"long", "short"}:
        raise ExecutionEvidenceError("POSITION_QUANTITY_INVALID")
    return quantity if side == "long" else -abs(quantity)


def _client_id_for_provider(value: object, orders: Mapping[str, Order]) -> str | None:
    if value is None:
        return None
    target = orders.get(str(value))
    if target is None:
        raise ExecutionEvidenceError("ORDER_REPLACEMENT_LINEAGE_INCOMPLETE")
    return target.client_order_id


def _finite_decimal(value: object, code: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExecutionEvidenceError(code) from exc
    if not result.is_finite():
        raise ExecutionEvidenceError(code)
    return result


def _contract_material(contract: OptionContract) -> dict[str, str]:
    try:
        parsed = parse_standard_option_contract_symbol(contract.symbol)
    except OptionContractSymbolError as error:
        if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
            raise ExecutionEvidenceError(error.code) from error
        raise ExecutionEvidenceError("OPTION_CONTRACT_METADATA_INVALID") from error
    if contract.size != "100" or contract.type is None or contract.style is None:
        raise ExecutionEvidenceError("OPTION_CONTRACT_METADATA_INVALID")
    kind = "call" if parsed.right == "C" else "put"
    contract_strike = _finite_decimal(contract.strike_price, "OPTION_CONTRACT_METADATA_INVALID")
    if (
        contract.status != AssetStatus.ACTIVE
        or contract.tradable is not True
        or contract.expiration_date != parsed.expiration_date
        or contract.type.value != kind
        or contract_strike != parsed.strike_price
        or contract.root_symbol != parsed.root_symbol
        or contract.root_symbol != contract.underlying_symbol
    ):
        raise ExecutionEvidenceError("OPTION_CONTRACT_METADATA_INVALID")
    return {
        "status": contract.status.value,
        "tradable": "true",
        "expiration_date": parsed.expiration_date.isoformat(),
        "root_symbol": contract.root_symbol,
        "underlying_symbol": contract.underlying_symbol,
        "type": kind,
        "style": contract.style.value,
        "strike_price": str(contract_strike),
        "size": contract.size,
    }


def _whole_positive(value: object, code: str) -> int:
    result = _finite_decimal(value, code)
    if result <= 0 or result != result.to_integral_value():
        raise ExecutionEvidenceError(code)
    return int(result)


def _whole_nonnegative(value: object, code: str) -> int:
    result = _finite_decimal(value, code)
    if result < 0 or result != result.to_integral_value():
        raise ExecutionEvidenceError(code)
    return int(result)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionEvidenceError("PROVIDER_CLOCK_INVALID")
    return value.astimezone(UTC)
