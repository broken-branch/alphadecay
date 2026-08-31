from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.trading.models import Calendar
from alpaca.trading.requests import GetCalendarRequest

from backend.app.alpaca.execution_evidence import (
    AlpacaExecutionReadCollector,
    AlpacaLifecycleAccountCollector,
    AlpacaOptionContractCollector,
    IndicativeGreekCollector,
)
from backend.app.alpaca.market_data import (
    AlpacaLifecycleMarketDataCollector,
    FrozenCompetitionBoundaryAuthority,
)
from backend.app.alpaca.opportunity import AlpacaOpportunitySnapshotCollector
from backend.app.alpaca.opportunity_catalyst import (
    BoundOpportunityCatalystClassifier,
    RetainedMCPNewsCatalystResearch,
    bind_catalyst_classification_context,
)
from backend.app.alpaca.opportunity_halt_stream import alpaca_trading_status_codebook
from backend.app.alpaca.opportunity_runtime import (
    OpportunityCatalystRuntimeAdapter,
    OpportunityHaltRuntimeAdapter,
    OpportunitySignalRuntimeAdapter,
    OpportunitySnapshotRuntimeAdapter,
)
from backend.app.alpaca.opportunity_signals import AlpacaOpportunitySignalCollector
from backend.app.alpaca.trading import AlpacaTradingReadAdapter
from backend.app.config import RuntimeRole, Settings
from backend.app.contracts.v1 import AccountRole, DataQuality
from backend.app.execution import Actor, ExecutionBlocked
from backend.app.lifecycle.composition import build_lifecycle_adapters
from backend.app.lifecycle.materialization import SQLAlchemyEntryMaterializer
from backend.app.lifecycle.terminal_materialization import (
    SQLAlchemyLifecycleTerminalMaterializer,
)
from backend.app.persistence import (
    RuntimePersistence,
    SQLAlchemyAgentServiceRepository,
    create_runtime_persistence,
)
from backend.app.persistence.opportunity_authority import (
    SQLAlchemyOpportunityAuthorityRepository,
)
from backend.app.persistence.opportunity_runtime import (
    SQLAlchemyOpportunityGreekAuthorityAdapter,
    SQLAlchemyOpportunityHistoryAdapter,
    SQLAlchemyOpportunityObservationAdapter,
    SQLAlchemyOpportunityPlanAdapter,
    SQLAlchemyOpportunityPriorDecisionAdapter,
    SQLAlchemyOpportunityThesisAdapter,
)
from backend.app.provider_settings import (
    CredentialCodec,
    OwnerModelTransportResolver,
    OwnerProviderSettingsService,
    SQLAlchemyProviderSettingsRepository,
)
from backend.app.services import (
    AgentAcquisitionPort,
    AgentRunService,
    CalibrationBinding,
    DevelopmentAcquisitionRouter,
    ObservedPaperAccountAuthority,
)
from backend.app.services.opportunity_catalyst import BoundedOpportunityCatalystAuthority
from backend.app.services.opportunity_composition import (
    OpportunityPlanAuthority,
    ProductionOpportunityComposer,
)
from backend.app.services.opportunity_halt_authority import (
    HaltAuthorityConfig,
    HaltSequenceAuthority,
    halt_authority_config_digest,
)

from .composition import (
    PAPER_TRADING_ENDPOINT,
    RuntimeAccountConfig,
    RuntimeComposition,
    RuntimeCompositionError,
    RuntimeDependencies,
    RuntimeResource,
    build_runtime,
)
from .providers import (
    OpportunityHaltAuthority,
    ProductionResources,
    build_production_opportunity_halt_resource,
    build_production_resources,
)

_NEW_YORK = ZoneInfo("America/New_York")


class RuntimeAccountAuthority:
    def __init__(
        self,
        persistence: RuntimePersistence,
        resources: ProductionResources,
        role: AccountRole,
    ) -> None:
        self._persistence = persistence
        self._resources = resources
        self._role = role

    def observe(self) -> ObservedPaperAccountAuthority:
        durable = self._persistence.agent_repository.get_account_authority(
            self._role,
            account_fingerprint=self._resources.account_fingerprint,
        )
        account = AlpacaTradingReadAdapter(
            self._resources.providers.trading.resource.value,
            account_role=self._role,
            expected_account_fingerprint=durable.account_fingerprint,
            baseline_status=DataQuality.UNKNOWN,
            autonomous_enabled=durable.autonomous_enabled,
        ).get_account()
        if account.role is not self._role or account.paper is not True:
            raise RuntimeCompositionError("OBSERVED_ACCOUNT_AUTHORITY_MISMATCH")
        return ObservedPaperAccountAuthority(
            self._role,
            durable.account_fingerprint,
            True,
            durable.autonomous_enabled,
        )

    def set_autonomous_enabled(
        self,
        enabled: bool,
        actor: Actor,
    ) -> ObservedPaperAccountAuthority:
        if enabled:
            self.observe()
        self._persistence.repository.set_autonomous_enabled(
            self._role,
            enabled,
            actor=actor,
        )
        return ObservedPaperAccountAuthority(
            self._role,
            self._resources.account_fingerprint,
            True,
            enabled,
        )


@dataclass(frozen=True)
class AccountAutonomyStatus:
    role: AccountRole
    server_enabled: bool
    account_enabled: bool
    effective: bool


class AccountAutonomyService:
    def __init__(
        self,
        authority: RuntimeAccountAuthority,
        *,
        server_enabled: bool,
    ) -> None:
        self._authority = authority
        self._server_enabled = server_enabled

    def enable(self, actor: Actor) -> AccountAutonomyStatus:
        if not self._server_enabled:
            raise ExecutionBlocked("SERVER_AUTONOMY_DISABLED")
        return self._status(self._authority.set_autonomous_enabled(True, actor))

    def disable(self, actor: Actor) -> AccountAutonomyStatus:
        return self._status(self._authority.set_autonomous_enabled(False, actor))

    def status(self) -> AccountAutonomyStatus:
        return self._status(self._authority.observe())

    def _status(
        self,
        authority: ObservedPaperAccountAuthority,
    ) -> AccountAutonomyStatus:
        account_enabled = authority.persistent_autonomy_enabled
        return AccountAutonomyStatus(
            role=authority.role,
            server_enabled=self._server_enabled,
            account_enabled=account_enabled,
            effective=self._server_enabled and account_enabled,
        )


@dataclass(frozen=True, init=False)
class SettingsCalibrationBinding:
    _DECISION_CODE = "CALIBRATION_BINDING_NO_TRADE"

    _policy_hash: str
    _calibration_hash: str
    _boundary: datetime
    _sealed_at: datetime

    def __init__(self, settings: Settings) -> None:
        object.__setattr__(self, "_policy_hash", settings.app_policy_hash.get_secret_value())
        object.__setattr__(
            self, "_calibration_hash", settings.app_calibration_hash.get_secret_value()
        )
        object.__setattr__(self, "_boundary", settings.app_calibration_decision_boundary)
        object.__setattr__(self, "_sealed_at", settings.app_calibration_sealed_at)

    def binding_for(self, authority: ObservedPaperAccountAuthority) -> CalibrationBinding:
        if authority.role is not AccountRole.SUBMISSION:
            raise ValueError("CALIBRATION_BINDING_AUTHORITY_MISMATCH")
        material = json.dumps(
            {
                "domain": "alphadecay.calibration-machine-binding.v1",
                "account_role": authority.role.value,
                "account_fingerprint": authority.account_fingerprint,
                "decision_code": self._DECISION_CODE,
                "policy_hash": self._policy_hash,
                "calibration_hash": self._calibration_hash,
                "decision_boundary": self._boundary.isoformat(),
                "sealed_at": self._sealed_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return CalibrationBinding(
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=authority.account_fingerprint,
            decision_code=self._DECISION_CODE,
            machine_binding_hash=hashlib.sha256(material).hexdigest(),
            calibration_hash=self._calibration_hash,
            decision_boundary=self._boundary,
            sealed_at=self._sealed_at,
        )


@dataclass
class ProductionAgent:
    service: AgentRunService
    autonomy: AccountAutonomyService
    runtime: RuntimeComposition
    persistence: RuntimePersistence
    resources: ProductionResources
    provider_settings: OwnerProviderSettingsService
    opportunity_halt: RuntimeResource[OpportunityHaltAuthority] | None = None

    def __post_init__(self) -> None:
        self._close_lock = asyncio.Lock()
        self._closed = False

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            errors: list[BaseException] = []
            try:
                self.provider_settings.close()
            except BaseException as error:
                errors.append(error)
            if self.opportunity_halt is not None and self.opportunity_halt.close is not None:
                try:
                    self.opportunity_halt.close()
                except BaseException as error:
                    errors.append(error)
            try:
                await self.runtime.aclose()
            except BaseException as error:
                errors.append(error)
            if errors:
                raise BaseExceptionGroup("PRODUCTION_AGENT_CLEANUP_FAILED", errors)


@dataclass(frozen=True)
class ProductionOpportunityWiring:
    acquisition: AgentAcquisitionPort
    halt: RuntimeResource[OpportunityHaltAuthority]


@dataclass(frozen=True)
class ProductionOpportunityAuthority:
    plans: SQLAlchemyOpportunityPlanAdapter
    plan: OpportunityPlanAuthority
    maximum_trade_age: timedelta


async def build_production_agent(
    settings: Settings,
    migrations_directory: Path,
    *,
    persistence_factory: Callable[..., RuntimePersistence] = create_runtime_persistence,
    resources_factory: Callable[[Settings], ProductionResources] = build_production_resources,
    runtime_factory: Callable[..., RuntimeComposition] = build_runtime,
    opportunity_factory: Callable[..., Awaitable[ProductionOpportunityWiring]] | None = None,
) -> ProductionAgent:
    if (
        settings.alpaca_api_endpoint != PAPER_TRADING_ENDPOINT
        or settings.alpaca_paper_trade is not True
    ):
        raise RuntimeCompositionError("PAPER_TRADING_REQUIRED")
    role = _account_role(settings.app_account_role)
    persistence = persistence_factory(
        settings.database_url.get_secret_value(),
        migrations_directory,
        entry_limits=settings.entry_budget_limits(),
        server_autonomy_enabled=settings.app_autonomous_enabled,
    )
    resources: ProductionResources | None = None
    runtime: RuntimeComposition | None = None
    opportunity_halt: RuntimeResource[OpportunityHaltAuthority] | None = None
    try:
        opportunity_runtime = _load_production_opportunity_authority(
            settings,
            persistence,
            role=role,
        )
        provider_settings_repository = SQLAlchemyProviderSettingsRepository(
            persistence.sessions,
            codec=CredentialCodec(
                settings.app_provider_settings_secret.get_secret_value().encode("utf-8")
            ),
            allowed_openai_origins=settings.openai_compatible_origins(),
        )
        provider_settings = OwnerProviderSettingsService(
            provider_settings_repository,
            clock=persistence.database_clock.now,
        )
        resources = resources_factory(settings)
        selectable_model = OwnerModelTransportResolver(
            provider_settings_repository,
            resources.model_transport.value,
        )
        persistence.repository.register_account(
            role=role,
            fingerprint=resources.account_fingerprint,
            equity=resources.observed_equity,
            autonomous_enabled=False,
        )
        account_authority = RuntimeAccountAuthority(persistence, resources, role)
        account_authority.observe()
        runtime = runtime_factory(
            RuntimeAccountConfig(
                role=role,
                endpoint=PAPER_TRADING_ENDPOINT,
                account_fingerprint=resources.account_fingerprint,
                autonomous_enabled=settings.app_autonomous_enabled,
            ),
            RuntimeDependencies(
                persistence=RuntimeResource.owned(persistence, persistence.close),
                providers=resources.providers,
                model_transport=RuntimeResource.owned(
                    selectable_model,
                    resources.model_transport.close,
                ),
                mcp_research=resources.mcp_research,
                clock=persistence.database_clock.now,
            ),
        )
        lifecycle_repository = persistence.lifecycle_repository
        if lifecycle_repository is None:
            raise RuntimeCompositionError("LIFECYCLE_PERSISTENCE_REQUIRED")
        decisions = SQLAlchemyAgentServiceRepository(
            persistence.agent_repository,
            server_autonomy_enabled=settings.app_autonomous_enabled,
        )
        trading_reads = AlpacaExecutionReadCollector(
            resources.providers.trading.resource.value,
            account_role=role,
            expected_account_fingerprint=resources.account_fingerprint,
            paper=True,
            clock=persistence.database_clock.now,
        )
        option_contracts = AlpacaOptionContractCollector(
            resources.providers.option_contracts.resource.value
        )
        greeks = IndicativeGreekCollector(
            resources.providers.option_snapshots.resource.value,
            option_contracts,
            clock=persistence.database_clock.now,
        )
        lifecycle = build_lifecycle_adapters(
            repository=lifecycle_repository,
            accounts=AlpacaLifecycleAccountCollector(
                trading_reads,
                resources.providers.activities.resource.value,
                greeks,
                clock=persistence.database_clock.now,
            ),
            markets=AlpacaLifecycleMarketDataCollector(
                resources.providers.option_snapshots.resource.value,
                option_contracts,
                resources.providers.stock_market_data.resource.value,
                resources.providers.trading.resource.value,
                FrozenCompetitionBoundaryAuthority(),
                clock=persistence.database_clock.now,
            ),
            mcp_research=runtime.mcp_research,
            classifier=runtime.evidence_classifier,
        )
        acquisition = lifecycle.acquisition
        if opportunity_runtime is not None:
            selected_opportunity_factory = (
                build_production_opportunity_wiring
                if opportunity_factory is None
                else opportunity_factory
            )
            opportunity_wiring = await selected_opportunity_factory(
                settings=settings,
                persistence=persistence,
                resources=resources,
                runtime=runtime,
                lifecycle_acquisition=lifecycle.acquisition,
                authority=opportunity_runtime,
            )
            opportunity_halt = opportunity_wiring.halt
            acquisition = opportunity_wiring.acquisition
        if role is AccountRole.DEVELOPMENT or opportunity_runtime is not None:
            await runtime.mcp_research.__aenter__()
        service = AgentRunService(
            account_authority=account_authority,
            clock=persistence.database_clock,
            calibration=SettingsCalibrationBinding(settings),
            acquisition=acquisition,
            decisions=decisions,
            runtime=runtime,
            server_autonomy_enabled=settings.app_autonomous_enabled,
            submission_opportunity_enabled=settings.app_submission_opportunity_enabled,
            entry_materializer=SQLAlchemyEntryMaterializer(persistence.sessions),
            lifecycle_terminal_materializer=SQLAlchemyLifecycleTerminalMaterializer(
                persistence.sessions
            ),
        )
        autonomy = AccountAutonomyService(
            account_authority,
            server_enabled=settings.app_autonomous_enabled,
        )
        return ProductionAgent(
            service,
            autonomy,
            runtime,
            persistence,
            resources,
            provider_settings,
            opportunity_halt,
        )
    except BaseException as startup_error:
        cleanup_errors: tuple[BaseException, ...]
        if runtime is not None:
            cleanup_errors = await _close_runtime_startup(opportunity_halt, runtime)
        else:
            cleanup_errors = await _close_partial(resources, persistence)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "PRODUCTION_AGENT_STARTUP_AND_CLEANUP_FAILED",
                (startup_error, *cleanup_errors),
            ) from None
        raise


def _account_role(role: RuntimeRole) -> AccountRole:
    return AccountRole(role.value)


async def build_production_opportunity_wiring(
    *,
    settings: Settings,
    persistence: RuntimePersistence,
    resources: ProductionResources,
    runtime: RuntimeComposition,
    lifecycle_acquisition: AgentAcquisitionPort,
    authority: ProductionOpportunityAuthority,
    halt_factory: Callable[..., RuntimeResource[OpportunityHaltAuthority]] = (
        build_production_opportunity_halt_resource
    ),
) -> ProductionOpportunityWiring:
    if (
        _account_role(settings.app_account_role)
        not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION)
        or not isinstance(authority, ProductionOpportunityAuthority)
    ):
        raise RuntimeCompositionError("OPPORTUNITY_RUNTIME_AUTHORITY_REQUIRED")
    account_role = _account_role(settings.app_account_role)
    authority_repository = SQLAlchemyOpportunityAuthorityRepository(
        persistence.sessions,
        account_role=account_role,
    )
    evidence_repository = persistence.opportunity_evidence_repository
    thesis_repository = persistence.opportunity_thesis_repository
    if (
        authority_repository is None
        or evidence_repository is None
        or thesis_repository is None
    ):
        raise RuntimeCompositionError("OPPORTUNITY_PERSISTENCE_REQUIRED")
    plans = authority.plans
    plan = authority.plan
    _validate_plan_account_authority(plan, resources.account_fingerprint, account_role)
    halt_config = _halt_config_for_plan(
        resources.providers.trading.resource.value,
        plan,
        maximum_trade_age=authority.maximum_trade_age,
    )
    halt = halt_factory(
        settings,
        config=halt_config,
        clock=persistence.database_clock.now,
    )
    try:
        catalyst_plan = plan.catalyst_plan
        catalyst = BoundedOpportunityCatalystAuthority(
            RetainedMCPNewsCatalystResearch(runtime.mcp_research),
            BoundOpportunityCatalystClassifier(
                runtime.evidence_classifier,
                bind_catalyst_classification_context(catalyst_plan),
            ),
        )
        option_contracts = AlpacaOptionContractCollector(
            resources.providers.option_contracts.resource.value
        )
        opportunity = ProductionOpportunityComposer(
            plans=plans,
            snapshots=OpportunitySnapshotRuntimeAdapter(
                AlpacaOpportunitySnapshotCollector(
                    resources.providers.trading.resource.value,
                    resources.providers.stock_market_data.resource.value,
                    resources.providers.option_snapshots.resource.value,
                    option_contracts,
                    clock=persistence.database_clock.now,
                )
            ),
            signals=OpportunitySignalRuntimeAdapter(
                AlpacaOpportunitySignalCollector(
                    resources.providers.trading.resource.value,
                    resources.providers.stock_market_data.resource.value,
                )
            ),
            halts=OpportunityHaltRuntimeAdapter(
                halt.value,
                symbol=plan.plan_spec.underlying,
            ),
            catalysts=OpportunityCatalystRuntimeAdapter(catalyst),
            history=SQLAlchemyOpportunityHistoryAdapter(
                authority_repository,
                evidence_repository,
                account_role=account_role,
            ),
            prior_decisions=SQLAlchemyOpportunityPriorDecisionAdapter(
                authority_repository,
                account_role=account_role,
            ),
            greek_authority=SQLAlchemyOpportunityGreekAuthorityAdapter(authority_repository),
            observations=SQLAlchemyOpportunityObservationAdapter(evidence_repository),
            theses=SQLAlchemyOpportunityThesisAdapter(thesis_repository),
        )
        acquisition = DevelopmentAcquisitionRouter(
            authority_repository,
            opportunity,
            lifecycle_acquisition,
        )
    except BaseException as startup_error:
        try:
            if halt.close is not None:
                halt.close()
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "OPPORTUNITY_RUNTIME_STARTUP_AND_CLEANUP_FAILED",
                (startup_error, cleanup_error),
            ) from None
        raise
    return ProductionOpportunityWiring(acquisition, halt)


def _load_production_opportunity_authority(
    settings: Settings,
    persistence: RuntimePersistence,
    *,
    role: AccountRole,
) -> ProductionOpportunityAuthority | None:
    configured = settings.opportunity_authority()
    if configured is None:
        return None
    opportunity_key, opportunity_version, maximum_trade_age = configured
    authority_repository = persistence.opportunity_authority_repository
    evidence_repository = persistence.opportunity_evidence_repository
    thesis_repository = persistence.opportunity_thesis_repository
    if (
        authority_repository is None
        or evidence_repository is None
        or thesis_repository is None
    ):
        raise RuntimeCompositionError("OPPORTUNITY_PERSISTENCE_REQUIRED")
    plans = SQLAlchemyOpportunityPlanAdapter(
        evidence_repository,
        opportunity_key=opportunity_key,
        version=opportunity_version,
        account_role=role,
    )
    plan = plans.load(trusted_at=persistence.database_clock.now())
    _validate_plan_runtime_authority(plan, settings)
    return ProductionOpportunityAuthority(plans, plan, maximum_trade_age)


def _halt_config_for_plan(
    trading: object,
    plan: OpportunityPlanAuthority,
    *,
    maximum_trade_age: timedelta,
) -> HaltAuthorityConfig:
    signal_session = plan.signal_request.signal_session
    get_calendar = getattr(trading, "get_calendar", None)
    if not callable(get_calendar):
        raise RuntimeCompositionError("OPPORTUNITY_CALENDAR_AUTHORITY_UNAVAILABLE")
    try:
        calendars = get_calendar(
            GetCalendarRequest(start=signal_session, end=signal_session)
        )
    except Exception as error:
        raise RuntimeCompositionError("OPPORTUNITY_CALENDAR_AUTHORITY_UNAVAILABLE") from error
    if (
        type(calendars) is not list
        or len(calendars) != 1
        or type(calendars[0]) is not Calendar
        or calendars[0].date != signal_session
    ):
        raise RuntimeCompositionError("OPPORTUNITY_CALENDAR_AUTHORITY_INVALID")
    session_open = _calendar_time(calendars[0].open, signal_session)
    session_close = _calendar_time(calendars[0].close, signal_session)
    decision_boundary = plan.plan_spec.policy.selected_decision_boundary
    last_entry_boundary = plan.plan_spec.policy.last_entry_boundary
    if (
        session_open >= session_close
        or not session_open < decision_boundary <= session_close
        or not decision_boundary < last_entry_boundary <= session_close
    ):
        raise RuntimeCompositionError("OPPORTUNITY_CALENDAR_AUTHORITY_INVALID")
    codebook = alpaca_trading_status_codebook()
    config = HaltAuthorityConfig(
        symbol=plan.plan_spec.underlying,
        feed=codebook.feed,
        sdk_version=codebook.sdk_version,
        session_date=signal_session,
        session_open_at=session_open,
        session_close_at=session_close,
        maximum_trade_age=maximum_trade_age,
        sequence_authority=HaltSequenceAuthority.ADAPTER_RECEIVE_ORDER_V1,
        codebook_hash=codebook.source_hash,
        source_hash="",
    )
    return replace(config, source_hash=halt_authority_config_digest(config))


def _validate_plan_runtime_authority(
    plan: OpportunityPlanAuthority,
    settings: Settings,
) -> None:
    policy = plan.plan_spec.policy
    limits = settings.entry_budget_limits()
    expected_policy_hash = settings.app_policy_hash.get_secret_value()
    if (
        plan.plan.policy_hash != expected_policy_hash
        or limits.policy_hash != expected_policy_hash
        or policy.maximum_lifetime_entries != limits.maximum_lifetime_entries
        or policy.maximum_lifetime_risk != limits.maximum_lifetime_risk
        or policy.maximum_position_loss != limits.maximum_position_loss
        or policy.maximum_quantity != limits.maximum_entry_quantity
        or policy.equity_floor != limits.equity_floor
    ):
        raise RuntimeCompositionError("OPPORTUNITY_POLICY_AUTHORITY_MISMATCH")


def _validate_plan_account_authority(
    plan: OpportunityPlanAuthority,
    account_fingerprint: str,
    account_role: AccountRole = AccountRole.DEVELOPMENT,
) -> None:
    request = plan.plan_spec.request_contract
    if (
        request.account_role is not account_role
        or getattr(plan.plan_spec, "account_role", account_role) is not account_role
        or getattr(getattr(plan, "plan", None), "account_role", account_role)
        is not account_role
        or getattr(plan.baseline_seal, "account_role", account_role) is not account_role
        or getattr(plan.baseline, "account_role", account_role) is not account_role
        or request.expected_account_fingerprint != account_fingerprint
        or plan.baseline_seal.account_fingerprint != account_fingerprint
        or plan.baseline.account_fingerprint != account_fingerprint
    ):
        raise RuntimeCompositionError("OPPORTUNITY_ACCOUNT_AUTHORITY_MISMATCH")


def _calendar_time(value: datetime, expected_session: date) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not None
        or value.utcoffset() is not None
        or value.date() != expected_session
        or value.second != 0
        or value.microsecond != 0
    ):
        raise RuntimeCompositionError("OPPORTUNITY_CALENDAR_AUTHORITY_INVALID")
    return value.replace(tzinfo=_NEW_YORK).astimezone(UTC)


async def _close_runtime_startup(
    opportunity_halt: RuntimeResource[OpportunityHaltAuthority] | None,
    runtime: RuntimeComposition,
) -> tuple[BaseException, ...]:
    errors: list[BaseException] = []
    if opportunity_halt is not None and opportunity_halt.close is not None:
        try:
            opportunity_halt.close()
        except BaseException as error:
            errors.append(error)
    try:
        await runtime.aclose()
    except BaseException as error:
        errors.append(error)
    return tuple(errors)


async def _close_partial(
    resources: ProductionResources | None,
    persistence: RuntimePersistence,
) -> tuple[BaseException, ...]:
    owned: list[RuntimeResource[object]] = [RuntimeResource.owned(persistence, persistence.close)]
    if resources is not None:
        owned.extend(
            (
                resources.providers.trading.resource,
                resources.providers.option_contracts.resource,
                resources.providers.activities.resource,
                resources.providers.option_snapshots.resource,
                resources.providers.stock_market_data.resource,
                resources.model_transport,
                resources.mcp_research,
            )
        )

    unique: list[RuntimeResource[object]] = []
    seen: set[int] = set()
    for resource in owned:
        identity = id(resource)
        if identity not in seen:
            seen.add(identity)
            unique.append(resource)

    errors: list[BaseException] = []
    for resource in reversed(unique):
        try:
            if resource.aclose is not None:
                await resource.aclose()
            elif resource.close is not None:
                resource.close()
        except BaseException as error:
            errors.append(error)
    return tuple(errors)
