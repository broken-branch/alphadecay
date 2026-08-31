from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select

from backend.app.alpaca.execution_evidence import (
    LifecycleAccountEvidence,
    LifecycleOptionEvidence,
)
from backend.app.alpaca.market_data import NormalizedLifecycleMarketEvidence
from backend.app.alpaca.mcp import MCPResearchAudit, MCPResearchResult
from backend.app.contracts.v1 import (
    AccountRole,
    EvidenceClassification,
    EvidenceRelation,
)
from backend.app.execution import Actor, ExecutionBlocked
from backend.app.lifecycle.composition import build_lifecycle_adapters
from backend.app.persistence.agent_repository import AgentDecisionRepository
from backend.app.persistence.agent_service_repository import SQLAlchemyAgentServiceRepository
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AlpacaMarketSessionRow,
    AssessmentCertificateRow,
    CompetitionEntryBudgetRow,
    ExecutionIntentRow,
    LifecycleAccountObservationRow,
    LifecycleObservationBindingRow,
)
from backend.app.services import (
    AcquisitionFailure,
    AgentRunService,
    ObservedPaperAccountAuthority,
)
from backend.tests.execution_lineage.test_lifecycle_repository import (
    repository as sql_repository,
)
from backend.tests.runtime_composition.test_development_acquisition import (
    NOW,
    TICK_ID,
    authority,
    context,
    observation,
)


@dataclass
class Repository:
    calls: list[tuple[str, object]] = field(default_factory=list)

    def load(self, _authority):
        self.calls.append(("load", None))
        return context()

    def persist_account_observation(self, **values: object) -> None:
        self.calls.append(("account", values))

    def persist_market_session(self, **values: object) -> None:
        self.calls.append(("market", values))

    def persist_research_sources(self, *values: object) -> None:
        self.calls.append(("research", values))

    def persist(self, **values: object) -> None:
        self.calls.append(("manifest", values))


class Accounts:
    def collect(self, *, context, trusted_at: datetime) -> LifecycleAccountEvidence:
        observed = observation()
        options = tuple(
            LifecycleOptionEvidence(
                symbol=item.symbol,
                signed_quantity=item.signed_quantity,
                multiplier=item.multiplier,
                bid_price=item.bid_price,
                ask_price=item.ask_price,
                delta=item.delta,
                gamma=item.gamma,
                theta_per_day=item.theta_per_day,
                vega_per_iv_point=item.vega_per_iv_point,
                feed=item.feed,
                source_timestamp=item.quote_observed_at,
                retrieved_at=item.retrieved_at,
                source_hash=item.source_hash,
            )
            for item in observed.options
        )
        return LifecycleAccountEvidence(observed.sweep, options)


class Markets:
    def collect(self, *, context, trusted_at: datetime) -> NormalizedLifecycleMarketEvidence:
        observed = observation()
        return NormalizedLifecycleMarketEvidence(
            observed.underlying,
            observed.atm_iv,
            observed.boundaries,
        )


@dataclass
class MCP:
    entered: int = 0
    calls: int = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def call(self, tool_name: str, arguments: dict[str, object]) -> MCPResearchResult:
        self.calls += 1
        summary = {
            "id": "dividend-1",
            "type": "CASH_DIVIDEND",
            "announced_at": "2026-08-31T14:59:00Z",
            "ex_date": "2026-09-04",
        }
        if tool_name == "get_news":
            data = {"news": []}
        elif tool_name == "get_corporate_actions":
            data = {"corporate_actions": [summary]}
        else:
            data = {
                "corporate_action": {
                    **summary,
                    "headline": "NVDA declares a cash dividend.",
                    "source_tier": "PRIMARY",
                    "independent_reporting_group": None,
                }
            }
        argument_hash = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        result_hash = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return MCPResearchResult(
            tool_name=tool_name,
            data=data,
            audit=MCPResearchAudit(
                tool_name=tool_name,
                argument_hash=argument_hash,
                started_at=NOW,
                completed_at=NOW,
                result_summary_hash=result_hash,
                quality="COMPLETE",
            ),
        )


class Classifier:
    def classify(self, thesis, clusters):
        assert len(clusters) == 1
        cluster = clusters[0]
        return (
            EvidenceClassification(
                cluster_id=cluster.cluster_id,
                source_ids=cluster.source_ids,
                event_code="CAPITAL",
                relation=EvidenceRelation.SUPPORTS,
                materiality=1,
                relevance=Decimal("0.50"),
                confidence=Decimal("0.90"),
                source_tier=cluster.source_tier,
                independent_reporting_group=cluster.independent_reporting_group,
            ),
        )


class ClosingClassifier:
    def classify(self, thesis, clusters):
        assert len(clusters) == 1
        cluster = clusters[0]
        return (
            EvidenceClassification(
                cluster_id=cluster.cluster_id,
                source_ids=cluster.source_ids,
                event_code="CAPITAL",
                relation=EvidenceRelation.CONTRADICTS,
                materiality=3,
                relevance=Decimal("1"),
                confidence=Decimal("1"),
                source_tier=cluster.source_tier,
                independent_reporting_group=cluster.independent_reporting_group,
                invalidates=True,
                invalidation_condition_id="GUIDANCE_REVERSED",
            ),
        )


class FixedAccountAuthority:
    def observe(self):
        return ObservedPaperAccountAuthority(
            AccountRole.DEVELOPMENT,
            context().account_fingerprint,
            True,
            True,
        )


class FixedClock:
    def now(self, _session=None):
        return NOW


class ForbiddenCalibration:
    def binding_for(self, _authority):
        raise AssertionError("development lifecycle must not read calibration")


@dataclass
class ZeroWriteExecution:
    calls: list[tuple[UUID, Actor, datetime]] = field(default_factory=list)

    def execute(self, intent_id: UUID, actor: Actor, now: datetime):
        self.calls.append((intent_id, actor, now))
        raise ExecutionBlocked("ZERO_WRITE_RUNTIME_SEAM")


@dataclass
class Runtime:
    execution: ZeroWriteExecution


@dataclass
class MissingManifestAcquisition:
    target: object

    async def acquire(self, *args, **kwargs):
        acquired = await self.target.acquire(*args, **kwargs)
        return replace(
            acquired,
            values=replace(acquired.values, acquisition_manifest_id=UUID(int=999999)),
        )


@dataclass
class TimelineState:
    trusted_at: datetime = NOW
    stale_options: bool = False
    account_calls: int = 0
    market_calls: int = 0
    classifier_calls: int = 0


@dataclass
class TimelineClock:
    trusted_at: datetime = NOW

    def now(self, _session=None):
        return self.trusted_at


@dataclass
class TimelineAccounts:
    state: TimelineState

    def collect(self, *, context, trusted_at: datetime) -> LifecycleAccountEvidence:
        self.state.trusted_at = trusted_at
        self.state.account_calls += 1
        observed = _timeline_observation(
            trusted_at,
            stale_options=self.state.stale_options,
        )
        options = tuple(
            LifecycleOptionEvidence(
                symbol=item.symbol,
                signed_quantity=item.signed_quantity,
                multiplier=item.multiplier,
                bid_price=item.bid_price,
                ask_price=item.ask_price,
                delta=item.delta,
                gamma=item.gamma,
                theta_per_day=item.theta_per_day,
                vega_per_iv_point=item.vega_per_iv_point,
                feed=item.feed,
                source_timestamp=item.quote_observed_at,
                retrieved_at=item.retrieved_at,
                source_hash=item.source_hash,
            )
            for item in observed.options
        )
        return LifecycleAccountEvidence(observed.sweep, options)


@dataclass
class TimelineMarkets:
    state: TimelineState

    def collect(self, *, context, trusted_at: datetime) -> NormalizedLifecycleMarketEvidence:
        self.state.market_calls += 1
        observed = _timeline_observation(trusted_at)
        return NormalizedLifecycleMarketEvidence(
            observed.underlying,
            observed.atm_iv,
            observed.boundaries,
        )


@dataclass
class TimelineMCP:
    state: TimelineState
    calls: int = 0
    entered: int = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def call(self, tool_name: str, arguments: dict[str, object]) -> MCPResearchResult:
        self.calls += 1
        trusted_at = self.state.trusted_at
        suffix = trusted_at.strftime("%H%M")
        action = {
            "id": f"dividend-{suffix}",
            "type": "CASH_DIVIDEND",
            "announced_at": (trusted_at - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            "ex_date": "2026-09-04",
        }
        if tool_name == "get_news":
            news = []
            if trusted_at >= NOW + timedelta(minutes=10):
                news.append(
                    {
                        "id": f"guidance-withdrawn-{suffix}",
                        "headline": "Issuer withdrew its prior guidance.",
                        "published_at": (trusted_at - timedelta(minutes=1))
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "source_tier": "PRIMARY",
                        "independent_reporting_group": None,
                    }
                )
            data = {"news": news}
        elif tool_name == "get_corporate_actions":
            data = {"corporate_actions": [action]}
        else:
            data = {
                "corporate_action": {
                    **action,
                    "headline": "NVDA declares a cash dividend.",
                    "source_tier": "PRIMARY",
                    "independent_reporting_group": None,
                }
            }
        argument_hash = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        result_hash = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return MCPResearchResult(
            tool_name=tool_name,
            data=data,
            audit=MCPResearchAudit(
                tool_name=tool_name,
                argument_hash=argument_hash,
                started_at=trusted_at - timedelta(seconds=1),
                completed_at=trusted_at,
                result_summary_hash=result_hash,
                quality="COMPLETE",
            ),
        )


@dataclass
class TimelineClassifier:
    state: TimelineState

    def classify(self, thesis, clusters):
        self.state.classifier_calls += 1
        return tuple(
            EvidenceClassification(
                cluster_id=item.cluster_id,
                source_ids=item.source_ids,
                event_code="GUIDANCE" if "withdrew" in item.headline else "CAPITAL",
                relation=(
                    EvidenceRelation.CONTRADICTS
                    if "withdrew" in item.headline
                    else EvidenceRelation.SUPPORTS
                ),
                materiality=3 if "withdrew" in item.headline else 1,
                relevance=Decimal("1") if "withdrew" in item.headline else Decimal("0.50"),
                confidence=Decimal("1") if "withdrew" in item.headline else Decimal("0.90"),
                source_tier=item.source_tier,
                independent_reporting_group=item.independent_reporting_group,
                invalidates="withdrew" in item.headline,
                invalidation_condition_id=(
                    "GUIDANCE_REVERSED" if "withdrew" in item.headline else None
                ),
            )
            for item in clusters
        )


def _timeline_hash(label: str, trusted_at: datetime) -> str:
    return hashlib.sha256(f"{label}:{trusted_at.isoformat()}".encode()).hexdigest()


def _timeline_observation(
    trusted_at: datetime,
    *,
    stale_options: bool = False,
):
    observed = observation()
    shift = trusted_at - NOW
    shifted_sweep = replace(
        observed.sweep,
        retrieval_started_at=observed.sweep.retrieval_started_at + shift,
        retrieval_completed_at=observed.sweep.retrieval_completed_at + shift,
        activity_pagination=replace(
            observed.sweep.activity_pagination,
            requested_end=observed.sweep.activity_pagination.requested_end + shift,
            retrieved_through=observed.sweep.activity_pagination.retrieved_through + shift,
            established_at=observed.sweep.activity_pagination.established_at + shift,
            visibility_complete_through=(
                observed.sweep.activity_pagination.visibility_complete_through + shift
            ),
        ),
        first_account=replace(
            observed.sweep.first_account,
            observed_at=observed.sweep.first_account.observed_at + shift,
        ),
        final_account=replace(
            observed.sweep.final_account,
            observed_at=observed.sweep.final_account.observed_at + shift,
        ),
    )
    option_shift = timedelta(0) if stale_options else shift
    shifted_options = tuple(
        replace(
            item,
            quote_observed_at=item.quote_observed_at + option_shift,
            greek_observed_at=item.greek_observed_at + option_shift,
            retrieved_at=item.retrieved_at + option_shift,
            source_hash=_timeline_hash(f"option:{item.symbol}", trusted_at),
        )
        for item in observed.options
    )
    shifted_underlying = replace(
        observed.underlying,
        quote_observed_at=observed.underlying.quote_observed_at + shift,
        quote_retrieved_at=observed.underlying.quote_retrieved_at + shift,
        quote_source_hash=_timeline_hash("underlying-quote", trusted_at),
        completed_bar_at=observed.underlying.completed_bar_at + shift,
        completed_bar_source_hash=_timeline_hash("underlying-bar", trusted_at),
        request_hash=_timeline_hash("market-request", trusted_at),
        benchmark_completed_bar_at=observed.underlying.benchmark_completed_bar_at + shift,
        benchmark_completed_bar_source_hash=_timeline_hash("benchmark-bar", trusted_at),
    )
    shifted_atm = replace(
        observed.atm_iv,
        observed_at=observed.atm_iv.observed_at + shift,
        retrieved_at=observed.atm_iv.retrieved_at + shift,
        source_hash=_timeline_hash("atm", trusted_at),
        request_hash=_timeline_hash("atm-request", trusted_at),
        call_source_hash=_timeline_hash("atm-call", trusted_at),
        put_source_hash=_timeline_hash("atm-put", trusted_at),
    )
    shifted_points = tuple(
        replace(
            item,
            completed_bar_at=item.completed_bar_at + shift,
            underlying_bar_source_hash=(
                shifted_underlying.completed_bar_source_hash
                if index == len(observed.boundaries.price_confirmation) - 1
                else _timeline_hash(f"confirmation-underlying:{index}", trusted_at)
            ),
            benchmark_bar_source_hash=(
                shifted_underlying.benchmark_completed_bar_source_hash
                if index == len(observed.boundaries.price_confirmation) - 1
                else _timeline_hash(f"confirmation-benchmark:{index}", trusted_at)
            ),
        )
        for index, item in enumerate(observed.boundaries.price_confirmation)
    )
    shifted_boundaries = replace(
        observed.boundaries,
        observed_at=observed.boundaries.observed_at + shift,
        source_hash=_timeline_hash("boundaries", trusted_at),
        price_confirmation=shifted_points,
    )
    return replace(
        observed,
        sweep=shifted_sweep,
        underlying=shifted_underlying,
        options=shifted_options,
        atm_iv=shifted_atm,
        boundaries=shifted_boundaries,
    )


def test_factory_runs_one_development_only_zero_write_acquisition() -> None:
    repository = Repository()
    mcp = MCP()
    adapters = build_lifecycle_adapters(
        repository=repository,
        accounts=Accounts(),
        markets=Markets(),
        mcp_research=mcp,
        classifier=Classifier(),
    )

    result = asyncio.run(
        adapters.acquisition.acquire(authority(), NOW, TICK_ID, actor=Actor.SCHEDULER)
    )

    assert adapters.repository is repository
    assert result.proposal is None
    assert [name for name, _ in repository.calls] == [
        "load",
        "account",
        "market",
        "research",
        "manifest",
    ]
    assert mcp.entered == 1
    assert mcp.calls == 3
    research_call = next(value for name, value in repository.calls if name == "research")
    assert isinstance(research_call, tuple)
    sources = research_call[1]
    assert len(sources) == 1
    assert sources[0].logical_source_id == "dividend-1"
    assert sources[0].source_kind == "MCP_CORPORATE_ACTION"
    assert len(sources[0].request_hash) == 64
    assert len(sources[0].result_hash) == 64
    assert len(sources[0].source_hash) == 64


def test_factory_rejects_cross_role_retained_context() -> None:
    repository = Repository()
    mcp = MCP()
    adapters = build_lifecycle_adapters(
        repository=repository,
        accounts=Accounts(),
        markets=Markets(),
        mcp_research=mcp,
        classifier=Classifier(),
    )

    with pytest.raises(AcquisitionFailure, match="RETAINED_CONTEXT_INVALID"):
        asyncio.run(
            adapters.acquisition.acquire(
                authority(AccountRole.SUBMISSION),
                datetime(2026, 8, 31, 15, tzinfo=UTC),
                UUID(int=999),
                actor=Actor.SCHEDULER,
            )
        )
    assert repository.calls == [("load", None)]
    assert mcp.entered == 0
    assert mcp.calls == 0


def test_scheduler_persists_and_dispatches_production_close_intent_without_provider_write() -> None:
    lifecycle_repository, sessions, engine = sql_repository()
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=context().account_fingerprint,
                equity=Decimal("100000"),
                autonomous_enabled=True,
            )
        )
        session.add(CompetitionEntryBudgetRow(account_role=AccountRole.DEVELOPMENT.value))
    adapters = build_lifecycle_adapters(
        repository=lifecycle_repository,
        accounts=Accounts(),
        markets=Markets(),
        mcp_research=MCP(),
        classifier=ClosingClassifier(),
    )
    decisions = SQLAlchemyAgentServiceRepository(
        AgentDecisionRepository(
            sessions,
            database_clock=FixedClock(),
            server_autonomy_enabled=True,
        ),
        server_autonomy_enabled=True,
    )
    execution = ZeroWriteExecution()
    service = AgentRunService(
        account_authority=FixedAccountAuthority(),
        clock=FixedClock(),
        calibration=ForbiddenCalibration(),
        acquisition=adapters.acquisition,
        decisions=decisions,
        runtime=Runtime(execution),
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.decision.code == "CLOSE_RISK_ONLY"
    assert result.terminal_code == "EXECUTION_BLOCKED"
    assert result.approved_intent_id is not None
    assert execution.calls == [(result.approved_intent_id, Actor.SCHEDULER, NOW)]
    with sessions() as session:
        certificate = session.scalar(select(AssessmentCertificateRow))
        intent = session.scalar(select(ExecutionIntentRow))
        assert certificate is not None
        assert certificate.assessment_id == result.decision.lifecycle.response.assessment_id
        assert certificate.agent_decision_id is not None
        assert intent is not None
        assert intent.intent_id == result.approved_intent_id
        assert intent.assessment_certificate_id == certificate.certificate_id
        assert intent.state == "APPROVED"
        binding = session.scalar(select(LifecycleObservationBindingRow))
        assert binding is not None
        decision = session.get(AgentDecisionRow, certificate.agent_decision_id)
        assert decision is not None
        assert binding.agent_input_snapshot_id == decision.input_snapshot_id
    engine.dispose()


def test_accelerated_market_timeline_recovers_from_stale_data_and_dispatches_once() -> None:
    lifecycle_repository, sessions, engine = sql_repository()
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=context().account_fingerprint,
                equity=Decimal("100000"),
                autonomous_enabled=True,
            )
        )
        session.add(CompetitionEntryBudgetRow(account_role=AccountRole.DEVELOPMENT.value))

    state = TimelineState()
    clock = TimelineClock()
    mcp = TimelineMCP(state)
    classifier = TimelineClassifier(state)
    adapters = build_lifecycle_adapters(
        repository=lifecycle_repository,
        accounts=TimelineAccounts(state),
        markets=TimelineMarkets(state),
        mcp_research=mcp,
        classifier=classifier,
    )
    decisions = SQLAlchemyAgentServiceRepository(
        AgentDecisionRepository(
            sessions,
            database_clock=clock,
            server_autonomy_enabled=True,
        ),
        server_autonomy_enabled=True,
    )
    execution = ZeroWriteExecution()
    service = AgentRunService(
        account_authority=FixedAccountAuthority(),
        clock=clock,
        calibration=ForbiddenCalibration(),
        acquisition=adapters.acquisition,
        decisions=decisions,
        runtime=Runtime(execution),
        server_autonomy_enabled=True,
    )

    hold = asyncio.run(service.run(Actor.SCHEDULER))
    assert hold.decision.code == "HOLD_CERTIFIED", hold.decision
    assert hold.terminal_code == "HOLD_CERTIFIED"
    assert hold.approved_intent_id is None

    state.stale_options = True
    clock.trusted_at = NOW + timedelta(minutes=5)
    stale = asyncio.run(service.run(Actor.SCHEDULER))
    assert stale.decision.code == "PROVIDER_FAILURE_NO_ACTION"
    assert stale.decision.provider_failure_code == "OBSERVATION_OPTION_EVIDENCE_STALE"
    assert stale.approved_intent_id is None

    state.stale_options = False
    clock.trusted_at = NOW + timedelta(minutes=10)
    close = asyncio.run(service.run(Actor.SCHEDULER))
    assert close.decision.code == "CLOSE_RISK_ONLY"
    assert close.terminal_code == "EXECUTION_BLOCKED"
    assert close.approved_intent_id is not None
    assert execution.calls == [(close.approved_intent_id, Actor.SCHEDULER, clock.trusted_at)]

    duplicate = asyncio.run(service.run(Actor.SCHEDULER))
    assert duplicate == close
    assert execution.calls == [(close.approved_intent_id, Actor.SCHEDULER, clock.trusted_at)]
    assert state.account_calls == 3
    assert state.market_calls == 2
    assert classifier.state.classifier_calls == 2
    assert mcp.entered == 1
    assert mcp.calls == 6

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentDecisionRow)) == 3
        assert session.scalar(select(func.count()).select_from(AssessmentCertificateRow)) == 1
        assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 1
        assert session.scalar(select(func.count()).select_from(LifecycleObservationBindingRow)) == 2
    engine.dispose()


def test_manifest_binding_failure_rolls_back_decision_certificate_and_intent() -> None:
    lifecycle_repository, sessions, engine = sql_repository()
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=context().account_fingerprint,
                equity=Decimal("100000"),
                autonomous_enabled=True,
            )
        )
        session.add(CompetitionEntryBudgetRow(account_role=AccountRole.DEVELOPMENT.value))
    adapters = build_lifecycle_adapters(
        repository=lifecycle_repository,
        accounts=Accounts(),
        markets=Markets(),
        mcp_research=MCP(),
        classifier=ClosingClassifier(),
    )
    decisions = SQLAlchemyAgentServiceRepository(
        AgentDecisionRepository(
            sessions,
            database_clock=FixedClock(),
            server_autonomy_enabled=True,
        ),
        server_autonomy_enabled=True,
    )
    service = AgentRunService(
        account_authority=FixedAccountAuthority(),
        clock=FixedClock(),
        calibration=ForbiddenCalibration(),
        acquisition=MissingManifestAcquisition(adapters.acquisition),
        decisions=decisions,
        runtime=Runtime(ZeroWriteExecution()),
        server_autonomy_enabled=True,
    )

    with pytest.raises(ExecutionBlocked, match="LIFECYCLE_INPUT_BINDING_INVALID"):
        asyncio.run(service.run(Actor.SCHEDULER))

    with sessions() as session:
        assert session.scalar(select(AgentInputSnapshotRow)) is None
        assert session.scalar(select(AgentDecisionRow)) is None
        assert session.scalar(select(AssessmentCertificateRow)) is None
        assert session.scalar(select(ExecutionIntentRow)) is None
        assert session.scalar(select(LifecycleObservationBindingRow)) is None
    engine.dispose()


def test_sql_observation_sink_is_idempotent_before_manifest_persistence() -> None:
    repository, sessions, engine = sql_repository()
    retained = context()
    observed = observation()
    market = NormalizedLifecycleMarketEvidence(
        observed.underlying,
        observed.atm_iv,
        observed.boundaries,
    )

    for _ in range(2):
        repository.persist_account_observation(
            context=retained,
            sweep=observed.sweep,
            trusted_at=NOW,
        )
        repository.persist_market_session(
            context=retained,
            evidence=market,
            trusted_at=NOW,
        )
    repository.persist(
        context=retained,
        observation=observed,
        clusters=(),
        classifications=(),
        manifest_id=UUID(int=998),
        manifest_hash="e" * 64,
        trusted_at=NOW,
    )

    with sessions() as session:
        account_rows = session.scalar(
            select(func.count()).select_from(LifecycleAccountObservationRow)
        )
        session_rows = session.scalar(select(func.count()).select_from(AlpacaMarketSessionRow))
        assert account_rows == 1
        assert session_rows == 1
    engine.dispose()
