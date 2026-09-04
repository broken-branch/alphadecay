from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.app.alpaca.opportunity import (
    OpportunityMarketSnapshot,
    OpportunitySnapshotError,
    OpportunitySnapshotRequest,
)
from backend.app.alpaca.opportunity_signals import OpportunitySignalRequest
from backend.app.contracts.v1 import AccountRole
from backend.app.domain.option_contract_symbol import NON_STANDARD_CONTRACT_UNSUPPORTED
from backend.app.execution import Actor, FrozenThesisVersion
from backend.app.persistence.opportunity_authority import GreekUnitAuthority
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineSeal,
    OpportunityObservationSpec,
    OpportunityPlanSpec,
    PersistedOpportunityBaseline,
    PersistedOpportunityObservation,
    PersistedOpportunityPlan,
    opportunity_observation_digest,
)
from backend.app.policy import OpportunityOutcome, OpportunityPolicy, evaluate_opportunity
from backend.app.policy.opportunity import TradingHaltState, derive_opportunity_direction
from backend.app.services.acquisition import (
    AcquisitionFailure,
    AcquisitionKind,
    ObservedPaperAccountAuthority,
    OpportunityAcquisition,
    OpportunityNoTradeAcquisition,
)
from backend.app.services.entry_authority import (
    EntryProposalAuthorityInput,
    build_development_entry_proposal,
)
from backend.app.services.opportunity_catalyst import (
    CatalystAuthorityResult,
    CatalystEvidencePlan,
)
from backend.app.services.opportunity_halt_authority import HaltAuthoritySnapshot
from backend.app.services.opportunity_input import (
    AccountBudgetAuthority,
    OpportunitySignalAuthority,
    PriorDecisionAuthority,
    assemble_opportunity_input,
)
from backend.app.services.opportunity_selection import (
    CandidateSelectionAuthority,
    CandidateSelectionResult,
    select_vertical_candidate,
)
from backend.app.services.opportunity_signals import OpportunityDirectionalSignalAuthority
from backend.app.services.opportunity_thesis import (
    OpportunityThesisFactoryInput,
    build_frozen_opportunity_thesis,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROPOSAL_TTL = timedelta(seconds=30)
_Result = TypeVar("_Result")


@dataclass(frozen=True)
class OpportunityPlanAuthority:
    plan_spec: OpportunityPlanSpec
    plan: PersistedOpportunityPlan
    baseline_seal: OpportunityBaselineSeal
    baseline: PersistedOpportunityBaseline
    signal_request: OpportunitySignalRequest
    catalyst_plan: CatalystEvidencePlan
    requested_maximum_quantity: int


@dataclass(frozen=True)
class OpportunitySignalEvidence:
    authority: OpportunityDirectionalSignalAuthority
    calendar_hash: str
    daily_hash: str
    intraday_hash: str


@dataclass(frozen=True)
class OpportunityHistoryEvidence:
    account: AccountBudgetAuthority
    budget_hash: str


class OpportunityPlanPort(Protocol):
    def load(self, *, trusted_at: datetime) -> OpportunityPlanAuthority: ...


class OpportunitySnapshotPort(Protocol):
    def collect(
        self, request: OpportunitySnapshotRequest, *, trusted_at: datetime
    ) -> OpportunityMarketSnapshot: ...


class OpportunitySignalPort(Protocol):
    def collect(
        self,
        request: OpportunitySignalRequest,
        *,
        policy: OpportunityPolicy,
        snapshot_source_hash: str,
        observed_at: datetime,
    ) -> OpportunitySignalEvidence: ...


class OpportunityHaltPort(Protocol):
    def read(self, *, symbol: str, trusted_at: datetime) -> HaltAuthoritySnapshot: ...


class OpportunityCatalystPort(Protocol):
    async def produce(
        self,
        *,
        plan: CatalystEvidencePlan,
        policy: OpportunityPolicy,
        trusted_at: datetime,
    ) -> CatalystAuthorityResult: ...


class OpportunityHistoryPort(Protocol):
    def load(
        self,
        *,
        expected_account_fingerprint: str,
        opportunity_key: str,
        trading_day: date,
        snapshot: OpportunityMarketSnapshot,
        baseline: PersistedOpportunityBaseline,
    ) -> OpportunityHistoryEvidence: ...


class OpportunityPriorDecisionPort(Protocol):
    def load(
        self,
        *,
        expected_account_fingerprint: str,
        opportunity_key: str,
        decision_boundary: datetime,
        as_of: datetime,
    ) -> PriorDecisionAuthority: ...


class OpportunityGreekAuthorityPort(Protocol):
    def load(self, *, effective_at: datetime) -> GreekUnitAuthority: ...


class OpportunityObservationPort(Protocol):
    def append(self, spec: OpportunityObservationSpec) -> PersistedOpportunityObservation: ...


class OpportunityThesisPort(Protocol):
    def persist(self, draft: FrozenThesisVersion) -> FrozenThesisVersion: ...


class _HaltStatusUnknown(RuntimeError):
    """The halt authority has not confirmed the session yet, so the read is retryable evidence."""

    code = "TRADING_HALT_STATUS_UNKNOWN"


_LOGGER = logging.getLogger(__name__)


def _confirmed_halt(snapshot: HaltAuthoritySnapshot) -> HaltAuthoritySnapshot:
    if snapshot.trading_halt_state is TradingHaltState.UNKNOWN:
        _LOGGER.warning(
            "halt authority unknown at read: state=%s observed_at=%s acknowledged_at=%s "
            "last_trade_at=%s last_event_at=%s last_sequence=%s transitions=%s rejected=%s",
            getattr(snapshot.state, "value", snapshot.state),
            snapshot.observed_at.isoformat() if snapshot.observed_at else None,
            snapshot.acknowledged_at.isoformat() if snapshot.acknowledged_at else None,
            snapshot.last_trade_at.isoformat() if snapshot.last_trade_at else None,
            snapshot.last_event_at.isoformat() if snapshot.last_event_at else None,
            snapshot.last_sequence,
            snapshot.transition_count,
            snapshot.last_rejected_event_hash,
        )
        raise _HaltStatusUnknown(_HaltStatusUnknown.code)
    return snapshot


def _unavailable_code(stage: str, error: BaseException) -> str:
    """Name the stage that failed and, when the error carries a code, the underlying cause."""
    cause = getattr(error, "code", None)
    if isinstance(cause, str) and cause and len(cause) <= 96 and cause.replace("_", "").isalnum():
        return f"OPPORTUNITY_{stage}_UNAVAILABLE__{cause}"
    return f"OPPORTUNITY_{stage}_UNAVAILABLE"


class ProductionOpportunityComposer:
    def __init__(
        self,
        *,
        plans: OpportunityPlanPort,
        snapshots: OpportunitySnapshotPort,
        signals: OpportunitySignalPort,
        halts: OpportunityHaltPort,
        catalysts: OpportunityCatalystPort,
        history: OpportunityHistoryPort,
        prior_decisions: OpportunityPriorDecisionPort,
        greek_authority: OpportunityGreekAuthorityPort,
        observations: OpportunityObservationPort,
        theses: OpportunityThesisPort,
    ) -> None:
        self._plans = plans
        self._snapshots = snapshots
        self._signals = signals
        self._halts = halts
        self._catalysts = catalysts
        self._history = history
        self._prior_decisions = prior_decisions
        self._greek_authority = greek_authority
        self._observations = observations
        self._theses = theses

    async def acquire(
        self,
        authority: ObservedPaperAccountAuthority,
        trusted_at: datetime,
        tick_id: UUID,
        *,
        actor: Actor,
    ) -> OpportunityAcquisition | OpportunityNoTradeAcquisition:
        if (
            authority.role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION)
            or actor is not Actor.SCHEDULER
            or trusted_at.tzinfo is None
            or trusted_at.utcoffset() != timedelta(0)
            or not isinstance(tick_id, UUID)
        ):
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY, "OPPORTUNITY_ACQUISITION_AUTHORITY_INVALID"
            )
        try:
            return await self._acquire(authority, trusted_at)
        except AcquisitionFailure:
            raise
        except Exception as error:
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY, "OPPORTUNITY_COMPOSITION_UNAVAILABLE"
            ) from error

    async def _acquire(
        self, account_authority: ObservedPaperAccountAuthority, trusted_at: datetime
    ) -> OpportunityAcquisition | OpportunityNoTradeAcquisition:
        plan_authority = self._stage("PLAN", lambda: self._plans.load(trusted_at=trusted_at))
        policy = plan_authority.plan_spec.policy
        request = plan_authority.plan_spec.request_contract
        self._validate_pre_provider_authority(plan_authority, account_authority, trusted_at)
        snapshot = self._stage(
            "SNAPSHOT", lambda: self._snapshots.collect(request, trusted_at=trusted_at)
        )
        signal_evidence = self._stage(
            "SIGNAL",
            lambda: self._signals.collect(
                plan_authority.signal_request,
                policy=policy,
                snapshot_source_hash=snapshot.source_hash,
                observed_at=snapshot.trusted_at,
            ),
        )
        halt = self._stage(
            "HALT",
            lambda: _confirmed_halt(
                self._halts.read(symbol=policy.underlying, trusted_at=snapshot.trusted_at)
            ),
        )
        signals = OpportunitySignalAuthority(
            snapshot_source_hash=signal_evidence.authority.snapshot_source_hash,
            calculation_source_hash=signal_evidence.authority.source_hash,
            beta=signal_evidence.authority.beta,
            vwap_distance=signal_evidence.authority.vwap_distance,
            relative_return=signal_evidence.authority.relative_return,
            trend=signal_evidence.authority.trend,
            absolute_first_reaction=signal_evidence.authority.absolute_first_reaction,
            trading_halt_state=halt.trading_halt_state,
            trading_status_observed_at=halt.trading_status_observed_at,
            trading_status_source_hash=halt.trading_status_source_hash,
        )
        catalyst = await self._async_stage(
            "CATALYST",
            self._catalysts.produce(
                plan=plan_authority.catalyst_plan,
                policy=policy,
                trusted_at=snapshot.trusted_at,
            ),
        )
        history = self._stage(
            "HISTORY",
            lambda: self._history.load(
                expected_account_fingerprint=request.expected_account_fingerprint,
                opportunity_key=policy.opportunity_key,
                trading_day=policy.selected_decision_boundary.date(),
                snapshot=snapshot,
                baseline=plan_authority.baseline,
            ),
        )
        prior = self._stage(
            "PRIOR_DECISION",
            lambda: self._prior_decisions.load(
                expected_account_fingerprint=request.expected_account_fingerprint,
                opportunity_key=policy.opportunity_key,
                decision_boundary=policy.selected_decision_boundary,
                as_of=snapshot.trusted_at,
            ),
        )
        greek = self._stage(
            "GREEK", lambda: self._greek_authority.load(effective_at=snapshot.trusted_at)
        )
        self._validate_sources(
            plan_authority,
            snapshot,
            signal_evidence,
            halt,
            catalyst,
            history,
            prior,
            greek,
        )

        direction = derive_opportunity_direction(
            policy,
            vwap_distance=signals.vwap_distance.value,
            relative_return=signals.relative_return.value,
            bull_trend_hits=signals.trend.bull_hits,
            bear_trend_hits=signals.trend.bear_hits,
        )
        selection_authority: CandidateSelectionAuthority | None = None
        selection: CandidateSelectionResult | None = None
        if direction is not None:
            remaining_risk = max(
                Decimal(0),
                policy.maximum_lifetime_risk
                - history.account.lifetime_approved_risk
                - history.account.reserved_approved_risk,
            )
            selection_authority = CandidateSelectionAuthority(
                snapshot_request_hash=snapshot.request_hash,
                snapshot_source_hash=snapshot.source_hash,
                account_fingerprint=history.account.account_fingerprint,
                observed_equity=history.account.clean_equity,
                observed_buying_power=snapshot.account_book.account.buying_power,
                available_risk=min(policy.maximum_position_loss, remaining_risk),
                available_buying_power=snapshot.account_book.account.buying_power,
                greek_unit_convention=greek.convention,
                greek_unit_evidence_hash=greek.evidence_hash,
            )
            selection = select_vertical_candidate(
                snapshot,
                policy,
                direction,
                plan_authority.requested_maximum_quantity,
                selection_authority,
            )
        assembly = self._stage(
            "INPUT",
            lambda: assemble_opportunity_input(
                request=request,
                snapshot=snapshot,
                policy=policy,
                requested_maximum_quantity=plan_authority.requested_maximum_quantity,
                selection_authority=selection_authority,
                selection=selection,
                signals=signals,
                catalyst=catalyst.authority,
                account=history.account,
                prior_decision=prior,
            ),
        )
        decision = self._stage("DECISION", lambda: evaluate_opportunity(policy, assembly.values))
        observation_spec = OpportunityObservationSpec(
            account_role=account_authority.role,
            plan_id=plan_authority.plan.plan_id,
            baseline_id=plan_authority.baseline.baseline_id,
            account_fingerprint=history.account.account_fingerprint,
            policy_hash=plan_authority.plan.policy_hash,
            request_hash=snapshot.request_hash,
            snapshot_hash=snapshot.source_hash,
            calendar_hash=signal_evidence.calendar_hash,
            daily_hash=signal_evidence.daily_hash,
            intraday_hash=signal_evidence.intraday_hash,
            signal_authority_hash=signals.calculation_source_hash,
            halt_hash=signals.trading_status_source_hash,
            catalyst_hash=catalyst.authority.source_hash,
            greek_hash=greek.evidence_hash,
            account_hash=history.account.snapshot_book_source_hash,
            activity_hash=history.account.history_source_hash,
            budget_hash=history.budget_hash,
            prior_decision_hash=prior.source_hash,
            trusted_at=snapshot.trusted_at,
            evaluated_at=snapshot.trusted_at,
        )
        observation = self._stage(
            "OBSERVATION", lambda: self._observations.append(observation_spec)
        )
        self._validate_observation(observation_spec, observation)
        if decision.outcome is not OpportunityOutcome.ENTRY_APPROVED:
            return OpportunityNoTradeAcquisition(policy, assembly.values, decision)
        if selection_authority is None or selection is None:
            raise AcquisitionFailure(AcquisitionKind.OPPORTUNITY, "OPPORTUNITY_SELECTION_MISSING")

        draft = self._stage(
            "THESIS",
            lambda: build_frozen_opportunity_thesis(
                OpportunityThesisFactoryInput(
                    plan_spec=plan_authority.plan_spec,
                    plan=plan_authority.plan,
                    baseline_seal=plan_authority.baseline_seal,
                    baseline=plan_authority.baseline,
                    observation_spec=observation_spec,
                    observation=observation,
                    request=request,
                    snapshot=snapshot,
                    requested_maximum_quantity=plan_authority.requested_maximum_quantity,
                    selection_authority=selection_authority,
                    selection=selection,
                    signals=signals,
                    catalyst=catalyst.authority,
                    account=history.account,
                    prior_decision=prior,
                    assembly=assembly,
                    decision=decision,
                    signal_calendar_hash=signal_evidence.calendar_hash,
                    signal_daily_hash=signal_evidence.daily_hash,
                    signal_intraday_hash=signal_evidence.intraday_hash,
                    signal_authority_hash=signals.calculation_source_hash,
                    budget_source_hash=history.budget_hash,
                )
            ),
        )
        thesis = self._stage("THESIS_PERSISTENCE", lambda: self._theses.persist(draft))
        expires_at = min(snapshot.trusted_at + _PROPOSAL_TTL, policy.last_entry_boundary)
        if expires_at <= snapshot.trusted_at:
            raise AcquisitionFailure(AcquisitionKind.OPPORTUNITY, "OPPORTUNITY_ENTRY_WINDOW_CLOSED")
        proposal = self._stage(
            "PROPOSAL",
            lambda: build_development_entry_proposal(
                EntryProposalAuthorityInput(
                    policy=policy,
                    values=assembly.values,
                    decision=decision,
                    thesis_version_id=thesis.thesis_version_id,
                    thesis_account_role=thesis.account_role,
                    thesis_policy_hash=thesis.policy_hash,
                    thesis_underlying=thesis.underlying,
                    thesis_frozen_at=thesis.frozen_at,
                    account_role=account_authority.role,
                    account_fingerprint=history.account.account_fingerprint,
                    valid_from=snapshot.trusted_at,
                    expires_at=expires_at,
                    benchmark_symbol=request.benchmark,
                    underlying_source_hash=snapshot.underlying_bar.source_hash,
                    benchmark_source_hash=snapshot.benchmark_bar.source_hash,
                    completed_bar_source_hash=snapshot.source_hash,
                )
            ),
        )
        return proposal.acquisition

    @staticmethod
    def _validate_pre_provider_authority(
        plan: OpportunityPlanAuthority,
        account: ObservedPaperAccountAuthority,
        trusted_at: datetime,
    ) -> None:
        try:
            policy = plan.plan_spec.policy
            request = plan.plan_spec.request_contract
        except (AttributeError, TypeError, ValueError) as error:
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY,
                "OPPORTUNITY_PLAN_AUTHORITY_INVALID",
            ) from error
        if (
            getattr(request, "account_role", AccountRole.DEVELOPMENT) is not account.role
            or request.expected_account_fingerprint != account.account_fingerprint
        ):
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY,
                "OPPORTUNITY_ACCOUNT_AUTHORITY_MISMATCH",
            )
        try:
            valid = (
                plan.plan_spec.opportunity_key == policy.opportunity_key
                and plan.plan_spec.underlying == policy.underlying
                and plan.plan.opportunity_key == policy.opportunity_key
                and plan.plan.plan_id == plan.baseline_seal.plan_id
                and plan.plan.plan_id == plan.baseline.plan_id
                and getattr(plan.plan_spec, "account_role", AccountRole.DEVELOPMENT) is account.role
                and getattr(plan.plan, "account_role", AccountRole.DEVELOPMENT) is account.role
                and getattr(plan.baseline_seal, "account_role", AccountRole.DEVELOPMENT)
                is account.role
                and getattr(plan.baseline, "account_role", AccountRole.DEVELOPMENT) is account.role
                and plan.baseline_seal.account_fingerprint == account.account_fingerprint
                and plan.baseline.account_fingerprint == account.account_fingerprint
                and plan.baseline_seal.captured_at == plan.baseline.captured_at
                and plan.plan.frozen_at == plan.plan_spec.frozen_at
                and plan.plan.frozen_at <= plan.baseline.captured_at
                and plan.baseline.captured_at <= trusted_at
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY,
                "OPPORTUNITY_PLAN_AUTHORITY_INVALID",
            )
        if trusted_at < policy.selected_decision_boundary:
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY,
                "OPPORTUNITY_DECISION_BOUNDARY_NOT_REACHED",
            )
        if trusted_at >= policy.last_entry_boundary:
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY,
                "OPPORTUNITY_ENTRY_WINDOW_CLOSED",
            )

    @staticmethod
    def _validate_observation(
        spec: OpportunityObservationSpec,
        observation: PersistedOpportunityObservation,
    ) -> None:
        expected_hash = opportunity_observation_digest(spec)
        expected_id = uuid5(
            NAMESPACE_URL,
            f"alphadecay:opportunity-observation:{expected_hash}",
        )
        if (
            not isinstance(observation, PersistedOpportunityObservation)
            or observation.observation_id != expected_id
            or observation.plan_id != spec.plan_id
            or observation.account_role is not spec.account_role
            or observation.baseline_id != spec.baseline_id
            or observation.manifest_hash != expected_hash
            or observation.trusted_at != spec.trusted_at
            or observation.evaluated_at != spec.evaluated_at
        ):
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY,
                "OPPORTUNITY_OBSERVATION_REPLAY_MISMATCH",
            )

    @staticmethod
    def _validate_sources(
        plan: OpportunityPlanAuthority,
        snapshot: OpportunityMarketSnapshot,
        signals: OpportunitySignalEvidence,
        halt: HaltAuthoritySnapshot,
        catalyst: CatalystAuthorityResult,
        history: OpportunityHistoryEvidence,
        prior: PriorDecisionAuthority,
        greek: GreekUnitAuthority,
    ) -> None:
        hashes = (
            snapshot.request_hash,
            snapshot.source_hash,
            snapshot.underlying_bar.source_hash,
            snapshot.benchmark_bar.source_hash,
            signals.authority.snapshot_source_hash,
            signals.calendar_hash,
            signals.daily_hash,
            signals.intraday_hash,
            signals.authority.source_hash,
            halt.trading_status_source_hash,
            catalyst.authority.source_hash,
            catalyst.plan_hash,
            catalyst.policy_hash,
            catalyst.criteria_hash,
            catalyst.research_source_hash,
            catalyst.classification_hash,
            catalyst.authority_hash,
            history.budget_hash,
            history.account.snapshot_book_source_hash,
            history.account.history_source_hash,
            prior.source_hash,
            greek.evidence_hash,
            greek.authority_hash,
        )
        if (
            any(type(value) is not str or _HASH.fullmatch(value) is None for value in hashes)
            or getattr(snapshot.account_book.account, "role", AccountRole.DEVELOPMENT)
            is not getattr(plan.plan_spec, "account_role", AccountRole.DEVELOPMENT)
            or getattr(history.account, "account_role", AccountRole.DEVELOPMENT)
            is not getattr(plan.plan_spec, "account_role", AccountRole.DEVELOPMENT)
            or signals.authority.snapshot_source_hash != snapshot.source_hash
            or catalyst.plan_hash != plan.plan.plan_hash
            or catalyst.policy_hash != plan.plan.policy_hash
            or catalyst.authority.source_hash != catalyst.authority_hash
        ):
            raise AcquisitionFailure(AcquisitionKind.OPPORTUNITY, "OPPORTUNITY_SOURCE_INVALID")

    @staticmethod
    def _stage(code: str, call: Callable[[], _Result]) -> _Result:
        try:
            return call()
        except AcquisitionFailure:
            raise
        except OpportunitySnapshotError as error:
            if code == "SNAPSHOT" and error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
                raise AcquisitionFailure(AcquisitionKind.OPPORTUNITY, error.code) from error
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY, _unavailable_code(code, error)
            ) from error
        except Exception as error:
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY, _unavailable_code(code, error)
            ) from error

    @staticmethod
    async def _async_stage(code: str, awaitable: Awaitable[_Result]) -> _Result:
        try:
            return await awaitable
        except AcquisitionFailure:
            raise
        except Exception as error:
            raise AcquisitionFailure(
                AcquisitionKind.OPPORTUNITY, _unavailable_code(code, error)
            ) from error
