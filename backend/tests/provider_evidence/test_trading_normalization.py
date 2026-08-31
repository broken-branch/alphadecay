from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderType
from alpaca.trading.enums import PositionIntent as AlpacaPositionIntent
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, ReplaceOrderRequest

from backend.app.alpaca.execution_evidence import baseline_account_fingerprint
from backend.app.alpaca.trading import (
    AlpacaOrderWriteAdapter,
    AlpacaTradingReadAdapter,
    ProviderDataError,
)
from backend.app.contracts.v1 import AccountRole, DataQuality, PositionIntent
from backend.app.execution import (
    AmbiguousBrokerResponse,
    ExecutionAction,
    OrderEnvelope,
    OrderLegIntent,
)

ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000901")
ACCOUNT_FINGERPRINT = baseline_account_fingerprint(ACCOUNT_ID)


class FixtureTradingClient:
    def __init__(self) -> None:
        self.account: object = {
            "id": ACCOUNT_ID,
            "status": "ACTIVE",
            "equity": "100000.00",
            "buying_power": "200000.00",
            "account_blocked": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "trade_suspended_by_user": False,
        }
        self.positions: object = [
            {
                "asset_class": "us_option",
                "symbol": "NVDA260918C00230000",
                "underlying_symbol": "NVDA",
                "expiration_date": "2026-09-18",
                "strike_price": "230",
                "option_type": "call",
                "qty": "2",
                "multiplier": "100",
            },
            {
                "asset_class": "us_option",
                "symbol": "NVDA260918C00240000",
                "underlying_symbol": "NVDA",
                "expiration_date": "2026-09-18",
                "strike_price": "240",
                "option_type": "call",
                "qty": "-2",
                "multiplier": "100",
            },
        ]
        self.orders: object = [
            {
                "id": "provider-order-1",
                "client_order_id": "approved-a0",
                "status": "new",
                "order_class": "mleg",
                "type": "limit",
                "qty": "2",
                "filled_qty": "0",
                "limit_price": "1.25",
                "submitted_at": "2026-08-28T15:10:00Z",
            }
        ]

    def get_account(self) -> object:
        return self.account

    def get_all_positions(self) -> object:
        return self.positions

    def get_orders(self, filter: GetOrdersRequest | None = None) -> object:
        assert filter is not None
        assert filter.status.value == "open"
        assert filter.nested is True
        return self.orders


def adapter(client: FixtureTradingClient | None = None) -> AlpacaTradingReadAdapter:
    return AlpacaTradingReadAdapter(
        client or FixtureTradingClient(),
        account_role=AccountRole.DEVELOPMENT,
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
        baseline_status=DataQuality.COMPLETE,
        autonomous_enabled=False,
    )


def test_account_position_and_order_fixtures_normalize_to_bounded_types() -> None:
    trading = adapter()

    account = trading.get_account()
    positions = trading.list_positions()
    orders = trading.list_open_orders()

    assert account.paper is True
    assert account.equity == Decimal("100000.00")
    assert len(positions.positions) == 2
    assert positions.positions[0].legs[0].intent == PositionIntent.BUY_TO_OPEN
    assert positions.positions[1].legs[0].intent == PositionIntent.SELL_TO_OPEN
    assert positions.positions[0].fingerprint != positions.positions[1].fingerprint
    assert orders == (
        {
            "provider_order_id": "provider-order-1",
            "client_order_id": "approved-a0",
            "status": "new",
            "order_class": "mleg",
            "order_type": "limit",
            "quantity": 2,
            "filled_quantity": 0,
            "limit_price": "1.25",
            "submitted_at": "2026-08-28T15:10:00Z",
        },
    )


def test_blocked_account_fails_with_specific_quality_code() -> None:
    client = FixtureTradingClient()
    assert isinstance(client.account, dict)
    client.account["trading_blocked"] = True

    with pytest.raises(ProviderDataError, match="ACCOUNT_BLOCKED"):
        adapter(client).get_account()


def test_unrelated_provider_fields_do_not_break_required_position_validation() -> None:
    client = FixtureTradingClient()
    assert isinstance(client.positions, list)
    assert isinstance(client.positions[0], dict)
    client.positions[0]["unexpected"] = "not accepted"

    assert len(adapter(client).list_positions().positions) == 2


def test_account_position_and_order_sdk_models_validate_from_attributes() -> None:
    client = FixtureTradingClient()
    assert isinstance(client.account, dict)
    assert isinstance(client.positions, list)
    assert isinstance(client.orders, list)
    client.account = SimpleNamespace(**client.account, evolving_provider_field="ignored")
    client.positions = [
        SimpleNamespace(**item, evolving_provider_field="ignored") for item in client.positions
    ]
    client.orders = [
        SimpleNamespace(**item, evolving_provider_field="ignored") for item in client.orders
    ]

    trading = adapter(client)

    assert trading.get_account().equity == Decimal("100000.00")
    assert len(trading.list_positions().positions) == 2
    assert trading.list_open_orders()[0]["provider_order_id"] == "provider-order-1"


def test_account_still_rejects_missing_required_provider_field() -> None:
    client = FixtureTradingClient()
    assert isinstance(client.account, dict)
    del client.account["trading_blocked"]

    with pytest.raises(ProviderDataError, match="ACCOUNT_SCHEMA_INVALID"):
        adapter(client).get_account()


def test_open_order_timestamp_requires_timezone() -> None:
    client = FixtureTradingClient()
    assert isinstance(client.orders, list)
    assert isinstance(client.orders[0], dict)
    client.orders[0]["submitted_at"] = "2026-08-28T15:10:00"

    with pytest.raises(ProviderDataError, match="ORDER_SCHEMA_INVALID"):
        adapter(client).list_open_orders()


def test_open_order_accepts_alpaca_pending_replace_as_nonterminal() -> None:
    client = FixtureTradingClient()
    assert isinstance(client.orders, list)
    assert isinstance(client.orders[0], dict)
    client.orders[0]["status"] = "pending_replace"
    client.orders[0]["filled_qty"] = "1"

    order = adapter(client).list_open_orders()[0]

    assert order["status"] == "pending_replace"
    assert order["filled_quantity"] == 1


@pytest.mark.parametrize("status", ["canceled", "replaced"])
def test_open_order_rejects_terminal_status_with_full_fill(status: str) -> None:
    client = FixtureTradingClient()
    assert isinstance(client.orders, list)
    assert isinstance(client.orders[0], dict)
    client.orders[0]["status"] = status
    client.orders[0]["filled_qty"] = client.orders[0]["qty"]

    with pytest.raises(ProviderDataError, match="ORDER_SCHEMA_INVALID"):
        adapter(client).list_open_orders()


def test_open_order_accepts_calculated_full_fill_while_settlement_is_pending() -> None:
    client = FixtureTradingClient()
    assert isinstance(client.orders, list)
    assert isinstance(client.orders[0], dict)
    client.orders[0]["status"] = "calculated"
    client.orders[0]["filled_qty"] = client.orders[0]["qty"]

    order = adapter(client).list_open_orders()[0]

    assert order["status"] == "calculated"
    assert order["filled_quantity"] == order["quantity"]


class FixtureOrderClient:
    def __init__(self) -> None:
        self.submitted: list[LimitOrderRequest] = []
        self.replaced: list[tuple[str, ReplaceOrderRequest]] = []
        self.canceled: list[str] = []

    def submit_order(self, order_data: LimitOrderRequest) -> object:
        self.submitted.append(order_data)
        return broker_order(client_order_id=order_data.client_order_id or "")

    def get_order_by_client_id(self, client_id: str) -> object:
        return broker_order(client_order_id=client_id)

    def get_order_by_id(self, order_id: str) -> object:
        return broker_order(client_order_id="approved-a0", status="canceled")

    def replace_order_by_id(self, order_id: str, order_data: ReplaceOrderRequest) -> object:
        self.replaced.append((order_id, order_data))
        return broker_order(client_order_id=order_data.client_order_id or "")

    def cancel_order_by_id(self, order_id: str) -> None:
        self.canceled.append(order_id)


def broker_order(*, client_order_id: str, status: str = "new") -> dict[str, object]:
    return {
        "id": "00000000-0000-0000-0000-000000000801",
        "client_order_id": client_order_id,
        "status": status,
        "qty": "2",
        "filled_qty": "0",
    }


def order_envelope() -> OrderEnvelope:
    return OrderEnvelope(
        action=ExecutionAction.ENTRY,
        authorization_certificate_id=UUID("00000000-0000-0000-0000-000000000802"),
        policy_hash="policy-v0.1",
        account_fingerprint="account-fingerprint",
        position_or_book_fingerprint="book-fingerprint",
        legs=(
            OrderLegIntent("NVDA260918C00230000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("NVDA260918C00240000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        quantity=2,
        minimum_limit=Decimal("1.00"),
        maximum_limit=Decimal("1.50"),
        approved_max_loss=Decimal("700"),
        event_key="NVDA-2026-08-28",
        trading_day=date(2026, 8, 28),
    )


def test_write_adapter_builds_one_exact_day_limit_mleg_request() -> None:
    client = FixtureOrderClient()
    trading = AlpacaOrderWriteAdapter(client)

    result = trading.submit(order_envelope(), "approved-a0")

    assert result.state == "NEW"
    assert result.quantity == 2
    assert len(client.submitted) == 1
    request = client.submitted[0]
    assert request.order_class == OrderClass.MLEG
    assert request.type == OrderType.LIMIT
    assert request.time_in_force.value == "day"
    assert request.extended_hours is False
    assert request.symbol is None and request.side is None
    assert request.qty == 2
    assert request.limit_price == 1.0
    assert request.client_order_id == "approved-a0"
    assert [leg.position_intent for leg in request.legs or []] == [
        AlpacaPositionIntent.BUY_TO_OPEN,
        AlpacaPositionIntent.SELL_TO_OPEN,
    ]


def test_write_adapter_uses_targeted_replace_cancel_and_lookup() -> None:
    client = FixtureOrderClient()
    trading = AlpacaOrderWriteAdapter(client)

    assert trading.lookup("approved-a0") is not None
    replacement = trading.replace(
        "00000000-0000-0000-0000-000000000801", "approved-a1", Decimal("1.10")
    )
    canceled = trading.cancel("00000000-0000-0000-0000-000000000801")

    assert replacement.state == "NEW"
    assert client.replaced[0][1].client_order_id == "approved-a1"
    assert client.replaced[0][1].limit_price == 1.1
    assert client.canceled == ["00000000-0000-0000-0000-000000000801"]
    assert canceled.state == "CANCELED"


def test_write_adapter_marks_malformed_post_submit_quantity_as_ambiguous() -> None:
    class MalformedClient(FixtureOrderClient):
        def submit_order(self, order_data: LimitOrderRequest) -> object:
            payload = broker_order(client_order_id=order_data.client_order_id or "")
            payload["filled_qty"] = "3"
            return payload

    with pytest.raises(AmbiguousBrokerResponse, match="SUBMIT_OUTCOME_UNKNOWN"):
        AlpacaOrderWriteAdapter(MalformedClient()).submit(order_envelope(), "approved-a0")


def test_write_adapter_returns_none_only_for_definitive_lookup_404() -> None:
    class Response:
        status_code = 404

    class HttpError:
        response = Response()

    class MissingClient(FixtureOrderClient):
        def get_order_by_client_id(self, client_id: str) -> object:
            raise APIError('{"code":40410000,"message":"order not found"}', HttpError())

    assert AlpacaOrderWriteAdapter(MissingClient()).lookup("missing-a0") is None


def test_write_adapter_marks_transport_failure_as_ambiguous() -> None:
    class TimeoutClient(FixtureOrderClient):
        def submit_order(self, order_data: LimitOrderRequest) -> object:
            raise requests.Timeout("outcome unknown")

    with pytest.raises(AmbiguousBrokerResponse):
        AlpacaOrderWriteAdapter(TimeoutClient()).submit(order_envelope(), "approved-a0")


def test_write_adapter_rejects_response_for_a_different_client_order_id() -> None:
    class WrongOrderClient(FixtureOrderClient):
        def get_order_by_client_id(self, client_id: str) -> object:
            return broker_order(client_order_id="different-a0")

    with pytest.raises(ProviderDataError, match="BROKER_ORDER_LINEAGE_MISMATCH"):
        AlpacaOrderWriteAdapter(WrongOrderClient()).lookup("approved-a0")


def test_write_adapter_rejects_an_unknown_broker_order_state() -> None:
    class UnknownStateClient(FixtureOrderClient):
        def get_order_by_client_id(self, client_id: str) -> object:
            return broker_order(client_order_id=client_id, status="settled_by_magic")

    with pytest.raises(ProviderDataError, match="BROKER_ORDER_SCHEMA_INVALID"):
        AlpacaOrderWriteAdapter(UnknownStateClient()).lookup("approved-a0")


def test_write_adapter_marks_uncertain_http_status_as_ambiguous() -> None:
    class Response:
        status_code = 503

    class HttpError:
        response = Response()

    class UnavailableClient(FixtureOrderClient):
        def replace_order_by_id(self, order_id: str, order_data: ReplaceOrderRequest) -> object:
            raise APIError('{"code":50310000,"message":"unavailable"}', HttpError())

    with pytest.raises(AmbiguousBrokerResponse):
        AlpacaOrderWriteAdapter(UnavailableClient()).replace(
            "00000000-0000-0000-0000-000000000801",
            "approved-a1",
            Decimal("1.10"),
        )
