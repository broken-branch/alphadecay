from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import (
    OrderClass,
    OrderType,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.enums import (
    PositionIntent as AlpacaPositionIntent,
)
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    OptionLegRequest,
    ReplaceOrderRequest,
)
from pydantic import BaseModel, ConfigDict, ValidationError
from requests import ConnectionError as RequestsConnectionError
from requests import Timeout as RequestsTimeout

from backend.app.alpaca.execution_evidence import baseline_account_fingerprint
from backend.app.contracts.v1 import (
    AccountResponse,
    AccountRole,
    DataQuality,
    OptionLeg,
    OptionRight,
    PositionIntent,
    PositionListResponse,
    PositionResponse,
)
from backend.app.domain.option_contract_symbol import parse_standard_option_contract_symbol
from backend.app.execution import (
    AmbiguousBrokerResponse,
    BrokerResult,
    OrderEnvelope,
    OrderLegIntent,
)
from backend.app.execution.order_status import (
    BrokerOrderPhase,
    broker_order_phase,
    broker_state_matches_fill,
    normalize_broker_order_state,
)


class ProviderDataError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TradingFixtureClient(Protocol):
    def get_account(self) -> object: ...
    def get_all_positions(self) -> object: ...
    def get_orders(self, filter: GetOrdersRequest | None = None) -> object: ...


class OrderWriteClient(Protocol):
    def submit_order(self, order_data: LimitOrderRequest) -> object: ...
    def get_order_by_client_id(self, client_id: str) -> object: ...
    def get_order_by_id(self, order_id: str) -> object: ...
    def replace_order_by_id(self, order_id: str, order_data: ReplaceOrderRequest) -> object: ...
    def cancel_order_by_id(self, order_id: str) -> None: ...


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, from_attributes=True)


class _AccountPayload(_ProviderModel):
    id: UUID
    status: str
    equity: Decimal
    buying_power: Decimal
    account_blocked: bool
    trading_blocked: bool
    transfers_blocked: bool
    trade_suspended_by_user: bool


class _PositionPayload(_ProviderModel):
    """Alpaca's position object omits contract terms; they are derived from the OCC symbol."""

    asset_class: str
    symbol: str
    qty: Decimal
    underlying_symbol: str | None = None
    expiration_date: date | None = None
    strike_price: Decimal | None = None
    option_type: str | None = None
    multiplier: Decimal | None = None


class _OrderPayload(_ProviderModel):
    id: str
    client_order_id: str
    status: str
    order_class: str
    type: str
    qty: Decimal
    filled_qty: Decimal
    limit_price: Decimal
    submitted_at: datetime


class _BrokerLegPayload(_ProviderModel):
    symbol: str
    side: Literal["buy", "sell"]
    ratio_qty: Decimal
    filled_qty: Decimal
    filled_avg_price: Decimal | None


class _BrokerOrderPayload(_ProviderModel):
    id: UUID | str
    client_order_id: str
    status: str
    qty: Decimal
    filled_qty: Decimal
    legs: tuple[_BrokerLegPayload, ...] | None = None


class AlpacaTradingReadAdapter:
    def __init__(
        self,
        client: TradingFixtureClient,
        *,
        account_role: AccountRole,
        expected_account_fingerprint: str,
        baseline_status: DataQuality,
        autonomous_enabled: bool,
    ) -> None:
        self._client = client
        self._account_role = account_role
        self._expected_account_fingerprint = expected_account_fingerprint
        self._baseline_status = baseline_status
        self._autonomous_enabled = autonomous_enabled

    def get_account(self) -> AccountResponse:
        payload = self._account_payload()
        if payload.status != "ACTIVE" or any(
            (
                payload.account_blocked,
                payload.trading_blocked,
                payload.transfers_blocked,
                payload.trade_suspended_by_user,
            )
        ):
            raise ProviderDataError("ACCOUNT_BLOCKED")
        return AccountResponse(
            role=self._account_role,
            paper=True,
            equity=payload.equity,
            buying_power=payload.buying_power,
            baseline_status=self._baseline_status,
            autonomous_enabled=self._autonomous_enabled,
        )

    def list_positions(self) -> PositionListResponse:
        self._account_payload()
        raw_positions = self._client.get_all_positions()
        if not isinstance(raw_positions, list):
            raise ProviderDataError("POSITION_SCHEMA_INVALID")
        try:
            payloads = tuple(_PositionPayload.model_validate(item) for item in raw_positions)
            positions = tuple(self._normalize_position(payload) for payload in payloads)
        except (ValidationError, ValueError) as exc:
            raise ProviderDataError("POSITION_SCHEMA_INVALID") from exc
        return PositionListResponse(positions=positions)

    def list_open_orders(self) -> tuple[dict[str, object], ...]:
        self._account_payload()
        raw_orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)
        )
        if not isinstance(raw_orders, list):
            raise ProviderDataError("ORDER_SCHEMA_INVALID")
        try:
            payloads = tuple(_OrderPayload.model_validate(item) for item in raw_orders)
            return tuple(self._normalize_order(payload) for payload in payloads)
        except (ValidationError, ValueError) as exc:
            raise ProviderDataError("ORDER_SCHEMA_INVALID") from exc

    def _account_payload(self) -> _AccountPayload:
        try:
            payload = _AccountPayload.model_validate(self._client.get_account())
        except ValidationError as exc:
            raise ProviderDataError("ACCOUNT_SCHEMA_INVALID") from exc
        if baseline_account_fingerprint(payload.id) != self._expected_account_fingerprint:
            raise ProviderDataError("ACCOUNT_FINGERPRINT_MISMATCH")
        return payload

    def _normalize_position(self, payload: _PositionPayload) -> PositionResponse:
        if payload.asset_class != "us_option":
            raise ValueError("unsupported option position")
        contract = parse_standard_option_contract_symbol(payload.symbol)
        underlying = payload.underlying_symbol or contract.root_symbol
        expiry = payload.expiration_date or contract.expiration_date
        strike = payload.strike_price if payload.strike_price is not None else contract.strike_price
        option_type = payload.option_type or ("call" if contract.right == "C" else "put")
        multiplier = payload.multiplier if payload.multiplier is not None else Decimal(100)
        if (
            multiplier != Decimal(100)
            or underlying != contract.root_symbol
            or expiry != contract.expiration_date
            or strike != contract.strike_price
        ):
            raise ValueError("unsupported option position")
        quantity = _whole_positive_quantity(abs(payload.qty))
        if payload.qty == 0:
            raise ValueError("zero position")
        option_right = {
            "call": OptionRight.CALL,
            "put": OptionRight.PUT,
        }.get(option_type.lower())
        if option_right is None or option_right.value[0].upper() != contract.right:
            raise ValueError("unknown option type")
        intent = PositionIntent.BUY_TO_OPEN if payload.qty > 0 else PositionIntent.SELL_TO_OPEN
        leg = OptionLeg(
            symbol=payload.symbol,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            right=option_right,
            intent=intent,
            ratio=1,
            quantity=quantity,
            multiplier=100,
        )
        fingerprint_material = leg.model_dump(mode="json")
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return PositionResponse(
            position_id=uuid5(NAMESPACE_URL, f"alphadecay:{self._account_role}:{payload.symbol}"),
            role=self._account_role,
            underlying=underlying,
            legs=(leg,),
            current_exposure=None,
            quality=DataQuality.COMPLETE,
            fingerprint=fingerprint,
        )

    @staticmethod
    def _normalize_order(payload: _OrderPayload) -> dict[str, object]:
        quantity = _whole_positive_quantity(payload.qty)
        filled_quantity = _whole_nonnegative_quantity(payload.filled_qty)
        if payload.submitted_at.tzinfo is None or payload.submitted_at.utcoffset() is None:
            raise ValueError("submitted_at requires timezone")
        if not broker_state_matches_fill(payload.status, filled_quantity, quantity):
            raise ValueError("order state and fill quantity disagree")
        if broker_order_phase(payload.status) is BrokerOrderPhase.TERMINAL:
            raise ValueError("terminal order returned by open-order operation")
        return {
            "provider_order_id": payload.id,
            "client_order_id": payload.client_order_id,
            "status": payload.status.lower(),
            "order_class": payload.order_class.lower(),
            "order_type": payload.type.lower(),
            "quantity": quantity,
            "filled_quantity": filled_quantity,
            "limit_price": str(payload.limit_price),
            "submitted_at": payload.submitted_at.isoformat().replace("+00:00", "Z"),
        }


class AlpacaOrderWriteAdapter:
    def __init__(self, client: OrderWriteClient) -> None:
        self._client = client

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        order = LimitOrderRequest(
            qty=request.quantity,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.MLEG,
            extended_hours=False,
            client_order_id=client_id,
            legs=[
                OptionLegRequest(
                    symbol=leg.symbol,
                    ratio_qty=leg.ratio,
                    position_intent=_ALPACA_POSITION_INTENTS[leg.intent],
                )
                for leg in request.legs
            ],
            limit_price=float(request.minimum_limit),
        )
        try:
            raw = self._client.submit_order(order)
            return _normalize_broker_order(
                raw,
                expected_client_id=client_id,
                expected_quantity=request.quantity,
                expected_legs=request.legs,
            )
        except APIError as exc:
            if _is_outcome_uncertain(exc):
                raise AmbiguousBrokerResponse("SUBMIT_OUTCOME_UNKNOWN") from exc
            raise
        except (RequestsConnectionError, RequestsTimeout) as exc:
            raise AmbiguousBrokerResponse("SUBMIT_OUTCOME_UNKNOWN") from exc
        except Exception as exc:
            raise AmbiguousBrokerResponse("SUBMIT_OUTCOME_UNKNOWN") from exc

    def lookup(self, client_id: str) -> BrokerResult | None:
        try:
            raw = self._client.get_order_by_client_id(client_id)
        except APIError as exc:
            if exc.status_code == 404:
                return None
            if _is_outcome_uncertain(exc):
                raise AmbiguousBrokerResponse("LOOKUP_OUTCOME_UNKNOWN") from exc
            raise
        except (RequestsConnectionError, RequestsTimeout) as exc:
            raise AmbiguousBrokerResponse("LOOKUP_OUTCOME_UNKNOWN") from exc
        return _normalize_broker_order(raw, expected_client_id=client_id)

    def replace(self, provider_order_id: str, client_id: str, limit: Decimal) -> BrokerResult:
        request = ReplaceOrderRequest(limit_price=float(limit), client_order_id=client_id)
        try:
            raw = self._client.replace_order_by_id(provider_order_id, request)
            return _normalize_broker_order(raw, expected_client_id=client_id)
        except APIError as exc:
            if _is_outcome_uncertain(exc):
                raise AmbiguousBrokerResponse("REPLACE_OUTCOME_UNKNOWN") from exc
            raise
        except (RequestsConnectionError, RequestsTimeout) as exc:
            raise AmbiguousBrokerResponse("REPLACE_OUTCOME_UNKNOWN") from exc
        except Exception as exc:
            raise AmbiguousBrokerResponse("REPLACE_OUTCOME_UNKNOWN") from exc

    def cancel(self, provider_order_id: str) -> BrokerResult:
        dispatched = False
        try:
            self._client.cancel_order_by_id(provider_order_id)
            dispatched = True
            raw = self._client.get_order_by_id(provider_order_id)
            return _normalize_broker_order(raw)
        except APIError as exc:
            if dispatched or _is_outcome_uncertain(exc):
                raise AmbiguousBrokerResponse("CANCEL_OUTCOME_UNKNOWN") from exc
            raise
        except (RequestsConnectionError, RequestsTimeout) as exc:
            raise AmbiguousBrokerResponse("CANCEL_OUTCOME_UNKNOWN") from exc
        except Exception as exc:
            raise AmbiguousBrokerResponse("CANCEL_OUTCOME_UNKNOWN") from exc


_ALPACA_POSITION_INTENTS = {
    PositionIntent.BUY_TO_OPEN: AlpacaPositionIntent.BUY_TO_OPEN,
    PositionIntent.SELL_TO_OPEN: AlpacaPositionIntent.SELL_TO_OPEN,
    PositionIntent.BUY_TO_CLOSE: AlpacaPositionIntent.BUY_TO_CLOSE,
    PositionIntent.SELL_TO_CLOSE: AlpacaPositionIntent.SELL_TO_CLOSE,
}


def _is_outcome_uncertain(error: APIError) -> bool:
    status = error.status_code
    return status in {408, 429} or status is not None and status >= 500


def _normalize_broker_order(
    raw: object,
    *,
    expected_client_id: str | None = None,
    expected_quantity: int | None = None,
    expected_legs: tuple[OrderLegIntent, ...] | None = None,
) -> BrokerResult:
    try:
        payload = _BrokerOrderPayload.model_validate(raw, from_attributes=True)
        quantity = _whole_positive_quantity(payload.qty)
        filled_quantity = _whole_nonnegative_quantity(payload.filled_qty)
        state = normalize_broker_order_state(payload.status)
        if not broker_state_matches_fill(state, filled_quantity, quantity):
            raise ValueError("invalid broker order state")
    except ValidationError as exc:
        if any(error["loc"] and error["loc"][0] == "legs" for error in exc.errors()):
            raise ProviderDataError("BROKER_ORDER_FILL_INVALID") from exc
        raise ProviderDataError("BROKER_ORDER_SCHEMA_INVALID") from exc
    except ValueError as exc:
        raise ProviderDataError("BROKER_ORDER_SCHEMA_INVALID") from exc
    if expected_client_id is not None and payload.client_order_id != expected_client_id:
        raise ProviderDataError("BROKER_ORDER_LINEAGE_MISMATCH")
    if expected_quantity is not None and quantity != expected_quantity:
        raise ProviderDataError("BROKER_ORDER_QUANTITY_MISMATCH")
    try:
        fill_cash_flow = _fill_cash_flow(
            payload.legs,
            filled_quantity,
            expected_legs,
        )
    except ValueError as exc:
        raise ProviderDataError("BROKER_ORDER_FILL_INVALID") from exc
    return BrokerResult(
        provider_order_id=str(payload.id),
        state=state,
        filled_quantity=filled_quantity,
        quantity=quantity,
        fill_cash_flow=fill_cash_flow,
    )


def _fill_cash_flow(
    legs: tuple[_BrokerLegPayload, ...] | None,
    parent_filled_quantity: int,
    expected_legs: tuple[OrderLegIntent, ...] | None,
) -> Decimal | None:
    if parent_filled_quantity == 0:
        if legs is not None and any(leg.filled_qty != 0 for leg in legs):
            raise ValueError("leg fill without parent fill")
        return None
    if legs is None or len(legs) not in {2, 4}:
        raise ValueError("nested fills required")
    if len({leg.symbol for leg in legs}) != len(legs):
        raise ValueError("duplicate filled leg")
    expected_by_symbol = (
        {leg.symbol: leg for leg in expected_legs} if expected_legs is not None else None
    )
    if expected_by_symbol is not None and set(expected_by_symbol) != {leg.symbol for leg in legs}:
        raise ValueError("filled legs do not match request")
    cash_flow = Decimal(0)
    for leg in legs:
        ratio = _whole_positive_quantity(leg.ratio_qty)
        filled_quantity = _whole_positive_quantity(leg.filled_qty)
        if filled_quantity != parent_filled_quantity * ratio:
            raise ValueError("filled leg quantity mismatch")
        if (
            leg.filled_avg_price is None
            or not leg.filled_avg_price.is_finite()
            or leg.filled_avg_price <= 0
        ):
            raise ValueError("filled leg price invalid")
        if expected_by_symbol is not None:
            expected = expected_by_symbol[leg.symbol]
            expected_side = (
                "buy"
                if expected.intent in {PositionIntent.BUY_TO_OPEN, PositionIntent.BUY_TO_CLOSE}
                else "sell"
            )
            if ratio != expected.ratio or leg.side != expected_side:
                raise ValueError("filled leg request mismatch")
        sign = Decimal(-1) if leg.side == "buy" else Decimal(1)
        cash_flow += sign * filled_quantity * leg.filled_avg_price * 100
    if not cash_flow.is_finite() or cash_flow == 0:
        raise ValueError("filled order cash flow invalid")
    return cash_flow


def _whole_positive_quantity(value: Decimal) -> int:
    quantity = _whole_nonnegative_quantity(value)
    if quantity == 0:
        raise ValueError("quantity must be positive")
    return quantity


def _whole_nonnegative_quantity(value: Decimal) -> int:
    integral = value.to_integral_value()
    if value != integral or value < 0:
        raise ValueError("quantity must be a nonnegative integer")
    return int(integral)
