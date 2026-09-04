from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

import httpx
from alpaca.data.enums import DataFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from pydantic import BaseModel, ConfigDict

from backend.app.alpaca.activities import (
    AccountActivitiesAdapter,
    LifecycleAccountActivitiesAdapter,
)
from backend.app.alpaca.execution_evidence import baseline_account_fingerprint
from backend.app.alpaca.mcp import AlpacaMCPResearchClient
from backend.app.alpaca.opportunity_halt_stream import (
    AlpacaOpportunityHaltStreamAdapter,
    HaltStockDataStream,
    PinnedAlpacaStockDataStream,
    alpaca_trading_status_codebook,
)
from backend.app.config import Settings
from backend.app.evidence.gemini import GeminiStructuredTransport
from backend.app.services.opportunity_halt_authority import (
    HaltAuthorityConfig,
    HaltAuthoritySnapshot,
    initial_halt_authority,
)

from .composition import (
    PAPER_TRADING_ENDPOINT,
    BoundProviderResource,
    ProviderBinding,
    RuntimeCompositionError,
    RuntimeProviderBundle,
    RuntimeResource,
)


class _Closable(Protocol):
    def close(self) -> None: ...


class OpportunityHaltAuthority(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def snapshot(self, *, read_at: datetime | None = None) -> HaltAuthoritySnapshot: ...


class _ObservedAccount(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: UUID
    equity: Decimal


@dataclass(frozen=True)
class ProductionResources:
    account_fingerprint: str
    observed_equity: Decimal
    providers: RuntimeProviderBundle
    model_transport: RuntimeResource[GeminiStructuredTransport]
    mcp_research: RuntimeResource[AlpacaMCPResearchClient]


def build_production_opportunity_halt_resource(
    settings: Settings,
    *,
    config: HaltAuthorityConfig,
    clock: Callable[[], datetime],
    stream_factory: Callable[..., HaltStockDataStream] = PinnedAlpacaStockDataStream,
    adapter_factory: Callable[..., OpportunityHaltAuthority] = (AlpacaOpportunityHaltStreamAdapter),
) -> RuntimeResource[OpportunityHaltAuthority]:
    _require_paper_settings(settings)
    codebook = alpaca_trading_status_codebook()
    if not callable(clock):
        raise RuntimeCompositionError("HALT_STREAM_CLOCK_INVALID")
    initial_halt_authority(config=config, read_at=config.session_open_at)
    if config.codebook_hash != codebook.source_hash:
        raise RuntimeCompositionError("HALT_STREAM_CODEBOOK_MISMATCH")

    stream: HaltStockDataStream | None = None
    adapter: OpportunityHaltAuthority | None = None
    try:
        stream = stream_factory(
            api_key=settings.alpaca_api_key.get_secret_value(),
            secret_key=settings.alpaca_secret_key.get_secret_value(),
            raw_data=False,
            feed=DataFeed.IEX,
            data_timeout=config.maximum_trade_age.total_seconds(),
        )
        adapter = adapter_factory(
            stream,
            config=config,
            codebook=codebook,
            clock=clock,
        )
        adapter.start()
    except BaseException as startup_error:
        cleanup_errors = _close_partial_halt_resource(adapter, stream)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "OPPORTUNITY_HALT_STREAM_STARTUP_AND_CLEANUP_FAILED",
                (startup_error, *cleanup_errors),
            ) from None
        raise
    return RuntimeResource.owned(adapter, adapter.stop)


def build_production_resources(
    settings: Settings,
    *,
    trading_factory: Callable[..., object] = TradingClient,
    option_data_factory: Callable[..., object] = OptionHistoricalDataClient,
    stock_data_factory: Callable[..., object] = StockHistoricalDataClient,
    http_factory: Callable[..., httpx.Client] = httpx.Client,
    model_factory: Callable[[str], GeminiStructuredTransport] = (
        GeminiStructuredTransport.from_api_key
    ),
    mcp_factory: Callable[..., AlpacaMCPResearchClient] = AlpacaMCPResearchClient,
) -> ProductionResources:
    _require_paper_settings(settings)
    api_key = settings.alpaca_api_key.get_secret_value()
    secret_key = settings.alpaca_secret_key.get_secret_value()
    with ExitStack() as construction:
        trading = trading_factory(
            api_key=api_key,
            secret_key=secret_key,
            paper=True,
            raw_data=False,
            url_override=settings.alpaca_api_endpoint,
        )
        close_trading = _close_alpaca_client(trading)
        construction.callback(close_trading)
        observed = _ObservedAccount.model_validate(trading.get_account())
        if not observed.equity.is_finite() or observed.equity <= 0:
            raise ValueError("OBSERVED_ACCOUNT_EQUITY_INVALID")
        account_fingerprint = baseline_account_fingerprint(observed.id)
        binding = ProviderBinding(
            endpoint=settings.alpaca_api_endpoint,
            account_fingerprint=account_fingerprint,
            account_binding_token=_binding_token(
                settings.app_session_secret.get_secret_value(), account_fingerprint
            ),
            paper=True,
        )
        option_data = option_data_factory(
            api_key=api_key,
            secret_key=secret_key,
            raw_data=False,
        )
        close_option_data = _close_alpaca_client(option_data)
        construction.callback(close_option_data)
        stock_data = stock_data_factory(
            api_key=api_key,
            secret_key=secret_key,
            raw_data=False,
        )
        close_stock_data = _close_alpaca_client(stock_data)
        construction.callback(close_stock_data)
        activity_http = http_factory(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
        )
        construction.callback(activity_http.close)
        activities = LifecycleAccountActivitiesAdapter(
            AccountActivitiesAdapter(
                activity_http,
                base_url=settings.alpaca_api_endpoint,
                api_key=api_key,
                secret_key=secret_key,
            ),
            expected_account_fingerprint=account_fingerprint,
        )
        model = model_factory(settings.gemini_api_key.get_secret_value())
        construction.callback(model.close)
        mcp = mcp_factory(api_key=api_key, secret_key=secret_key)
        construction.pop_all()

    trading_resource = RuntimeResource.owned(trading, close_trading)
    option_data_resource = RuntimeResource.owned(option_data, close_option_data)
    stock_data_resource = RuntimeResource.owned(stock_data, close_stock_data)
    activity_resource = RuntimeResource.owned(activities, activity_http.close)

    async def close_mcp() -> None:
        await mcp.__aexit__(None, None, None)

    return ProductionResources(
        account_fingerprint=account_fingerprint,
        observed_equity=observed.equity,
        providers=RuntimeProviderBundle(
            binding=binding,
            trading=BoundProviderResource(binding, trading_resource),
            activities=BoundProviderResource(binding, activity_resource),
            option_contracts=BoundProviderResource(binding, trading_resource),
            option_snapshots=BoundProviderResource(binding, option_data_resource),
            stock_market_data=BoundProviderResource(binding, stock_data_resource),
        ),
        model_transport=RuntimeResource.owned(model, model.close),
        mcp_research=RuntimeResource.async_owned(mcp, close_mcp),
    )


def _binding_token(secret: str, account_fingerprint: str) -> str:
    return hmac.new(
        secret.encode(),
        f"alphadecay:provider-binding:v1:{account_fingerprint}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _require_paper_settings(settings: Settings) -> None:
    if (
        settings.alpaca_api_endpoint != PAPER_TRADING_ENDPOINT
        or settings.alpaca_paper_trade is not True
    ):
        raise RuntimeCompositionError("PAPER_TRADING_REQUIRED")


def _close_partial_halt_resource(
    adapter: OpportunityHaltAuthority | None,
    stream: HaltStockDataStream | None,
) -> tuple[BaseException, ...]:
    errors: list[BaseException] = []
    if adapter is not None:
        close = adapter.stop
    elif stream is not None:
        close = stream.stop
    else:
        return ()
    try:
        close()
    except BaseException as error:
        errors.append(error)
    return tuple(errors)


def _close_alpaca_client(client: object) -> Callable[[], None]:
    session = getattr(client, "_session", None)
    if session is None or not callable(getattr(session, "close", None)):
        raise TypeError("ALPACA_CLIENT_SESSION_UNAVAILABLE")
    return session.close
