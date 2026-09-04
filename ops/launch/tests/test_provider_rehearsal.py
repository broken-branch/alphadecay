from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import os
import sys
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alpaca.trading.enums import OrderType
from alpaca.trading.models import Order, Position, TradeAccount, USDPositionValues
from pydantic import BaseModel

import ops.launch.provider_rehearsal as rehearsal
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import Actor
from backend.app.services.acquisition import (
    AcquisitionFailure,
    AcquisitionKind,
    CalibrationBinding,
    ObservedPaperAccountAuthority,
    PermanentAccountLatch,
)
from backend.app.services.agent import (
    AgentRunResult,
    AgentRunService,
    AgentTick,
    PersistedAgentDecision,
)
from ops.launch.provider_rehearsal import (
    MAX_PROVIDER_STRING,
    PAPER_ENDPOINT,
    ActivityHttpBoundary,
    ArtifactStore,
    BookCapture,
    CliPin,
    Credentials,
    DurableAuthorityEvidence,
    FixtureSubmissionBoundary,
    HttpResponse,
    MCPBoundary,
    Mode,
    RehearsalError,
    RehearsalResult,
    SafetyCounters,
    TradingBoundary,
    TransportLedger,
    VerifiedCli,
    _probe_canonical_development,
    _probe_canonical_submission,
    _qualify_provider_rehearsal,
    _validate_preexisting_authority,
    run_development_operator,
    run_fixture_development,
    run_fixture_submission_no_trade,
    run_submission_no_trade_operator,
)

NOW = datetime(2026, 8, 29, 12, 34, 56, tzinfo=UTC)


class FailingLifecycleAcquisition:
    async def acquire(self, *_args: object, **_kwargs: object) -> object:
        raise AcquisitionFailure(
            AcquisitionKind.LIFECYCLE,
            "CONTEXT_ACTIVE_POSITION_NOT_UNIQUE",
        )


def _official_doctor_output() -> str:
    return "\n".join(
        [
            "Alpaca CLI 0.0.13",
            "  Go:       go1.25.0",
            "  OS/Arch:  linux/amd64",
            "",
            "Config:     /operator/config",
            "  ✓ config directory does not exist (ok when using env vars)",
            "  ✓ no saved profiles configured (using env var credentials)",
            "  ✓ active profile: paper",
            "  ✓ API key credentials from env (ALPACA_API_KEY + ALPACA_SECRET_KEY)",
            "",
            "Connectivity:",
            f"  Trading:  {PAPER_ENDPOINT}",
            "  ✓ trading API: connected",
            "  Data:     https://data.alpaca.markets",
            "  ✓ data API: connected",
            "",
            "Update:",
            "  ✓ up to date (0.0.13)",
            "",
            "All checks passed.",
        ]
    )


def _archive(tmp_path: Path, binary: bytes = b"#!/bin/sh\nexit 0\n") -> tuple[Path, CliPin]:
    archive = tmp_path / "alpaca.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("alpaca")
        info.mode = 0o755
        info.size = len(binary)
        bundle.addfile(info, io.BytesIO(binary))
    return archive, CliPin(
        version="0.0.13",
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        binary_sha256=hashlib.sha256(binary).hexdigest(),
        archive_member="alpaca",
    )


@dataclass
class FakeProcess:
    outputs: list[str] = field(
        default_factory=lambda: [
            "0.0.13\n",
            _official_doctor_output(),
            json.dumps(
                {
                    "client_order_id": "dry",
                    "legs": [
                        {
                            "symbol": "ZZZZ991231C00001000",
                            "ratio_qty": "1",
                            "position_intent": "buy_to_open",
                        },
                        {
                            "symbol": "ZZZZ991231C00002000",
                            "ratio_qty": "1",
                            "position_intent": "sell_to_open",
                        },
                    ],
                    "limit_price": "0.01",
                    "order_class": "mleg",
                    "qty": "1",
                    "time_in_force": "day",
                    "type": "limit",
                    "advanced_instructions": {},
                }
            ),
        ]
    )
    calls: list[tuple[int, tuple[str, ...]]] = field(default_factory=list)

    def run_fd(self, descriptor: int, argv: tuple[str, ...], environment: dict[str, str]) -> str:
        assert set(environment) == {
            "ALPACA_API_KEY",
            "ALPACA_SECRET_KEY",
            "ALPACA_LIVE_TRADE",
            "LC_ALL",
        }
        self.calls.append((descriptor, argv))
        return self.outputs.pop(0)


class FakeTradingClient:
    def __init__(self) -> None:
        self._session = FakeSession()
        self.account: object = _account_model()
        self.positions: list[object] = []
        self.orders: list[object] = []

    def get_account(self):
        self._session.request("GET", PAPER_ENDPOINT + "/v2/account")
        return self.account

    def get_all_positions(self):
        self._session.request("GET", PAPER_ENDPOINT + "/v2/positions")
        return self.positions

    def get_orders(self, _request):
        self._session.request("GET", PAPER_ENDPOINT + "/v2/orders")
        return self.orders


def _account_model() -> TradeAccount:
    return TradeAccount.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "account_number": "PA123",
            "status": "ACTIVE",
            "equity": "100000",
            "buying_power": "200000",
            "options_buying_power": "100000",
            "trading_blocked": False,
            "transfers_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
            "options_approved_level": 3,
            "options_trading_level": 3,
        }
    )


def _position_model(*, asset_id: str, symbol: str) -> Position:
    return Position.model_validate(
        {
            "asset_id": asset_id,
            "symbol": symbol,
            "exchange": "ARCA",
            "asset_class": "us_equity",
            "avg_entry_price": "1",
            "qty": "1",
            "side": "long",
            "cost_basis": "1",
        }
    )


def _order_model(*, order_id: str, client_order_id: str) -> Order:
    return Order.model_validate(
        {
            "id": order_id,
            "client_order_id": client_order_id,
            "created_at": NOW,
            "updated_at": NOW,
            "submitted_at": NOW,
            "order_class": "simple",
            "time_in_force": "day",
            "status": "new",
            "extended_hours": False,
        }
    )


def _set_account_field(client: FakeTradingClient, field: str, value: object) -> None:
    object.__setattr__(client.account, field, value)


@dataclass
class FakeHttp:
    pages: list[HttpResponse] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.pages:
            self.pages = [HttpResponse(200, [], None, None)]

    def request(self, **kwargs: object) -> HttpResponse:
        self.calls.append(kwargs)
        request = SimpleNamespace(method=kwargs["method"], url=kwargs["url"])
        return self.send(request, follow_redirects=kwargs.get("follow_redirects"))

    def send(self, _request: object, **_kwargs: object) -> HttpResponse:
        return self.pages.pop(0)


class FakeSession:
    def request(self, method: str, url: str, **kwargs: object) -> object:
        request = SimpleNamespace(method=method, url=url)
        return self.send(request, allow_redirects=kwargs.get("allow_redirects", False))

    def send(self, _request: object, **_kwargs: object) -> object:
        return SimpleNamespace()


def _trading(
    client: FakeTradingClient | None = None,
    http: FakeHttp | None = None,
    *,
    role: AccountRole = AccountRole.DEVELOPMENT,
):
    ledger = TransportLedger()
    return TradingBoundary(
        client_factory=lambda _credentials: client or FakeTradingClient(),
        activity_http=ActivityHttpBoundary(http or FakeHttp(), ledger),
        endpoint=PAPER_ENDPOINT,
        ledger=ledger,
        role=role,
    )


def test_operator_entrypoints_expose_no_mode_role_or_resource_selection() -> None:
    assert list(inspect.signature(run_development_operator).parameters) == []
    assert list(inspect.signature(run_submission_no_trade_operator).parameters) == []


@pytest.mark.parametrize("bad", ["nan", "Infinity", "one", None])
def test_trading_rejects_nonnumeric_material_values(bad: object) -> None:
    client = FakeTradingClient()
    _set_account_field(client, "equity", bad)
    with pytest.raises(RehearsalError, match="PROVIDER_NUMBER_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_trading_rejects_custom_provider_records_sequences_and_scalar_subclasses() -> None:
    class DictSubclass(dict):
        pass

    class ListSubclass(list):
        pass

    class StringSubclass(str):
        pass

    class IntegerSubclass(int):
        pass

    client = FakeTradingClient()
    client.account = DictSubclass(client.account.model_dump(mode="json"))
    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))

    client = FakeTradingClient()
    client.positions = ListSubclass()
    with pytest.raises(RehearsalError, match="PROVIDER_COLLECTION_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))

    client = FakeTradingClient()
    row = client.account.model_dump(mode="json")
    row["status"] = StringSubclass("ACTIVE")
    with pytest.raises(RehearsalError, match="PROVIDER_STRING_INVALID"):
        rehearsal._normalize_account(row)

    client = FakeTradingClient()
    row = client.account.model_dump(mode="json")
    row["options_approved_level"] = IntegerSubclass(3)
    with pytest.raises(RehearsalError, match="PROVIDER_NUMBER_INVALID"):
        rehearsal._normalize_account(row)


def test_trading_rejects_custom_model_dump_impostor() -> None:
    class Impostor:
        def model_dump(self, **_kwargs: object) -> dict[str, object]:
            return FakeTradingClient().account

    client = FakeTradingClient()
    client.account = Impostor()
    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))

    class CustomProviderModel(BaseModel):
        id: str

    client = FakeTradingClient()
    client.account = CustomProviderModel(id="00000000-0000-0000-0000-000000000001")
    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_trading_rejects_caller_model_with_alpaca_module_label() -> None:
    class CallerDefinedProviderModel(BaseModel):
        def model_dump(self, **_kwargs: object) -> dict[str, object]:
            return FakeTradingClient().account

    CallerDefinedProviderModel.__module__ = "alpaca.trading.models"
    client = FakeTradingClient()
    client.account = CallerDefinedProviderModel()

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_trading_accepts_exact_official_record_classes() -> None:
    client = FakeTradingClient()
    client.positions = [
        Position.model_validate(
            {
                "asset_id": "00000000-0000-0000-0000-000000000002",
                "symbol": "SPY",
                "exchange": "ARCA",
                "asset_class": "us_equity",
                "avg_entry_price": "1",
                "qty": "1",
                "side": "long",
                "cost_basis": "1",
            }
        )
    ]
    client.orders = [
        Order.model_validate(
            {
                "id": "00000000-0000-0000-0000-000000000003",
                "client_order_id": "official-order",
                "created_at": NOW,
                "updated_at": NOW,
                "submitted_at": NOW,
                "order_class": "simple",
                "time_in_force": "day",
                "status": "new",
                "extended_hours": False,
            }
        )
    ]

    capture = _trading(client).capture(Credentials("key", "secret"))

    assert capture.positions[0]["asset_id"] == "00000000-0000-0000-0000-000000000002"
    assert capture.orders[0]["id"] == "00000000-0000-0000-0000-000000000003"


def test_trading_rejects_official_record_subclass_and_unrelated_model() -> None:
    class TradeAccountSubclass(TradeAccount):
        pass

    client = FakeTradingClient()
    client.account = TradeAccountSubclass.model_validate(client.account.model_dump(mode="json"))
    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))

    client = FakeTradingClient()
    client.account = Order.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000003",
            "client_order_id": "wrong-record-kind",
            "created_at": NOW,
            "updated_at": NOW,
            "submitted_at": NOW,
            "order_class": "simple",
            "time_in_force": "day",
            "status": "new",
            "extended_hours": False,
        }
    )
    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_builtin_dicts_relabeling_provider_records() -> None:
    position = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
    order = _order_model(order_id="00000000-0000-0000-0000-000000000003", client_order_id="root")
    cases = (
        ("account", _account_model().model_dump(mode="json")),
        ("position", position.model_dump(mode="json")),
        ("order", order.model_dump(mode="json")),
    )
    for record_kind, record in cases:
        client = FakeTradingClient()
        if record_kind == "account":
            client.account = record
        elif record_kind == "position":
            client.positions = [record]
        else:
            client.orders = [record]

        with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
            _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_wrong_official_record_kinds() -> None:
    account = _account_model()
    position = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
    order = _order_model(
        order_id="00000000-0000-0000-0000-000000000003", client_order_id="wrong-kind"
    )
    cases = (("account", order), ("position", order), ("order", position))
    for record_kind, record in cases:
        client = FakeTradingClient()
        client.account = account
        if record_kind == "account":
            client.account = record
        elif record_kind == "position":
            client.positions = [record]
        else:
            client.orders = [record]

        with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
            _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_root_and_position_subclasses() -> None:
    class PositionSubclass(Position):
        pass

    class OrderSubclass(Order):
        pass

    position = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
    order = _order_model(
        order_id="00000000-0000-0000-0000-000000000003", client_order_id="subclass"
    )
    cases = (
        ("position", PositionSubclass.model_validate(position.model_dump(mode="json"))),
        ("order", OrderSubclass.model_validate(order.model_dump(mode="json"))),
    )
    for record_kind, record in cases:
        client = FakeTradingClient()
        if record_kind == "position":
            client.positions = [record]
        else:
            client.orders = [record]

        with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
            _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_dict_and_wrong_kind_nested_legs() -> None:
    root = _order_model(order_id="00000000-0000-0000-0000-000000000003", client_order_id="root")
    leg = _order_model(order_id="00000000-0000-0000-0000-000000000004", client_order_id="leg")
    position = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
    for nested in (leg.model_dump(mode="json"), position):
        object.__setattr__(root, "legs", [nested])
        client = FakeTradingClient()
        client.orders = [root]

        with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
            _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_instance_overridden_sdk_serialization() -> None:
    root = _order_model(order_id="00000000-0000-0000-0000-000000000003", client_order_id="root")
    forged = root.model_dump(mode="json")
    forged["legs"] = [
        _order_model(
            order_id="00000000-0000-0000-0000-000000000004", client_order_id="forged-leg"
        ).model_dump(mode="json")
    ]
    object.__setattr__(root, "model_dump", lambda **_kwargs: forged)
    client = FakeTradingClient()
    client.orders = [root]

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_serializer_shadowing_on_every_sdk_record() -> None:
    account = _account_model()
    position = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
    root = _order_model(order_id="00000000-0000-0000-0000-000000000003", client_order_id="root")
    leg = _order_model(order_id="00000000-0000-0000-0000-000000000004", client_order_id="leg")
    object.__setattr__(root, "legs", [leg])
    for record_kind, record in (
        ("account", account),
        ("position", position),
        ("order", root),
        ("nested", leg),
    ):
        object.__setattr__(record, "model_dump", lambda **_kwargs: {})
        client = FakeTradingClient()
        if record_kind == "account":
            client.account = record
        elif record_kind == "position":
            client.positions = [record]
        else:
            client.orders = [root]

        with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
            _trading(client).capture(Credentials("key", "secret"))
        object.__delattr__(record, "model_dump")


def test_production_capture_rejects_injected_private_sdk_state() -> None:
    nested_root = _order_model(
        order_id="00000000-0000-0000-0000-000000000003", client_order_id="root"
    )
    nested_leg = _order_model(
        order_id="00000000-0000-0000-0000-000000000004", client_order_id="leg"
    )
    object.__setattr__(nested_root, "legs", [nested_leg])
    for record_kind, record in (
        ("account", _account_model()),
        (
            "position",
            _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY"),
        ),
        (
            "order",
            _order_model(
                order_id="00000000-0000-0000-0000-000000000003",
                client_order_id="root",
            ),
        ),
        ("nested", nested_leg),
    ):
        object.__setattr__(record, "_forged_provider_state", {"id": "forged"})
        client = FakeTradingClient()
        if record_kind == "account":
            client.account = record
        elif record_kind == "position":
            client.positions = [record]
        elif record_kind == "order":
            client.orders = [record]
        else:
            client.orders = [nested_root]

        with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
            _trading(client).capture(Credentials("key", "secret"))


@pytest.mark.parametrize("record_kind", ["account", "position", "root", "nested"])
def test_production_capture_revalidates_serialized_sdk_identity(
    monkeypatch: pytest.MonkeyPatch, record_kind: str
) -> None:
    client = FakeTradingClient()
    if record_kind == "account":
        target = _account_model()
        client.account = target
        identity_field = "id"
    elif record_kind == "position":
        target = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
        client.positions = [target]
        identity_field = "asset_id"
    else:
        root = _order_model(order_id="00000000-0000-0000-0000-000000000003", client_order_id="root")
        leg = _order_model(order_id="00000000-0000-0000-0000-000000000004", client_order_id="leg")
        object.__setattr__(root, "legs", [leg])
        target = root if record_kind == "root" else leg
        client.orders = [root]
        identity_field = "id"
    official_type = type(target)
    original_dump = official_type.model_dump

    def forged_dump(value: object, **kwargs: object) -> dict[str, object]:
        result = original_dump(value, **kwargs)
        if value is target:
            result[identity_field] = "00000000-0000-0000-0000-000000000099"
        elif record_kind == "nested" and value is root:
            result["legs"][0]["id"] = "00000000-0000-0000-0000-000000000099"
        return result

    monkeypatch.setattr(official_type, "model_dump", forged_dump)

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_class_serializer_material_field_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeTradingClient()
    original_dump = TradeAccount.model_dump

    def forged_dump(value: object, **kwargs: object) -> dict[str, object]:
        result = original_dump(value, **kwargs)
        result["equity"] = "999999"
        return result

    monkeypatch.setattr(TradeAccount, "model_dump", forged_dump)

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


@pytest.mark.parametrize("record_kind", ["account", "position", "root", "nested"])
def test_production_capture_rejects_material_substitution_for_every_sdk_record(
    monkeypatch: pytest.MonkeyPatch, record_kind: str
) -> None:
    client = FakeTradingClient()
    if record_kind == "account":
        official_type = TradeAccount
        material_field = "equity"
        replacement = "999999"
    elif record_kind == "position":
        position = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
        client.positions = [position]
        official_type = Position
        material_field = "symbol"
        replacement = "QQQ"
    else:
        root = _order_model(order_id="00000000-0000-0000-0000-000000000003", client_order_id="root")
        leg = _order_model(order_id="00000000-0000-0000-0000-000000000004", client_order_id="leg")
        object.__setattr__(root, "legs", [leg])
        client.orders = [root]
        official_type = Order
        material_field = "status"
        replacement = "filled"
    original_dump = official_type.model_dump

    def forged_dump(value: object, **kwargs: object) -> dict[str, object]:
        result = original_dump(value, **kwargs)
        if record_kind == "nested":
            result["legs"][0][material_field] = replacement
        else:
            result[material_field] = replacement
        return result

    monkeypatch.setattr(official_type, "model_dump", forged_dump)

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


@pytest.mark.parametrize("authority", ["registry", "module", "sdk_module", "fields", "serializer"])
def test_production_capture_rejects_mutated_sdk_authority(
    monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    client = FakeTradingClient()
    if authority == "registry":
        monkeypatch.setattr(
            rehearsal,
            "OFFICIAL_PROVIDER_RECORD_TYPES",
            {"account": TradeAccount, "position": Position, "order": Order},
        )
    elif authority == "module":
        monkeypatch.setattr(rehearsal, "TradeAccount", Position)
    elif authority == "sdk_module":
        monkeypatch.setattr(sys.modules[TradeAccount.__module__], "TradeAccount", Position)
    elif authority == "fields":
        monkeypatch.setattr(TradeAccount, "model_fields", dict(TradeAccount.model_fields))
    else:
        original_serializer = TradeAccount.__pydantic_serializer__

        class ForgedSerializer:
            def to_python(self, value: object, **kwargs: object) -> dict[str, object]:
                result = original_serializer.to_python(value, **kwargs)
                result["equity"] = "999999"
                return result

        monkeypatch.setattr(TradeAccount, "__pydantic_serializer__", ForgedSerializer())

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_seals_malformed_sdk_instance_error() -> None:
    client = FakeTradingClient()
    client.account.__dict__.pop("equity")
    object.__delattr__(client.account, "__pydantic_fields_set__")

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_mutated_sdk_field_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeTradingClient()
    object.__setattr__(client.account, "created_at", NOW.isoformat())
    monkeypatch.setattr(TradeAccount.model_fields["created_at"], "annotation", str | None)

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_mutated_nested_sdk_field_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeTradingClient()
    position = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
    usd = USDPositionValues.model_validate(
        {
            "avg_entry_price": "1",
            "market_value": "1",
            "cost_basis": "1",
            "unrealized_pl": "0",
            "unrealized_plpc": "0",
            "unrealized_intraday_pl": "0",
            "unrealized_intraday_plpc": "0",
            "current_price": "1",
            "lastday_price": "1",
            "change_today": "0",
        }
    )
    object.__setattr__(usd, "avg_entry_price", 1)
    object.__setattr__(position, "usd", usd)
    client.positions = [position]
    monkeypatch.setattr(USDPositionValues.model_fields["avg_entry_price"], "annotation", int)

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_mutated_sdk_enum_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeTradingClient()
    order = _order_model(
        order_id="00000000-0000-0000-0000-000000000003",
        client_order_id="root",
    )
    client.orders = [order]
    monkeypatch.setattr(order.status, "_value_", "filled")

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_equal_subclassed_sdk_enum_member_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EqualString(str):
        pass

    client = FakeTradingClient()
    position = _position_model(
        asset_id="00000000-0000-0000-0000-000000000002",
        symbol="SPY",
    )
    client.positions = [position]
    member = position.asset_class
    monkeypatch.setattr(member, "_name_", EqualString(member.name))

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_mutated_reachable_optional_enum_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeTradingClient()
    client.orders = [
        _order_model(
            order_id="00000000-0000-0000-0000-000000000003",
            client_order_id="root",
        )
    ]
    monkeypatch.setattr(OrderType.MARKET, "_name_", "FORGED")

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


@pytest.mark.parametrize(
    "authority",
    ["module", "member-name", "member-object", "value-registry"],
)
def test_production_capture_rejects_mutated_sdk_enum_authority(
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
) -> None:
    client = FakeTradingClient()
    position = _position_model(
        asset_id="00000000-0000-0000-0000-000000000002",
        symbol="SPY",
    )
    client.positions = [position]
    member = position.asset_class
    enum_type = type(member)
    if authority == "module":
        monkeypatch.setattr(
            sys.modules[enum_type.__module__],
            enum_type.__name__,
            type(client.account.status),
        )
    elif authority == "member-name":
        monkeypatch.setattr(member, "_name_", "FORGED")
    elif authority == "member-object":
        replacement = next(candidate for candidate in enum_type if candidate is not member)
        monkeypatch.setitem(vars(enum_type)["_member_map_"], member.name, replacement)
    else:
        replacement = next(candidate for candidate in enum_type if candidate is not member)
        monkeypatch.setitem(vars(enum_type)["_value2member_map_"], member.value, replacement)

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


@pytest.mark.parametrize("record_kind", ["account", "position", "root", "nested"])
def test_production_capture_rejects_sdk_field_value_family_substitution(
    record_kind: str,
) -> None:
    client = FakeTradingClient()
    if record_kind == "account":
        target = client.account
        field_name = "created_at"
    elif record_kind == "position":
        target = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
        client.positions = [target]
        field_name = "expiration_date"
    else:
        root = _order_model(order_id="00000000-0000-0000-0000-000000000003", client_order_id="root")
        leg = _order_model(order_id="00000000-0000-0000-0000-000000000004", client_order_id="leg")
        object.__setattr__(root, "legs", [leg])
        client.orders = [root]
        target = root if record_kind == "root" else leg
        field_name = "submitted_at"
    object.__setattr__(target, field_name, NOW.isoformat())

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_accepts_exact_nested_order_graph() -> None:
    root = _order_model(order_id="00000000-0000-0000-0000-000000000003", client_order_id="root")
    leg = _order_model(order_id="00000000-0000-0000-0000-000000000004", client_order_id="leg")
    object.__setattr__(root, "legs", [leg])
    client = FakeTradingClient()
    client.orders = [root]

    capture = _trading(client).capture(Credentials("key", "secret"))

    assert capture.orders[0]["id"] == str(root.id)
    assert [item["id"] for item in capture.orders[0]["legs"]] == [str(leg.id)]


def test_trading_rejects_official_order_with_subclassed_nested_leg() -> None:
    class OrderSubclass(Order):
        pass

    root = Order.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000003",
            "client_order_id": "official-root",
            "created_at": NOW,
            "updated_at": NOW,
            "submitted_at": NOW,
            "order_class": "mleg",
            "time_in_force": "day",
            "status": "new",
            "extended_hours": False,
        }
    )
    leg = OrderSubclass.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000004",
            "client_order_id": "subclassed-leg",
            "created_at": NOW,
            "updated_at": NOW,
            "submitted_at": NOW,
            "order_class": "simple",
            "time_in_force": "day",
            "status": "new",
            "extended_hours": False,
        }
    )
    object.__setattr__(root, "legs", [leg])
    client = FakeTradingClient()
    client.orders = [root]

    with pytest.raises(RehearsalError, match="PROVIDER_RECORD_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_accepts_exact_annotated_position_value_model() -> None:
    client = FakeTradingClient()
    position = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
    object.__setattr__(
        position,
        "usd",
        USDPositionValues.model_validate(
            {
                "avg_entry_price": "1",
                "market_value": "1",
                "cost_basis": "1",
                "unrealized_pl": "0",
                "unrealized_plpc": "0",
                "unrealized_intraday_pl": "0",
                "unrealized_intraday_plpc": "0",
                "current_price": "1",
                "lastday_price": "1",
                "change_today": "0",
            }
        ),
    )
    client.positions = [position]

    _trading(client).capture(Credentials("key", "secret"))


def test_production_capture_rejects_oversized_string_in_exact_annotated_nested_model() -> None:
    client = FakeTradingClient()
    position = _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="SPY")
    usd = USDPositionValues.model_validate(
        {
            "avg_entry_price": "1",
            "market_value": "1",
            "cost_basis": "1",
            "unrealized_pl": "0",
            "unrealized_plpc": "0",
            "unrealized_intraday_pl": "0",
            "unrealized_intraday_plpc": "0",
            "current_price": "1",
            "lastday_price": "1",
            "change_today": "0",
        }
    )
    object.__setattr__(usd, "avg_entry_price", "x" * (MAX_PROVIDER_STRING + 1))
    object.__setattr__(position, "usd", usd)
    client.positions = [position]

    with pytest.raises(RehearsalError, match="PROVIDER_STRING_TOO_LONG"):
        _trading(client).capture(Credentials("key", "secret"))


def test_trading_owns_trace_and_rejects_redirects_and_incomplete_pagination() -> None:
    redirect = FakeHttp([HttpResponse(302, [], PAPER_ENDPOINT + "/other", None)])
    with pytest.raises(RehearsalError, match="PROVIDER_REDIRECT_FORBIDDEN"):
        _trading(http=redirect).capture(Credentials("key", "secret"))
    full_page = [{"id": f"a-{index}", "activity_type": "FILL"} for index in range(100)]
    incomplete = FakeHttp([HttpResponse(200, full_page, None, None)] * 100)
    with pytest.raises(RehearsalError, match="ACTIVITY_PAGINATION_INCOMPLETE"):
        _trading(http=incomplete).capture(Credentials("key", "secret"))


def test_activity_boundary_rejects_custom_json_sequence_and_status_subclasses() -> None:
    class ListSubclass(list):
        pass

    class IntegerSubclass(int):
        pass

    custom_payload = FakeHttp([HttpResponse(200, ListSubclass(), None, None)])
    with pytest.raises(RehearsalError, match="ACTIVITY_RESPONSE_INVALID"):
        _trading(http=custom_payload).capture(Credentials("key", "secret"))

    custom_status = FakeHttp([HttpResponse(IntegerSubclass(200), [], None, None)])
    with pytest.raises(RehearsalError, match="ACTIVITY_RESPONSE_INVALID"):
        _trading(http=custom_status).capture(Credentials("key", "secret"))


def test_trading_blocks_hidden_sdk_write_from_a_read_method() -> None:
    class HiddenWriteClient(FakeTradingClient):
        def get_account(self):
            self._session.request("POST", PAPER_ENDPOINT + "/v2/orders")
            return self.account

    with pytest.raises(RehearsalError, match="MUTATING_HTTP_METHOD_FORBIDDEN"):
        _trading(HiddenWriteClient()).capture(Credentials("key", "secret"))


def test_transport_ledger_blocks_hidden_raw_sdk_post() -> None:
    ledger = TransportLedger()

    class Session:
        def request(self, method, url, **kwargs):
            return self.send(SimpleNamespace(method=method, url=url), **kwargs)

        def send(self, _request, **_kwargs):
            raise AssertionError("transport send must not occur")

    session = Session()
    ledger.instrument_requests_session(session)
    with pytest.raises(RehearsalError, match="MUTATING_HTTP_METHOD_FORBIDDEN"):
        session.request("POST", PAPER_ENDPOINT + "/v2/orders")
    assert ledger.rejected_writes == 1

    with pytest.raises(RehearsalError, match="MUTATING_HTTP_METHOD_FORBIDDEN"):
        session.request("HEAD", PAPER_ENDPOINT + "/v2/account")
    assert ledger.rejected_writes == 2


def test_transport_ledger_rejects_unrelated_reads_on_an_allowed_host() -> None:
    class HiddenReadClient(FakeTradingClient):
        def get_account(self):
            self._session.request("GET", PAPER_ENDPOINT + "/v2/account/configurations")
            return self.account

    with pytest.raises(RehearsalError, match="PROVIDER_ENDPOINT_INVALID"):
        _trading(HiddenReadClient()).capture(Credentials("key", "secret"))


def test_transport_ledger_rejects_hidden_sdk_redirect_response() -> None:
    ledger = TransportLedger()

    class Session:
        def send(self, _request, **_kwargs):
            return SimpleNamespace(status_code=302, history=())

    session = Session()
    ledger.instrument_requests_session(session)
    request = SimpleNamespace(method="GET", url=PAPER_ENDPOINT + "/v2/account")
    with pytest.raises(RehearsalError, match="PROVIDER_REDIRECT_FORBIDDEN"):
        session.send(request)


def test_transport_ledger_forces_sdk_redirect_flags_off_before_send() -> None:
    ledger = TransportLedger()
    observed: list[dict[str, object]] = []

    class Session:
        def send(self, _request, **kwargs):
            observed.append(kwargs)
            return SimpleNamespace(status_code=200, history=())

    session = Session()
    ledger.instrument_requests_session(session)
    request = SimpleNamespace(method="GET", url=PAPER_ENDPOINT + "/v2/account")
    session.send(request, allow_redirects=True, follow_redirects=True)
    assert observed == [{"allow_redirects": False, "follow_redirects": False}]


def test_trading_detects_duplicates() -> None:
    client = FakeTradingClient()
    client.positions = [
        _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="A"),
        _position_model(asset_id="00000000-0000-0000-0000-000000000002", symbol="B"),
    ]
    with pytest.raises(RehearsalError, match="PROVIDER_DUPLICATE_ITEM"):
        _trading(client).capture(Credentials("key", "secret"))


def test_trading_rejects_deep_and_branching_order_leg_trees() -> None:
    nested = _order_model(order_id="00000000-0000-0000-0000-000000000005", client_order_id="nested")
    deep_leg = _order_model(
        order_id="00000000-0000-0000-0000-000000000004", client_order_id="deep-leg"
    )
    object.__setattr__(deep_leg, "legs", [nested])
    deep = _order_model(
        order_id="00000000-0000-0000-0000-000000000003", client_order_id="deep-root"
    )
    object.__setattr__(deep, "legs", [deep_leg])

    branching = _order_model(
        order_id="00000000-0000-0000-0000-000000000006", client_order_id="branch-root"
    )
    branch_leg = _order_model(
        order_id="00000000-0000-0000-0000-000000000007", client_order_id="branch-leg"
    )
    object.__setattr__(branch_leg, "legs", [nested])
    object.__setattr__(branching, "legs", [branch_leg])
    for order in (deep, branching):
        client = FakeTradingClient()
        client.orders = [order]
        with pytest.raises(RehearsalError, match="PROVIDER_ORDER_LEG_TREE_INVALID"):
            _trading(client).capture(Credentials("key", "secret"))


def test_trading_rejects_duplicate_leg_identity_across_root_orders() -> None:
    client = FakeTradingClient()
    shared_leg = _order_model(
        order_id="00000000-0000-0000-0000-000000000004", client_order_id="shared-leg"
    )
    root_a = _order_model(order_id="00000000-0000-0000-0000-000000000005", client_order_id="root-a")
    root_b = _order_model(order_id="00000000-0000-0000-0000-000000000006", client_order_id="root-b")
    object.__setattr__(root_a, "legs", [shared_leg])
    object.__setattr__(root_b, "legs", [shared_leg])
    client.orders = [root_a, root_b]
    with pytest.raises(RehearsalError, match="PROVIDER_DUPLICATE_ITEM"):
        _trading(client).capture(Credentials("key", "secret"))


def test_cli_executes_exact_verified_archive_member_and_rechecks_fd(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)
    process = FakeProcess()
    cli = VerifiedCli.from_archive(archive, pin, process)
    evidence = cli.probe(Credentials("key", "secret"))
    assert evidence["paper_host_verified"] is True
    assert len({descriptor for descriptor, _ in process.calls}) == 1
    assert all(argv[0].startswith("/proc/self/fd/") for _, argv in process.calls)
    archive.write_bytes(b"changed")
    assert len(process.calls) == 3
    cli.close()


def test_cli_retained_executable_fd_is_read_only(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)

    class TamperingProcess(FakeProcess):
        def run_fd(self, descriptor, argv, environment):
            with pytest.raises(OSError):
                os.write(descriptor, b"changed")
            return super().run_fd(descriptor, argv, environment)

    cli = VerifiedCli.from_archive(archive, pin, TamperingProcess())
    assert cli.probe(Credentials("key", "secret"))["binary_sha256"] == pin.binary_sha256
    cli.close()


def test_cli_rejects_archive_whose_member_is_not_the_pinned_binary(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)
    wrong_pin = CliPin(pin.version, pin.archive_sha256, "0" * 64, pin.archive_member)
    with pytest.raises(RehearsalError, match="CLI_BINARY_DIGEST_MISMATCH"):
        VerifiedCli.from_archive(archive, wrong_pin, FakeProcess())


@pytest.mark.parametrize(
    "doctor",
    [
        {"connectivity": "failed", "authentication": "ok", "effective_api_url": PAPER_ENDPOINT},
        {"connectivity": "ok", "authentication": "failed", "effective_api_url": PAPER_ENDPOINT},
        {
            "connectivity": "ok",
            "authentication": "ok",
            "effective_api_url": "https://api.alpaca.markets/",
        },
    ],
)
def test_cli_doctor_must_prove_auth_connectivity_and_exact_paper_host(
    tmp_path: Path, doctor: dict[str, object]
) -> None:
    archive, pin = _archive(tmp_path)
    process = FakeProcess(outputs=["0.0.13\n", json.dumps(doctor)])
    cli = VerifiedCli.from_archive(archive, pin, process)
    with pytest.raises(RehearsalError, match="CLI_PAPER_HOST_NOT_VERIFIED"):
        cli.probe(Credentials("key", "secret"))
    assert cli.closed is True
    cli.close()


@pytest.mark.parametrize(
    "marker",
    ["SKIPPED", "WARN", "UNKNOWN", "MISSING", "NOT OK", "DISABLED", "UNAVAILABLE"],
)
def test_plain_doctor_rejects_nonaffirmative_checks(tmp_path: Path, marker: str) -> None:
    archive, pin = _archive(tmp_path)
    doctor = "\n".join(
        [
            "Alpaca CLI doctor PASS",
            "Configuration PASS",
            "Credentials OK",
            "Connectivity OK",
            f"Paper trading endpoint {PAPER_ENDPOINT}",
            f"Optional check {marker}",
        ]
    )
    cli = VerifiedCli.from_archive(archive, pin, FakeProcess(outputs=["0.0.13\n", doctor]))
    with pytest.raises(RehearsalError, match="CLI_PAPER_HOST_NOT_VERIFIED"):
        cli.probe(Credentials("key", "secret"))


def test_cli_accepts_bounded_real_plain_text_doctor_without_retaining_it(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)
    process = FakeProcess()
    process.outputs[1] = _official_doctor_output()
    cli = VerifiedCli.from_archive(archive, pin, process)
    assert cli.probe(Credentials("key", "secret"))["paper_host_verified"] is True
    cli.close()


def test_cli_accepts_official_update_available_message(tmp_path: Path) -> None:
    doctor = _official_doctor_output().replace(
        "  ✓ up to date (0.0.13)",
        "  - update available: 0.0.13 -> v0.0.14, run "
        "`go install github.com/alpacahq/cli/cmd/alpaca@latest`",
    )
    archive, pin = _archive(tmp_path)
    process = FakeProcess()
    process.outputs[1] = doctor
    cli = VerifiedCli.from_archive(
        archive,
        pin,
        process,
    )
    assert cli.probe(Credentials("key", "secret"))["paper_host_verified"] is True


@pytest.mark.parametrize(
    "doctor",
    [
        _official_doctor_output().replace(PAPER_ENDPOINT, "https://api.alpaca.markets"),
        _official_doctor_output().replace("✓ trading API: connected", "✗ trading API: denied"),
    ],
)
def test_official_doctor_requires_connected_exact_paper_host(tmp_path: Path, doctor: str) -> None:
    archive, pin = _archive(tmp_path)
    cli = VerifiedCli.from_archive(
        archive,
        pin,
        FakeProcess(outputs=["0.0.13\n", doctor]),
    )
    with pytest.raises(RehearsalError, match="CLI_PAPER_HOST_NOT_VERIFIED"):
        cli.probe(Credentials("key", "secret"))


@pytest.mark.parametrize(
    "contradiction",
    [
        "Trading:  https://api.alpaca.markets",
        f"Trading:  {PAPER_ENDPOINT}",
        "Data:     https://alternate-data.example",
        "Data:     https://data.alpaca.markets",
        "✓ API key credentials from env (ALPACA_API_KEY + ALPACA_SECRET_KEY)",
        '✓ API key credentials from profile "live"',
        '✓ OAuth token from profile "paper"',
        "✗ trading API: disconnected",
        "✓ trading API: connected elsewhere",
    ],
)
def test_official_doctor_rejects_conflicting_effective_authority_lines(
    tmp_path: Path, contradiction: str
) -> None:
    archive, pin = _archive(tmp_path)
    doctor = _official_doctor_output().replace("Connectivity:", f"{contradiction}\nConnectivity:")
    process = FakeProcess()
    process.outputs[1] = doctor
    cli = VerifiedCli.from_archive(archive, pin, process)
    with pytest.raises(RehearsalError, match="CLI_PAPER_HOST_NOT_VERIFIED"):
        cli.probe(Credentials("key", "secret"))


@pytest.mark.parametrize(
    "contradiction",
    [
        "✓ active profile: live",
        "Profile authority: paper",
        "Live Trading: enabled",
        "oauth TOKEN FROM cached configuration",
        "OAuth enabled",
        "Effective API URL: https://api.alpaca.markets",
        "Effective API URL: HTTPS://API.ALPACA.MARKETS",
        "Effective API host: api.alpaca.markets",
        "Connectivity warning: ignored",
        "Trading API failure: none",
    ],
)
def test_official_doctor_rejects_extra_profile_live_url_and_failure_authority(
    tmp_path: Path, contradiction: str
) -> None:
    archive, pin = _archive(tmp_path)
    doctor = _official_doctor_output().replace("Connectivity:", f"{contradiction}\nConnectivity:")
    cli = VerifiedCli.from_archive(
        archive,
        pin,
        FakeProcess(outputs=["0.0.13\n", doctor]),
    )
    with pytest.raises(RehearsalError, match="CLI_PAPER_HOST_NOT_VERIFIED"):
        cli.probe(Credentials("key", "secret"))


def test_cli_rejects_unknown_advanced_instructions(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)
    process = FakeProcess()
    payload = json.loads(process.outputs[2])
    payload["advanced_instructions"] = {"submit": True}
    process.outputs[2] = json.dumps(payload)
    cli = VerifiedCli.from_archive(archive, pin, process)
    with pytest.raises(RehearsalError, match="CLI_DRY_RUN_INVALID"):
        cli.probe(Credentials("key", "secret"))
    cli.close()


def test_cli_rejects_duplicate_dry_run_json_keys_even_when_the_last_value_is_safe(
    tmp_path: Path,
) -> None:
    archive, pin = _archive(tmp_path)
    safe = json.dumps(json.loads(FakeProcess().outputs[2]))
    duplicate = safe.replace('"qty": "1"', '"qty": "999", "qty": "1"', 1)
    cli = VerifiedCli.from_archive(
        archive,
        pin,
        FakeProcess(outputs=["0.0.13\n", _official_doctor_output(), duplicate]),
    )
    with pytest.raises(RehearsalError, match="CLI_DRY_RUN_INVALID"):
        cli.probe(Credentials("key", "secret"))


class FakeMCPClient:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *_args):
        self.exited += 1

    async def call(self, name: str, arguments: dict[str, object]):
        self.calls.append((name, arguments))
        return SimpleNamespace(audit=SimpleNamespace(result_summary_hash="a" * 64))


def test_mcp_boundary_owns_exact_call_trace() -> None:
    client = FakeMCPClient()
    evidence = asyncio.run(MCPBoundary(lambda: client, now=lambda: NOW).probe())
    assert evidence.call_trace == (("get_clock", {}),)
    assert evidence.tool_surface_count == 22
    assert client.calls == [("get_clock", {})]
    assert client.entered == client.exited == 1


def test_mcp_boundary_reuses_retained_client_without_reentering_it() -> None:
    client = FakeMCPClient()

    async def probe_retained_client():
        await client.__aenter__()
        evidence = await MCPBoundary.from_retained_client(
            client,
            now=lambda: NOW,
        ).probe()
        assert client.entered == 1
        assert client.exited == 0
        await client.__aexit__(None, None, None)
        return evidence

    evidence = asyncio.run(probe_retained_client())
    assert evidence.call_trace == (("get_clock", {}),)
    assert client.calls == [("get_clock", {})]
    assert client.entered == client.exited == 1


def test_counted_acquisition_forwards_actor_keyword() -> None:
    calls: list[Actor] = []

    class ActorAwareAcquisition:
        async def acquire(self, *_args: object, actor: Actor) -> str:
            calls.append(actor)
            return "acquired"

    counts = {"acquisition": 0}
    acquisition = rehearsal._CountedAcquisition(ActorAwareAcquisition(), counts)
    result = asyncio.run(acquisition.acquire(object(), actor=Actor.SCHEDULER))
    assert result == "acquired"
    assert calls == [Actor.SCHEDULER]
    assert counts == {"acquisition": 1}


@dataclass
class Authority:
    value: ObservedPaperAccountAuthority

    def observe(self):
        return self.value


class Clock:
    def now(self):
        return NOW


@dataclass
class Calibration:
    value: CalibrationBinding

    def binding_for(self, _authority):
        return self.value


class Decisions:
    def __init__(self, authority):
        self.tick = AgentTick(uuid4(), uuid4(), authority, Actor.SCHEDULER, NOW)

    def begin_tick(self, *_args):
        return self.tick

    def permanent_latch(self, _authority):
        return PermanentAccountLatch(False)

    def persist_decision(self, _tick, decision, _proposal):
        return PersistedAgentDecision(decision, None)

    def complete_tick(self, tick, terminal_code, _certificate):
        return AgentRunResult(tick.tick_id, terminal_code, SimpleNamespace(), None, None, "f" * 64)


class Runtime:
    class Execution:
        def execute(self, *_args):
            raise AssertionError("submission execution must not run")

    execution = Execution()


def test_submission_boundary_runs_actual_service_and_owns_zero_counts() -> None:
    authority = ObservedPaperAccountAuthority(AccountRole.SUBMISSION, "a" * 64, True, True)
    binding = CalibrationBinding(
        AccountRole.SUBMISSION,
        "a" * 64,
        "CALIBRATION_BINDING_NO_TRADE",
        "b" * 64,
        "c" * 64,
        NOW,
        NOW,
    )
    boundary = FixtureSubmissionBoundary(
        account_authority=Authority(authority),
        clock=Clock(),
        calibration=Calibration(binding),
        decisions=Decisions(authority),
        runtime=Runtime(),
    )
    evidence = asyncio.run(boundary.probe())
    assert evidence.terminal_code == "CALIBRATION_BINDING_NO_TRADE"
    assert evidence.counts == {
        "acquisition": 0,
        "mcp": 0,
        "gemini": 0,
        "proposal": 0,
        "intent": 0,
        "execution": 0,
        "provider_write": 0,
        "repository": 4,
    }


def test_submission_rehearsal_binds_observed_account_to_service_authority(tmp_path: Path) -> None:
    wrong = ObservedPaperAccountAuthority(AccountRole.SUBMISSION, "a" * 64, True, False)
    binding = CalibrationBinding(
        AccountRole.SUBMISSION,
        "a" * 64,
        "CALIBRATION_BINDING_NO_TRADE",
        "b" * 64,
        "c" * 64,
        NOW,
        NOW,
    )
    submission = FixtureSubmissionBoundary(
        account_authority=Authority(wrong),
        clock=Clock(),
        calibration=Calibration(binding),
        decisions=Decisions(wrong),
        runtime=Runtime(),
    )
    http = FakeHttp([HttpResponse(200, [], None, None)])
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="ACCOUNT_AUTHORITY_FINGERPRINT_MISMATCH"):
        asyncio.run(
            run_fixture_submission_no_trade(
                Credentials("key", "secret"),
                _trading(http=http, role=AccountRole.SUBMISSION),
                submission,
                store,
                now=lambda: NOW,
            )
        )
    store.close()


def test_submission_fixture_captures_unchanged_book_before_and_after(tmp_path: Path) -> None:
    account_id = "00000000-0000-0000-0000-000000000001"
    fingerprint = hashlib.sha256(f"{account_id}\n".encode()).hexdigest()
    authority = ObservedPaperAccountAuthority(
        AccountRole.SUBMISSION,
        fingerprint,
        True,
        False,
    )
    binding = CalibrationBinding(
        AccountRole.SUBMISSION,
        fingerprint,
        "CALIBRATION_BINDING_NO_TRADE",
        "b" * 64,
        "c" * 64,
        NOW,
        NOW,
    )
    submission = FixtureSubmissionBoundary(
        account_authority=Authority(authority),
        clock=Clock(),
        calibration=Calibration(binding),
        decisions=Decisions(authority),
        runtime=Runtime(),
    )
    http = FakeHttp([HttpResponse(200, [], None, None), HttpResponse(200, [], None, None)])
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    result = asyncio.run(
        run_fixture_submission_no_trade(
            Credentials("key", "secret"),
            _trading(http=http, role=AccountRole.SUBMISSION),
            submission,
            store,
            now=lambda: NOW,
        )
    )
    assert result.summary["artifact_class"] == "FIXTURE_TEST_ONLY"
    assert result.summary["details"]["book_unchanged"] is True
    assert result.summary["details"]["provider_request_count"] == 8
    assert result.summary["competition_evidence"] is False
    result.artifact_directory.close()
    store.close()


def test_submission_fixture_rejects_development_trading_role(tmp_path: Path) -> None:
    authority = ObservedPaperAccountAuthority(AccountRole.SUBMISSION, "a" * 64, True, False)
    binding = CalibrationBinding(
        AccountRole.SUBMISSION,
        "a" * 64,
        "CALIBRATION_BINDING_NO_TRADE",
        "b" * 64,
        "c" * 64,
        NOW,
        NOW,
    )
    submission = FixtureSubmissionBoundary(
        account_authority=Authority(authority),
        clock=Clock(),
        calibration=Calibration(binding),
        decisions=Decisions(authority),
        runtime=Runtime(),
    )
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="SUBMISSION_ROLE_REQUIRED"):
        asyncio.run(
            run_fixture_submission_no_trade(
                Credentials("key", "secret"),
                _trading(),
                submission,
                store,
                now=lambda: NOW,
            )
        )
    store.close()


def test_artifact_store_uses_held_root_and_binds_public_summary(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    moved = tmp_path / "moved"
    root.rename(moved)
    root.symlink_to(tmp_path / "attacker", target_is_directory=True)
    result = store.seal_fixture(
        Mode.DEVELOPMENT,
        NOW,
        {"account_fingerprint": "private"},
        {"fixture": "DEVELOPMENT"},
        run_id="12345678-1234-5678-1234-567812345678",
    )
    public_bytes = result.read_public()
    summary = json.loads(public_bytes)
    private = json.loads((moved / result.name / "manifest.private.json").read_text())
    assert private["public_summary_sha256"] == hashlib.sha256(public_bytes).hexdigest()
    assert summary["private_evidence_sha256"]
    assert summary["run_id"] == "12345678-1234-5678-1234-567812345678"
    assert summary["captured_at"] == "2026-08-29T12:34:56Z"
    assert not (tmp_path / "attacker").exists()
    with pytest.raises(RehearsalError, match="ARTIFACT_PATH_IDENTITY_CHANGED"):
        _ = result.path
    result.close()
    store.close()


def test_artifact_second_write_failure_leaves_no_partial_sealed_run(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root, fail_after_writes=1)
    with pytest.raises(RehearsalError, match="ARTIFACT_WRITE_FAILED"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {"private": True}, {"public": True})
    assert list(root.iterdir()) == []
    store.close()


def test_artifact_verification_failure_restores_permissions_and_removes_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)

    def reject(_descriptor: int) -> tuple[bytes, dict[str, object]]:
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED")

    monkeypatch.setattr(rehearsal, "_verify_artifact_directory", reject)
    with pytest.raises(RehearsalError, match="ARTIFACT_WRITE_FAILED"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {}, {})
    assert list(root.iterdir()) == []
    store.close()


def test_artifact_store_never_replaces_existing_final_directory(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    identifier = "12345678-1234-5678-1234-567812345678"
    final = root / f"20260829T123456Z-development-{identifier}"
    final.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="ARTIFACT_DIRECTORY_CREATE_FAILED"):
        store.seal_fixture(
            Mode.DEVELOPMENT,
            NOW,
            {"private": True},
            {"public": True},
            run_id=identifier,
        )
    assert final.is_dir()
    assert list(final.iterdir()) == []
    store.close()


def test_artifact_is_self_contained_and_rejects_tampered_public_bytes(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    first = store.seal_fixture(Mode.DEVELOPMENT, NOW, {"private": True}, {"public": True})
    assert all(not path.name.startswith(".staging-") for path in root.iterdir())
    summary = first.path / "summary.public.json"
    summary.chmod(0o600)
    summary.write_text("{}\n")
    summary.chmod(0o400)
    with pytest.raises(RehearsalError, match="ARTIFACT_VERIFICATION_FAILED"):
        first.read_public()
    first.close()
    store.close()


def test_stdout_and_provider_strings_are_bounded(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)
    cli = VerifiedCli.from_archive(archive, pin, FakeProcess(outputs=["x" * 70_000]))
    with pytest.raises(RehearsalError, match="CLI_OUTPUT_TOO_LARGE"):
        cli.probe(Credentials("key", "secret"))
    cli.close()
    client = FakeTradingClient()
    _set_account_field(client, "status", "x" * 300)
    with pytest.raises(RehearsalError, match="PROVIDER_STRING_TOO_LONG"):
        _trading(client).capture(Credentials("key", "secret"))


def test_provider_numeric_strings_are_bounded_before_decimal_conversion() -> None:
    client = FakeTradingClient()
    _set_account_field(client, "equity", "9" * 100_000)
    with pytest.raises(RehearsalError, match="PROVIDER_NUMBER_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_provider_decimal_normalization_does_not_round_material_digits() -> None:
    client = FakeTradingClient()
    _set_account_field(client, "equity", "0.12345678901234567890123456789012")
    capture = _trading(client).capture(Credentials("key", "secret"))
    assert capture.account["equity"] == "0.12345678901234567890123456789012"


def test_rehearsal_detects_change_to_material_account_field(tmp_path: Path) -> None:
    client = FakeTradingClient()
    _set_account_field(client, "cash", "100000")
    archive, pin = _archive(tmp_path)

    class MutatingProcess(FakeProcess):
        def run_fd(self, descriptor, argv, environment):
            output = super().run_fd(descriptor, argv, environment)
            if argv[1] == "doctor":
                _set_account_field(client, "cash", "99999")
            return output

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    cli = VerifiedCli.from_archive(archive, pin, MutatingProcess())
    http = FakeHttp([HttpResponse(200, [], None, None), HttpResponse(200, [], None, None)])
    trading = _trading(client, http)
    with pytest.raises(RehearsalError, match="ACCOUNT_BOOK_CHANGED"):
        asyncio.run(
            run_fixture_development(
                Credentials("key", "secret"),
                trading,
                cli,
                MCPBoundary(FakeMCPClient, now=lambda: NOW),
                store,
                now=lambda: NOW,
            )
        )
    cli.close()
    store.close()


def test_development_public_summary_is_bound_and_identifier_free(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)
    cli = VerifiedCli.from_archive(archive, pin, FakeProcess())
    http = FakeHttp([HttpResponse(200, [], None, None), HttpResponse(200, [], None, None)])
    trading = _trading(http=http)
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    result = asyncio.run(
        run_fixture_development(
            Credentials("key", "secret"),
            trading,
            cli,
            MCPBoundary(FakeMCPClient, now=lambda: NOW),
            store,
            now=lambda: NOW,
        )
    )
    encoded = json.dumps(result.summary)
    assert "private-account" not in encoded
    assert "100000" not in encoded
    assert "archive_sha256" not in encoded
    assert "binary_sha256" not in encoded
    assert result.summary["private_evidence_sha256"]
    assert result.summary["artifact_class"] == "FIXTURE_TEST_ONLY"
    assert result.summary["competition_evidence"] is False
    result.artifact_directory.close()
    cli.close()
    store.close()


@pytest.mark.parametrize("value", ["1e999", "1e-999", "-9.99e999"])
def test_provider_numbers_reject_exponent_render_amplification(value: str) -> None:
    client = FakeTradingClient()
    _set_account_field(client, "equity", value)
    with pytest.raises(RehearsalError, match="PROVIDER_NUMBER_INVALID"):
        _trading(client).capture(Credentials("key", "secret"))


def test_plain_doctor_rejects_semantically_negative_ok_lines(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)
    doctor = "\n".join(
        [
            "Configuration corrupt PASS",
            "Credentials invalid OK",
            "Connectivity unreachable OK",
            f"Paper trading endpoint {PAPER_ENDPOINT}",
        ]
    )
    cli = VerifiedCli.from_archive(archive, pin, FakeProcess(outputs=["0.0.13\n", doctor]))
    with pytest.raises(RehearsalError, match="CLI_PAPER_HOST_NOT_VERIFIED"):
        cli.probe(Credentials("key", "secret"))


def test_closed_artifact_handle_rejects_every_read(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    handle = store.seal_fixture(Mode.DEVELOPMENT, NOW, {}, {})
    handle.close()
    with pytest.raises(RehearsalError, match="ARTIFACT_HANDLE_CLOSED"):
        handle.read_public()
    with pytest.raises(RehearsalError, match="ARTIFACT_HANDLE_CLOSED"):
        _ = handle.path
    store.close()


def test_artifact_store_closes_descriptor_when_root_is_not_private(tmp_path: Path) -> None:
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(10):
        with pytest.raises(RehearsalError, match="ARTIFACT_ROOT_NOT_PRIVATE"):
            ArtifactStore.open(root)
    assert len(os.listdir("/proc/self/fd")) == before


def test_artifact_store_rechecks_root_privacy_before_each_seal(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    root.chmod(0o755)
    with pytest.raises(RehearsalError, match="ARTIFACT_ROOT_NOT_PRIVATE"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {}, {})
    assert list(root.iterdir()) == []
    store.close()


def test_oversized_artifact_fails_before_staging_is_visible(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="PRIVATE_ARTIFACT_TOO_LARGE"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {"capture": "x" * (11 * 1024 * 1024)}, {})
    assert list(root.iterdir()) == []
    store.close()


def test_fixture_store_cannot_set_production_authority_fields(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="FIXTURE_ARTIFACT_FIELD_FORBIDDEN"):
        store.seal_fixture(
            Mode.SUBMISSION_NO_TRADE,
            NOW,
            {},
            {"competition_evidence": True},
        )
    with pytest.raises(RehearsalError, match="FIXTURE_ARTIFACT_FIELD_FORBIDDEN"):
        store.seal_fixture(
            Mode.SUBMISSION_NO_TRADE,
            NOW,
            {},
            {"claims": {"competition_evidence": True}},
        )
    assert not hasattr(rehearsal, "_ProductionArtifact")
    assert not hasattr(rehearsal, "_PRODUCTION_ARTIFACT_TOKEN")
    assert not hasattr(rehearsal, "_CANONICAL_SUBMISSION_TOKEN")
    assert not hasattr(rehearsal, "_qualify_production")
    forged = rehearsal._ProviderRehearsalArtifact(
        Mode.SUBMISSION_NO_TRADE,
        NOW,
        {},
        {"competition_evidence": True},
    )
    with pytest.raises(RehearsalError, match="COMPETITION_EVIDENCE_FORBIDDEN"):
        store._seal_provider_rehearsal(forged)
    assert list(root.iterdir()) == []
    store.close()


def test_fixture_store_cannot_emit_competition_true_through_stateful_key_coercion(
    tmp_path: Path,
) -> None:
    class StatefulKey:
        def __init__(self) -> None:
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return "competition_evidence" if self.calls >= 3 else "fixture_note"

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {StatefulKey(): True}, {})
    assert list(root.iterdir()) == []
    store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {1: "coercive", "1": "duplicate-after-coercion"},
        type("DictSubclass", (dict,), {})({"safe": True}),
    ],
)
def test_artifact_json_rejects_coercive_or_custom_containers(
    tmp_path: Path, payload: object
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="ARTIFACT_JSON_INVALID"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, payload, {})
    assert list(root.iterdir()) == []
    store.close()


def test_artifact_json_depth_is_bounded_before_staging(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    value: dict[str, object] = {}
    cursor = value
    for _ in range(1_200):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(RehearsalError, match="ARTIFACT_JSON_BOUNDS_EXCEEDED"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, value, {})
    assert list(root.iterdir()) == []
    store.close()


def test_artifact_json_rejects_unbounded_non_provider_numbers_before_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="ARTIFACT_JSON_BOUNDS_EXCEEDED"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {}, {"amplified": 1e300})
    assert list(root.iterdir()) == []
    store.close()


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf"), 1e300])
def test_artifact_json_rejects_nonfinite_or_amplifying_numbers_before_staging(
    tmp_path: Path, number: float
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="ARTIFACT_JSON_BOUNDS_EXCEEDED"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {}, {"numeric_value": number})
    assert list(root.iterdir()) == []
    store.close()


def test_artifact_json_rejects_aggregate_numeric_amplification_before_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="ARTIFACT_JSON_BOUNDS_EXCEEDED"):
        store.seal_fixture(
            Mode.DEVELOPMENT,
            NOW,
            {},
            {"numeric_values": [10**18] * 10_000},
        )
    assert list(root.iterdir()) == []
    store.close()


def test_artifact_publish_is_atomic_no_replace_under_final_name_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    identifier = "12345678-1234-5678-1234-567812345678"
    final_name = f"20260829T123456Z-development-{identifier}"
    original = store._require_name_available
    checks = 0

    def race(name: str) -> None:
        nonlocal checks
        checks += 1
        original(name)
        if checks == 2:
            (root / name).mkdir(mode=0o700)

    monkeypatch.setattr(store, "_require_name_available", race)
    with pytest.raises(RehearsalError, match="ARTIFACT_DIRECTORY_CREATE_FAILED"):
        store.seal_fixture(
            Mode.DEVELOPMENT,
            NOW,
            {},
            {},
            run_id=identifier,
        )
    assert (root / final_name).is_dir()
    assert list((root / final_name).iterdir()) == []
    assert all(not path.name.startswith(".staging-") for path in root.iterdir())
    store.close()


def test_artifact_handle_can_verify_public_summary_repeatedly(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    handle = store.seal_fixture(Mode.DEVELOPMENT, NOW, {}, {"result": "safe"})

    first = handle.read_public()
    second = handle.read_public()

    assert first == second
    handle.close()
    store.close()


def test_artifact_publish_fails_closed_when_atomic_noreplace_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NoRenameAt2:
        pass

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    monkeypatch.setattr(rehearsal.ctypes, "CDLL", lambda *_args, **_kwargs: NoRenameAt2())
    with pytest.raises(RehearsalError, match="ATOMIC_ARTIFACT_PUBLISH_UNAVAILABLE"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {}, {})
    assert list(root.iterdir()) == []
    store.close()


def test_artifact_publish_rejects_final_name_identity_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    original = rehearsal._rename_directory_noreplace

    def swap_after_publish(
        source_directory: int,
        source_name: str,
        destination_directory: int,
        destination_name: str,
    ) -> None:
        original(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
        )
        os.rename(
            destination_name,
            "displaced",
            src_dir_fd=destination_directory,
            dst_dir_fd=destination_directory,
        )
        os.mkdir(destination_name, mode=0o700, dir_fd=destination_directory)

    monkeypatch.setattr(rehearsal, "_rename_directory_noreplace", swap_after_publish)
    with pytest.raises(RehearsalError, match="ARTIFACT_WRITE_FAILED"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {}, {})
    assert sorted(path.name for path in root.iterdir()) == ["displaced"]
    assert list((root / "displaced").iterdir()) == []
    (root / "displaced").rmdir()
    store.close()


def test_artifact_json_rejects_integer_subclasses_instead_of_treating_them_as_numbers(
    tmp_path: Path,
) -> None:
    class BoolLikeInteger(int):
        pass

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="ARTIFACT_JSON_INVALID"):
        store.seal_fixture(
            Mode.DEVELOPMENT,
            NOW,
            {},
            {"numeric_value": BoolLikeInteger(True)},
        )
    assert list(root.iterdir()) == []
    store.close()


def test_artifact_store_rejects_stateful_metadata_subclasses_and_invalid_unicode(
    tmp_path: Path,
) -> None:
    class StatefulRunId(str):
        def __format__(self, _spec: str) -> str:
            return "../../escape"

    class DatetimeSubclass(datetime):
        pass

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    with pytest.raises(RehearsalError, match="ARTIFACT_JSON_INVALID"):
        store.seal_fixture(
            Mode.DEVELOPMENT,
            NOW,
            {},
            {},
            run_id=StatefulRunId("12345678-1234-5678-1234-567812345678"),
        )
    with pytest.raises(RehearsalError, match="ARTIFACT_JSON_INVALID"):
        store.seal_fixture(
            Mode.DEVELOPMENT,
            DatetimeSubclass(2026, 8, 29, tzinfo=UTC),
            {},
            {},
        )
    with pytest.raises(RehearsalError, match="ARTIFACT_JSON_INVALID"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {}, {"text": "\ud800"})
    assert list(root.iterdir()) == []
    store.close()


def test_closed_artifact_store_cannot_reuse_a_recycled_descriptor(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    store.close()
    with pytest.raises(RehearsalError, match="ARTIFACT_ROOT_INVALID"):
        store.seal_fixture(Mode.DEVELOPMENT, NOW, {}, {})
    assert list(root.iterdir()) == []


def _book(role: AccountRole, fingerprint: str) -> BookCapture:
    return BookCapture(
        role=role.value,
        account_fingerprint=fingerprint,
        account={
            "status": "ACTIVE",
            "equity": "100000",
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
        },
        positions=(),
        orders=(),
        activities=(),
        sdk_trace=("get_account", "get_all_positions", "get_orders"),
        http_trace=("GET",),
        pagination_terminal=True,
    )


def _read_transport() -> TransportLedger:
    ledger = TransportLedger()
    ledger._record("GET", PAPER_ENDPOINT + "/v2/account", {})
    return ledger


def _development_counters() -> SafetyCounters:
    counters = SafetyCounters()
    counters.record("mcp_sessions")
    counters.record("mcp_calls")
    counters.record("repository_calls")
    return counters


def _service_evidence(role: AccountRole, fingerprint: str):
    if role is AccountRole.DEVELOPMENT:
        terminal_code = "PROVIDER_FAILURE_NO_ACTION"
        acquisition = 1
    else:
        terminal_code = "CALIBRATION_BINDING_NO_TRADE"
        acquisition = 0
    return rehearsal._CanonicalServiceEvidence(
        terminal_code,
        "CONTEXT_ACTIVE_POSITION_NOT_UNIQUE" if role is AccountRole.DEVELOPMENT else None,
        {
            "acquisition": acquisition,
            "mcp": 0,
            "gemini": 0,
            "proposal": 0,
            "intent": 0,
            "execution": 0,
            "provider_write": 0,
            "repository": 4,
        },
        fingerprint,
    )


def test_submission_production_qualification_requires_clean_baseline() -> None:
    fingerprint = "a" * 64
    capture = _book(AccountRole.SUBMISSION, fingerprint)
    counters = SafetyCounters()
    counters.record("repository_calls")
    with pytest.raises(RehearsalError, match="SUBMISSION_PRODUCTION_QUALIFICATION_FAILED"):
        _qualify_provider_rehearsal(
            Mode.SUBMISSION_NO_TRADE,
            NOW,
            capture,
            capture,
            transport=_read_transport(),
            cli=None,
            mcp=None,
            service=_service_evidence(AccountRole.SUBMISSION, fingerprint),
            provider_counters=counters,
            authority=DurableAuthorityEvidence(AccountRole.SUBMISSION, fingerprint, None),
        )


def test_production_qualification_has_no_caller_controlled_fixture_switch() -> None:
    parameters = inspect.signature(_qualify_provider_rehearsal).parameters
    assert "fixture" not in parameters
    assert "qualified" not in parameters
    assert "competition" not in parameters


def test_submission_authority_requires_preexisting_clean_durable_baseline() -> None:
    fingerprint = "a" * 64
    account = SimpleNamespace(
        role=AccountRole.SUBMISSION.value,
        account_fingerprint=fingerprint,
        equity=Decimal("100000"),
    )
    baseline = SimpleNamespace(
        account_role=AccountRole.SUBMISSION.value,
        account_fingerprint=fingerprint,
        equity=Decimal("100000"),
        captured_at=NOW,
        positions_hash="b" * 64,
        orders_hash="c" * 64,
        activities_hash="d" * 64,
        contaminated=False,
    )

    class Session:
        def __init__(self, account_row, baseline_row):
            self.account = account_row
            self.baseline = baseline_row

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, *_args):
            return self.account

        def scalar(self, _statement):
            return self.baseline

    class Sessions:
        def __init__(self, session):
            self.session = session

        def __call__(self):
            return self.session

    persistence = SimpleNamespace(sessions=Sessions(Session(account, baseline)))
    authority = _validate_preexisting_authority(
        persistence,
        role=AccountRole.SUBMISSION,
        account_fingerprint=fingerprint,
    )
    assert authority.role is AccountRole.SUBMISSION
    assert authority.account_fingerprint == fingerprint
    assert len(authority.baseline_evidence_hash or "") == 64

    persistence.sessions = Sessions(Session(None, baseline))
    with pytest.raises(RehearsalError, match="PREEXISTING_ACCOUNT_AUTHORITY_REQUIRED"):
        _validate_preexisting_authority(
            persistence,
            role=AccountRole.SUBMISSION,
            account_fingerprint=fingerprint,
        )

    account.role = AccountRole.DEVELOPMENT.value
    persistence.sessions = Sessions(Session(account, baseline))
    with pytest.raises(RehearsalError, match="PREEXISTING_ACCOUNT_AUTHORITY_REQUIRED"):
        _validate_preexisting_authority(
            persistence,
            role=AccountRole.SUBMISSION,
            account_fingerprint=fingerprint,
        )

    account.role = AccountRole.SUBMISSION.value
    baseline.contaminated = True
    persistence.sessions = Sessions(Session(account, baseline))
    with pytest.raises(RehearsalError, match="CLEAN_SUBMISSION_BASELINE_REQUIRED"):
        _validate_preexisting_authority(
            persistence,
            role=AccountRole.SUBMISSION,
            account_fingerprint=fingerprint,
        )


def test_production_artifact_is_independently_verified_and_tamper_evident(
    tmp_path: Path,
) -> None:
    fingerprint = "a" * 64
    capture = _book(AccountRole.DEVELOPMENT, fingerprint)
    artifact = _qualify_provider_rehearsal(
        Mode.DEVELOPMENT,
        NOW,
        capture,
        capture,
        transport=_read_transport(),
        cli={"version": "0.0.13", "paper_host_verified": True, "dry_run": True},
        mcp=SimpleNamespace(
            tool_surface_count=22,
            call_trace=(("get_clock", {}),),
            duration_ms=1,
            result_summary_hash="e" * 64,
        ),
        service=_service_evidence(AccountRole.DEVELOPMENT, fingerprint),
        provider_counters=_development_counters(),
        authority=DurableAuthorityEvidence(AccountRole.DEVELOPMENT, fingerprint, None),
    )
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = ArtifactStore.open(root)
    handle = store._seal_provider_rehearsal(artifact)
    summary = json.loads(handle.read_public())
    assert summary["artifact_class"] == "PROVIDER_REHEARSAL"
    assert summary["provider_rehearsal"] is True
    assert summary["competition_evidence"] is False
    assert summary["production_factory"] == "backend.app.runtime.build_production_agent"

    private = handle.path / "manifest.private.json"
    private.chmod(0o600)
    private.write_text("{}\n")
    private.chmod(0o400)
    with pytest.raises(RehearsalError, match="ARTIFACT_VERIFICATION_FAILED"):
        handle.read_public()
    handle.close()
    store.close()


def test_canonical_submission_probe_instruments_existing_service() -> None:
    authority = ObservedPaperAccountAuthority(AccountRole.SUBMISSION, "a" * 64, True, False)
    binding = CalibrationBinding(
        AccountRole.SUBMISSION,
        "a" * 64,
        "CALIBRATION_BINDING_NO_TRADE",
        "b" * 64,
        "c" * 64,
        NOW,
        NOW,
    )

    class CanonicalDecisions(Decisions):
        decision = None

        def persist_decision(self, tick, decision, proposal):
            self.decision = decision
            return super().persist_decision(tick, decision, proposal)

        def complete_tick(self, tick, terminal_code, certificate):
            return AgentRunResult(tick.tick_id, terminal_code, self.decision, None, None, "f" * 64)

    service = AgentRunService(
        account_authority=Authority(authority),
        clock=Clock(),
        calibration=Calibration(binding),
        acquisition=SimpleNamespace(acquire=lambda *_args: None),
        decisions=CanonicalDecisions(authority),
        runtime=Runtime(),
        server_autonomy_enabled=False,
    )
    original = (
        service._account_authority,
        service._acquisition,
        service._decisions,
        service._runtime,
    )
    evidence = asyncio.run(_probe_canonical_submission(SimpleNamespace(service=service)))
    assert evidence.counts["acquisition"] == 0
    assert evidence.counts["execution"] == 0
    assert (
        service._account_authority,
        service._acquisition,
        service._decisions,
        service._runtime,
    ) == original


def test_canonical_development_probe_runs_actual_service_and_counts_outcome() -> None:
    authority = ObservedPaperAccountAuthority(AccountRole.DEVELOPMENT, "a" * 64, True, False)

    class CanonicalDecisions(Decisions):
        decision = None

        def persist_decision(self, tick, decision, proposal):
            self.decision = decision
            return super().persist_decision(tick, decision, proposal)

        def complete_tick(self, tick, terminal_code, certificate):
            return AgentRunResult(tick.tick_id, terminal_code, self.decision, None, None, "f" * 64)

    service = AgentRunService(
        account_authority=Authority(authority),
        clock=Clock(),
        calibration=SimpleNamespace(),
        acquisition=FailingLifecycleAcquisition(),
        decisions=CanonicalDecisions(authority),
        runtime=Runtime(),
        server_autonomy_enabled=False,
    )
    evidence = asyncio.run(_probe_canonical_development(SimpleNamespace(service=service)))
    assert evidence.terminal_code == "PROVIDER_FAILURE_NO_ACTION"
    assert evidence.counts == {
        "acquisition": 1,
        "mcp": 0,
        "gemini": 0,
        "proposal": 0,
        "intent": 0,
        "execution": 0,
        "provider_write": 0,
        "repository": 4,
    }
    assert evidence.account_fingerprint == "a" * 64


def test_development_operator_runs_canonical_service_before_sealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.app.config import RuntimeRole

    account_id = "00000000-0000-0000-0000-000000000001"
    fingerprint = hashlib.sha256(f"{account_id}\n".encode()).hexdigest()
    authority = ObservedPaperAccountAuthority(
        AccountRole.DEVELOPMENT,
        fingerprint,
        True,
        False,
    )

    class CanonicalDecisions(Decisions):
        decision = None

        def persist_decision(self, tick, decision, proposal):
            self.decision = decision
            return super().persist_decision(tick, decision, proposal)

        def complete_tick(self, tick, terminal_code, certificate):
            return AgentRunResult(tick.tick_id, terminal_code, self.decision, None, None, "f" * 64)

    service = AgentRunService(
        account_authority=Authority(authority),
        clock=Clock(),
        calibration=SimpleNamespace(),
        acquisition=FailingLifecycleAcquisition(),
        decisions=CanonicalDecisions(authority),
        runtime=Runtime(),
        server_autonomy_enabled=False,
    )
    agent = SimpleNamespace(
        service=service,
        persistence=SimpleNamespace(database_clock=Clock()),
        resources=SimpleNamespace(),
        aclose=lambda: asyncio.sleep(0),
    )
    settings = SimpleNamespace(
        app_account_role=RuntimeRole.DEVELOPMENT,
        alpaca_api_key=SimpleNamespace(get_secret_value=lambda: "key"),
        alpaca_secret_key=SimpleNamespace(get_secret_value=lambda: "secret"),
    )
    store_root = tmp_path / "private"
    store_root.mkdir(mode=0o700)
    store = ArtifactStore.open(store_root)

    async def build(_settings, role, transport, counters):
        assert role is AccountRole.DEVELOPMENT
        counters.record("repository_calls")
        retained_mcp = rehearsal._CountedMCPClient(FakeMCPClient(), counters)
        await retained_mcp.__aenter__()
        agent.resources.mcp_research = SimpleNamespace(value=retained_mcp)

        async def close_agent():
            await retained_mcp.__aexit__(None, None, None)

        agent.aclose = close_agent
        return agent, DurableAuthorityEvidence(role, fingerprint, None)

    def trading(_agent, transport, role):
        return TradingBoundary(
            client_factory=lambda _credentials: FakeTradingClient(),
            activity_http=ActivityHttpBoundary(
                FakeHttp([HttpResponse(200, [], None, None), HttpResponse(200, [], None, None)]),
                transport,
            ),
            endpoint=PAPER_ENDPOINT,
            ledger=transport,
            role=role,
        )

    cli = SimpleNamespace(
        probe=lambda _credentials: {
            "version": "0.0.13",
            "paper_host_verified": True,
            "dry_run": True,
        },
        close=lambda: None,
    )
    monkeypatch.setattr("backend.app.config.Settings", lambda: settings)
    monkeypatch.setattr(rehearsal, "_build_canonical_production_agent", build)
    monkeypatch.setattr(rehearsal, "_trading_boundary_from_agent", trading)
    monkeypatch.setattr(VerifiedCli, "from_archive", lambda *_args: cli)
    monkeypatch.setattr(ArtifactStore, "open_fixed", classmethod(lambda _cls: store))

    result = asyncio.run(run_development_operator())
    assert result.summary["service"] == {
        "terminal_code": "PROVIDER_FAILURE_NO_ACTION",
        "failure_code": "CONTEXT_ACTIVE_POSITION_NOT_UNIQUE",
        "counts": {
            "acquisition": 1,
            "mcp": 0,
            "gemini": 0,
            "proposal": 0,
            "intent": 0,
            "execution": 0,
            "provider_write": 0,
            "repository": 4,
        },
    }
    assert result.summary["competition_evidence"] is False
    receipt = rehearsal.build_public_rehearsal_receipt(result.summary)
    assert receipt["service_terminal_code"] == "PROVIDER_FAILURE_NO_ACTION"
    assert receipt["safe_stop_reason"] == "CONTEXT_ACTIVE_POSITION_NOT_UNIQUE"
    assert receipt["provider_write_calls"] == 0
    assert receipt["competition_evidence"] is False
    assert {
        "private_evidence_sha256",
        "run_id",
        "provider_counters",
        "invocation",
    }.isdisjoint(receipt)
    tampered = dict(result.summary)
    tampered["provider_write_calls"] = 1
    with pytest.raises(RehearsalError, match="PUBLIC_RECEIPT_INVALID"):
        rehearsal.build_public_rehearsal_receipt(tampered)
    result.artifact_directory.close()


@pytest.mark.parametrize(
    ("command", "runner_name"),
    [
        ("development", "run_development_operator"),
        ("submission-no-trade", "run_submission_no_trade_operator"),
    ],
)
def test_fixed_command_dispatch_closes_handle(monkeypatch, capsys, command, runner_name) -> None:
    class Handle:
        name = "safe-artifact-name"
        closed = False

        def read_public(self):
            return b"{}\n"

        def close(self):
            self.closed = True

    handle = Handle()

    async def run():
        return RehearsalResult(handle, {})

    monkeypatch.setattr(f"ops.launch.provider_rehearsal.{runner_name}", run)
    from ops.launch.provider_rehearsal import main

    assert main([command]) == 0
    assert handle.closed is True
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "artifact": "safe-artifact-name",
        "mode": command,
        "public_summary_sha256": hashlib.sha256(b"{}\n").hexdigest(),
        "status": "ok",
    }


@pytest.mark.parametrize("arguments", [[], ["submission"], ["development", "--role", "SUBMISSION"]])
def test_fixed_command_rejects_every_selector(arguments: list[str], capsys) -> None:
    from ops.launch.provider_rehearsal import main

    assert main(arguments) == 2
    assert json.loads(capsys.readouterr().err) == {"code": "USAGE", "status": "error"}
