from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Protocol

from backend.app.alpaca.opportunity import (
    OpportunityMarketSnapshot,
    OpportunitySnapshotRequest,
    opportunity_account_book_digest,
    opportunity_bar_digest,
    opportunity_market_session_digest,
    opportunity_market_snapshot_digest,
    opportunity_option_digest,
    opportunity_snapshot_request_digest,
)
from backend.app.alpaca.opportunity_signals import (
    CollectedOpportunitySignalEvidence,
    OpportunitySignalRequest,
    opportunity_signal_daily_evidence_digest,
    opportunity_signal_intraday_evidence_digest,
)
from backend.app.policy import OpportunityPolicy
from backend.app.policy.opportunity import opportunity_policy_hash
from backend.app.services.opportunity_catalyst import (
    CatalystAuthorityResult,
    CatalystEvidencePlan,
    catalyst_evidence_plan_digest,
)
from backend.app.services.opportunity_composition import OpportunitySignalEvidence
from backend.app.services.opportunity_halt_authority import (
    HaltAuthoritySnapshot,
    halt_authority_snapshot_digest,
)
from backend.app.services.opportunity_input import CatalystAuthority
from backend.app.services.opportunity_signals import (
    OpportunityDirectionalSignalAuthority,
    calculate_opportunity_signals,
    signal_bar_digest,
    signal_calendar_digest,
    signal_daily_close_digest,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")


class OpportunityRuntimeAdapterError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OpportunitySnapshotCollectorPort(Protocol):
    def collect(
        self, request: OpportunitySnapshotRequest, *, trusted_at: datetime
    ) -> OpportunityMarketSnapshot: ...


class OpportunitySignalCollectorPort(Protocol):
    def collect_evidence(
        self,
        request: OpportunitySignalRequest,
        *,
        policy: OpportunityPolicy,
        snapshot_source_hash: str,
        observed_at: datetime,
    ) -> CollectedOpportunitySignalEvidence: ...


class OpportunityHaltSnapshotPort(Protocol):
    def snapshot(self, *, read_at: datetime | None = None) -> HaltAuthoritySnapshot: ...


class OpportunityCatalystAuthorityPort(Protocol):
    async def produce(
        self,
        *,
        plan: CatalystEvidencePlan,
        policy: OpportunityPolicy,
        trusted_at: datetime,
    ) -> CatalystAuthorityResult: ...


class OpportunitySnapshotRuntimeAdapter:
    def __init__(self, target: OpportunitySnapshotCollectorPort) -> None:
        self._target = target

    def collect(
        self, request: OpportunitySnapshotRequest, *, trusted_at: datetime
    ) -> OpportunityMarketSnapshot:
        snapshot = self._target.collect(request, trusted_at=trusted_at)
        if not _snapshot_valid(snapshot, request, trusted_at):
            raise OpportunityRuntimeAdapterError("OPPORTUNITY_SNAPSHOT_AUTHORITY_INVALID")
        return snapshot


class OpportunitySignalRuntimeAdapter:
    def __init__(self, target: OpportunitySignalCollectorPort) -> None:
        self._target = target

    def collect(
        self,
        request: OpportunitySignalRequest,
        *,
        policy: OpportunityPolicy,
        snapshot_source_hash: str,
        observed_at: datetime,
    ) -> OpportunitySignalEvidence:
        evidence = self._target.collect_evidence(
            request,
            policy=policy,
            snapshot_source_hash=snapshot_source_hash,
            observed_at=observed_at,
        )
        if not _signal_evidence_valid(
            evidence,
            request=request,
            policy=policy,
            snapshot_source_hash=snapshot_source_hash,
            observed_at=observed_at,
        ):
            raise OpportunityRuntimeAdapterError("OPPORTUNITY_SIGNAL_AUTHORITY_INVALID")
        return OpportunitySignalEvidence(
            authority=evidence.authority,
            calendar_hash=evidence.calendar.source_hash,
            daily_hash=evidence.daily_source_hash,
            intraday_hash=evidence.intraday_source_hash,
        )


class OpportunityHaltRuntimeAdapter:
    def __init__(self, target: OpportunityHaltSnapshotPort, *, symbol: str) -> None:
        if type(symbol) is not str or not symbol:
            raise OpportunityRuntimeAdapterError("OPPORTUNITY_HALT_BINDING_INVALID")
        self._target = target
        self._symbol = symbol

    def read(self, *, symbol: str, trusted_at: datetime) -> HaltAuthoritySnapshot:
        if symbol != self._symbol:
            raise OpportunityRuntimeAdapterError("OPPORTUNITY_HALT_BINDING_INVALID")
        snapshot = self._target.snapshot(read_at=trusted_at)
        if (
            type(snapshot) is not HaltAuthoritySnapshot
            or snapshot.symbol != self._symbol
            or snapshot.observed_at < trusted_at
            or snapshot.source_hash != halt_authority_snapshot_digest(snapshot)
        ):
            raise OpportunityRuntimeAdapterError("OPPORTUNITY_HALT_AUTHORITY_INVALID")
        return snapshot


class OpportunityCatalystRuntimeAdapter:
    def __init__(self, target: OpportunityCatalystAuthorityPort) -> None:
        self._target = target

    async def produce(
        self,
        *,
        plan: CatalystEvidencePlan,
        policy: OpportunityPolicy,
        trusted_at: datetime,
    ) -> CatalystAuthorityResult:
        result = await self._target.produce(plan=plan, policy=policy, trusted_at=trusted_at)
        if not _catalyst_result_valid(result, plan, policy, trusted_at):
            raise OpportunityRuntimeAdapterError("OPPORTUNITY_CATALYST_AUTHORITY_INVALID")
        return result


def _snapshot_valid(
    snapshot: object,
    request: OpportunitySnapshotRequest,
    trusted_at: datetime,
) -> bool:
    if type(snapshot) is not OpportunityMarketSnapshot:
        return False
    return (
        snapshot.trusted_at == trusted_at
        and snapshot.request_hash == opportunity_snapshot_request_digest(request)
        and snapshot.account_book.account_fingerprint == request.expected_account_fingerprint
        and snapshot.account_book.source_hash
        == opportunity_account_book_digest(snapshot.account_book)
        and snapshot.session.source_hash == opportunity_market_session_digest(snapshot.session)
        and snapshot.underlying_bar.symbol == request.underlying
        and snapshot.underlying_bar.source_hash == opportunity_bar_digest(snapshot.underlying_bar)
        and snapshot.benchmark_bar.symbol == request.benchmark
        and snapshot.benchmark_bar.source_hash == opportunity_bar_digest(snapshot.benchmark_bar)
        and all(
            option.underlying == request.underlying
            and option.source_hash == opportunity_option_digest(option)
            for option in snapshot.options
        )
        and snapshot.source_hash == opportunity_market_snapshot_digest(snapshot)
    )


def _signal_evidence_valid(
    evidence: object,
    *,
    request: OpportunitySignalRequest,
    policy: OpportunityPolicy,
    snapshot_source_hash: str,
    observed_at: datetime,
) -> bool:
    if type(evidence) is not CollectedOpportunitySignalEvidence:
        return False
    try:
        expected_authority = calculate_opportunity_signals(
            account_role=request.account_role,
            policy=policy,
            snapshot_source_hash=snapshot_source_hash,
            observed_at=observed_at,
            calendar=evidence.calendar,
            underlying_daily_closes=evidence.underlying_daily_closes,
            benchmark_daily_closes=evidence.benchmark_daily_closes,
            first_reaction_close=evidence.first_reaction_close,
            underlying_bars=evidence.underlying_bars,
            benchmark_bars=evidence.benchmark_bars,
        )
    except Exception:
        return False
    daily = (*evidence.underlying_daily_closes, *evidence.benchmark_daily_closes)
    bars = (*evidence.underlying_bars, *evidence.benchmark_bars)
    return (
        type(evidence.authority) is OpportunityDirectionalSignalAuthority
        and evidence.authority == expected_authority
        and evidence.authority.snapshot_source_hash == snapshot_source_hash
        and _valid_hash(evidence.authority.source_hash)
        and evidence.calendar.source_hash == signal_calendar_digest(evidence.calendar)
        and evidence.calendar.signal_session == request.signal_session
        and evidence.calendar.pre_event_cutoff == request.pre_event_cutoff
        and all(item.source_hash == signal_daily_close_digest(item) for item in daily)
        and evidence.first_reaction_close.source_hash
        == signal_daily_close_digest(evidence.first_reaction_close)
        and all(item.source_hash == signal_bar_digest(item) for item in bars)
        and evidence.daily_source_hash
        == opportunity_signal_daily_evidence_digest(
            evidence.underlying_daily_closes,
            evidence.benchmark_daily_closes,
            evidence.first_reaction_close,
        )
        and evidence.intraday_source_hash
        == opportunity_signal_intraday_evidence_digest(
            evidence.underlying_bars,
            evidence.benchmark_bars,
        )
    )


def _catalyst_result_valid(
    result: object,
    plan: CatalystEvidencePlan,
    policy: OpportunityPolicy,
    trusted_at: datetime,
) -> bool:
    if (
        type(result) is not CatalystAuthorityResult
        or type(result.authority) is not CatalystAuthority
    ):
        return False
    return (
        result.plan_hash == plan.plan_hash
        and result.policy_hash == opportunity_policy_hash(policy)
        and result.criteria_hash == catalyst_evidence_plan_digest(plan)
        and result.authority.opportunity_key == plan.opportunity_key
        and result.authority.source_hash == result.authority_hash
        and all(
            _valid_hash(value)
            for value in (
                result.plan_hash,
                result.policy_hash,
                result.criteria_hash,
                result.research_source_hash,
                result.classification_hash,
                result.authority_hash,
            )
        )
        and result.authority.observed_at - trusted_at <= timedelta(seconds=30)
        and trusted_at - result.authority.observed_at <= policy.maximum_catalyst_age
    )


def _valid_hash(value: object) -> bool:
    return type(value) is str and _HASH.fullmatch(value) is not None
