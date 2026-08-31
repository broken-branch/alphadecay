from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace as dataclass_replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from alpaca.trading.models import Calendar
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.alpaca.execution_evidence import baseline_account_fingerprint
from backend.app.config import RuntimeRole, Settings
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import Actor, ExecutionBlocked
from backend.app.lifecycle.repository import SQLAlchemyLifecycleRepository
from backend.app.lifecycle.terminal_materialization import (
    SQLAlchemyLifecycleTerminalMaterializer,
)
from backend.app.persistence import AgentDecisionRepository
from backend.app.persistence.runtime import RuntimeDatabaseClock, RuntimePersistence
from backend.app.persistence.sqlalchemy_models import Base
from backend.app.persistence.sqlalchemy_repository import (
    SQLAlchemyExecutionRepository,
    SQLAlchemyTrustedDatabaseClock,
)
from backend.app.provider_settings import ProviderSettingsRepositoryError
from backend.app.runtime import (
    BoundProviderResource,
    ProductionResources,
    ProviderBinding,
    RuntimeCompositionError,
    RuntimeProviderBundle,
    RuntimeResource,
    SettingsCalibrationBinding,
    build_production_agent,
)
from backend.app.runtime.production import (
    ProductionOpportunityWiring,
    _halt_config_for_plan,
    _validate_plan_account_authority,
)
from backend.app.services import (
    AcquisitionFailure,
    AcquisitionKind,
    DevelopmentAcquisitionRouter,
    DevelopmentRoute,
    DevelopmentRouteAuthority,
    ObservedPaperAccountAuthority,
)

ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000123")
FINGERPRINT = baseline_account_fingerprint(ACCOUNT_ID)


def settings(
    role: RuntimeRole,
    *,
    server_autonomy: bool = False,
    policy_hash: str = "a" * 64,
    calibration_hash: str = "b" * 64,
    opportunity: bool = False,
) -> Settings:
    configured = Settings(
        app_account_role=role,
        app_autonomous_enabled=server_autonomy,
        app_policy_hash=SecretStr(policy_hash),
        app_calibration_hash=SecretStr(calibration_hash),
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
    if not opportunity:
        return configured
    return configured.model_copy(
        update={
            "app_opportunity_key": SecretStr("ACME_EVENT"),
            "app_opportunity_plan_version": SecretStr("3"),
            "app_halt_maximum_trade_age_seconds": SecretStr("15"),
        }
    )


def persistence(*, server_autonomy: bool = False) -> RuntimePersistence:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    trusted = SQLAlchemyTrustedDatabaseClock()
    return RuntimePersistence(
        engine=engine,
        sessions=sessions,
        repository=SQLAlchemyExecutionRepository(sessions, trusted_clock=trusted),
        agent_repository=AgentDecisionRepository(
            sessions,
            database_clock=trusted,
            server_autonomy_enabled=server_autonomy,
        ),
        database_clock=RuntimeDatabaseClock(sessions, trusted),
        performance_repository=object(),
        evidence_ledger=object(),
        lifecycle_repository=SQLAlchemyLifecycleRepository(sessions),
    )


class Trading:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.account_id = ACCOUNT_ID
        self.status = "ACTIVE"
        self.account_blocked = False

    def get_account(self) -> object:
        return SimpleNamespace(
            id=self.account_id,
            status=self.status,
            equity=Decimal("100000"),
            buying_power=Decimal("100000"),
            account_blocked=self.account_blocked,
            trading_blocked=False,
            transfers_blocked=False,
            trade_suspended_by_user=False,
        )

    def close(self) -> None:
        self.log.append("trading")


class Closeable:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log

    def close(self) -> None:
        self.log.append(self.name)


class MCP:
    def __init__(self, log: list[str], *, fail_close: bool = False) -> None:
        self.log = log
        self.entered = 0
        self.fail_close = fail_close

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.log.append("mcp")
        if self.fail_close:
            raise RuntimeError("mcp cleanup failed")

    async def call(self, *_args: object) -> object:
        raise AssertionError("MCP must remain unavailable")


def resources(
    log: list[str], *, fail_mcp_close: bool = False
) -> tuple[ProductionResources, Trading, MCP]:
    trading = Trading(log)
    activity = Closeable("activity", log)
    option = Closeable("option", log)
    model = Closeable("model", log)
    mcp = MCP(log, fail_close=fail_mcp_close)

    async def close_mcp() -> None:
        await mcp.__aexit__(None, None, None)

    binding = ProviderBinding(
        endpoint="https://paper-api.alpaca.markets",
        account_fingerprint=FINGERPRINT,
        account_binding_token="binding",
    )
    trading_resource = RuntimeResource.owned(trading, trading.close)
    option_resource = RuntimeResource.owned(option, option.close)
    return (
        ProductionResources(
            account_fingerprint=FINGERPRINT,
            observed_equity=Decimal("100000"),
            providers=RuntimeProviderBundle(
                binding=binding,
                trading=BoundProviderResource(binding, trading_resource),
                activities=BoundProviderResource(
                    binding, RuntimeResource.owned(activity, activity.close)
                ),
                option_contracts=BoundProviderResource(binding, trading_resource),
                option_snapshots=BoundProviderResource(binding, option_resource),
                stock_market_data=BoundProviderResource(binding, option_resource),
            ),
            model_transport=RuntimeResource.owned(model, model.close),
            mcp_research=RuntimeResource.async_owned(mcp, close_mcp),
        ),
        trading,
        mcp,
    )


class Runtime:
    class Execution:
        def execute(self, *_args: object) -> None:
            raise AssertionError("execution must remain unavailable")

    execution = Execution()

    class EvidenceClassifier:
        def classify(self, *_args: object) -> None:
            raise AssertionError("classification must remain unavailable")

    evidence_classifier = EvidenceClassifier()

    def __init__(self, dependencies, log: list[str]) -> None:
        self.dependencies = dependencies
        self.log = log
        self.closed = False
        self.mcp_research = dependencies.mcp_research.value

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.dependencies.mcp_research.aclose()
        self.dependencies.model_transport.close()
        self.dependencies.providers.option_snapshots.resource.close()
        self.dependencies.providers.activities.resource.close()
        self.dependencies.providers.trading.resource.close()
        self.dependencies.persistence.close()


async def make_agent(
    configured: Settings,
    store: RuntimePersistence,
    provider: ProductionResources,
    log: list[str],
):
    return await build_production_agent(
        configured,
        Path("migrations"),
        persistence_factory=lambda *_args, **_kwargs: store,
        resources_factory=lambda _settings: provider,
        runtime_factory=lambda _account, dependencies: Runtime(dependencies, log),
    )


class _Route:
    def __init__(self, route: DevelopmentRoute) -> None:
        self.route = route

    def load_development_route(
        self, *, expected_account_fingerprint: str
    ) -> DevelopmentRouteAuthority:
        if self.route is DevelopmentRoute.EMPTY:
            return DevelopmentRouteAuthority(
                expected_account_fingerprint,
                self.route,
                0,
                None,
                None,
                "1" * 64,
            )
        return DevelopmentRouteAuthority(
            expected_account_fingerprint,
            self.route,
            1,
            UUID("00000000-0000-0000-0000-000000000456"),
            "2" * 64,
            "3" * 64,
        )


class _FailingAcquisition:
    def __init__(self, kind: AcquisitionKind, code: str, calls: list[str]) -> None:
        self.kind = kind
        self.code = code
        self.calls = calls

    async def acquire(self, *_args: object, **_kwargs: object):
        self.calls.append(self.code)
        raise AcquisitionFailure(self.kind, self.code)


def _opportunity_factory(
    route: DevelopmentRoute,
    calls: list[str],
    log: list[str],
):
    async def factory(**_kwargs: object) -> ProductionOpportunityWiring:
        calls.append("factory")
        halt = Closeable("halt", log)
        return ProductionOpportunityWiring(
            DevelopmentAcquisitionRouter(
                _Route(route),
                _FailingAcquisition(
                    AcquisitionKind.OPPORTUNITY,
                    "OPPORTUNITY_TEST_STOP",
                    calls,
                ),
                _FailingAcquisition(
                    AcquisitionKind.LIFECYCLE,
                    "LIFECYCLE_TEST_STOP",
                    calls,
                ),
            ),
            RuntimeResource.owned(halt, halt.close),
        )

    return factory


def _stub_opportunity_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.runtime.production._load_production_opportunity_authority",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )


def test_halt_config_uses_one_exact_retained_calendar_session() -> None:
    calls: list[object] = []

    class CalendarTrading:
        def get_calendar(self, filters) -> list[Calendar]:
            calls.append(filters)
            return [Calendar(date="2026-08-31", open="09:30", close="16:00")]

    plan = SimpleNamespace(
        signal_request=SimpleNamespace(signal_session=date(2026, 8, 31)),
        plan_spec=SimpleNamespace(
            underlying="ACME",
            policy=SimpleNamespace(
                selected_decision_boundary=datetime(2026, 8, 31, 14, tzinfo=UTC),
                last_entry_boundary=datetime(2026, 8, 31, 15, tzinfo=UTC),
            ),
        ),
    )

    config = _halt_config_for_plan(
        CalendarTrading(),
        plan,
        maximum_trade_age=timedelta(seconds=15),
    )

    assert len(calls) == 1
    calendar = Calendar(date="2026-08-31", open="09:30", close="16:00")
    assert calendar.open.tzinfo is None and calendar.close.tzinfo is None
    assert calls[0].start == date(2026, 8, 31)
    assert calls[0].end == date(2026, 8, 31)
    assert config.session_open_at == datetime(2026, 8, 31, 13, 30, tzinfo=UTC)
    assert config.session_close_at == datetime(2026, 8, 31, 20, tzinfo=UTC)
    assert config.maximum_trade_age == timedelta(seconds=15)


@pytest.mark.parametrize(
    "bad_open",
    (
        datetime(2026, 8, 30, 9, 30),
        datetime(2026, 8, 31, 9, 30, 1),
    ),
)
def test_halt_config_rejects_noncanonical_alpaca_calendar_times(bad_open: datetime) -> None:
    calendar = Calendar(date="2026-08-31", open="09:30", close="16:00").model_copy(
        update={"open": bad_open}
    )
    plan = SimpleNamespace(
        signal_request=SimpleNamespace(signal_session=date(2026, 8, 31)),
        plan_spec=SimpleNamespace(
            underlying="ACME",
            policy=SimpleNamespace(
                selected_decision_boundary=datetime(2026, 8, 31, 14, tzinfo=UTC),
                last_entry_boundary=datetime(2026, 8, 31, 15, tzinfo=UTC),
            ),
        ),
    )

    with pytest.raises(RuntimeCompositionError, match="OPPORTUNITY_CALENDAR_AUTHORITY_INVALID"):
        _halt_config_for_plan(
            SimpleNamespace(get_calendar=lambda _filters: [calendar]),
            plan,
            maximum_trade_age=timedelta(seconds=15),
        )


def test_absent_opportunity_authority_keeps_lifecycle_only_without_factory_call() -> None:
    log: list[str] = []
    calls: list[str] = []
    store = persistence()
    provider, _trading, _mcp = resources(log)

    async def forbidden(**_kwargs: object) -> ProductionOpportunityWiring:
        calls.append("factory")
        raise AssertionError("absent authority constructed opportunity runtime")

    aggregate = asyncio.run(
        build_production_agent(
            settings(RuntimeRole.DEVELOPMENT),
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: store,
            resources_factory=lambda _settings: provider,
            runtime_factory=lambda _account, dependencies: Runtime(dependencies, log),
            opportunity_factory=forbidden,
        )
    )

    result = asyncio.run(aggregate.service.run(Actor.SCHEDULER))

    assert calls == []
    assert result.decision.provider_failure_code == "CONTEXT_ACTIVE_POSITION_NOT_UNIQUE"
    assert aggregate.opportunity_halt is None
    assert _mcp.entered == 1
    asyncio.run(aggregate.aclose())


def test_plan_policy_preflight_rejects_before_provider_resources(monkeypatch) -> None:
    calls: list[str] = []
    store = dataclass_replace(
        persistence(),
        opportunity_authority_repository=object(),
        opportunity_evidence_repository=object(),
        opportunity_thesis_repository=object(),
    )
    configured = settings(RuntimeRole.DEVELOPMENT, opportunity=True)
    limits = configured.entry_budget_limits()
    plan = SimpleNamespace(
        plan=SimpleNamespace(policy_hash="b" * 64),
        plan_spec=SimpleNamespace(
            policy=SimpleNamespace(
                maximum_lifetime_entries=limits.maximum_lifetime_entries,
                maximum_lifetime_risk=limits.maximum_lifetime_risk,
                maximum_position_loss=limits.maximum_position_loss,
                maximum_quantity=limits.maximum_entry_quantity,
                equity_floor=limits.equity_floor,
            )
        ),
    )
    monkeypatch.setattr(
        "backend.app.runtime.production.SQLAlchemyOpportunityPlanAdapter.load",
        lambda *_args, **_kwargs: plan,
    )

    with pytest.raises(RuntimeCompositionError, match="OPPORTUNITY_POLICY_AUTHORITY_MISMATCH"):
        asyncio.run(
            build_production_agent(
                configured,
                Path("migrations"),
                persistence_factory=lambda *_args, **_kwargs: store,
                resources_factory=lambda _settings: calls.append("resources"),
            )
        )

    assert calls == []


def test_plan_account_preflight_rejects_before_calendar_or_halt() -> None:
    plan = SimpleNamespace(
        plan_spec=SimpleNamespace(
            request_contract=SimpleNamespace(
                account_role=AccountRole.DEVELOPMENT,
                expected_account_fingerprint="f" * 64,
            )
        ),
        baseline_seal=SimpleNamespace(account_fingerprint=FINGERPRINT),
        baseline=SimpleNamespace(account_fingerprint=FINGERPRINT),
    )

    with pytest.raises(RuntimeCompositionError, match="OPPORTUNITY_ACCOUNT_AUTHORITY_MISMATCH"):
        _validate_plan_account_authority(plan, FINGERPRINT)


def test_failure_after_mcp_enter_closes_entered_session_through_aggregate(monkeypatch) -> None:
    log: list[str] = []
    store = persistence()
    provider, _trading, mcp = resources(log)
    _stub_opportunity_preflight(monkeypatch)

    async def failing_factory(*, runtime: Runtime, **_kwargs: object):
        await runtime.mcp_research.__aenter__()
        raise RuntimeError("opportunity construction failed")

    with pytest.raises(RuntimeError, match="opportunity construction failed"):
        asyncio.run(
            build_production_agent(
                settings(RuntimeRole.DEVELOPMENT, opportunity=True),
                Path("migrations"),
                persistence_factory=lambda *_args, **_kwargs: store,
                resources_factory=lambda _settings: provider,
                runtime_factory=lambda _account, dependencies: Runtime(dependencies, log),
                opportunity_factory=failing_factory,
            )
        )

    assert mcp.entered == 1
    assert log == ["mcp", "model", "option", "activity", "trading"]


def test_development_factory_routes_empty_book_to_typed_opportunity(monkeypatch) -> None:
    log: list[str] = []
    calls: list[str] = []
    store = persistence()
    provider, _trading, mcp = resources(log)
    _stub_opportunity_preflight(monkeypatch)
    aggregate = asyncio.run(
        build_production_agent(
            settings(RuntimeRole.DEVELOPMENT, opportunity=True),
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: store,
            resources_factory=lambda _settings: provider,
            runtime_factory=lambda _account, dependencies: Runtime(dependencies, log),
            opportunity_factory=_opportunity_factory(DevelopmentRoute.EMPTY, calls, log),
        )
    )

    result = asyncio.run(aggregate.service.run(Actor.SCHEDULER))

    assert calls == ["factory", "OPPORTUNITY_TEST_STOP"]
    assert result.decision.provider_failure_code == "OPPORTUNITY_TEST_STOP"
    assert mcp.entered == 1
    asyncio.run(aggregate.aclose())
    asyncio.run(aggregate.aclose())
    assert log == ["halt", "mcp", "model", "option", "activity", "trading"]


def test_development_factory_keeps_managed_book_on_lifecycle(monkeypatch) -> None:
    log: list[str] = []
    calls: list[str] = []
    store = persistence()
    provider, _trading, _mcp = resources(log)
    _stub_opportunity_preflight(monkeypatch)
    aggregate = asyncio.run(
        build_production_agent(
            settings(RuntimeRole.DEVELOPMENT, opportunity=True),
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: store,
            resources_factory=lambda _settings: provider,
            runtime_factory=lambda _account, dependencies: Runtime(dependencies, log),
            opportunity_factory=_opportunity_factory(
                DevelopmentRoute.MANAGED_POSITION,
                calls,
                log,
            ),
        )
    )

    result = asyncio.run(aggregate.service.run(Actor.OWNER))

    assert calls == ["factory", "LIFECYCLE_TEST_STOP"]
    assert result.decision.provider_failure_code == "LIFECYCLE_TEST_STOP"
    asyncio.run(aggregate.aclose())


def test_halt_cleanup_failure_does_not_leak_runtime_and_is_not_retried(monkeypatch) -> None:
    log: list[str] = []
    store = persistence()
    provider, _trading, _mcp = resources(log)
    _stub_opportunity_preflight(monkeypatch)

    class FailingHalt:
        def close(self) -> None:
            log.append("halt")
            raise RuntimeError("halt cleanup failed")

    async def factory(**_kwargs: object) -> ProductionOpportunityWiring:
        halt = FailingHalt()
        return ProductionOpportunityWiring(
            _FailingAcquisition(AcquisitionKind.OPPORTUNITY, "UNUSED", []),
            RuntimeResource.owned(halt, halt.close),
        )

    aggregate = asyncio.run(
        build_production_agent(
            settings(RuntimeRole.DEVELOPMENT, opportunity=True),
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: store,
            resources_factory=lambda _settings: provider,
            runtime_factory=lambda _account, dependencies: Runtime(dependencies, log),
            opportunity_factory=factory,
        )
    )

    with pytest.raises(BaseExceptionGroup) as captured:
        asyncio.run(aggregate.aclose())

    assert [str(error) for error in captured.value.exceptions] == ["halt cleanup failed"]
    assert log == ["halt", "mcp", "model", "option", "activity", "trading"]
    asyncio.run(aggregate.aclose())
    assert log == ["halt", "mcp", "model", "option", "activity", "trading"]


def test_submission_never_constructs_configured_opportunity_runtime() -> None:
    log: list[str] = []
    calls: list[str] = []
    store = persistence()
    provider, _trading, _mcp = resources(log)

    async def forbidden(**_kwargs: object) -> ProductionOpportunityWiring:
        calls.append("factory")
        raise AssertionError("submission constructed opportunity runtime")

    aggregate = asyncio.run(
        build_production_agent(
            settings(RuntimeRole.SUBMISSION, opportunity=True),
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: store,
            resources_factory=lambda _settings: provider,
            runtime_factory=lambda _account, dependencies: Runtime(dependencies, log),
            opportunity_factory=forbidden,
        )
    )

    assert aggregate.service is not None
    assert calls == []
    assert aggregate.opportunity_halt is None
    asyncio.run(aggregate.aclose())


def test_submission_gate_loads_exact_role_and_constructs_opportunity_runtime(
    monkeypatch,
) -> None:
    log: list[str] = []
    loaded_roles: list[AccountRole] = []
    factory_authority: list[object] = []
    store = persistence(server_autonomy=True)
    provider, _trading, _mcp = resources(log)
    authority = SimpleNamespace(name="submission-authority")

    def load_authority(_settings, _persistence, *, role):
        loaded_roles.append(role)
        return authority

    async def factory(**kwargs: object) -> ProductionOpportunityWiring:
        factory_authority.append(kwargs["authority"])
        halt = Closeable("halt", log)
        return ProductionOpportunityWiring(
            _FailingAcquisition(
                AcquisitionKind.OPPORTUNITY,
                "OPPORTUNITY_TEST_STOP",
                [],
            ),
            RuntimeResource.owned(halt, halt.close),
        )

    monkeypatch.setattr(
        "backend.app.runtime.production._load_production_opportunity_authority",
        load_authority,
    )
    configured = settings(
        RuntimeRole.SUBMISSION,
        server_autonomy=True,
        opportunity=True,
    ).model_copy(update={"app_submission_opportunity_enabled": True})

    aggregate = asyncio.run(
        build_production_agent(
            configured,
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: store,
            resources_factory=lambda _settings: provider,
            runtime_factory=lambda _account, dependencies: Runtime(dependencies, log),
            opportunity_factory=factory,
        )
    )

    assert loaded_roles == [AccountRole.SUBMISSION]
    assert factory_authority == [authority]
    assert aggregate.opportunity_halt is not None
    asyncio.run(aggregate.aclose())


@pytest.mark.parametrize("actor", (Actor.OWNER, Actor.SCHEDULER))
def test_submission_both_actors_persist_frozen_no_trade_without_provider_entry(actor) -> None:
    log: list[str] = []
    store = persistence()
    provider, _trading, mcp = resources(log)
    aggregate = asyncio.run(make_agent(settings(RuntimeRole.SUBMISSION), store, provider, log))

    result = asyncio.run(aggregate.service.run(actor))

    assert result.terminal_code == "CALIBRATION_BINDING_NO_TRADE"
    assert result.decision.calibration is not None
    assert (
        store.agent_repository.get_account_authority(AccountRole.SUBMISSION).autonomous_enabled
        is False
    )
    assert mcp.entered == 0
    assert log == []
    assert isinstance(
        aggregate.service._lifecycle_terminal_materializer,
        SQLAlchemyLifecycleTerminalMaterializer,
    )
    asyncio.run(aggregate.aclose())


@pytest.mark.parametrize("actor", (Actor.OWNER, Actor.SCHEDULER))
def test_absent_retained_lifecycle_context_is_durable_no_action_and_repeats(actor) -> None:
    log: list[str] = []
    store = persistence()
    provider, _trading, mcp = resources(log)
    aggregate = asyncio.run(make_agent(settings(RuntimeRole.DEVELOPMENT), store, provider, log))

    first = asyncio.run(aggregate.service.run(actor))
    repeated = asyncio.run(aggregate.service.run(actor))

    assert repeated == first
    assert first.terminal_code == "PROVIDER_FAILURE_NO_ACTION"
    assert first.decision.provider_failure_code == "CONTEXT_ACTIVE_POSITION_NOT_UNIQUE"
    assert mcp.entered == 1
    asyncio.run(aggregate.aclose())
    asyncio.run(aggregate.aclose())
    assert log == ["mcp", "model", "option", "activity", "trading"]


def test_completed_tick_is_reused_after_factory_restart() -> None:
    first_log: list[str] = []
    store = persistence()
    first_provider, _trading, _mcp = resources(first_log)
    configured = settings(RuntimeRole.SUBMISSION)
    first = asyncio.run(make_agent(configured, store, first_provider, first_log))
    result = asyncio.run(first.service.run(Actor.SCHEDULER))

    restarted_log: list[str] = []
    restarted_provider, _trading, _mcp = resources(restarted_log)
    restarted = asyncio.run(make_agent(configured, store, restarted_provider, restarted_log))

    assert asyncio.run(restarted.service.run(Actor.SCHEDULER)) == result
    asyncio.run(restarted.aclose())
    asyncio.run(first.aclose())


def test_factory_binds_exact_account_server_gate_and_shared_persistence() -> None:
    log: list[str] = []
    store = persistence(server_autonomy=True)
    provider, _trading, _mcp = resources(log)
    captured: dict[str, object] = {}

    def capture_runtime(account, dependencies):
        captured["account"] = account
        captured["dependencies"] = dependencies
        return Runtime(dependencies, log)

    aggregate = asyncio.run(
        build_production_agent(
            settings(RuntimeRole.DEVELOPMENT, server_autonomy=True),
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: store,
            resources_factory=lambda _settings: provider,
            runtime_factory=capture_runtime,
        )
    )

    account = captured["account"]
    dependencies = captured["dependencies"]
    assert account.role is AccountRole.DEVELOPMENT
    assert account.account_fingerprint == FINGERPRINT
    assert account.endpoint == "https://paper-api.alpaca.markets"
    assert account.paper is True
    assert account.autonomous_enabled is True
    assert dependencies.persistence.value is store
    assert store.agent_repository.server_autonomy_enabled is True
    asyncio.run(aggregate.aclose())


def test_registration_preserves_durable_autonomy_and_revalidates_account() -> None:
    log: list[str] = []
    store = persistence(server_autonomy=True)
    store.repository.register_account(
        role=AccountRole.DEVELOPMENT,
        fingerprint=FINGERPRINT,
        equity=Decimal("90000"),
        autonomous_enabled=False,
    )
    store.repository.set_autonomous_enabled(AccountRole.DEVELOPMENT, True, actor=Actor.OWNER)
    provider, trading, _mcp = resources(log)
    aggregate = asyncio.run(
        make_agent(
            settings(RuntimeRole.DEVELOPMENT, server_autonomy=True),
            store,
            provider,
            log,
        )
    )

    assert (
        store.agent_repository.get_account_authority(AccountRole.DEVELOPMENT).autonomous_enabled
        is True
    )
    trading.account_id = UUID("00000000-0000-0000-0000-000000000124")
    with pytest.raises(ValueError, match="ACCOUNT_FINGERPRINT_MISMATCH"):
        asyncio.run(aggregate.service.run(Actor.OWNER))
    asyncio.run(aggregate.aclose())


def test_owner_arms_persistent_autonomy_when_server_gate_is_enabled() -> None:
    log: list[str] = []
    store = persistence(server_autonomy=True)
    provider, _trading, _mcp = resources(log)
    aggregate = asyncio.run(
        make_agent(
            settings(RuntimeRole.DEVELOPMENT, server_autonomy=True),
            store,
            provider,
            log,
        )
    )

    status = aggregate.autonomy.enable(Actor.OWNER)

    assert status.role is AccountRole.DEVELOPMENT
    assert status.server_enabled is True
    assert status.account_enabled is True
    assert status.effective is True
    assert (
        store.agent_repository.get_account_authority(AccountRole.DEVELOPMENT).autonomous_enabled
        is True
    )
    result = asyncio.run(aggregate.service.run(Actor.SCHEDULER))
    assert result.terminal_code == "PROVIDER_FAILURE_NO_ACTION"
    asyncio.run(aggregate.aclose())


def test_owner_can_disarm_persistent_autonomy() -> None:
    log: list[str] = []
    store = persistence(server_autonomy=True)
    provider, _trading, _mcp = resources(log)
    aggregate = asyncio.run(
        make_agent(
            settings(RuntimeRole.DEVELOPMENT, server_autonomy=True),
            store,
            provider,
            log,
        )
    )
    aggregate.autonomy.enable(Actor.OWNER)

    status = aggregate.autonomy.disable(Actor.OWNER)

    assert status.role is AccountRole.DEVELOPMENT
    assert status.server_enabled is True
    assert status.account_enabled is False
    assert status.effective is False
    assert (
        store.agent_repository.get_account_authority(AccountRole.DEVELOPMENT).autonomous_enabled
        is False
    )
    asyncio.run(aggregate.aclose())


def test_closed_server_gate_cannot_be_armed_and_scheduler_cannot_arm_it() -> None:
    log: list[str] = []
    store = persistence(server_autonomy=False)
    provider, _trading, _mcp = resources(log)
    aggregate = asyncio.run(
        make_agent(
            settings(RuntimeRole.DEVELOPMENT, server_autonomy=False),
            store,
            provider,
            log,
        )
    )

    with pytest.raises(ExecutionBlocked, match="SERVER_AUTONOMY_DISABLED"):
        aggregate.autonomy.enable(Actor.OWNER)
    assert aggregate.autonomy.status().effective is False
    asyncio.run(aggregate.aclose())

    second_log: list[str] = []
    second_store = persistence(server_autonomy=True)
    second_provider, _trading, _mcp = resources(second_log)
    second = asyncio.run(
        make_agent(
            settings(RuntimeRole.DEVELOPMENT, server_autonomy=True),
            second_store,
            second_provider,
            second_log,
        )
    )
    with pytest.raises(ExecutionBlocked, match="SCHEDULER_CANNOT_ENABLE_AUTONOMY"):
        second.autonomy.enable(Actor.SCHEDULER)
    assert second.autonomy.status().effective is False
    asyncio.run(second.aclose())


def test_failed_provider_revalidation_does_not_arm_persistent_autonomy() -> None:
    log: list[str] = []
    store = persistence(server_autonomy=True)
    provider, trading, _mcp = resources(log)
    aggregate = asyncio.run(
        make_agent(
            settings(RuntimeRole.DEVELOPMENT, server_autonomy=True),
            store,
            provider,
            log,
        )
    )
    trading.account_id = UUID("00000000-0000-0000-0000-000000000124")

    with pytest.raises(ValueError, match="ACCOUNT_FINGERPRINT_MISMATCH"):
        aggregate.autonomy.enable(Actor.OWNER)

    assert (
        store.agent_repository.get_account_authority(AccountRole.DEVELOPMENT).autonomous_enabled
        is False
    )
    asyncio.run(aggregate.aclose())


def test_owner_can_disarm_when_provider_revalidation_is_unavailable() -> None:
    log: list[str] = []
    store = persistence(server_autonomy=True)
    provider, trading, _mcp = resources(log)
    aggregate = asyncio.run(
        make_agent(
            settings(RuntimeRole.DEVELOPMENT, server_autonomy=True),
            store,
            provider,
            log,
        )
    )
    aggregate.autonomy.enable(Actor.OWNER)
    trading.account_id = UUID("00000000-0000-0000-0000-000000000124")

    status = aggregate.autonomy.disable(Actor.OWNER)

    assert status.account_enabled is False
    assert status.effective is False
    asyncio.run(aggregate.aclose())


@pytest.mark.parametrize(
    ("server_autonomy", "durable_autonomy"),
    ((False, False), (True, False), (False, True), (True, True)),
)
def test_registration_never_changes_durable_autonomy(
    server_autonomy: bool,
    durable_autonomy: bool,
) -> None:
    log: list[str] = []
    store = persistence(server_autonomy=server_autonomy)
    store.repository.register_account(
        role=AccountRole.DEVELOPMENT,
        fingerprint=FINGERPRINT,
        equity=Decimal("90000"),
        autonomous_enabled=False,
    )
    if durable_autonomy:
        store.repository.set_autonomous_enabled(AccountRole.DEVELOPMENT, True, actor=Actor.OWNER)
    provider, _trading, _mcp = resources(log)

    aggregate = asyncio.run(
        make_agent(
            settings(RuntimeRole.DEVELOPMENT, server_autonomy=server_autonomy),
            store,
            provider,
            log,
        )
    )

    assert (
        store.agent_repository.get_account_authority(AccountRole.DEVELOPMENT).autonomous_enabled
        is durable_autonomy
    )
    asyncio.run(aggregate.aclose())


def test_calibration_machine_binding_changes_for_authority_and_settings() -> None:
    authority = ObservedPaperAccountAuthority(AccountRole.SUBMISSION, FINGERPRINT, True, False)
    original = SettingsCalibrationBinding(settings(RuntimeRole.SUBMISSION)).binding_for(authority)
    changed_policy = SettingsCalibrationBinding(
        settings(RuntimeRole.SUBMISSION, policy_hash="c" * 64)
    ).binding_for(authority)
    changed_calibration = SettingsCalibrationBinding(
        settings(RuntimeRole.SUBMISSION, calibration_hash="d" * 64)
    ).binding_for(authority)
    changed_authority = SettingsCalibrationBinding(settings(RuntimeRole.SUBMISSION)).binding_for(
        ObservedPaperAccountAuthority(AccountRole.SUBMISSION, "e" * 64, True, False)
    )

    assert (
        len(
            {
                original.machine_binding_hash,
                changed_policy.machine_binding_hash,
                changed_calibration.machine_binding_hash,
                changed_authority.machine_binding_hash,
            }
        )
        == 4
    )
    with pytest.raises(ValueError, match="CALIBRATION_BINDING_AUTHORITY_MISMATCH"):
        SettingsCalibrationBinding(settings(RuntimeRole.SUBMISSION)).binding_for(
            ObservedPaperAccountAuthority(AccountRole.DEVELOPMENT, FINGERPRINT, True, False)
        )

    material_without_decision_code = {
        "domain": "alphadecay.calibration-machine-binding.v1",
        "account_role": authority.role.value,
        "account_fingerprint": authority.account_fingerprint,
        "policy_hash": "a" * 64,
        "calibration_hash": "b" * 64,
        "decision_boundary": datetime(2026, 8, 28, 16, tzinfo=UTC).isoformat(),
        "sealed_at": datetime(2026, 8, 28, 16, 1, tzinfo=UTC).isoformat(),
    }
    assert "decision_code" not in material_without_decision_code
    assert (
        original.machine_binding_hash
        != hashlib.sha256(
            json.dumps(
                material_without_decision_code,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    material_with_decision_code = {
        **material_without_decision_code,
        "decision_code": "CALIBRATION_BINDING_NO_TRADE",
    }
    assert (
        original.machine_binding_hash
        == hashlib.sha256(
            json.dumps(
                material_with_decision_code,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        ("inactive", "ACCOUNT_BLOCKED"),
        ("blocked", "ACCOUNT_BLOCKED"),
        ("mismatch", "ACCOUNT_FINGERPRINT_MISMATCH"),
    ),
)
def test_full_retained_account_validation_precedes_runtime_publication(
    mutation: str,
    code: str,
) -> None:
    log: list[str] = []
    store = persistence()
    provider, trading, _mcp = resources(log)
    if mutation == "inactive":
        trading.status = "INACTIVE"
    elif mutation == "blocked":
        trading.account_blocked = True
    else:
        trading.account_id = UUID("00000000-0000-0000-0000-000000000124")
    runtime_calls = 0

    def forbidden_runtime(*_args: object):
        nonlocal runtime_calls
        runtime_calls += 1
        raise AssertionError("invalid account reached runtime construction")

    async def build() -> None:
        await build_production_agent(
            settings(RuntimeRole.SUBMISSION),
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: store,
            resources_factory=lambda _settings: provider,
            runtime_factory=forbidden_runtime,
        )

    with pytest.raises(ValueError, match=code):
        asyncio.run(build())
    assert runtime_calls == 0
    assert log == ["mcp", "model", "option", "activity", "trading"]


def test_role_substitution_fails_closed_and_cleans_constructed_resources() -> None:
    log: list[str] = []
    store = persistence()
    store.repository.register_account(
        role=AccountRole.DEVELOPMENT,
        fingerprint=FINGERPRINT,
        equity=Decimal("100000"),
        autonomous_enabled=False,
    )
    provider, _trading, _mcp = resources(log)

    with pytest.raises(ValueError, match="ACCOUNT_FINGERPRINT_ROLE_MISMATCH"):
        asyncio.run(make_agent(settings(RuntimeRole.SUBMISSION), store, provider, log))

    assert log == ["mcp", "model", "option", "activity", "trading"]


def test_successful_cleanup_is_reverse_order_and_once_only() -> None:
    log: list[str] = []
    store = persistence()
    provider, _trading, _mcp = resources(log)
    aggregate = asyncio.run(make_agent(settings(RuntimeRole.SUBMISSION), store, provider, log))

    async def close_twice() -> None:
        await aggregate.aclose()
        await aggregate.aclose()

    asyncio.run(close_twice())

    assert log == ["mcp", "model", "option", "activity", "trading"]
    with pytest.raises(ProviderSettingsRepositoryError, match="PROVIDER_SETTINGS_CLOSED"):
        aggregate.provider_settings.status()


def test_database_clock_is_utc_and_constructor_failure_cleans_reverse_once() -> None:
    log: list[str] = []
    store = persistence()
    provider, _trading, _mcp = resources(log)
    now = store.database_clock.now()
    assert now.tzinfo is not None and now.utcoffset().total_seconds() == 0

    async def build() -> None:
        await build_production_agent(
            settings(RuntimeRole.DEVELOPMENT),
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: store,
            resources_factory=lambda _settings: provider,
            runtime_factory=lambda *_args: (_ for _ in ()).throw(RuntimeError("runtime failed")),
        )

    with pytest.raises(RuntimeError, match="runtime failed"):
        asyncio.run(build())
    assert log == ["mcp", "model", "option", "activity", "trading"]


def test_partial_cleanup_continues_and_retains_startup_and_cleanup_failures() -> None:
    log: list[str] = []
    store = persistence()

    class LoggedPersistence:
        def __getattr__(self, name: str):
            return getattr(store, name)

        def close(self) -> None:
            log.append("persistence")
            store.close()

    wrapped = LoggedPersistence()
    provider, _trading, _mcp = resources(log, fail_mcp_close=True)

    async def build() -> None:
        await build_production_agent(
            settings(RuntimeRole.DEVELOPMENT),
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: wrapped,
            resources_factory=lambda _settings: provider,
            runtime_factory=lambda *_args: (_ for _ in ()).throw(RuntimeError("startup failed")),
        )

    with pytest.raises(BaseExceptionGroup) as captured:
        asyncio.run(build())

    assert [str(error) for error in captured.value.exceptions] == [
        "startup failed",
        "mcp cleanup failed",
    ]
    assert log == ["mcp", "model", "option", "activity", "trading", "persistence"]


@pytest.mark.parametrize(
    "updates",
    (
        {"alpaca_api_endpoint": "https://api.alpaca.markets"},
        {"alpaca_paper_trade": False},
    ),
)
def test_factory_rejects_live_configuration_before_persistence_or_provider(
    updates: dict[str, object],
) -> None:
    calls: list[str] = []
    bypassed = settings(RuntimeRole.SUBMISSION).model_copy(update=updates)

    async def build() -> None:
        await build_production_agent(
            bypassed,
            Path("migrations"),
            persistence_factory=lambda *_args, **_kwargs: calls.append("persistence"),
            resources_factory=lambda _settings: calls.append("resources"),
        )

    with pytest.raises(RuntimeCompositionError, match="PAPER_TRADING_REQUIRED"):
        asyncio.run(build())
    assert calls == []
