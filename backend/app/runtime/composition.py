from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

from backend.app.alpaca.execution_evidence import (
    ActivitySource,
    AlpacaExecutionReadCollector,
    AlpacaOptionContractCollector,
    AlpacaReplacementQuoteCollector,
    AlpacaWholeAccountSweepPort,
    IndicativeGreekCollector,
    LifecycleActivitySource,
    OptionContractClient,
    OptionSnapshotClient,
    TradingReadClient,
)
from backend.app.alpaca.market_data import StockMarketDataClient
from backend.app.alpaca.trading import (
    AlpacaOrderWriteAdapter,
    AlpacaTradingReadAdapter,
    OrderWriteClient,
    TradingFixtureClient,
)
from backend.app.contracts.v1 import (
    AccountResponse,
    AccountRole,
    DataQuality,
    EvidenceClassification,
    PositionListResponse,
    SourceCluster,
    ThesisResponse,
)
from backend.app.evidence.classifier import (
    EvidenceClassificationContext,
    EvidenceClassifier,
    StructuredModelTransport,
)
from backend.app.evidence.repository import EvidenceLedger
from backend.app.execution import Actor, ExecutionBlocked, ExecutionCertificate
from backend.app.execution.reconciliation import ReconciliationExpectation
from backend.app.services.execution import (
    ExecutionAuthorityRepository,
    ExecutionService,
    WholeAccountEvidence,
)

PAPER_TRADING_ENDPOINT = "https://paper-api.alpaca.markets"
_EXECUTABLE_ROLES = frozenset({AccountRole.SUBMISSION, AccountRole.DEVELOPMENT})
_FINGERPRINT_LENGTH = 64
_BINDING_TOKEN_MAX_LENGTH = 256


class RuntimeCompositionError(RuntimeError):
    pass


class RuntimePersistence(Protocol):
    repository: ExecutionAuthorityRepository
    evidence_ledger: EvidenceLedger

    def close(self) -> None: ...


class MCPResearchPort(Protocol):
    async def __aenter__(self) -> MCPResearchPort: ...
    async def __aexit__(self, *args: object) -> None: ...
    async def call(self, tool_name: str, arguments: Mapping[str, object]) -> object: ...


class RuntimeTradingClient(TradingFixtureClient, TradingReadClient, OrderWriteClient, Protocol):
    pass


class RuntimeActivitySource(ActivitySource, LifecycleActivitySource, Protocol):
    pass


@dataclass(frozen=True)
class RuntimeAccountConfig:
    role: AccountRole
    endpoint: str
    account_fingerprint: str
    paper: bool = True
    baseline_status: DataQuality = DataQuality.UNKNOWN
    autonomous_enabled: bool = False

    def __post_init__(self) -> None:
        if self.role not in _EXECUTABLE_ROLES:
            raise RuntimeCompositionError("EXECUTABLE_ACCOUNT_ROLE_REQUIRED")
        if self.endpoint != PAPER_TRADING_ENDPOINT or self.paper is not True:
            raise RuntimeCompositionError("PAPER_TRADING_REQUIRED")
        _validate_fingerprint(self.account_fingerprint)


@dataclass(frozen=True)
class ProviderBinding:
    endpoint: str
    account_fingerprint: str
    account_binding_token: str = field(repr=False)
    paper: bool = True

    def __post_init__(self) -> None:
        if self.endpoint != PAPER_TRADING_ENDPOINT or self.paper is not True:
            raise RuntimeCompositionError("PAPER_TRADING_REQUIRED")
        _validate_fingerprint(self.account_fingerprint)
        token = self.account_binding_token
        if (
            not isinstance(token, str)
            or not token
            or len(token) > _BINDING_TOKEN_MAX_LENGTH
            or any(character.isspace() or not character.isprintable() for character in token)
        ):
            raise RuntimeCompositionError("ACCOUNT_BINDING_TOKEN_INVALID")


@dataclass(frozen=True)
class RuntimeResource[T]:
    value: T
    close: Callable[[], None] | None = None
    aclose: Callable[[], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        if self.close is not None and self.aclose is not None:
            raise RuntimeCompositionError("RUNTIME_RESOURCE_CLEANUP_AMBIGUOUS")
        if self.close is not None and (not callable(self.close) or _is_async_callable(self.close)):
            raise RuntimeCompositionError("RUNTIME_RESOURCE_SYNC_CLEANUP_INVALID")
        if self.aclose is not None and (
            not callable(self.aclose) or not _is_async_callable(self.aclose)
        ):
            raise RuntimeCompositionError("RUNTIME_RESOURCE_ASYNC_CLEANUP_INVALID")

    @classmethod
    def borrowed(cls, value: T) -> RuntimeResource[T]:
        return cls(value=value)

    @classmethod
    def owned(cls, value: T, close: Callable[[], None]) -> RuntimeResource[T]:
        return cls(value=value, close=close)

    @classmethod
    def async_owned(cls, value: T, aclose: Callable[[], Awaitable[None]]) -> RuntimeResource[T]:
        return cls(value=value, aclose=aclose)


@dataclass(frozen=True)
class BoundProviderResource[T]:
    binding: ProviderBinding
    resource: RuntimeResource[T]


@dataclass(frozen=True)
class RuntimeProviderBundle:
    binding: ProviderBinding
    trading: BoundProviderResource[RuntimeTradingClient]
    activities: BoundProviderResource[RuntimeActivitySource]
    option_contracts: BoundProviderResource[OptionContractClient]
    option_snapshots: BoundProviderResource[OptionSnapshotClient]
    stock_market_data: BoundProviderResource[StockMarketDataClient]

    def __post_init__(self) -> None:
        components = (
            self.trading,
            self.activities,
            self.option_contracts,
            self.option_snapshots,
            self.stock_market_data,
        )
        if any(component.binding is not self.binding for component in components):
            raise RuntimeCompositionError("PROVIDER_ACCOUNT_BINDING_MISMATCH")


@dataclass(frozen=True)
class RuntimeDependencies:
    persistence: RuntimeResource[RuntimePersistence]
    providers: RuntimeProviderBundle
    model_transport: RuntimeResource[StructuredModelTransport]
    mcp_research: RuntimeResource[MCPResearchPort]
    clock: Callable[[], datetime]


class _RuntimeState(str, Enum):
    OPEN = "OPEN"
    CLEANING = "CLEANING"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CLOSED = "CLOSED"


@dataclass
class _CleanupStep:
    close: Callable[[], None] | None
    aclose: Callable[[], Awaitable[None]] | None
    complete: bool = False


class _RuntimeGuard:
    def __init__(self) -> None:
        self.__state = _RuntimeState.OPEN
        self.__active_operations = 0
        self.__condition = threading.Condition(threading.RLock())

    @property
    def state(self) -> _RuntimeState:
        with self.__condition:
            return self.__state

    @contextmanager
    def operation(self) -> Iterator[None]:
        with self.__condition:
            if self.__state is not _RuntimeState.OPEN:
                raise RuntimeCompositionError("RUNTIME_CLOSED")
            self.__active_operations += 1
        try:
            yield
        finally:
            with self.__condition:
                self.__active_operations -= 1
                if self.__active_operations == 0:
                    self.__condition.notify_all()

    def begin_cleanup(self) -> None:
        with self.__condition:
            self.__state = _RuntimeState.CLEANING
            while self.__active_operations:
                self.__condition.wait()

    def mark_cleanup_failed(self) -> None:
        with self.__condition:
            self.__state = _RuntimeState.CLEANUP_FAILED
            self.__condition.notify_all()

    def mark_closed(self) -> None:
        with self.__condition:
            self.__state = _RuntimeState.CLOSED
            self.__condition.notify_all()

    def require_open(self) -> None:
        with self.__condition:
            if self.__state is not _RuntimeState.OPEN:
                raise RuntimeCompositionError("RUNTIME_CLOSED")


class _ObservedAccountAuthority:
    def __init__(
        self,
        target: AlpacaTradingReadAdapter,
        account: RuntimeAccountConfig,
    ) -> None:
        self.__target = target
        self.__account = account

    def verify(self) -> AccountResponse:
        observed = self.__target.get_account()
        if observed.role is not self.__account.role or observed.paper is not True:
            raise RuntimeCompositionError("OBSERVED_ACCOUNT_AUTHORITY_MISMATCH")
        return observed


class RuntimeAccountView:
    def __init__(self, target: AlpacaTradingReadAdapter, guard: _RuntimeGuard) -> None:
        self.__target = target
        self.__guard = guard

    def get_account(self) -> AccountResponse:
        with self.__guard.operation():
            return self.__target.get_account()

    def list_positions(self) -> PositionListResponse:
        with self.__guard.operation():
            return self.__target.list_positions()

    def list_open_orders(self) -> tuple[dict[str, object], ...]:
        with self.__guard.operation():
            return self.__target.list_open_orders()


class RuntimeAccountSweep:
    def __init__(
        self,
        target: AlpacaWholeAccountSweepPort,
        guard: _RuntimeGuard,
        account_authority: _ObservedAccountAuthority,
    ) -> None:
        self.__target = target
        self.__guard = guard
        self.__account_authority = account_authority

    def collect(self, expectation: ReconciliationExpectation) -> WholeAccountEvidence:
        with self.__guard.operation():
            self.__account_authority.verify()
            return self.__target.collect(expectation)


class RuntimeEvidenceClassifier:
    def __init__(
        self,
        target: EvidenceClassifier,
        guard: _RuntimeGuard,
        account_authority: _ObservedAccountAuthority,
    ) -> None:
        self.__target = target
        self.__guard = guard
        self.__account_authority = account_authority

    @property
    def model_calls(self) -> int:
        with self.__guard.operation():
            self.__account_authority.verify()
            return self.__target.model_calls

    def classify(
        self, thesis: ThesisResponse, clusters: tuple[SourceCluster, ...]
    ) -> tuple[EvidenceClassification, ...]:
        with self.__guard.operation():
            self.__account_authority.verify()
            return self.__target.classify(thesis, clusters)

    def classify_context(
        self,
        context: EvidenceClassificationContext,
        clusters: tuple[SourceCluster, ...],
    ) -> tuple[EvidenceClassification, ...]:
        with self.__guard.operation():
            self.__account_authority.verify()
            return self.__target.classify_context(context, clusters)


class RuntimeMCPResearch:
    def __init__(
        self,
        target: MCPResearchPort,
        guard: _RuntimeGuard,
        account_authority: _ObservedAccountAuthority,
        owned_aclose: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self.__target = target
        self.__guard = guard
        self.__account_authority = account_authority
        self.__owned_aclose = owned_aclose
        self.__enter_attempted = False
        self.__entered = False
        self.__closed = False
        self.__closing = False
        self.__operation_lock = asyncio.Lock()
        self.__cleanup_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> RuntimeMCPResearch:
        async with self.__operation_lock:
            self.__guard.require_open()
            if self.__closed or self.__closing:
                raise RuntimeCompositionError("MCP_SESSION_CLOSED")
            if self.__enter_attempted:
                raise RuntimeCompositionError("MCP_SESSION_ALREADY_ENTERED")
            self.__account_authority.verify()
            self.__enter_attempted = True
            await self.__target.__aenter__()
            self.__entered = True
            return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose(*args)

    async def call(self, tool_name: str, arguments: Mapping[str, object]) -> object:
        async with self.__operation_lock:
            self.__guard.require_open()
            if not self.__entered or self.__closed or self.__closing:
                raise RuntimeCompositionError("MCP_SESSION_NOT_OPEN")
            self.__account_authority.verify()
            return await self.__target.call(tool_name, arguments)

    async def aclose(self, *args: object) -> None:
        async with self.__operation_lock:
            if self.__closed:
                return
            task = self.__cleanup_task
            if task is None:
                self.__closing = True
                task = asyncio.create_task(self.__run_cleanup(args))
                self.__cleanup_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                async with self.__operation_lock:
                    if self.__cleanup_task is task:
                        self.__cleanup_task = None
                        self.__closing = False
            raise
        except BaseException:
            async with self.__operation_lock:
                if self.__cleanup_task is task:
                    self.__cleanup_task = None
                    self.__closing = False
            raise

    async def __run_cleanup(self, args: tuple[object, ...]) -> None:
        if self.__owned_aclose is not None:
            await self.__owned_aclose()
        elif self.__entered:
            await self.__target.__aexit__(*args)
        self.__entered = False
        self.__closed = True
        self.__closing = False


class AuthorizedExecution:
    def __init__(
        self,
        execute: Callable[..., ExecutionCertificate],
        guard: _RuntimeGuard,
        account: RuntimeAccountConfig,
        account_authority: _ObservedAccountAuthority,
    ) -> None:
        self.__execute = execute
        self.__guard = guard
        self.__account = account
        self.__account_authority = account_authority

    def execute(self, intent_id: UUID, actor: Actor, now: datetime) -> ExecutionCertificate:
        with self.__guard.operation():
            if actor is Actor.SCHEDULER and not self.__account.autonomous_enabled:
                raise ExecutionBlocked("SERVER_AUTONOMY_DISABLED")
            observed = self.__account_authority.verify()
            return self.__execute(
                intent_id,
                actor,
                now,
                account_role=observed.role,
                account_fingerprint=self.__account.account_fingerprint,
            )


class RuntimeComposition:
    def __init__(
        self,
        *,
        account: RuntimeAccountConfig,
        account_view: RuntimeAccountView,
        account_sweep: RuntimeAccountSweep,
        evidence_classifier: RuntimeEvidenceClassifier,
        mcp_research: RuntimeMCPResearch,
        execution: AuthorizedExecution,
        guard: _RuntimeGuard,
        cleanup_steps: tuple[_CleanupStep, ...],
    ) -> None:
        self.account = account
        self.account_view = account_view
        self.account_sweep = account_sweep
        self.evidence_classifier = evidence_classifier
        self.mcp_research = mcp_research
        self.execution = execution
        self.__guard = guard
        self.__cleanup_steps = cleanup_steps
        self.__cleanup_condition = threading.Condition(threading.RLock())
        self.__async_cleanup_task: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        return self.__guard.state is _RuntimeState.CLOSED

    def close(self) -> None:
        with self.__cleanup_condition:
            while self.__guard.state is _RuntimeState.CLEANING:
                if self.__async_cleanup_task is not None:
                    raise RuntimeCompositionError("RUNTIME_CLEANUP_IN_PROGRESS")
                self.__cleanup_condition.wait()
            if self.closed:
                return
            if any(step.aclose is not None and not step.complete for step in self.__cleanup_steps):
                raise RuntimeCompositionError("ASYNC_RUNTIME_CLEANUP_REQUIRED")
            self.__guard.begin_cleanup()
        try:
            for step in reversed(self.__cleanup_steps):
                if not step.complete and step.close is not None:
                    step.close()
                    step.complete = True
        except BaseException as error:
            with self.__cleanup_condition:
                self.__guard.mark_cleanup_failed()
                self.__cleanup_condition.notify_all()
            raise RuntimeCompositionError("RUNTIME_CLEANUP_FAILED") from error
        with self.__cleanup_condition:
            self.__guard.mark_closed()
            self.__cleanup_condition.notify_all()

    async def aclose(self) -> None:
        if self.closed:
            return
        with self.__cleanup_condition:
            if self.closed:
                return
            if self.__guard.state is _RuntimeState.CLEANING:
                task = self.__async_cleanup_task
                if task is None or task.get_loop() is not asyncio.get_running_loop():
                    raise RuntimeCompositionError("RUNTIME_CLEANUP_IN_PROGRESS")
            else:
                self.__guard.begin_cleanup()
                task = asyncio.create_task(self.__run_async_cleanup())
                self.__async_cleanup_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except BaseException:
            with self.__cleanup_condition:
                if self.__async_cleanup_task is task:
                    self.__async_cleanup_task = None
            raise

    async def __run_async_cleanup(self) -> None:
        try:
            for step in reversed(self.__cleanup_steps):
                if step.complete:
                    continue
                if step.aclose is not None:
                    await step.aclose()
                elif step.close is not None:
                    step.close()
                step.complete = True
        except BaseException as error:
            with self.__cleanup_condition:
                self.__guard.mark_cleanup_failed()
                self.__cleanup_condition.notify_all()
            raise RuntimeCompositionError("RUNTIME_CLEANUP_FAILED") from error
        with self.__cleanup_condition:
            self.__guard.mark_closed()
            self.__cleanup_condition.notify_all()

    def __enter__(self) -> RuntimeComposition:
        self.__guard.require_open()
        if any(step.aclose is not None and not step.complete for step in self.__cleanup_steps):
            raise RuntimeCompositionError("ASYNC_RUNTIME_CONTEXT_REQUIRED")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    async def __aenter__(self) -> RuntimeComposition:
        self.__guard.require_open()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()


def build_runtime(
    account: RuntimeAccountConfig,
    dependencies: RuntimeDependencies,
) -> RuntimeComposition:
    _validate_dependencies(account, dependencies)
    persistence = dependencies.persistence.value
    providers = dependencies.providers
    trading_client = providers.trading.resource.value
    account_view_adapter = AlpacaTradingReadAdapter(
        trading_client,
        account_role=account.role,
        expected_account_fingerprint=account.account_fingerprint,
        baseline_status=account.baseline_status,
        autonomous_enabled=account.autonomous_enabled,
    )
    account_authority = _ObservedAccountAuthority(account_view_adapter, account)
    trading = AlpacaExecutionReadCollector(
        trading_client,
        account_role=account.role,
        expected_account_fingerprint=account.account_fingerprint,
        paper=True,
        clock=dependencies.clock,
    )
    contracts = AlpacaOptionContractCollector(providers.option_contracts.resource.value)
    greeks = IndicativeGreekCollector(
        providers.option_snapshots.resource.value,
        contracts,
        clock=dependencies.clock,
    )
    sweep = AlpacaWholeAccountSweepPort(
        trading,
        providers.activities.resource.value,
        greeks,
        persistence.repository,
        clock=dependencies.clock,
    )
    execution_service = ExecutionService(
        persistence.repository,
        AlpacaOrderWriteAdapter(trading_client),
        sweep,
        AlpacaReplacementQuoteCollector(
            providers.option_snapshots.resource.value,
            contracts,
            clock=dependencies.clock,
        ),
        account_role=account.role,
        account_fingerprint=account.account_fingerprint,
    )
    classifier = EvidenceClassifier(
        dependencies.model_transport.value,
        ledger=persistence.evidence_ledger,
    )
    guard = _RuntimeGuard()
    mcp_research = RuntimeMCPResearch(
        dependencies.mcp_research.value,
        guard,
        account_authority,
        dependencies.mcp_research.aclose,
    )
    resources = _runtime_resources(dependencies)
    unique_resources: list[RuntimeResource[object]] = []
    for resource in resources:
        if not any(resource is existing for existing in unique_resources):
            unique_resources.append(resource)
    cleanup_steps = tuple(
        _CleanupStep(
            resource.close,
            mcp_research.aclose
            if resource is dependencies.mcp_research and resource.aclose is not None
            else resource.aclose,
        )
        for resource in unique_resources
        if resource.close is not None or resource.aclose is not None
    )
    return RuntimeComposition(
        account=account,
        account_view=RuntimeAccountView(account_view_adapter, guard),
        account_sweep=RuntimeAccountSweep(sweep, guard, account_authority),
        evidence_classifier=RuntimeEvidenceClassifier(classifier, guard, account_authority),
        mcp_research=mcp_research,
        execution=AuthorizedExecution(
            execution_service.execute,
            guard,
            account,
            account_authority,
        ),
        guard=guard,
        cleanup_steps=cleanup_steps,
    )


def _validate_dependencies(
    account: RuntimeAccountConfig, dependencies: RuntimeDependencies
) -> None:
    binding = dependencies.providers.binding
    if (
        binding.endpoint != account.endpoint
        or binding.paper is not account.paper
        or binding.account_fingerprint != account.account_fingerprint
    ):
        raise RuntimeCompositionError("PROVIDER_ACCOUNT_BINDING_MISMATCH")
    _validate_resource_ownership(dependencies)
    persistence = dependencies.persistence.value
    _require_sync_methods(persistence, "close")
    _require_sync_methods(
        persistence.repository,
        "claim_intent",
        "get_intent",
        "next_broker_mutation",
        "trusted_execution_time",
        "plan_broker_mutation",
        "prepare_broker_mutation",
        "acquire_broker_dispatch",
        "record_attempt_observation",
        "mark_broker_dispatch_ambiguous",
        "record_attempt_absence",
        "final_reconciliation_expectation",
        "attempts_for",
        "execution_attempts_for",
        "get_execution_certificate",
        "finalize_execution_authorized",
    )
    _require_sync_methods(
        persistence.evidence_ledger,
        "acquire",
        "reserve_model_request",
        "complete",
        "release",
        "model_request_count",
    )
    _require_sync_methods(
        dependencies.providers.trading.resource.value,
        "get_account",
        "get_all_positions",
        "get_orders",
        "get_order_by_id",
        "submit_order",
        "get_order_by_client_id",
        "replace_order_by_id",
        "cancel_order_by_id",
    )
    _require_sync_methods(
        dependencies.providers.activities.resource.value,
        "collect",
        "collect_lifecycle",
    )
    _require_sync_methods(
        dependencies.providers.option_contracts.resource.value, "get_option_contracts"
    )
    _require_sync_methods(
        dependencies.providers.option_snapshots.resource.value, "get_option_snapshot"
    )
    _require_sync_methods(
        dependencies.providers.stock_market_data.resource.value,
        "get_stock_latest_quote",
        "get_stock_bars",
    )
    _require_sync_methods(dependencies.model_transport.value, "generate")
    _require_async_methods(dependencies.mcp_research.value, "__aenter__", "__aexit__", "call")
    if not callable(dependencies.clock) or _is_async_callable(dependencies.clock):
        raise RuntimeCompositionError("RUNTIME_DEPENDENCY_INVALID:clock")


def _require_sync_methods(value: object, *method_names: str) -> None:
    for method_name in method_names:
        method = getattr(value, method_name, None)
        if not callable(method) or _is_async_callable(method):
            raise RuntimeCompositionError(f"RUNTIME_SYNC_DEPENDENCY_INVALID:{method_name}")


def _validate_resource_ownership(dependencies: RuntimeDependencies) -> None:
    owners_by_value: dict[int, RuntimeResource[object]] = {}
    for resource in _runtime_resources(dependencies):
        if resource.close is None and resource.aclose is None:
            continue
        existing = owners_by_value.get(id(resource.value))
        if existing is not None and existing is not resource:
            raise RuntimeCompositionError("RUNTIME_RESOURCE_OWNERSHIP_AMBIGUOUS")
        owners_by_value[id(resource.value)] = resource


def _runtime_resources(
    dependencies: RuntimeDependencies,
) -> tuple[RuntimeResource[object], ...]:
    return (
        dependencies.persistence,
        dependencies.providers.trading.resource,
        dependencies.providers.activities.resource,
        dependencies.providers.option_contracts.resource,
        dependencies.providers.option_snapshots.resource,
        dependencies.providers.stock_market_data.resource,
        dependencies.model_transport,
        dependencies.mcp_research,
    )


def _require_async_methods(value: object, *method_names: str) -> None:
    for method_name in method_names:
        method = getattr(value, method_name, None)
        if not callable(method) or not _is_async_callable(method):
            raise RuntimeCompositionError(f"RUNTIME_ASYNC_DEPENDENCY_INVALID:{method_name}")


def _is_async_callable(value: object) -> bool:
    call_method = next(
        (base.__dict__["__call__"] for base in type(value).__mro__ if "__call__" in base.__dict__),
        None,
    )
    return inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(call_method)


def _validate_fingerprint(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _FINGERPRINT_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeCompositionError("ACCOUNT_FINGERPRINT_INVALID")
