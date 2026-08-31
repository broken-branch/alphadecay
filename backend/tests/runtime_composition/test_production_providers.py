from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr

from backend.app.config import RuntimeRole, Settings
from backend.app.runtime import RuntimeCompositionError
from backend.app.runtime.providers import build_production_resources


class _Session:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self._session = _Session()

    def get_account(self) -> object:
        return SimpleNamespace(
            id=UUID("00000000-0000-0000-0000-000000000123"),
            equity=Decimal("100000"),
        )


class _HttpClient:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Model:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _MCP:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False

    async def __aenter__(self) -> _MCP:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True


@dataclass
class _Factories:
    trading: _Client | None = None
    option_data: _Client | None = None
    stock_data: _Client | None = None
    http: _HttpClient | None = None
    model: _Model | None = None
    mcp: _MCP | None = None

    def trading_factory(self, **kwargs: object) -> _Client:
        self.trading = _Client(**kwargs)
        return self.trading

    def option_data_factory(self, **kwargs: object) -> _Client:
        self.option_data = _Client(**kwargs)
        return self.option_data

    def stock_data_factory(self, **kwargs: object) -> _Client:
        self.stock_data = _Client(**kwargs)
        return self.stock_data

    def http_factory(self, **kwargs: object) -> _HttpClient:
        self.http = _HttpClient(**kwargs)
        return self.http

    def model_factory(self, api_key: str) -> _Model:
        self.model = _Model(api_key)
        return self.model

    def mcp_factory(self, **kwargs: object) -> _MCP:
        self.mcp = _MCP(**kwargs)
        return self.mcp


def _settings() -> Settings:
    return Settings(
        app_account_role=RuntimeRole.SUBMISSION,
        app_policy_hash=SecretStr("a" * 64),
        app_calibration_hash=SecretStr("b" * 64),
        app_calibration_decision_boundary=datetime(2026, 8, 28, 16, tzinfo=UTC),
        app_calibration_sealed_at=datetime(2026, 8, 28, 16, 1, tzinfo=UTC),
        app_entry_equity_floor=SecretStr("99000"),
        app_maximum_lifetime_entries=SecretStr("3"),
        app_maximum_lifetime_risk=SecretStr("1500"),
        app_maximum_position_loss=SecretStr("900"),
        app_maximum_entry_quantity=SecretStr("4"),
        alpaca_api_endpoint="https://paper-api.alpaca.markets",
        alpaca_api_key=SecretStr("paper-key"),
        alpaca_secret_key=SecretStr("paper-secret"),
        database_url=SecretStr("postgresql://db.invalid/alphadecay"),
        gemini_api_key=SecretStr("model-key"),
        app_owner_access_code=SecretStr("owner-access-code"),
        app_session_secret=SecretStr("s" * 32),
        app_provider_settings_secret=SecretStr("p" * 32),
        app_allowed_origin="https://alphadecay.example",
        scheduler_token=SecretStr("t" * 32),
    )


def test_production_resources_are_paper_bound_and_share_one_trading_owner() -> None:
    factories = _Factories()
    resources = build_production_resources(
        _settings(),
        trading_factory=factories.trading_factory,
        option_data_factory=factories.option_data_factory,
        stock_data_factory=factories.stock_data_factory,
        http_factory=factories.http_factory,
        model_factory=factories.model_factory,
        mcp_factory=factories.mcp_factory,
    )

    providers = resources.providers
    assert len(resources.account_fingerprint) == 64
    assert resources.observed_equity == Decimal("100000")
    assert providers.binding.endpoint == "https://paper-api.alpaca.markets"
    assert providers.binding.paper is True
    assert providers.trading.resource is providers.option_contracts.resource
    assert providers.stock_market_data.resource.value is factories.stock_data
    assert callable(providers.activities.resource.value.collect_lifecycle)
    assert factories.trading is not None
    assert factories.trading.kwargs["paper"] is True
    assert factories.trading.kwargs["url_override"] == providers.binding.endpoint
    assert factories.http is not None
    assert factories.http.kwargs["follow_redirects"] is False
    assert "paper-key" not in repr(resources)
    assert "paper-secret" not in repr(resources)
    assert "model-key" not in repr(resources)


def test_production_resource_cleanup_closes_each_owned_client() -> None:
    factories = _Factories()
    resources = build_production_resources(
        _settings(),
        trading_factory=factories.trading_factory,
        option_data_factory=factories.option_data_factory,
        stock_data_factory=factories.stock_data_factory,
        http_factory=factories.http_factory,
        model_factory=factories.model_factory,
        mcp_factory=factories.mcp_factory,
    )

    resources.providers.trading.resource.close()
    resources.providers.option_snapshots.resource.close()
    resources.providers.stock_market_data.resource.close()
    resources.providers.activities.resource.close()
    resources.model_transport.close()
    asyncio.run(resources.mcp_research.aclose())

    assert factories.trading is not None and factories.trading._session.closed
    assert factories.option_data is not None and factories.option_data._session.closed
    assert factories.stock_data is not None and factories.stock_data._session.closed
    assert factories.http is not None and factories.http.closed
    assert factories.model is not None and factories.model.closed
    assert factories.mcp is not None and factories.mcp.closed


def test_construction_failure_closes_clients_already_created() -> None:
    factories = _Factories()

    def fail_model(_api_key: str) -> _Model:
        raise RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        build_production_resources(
            _settings(),
            trading_factory=factories.trading_factory,
            option_data_factory=factories.option_data_factory,
            stock_data_factory=factories.stock_data_factory,
            http_factory=factories.http_factory,
            model_factory=fail_model,
            mcp_factory=factories.mcp_factory,
        )

    assert factories.trading is not None and factories.trading._session.closed
    assert factories.option_data is not None and factories.option_data._session.closed
    assert factories.stock_data is not None and factories.stock_data._session.closed
    assert factories.http is not None and factories.http.closed


@pytest.mark.parametrize(
    "updates",
    [
        {"alpaca_api_endpoint": "https://api.alpaca.markets"},
        {"alpaca_paper_trade": False},
    ],
)
def test_final_provider_boundary_rejects_live_settings_before_any_factory_call(
    updates: dict[str, object],
) -> None:
    calls = 0

    def forbidden_factory(**_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("provider factory must not run")

    bypassed = _settings().model_copy(update=updates)

    with pytest.raises(RuntimeCompositionError, match="PAPER_TRADING_REQUIRED"):
        build_production_resources(bypassed, trading_factory=forbidden_factory)

    assert calls == 0
