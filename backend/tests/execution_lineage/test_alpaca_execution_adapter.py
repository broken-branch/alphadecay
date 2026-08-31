from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from alpaca.trading.requests import LimitOrderRequest, ReplaceOrderRequest

from backend.app.alpaca.trading import AlpacaOrderWriteAdapter, ProviderDataError
from backend.app.contracts.v1 import PositionIntent
from backend.app.execution import (
    AmbiguousBrokerResponse,
    ExecutionAction,
    OrderEnvelope,
    OrderLegIntent,
)


class FilledOrderClient:
    def submit_order(self, order_data: LimitOrderRequest) -> object:
        return filled_order(order_data.client_order_id or "")

    def get_order_by_client_id(self, client_id: str) -> object:
        return filled_order(client_id)

    def get_order_by_id(self, order_id: str) -> object:
        return filled_order("approved-a0")

    def replace_order_by_id(self, order_id: str, order_data: ReplaceOrderRequest) -> object:
        return filled_order(order_data.client_order_id or "")

    def cancel_order_by_id(self, order_id: str) -> None:
        pass


def test_nested_leg_fills_derive_authoritative_cash_flow() -> None:
    adapter = AlpacaOrderWriteAdapter(FilledOrderClient())

    submitted = adapter.submit(order_envelope(), "approved-a0")
    looked_up = adapter.lookup("approved-a0")

    assert submitted.fill_cash_flow == Decimal("-240")
    assert looked_up is not None
    assert looked_up.fill_cash_flow == Decimal("-240")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("side", "sell"),
        ("filled_qty", "1"),
        ("filled_avg_price", "0"),
        ("filled_avg_price", "NaN"),
    ],
)
def test_nested_fill_rejects_wrong_side_quantity_or_price(
    field: str,
    value: str,
) -> None:
    class MalformedClient(FilledOrderClient):
        def submit_order(self, order_data: LimitOrderRequest) -> object:
            payload = filled_order(order_data.client_order_id or "")
            legs = payload["legs"]
            assert isinstance(legs, list)
            leg = legs[0]
            assert isinstance(leg, dict)
            leg[field] = value
            return payload

    with pytest.raises(AmbiguousBrokerResponse, match="SUBMIT_OUTCOME_UNKNOWN"):
        AlpacaOrderWriteAdapter(MalformedClient()).submit(
            order_envelope(),
            "approved-a0",
        )


@pytest.mark.parametrize("operation", ["submit", "replace", "cancel"])
def test_malformed_response_after_mutation_is_ambiguous(operation: str) -> None:
    client = MalformedMutationClient()
    adapter = AlpacaOrderWriteAdapter(client)

    with pytest.raises(AmbiguousBrokerResponse, match=f"{operation.upper()}_OUTCOME_UNKNOWN"):
        if operation == "submit":
            adapter.submit(order_envelope(), "approved-a0")
        elif operation == "replace":
            adapter.replace("provider-order-1", "approved-a1", Decimal("1.25"))
        else:
            adapter.cancel("provider-order-1")

    assert client.mutations == [operation]


@pytest.mark.parametrize(
    ("status", "filled_qty"),
    [("canceled", "2"), ("replaced", "2"), ("rejected", "1")],
)
def test_lookup_uses_the_shared_status_fill_matrix(status: str, filled_qty: str) -> None:
    class MalformedLookupClient(FilledOrderClient):
        def get_order_by_client_id(self, client_id: str) -> object:
            payload = filled_order(client_id)
            payload["status"] = status
            payload["filled_qty"] = filled_qty
            return payload

    with pytest.raises(ProviderDataError, match="BROKER_ORDER_SCHEMA_INVALID"):
        AlpacaOrderWriteAdapter(MalformedLookupClient()).lookup("approved-a0")


class MalformedMutationClient(FilledOrderClient):
    def __init__(self) -> None:
        self.mutations: list[str] = []

    def submit_order(self, order_data: LimitOrderRequest) -> object:
        self.mutations.append("submit")
        return {"id": "provider-order-1"}

    def replace_order_by_id(self, order_id: str, order_data: ReplaceOrderRequest) -> object:
        self.mutations.append("replace")
        return {"id": order_id}

    def cancel_order_by_id(self, order_id: str) -> None:
        self.mutations.append("cancel")

    def get_order_by_id(self, order_id: str) -> object:
        return {"id": order_id}


def filled_order(client_order_id: str) -> dict[str, object]:
    return {
        "id": "00000000-0000-0000-0000-000000000801",
        "client_order_id": client_order_id,
        "status": "filled",
        "qty": "2",
        "filled_qty": "2",
        "legs": [
            {
                "symbol": "NVDA260918C00230000",
                "side": "buy",
                "ratio_qty": "1",
                "filled_qty": "2",
                "filled_avg_price": "1.50",
            },
            {
                "symbol": "NVDA260918C00240000",
                "side": "sell",
                "ratio_qty": "1",
                "filled_qty": "2",
                "filled_avg_price": "0.30",
            },
        ],
    }


def order_envelope() -> OrderEnvelope:
    return OrderEnvelope(
        action=ExecutionAction.ENTRY,
        authorization_certificate_id=UUID("00000000-0000-0000-0000-000000000802"),
        policy_hash="policy-v0.1",
        account_fingerprint="account-fingerprint",
        position_or_book_fingerprint="book-fingerprint",
        legs=(
            OrderLegIntent(
                "NVDA260918C00230000",
                PositionIntent.BUY_TO_OPEN,
                1,
            ),
            OrderLegIntent(
                "NVDA260918C00240000",
                PositionIntent.SELL_TO_OPEN,
                1,
            ),
        ),
        quantity=2,
        minimum_limit=Decimal("1.00"),
        maximum_limit=Decimal("1.50"),
        approved_max_loss=Decimal("700"),
        event_key="NVDA-2026-08-28",
        trading_day=date(2026, 8, 28),
    )
