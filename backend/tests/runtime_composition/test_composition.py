from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alpaca.data.models import OptionsSnapshot
from alpaca.trading.enums import (
    AccountStatus,
    AssetClass,
    AssetExchange,
    AssetStatus,
    ContractType,
    ExerciseStyle,
    PositionSide,
)
from alpaca.trading.models import OptionContract, OptionContractsResponse, Position
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.alpaca.execution_evidence import (
    AlpacaOptionContractCollector,
    AlpacaReplacementQuoteCollector,
    ExecutionEvidenceError,
    baseline_account_fingerprint,
)
from backend.app.alpaca.mcp import AlpacaMCPResearchClient, MCPBoundaryError
from backend.app.alpaca.trading import ProviderDataError
from backend.app.contracts.v1 import (
    AccountRole,
    DataQuality,
    EvidenceRelation,
    GreekExposure,
    PositionIntent,
    SourceCluster,
    ThesisCreateRequest,
    ThesisResponse,
)
from backend.app.evidence.classifier import EvidenceClassificationContext
from backend.app.evidence.repository import SQLAlchemyEvidenceLedger
from backend.app.execution import (
    AccountObservation,
    AccountReconciliationState,
    ActivityItem,
    ActivityPaginationEvidence,
    ActivityType,
    Actor,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionBlocked,
    FrozenThesisVersion,
    OrderEnvelope,
    OrderLegIntent,
    SweepObservation,
    order_envelope_hash,
)
from backend.app.order_limits import EntryBudgetLimits
from backend.app.persistence import AgentDecisionRepository, SQLAlchemyExecutionRepository
from backend.app.persistence.sqlalchemy_models import (
    AccountReconciliationStateRow,
    Base,
    ModelCallBudgetRow,
    SubmissionBaselineRow,
)
from backend.app.runtime import (
    PAPER_TRADING_ENDPOINT,
    BoundProviderResource,
    ProviderBinding,
    RuntimeAccountConfig,
    RuntimeCompositionError,
    RuntimeDependencies,
    RuntimeProviderBundle,
    RuntimeResource,
    build_runtime,
)
from backend.app.runtime import composition as composition_module

ACCOUNT_ID = UUID(int=1)
FINGERPRINT = baseline_account_fingerprint(ACCOUNT_ID)
NOW = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
BINDING = ProviderBinding(
    endpoint=PAPER_TRADING_ENDPOINT,
    account_fingerprint=FINGERPRINT,
    account_binding_token="opaque-test-account-binding",
)
ENTRY_LIMITS = EntryBudgetLimits(
    policy_hash="d" * 64,
    equity_floor=Decimal("99000"),
    maximum_lifetime_entries=3,
    maximum_lifetime_risk=Decimal("1500"),
    maximum_position_loss=Decimal("800"),
    maximum_entry_quantity=4,
)


class CallGuard:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def guarded(*_args, **_kwargs):
            self.calls.append(name)
            raise AssertionError(f"unexpected call to {name}")

        return guarded


class RepositoryGuard(CallGuard):
    def __init__(self, error: str = "INTENT_NOT_AUTHORIZED") -> None:
        super().__init__()
        self.error = error
        self.claim_bindings: list[tuple[AccountRole | None, str | None]] = []

    def claim_intent(self, *_args, **_kwargs):
        self.calls.append("claim_intent")
        self.claim_bindings.append(
            (
                _kwargs.get("account_role"),
                _kwargs.get("account_fingerprint"),
            )
        )
        raise ExecutionBlocked(self.error)


class PersistenceGuard:
    def __init__(self) -> None:
        self.repository = RepositoryGuard()
        self.evidence_ledger = CallGuard()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class MCPGuard(CallGuard):
    async def __aenter__(self):
        self.calls.append("__aenter__")
        raise AssertionError("unexpected MCP start")

    async def __aexit__(self, *_args):
        self.calls.append("__aexit__")

    async def call(self, *_args, **_kwargs):
        self.calls.append("call")
        raise AssertionError("unexpected MCP call")


class ObservedAccountTrading(CallGuard):
    def __init__(self, account_id: UUID) -> None:
        super().__init__()
        self.account_id = account_id

    def get_account(self) -> object:
        self.calls.append("get_account")
        return SimpleNamespace(
            id=self.account_id,
            status="ACTIVE",
            equity="100000.00",
            buying_power="200000.00",
            account_blocked=False,
            trading_blocked=False,
            transfers_blocked=False,
            trade_suspended_by_user=False,
        )

    def get_all_positions(self) -> object:
        self.calls.append("get_all_positions")
        return []

    def get_orders(self, *_args, **_kwargs) -> object:
        self.calls.append("get_orders")
        return []


class ComposedTradingClient(ObservedAccountTrading):
    def get_account(self) -> object:
        self.calls.append("get_account")
        return SimpleNamespace(
            id=self.account_id,
            status=AccountStatus.ACTIVE,
            equity="100000.00",
            buying_power="200000.00",
            cash="100000.00",
            account_blocked=False,
            trading_blocked=False,
            transfers_blocked=False,
            trade_suspended_by_user=False,
            options_trading_level=3,
        )

    def submit_order(self, order_data: object) -> object:
        self.calls.append("submit_order")
        return SimpleNamespace(
            id=UUID(int=91),
            client_order_id=order_data.client_order_id,
            status="rejected",
            qty=order_data.qty,
            filled_qty="0",
            legs=None,
        )


class FilledComposedTradingClient(ComposedTradingClient):
    def __init__(self) -> None:
        super().__init__(ACCOUNT_ID)
        self.filled = False
        self.last_order: dict[str, object] | None = None

    def get_account(self) -> object:
        self.calls.append("get_account")
        return SimpleNamespace(
            id=self.account_id,
            status=AccountStatus.ACTIVE,
            equity="99880.00" if self.filled else "100000.00",
            buying_power="199760.00" if self.filled else "200000.00",
            cash="99880.00" if self.filled else "100000.00",
            account_blocked=False,
            trading_blocked=False,
            transfers_blocked=False,
            trade_suspended_by_user=False,
            options_trading_level=3,
        )

    def get_all_positions(self) -> object:
        self.calls.append("get_all_positions")
        if not self.filled:
            return []
        return [
            Position(
                asset_id=UUID(int=81),
                symbol="TEST260918C00100000",
                exchange=AssetExchange.NASDAQ,
                asset_class=AssetClass.US_OPTION,
                avg_entry_price="1.50",
                qty="1",
                side=PositionSide.LONG,
                cost_basis="150",
            ),
            Position(
                asset_id=UUID(int=82),
                symbol="TEST260918C00105000",
                exchange=AssetExchange.NASDAQ,
                asset_class=AssetClass.US_OPTION,
                avg_entry_price="0.30",
                qty="1",
                side=PositionSide.SHORT,
                cost_basis="30",
            ),
        ]

    def submit_order(self, order_data: object) -> object:
        self.calls.append("submit_order")
        self.filled = True
        self.last_order = {
            "id": str(UUID(int=91)),
            "client_order_id": order_data.client_order_id,
            "status": "filled",
            "qty": "1",
            "filled_qty": "1",
            "legs": [
                {
                    "symbol": "TEST260918C00100000",
                    "side": "buy",
                    "ratio_qty": "1",
                    "filled_qty": "1",
                    "filled_avg_price": "1.50",
                },
                {
                    "symbol": "TEST260918C00105000",
                    "side": "sell",
                    "ratio_qty": "1",
                    "filled_qty": "1",
                    "filled_avg_price": "0.30",
                },
            ],
        }
        return self.last_order

    def get_order_by_client_id(self, _client_id: str) -> object:
        assert self.last_order is not None
        return self.last_order

    def get_order_by_id(self, _order_id: str, *_args: object) -> object:
        assert self.last_order is not None
        return self.last_order


class FilledContractClient:
    def __init__(self) -> None:
        self.calls = 0

    def get_option_contracts(self, _request: object) -> OptionContractsResponse:
        self.calls += 1
        return OptionContractsResponse(
            option_contracts=[
                self._contract("TEST260918C00100000", Decimal("100")),
                self._contract("TEST260918C00105000", Decimal("105")),
            ],
            next_page_token=None,
        )

    @staticmethod
    def _contract(symbol: str, strike: Decimal) -> OptionContract:
        return OptionContract(
            id=f"contract-{symbol}",
            symbol=symbol,
            name="fixture",
            status=AssetStatus.ACTIVE,
            tradable=True,
            expiration_date=date(2026, 9, 18),
            root_symbol="TEST",
            underlying_symbol="TEST",
            underlying_asset_id=UUID(int=83),
            type=ContractType.CALL,
            style=ExerciseStyle.AMERICAN,
            strike_price=strike,
            size="100",
        )


class FilledSnapshotClient:
    def __init__(self) -> None:
        self.calls = 0

    def get_option_snapshot(self, request: object) -> dict[str, OptionsSnapshot]:
        self.calls += 1
        assert request.feed.value == "indicative"
        return {
            "TEST260918C00100000": self._snapshot("TEST260918C00100000", 0.55),
            "TEST260918C00105000": self._snapshot("TEST260918C00105000", 0.30),
        }

    @staticmethod
    def _snapshot(symbol: str, delta: float) -> OptionsSnapshot:
        return OptionsSnapshot(
            symbol,
            {
                "latestQuote": {
                    "t": sqlite_provider_clock(),
                    "bp": 1.0,
                    "ap": 1.2,
                    "bs": 2,
                    "as": 2,
                },
                "impliedVolatility": 0.4,
                "greeks": {
                    "delta": delta,
                    "gamma": 0.02,
                    "rho": 0.1,
                    "theta": -0.1,
                    "vega": 0.2,
                },
            },
        )


def test_replacement_quote_collector_preserves_requested_order_and_retrieval_time() -> None:
    symbols = ("TEST260918C00100000", "TEST260918C00105000")
    snapshots = FilledSnapshotClient()
    retrieved_at = sqlite_provider_clock() + timedelta(seconds=1)

    quotes = AlpacaReplacementQuoteCollector(
        snapshots,
        AlpacaOptionContractCollector(FilledContractClient()),
        clock=lambda: retrieved_at,
    ).collect(symbols)

    assert tuple(quote.symbol for quote in quotes) == symbols
    assert tuple(quote.underlying for quote in quotes) == ("TEST", "TEST")
    assert all(quote.retrieved_at == retrieved_at for quote in quotes)
    assert all(quote.bid_price == Decimal("1.0") for quote in quotes)
    assert snapshots.calls == 1


def test_replacement_quote_adjusted_symbol_stops_before_every_provider_call() -> None:
    calls: list[str] = []
    retrieved_at = sqlite_provider_clock() + timedelta(seconds=1)

    class SnapshotTrap:
        def get_option_snapshot(self, _request):
            calls.append("snapshot")
            raise AssertionError("snapshot provider must not be called")

    class ContractTrap:
        def contracts_for(self, _symbols):
            calls.append("contract")
            raise AssertionError("contract provider must not be called")

    collector = AlpacaReplacementQuoteCollector(
        SnapshotTrap(),
        ContractTrap(),
        clock=lambda: calls.append("clock") or retrieved_at,
    )

    with pytest.raises(ExecutionEvidenceError) as raised:
        collector.collect(("TEST1260918C00100000", "TEST1260918C00105000"))

    assert raised.value.code == "NON_STANDARD_CONTRACT_UNSUPPORTED"
    assert calls == []


class ComposedActivitySource:
    def __init__(self, baseline_at: datetime) -> None:
        self.baseline_at = baseline_at
        self.calls = 0

    def collect(
        self,
        *,
        since: datetime,
        until: datetime,
        provider_to_client: object,
        initial_funding: object,
        observed_account_fingerprint: str,
    ):
        del provider_to_client
        self.calls += 1
        assert since == self.baseline_at
        assert observed_account_fingerprint == FINGERPRINT
        funding = ActivityItem(
            activity_id_hash=initial_funding.activity_id_hash,
            activity_type=ActivityType.INITIAL_FUNDING,
            occurred_at=self.baseline_at,
            symbol=None,
            signed_quantity=Decimal("100000"),
        )
        return (
            (funding,),
            ActivityPaginationEvidence(
                requested_start=since,
                requested_end=until,
                retrieved_through=until,
                established_at=until,
                page_count=1,
                terminal_page_seen=True,
                visibility_complete_through=self.baseline_at,
                visibility_horizon=timedelta(hours=24),
            ),
        )

    def collect_lifecycle(self, **_kwargs: object):
        raise AssertionError("lifecycle collection is not used in this execution fixture")


class FilledComposedActivitySource(ComposedActivitySource):
    def collect(self, **kwargs):
        activities, pagination = super().collect(**kwargs)
        provider_to_client = kwargs["provider_to_client"]
        if not provider_to_client:
            return activities, pagination
        provider_order_id, client_order_id = next(iter(provider_to_client.items()))
        fills = (
            ActivityItem(
                activity_id_hash="c" * 64,
                activity_type=ActivityType.FILL,
                occurred_at=self.baseline_at + timedelta(milliseconds=1),
                symbol="TEST260918C00100000",
                signed_quantity=Decimal("1"),
                provider_order_id=provider_order_id,
                client_order_id=client_order_id,
            ),
            ActivityItem(
                activity_id_hash="d" * 64,
                activity_type=ActivityType.FILL,
                occurred_at=self.baseline_at + timedelta(milliseconds=1),
                symbol="TEST260918C00105000",
                signed_quantity=Decimal("-1"),
                provider_order_id=provider_order_id,
                client_order_id=client_order_id,
            ),
        )
        return tuple(
            sorted((*activities, *fills), key=lambda item: item.activity_id_hash)
        ), pagination


class ComposedModelTransport:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, _request: object) -> str:
        self.calls += 1
        return json.dumps(
            {
                "classifications": [
                    {
                        "cluster_id": "cluster-1",
                        "source_ids": ["source-1"],
                        "event_code": "GUIDANCE",
                        "relation": "CONTRADICTS",
                        "materiality": 3,
                        "relevance": 0.9,
                        "confidence": 0.8,
                        "invalidation_condition_id": "inv-guidance",
                    }
                ]
            }
        )


class ComposedMCP:
    def __init__(self) -> None:
        self.entered = False
        self.calls = 0

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.entered = False

    async def call(self, tool_name: str, arguments: object) -> object:
        assert self.entered is True
        self.calls += 1
        return {"tool_name": tool_name, "arguments": arguments}


class ComposedPersistence:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.sessions = sessions
        with sessions.begin() as session:
            session.add(
                ModelCallBudgetRow(
                    model="gemini-3.7-flash",
                    request_count=0,
                    hard_limit=50,
                )
            )
        self.repository = SQLAlchemyExecutionRepository(sessions, entry_limits=ENTRY_LIMITS)
        self.agent_repository = AgentDecisionRepository(
            sessions,
            server_autonomy_enabled=True,
        )
        self.evidence_ledger = SQLAlchemyEvidenceLedger(sessions)

    def close(self) -> None:
        self.engine.dispose()


def account(role: AccountRole = AccountRole.SUBMISSION) -> RuntimeAccountConfig:
    return RuntimeAccountConfig(
        role=role,
        endpoint=PAPER_TRADING_ENDPOINT,
        account_fingerprint=FINGERPRINT,
        baseline_status=DataQuality.COMPLETE,
    )


def composed_thesis() -> ThesisResponse:
    return ThesisResponse(
        thesis_id=UUID(int=71),
        version=1,
        frozen=True,
        thesis_hash="thesis-hash",
        thesis=ThesisCreateRequest(
            underlying="TEST",
            thesis_code="REVENUE_OUTLOOK_IMPROVING",
            invalidation_codes=("inv-guidance",),
            intended_exposure=GreekExposure(
                delta=Decimal("0"),
                gamma=Decimal("0"),
                theta_per_day=Decimal("0"),
                vega_per_iv_point=Decimal("0"),
            ),
            source_policy_hash="source-policy-hash",
        ),
    )


def composed_cluster() -> SourceCluster:
    return SourceCluster(
        cluster_id="cluster-1",
        source_ids=("source-1",),
        headline="Issuer narrowed its outlook.",
        observed_at=NOW,
        source_tier="PRIMARY",
    )


def composed_envelope() -> OrderEnvelope:
    return OrderEnvelope(
        action=ExecutionAction.ENTRY,
        authorization_certificate_id=UUID(int=72),
        policy_hash="d" * 64,
        account_fingerprint=FINGERPRINT,
        position_or_book_fingerprint="book-fingerprint",
        legs=(
            OrderLegIntent("TEST260918C00100000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("TEST260918C00105000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        quantity=1,
        minimum_limit=Decimal("1.00"),
        maximum_limit=Decimal("1.50"),
        approved_max_loss=Decimal("500"),
        event_key="TEST-2026-08-28",
        trading_day=date(2026, 8, 28),
    )


def initial_sweep(
    baseline_at: datetime,
    observed_at: datetime = NOW,
    role: AccountRole = AccountRole.SUBMISSION,
) -> SweepObservation:
    account_observation = AccountObservation(
        role=role,
        account_fingerprint=FINGERPRINT,
        paper=True,
        status="ACTIVE",
        account_blocked=False,
        trading_blocked=False,
        options_trading_blocked=False,
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        cash=Decimal("100000"),
        observed_at=observed_at,
        time_quality="RETRIEVAL_TIME_ONLY",
    )
    funding = ActivityItem(
        activity_id_hash="f" * 64,
        activity_type=ActivityType.INITIAL_FUNDING,
        occurred_at=baseline_at,
        symbol=None,
        signed_quantity=Decimal("100000"),
    )
    return SweepObservation(
        retrieval_started_at=observed_at,
        retrieval_completed_at=observed_at,
        activity_pagination=ActivityPaginationEvidence(
            requested_start=baseline_at,
            requested_end=observed_at,
            retrieved_through=observed_at,
            established_at=observed_at,
            page_count=1,
            terminal_page_seen=True,
            visibility_complete_through=baseline_at,
            visibility_horizon=timedelta(hours=24),
        ),
        first_account=account_observation,
        final_account=account_observation,
        first_positions=(),
        final_positions=(),
        first_open_orders=(),
        final_open_orders=(),
        activities=(funding,),
        positions_complete=True,
        orders_complete=True,
    )


def prepare_development_entry(
    persistence: ComposedPersistence,
    order: OrderEnvelope,
    intent_id: UUID,
    observed_at: datetime,
) -> datetime:
    baseline_at = observed_at - timedelta(days=2)
    repository = persistence.repository
    repository.register_account(
        role=AccountRole.DEVELOPMENT,
        fingerprint=FINGERPRINT,
        equity=Decimal("100000"),
        autonomous_enabled=False,
    )
    thesis_version_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    repository.add_thesis_version(
        FrozenThesisVersion(
            thesis_version_id=thesis_version_id,
            thesis_id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            account_role=AccountRole.DEVELOPMENT,
            version=1,
            thesis_hash="f" * 64,
            policy_hash=order.policy_hash,
            underlying="TEST",
            thesis_code="COMPOSITION_TEST",
            frozen_at=baseline_at,
            target_at=baseline_at + timedelta(days=7),
            intended_exposure={},
            exposure_limits={},
            volatility_view="NEUTRAL",
            entry_atm_iv=Decimal("0.4"),
            approved_max_loss=order.approved_max_loss,
            portfolio_risk_cap=order.approved_max_loss,
            invalidation_codes=("TEST_INVALIDATION",),
            thesis_payload={"fixture": "runtime_composition"},
            created_at=baseline_at,
        )
    )
    _seed_composed_development_reconciliation(persistence, baseline_at, observed_at)
    repository.set_autonomous_enabled(AccountRole.DEVELOPMENT, True, actor=Actor.OWNER)
    authorization = EntryApprovalAuthorization(
        approval_id=order.authorization_certificate_id,
        thesis_version_id=thesis_version_id,
        account_role=AccountRole.DEVELOPMENT,
        policy_hash=order.policy_hash,
        book_fingerprint=order.position_or_book_fingerprint,
        envelope_hash=order_envelope_hash(order),
        approved_max_loss=order.approved_max_loss,
        quantity=order.quantity,
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    boundary = baseline_at
    tick = persistence.agent_repository.reserve_tick(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=FINGERPRINT,
        actor="SCHEDULER",
        trusted_at=boundary,
        tick_key=f"composition:{intent_id}",
    )
    assert tick.reservation_token is not None
    persistence.agent_repository.record_decision(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=boundary,
        observed_at=boundary,
        normalized_input={"fixture": "runtime_composition"},
        outcome="ENTRY_APPROVED",
        reason_code="POLICY_APPROVED",
        policy_hash=order.policy_hash,
        result_payload={"fixture": "runtime_composition"},
        thesis_version_id=thesis_version_id,
        authorization=authorization,
        envelope=order,
        intent_id=intent_id,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )
    return baseline_at


def _seed_composed_development_reconciliation(
    persistence: ComposedPersistence,
    baseline_at: datetime,
    observed_at: datetime,
) -> None:
    sweep = initial_sweep(baseline_at, observed_at, AccountRole.DEVELOPMENT)
    state = AccountReconciliationState._from_repository_state(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=FINGERPRINT,
        baseline_captured_at=baseline_at,
        accepted_at=observed_at,
        expected_cash=Decimal("100000"),
        expected_positions=(),
        expected_open_orders=(),
        known_activities=sweep.activities,
        activity_complete_through=baseline_at,
    )
    baseline_id = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    funding = sweep.activities[0]
    with persistence.sessions.begin() as session:
        session.add(
            SubmissionBaselineRow(
                baseline_id=baseline_id,
                account_role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=FINGERPRINT,
                equity=Decimal("100000"),
                captured_at=baseline_at,
                positions_hash="1" * 64,
                orders_hash="2" * 64,
                activities_hash="3" * 64,
                contaminated=False,
            )
        )
        session.flush()
        session.add(
            AccountReconciliationStateRow(
                state_id=state.state_id,
                account_role=AccountRole.DEVELOPMENT.value,
                sequence=1,
                account_fingerprint=FINGERPRINT,
                baseline_id=baseline_id,
                baseline_captured_at=baseline_at,
                accepted_at=observed_at,
                expected_cash=Decimal("100000"),
                expected_positions=[],
                expected_open_orders=[],
                known_activities=[
                    {
                        "activity_id_hash": funding.activity_id_hash,
                        "activity_type": funding.activity_type.value,
                        "occurred_at": funding.occurred_at.isoformat(),
                        "symbol": funding.symbol,
                        "signed_quantity": str(funding.signed_quantity),
                        "provider_order_id": funding.provider_order_id,
                        "client_order_id": funding.client_order_id,
                        "time_quality": funding.time_quality,
                        "provider_activity_type": funding.provider_activity_type,
                    }
                ],
                activity_complete_through=baseline_at,
                resolved_activity_hashes=[],
                predecessor_state_id=None,
                authority_reconciliation_id=None,
                authority_permit_id=None,
                authority_observation_id=None,
                authority_permit_request_hash=None,
                transition_hash=None,
                state_hash=state.state_hash,
            )
        )


def sqlite_provider_clock() -> datetime:
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def bound(value: object, *, binding: ProviderBinding = BINDING):
    return BoundProviderResource(binding=binding, resource=RuntimeResource.borrowed(value))


def dependencies() -> tuple[RuntimeDependencies, dict[str, object]]:
    persistence = PersistenceGuard()
    guards: dict[str, object] = {
        "persistence": persistence,
        "trading": ObservedAccountTrading(ACCOUNT_ID),
        "contracts": CallGuard(),
        "snapshots": CallGuard(),
        "activities": CallGuard(),
        "model": CallGuard(),
        "mcp": MCPGuard(),
    }
    providers = RuntimeProviderBundle(
        binding=BINDING,
        trading=bound(guards["trading"]),
        activities=bound(guards["activities"]),
        option_contracts=bound(guards["contracts"]),
        option_snapshots=bound(guards["snapshots"]),
        stock_market_data=bound(CallGuard()),
    )
    return (
        RuntimeDependencies(
            persistence=RuntimeResource.owned(persistence, persistence.close),
            providers=providers,
            model_transport=RuntimeResource.borrowed(guards["model"]),
            mcp_research=RuntimeResource.borrowed(guards["mcp"]),
            clock=lambda: datetime(2026, 8, 28, 20, tzinfo=UTC),
        ),
        guards,
    )


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"role": AccountRole.REPLAY}, "EXECUTABLE_ACCOUNT_ROLE_REQUIRED"),
        ({"endpoint": "https://api.alpaca.markets"}, "PAPER_TRADING_REQUIRED"),
        ({"paper": False}, "PAPER_TRADING_REQUIRED"),
        ({"account_fingerprint": "not-a-fingerprint"}, "ACCOUNT_FINGERPRINT_INVALID"),
    ],
)
def test_account_configuration_fails_closed(values: dict[str, object], code: str) -> None:
    fields: dict[str, object] = {
        "role": AccountRole.SUBMISSION,
        "endpoint": PAPER_TRADING_ENDPOINT,
        "account_fingerprint": FINGERPRINT,
    }
    fields.update(values)
    with pytest.raises(RuntimeCompositionError, match=code):
        RuntimeAccountConfig(**fields)


def test_submission_and_development_roles_remain_explicit() -> None:
    assert account(AccountRole.SUBMISSION).role is AccountRole.SUBMISSION
    assert account(AccountRole.DEVELOPMENT).role is AccountRole.DEVELOPMENT


def test_runtime_configuration_represents_both_autonomy_states() -> None:
    assert account().autonomous_enabled is False
    assert replace(account(), autonomous_enabled=True).autonomous_enabled is True


def test_scheduler_server_autonomy_gate_precedes_provider_and_repository_authority() -> None:
    injected, guards = dependencies()
    runtime = build_runtime(account(), injected)

    with pytest.raises(ExecutionBlocked, match="SERVER_AUTONOMY_DISABLED"):
        runtime.execution.execute(uuid4(), Actor.SCHEDULER, datetime(2026, 8, 28, 20, tzinfo=UTC))

    assert_all_guards_unused(guards)


def test_owner_execution_remains_role_bound_when_server_autonomy_is_disabled() -> None:
    injected, guards = dependencies()
    runtime = build_runtime(account(), injected)

    with pytest.raises(ExecutionBlocked, match="INTENT_NOT_AUTHORIZED"):
        runtime.execution.execute(uuid4(), Actor.OWNER, datetime(2026, 8, 28, 20, tzinfo=UTC))

    persistence = guards["persistence"]
    assert isinstance(persistence, PersistenceGuard)
    assert persistence.repository.claim_bindings == [(AccountRole.SUBMISSION, FINGERPRINT)]


def test_scheduler_requires_persisted_autonomy_after_server_gate_and_account_proof() -> None:
    injected, guards = dependencies()
    persistence = guards["persistence"]
    assert isinstance(persistence, PersistenceGuard)
    persistence.repository = RepositoryGuard("AUTONOMOUS_DISABLED")
    runtime = build_runtime(replace(account(), autonomous_enabled=True), injected)

    with pytest.raises(ExecutionBlocked, match="AUTONOMOUS_DISABLED"):
        runtime.execution.execute(uuid4(), Actor.SCHEDULER, datetime(2026, 8, 28, 20, tzinfo=UTC))

    trading = guards["trading"]
    assert isinstance(trading, CallGuard)
    assert trading.calls == ["get_account"]
    assert persistence.repository.claim_bindings == [(AccountRole.SUBMISSION, FINGERPRINT)]
    assert all(
        not guard.calls
        for name, guard in guards.items()
        if name not in {"persistence", "trading"} and isinstance(guard, CallGuard)
    )


def test_live_provider_binding_is_rejected() -> None:
    with pytest.raises(RuntimeCompositionError, match="PAPER_TRADING_REQUIRED"):
        replace(BINDING, endpoint="https://api.alpaca.markets")


def test_binding_token_is_withheld_from_repr() -> None:
    assert BINDING.account_binding_token not in repr(BINDING)


def test_differently_bound_provider_component_is_rejected() -> None:
    other = replace(
        BINDING,
        account_binding_token="different-account-binding",
    )
    with pytest.raises(RuntimeCompositionError, match="PROVIDER_ACCOUNT_BINDING_MISMATCH"):
        RuntimeProviderBundle(
            binding=BINDING,
            trading=bound(CallGuard()),
            activities=bound(CallGuard(), binding=other),
            option_contracts=bound(CallGuard()),
            option_snapshots=bound(CallGuard()),
            stock_market_data=bound(CallGuard()),
        )


def test_equal_but_separately_substituted_binding_is_rejected() -> None:
    substituted = replace(BINDING)
    with pytest.raises(RuntimeCompositionError, match="PROVIDER_ACCOUNT_BINDING_MISMATCH"):
        RuntimeProviderBundle(
            binding=BINDING,
            trading=bound(CallGuard(), binding=substituted),
            activities=bound(CallGuard()),
            option_contracts=bound(CallGuard()),
            option_snapshots=bound(CallGuard()),
            stock_market_data=bound(CallGuard()),
        )


def test_account_and_provider_binding_must_match_before_any_call() -> None:
    injected, guards = dependencies()
    mismatched = replace(account(), account_fingerprint="b" * 64)

    with pytest.raises(RuntimeCompositionError, match="PROVIDER_ACCOUNT_BINDING_MISMATCH"):
        build_runtime(mismatched, injected)

    assert_all_guards_unused(guards)


def test_factory_uses_one_trading_client_without_construction_side_effects() -> None:
    injected, guards = dependencies()

    runtime = build_runtime(account(), injected)

    assert runtime.account.role is AccountRole.SUBMISSION
    assert not hasattr(runtime, "persistence")
    assert not hasattr(runtime.execution, "_service")
    assert not hasattr(runtime.execution, "_broker")
    assert all("service" not in name and "broker" not in name for name in vars(runtime.execution))
    assert_all_guards_unused(guards)


def test_composed_runtime_classifies_researches_sweeps_and_executes() -> None:
    observed_at = datetime.now(UTC)
    persistence = ComposedPersistence()
    order = composed_envelope()
    intent_id = UUID(int=73)
    baseline_at = prepare_development_entry(persistence, order, intent_id, observed_at)
    trading = ComposedTradingClient(ACCOUNT_ID)
    activities = ComposedActivitySource(baseline_at)
    model = ComposedModelTransport()
    mcp = ComposedMCP()
    providers = RuntimeProviderBundle(
        binding=BINDING,
        trading=bound(trading),
        activities=bound(activities),
        option_contracts=bound(CallGuard()),
        option_snapshots=bound(CallGuard()),
        stock_market_data=bound(CallGuard()),
    )
    runtime = build_runtime(
        replace(account(AccountRole.DEVELOPMENT), autonomous_enabled=True),
        RuntimeDependencies(
            persistence=RuntimeResource.owned(persistence, persistence.close),
            providers=providers,
            model_transport=RuntimeResource.borrowed(model),
            mcp_research=RuntimeResource.borrowed(mcp),
            clock=sqlite_provider_clock,
        ),
    )

    classifications = runtime.evidence_classifier.classify(
        composed_thesis(),
        (composed_cluster(),),
    )
    thesis_value = composed_thesis()
    context_classifications = runtime.evidence_classifier.classify_context(
        EvidenceClassificationContext(
            context_hash=thesis_value.thesis_hash,
            version=thesis_value.version,
            underlying=thesis_value.thesis.underlying,
            thesis_code=thesis_value.thesis.thesis_code,
            invalidation_condition_ids=thesis_value.thesis.invalidation_codes,
        ),
        (composed_cluster(),),
    )

    async def research() -> object:
        async with runtime.mcp_research:
            return await runtime.mcp_research.call("get_clock", {})

    research_result = asyncio.run(research())
    certificate = runtime.execution.execute(intent_id, Actor.SCHEDULER, observed_at)
    runtime.close()

    assert classifications[0].relation is EvidenceRelation.CONTRADICTS
    assert context_classifications == classifications
    assert model.calls == 2
    assert research_result == {"tool_name": "get_clock", "arguments": {}}
    assert mcp.calls == 1
    assert activities.calls == 2
    assert certificate.execution_status == "REJECTED"
    assert trading.calls.count("submit_order") == 1
    assert runtime.closed is True


def test_composed_runtime_joins_research_classification_greeks_and_filled_execution() -> None:
    observed_at = datetime.now(UTC)
    persistence = ComposedPersistence()
    order = composed_envelope()
    intent_id = UUID(int=74)
    baseline_at = prepare_development_entry(persistence, order, intent_id, observed_at)
    trading = FilledComposedTradingClient()
    activities = FilledComposedActivitySource(baseline_at)
    contracts = FilledContractClient()
    snapshots = FilledSnapshotClient()
    model = ComposedModelTransport()
    mcp = ComposedMCP()
    providers = RuntimeProviderBundle(
        binding=BINDING,
        trading=bound(trading),
        activities=bound(activities),
        option_contracts=bound(contracts),
        option_snapshots=bound(snapshots),
        stock_market_data=bound(CallGuard()),
    )
    runtime = build_runtime(
        replace(account(AccountRole.DEVELOPMENT), autonomous_enabled=True),
        RuntimeDependencies(
            persistence=RuntimeResource.owned(persistence, persistence.close),
            providers=providers,
            model_transport=RuntimeResource.borrowed(model),
            mcp_research=RuntimeResource.borrowed(mcp),
            clock=sqlite_provider_clock,
        ),
    )

    async def run_joined_flow():
        async with runtime.mcp_research:
            research = await runtime.mcp_research.call("get_clock", {})
        assert research["tool_name"] == "get_clock"
        classifications = runtime.evidence_classifier.classify(
            composed_thesis(),
            (composed_cluster(),),
        )
        certificate = runtime.execution.execute(intent_id, Actor.SCHEDULER, observed_at)
        await runtime.aclose()
        return classifications, certificate

    classifications, certificate = asyncio.run(run_joined_flow())

    assert classifications[0].relation is EvidenceRelation.CONTRADICTS
    assert certificate.execution_status == "FILLED"
    assert certificate.actual_exposure is not None
    assert certificate.actual_exposure.delta == Decimal("25.00")
    assert certificate.actual_exposure.gamma == Decimal("0.00")
    assert certificate.actual_exposure.theta_per_day == Decimal("0.00")
    assert certificate.actual_exposure.vega_per_iv_point == Decimal("0.00")
    assert trading.calls.count("get_account") >= 5
    assert trading.calls.count("submit_order") == 1
    assert activities.calls == 2
    assert contracts.calls == 1
    assert snapshots.calls == 1
    assert model.calls == 1
    assert mcp.calls == 1
    assert runtime.closed is True


def test_execution_claims_authority_before_touching_the_broker() -> None:
    injected, guards = dependencies()
    runtime = build_runtime(account(), injected)

    with pytest.raises(ExecutionBlocked, match="INTENT_NOT_AUTHORIZED"):
        runtime.execution.execute(uuid4(), Actor.OWNER, datetime(2026, 8, 28, 20, tzinfo=UTC))

    persistence = guards["persistence"]
    trading = guards["trading"]
    assert isinstance(persistence, PersistenceGuard)
    assert isinstance(trading, CallGuard)
    assert persistence.repository.calls == ["claim_intent"]
    assert persistence.repository.claim_bindings == [(AccountRole.SUBMISSION, FINGERPRINT)]
    assert trading.calls == ["get_account"]


@pytest.mark.parametrize("operation", ["get_account", "list_positions", "list_open_orders"])
def test_account_facing_reads_reject_observed_account_mismatch_before_authority_access(
    operation: str,
) -> None:
    injected, guards = dependencies()
    trading = ObservedAccountTrading(UUID(int=41))
    providers = replace(injected.providers, trading=bound(trading))
    runtime = build_runtime(account(), replace(injected, providers=providers))

    with pytest.raises(ProviderDataError, match="ACCOUNT_FINGERPRINT_MISMATCH"):
        getattr(runtime.account_view, operation)()

    persistence = guards["persistence"]
    assert isinstance(persistence, PersistenceGuard)
    assert persistence.repository.calls == []
    assert trading.calls == ["get_account"]


def test_account_facing_reads_use_one_matching_observed_account_client() -> None:
    injected, _ = dependencies()
    trading = ObservedAccountTrading(ACCOUNT_ID)
    providers = replace(injected.providers, trading=bound(trading))
    runtime = build_runtime(account(), replace(injected, providers=providers))

    assert runtime.account_view.get_account().role is AccountRole.SUBMISSION
    assert runtime.account_view.list_positions().positions == ()
    assert runtime.account_view.list_open_orders() == ()
    assert trading.calls == [
        "get_account",
        "get_account",
        "get_all_positions",
        "get_account",
        "get_orders",
    ]


@pytest.mark.parametrize(
    "operation",
    [
        "account_sweep",
        "classifier",
        "mcp_entry",
        "execution",
    ],
)
def test_observed_account_mismatch_blocks_every_runtime_authority(
    operation: str,
) -> None:
    injected, guards = dependencies()
    trading = ObservedAccountTrading(UUID(int=41))
    providers = replace(injected.providers, trading=bound(trading))
    runtime = build_runtime(account(), replace(injected, providers=providers))

    with pytest.raises(ProviderDataError, match="ACCOUNT_FINGERPRINT_MISMATCH"):
        if operation == "account_sweep":
            runtime.account_sweep.collect(None)
        elif operation == "classifier":
            _ = runtime.evidence_classifier.model_calls
        elif operation == "mcp_entry":
            asyncio.run(runtime.mcp_research.__aenter__())
        else:
            runtime.execution.execute(uuid4(), Actor.OWNER, datetime(2026, 8, 28, 20, tzinfo=UTC))

    persistence = guards["persistence"]
    assert isinstance(persistence, PersistenceGuard)
    assert persistence.repository.calls == []
    assert trading.calls == ["get_account"]
    assert_all_call_guards_unused(
        {name: guard for name, guard in guards.items() if name not in {"persistence", "trading"}}
    )


def test_mcp_call_reobserves_account_after_successful_session_entry() -> None:
    injected, _ = dependencies()
    trading = ObservedAccountTrading(ACCOUNT_ID)
    mcp = ComposedMCP()
    providers = replace(injected.providers, trading=bound(trading))
    runtime = build_runtime(
        account(),
        replace(
            injected,
            providers=providers,
            mcp_research=RuntimeResource.borrowed(mcp),
        ),
    )

    async def exercise() -> None:
        async with runtime.mcp_research:
            trading.account_id = UUID(int=41)
            with pytest.raises(ProviderDataError, match="ACCOUNT_FINGERPRINT_MISMATCH"):
                await runtime.mcp_research.call("get_clock", {})

    asyncio.run(exercise())

    assert trading.calls == ["get_account", "get_account"]
    assert mcp.calls == 0


def test_close_blocks_every_sync_operational_facade() -> None:
    injected, guards = dependencies()
    runtime = build_runtime(account(), injected)
    runtime.close()

    operations = (
        runtime.account_view.get_account,
        runtime.account_view.list_positions,
        runtime.account_view.list_open_orders,
        lambda: runtime.account_sweep.collect(None),
        lambda: runtime.evidence_classifier.model_calls,
        lambda: runtime.evidence_classifier.classify(None, ()),
        lambda: runtime.execution.execute(
            uuid4(), Actor.OWNER, datetime(2026, 8, 28, 20, tzinfo=UTC)
        ),
    )
    for operation in operations:
        with pytest.raises(RuntimeCompositionError, match="RUNTIME_CLOSED"):
            operation()

    assert_all_call_guards_unused(guards)


def test_close_blocks_mcp_facade() -> None:
    injected, guards = dependencies()
    runtime = build_runtime(account(), injected)
    runtime.close()

    with pytest.raises(RuntimeCompositionError, match="RUNTIME_CLOSED"):
        asyncio.run(runtime.mcp_research.call("tool", {}))

    mcp = guards["mcp"]
    assert isinstance(mcp, MCPGuard)
    assert mcp.calls == []


def test_close_is_idempotent_and_context_manager_closes_owned_resource() -> None:
    injected, guards = dependencies()

    with build_runtime(account(), injected) as runtime:
        assert runtime.closed is False
    runtime.close()

    persistence = guards["persistence"]
    assert isinstance(persistence, PersistenceGuard)
    assert runtime.closed is True
    assert persistence.close_calls == 1


def test_concurrent_sync_shutdown_runs_owned_cleanup_once() -> None:
    injected, guards = dependencies()
    persistence = guards["persistence"]
    assert isinstance(persistence, PersistenceGuard)
    close_calls = 0

    def close_persistence() -> None:
        nonlocal close_calls
        close_calls += 1

    runtime = build_runtime(
        account(),
        replace(
            injected,
            persistence=RuntimeResource.owned(persistence, close_persistence),
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _index: runtime.close(), range(2)))

    assert results == (None, None)
    assert runtime.closed is True
    assert close_calls == 1


def test_sync_classification_finishes_before_owned_resources_close() -> None:
    started = Event()
    release = Event()
    model_closed = Event()
    close_called = Event()

    class BlockingModel(ComposedModelTransport):
        def generate(self, request: object) -> str:
            started.set()
            assert release.wait(timeout=2)
            return super().generate(request)

    persistence = ComposedPersistence()
    model = BlockingModel()
    trading = ComposedTradingClient(ACCOUNT_ID)
    runtime = build_runtime(
        account(),
        RuntimeDependencies(
            persistence=RuntimeResource.owned(persistence, persistence.close),
            providers=RuntimeProviderBundle(
                binding=BINDING,
                trading=bound(trading),
                activities=bound(CallGuard()),
                option_contracts=bound(CallGuard()),
                option_snapshots=bound(CallGuard()),
                stock_market_data=bound(CallGuard()),
            ),
            model_transport=RuntimeResource.owned(model, model_closed.set),
            mcp_research=RuntimeResource.borrowed(ComposedMCP()),
            clock=sqlite_provider_clock,
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        classification = pool.submit(
            runtime.evidence_classifier.classify,
            composed_thesis(),
            (composed_cluster(),),
        )
        assert started.wait(timeout=2)

        def close_runtime() -> None:
            close_called.set()
            runtime.close()

        closing = pool.submit(close_runtime)
        assert close_called.wait(timeout=2)
        deadline = time.monotonic() + 2
        while True:
            try:
                _ = runtime.evidence_classifier.model_calls
            except RuntimeCompositionError as error:
                assert str(error) == "RUNTIME_CLOSED"
                break
            assert time.monotonic() < deadline
            time.sleep(0.001)
        assert not model_closed.wait(timeout=0.05)
        release.set()
        assert classification.result(timeout=2)[0].relation is EvidenceRelation.CONTRADICTS
        closing.result(timeout=2)

    assert model_closed.is_set()


def test_broker_dispatch_finishes_before_owned_provider_closes() -> None:
    dispatch_started = Event()
    dispatch_release = Event()
    trading_closed = Event()

    class BlockingTrading(ComposedTradingClient):
        def submit_order(self, order_data: object) -> object:
            dispatch_started.set()
            assert dispatch_release.wait(timeout=2)
            return super().submit_order(order_data)

    observed_at = datetime.now(UTC)
    persistence = ComposedPersistence()
    order = composed_envelope()
    intent_id = UUID(int=75)
    baseline_at = prepare_development_entry(persistence, order, intent_id, observed_at)
    trading = BlockingTrading(ACCOUNT_ID)
    runtime = build_runtime(
        replace(account(AccountRole.DEVELOPMENT), autonomous_enabled=True),
        RuntimeDependencies(
            persistence=RuntimeResource.owned(persistence, persistence.close),
            providers=RuntimeProviderBundle(
                binding=BINDING,
                trading=BoundProviderResource(
                    BINDING,
                    RuntimeResource.owned(trading, trading_closed.set),
                ),
                activities=bound(ComposedActivitySource(baseline_at)),
                option_contracts=bound(CallGuard()),
                option_snapshots=bound(CallGuard()),
                stock_market_data=bound(CallGuard()),
            ),
            model_transport=RuntimeResource.borrowed(ComposedModelTransport()),
            mcp_research=RuntimeResource.borrowed(ComposedMCP()),
            clock=sqlite_provider_clock,
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        execution = pool.submit(
            runtime.execution.execute,
            intent_id,
            Actor.SCHEDULER,
            observed_at,
        )
        assert dispatch_started.wait(timeout=2)
        closing = pool.submit(runtime.close)
        assert not trading_closed.wait(timeout=0.05)
        dispatch_release.set()
        assert execution.result(timeout=2).execution_status == "REJECTED"
        closing.result(timeout=2)

    assert trading_closed.is_set()


def test_cleanup_failure_is_retryable_and_runtime_stays_gated() -> None:
    injected, guards = dependencies()
    persistence = guards["persistence"]
    assert isinstance(persistence, PersistenceGuard)
    attempts = 0

    def flaky_close() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("fixture cleanup failure")

    runtime = build_runtime(
        account(),
        replace(injected, persistence=RuntimeResource.owned(persistence, flaky_close)),
    )

    with pytest.raises(RuntimeCompositionError, match="RUNTIME_CLEANUP_FAILED"):
        runtime.close()
    assert runtime.closed is False
    with pytest.raises(RuntimeCompositionError, match="RUNTIME_CLOSED"):
        runtime.account_view.get_account()

    runtime.close()
    assert runtime.closed is True
    assert attempts == 2


def test_owned_cleanup_is_reverse_ordered_and_borrowed_resources_are_ignored() -> None:
    injected, guards = dependencies()
    order: list[str] = []
    persistence = guards["persistence"]
    trading = guards["trading"]
    model = guards["model"]
    assert isinstance(persistence, PersistenceGuard)
    providers = replace(
        injected.providers,
        trading=BoundProviderResource(
            BINDING,
            RuntimeResource.owned(trading, lambda: order.append("trading")),
        ),
    )
    runtime = build_runtime(
        account(),
        replace(
            injected,
            persistence=RuntimeResource.owned(persistence, lambda: order.append("persistence")),
            providers=providers,
            model_transport=RuntimeResource.owned(model, lambda: order.append("model")),
        ),
    )

    runtime.close()

    assert order == ["model", "trading", "persistence"]


def test_distinct_resource_cleanup_retry_preserves_reverse_order() -> None:
    injected, guards = dependencies()
    order: list[str] = []
    persistence = guards["persistence"]
    trading = guards["trading"]
    model = guards["model"]
    attempts = 0
    assert isinstance(persistence, PersistenceGuard)

    def close_trading() -> None:
        nonlocal attempts
        attempts += 1
        order.append("trading")
        if attempts == 1:
            raise RuntimeError("fixture cleanup failure")

    providers = replace(
        injected.providers,
        trading=BoundProviderResource(BINDING, RuntimeResource.owned(trading, close_trading)),
    )
    runtime = build_runtime(
        account(),
        replace(
            injected,
            persistence=RuntimeResource.owned(persistence, lambda: order.append("persistence")),
            providers=providers,
            model_transport=RuntimeResource.owned(model, lambda: order.append("model")),
        ),
    )

    with pytest.raises(RuntimeCompositionError, match="RUNTIME_CLEANUP_FAILED"):
        runtime.close()
    runtime.close()

    assert runtime.closed is True
    assert order == ["model", "trading", "trading", "persistence"]


def test_shared_owned_provider_resource_is_closed_once() -> None:
    injected, guards = dependencies()
    client = guards["trading"]
    close_calls = 0

    def close_client() -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls > 1:
            raise RuntimeError("provider client closed more than once")

    shared = RuntimeResource.owned(client, close_client)
    providers = replace(
        injected.providers,
        trading=BoundProviderResource(BINDING, shared),
        option_contracts=BoundProviderResource(BINDING, shared),
    )
    runtime = build_runtime(account(), replace(injected, providers=providers))

    runtime.close()

    assert runtime.closed is True
    assert close_calls == 1


def test_distinct_owners_of_one_provider_value_are_rejected_before_access() -> None:
    injected, guards = dependencies()
    client = guards["trading"]
    close_calls: list[str] = []
    providers = replace(
        injected.providers,
        trading=BoundProviderResource(
            BINDING,
            RuntimeResource.owned(client, lambda: close_calls.append("trading")),
        ),
        option_contracts=BoundProviderResource(
            BINDING,
            RuntimeResource.owned(client, lambda: close_calls.append("contracts")),
        ),
    )

    with pytest.raises(RuntimeCompositionError, match="RUNTIME_RESOURCE_OWNERSHIP_AMBIGUOUS"):
        build_runtime(account(), replace(injected, providers=providers))

    assert close_calls == []
    assert_all_guards_unused(guards)


def test_shared_async_owned_provider_resource_is_closed_once() -> None:
    injected, guards = dependencies()
    client = guards["trading"]
    close_calls = 0

    async def close_client() -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls > 1:
            raise RuntimeError("provider client closed more than once")

    shared = RuntimeResource.async_owned(client, close_client)
    providers = replace(
        injected.providers,
        trading=BoundProviderResource(BINDING, shared),
        option_contracts=BoundProviderResource(BINDING, shared),
    )
    runtime = build_runtime(account(), replace(injected, providers=providers))

    asyncio.run(runtime.aclose())

    assert runtime.closed is True
    assert close_calls == 1


def test_distinct_async_owners_of_one_provider_value_are_rejected_before_access() -> None:
    injected, guards = dependencies()
    client = guards["trading"]
    close_calls: list[str] = []

    async def close_trading() -> None:
        close_calls.append("trading")

    async def close_contracts() -> None:
        close_calls.append("contracts")

    providers = replace(
        injected.providers,
        trading=BoundProviderResource(
            BINDING,
            RuntimeResource.async_owned(client, close_trading),
        ),
        option_contracts=BoundProviderResource(
            BINDING,
            RuntimeResource.async_owned(client, close_contracts),
        ),
    )

    with pytest.raises(RuntimeCompositionError, match="RUNTIME_RESOURCE_OWNERSHIP_AMBIGUOUS"):
        build_runtime(account(), replace(injected, providers=providers))

    assert close_calls == []
    assert_all_guards_unused(guards)


def test_async_owned_cleanup_requires_aclose_and_is_idempotent() -> None:
    injected, guards = dependencies()
    mcp = guards["mcp"]
    attempts = 0

    async def close_mcp() -> None:
        nonlocal attempts
        attempts += 1

    runtime = build_runtime(
        account(),
        replace(
            injected,
            mcp_research=RuntimeResource.async_owned(mcp, close_mcp),
        ),
    )

    with pytest.raises(RuntimeCompositionError, match="ASYNC_RUNTIME_CLEANUP_REQUIRED"):
        runtime.close()
    assert runtime.closed is False
    asyncio.run(runtime.aclose())
    asyncio.run(runtime.aclose())
    assert runtime.closed is True
    assert attempts == 1


def test_concurrent_async_shutdown_runs_owned_cleanup_once() -> None:
    injected, guards = dependencies()
    mcp = guards["mcp"]
    started = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def close_mcp() -> None:
        nonlocal attempts
        attempts += 1
        started.set()
        await release.wait()

    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.async_owned(mcp, close_mcp)),
    )

    async def exercise() -> None:
        first = asyncio.create_task(runtime.aclose())
        await started.wait()
        second = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(exercise())

    assert runtime.closed is True
    assert attempts == 1


def test_canceled_async_shutdown_retains_the_same_cleanup() -> None:
    injected, guards = dependencies()
    mcp = guards["mcp"]
    started = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def close_mcp() -> None:
        nonlocal attempts
        attempts += 1
        started.set()
        await release.wait()

    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.async_owned(mcp, close_mcp)),
    )

    async def exercise() -> None:
        first = asyncio.create_task(runtime.aclose())
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        await runtime.aclose()

    asyncio.run(exercise())

    assert runtime.closed is True
    assert attempts == 1


def test_failed_async_cleanup_retries_only_the_incomplete_step() -> None:
    injected, guards = dependencies()
    mcp = guards["mcp"]
    attempts = 0

    async def close_mcp() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("fixture cleanup failure")

    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.async_owned(mcp, close_mcp)),
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeCompositionError, match="RUNTIME_CLEANUP_FAILED"):
            await runtime.aclose()
        with pytest.raises(RuntimeCompositionError, match="RUNTIME_CLOSED"):
            runtime.account_view.get_account()
        await runtime.aclose()

    asyncio.run(exercise())

    assert runtime.closed is True
    assert attempts == 2


def test_internal_cleanup_cancellation_is_retryable() -> None:
    injected, guards = dependencies()
    mcp = guards["mcp"]
    attempts = 0

    async def close_mcp() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError

    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.async_owned(mcp, close_mcp)),
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeCompositionError, match="RUNTIME_CLEANUP_FAILED"):
            await runtime.aclose()
        await runtime.aclose()

    asyncio.run(exercise())

    assert runtime.closed is True
    assert attempts == 2


def test_internal_mcp_cleanup_cancellation_is_retryable() -> None:
    injected, guards = dependencies()
    mcp = guards["mcp"]
    attempts = 0

    async def close_mcp() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError

    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.async_owned(mcp, close_mcp)),
    )

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await runtime.mcp_research.aclose()
        await runtime.mcp_research.aclose()

    asyncio.run(exercise())

    assert attempts == 2


def test_nested_mcp_context_and_runtime_shutdown_share_one_lifecycle_owner() -> None:
    injected, _ = dependencies()

    class LifecycleMCP:
        def __init__(self) -> None:
            self.enter_calls = 0
            self.exit_calls = 0

        async def __aenter__(self):
            self.enter_calls += 1
            return self

        async def __aexit__(self, *_args):
            self.exit_calls += 1

        async def call(self, *_args, **_kwargs):
            return {"quality": "COMPLETE"}

    mcp = LifecycleMCP()

    async def close_mcp() -> None:
        await mcp.__aexit__(None, None, None)

    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.async_owned(mcp, close_mcp)),
    )

    async def exercise() -> None:
        async with runtime, runtime.mcp_research:
            assert await runtime.mcp_research.call("get_clock", {}) == {"quality": "COMPLETE"}

    asyncio.run(exercise())

    assert runtime.closed is True
    assert mcp.enter_calls == 1
    assert mcp.exit_calls == 1


def test_mcp_facade_is_single_use_and_never_reenters_the_target() -> None:
    injected, _ = dependencies()
    mcp = ComposedMCP()
    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.borrowed(mcp)),
    )

    async def exercise() -> None:
        async with runtime.mcp_research:
            pass
        with pytest.raises(RuntimeCompositionError, match="MCP_SESSION_CLOSED"):
            await runtime.mcp_research.__aenter__()

    asyncio.run(exercise())

    assert mcp.entered is False
    assert mcp.calls == 0


def test_mcp_close_waits_for_enter_and_exits_the_successful_session_once() -> None:
    injected, _ = dependencies()
    enter_started = asyncio.Event()
    enter_release = asyncio.Event()

    class SlowEnterMCP(ComposedMCP):
        def __init__(self) -> None:
            super().__init__()
            self.enter_calls = 0
            self.exit_calls = 0

        async def __aenter__(self):
            self.enter_calls += 1
            enter_started.set()
            await enter_release.wait()
            self.entered = True
            return self

        async def __aexit__(self, *_args: object) -> None:
            self.exit_calls += 1
            self.entered = False

    mcp = SlowEnterMCP()
    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.borrowed(mcp)),
    )

    async def exercise() -> None:
        entering = asyncio.create_task(runtime.mcp_research.__aenter__())
        await enter_started.wait()
        closing = asyncio.create_task(runtime.mcp_research.aclose())
        await asyncio.sleep(0)
        enter_release.set()
        await entering
        await closing

    asyncio.run(exercise())

    assert mcp.enter_calls == 1
    assert mcp.exit_calls == 1
    assert mcp.entered is False


def test_mcp_close_waits_for_an_in_flight_call_before_exit() -> None:
    injected, _ = dependencies()
    call_started = asyncio.Event()
    call_release = asyncio.Event()
    events: list[str] = []

    class SlowCallMCP(ComposedMCP):
        async def call(self, *_args: object, **_kwargs: object) -> object:
            events.append("call-start")
            call_started.set()
            await call_release.wait()
            events.append("call-end")
            return {}

        async def __aexit__(self, *_args: object) -> None:
            events.append("exit")
            await super().__aexit__(*_args)

    mcp = SlowCallMCP()
    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.borrowed(mcp)),
    )

    async def exercise() -> None:
        await runtime.mcp_research.__aenter__()
        call = asyncio.create_task(runtime.mcp_research.call("get_clock", {}))
        await call_started.wait()
        closing = asyncio.create_task(runtime.mcp_research.aclose())
        await asyncio.sleep(0)
        call_release.set()
        await call
        await closing

    asyncio.run(exercise())

    assert events == ["call-start", "call-end", "exit"]


def test_mcp_failed_exit_is_retryable_without_reentering() -> None:
    injected, _ = dependencies()

    class FlakyExitMCP(ComposedMCP):
        def __init__(self) -> None:
            super().__init__()
            self.enter_calls = 0
            self.exit_calls = 0

        async def __aenter__(self):
            self.enter_calls += 1
            return await super().__aenter__()

        async def __aexit__(self, *_args: object) -> None:
            self.exit_calls += 1
            if self.exit_calls == 1:
                raise RuntimeError("partial external exit")
            await super().__aexit__(*_args)

    mcp = FlakyExitMCP()
    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.borrowed(mcp)),
    )

    async def exercise() -> None:
        await runtime.mcp_research.__aenter__()
        with pytest.raises(RuntimeError, match="partial external exit"):
            await runtime.mcp_research.aclose()
        await runtime.mcp_research.aclose()
        with pytest.raises(RuntimeCompositionError, match="MCP_SESSION_CLOSED"):
            await runtime.mcp_research.__aenter__()

    asyncio.run(exercise())

    assert mcp.enter_calls == 1
    assert mcp.exit_calls == 2


def test_runtime_cleanup_retries_a_failed_mcp_initialization_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", f"{Path(sys.prefix) / 'bin'}:{os.environ['PATH']}")
    injected, _ = dependencies()

    class InvalidSurfaceMCP:
        def __init__(self) -> None:
            self.enter_calls = 0
            self.exit_calls = 0
            self.open = False

        async def __aenter__(self) -> InvalidSurfaceMCP:
            self.enter_calls += 1
            self.open = True
            return self

        async def __aexit__(self, *_args: object) -> None:
            self.exit_calls += 1
            if self.exit_calls == 1:
                raise RuntimeError("transient initialization cleanup failure")
            self.open = False

        async def list_tools(self, *, max_pages: int) -> list[object]:
            assert max_pages == 1
            return []

        async def call_tool_mcp(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("invalid MCP surface accepted a tool call")

    connected = InvalidSurfaceMCP()
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        client_factory=lambda _spec, _limits: connected,
    )

    async def close_client() -> None:
        await client.__aexit__(None, None, None)

    runtime = build_runtime(
        account(),
        replace(
            injected,
            mcp_research=RuntimeResource.async_owned(
                client,
                close_client,
            ),
        ),
    )

    async def exercise() -> None:
        with pytest.raises(MCPBoundaryError, match="MCP_TOOL_SURFACE_MISMATCH"):
            async with runtime, runtime.mcp_research:
                raise AssertionError("invalid MCP surface entered the runtime")

    asyncio.run(exercise())

    assert runtime.closed is True
    assert connected.enter_calls == 1
    assert connected.exit_calls == 2
    assert connected.open is False


def test_runtime_cleanup_keeps_a_canceled_mcp_retry_for_other_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", f"{Path(sys.prefix) / 'bin'}:{os.environ['PATH']}")
    injected, _ = dependencies()

    class InvalidSurfaceMCP:
        def __init__(self) -> None:
            self.enter_calls = 0
            self.exit_calls = 0
            self.open = False
            self.retry_started = asyncio.Event()
            self.retry_release = asyncio.Event()

        async def __aenter__(self) -> InvalidSurfaceMCP:
            self.enter_calls += 1
            self.open = True
            return self

        async def __aexit__(self, *_args: object) -> None:
            self.exit_calls += 1
            if self.exit_calls == 1:
                raise asyncio.CancelledError
            self.retry_started.set()
            await self.retry_release.wait()
            self.open = False

        async def list_tools(self, *, max_pages: int) -> list[object]:
            assert max_pages == 1
            return []

        async def call_tool_mcp(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("invalid MCP surface accepted a tool call")

    connected = InvalidSurfaceMCP()
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        client_factory=lambda _spec, _limits: connected,
    )

    async def close_client() -> None:
        await client.__aexit__(None, None, None)

    runtime = build_runtime(
        account(),
        replace(
            injected,
            mcp_research=RuntimeResource.async_owned(client, close_client),
        ),
    )

    async def exercise() -> None:
        with pytest.raises(MCPBoundaryError, match="MCP_TOOL_SURFACE_MISMATCH"):
            await runtime.mcp_research.__aenter__()
        with pytest.raises(RuntimeCompositionError, match="MCP_SESSION_ALREADY_ENTERED"):
            await runtime.mcp_research.__aenter__()
        first = asyncio.create_task(runtime.aclose())
        await connected.retry_started.wait()
        canceled_waiter = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        canceled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await canceled_waiter
        assert runtime.closed is False
        connected.retry_release.set()
        await first
        await runtime.aclose()

    asyncio.run(exercise())

    assert runtime.closed is True
    assert connected.enter_calls == 1
    assert connected.exit_calls == 2
    assert connected.open is False


def test_sync_close_rejects_an_active_async_cleanup_without_double_closing() -> None:
    injected, guards = dependencies()
    mcp = guards["mcp"]
    started = asyncio.Event()
    release = asyncio.Event()
    close_calls = 0

    async def close_mcp() -> None:
        nonlocal close_calls
        close_calls += 1
        started.set()
        await release.wait()

    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.async_owned(mcp, close_mcp)),
    )

    async def exercise() -> None:
        closing = asyncio.create_task(runtime.aclose())
        await started.wait()
        with pytest.raises(RuntimeCompositionError, match="RUNTIME_CLEANUP_IN_PROGRESS"):
            runtime.close()
        release.set()
        await closing

    asyncio.run(exercise())

    assert runtime.closed is True
    assert close_calls == 1


def test_sync_context_rejects_async_cleanup_before_entering_body() -> None:
    injected, guards = dependencies()
    mcp = guards["mcp"]
    body_entered = False

    async def close_mcp() -> None:
        return None

    runtime = build_runtime(
        account(),
        replace(injected, mcp_research=RuntimeResource.async_owned(mcp, close_mcp)),
    )

    with pytest.raises(RuntimeCompositionError, match="ASYNC_RUNTIME_CONTEXT_REQUIRED"), runtime:
        body_entered = True

    assert body_entered is False
    assert runtime.closed is False
    asyncio.run(runtime.aclose())
    assert runtime.closed is True


def test_async_write_method_cannot_satisfy_sync_provider_port() -> None:
    injected, guards = dependencies()

    class AsyncTrading(CallGuard):
        async def submit_order(self, *_args, **_kwargs):
            return None

    providers = replace(injected.providers, trading=bound(AsyncTrading()))

    with pytest.raises(
        RuntimeCompositionError, match="RUNTIME_SYNC_DEPENDENCY_INVALID:submit_order"
    ):
        build_runtime(account(), replace(injected, providers=providers))

    assert_all_guards_unused(guards)


def test_sync_object_cannot_satisfy_async_mcp_port() -> None:
    injected, _ = dependencies()
    sync_mcp = SimpleNamespace(
        __aenter__=lambda: None,
        __aexit__=lambda *_args: None,
        call=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        RuntimeCompositionError, match="RUNTIME_ASYNC_DEPENDENCY_INVALID:__aenter__"
    ):
        build_runtime(
            account(),
            replace(injected, mcp_research=RuntimeResource.borrowed(sync_mcp)),
        )


def test_import_does_not_launch_a_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("runtime import launched a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    importlib.reload(composition_module)


def assert_all_guards_unused(guards: dict[str, object]) -> None:
    assert_all_call_guards_unused(guards)
    persistence = guards["persistence"]
    assert isinstance(persistence, PersistenceGuard)
    assert persistence.close_calls == 0


def assert_all_call_guards_unused(guards: dict[str, object]) -> None:
    for guard in guards.values():
        if isinstance(guard, CallGuard):
            assert guard.calls == []
