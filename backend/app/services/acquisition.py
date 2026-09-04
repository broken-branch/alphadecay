from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Literal, Protocol, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.app.contracts.v1 import (
    AccountRole,
    DataQuality,
    EvidenceClassification,
    EvidenceState,
    GreekExposure,
    PositionIntent,
    SourceCluster,
    ThesisResponse,
)
from backend.app.domain.exposure import ExposureBlocked, GreekLeg, aggregate_greeks
from backend.app.domain.option_contract_symbol import (
    NON_STANDARD_CONTRACT_UNSUPPORTED,
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)
from backend.app.execution import (
    AccountObservation,
    ActivityType,
    Actor,
    AssessmentCertificate,
    EntryApprovalAuthorization,
    ExecutionAction,
    InventoryItem,
    InventoryKind,
    OrderEnvelope,
    OrderLegIntent,
    SweepObservation,
    intent_digest,
    order_envelope_hash,
)
from backend.app.execution.models import ExecutionIntent, IntentState
from backend.app.lifecycle import LifecycleLaunchAuthority
from backend.app.lifecycle.fingerprint import option_position_fingerprint
from backend.app.lifecycle.structural_pilot import structural_pilot_lifecycle
from backend.app.order_limits import (
    MAX_STRUCTURAL_APPROVED_RISK,
    MAX_STRUCTURAL_LIFETIME_RISK,
    MAX_STRUCTURAL_OPTION_QUANTITY,
)
from backend.app.policy import (
    AssessmentInput,
    EvidenceClaim,
    ExecutionDecision,
    FreshnessInput,
    FreshnessKind,
    HardGateInput,
    OpportunityDecisionRecord,
    OpportunityInput,
    OpportunityOutcome,
    OpportunityPolicy,
    RollCandidate,
    ScoreInput,
    VolatilityView,
    check_freshness,
    evaluate_assessment,
    evaluate_opportunity,
    score_drift,
    score_evidence,
)

_RETRIEVAL_SKEW_TOLERANCE = timedelta(seconds=30)


T = TypeVar("T")
_FAILURE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_EVENT_CODES = frozenset(
    {
        "RESULTS",
        "GUIDANCE",
        "DEMAND",
        "SUPPLY",
        "PRODUCT",
        "CUSTOMER_PARTNER",
        "CAPITAL",
        "REGULATORY_LEGAL",
        "MANAGEMENT",
        "MACRO",
        "OTHER",
    }
)
_MARKET_SYNC_WINDOW = timedelta(seconds=5)
_OPTION_AUTHORIZATION_TTL = timedelta(seconds=30)
_MAX_ACTIVITY_ITEMS = 4096
_MAX_RESEARCH_CLUSTERS = 12
_MAX_SOURCE_IDS = 48
_MAX_TEXT_LENGTH = 512
_MAX_MANIFEST_ITEMS = 8192
_MAX_MANIFEST_BYTES = 1_000_000
_MAX_MANIFEST_DEPTH = 12
_MAX_NUMERIC_MAGNITUDE = Decimal("1000000000")


@dataclass(frozen=True)
class ObservedPaperAccountAuthority:
    role: AccountRole
    account_fingerprint: str
    paper: Literal[True]
    persistent_autonomy_enabled: bool

    def __post_init__(self) -> None:
        if (
            self.role not in {AccountRole.SUBMISSION, AccountRole.DEVELOPMENT}
            or self.paper is not True
            or not _is_hash(self.account_fingerprint)
        ):
            raise ValueError("OBSERVED_PAPER_ACCOUNT_AUTHORITY_INVALID")


@dataclass(frozen=True)
class CalibrationBinding:
    account_role: AccountRole
    account_fingerprint: str
    decision_code: Literal["CALIBRATION_BINDING_NO_TRADE"]
    machine_binding_hash: str
    calibration_hash: str
    decision_boundary: datetime
    sealed_at: datetime

    def __post_init__(self) -> None:
        if (
            self.account_role is not AccountRole.SUBMISSION
            or self.decision_code != "CALIBRATION_BINDING_NO_TRADE"
            or not _is_hash(self.account_fingerprint)
            or not _is_hash(self.machine_binding_hash)
            or not _is_hash(self.calibration_hash)
            or self.decision_boundary.tzinfo is None
            or self.decision_boundary.utcoffset() != timedelta(0)
            or self.sealed_at.tzinfo is None
            or self.sealed_at.utcoffset() != timedelta(0)
            or self.sealed_at < self.decision_boundary
        ):
            raise ValueError("CALIBRATION_BINDING_INVALID")


@dataclass(frozen=True)
class PermanentAccountLatch:
    latched: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.latched != (self.reason is not None):
            raise ValueError("PERMANENT_ACCOUNT_LATCH_INVALID")


@dataclass(frozen=True)
class AuthorizationIntentProposal:
    authorization: EntryApprovalAuthorization | AssessmentCertificate
    intent: ExecutionIntent


@dataclass(frozen=True)
class OpportunityAcquisition:
    policy: OpportunityPolicy
    values: OpportunityInput
    thesis_version_id: UUID
    proposal: AuthorizationIntentProposal | None = None
    launch_authority: LifecycleLaunchAuthority | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.thesis_version_id, UUID)
            or evaluate_opportunity(self.policy, self.values).outcome
            is not OpportunityOutcome.ENTRY_APPROVED
        ):
            raise ValueError("OPPORTUNITY_APPROVED_ACQUISITION_INVALID")


@dataclass(frozen=True)
class OpportunityNoTradeAcquisition:
    policy: OpportunityPolicy
    values: OpportunityInput
    decision: OpportunityDecisionRecord

    def __post_init__(self) -> None:
        if (
            self.decision.outcome is not OpportunityOutcome.NO_TRADE
            or evaluate_opportunity(self.policy, self.values) != self.decision
        ):
            raise ValueError("OPPORTUNITY_NO_TRADE_ACQUISITION_INVALID")


@dataclass(frozen=True)
class LifecycleAcquisition:
    values: AssessmentInput
    thesis_version_id: UUID
    proposal: AuthorizationIntentProposal | None = None


DecisionAcquisition = OpportunityAcquisition | OpportunityNoTradeAcquisition | LifecycleAcquisition


class AcquisitionKind(StrEnum):
    OPPORTUNITY = "OPPORTUNITY"
    LIFECYCLE = "LIFECYCLE"


class AcquisitionFailure(RuntimeError):
    def __init__(self, kind: AcquisitionKind, code: str) -> None:
        if not code:
            raise ValueError("ACQUISITION_FAILURE_CODE_REQUIRED")
        super().__init__(code)
        self.kind = kind
        self.code = code


@dataclass(frozen=True)
class RetainedOptionPosition:
    symbol: str
    signed_quantity: Decimal
    multiplier: int


@dataclass(frozen=True)
class GreekAuthorityEvidence:
    authority_id: UUID
    version: int
    effective_at: datetime
    timestamp_contract_hash: str
    units_source_hash: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authority_id, UUID)
            or type(self.version) is not int
            or self.version < 1
            or not _is_utc(self.effective_at)
            or not _is_hash(self.timestamp_contract_hash)
            or not _is_hash(self.units_source_hash)
        ):
            raise ValueError("GREEK_AUTHORITY_INVALID")


@dataclass(frozen=True)
class RetainedLifecycleTransition:
    action: Literal["ENTRY", "ROLL"]
    occurred_at: datetime
    market_session_id: UUID
    cashflow: Decimal
    activity_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            self.action not in {"ENTRY", "ROLL"}
            or not _is_utc(self.occurred_at)
            or not isinstance(self.market_session_id, UUID)
            or not isinstance(self.cashflow, Decimal)
            or not self.cashflow.is_finite()
            or abs(self.cashflow) > _MAX_NUMERIC_MAGNITUDE
            or self.activity_hashes != tuple(sorted(self.activity_hashes))
            or len(self.activity_hashes) > 64
            or len(set(self.activity_hashes)) != len(self.activity_hashes)
            or any(not _is_hash(item) for item in self.activity_hashes)
        ):
            raise ValueError("LIFECYCLE_TRANSITION_INVALID")


@dataclass(frozen=True)
class RetainedLifecycleContext:
    thesis_version_id: UUID
    account_role: AccountRole
    account_fingerprint: str
    policy_hash: str
    thesis: ThesisResponse
    thesis_frozen_at: datetime
    lifecycle_origin_at: datetime
    lifecycle_transitions: tuple[RetainedLifecycleTransition, ...]
    target_at: datetime
    position_fingerprint: str
    expected_positions: tuple[RetainedOptionPosition, RetainedOptionPosition]
    account_expected_positions: tuple[RetainedOptionPosition, ...]
    account_activity_hashes: tuple[str, ...]
    account_lifecycle_origin_at: datetime
    delta_low: Decimal
    delta_high: Decimal
    vega_low: Decimal
    vega_high: Decimal
    maximum_daily_theta: Decimal
    minimum_dte: int
    maximum_dte: int
    maximum_relative_spread: Decimal
    liquidity_authority_hash: str
    volatility_view: VolatilityView
    entry_atm_iv: Decimal
    approved_max_loss: Decimal
    portfolio_risk_cap: Decimal
    greek_authority: GreekAuthorityEvidence
    managed_position_id: UUID
    current_snapshot_id: UUID
    launch_authority: LifecycleLaunchAuthority


@dataclass(frozen=True)
class LifecycleOptionObservation:
    symbol: str
    signed_quantity: Decimal
    multiplier: int
    active: bool
    tradable: bool
    feed: str
    bid_price: Decimal
    ask_price: Decimal
    delta: Decimal
    gamma: Decimal
    theta_per_day: Decimal
    vega_per_iv_point: Decimal
    quote_observed_at: datetime
    greek_observed_at: datetime | None
    retrieved_at: datetime
    greek_authority_id: UUID
    greek_timestamp_source_hash: str
    greek_units_source_hash: str
    source_hash: str


@dataclass(frozen=True)
class AtmIvObservation:
    underlying: str
    value: Decimal
    feed: str
    observed_at: datetime
    retrieved_at: datetime
    source_hash: str
    request_hash: str
    call_source_hash: str
    put_source_hash: str


@dataclass(frozen=True)
class PriceConfirmationPoint:
    completed_bar_at: datetime
    vwap_side: Decimal
    relative_return_side: Decimal
    source_hash: str
    underlying_bar_source_hash: str
    benchmark_bar_source_hash: str


@dataclass(frozen=True)
class UnderlyingMarketObservation:
    underlying: str
    bid_price: Decimal
    ask_price: Decimal
    quote_observed_at: datetime
    quote_retrieved_at: datetime
    quote_source_hash: str
    completed_bar_at: datetime
    completed_bar_source_hash: str
    request_hash: str
    benchmark_symbol: str
    benchmark_completed_bar_at: datetime
    benchmark_completed_bar_source_hash: str


@dataclass(frozen=True)
class AlpacaMarketSession:
    market_session_id: UUID
    session_date: date
    open_at: datetime
    close_at: datetime
    source_hash: str
    request_hash: str
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.market_session_id, UUID)
            or not isinstance(self.session_date, date)
            or not _is_utc(self.open_at)
            or not _is_utc(self.close_at)
            or self.open_at >= self.close_at
            or not _is_hash(self.source_hash)
            or not _is_hash(self.request_hash)
            or not _is_utc(self.retrieved_at)
        ):
            raise ValueError("ALPACA_MARKET_SESSION_INVALID")


@dataclass(frozen=True)
class LifecycleBoundaryObservation:
    market_session: AlpacaMarketSession
    observed_at: datetime
    source_hash: str
    price_confirmation: tuple[PriceConfirmationPoint, ...]
    short_call_close_at: datetime | None
    weekend_close_at: datetime | None
    contest_end_at: datetime | None


@dataclass(frozen=True)
class LifecycleRollObservation:
    positions: tuple[RetainedOptionPosition, RetainedOptionPosition]
    options: tuple[LifecycleOptionObservation, LifecycleOptionObservation]


@dataclass(frozen=True)
class LifecycleProviderObservation:
    sweep: SweepObservation
    underlying: UnderlyingMarketObservation
    options: tuple[LifecycleOptionObservation, LifecycleOptionObservation]
    atm_iv: AtmIvObservation
    boundaries: LifecycleBoundaryObservation
    roll: LifecycleRollObservation | None = None
    roll_candidates: tuple[LifecycleRollObservation, ...] = ()


@dataclass(frozen=True)
class _ParsedOption:
    symbol: str
    root: str
    expiry: date
    right: str
    strike: Decimal


@dataclass(frozen=True)
class _ValidatedObservation:
    options: tuple[LifecycleOptionObservation, LifecycleOptionObservation]
    dte: int
    liquidation_pnl: Decimal


@dataclass(frozen=True)
class _RollSelection:
    candidate: RollCandidate
    observation: LifecycleRollObservation


class RetainedLifecycleContextPort(Protocol):
    def load(self, authority: ObservedPaperAccountAuthority) -> RetainedLifecycleContext: ...


class LifecycleObservationPort(Protocol):
    def observe(
        self,
        context: RetainedLifecycleContext,
        trusted_at: datetime,
    ) -> LifecycleProviderObservation: ...


class LifecycleResearchPort(Protocol):
    async def research(
        self,
        context: RetainedLifecycleContext,
        trusted_at: datetime,
    ) -> tuple[SourceCluster, ...]: ...


class LifecycleClassifierPort(Protocol):
    def classify(
        self,
        thesis: ThesisResponse,
        clusters: tuple[SourceCluster, ...],
    ) -> tuple[EvidenceClassification, ...]: ...


class LifecycleManifestPort(Protocol):
    def persist(
        self,
        *,
        context: RetainedLifecycleContext,
        observation: LifecycleProviderObservation,
        clusters: tuple[SourceCluster, ...],
        classifications: tuple[EvidenceClassification, ...],
        manifest_id: UUID,
        manifest_hash: str,
        trusted_at: datetime,
    ) -> None: ...


class DevelopmentLifecycleAcquisition:
    """Build a lifecycle decision and its exact executable proposal from fresh evidence."""

    def __init__(
        self,
        contexts: RetainedLifecycleContextPort,
        observations: LifecycleObservationPort,
        research: LifecycleResearchPort,
        classifier: LifecycleClassifierPort,
        manifests: LifecycleManifestPort,
    ) -> None:
        self._contexts = contexts
        self._observations = observations
        self._research = research
        self._classifier = classifier
        self._manifests = manifests

    async def acquire(
        self,
        authority: ObservedPaperAccountAuthority,
        trusted_at: datetime,
        tick_id: UUID,
        *,
        actor: Actor,
    ) -> LifecycleAcquisition:
        if authority.role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "SUBMISSION_ACQUISITION_FORBIDDEN",
            )
        if not isinstance(actor, Actor):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ACTOR_INVALID")
        if not _is_utc(trusted_at):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "TRUSTED_TIME_INVALID")
        if not isinstance(tick_id, UUID):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "TICK_ID_INVALID")

        context = self._load_context(authority)
        structural_lifecycle = structural_pilot_lifecycle(context)
        if authority.role is AccountRole.SUBMISSION and structural_lifecycle is None:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "SUBMISSION_ACQUISITION_FORBIDDEN",
            )
        if trusted_at < context.lifecycle_origin_at:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "LIFECYCLE_NOT_YET_ACTIVE")
        observation, normalized = self._load_observation(context, authority, trusted_at)
        structural_pilot = structural_lifecycle is not None
        if structural_pilot:
            clusters = ()
            classifications = ()
        else:
            clusters = await self._load_research(context, trusted_at)
            self._validate_clusters(clusters, trusted_at)
            classifications = self._call_source(
                "CLASSIFICATION",
                self._classifier.classify,
                context.thesis,
                clusters,
            )
            self._validate_classifications(context, clusters, classifications)
        manifest_hash = _acquisition_manifest_hash(
            context,
            observation,
            clusters,
            classifications,
        )

        evidence_state = EvidenceState.ASSESSED if classifications else EvidenceState.NO_CHANGE
        evidence = score_evidence(
            evidence_state,
            tuple(
                EvidenceClaim(
                    cluster_id=item.cluster_id,
                    relation=item.relation,
                    materiality=item.materiality,
                    relevance=item.relevance,
                    confidence=item.confidence,
                    source_tier=item.source_tier,
                    invalidates=item.invalidation_condition_id is not None,
                    independent_reporting_group=item.independent_reporting_group,
                )
                for item in classifications
            ),
        )
        if evidence.evidence_drift is None:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "EVIDENCE_SCORE_INCOMPLETE",
            )
        actual_exposure = _aggregate_option_greeks(
            normalized.options,
            "POSITION_GREEK_EVIDENCE_INVALID",
        )

        horizon_fraction = Decimal(
            _timedelta_microseconds(trusted_at - context.lifecycle_origin_at)
        ) / Decimal(_timedelta_microseconds(context.target_at - context.lifecycle_origin_at))
        scores = ScoreInput(
            evidence_drift=evidence.evidence_drift,
            delta=actual_exposure.delta,
            delta_low=context.delta_low,
            delta_high=context.delta_high,
            vega=actual_exposure.vega_per_iv_point,
            vega_low=context.vega_low,
            vega_high=context.vega_high,
            theta_per_day=actual_exposure.theta_per_day,
            max_daily_theta=context.maximum_daily_theta,
            dte=normalized.dte,
            minimum_dte=context.minimum_dte,
            maximum_dte=context.maximum_dte,
            horizon_fraction=horizon_fraction,
            volatility_view=context.volatility_view,
            entry_atm_iv=context.entry_atm_iv,
            current_atm_iv=observation.atm_iv.value,
            liquidation_pnl=normalized.liquidation_pnl,
            approved_max_loss=context.approved_max_loss,
        )
        if structural_pilot:
            try:
                assert structural_lifecycle is not None
                strategy_close_reason = structural_lifecycle.close_reason(
                    context,
                    executable_value=_close_cashflow(normalized.options),
                    trusted_at=trusted_at,
                )
            except ValueError as error:
                raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, str(error)) from error
            hard_gates = HardGateInput(strategy_close_reason=strategy_close_reason)
            roll_selection = None
        else:
            hard_gates = self._derive_hard_gates(
                context,
                observation.boundaries,
                classifications,
                normalized.dte,
                trusted_at,
            )
            roll_selection = self._derive_roll_candidate(
                context,
                observation,
                scores,
                observation.boundaries.market_session,
                trusted_at,
            )
        manifest_id = uuid5(
            NAMESPACE_URL,
            f"alphadecay:lifecycle-acquisition:{manifest_hash}",
        )
        values = AssessmentInput(
            assessment_id=uuid5(
                NAMESPACE_URL,
                f"alphadecay:lifecycle-assessment:{tick_id}",
            ),
            run_id=tick_id,
            policy_hash=context.policy_hash,
            quality=DataQuality.COMPLETE,
            actual_exposure=actual_exposure,
            thesis_status=evidence.thesis_status,
            evidence_state=evidence.state,
            scores=scores,
            hard_gates=hard_gates,
            roll_candidate=roll_selection.candidate if roll_selection is not None else None,
            acquisition_manifest_id=manifest_id,
            acquisition_manifest_hash=manifest_hash,
        )
        self._persist_manifest(
            context=context,
            observation=observation,
            clusters=clusters,
            classifications=classifications,
            manifest_id=manifest_id,
            manifest_hash=manifest_hash,
            trusted_at=trusted_at,
        )
        proposal = _lifecycle_proposal(
            context=context,
            observation=observation,
            values=values,
            selected_roll=roll_selection.observation if roll_selection is not None else None,
            trusted_at=trusted_at,
        )
        return LifecycleAcquisition(values, context.thesis_version_id, proposal)

    def _persist_manifest(
        self,
        *,
        context: RetainedLifecycleContext,
        observation: LifecycleProviderObservation,
        clusters: tuple[SourceCluster, ...],
        classifications: tuple[EvidenceClassification, ...],
        manifest_id: UUID,
        manifest_hash: str,
        trusted_at: datetime,
    ) -> None:
        try:
            self._manifests.persist(
                context=context,
                observation=observation,
                clusters=clusters,
                classifications=classifications,
                manifest_id=manifest_id,
                manifest_hash=manifest_hash,
                trusted_at=trusted_at,
            )
        except Exception as error:
            raise _source_failure("MANIFEST", error) from error

    @staticmethod
    def _call_source(stage: str, operation: Callable[..., T], *args: object) -> T:
        try:
            return operation(*args)
        except Exception as error:
            raise _source_failure(stage, error) from error

    def _load_context(
        self,
        authority: ObservedPaperAccountAuthority,
    ) -> RetainedLifecycleContext:
        context = self._call_source("CONTEXT", self._contexts.load, authority)
        try:
            self._validate_context(context, authority)
        except AcquisitionFailure:
            raise
        except Exception as error:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "RETAINED_CONTEXT_INVALID",
            ) from error
        return context

    def _load_observation(
        self,
        context: RetainedLifecycleContext,
        authority: ObservedPaperAccountAuthority,
        trusted_at: datetime,
    ) -> tuple[LifecycleProviderObservation, _ValidatedObservation]:
        observation = self._call_source(
            "OBSERVATION",
            self._observations.observe,
            context,
            trusted_at,
        )
        try:
            normalized = self._validate_observation(
                context,
                observation,
                authority,
                trusted_at,
            )
        except AcquisitionFailure:
            raise
        except Exception as error:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "LIFECYCLE_OBSERVATION_INVALID",
            ) from error
        return observation, normalized

    async def _load_research(
        self,
        context: RetainedLifecycleContext,
        trusted_at: datetime,
    ) -> tuple[SourceCluster, ...]:
        try:
            return await self._research.research(context, trusted_at)
        except Exception as error:
            raise _source_failure("RESEARCH", error) from error

    @staticmethod
    def _validate_context(
        context: RetainedLifecycleContext,
        authority: ObservedPaperAccountAuthority,
    ) -> None:
        if not isinstance(context, RetainedLifecycleContext):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "RETAINED_CONTEXT_INVALID")
        if (
            not isinstance(context.thesis, ThesisResponse)
            or not isinstance(context.expected_positions, tuple)
            or not isinstance(context.account_expected_positions, tuple)
            or any(
                not isinstance(item, RetainedOptionPosition) for item in context.expected_positions
            )
            or any(
                not isinstance(item, RetainedOptionPosition)
                for item in context.account_expected_positions
            )
        ):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "RETAINED_CONTEXT_INVALID")
        positions = context.expected_positions
        try:
            parsed = _parse_vertical(positions)
        except (TypeError, ValueError) as error:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "RETAINED_CONTEXT_INVALID",
            ) from error
        quantities = tuple(item.signed_quantity for item in positions)
        intended = context.thesis.thesis.intended_exposure
        decimals = (
            context.delta_low,
            context.delta_high,
            context.vega_low,
            context.vega_high,
            context.maximum_daily_theta,
            context.entry_atm_iv,
            context.approved_max_loss,
            context.portfolio_risk_cap,
            context.maximum_relative_spread,
        )
        valid = (
            isinstance(context.thesis_version_id, UUID)
            and context.account_role in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION)
            and context.account_role is authority.role
            and context.account_fingerprint == authority.account_fingerprint
            and _is_hash(context.policy_hash)
            and _is_hash(context.position_fingerprint)
            and _is_utc(context.account_lifecycle_origin_at)
            and context.account_lifecycle_origin_at <= context.lifecycle_origin_at
            and context.account_activity_hashes == tuple(sorted(context.account_activity_hashes))
            and len(set(context.account_activity_hashes)) == len(context.account_activity_hashes)
            and all(_is_hash(value) for value in context.account_activity_hashes)
            and context.thesis.frozen is True
            and _is_hash(context.thesis.thesis_hash)
            and context.thesis.thesis.source_policy_hash == context.policy_hash
            and _is_utc(context.thesis_frozen_at)
            and _is_utc(context.target_at)
            and _is_utc(context.lifecycle_origin_at)
            and context.thesis_frozen_at <= context.lifecycle_origin_at
            and context.lifecycle_origin_at < context.target_at
            and parsed[0].root == context.thesis.thesis.underlying
            and all(item.multiplier == 100 for item in positions)
            and _position_fingerprint(positions) == context.position_fingerprint
            and all(_valid_quantity(value) for value in quantities)
            and {value > 0 for value in quantities} == {True, False}
            and abs(quantities[0]) == abs(quantities[1])
            and all(isinstance(value, Decimal) and value.is_finite() for value in decimals)
            and all(abs(value) <= _MAX_NUMERIC_MAGNITUDE for value in decimals)
            and context.delta_low <= context.delta_high
            and context.vega_low <= context.vega_high
            and context.delta_low <= intended.delta <= context.delta_high
            and context.vega_low <= intended.vega_per_iv_point <= context.vega_high
            and abs(min(intended.theta_per_day, Decimal(0))) <= context.maximum_daily_theta
            and context.maximum_daily_theta > 0
            and type(context.minimum_dte) is int
            and type(context.maximum_dte) is int
            and 1 <= context.minimum_dte <= context.maximum_dte
            and 0 < context.maximum_relative_spread < 1
            and context.liquidity_authority_hash
            == lifecycle_liquidity_authority_hash(
                context.policy_hash,
                context.maximum_relative_spread,
            )
            and context.entry_atm_iv > 0
            and Decimal(0) < context.approved_max_loss <= MAX_STRUCTURAL_APPROVED_RISK
            and Decimal(0) < context.portfolio_risk_cap <= MAX_STRUCTURAL_LIFETIME_RISK
            and isinstance(context.volatility_view, VolatilityView)
            and isinstance(context.lifecycle_transitions, tuple)
            and 1 <= len(context.lifecycle_transitions) <= 64
            and all(
                isinstance(item, RetainedLifecycleTransition)
                for item in context.lifecycle_transitions
            )
            and context.lifecycle_transitions[0].action == "ENTRY"
            and all(item.action == "ROLL" for item in context.lifecycle_transitions[1:])
            and tuple(item.occurred_at for item in context.lifecycle_transitions)
            == tuple(sorted(item.occurred_at for item in context.lifecycle_transitions))
            and context.lifecycle_origin_at == context.lifecycle_transitions[0].occurred_at
            and isinstance(context.greek_authority, GreekAuthorityEvidence)
            and context.greek_authority.effective_at <= context.lifecycle_origin_at
            and isinstance(context.managed_position_id, UUID)
            and isinstance(context.current_snapshot_id, UUID)
            and isinstance(context.launch_authority, LifecycleLaunchAuthority)
            and context.launch_authority.entry_policy_hash == context.policy_hash
            and context.launch_authority.entry_boundary_at <= context.thesis_frozen_at
        )
        if not valid:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "RETAINED_CONTEXT_INVALID")

    @staticmethod
    def _validate_observation(
        context: RetainedLifecycleContext,
        observation: LifecycleProviderObservation,
        authority: ObservedPaperAccountAuthority,
        trusted_at: datetime,
    ) -> _ValidatedObservation:
        if not isinstance(observation, LifecycleProviderObservation):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "LIFECYCLE_OBSERVATION_INVALID",
            )
        if not isinstance(observation.sweep, SweepObservation):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "LIFECYCLE_ACCOUNT_EVIDENCE_INVALID",
            )
        sweep = observation.sweep
        accounts = (sweep.first_account, sweep.final_account)
        if any(
            not isinstance(account, AccountObservation)
            or account.role is not authority.role
            or account.account_fingerprint != authority.account_fingerprint
            for account in accounts
        ):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "OBSERVED_ACCOUNT_AUTHORITY_MISMATCH",
            )
        if any(account.paper is not True for account in accounts):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "PAPER_TRADING_REQUIRED")
        if any(
            account.status != "ACTIVE"
            or account.account_blocked
            or account.trading_blocked
            or account.options_trading_blocked
            for account in accounts
        ):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ACCOUNT_NOT_EXECUTABLE")
        if (
            not sweep.positions_complete
            or not sweep.orders_complete
            or not sweep.activity_pagination.complete
            or sweep.activity_pagination.visibility_complete_through
            < sweep.activity_pagination.requested_end - sweep.activity_pagination.visibility_horizon
            or sweep.retrieval_completed_at - sweep.retrieval_started_at > timedelta(seconds=15)
            or sweep.first_positions != sweep.final_positions
            or sweep.first_open_orders != sweep.final_open_orders
            or len(sweep.activities) > _MAX_ACTIVITY_ITEMS
        ):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "LIFECYCLE_ACCOUNT_EVIDENCE_INCOMPLETE",
            )
        # The sweep covers the whole account, so the expected activities are every active
        # managed position's, and the window opens at the earliest active origin.
        expected_activity_hashes = tuple(sorted(context.account_activity_hashes))
        observed_known_activity_hashes = tuple(
            sorted(
                item.activity_id_hash
                for item in sweep.activities
                if item.activity_type is not ActivityType.INITIAL_FUNDING
            )
        )
        if (
            sweep.activity_pagination.requested_start != context.account_lifecycle_origin_at
            or observed_known_activity_hashes != expected_activity_hashes
        ):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "ACTIVITY_LINEAGE_INCOMPLETE",
            )
        first_account = sweep.first_account
        final_account = sweep.final_account
        if _account_material(first_account) != _account_material(final_account):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "LIFECYCLE_ACCOUNT_EVIDENCE_UNSTABLE",
            )
        if sweep.final_open_orders:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "OPEN_ORDER_EXISTS")
        unsafe_activity_types = {
            ActivityType.OPASN,
            ActivityType.OPEXC,
            ActivityType.OPEXP,
            ActivityType.OPXRC,
            ActivityType.RESET,
            ActivityType.DEPOSIT,
            ActivityType.WITHDRAWAL,
            ActivityType.TRANSFER,
            ActivityType.JOURNAL,
            ActivityType.UNKNOWN_CASH,
            ActivityType.DIVIDEND,
            ActivityType.FEE,
            ActivityType.INTEREST,
            ActivityType.CORPORATE_ACTION,
        }
        if any(
            item.activity_type in unsafe_activity_types
            or (
                item.activity_type is not ActivityType.INITIAL_FUNDING
                and item.activity_id_hash not in expected_activity_hashes
            )
            for item in sweep.activities
        ):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ACCOUNT_ACTIVITY_UNSAFE")
        expected_inventory = tuple(
            InventoryItem(
                kind=InventoryKind.OPTION,
                symbol=item.symbol,
                signed_quantity=item.signed_quantity,
                multiplier=item.multiplier,
            )
            for item in context.account_expected_positions
        )
        if sweep.final_positions != expected_inventory:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "UNEXPECTED_ACCOUNT_INVENTORY",
            )
        options = _validate_option_evidence(
            context.expected_positions,
            observation.options,
            trusted_at,
            context.greek_authority,
        )
        replacement_options: tuple[LifecycleOptionObservation, ...] = ()
        if observation.roll is not None:
            if not isinstance(observation.roll, LifecycleRollObservation):
                raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ROLL_EVIDENCE_INVALID")
            replacement_options = _validate_option_evidence(
                observation.roll.positions,
                observation.roll.options,
                trusted_at,
                context.greek_authority,
            )
        if not isinstance(observation.atm_iv, AtmIvObservation):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "LIFECYCLE_MARKET_EVIDENCE_INVALID",
            )
        atm_iv = observation.atm_iv
        if (
            atm_iv.underlying != context.thesis.thesis.underlying
            or not isinstance(atm_iv.underlying, str)
            or not atm_iv.underlying
            or not isinstance(atm_iv.value, Decimal)
            or not atm_iv.value.is_finite()
            or atm_iv.value <= 0
            or atm_iv.value > Decimal("100")
            or atm_iv.feed != "indicative"
            or not _is_utc(atm_iv.observed_at)
            or not _is_utc(atm_iv.retrieved_at)
            or atm_iv.observed_at > atm_iv.retrieved_at
            or not _is_hash(atm_iv.source_hash)
            or not _is_hash(atm_iv.request_hash)
            or not _is_hash(atm_iv.call_source_hash)
            or not _is_hash(atm_iv.put_source_hash)
            or atm_iv.call_source_hash == atm_iv.put_source_hash
        ):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "ATM_IV_EVIDENCE_INVALID",
            )
        _validate_boundaries(observation.boundaries, trusted_at)
        underlying = observation.underlying
        if (
            not isinstance(underlying, UnderlyingMarketObservation)
            or underlying.underlying != context.thesis.thesis.underlying
            or not isinstance(underlying.bid_price, Decimal)
            or not underlying.bid_price.is_finite()
            or not isinstance(underlying.ask_price, Decimal)
            or not underlying.ask_price.is_finite()
            or underlying.bid_price <= 0
            or underlying.ask_price < underlying.bid_price
            or underlying.ask_price > _MAX_NUMERIC_MAGNITUDE
            or not _is_utc(underlying.quote_observed_at)
            or not _is_utc(underlying.quote_retrieved_at)
            or underlying.quote_observed_at > underlying.quote_retrieved_at
            or not _is_hash(underlying.quote_source_hash)
            or not _is_utc(underlying.completed_bar_at)
            or not _is_hash(underlying.completed_bar_source_hash)
            or not _is_hash(underlying.request_hash)
            or underlying.benchmark_symbol != context.launch_authority.benchmark_symbol
            or not _is_utc(underlying.benchmark_completed_bar_at)
            or not _is_hash(underlying.benchmark_completed_bar_source_hash)
            or underlying.completed_bar_at
            != observation.boundaries.price_confirmation[-1].completed_bar_at
            or underlying.completed_bar_source_hash
            != observation.boundaries.price_confirmation[-1].underlying_bar_source_hash
            or underlying.benchmark_completed_bar_at
            != observation.boundaries.price_confirmation[-1].completed_bar_at
            or underlying.benchmark_completed_bar_source_hash
            != observation.boundaries.price_confirmation[-1].benchmark_bar_source_hash
        ):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "LIFECYCLE_MARKET_TIMESTAMP_INVALID",
            )
        freshness = check_freshness(
            trusted_at,
            (
                FreshnessInput(FreshnessKind.ACCOUNT, sweep.final_account.observed_at),
                FreshnessInput(FreshnessKind.POSITIONS, sweep.retrieval_completed_at),
                FreshnessInput(FreshnessKind.OPEN_ORDERS, sweep.retrieval_completed_at),
                FreshnessInput(
                    FreshnessKind.UNDERLYING_QUOTE,
                    underlying.quote_observed_at,
                ),
                FreshnessInput(FreshnessKind.UNDERLYING_QUOTE, underlying.quote_retrieved_at),
                FreshnessInput(FreshnessKind.COMPLETED_BAR, underlying.completed_bar_at),
                FreshnessInput(FreshnessKind.OPTION_SNAPSHOT, atm_iv.observed_at),
                FreshnessInput(FreshnessKind.OPTION_SNAPSHOT, atm_iv.retrieved_at),
            ),
        )
        if not freshness.complete:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, freshness.failures[0])
        synchronized_times = (
            underlying.quote_observed_at,
            atm_iv.observed_at,
            observation.boundaries.observed_at,
            *(item.quote_observed_at for item in options),
            *(item.greek_observed_at for item in options),
            *(item.quote_observed_at for item in replacement_options),
            *(item.greek_observed_at for item in replacement_options),
        )
        if max(synchronized_times) - min(synchronized_times) > _MARKET_SYNC_WINDOW:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "MARKET_EVIDENCE_NOT_SYNCHRONIZED",
            )
        parsed = _parse_vertical(context.expected_positions)
        dte = (parsed[0].expiry - trusted_at.date()).days
        return _ValidatedObservation(
            options=options,
            dte=dte,
            liquidation_pnl=_cumulative_cashflow(context) + _close_cashflow(options),
        )

    @staticmethod
    def _validate_clusters(clusters: tuple[SourceCluster, ...], trusted_at: datetime) -> None:
        if (
            not isinstance(clusters, tuple)
            or any(not isinstance(item, SourceCluster) for item in clusters)
            or len(clusters) > _MAX_RESEARCH_CLUSTERS
            or len({item.cluster_id for item in clusters}) != len(clusters)
            or any(not item.cluster_id or len(item.cluster_id) > 128 for item in clusters)
            or any(len(item.headline) > _MAX_TEXT_LENGTH for item in clusters)
            or any(
                item.independent_reporting_group is not None
                and len(item.independent_reporting_group) > 128
                for item in clusters
            )
            or any(
                not source_id or len(source_id) > 128
                for item in clusters
                for source_id in item.source_ids
            )
            or len({source_id for item in clusters for source_id in item.source_ids})
            != sum(len(item.source_ids) for item in clusters)
            or sum(len(item.source_ids) for item in clusters) > _MAX_SOURCE_IDS
            or any(not _is_utc(item.observed_at) for item in clusters)
        ):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "RESEARCH_CLUSTER_SET_INVALID")
        freshness = check_freshness(
            trusted_at,
            tuple(FreshnessInput(FreshnessKind.NEWS, item.observed_at) for item in clusters),
        )
        if not freshness.complete:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, freshness.failures[0])

    @staticmethod
    def _validate_classifications(
        context: RetainedLifecycleContext,
        clusters: tuple[SourceCluster, ...],
        classifications: tuple[EvidenceClassification, ...],
    ) -> None:
        if not isinstance(classifications, tuple) or any(
            not isinstance(item, EvidenceClassification) for item in classifications
        ):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "CLASSIFICATION_SET_INVALID",
            )
        by_cluster = {item.cluster_id: item for item in clusters}
        if len({item.cluster_id for item in classifications}) != len(classifications) or set(
            by_cluster
        ) != {item.cluster_id for item in classifications}:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "CLASSIFICATION_CLUSTER_SET_MISMATCH",
            )
        for item in classifications:
            cluster = by_cluster[item.cluster_id]
            invalidation_bound = item.invalidation_condition_id in set(
                context.thesis.thesis.invalidation_codes
            )
            if (
                item.source_ids != cluster.source_ids
                or item.source_tier is not cluster.source_tier
                or item.independent_reporting_group != cluster.independent_reporting_group
                or item.event_code not in _EVENT_CODES
                or item.invalidates != invalidation_bound
                or (invalidation_bound and item.relation.value != "CONTRADICTS")
            ):
                raise AcquisitionFailure(
                    AcquisitionKind.LIFECYCLE,
                    "CLASSIFICATION_SOURCE_BINDING_MISMATCH",
                )

    @staticmethod
    def _derive_hard_gates(
        context: RetainedLifecycleContext,
        boundaries: LifecycleBoundaryObservation,
        classifications: tuple[EvidenceClassification, ...],
        dte: int,
        trusted_at: datetime,
    ) -> HardGateInput:
        parsed = _parse_vertical(context.expected_positions)
        short = next(
            option
            for option, position in zip(parsed, context.expected_positions, strict=True)
            if position.signed_quantity < 0
        )
        if short.right == "C" and boundaries.short_call_close_at is None:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "SHORT_CALL_DIVIDEND_EVIDENCE_MISSING",
            )
        invalidating = tuple(
            item for item in classifications if item.invalidation_condition_id is not None
        )
        primary = any(item.source_tier.value == "PRIMARY" for item in invalidating)
        reporting_groups = {
            item.independent_reporting_group
            for item in invalidating
            if item.source_tier.value == "ORIGINAL_REPORTING"
            and item.independent_reporting_group is not None
        }
        price_broken = len(boundaries.price_confirmation) == 2 and all(
            point.vwap_side < 0 and point.relative_return_side < 0
            for point in boundaries.price_confirmation
        )
        current_max_loss = _current_vertical_max_loss(context)
        return HardGateInput(
            verified_invalidation=primary or len(reporting_groups) >= 2,
            price_confirmation_broken=price_broken,
            short_dte=dte,
            short_call_ex_dividend_boundary=bool(
                short.right == "C"
                and boundaries.short_call_close_at is not None
                and trusted_at >= boundaries.short_call_close_at
            ),
            bounded_as_approved=True,
            risk_cap_exceeded=(
                current_max_loss > context.portfolio_risk_cap
                or current_max_loss > context.approved_max_loss
            ),
            weekend_gate_failed=bool(
                boundaries.weekend_close_at is not None
                and trusted_at >= boundaries.weekend_close_at
            ),
            contest_end_window=bool(
                boundaries.contest_end_at is not None and trusted_at >= boundaries.contest_end_at
            ),
        )

    @staticmethod
    def _derive_roll_candidate(
        context: RetainedLifecycleContext,
        observation: LifecycleProviderObservation,
        current_scores: ScoreInput,
        roll_session: AlpacaMarketSession,
        trusted_at: datetime,
    ) -> _RollSelection | None:
        candidates = observation.roll_candidates
        if observation.roll is not None:
            candidates = (observation.roll, *candidates)
        if not candidates:
            return None
        if (
            not isinstance(candidates, tuple)
            or len(candidates) > 8
            or any(not isinstance(item, LifecycleRollObservation) for item in candidates)
        ):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ROLL_EVIDENCE_INVALID")
        selections = tuple(
            DevelopmentLifecycleAcquisition._derive_single_roll_candidate(
                context,
                roll,
                observation.options,
                current_scores,
                roll_session,
                trusted_at,
            )
            for roll in candidates
        )
        eligible = tuple(
            item
            for item in selections
            if item.candidate.valid
            and item.candidate.relative_spread <= item.candidate.maximum_relative_spread
            and item.candidate.incremental_debit <= item.candidate.maximum_incremental_debit
        )
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda item: (
                -item.candidate.drift_reduction,
                item.candidate.incremental_debit,
                item.candidate.relative_spread,
                tuple(position.symbol for position in item.observation.positions),
            ),
        )

    @staticmethod
    def _derive_single_roll_candidate(
        context: RetainedLifecycleContext,
        roll: LifecycleRollObservation,
        current_options: tuple[LifecycleOptionObservation, ...],
        current_scores: ScoreInput,
        roll_session: AlpacaMarketSession,
        trusted_at: datetime,
    ) -> _RollSelection:
        try:
            current = _parse_vertical(context.expected_positions)
            replacement = _parse_vertical(roll.positions)
            options = _validate_option_evidence(
                roll.positions,
                roll.options,
                trusted_at,
                context.greek_authority,
            )
        except AcquisitionFailure:
            raise
        except (TypeError, ValueError) as error:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "ROLL_EVIDENCE_INVALID",
            ) from error
        if (
            replacement[0].root != current[0].root
            or replacement[0].right != current[0].right
            or replacement[0].expiry <= current[0].expiry
            or tuple(item.signed_quantity for item in roll.positions)
            != tuple(item.signed_quantity for item in context.expected_positions)
        ):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ROLL_EVIDENCE_INVALID")
        current_close_cashflow = current_scores.liquidation_pnl - _cumulative_cashflow(context)
        roll_cashflow = current_close_cashflow + _open_cashflow(options)
        incremental_debit = max(Decimal(0), -roll_cashflow)
        maximum_incremental_debit = min(
            context.approved_max_loss,
            context.portfolio_risk_cap,
        )
        all_legs_liquid = all(
            _leg_relative_spread(option) <= context.maximum_relative_spread
            for option in (*current_options, *options)
        )
        relative_spread = _package_relative_spread((*current_options, *options))
        rolled_cumulative_cashflow = _cumulative_cashflow(context) + roll_cashflow
        expected_roll_max_loss = _vertical_max_loss(
            roll.positions,
            rolled_cumulative_cashflow,
        )
        try:
            exposure = _aggregate_option_greeks(options, "ROLL_EVIDENCE_INVALID")
            replacement_scores = ScoreInput(
                evidence_drift=current_scores.evidence_drift,
                delta=exposure.delta,
                delta_low=current_scores.delta_low,
                delta_high=current_scores.delta_high,
                vega=exposure.vega_per_iv_point,
                vega_low=current_scores.vega_low,
                vega_high=current_scores.vega_high,
                theta_per_day=exposure.theta_per_day,
                max_daily_theta=current_scores.max_daily_theta,
                dte=(replacement[0].expiry - trusted_at.date()).days,
                minimum_dte=current_scores.minimum_dte,
                maximum_dte=current_scores.maximum_dte,
                horizon_fraction=current_scores.horizon_fraction,
                volatility_view=current_scores.volatility_view,
                entry_atm_iv=current_scores.entry_atm_iv,
                current_atm_iv=current_scores.current_atm_iv,
                liquidation_pnl=rolled_cumulative_cashflow + _close_cashflow(options),
                approved_max_loss=current_scores.approved_max_loss,
            )
            drift_reduction = (
                score_drift(current_scores).display_score
                - score_drift(replacement_scores).display_score
            )
        except (ExposureBlocked, ValueError) as error:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ROLL_EVIDENCE_INVALID") from error
        candidate = RollCandidate(
            valid=all_legs_liquid,
            drift_reduction=drift_reduction,
            expiry_extension_days=(replacement[0].expiry - current[0].expiry).days,
            relative_spread=relative_spread,
            maximum_relative_spread=context.maximum_relative_spread,
            incremental_debit=incremental_debit,
            maximum_incremental_debit=maximum_incremental_debit,
            within_loss_budget=expected_roll_max_loss <= maximum_incremental_debit,
            covered_verticals=True,
            no_prior_roll_today=not any(
                item.action == "ROLL" and item.market_session_id == roll_session.market_session_id
                for item in context.lifecycle_transitions
            ),
            expected_after_exposure=exposure,
        )
        return _RollSelection(candidate, roll)


class AccountAuthorityPort(Protocol):
    def observe(self) -> ObservedPaperAccountAuthority: ...


class TrustedClockPort(Protocol):
    def now(self) -> datetime: ...


class CalibrationBindingPort(Protocol):
    def binding_for(self, authority: ObservedPaperAccountAuthority) -> CalibrationBinding: ...


class AgentAcquisitionPort(Protocol):
    async def acquire(
        self,
        authority: ObservedPaperAccountAuthority,
        trusted_at: datetime,
        tick_id: UUID,
        *,
        actor: Actor,
    ) -> DecisionAcquisition:
        """Await a complete policy bundle or raise a typed acquisition failure.

        Production adapters can await the official MCP client directly. Provider
        errors must be translated to ``AcquisitionFailure`` with the correct kind.
        """
        ...


def _is_hash(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _is_utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


def _valid_quantity(value: object) -> bool:
    return bool(
        isinstance(value, Decimal)
        and value.is_finite()
        and value != 0
        and value == value.to_integral_value()
        and abs(value) <= MAX_STRUCTURAL_OPTION_QUANTITY
    )


def _account_material(account: AccountObservation) -> tuple[object, ...]:
    return (
        account.role,
        account.account_fingerprint,
        account.paper,
        account.status,
        account.account_blocked,
        account.trading_blocked,
        account.options_trading_blocked,
        # Equity and buying power move with option marks between the bookend reads.
        account.cash,
    )


def _aggregate_option_greeks(
    options: tuple[LifecycleOptionObservation, ...],
    failure_code: str,
) -> GreekExposure:
    try:
        return aggregate_greeks(
            tuple(
                GreekLeg(
                    contracts=int(abs(item.signed_quantity)),
                    is_long=item.signed_quantity > 0,
                    delta=item.delta,
                    gamma=item.gamma,
                    theta_per_day=item.theta_per_day,
                    vega_per_iv_point=item.vega_per_iv_point,
                    multiplier=item.multiplier,
                    units_verified=True,
                )
                for item in options
            )
        )
    except (ExposureBlocked, ValueError) as error:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, failure_code) from error


def _close_cashflow(options: tuple[LifecycleOptionObservation, ...]) -> Decimal:
    return sum(
        (item.bid_price if item.signed_quantity > 0 else -item.ask_price)
        * abs(item.signed_quantity)
        * item.multiplier
        for item in options
    )


def _open_cashflow(options: tuple[LifecycleOptionObservation, ...]) -> Decimal:
    return sum(
        (-item.ask_price if item.signed_quantity > 0 else item.bid_price)
        * abs(item.signed_quantity)
        * item.multiplier
        for item in options
    )


def _package_relative_spread(options: tuple[LifecycleOptionObservation, ...]) -> Decimal:
    gross_midpoint = sum(
        ((item.bid_price + item.ask_price) / Decimal(2)) * abs(item.signed_quantity)
        for item in options
    )
    quoted_width = sum(
        (item.ask_price - item.bid_price) * abs(item.signed_quantity) for item in options
    )
    if gross_midpoint <= 0 or quoted_width < 0:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ROLL_LIQUIDITY_INVALID")
    return (quoted_width / gross_midpoint).quantize(
        Decimal("0.0000000001"),
        rounding=ROUND_CEILING,
    )


def _leg_relative_spread(option: LifecycleOptionObservation) -> Decimal:
    midpoint = (option.bid_price + option.ask_price) / Decimal(2)
    width = option.ask_price - option.bid_price
    if midpoint <= 0 or width < 0:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ROLL_LIQUIDITY_INVALID")
    return (width / midpoint).quantize(
        Decimal("0.0000000001"),
        rounding=ROUND_CEILING,
    )


def lifecycle_liquidity_authority_hash(
    policy_hash: str,
    maximum_relative_spread: Decimal,
) -> str:
    payload = {
        "domain": "alphadecay.lifecycle-liquidity-authority.v1",
        "value": {
            "maximum_relative_spread": maximum_relative_spread,
            "policy_hash": policy_hash,
        },
    }
    return hashlib.sha256(
        json.dumps(
            _manifest_value(payload),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _lifecycle_proposal(
    *,
    context: RetainedLifecycleContext,
    observation: LifecycleProviderObservation,
    values: AssessmentInput,
    selected_roll: LifecycleRollObservation | None,
    trusted_at: datetime,
) -> AuthorizationIntentProposal | None:
    result = evaluate_assessment(values)
    action = {
        ExecutionDecision.CLOSE_APPROVED: ExecutionAction.CLOSE,
        ExecutionDecision.CLOSE_RISK_ONLY: ExecutionAction.CLOSE,
        ExecutionDecision.ROLL_APPROVED: ExecutionAction.ROLL,
    }.get(result.execution_decision)
    if action is None:
        return None

    current_legs = tuple(
        OrderLegIntent(
            item.symbol,
            (
                PositionIntent.SELL_TO_CLOSE
                if item.signed_quantity > 0
                else PositionIntent.BUY_TO_CLOSE
            ),
            1,
        )
        for item in context.expected_positions
    )
    current_options = observation.options
    eligible_alternatives = tuple(item for item in result.response.alternatives if item.eligible)
    if len(eligible_alternatives) != 1:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "LIFECYCLE_ALTERNATIVE_INVALID")
    expected_after_exposure = eligible_alternatives[0].expected_exposure
    if action is ExecutionAction.ROLL:
        if selected_roll is None or values.roll_candidate is None:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ROLL_EVIDENCE_REQUIRED")
        opening_legs = tuple(
            OrderLegIntent(
                item.symbol,
                (
                    PositionIntent.BUY_TO_OPEN
                    if item.signed_quantity > 0
                    else PositionIntent.SELL_TO_OPEN
                ),
                1,
            )
            for item in selected_roll.positions
        )
        legs = current_legs + opening_legs
        quoted_options = current_options + selected_roll.options
        if expected_after_exposure is None:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ROLL_EXPOSURE_REQUIRED")
        payoff_width = _vertical_width(context.expected_positions) + _vertical_width(
            selected_roll.positions
        )
    else:
        legs = current_legs
        quoted_options = current_options
        payoff_width = _vertical_width(context.expected_positions)

    minimum_limit, maximum_limit = _provider_limit_bounds(
        legs,
        quoted_options,
        payoff_width,
    )
    certificate_id = uuid5(
        NAMESPACE_URL,
        f"alphadecay:lifecycle-certificate:{values.assessment_id}:{action.value}",
    )
    quantity = int(abs(context.expected_positions[0].signed_quantity))
    envelope = OrderEnvelope(
        action=action,
        authorization_certificate_id=certificate_id,
        policy_hash=context.policy_hash,
        account_fingerprint=context.account_fingerprint,
        position_or_book_fingerprint=context.position_fingerprint,
        legs=legs,
        quantity=quantity,
        minimum_limit=minimum_limit,
        maximum_limit=maximum_limit,
        approved_max_loss=context.approved_max_loss,
        event_key=f"lifecycle:{values.assessment_id}:{action.value.lower()}",
        trading_day=observation.boundaries.market_session.session_date,
        market_session_id=(
            observation.boundaries.market_session.market_session_id
            if action is ExecutionAction.ROLL
            else None
        ),
        quoted_relative_spread=(
            values.roll_candidate.relative_spread
            if action is ExecutionAction.ROLL and values.roll_candidate is not None
            else None
        ),
        maximum_relative_spread=(
            values.roll_candidate.maximum_relative_spread
            if action is ExecutionAction.ROLL and values.roll_candidate is not None
            else None
        ),
        incremental_debit=(
            values.roll_candidate.incremental_debit
            if action is ExecutionAction.ROLL and values.roll_candidate is not None
            else None
        ),
        maximum_incremental_debit=(
            values.roll_candidate.maximum_incremental_debit
            if action is ExecutionAction.ROLL and values.roll_candidate is not None
            else None
        ),
    )
    expires_at = min(
        observation.boundaries.market_session.close_at,
        *(item.quote_observed_at + _OPTION_AUTHORIZATION_TTL for item in quoted_options),
        *(item.retrieved_at + _OPTION_AUTHORIZATION_TTL for item in quoted_options),
    )
    if expires_at <= trusted_at:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "LIFECYCLE_AUTHORIZATION_EXPIRED")
    authorization = AssessmentCertificate(
        certificate_id=certificate_id,
        assessment_id=values.assessment_id,
        thesis_version_id=context.thesis_version_id,
        account_role=context.account_role,
        action=action,
        position_fingerprint=context.position_fingerprint,
        envelope_hash=order_envelope_hash(envelope),
        approved_max_loss=context.approved_max_loss,
        quantity=quantity,
        expected_after_exposure=expected_after_exposure,
        policy_hash=context.policy_hash,
        created_at=trusted_at,
        expires_at=expires_at,
    )
    intent = ExecutionIntent(
        intent_id=uuid5(NAMESPACE_URL, f"alphadecay:lifecycle-intent:{intent_digest(envelope)}"),
        account_role=context.account_role,
        envelope=envelope,
        digest=intent_digest(envelope),
        state=IntentState.APPROVED,
    )
    return AuthorizationIntentProposal(authorization, intent)


def _vertical_width(positions: tuple[RetainedOptionPosition, ...]) -> Decimal:
    parsed = _parse_vertical(positions)
    return parsed[1].strike - parsed[0].strike


def _provider_limit_bounds(
    legs: tuple[OrderLegIntent, ...],
    options: tuple[LifecycleOptionObservation, ...],
    payoff_width: Decimal,
) -> tuple[Decimal, Decimal]:
    by_symbol = {item.symbol: item for item in options}
    if len(by_symbol) != len(options) or set(by_symbol) != {leg.symbol for leg in legs}:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "LIFECYCLE_ORDER_QUOTES_INVALID")
    midpoint = Decimal(0)
    natural = Decimal(0)
    for leg in legs:
        quote = by_symbol[leg.symbol]
        middle = (quote.bid_price + quote.ask_price) / Decimal(2)
        if leg.intent in {PositionIntent.BUY_TO_OPEN, PositionIntent.BUY_TO_CLOSE}:
            midpoint += middle
            natural += quote.ask_price
        else:
            midpoint -= middle
            natural -= quote.bid_price
    cent = Decimal("0.01")
    minimum_limit = midpoint.quantize(cent, rounding=ROUND_FLOOR)
    maximum_limit = natural.quantize(cent, rounding=ROUND_CEILING)
    if (
        minimum_limit == 0
        or maximum_limit == 0
        or minimum_limit > maximum_limit
        or abs(minimum_limit) >= payoff_width
        or abs(maximum_limit) >= payoff_width
    ):
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "LIFECYCLE_ORDER_PRICE_INVALID")
    return minimum_limit, maximum_limit


def _source_failure(stage: str, error: Exception) -> AcquisitionFailure:
    candidate = getattr(error, "code", None)
    code = (
        candidate if isinstance(candidate, str) and _FAILURE_CODE.fullmatch(candidate) else "FAILED"
    )
    return AcquisitionFailure(AcquisitionKind.LIFECYCLE, f"{stage}_{code}")


def _timedelta_microseconds(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _parse_vertical(
    positions: tuple[RetainedOptionPosition, ...],
) -> tuple[_ParsedOption, _ParsedOption]:
    if (
        not isinstance(positions, tuple)
        or len(positions) != 2
        or any(not isinstance(item, RetainedOptionPosition) for item in positions)
    ):
        raise ValueError("VERTICAL_STRUCTURE_INVALID")
    parsed: list[_ParsedOption] = []
    for item in positions:
        try:
            contract = parse_standard_option_contract_symbol(item.symbol)
        except OptionContractSymbolError as error:
            if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
                raise ValueError(error.code) from error
            raise ValueError("VERTICAL_OCC_INVALID") from error
        parsed.append(
            _ParsedOption(
                symbol=item.symbol,
                root=contract.root_symbol,
                expiry=contract.expiration_date,
                right=contract.right,
                strike=contract.strike_price,
            )
        )
    quantities = tuple(item.signed_quantity for item in positions)
    if (
        parsed[0].root != parsed[1].root
        or parsed[0].right != parsed[1].right
        or parsed[0].expiry != parsed[1].expiry
        or parsed[0].strike >= parsed[1].strike
        or any(item.multiplier != 100 for item in positions)
        or any(not _valid_quantity(value) for value in quantities)
        or {value > 0 for value in quantities} != {True, False}
        or abs(quantities[0]) != abs(quantities[1])
    ):
        raise ValueError("VERTICAL_STRUCTURE_INVALID")
    return parsed[0], parsed[1]


def _position_fingerprint(positions: tuple[RetainedOptionPosition, ...]) -> str:
    return option_position_fingerprint(
        tuple((item.symbol, item.signed_quantity, item.multiplier) for item in positions)
    )


def _position_fingerprint_from_inventory(positions: tuple[InventoryItem, ...]) -> str:
    retained = tuple(
        RetainedOptionPosition(item.symbol, item.signed_quantity, item.multiplier)
        for item in positions
        if item.kind is InventoryKind.OPTION
    )
    if len(retained) != len(positions):
        return ""
    return _position_fingerprint(retained)


def _validate_option_evidence(
    positions: tuple[RetainedOptionPosition, RetainedOptionPosition],
    options: tuple[LifecycleOptionObservation, LifecycleOptionObservation],
    trusted_at: datetime,
    authority: GreekAuthorityEvidence,
) -> tuple[LifecycleOptionObservation, LifecycleOptionObservation]:
    try:
        parsed = _parse_vertical(positions)
    except (TypeError, ValueError) as error:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "OPTION_STRUCTURE_INVALID") from error
    if (
        not isinstance(options, tuple)
        or len(options) != 2
        or any(not isinstance(item, LifecycleOptionObservation) for item in options)
        or tuple(item.symbol for item in options) != tuple(item.symbol for item in positions)
        or tuple(item.signed_quantity for item in options)
        != tuple(item.signed_quantity for item in positions)
    ):
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "OPTION_EVIDENCE_INCOMPLETE")
    for item in options:
        decimals = (
            item.signed_quantity,
            item.bid_price,
            item.ask_price,
            item.delta,
            item.gamma,
            item.theta_per_day,
            item.vega_per_iv_point,
        )
        if (
            any(not isinstance(value, Decimal) or not value.is_finite() for value in decimals)
            or item.multiplier != 100
            or item.active is not True
            or item.tradable is not True
            or item.feed != "indicative"
            or item.bid_price <= 0
            or item.ask_price < item.bid_price
            or item.ask_price > _MAX_NUMERIC_MAGNITUDE
            or not Decimal("-1") <= item.delta <= Decimal("1")
            or not Decimal(0) <= item.gamma <= Decimal(10)
            or abs(item.theta_per_day) > Decimal(1000)
            or not Decimal(0) <= item.vega_per_iv_point <= Decimal(1000)
            or not _is_utc(item.quote_observed_at)
            or not _is_utc(item.greek_observed_at)
            or not _is_utc(item.retrieved_at)
            or item.greek_authority_id != authority.authority_id
            or item.quote_observed_at > item.retrieved_at
            or item.greek_observed_at > item.retrieved_at
            or not _is_hash(item.greek_timestamp_source_hash)
            or not _is_hash(item.greek_units_source_hash)
            or not _is_hash(item.source_hash)
            or item.greek_units_source_hash != authority.units_source_hash
            or item.greek_timestamp_source_hash != authority.timestamp_contract_hash
            or item.greek_timestamp_source_hash == item.source_hash
        ):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "OPTION_EVIDENCE_INVALID")
        freshness = check_freshness(
            trusted_at,
            (
                FreshnessInput(FreshnessKind.OPTION_SNAPSHOT, item.quote_observed_at),
                FreshnessInput(FreshnessKind.OPTION_SNAPSHOT, item.greek_observed_at),
                FreshnessInput(FreshnessKind.OPTION_SNAPSHOT, item.retrieved_at),
            ),
        )
        if not freshness.complete:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, freshness.failures[0])
    mids = tuple((item.bid_price + item.ask_price) / Decimal(2) for item in options)
    if (
        len({item.source_hash for item in options}) != 2
        or options[0].delta <= options[1].delta
        or (parsed[0].right == "C" and mids[0] <= mids[1])
        or (parsed[0].right == "P" and mids[0] >= mids[1])
        or abs(
            (options[0].bid_price if options[0].signed_quantity > 0 else -options[0].ask_price)
            + (options[1].bid_price if options[1].signed_quantity > 0 else -options[1].ask_price)
        )
        >= parsed[1].strike - parsed[0].strike
    ):
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "OPTION_STRUCTURE_INVALID")
    return options


def _validate_boundaries(
    boundaries: LifecycleBoundaryObservation,
    trusted_at: datetime,
) -> None:
    if (
        not isinstance(boundaries, LifecycleBoundaryObservation)
        or not isinstance(boundaries.market_session, AlpacaMarketSession)
        or not boundaries.market_session.open_at <= trusted_at <= boundaries.market_session.close_at
        or boundaries.market_session.retrieved_at - trusted_at > _RETRIEVAL_SKEW_TOLERANCE
        or not _is_utc(boundaries.observed_at)
        or not _is_hash(boundaries.source_hash)
        or not isinstance(boundaries.price_confirmation, tuple)
        or len(boundaries.price_confirmation) != 2
        or any(
            not isinstance(item, PriceConfirmationPoint)
            or not _is_utc(item.completed_bar_at)
            or not _is_hash(item.source_hash)
            or not _is_hash(item.underlying_bar_source_hash)
            or not _is_hash(item.benchmark_bar_source_hash)
            or not isinstance(item.vwap_side, Decimal)
            or not item.vwap_side.is_finite()
            or not isinstance(item.relative_return_side, Decimal)
            or not item.relative_return_side.is_finite()
            or abs(item.vwap_side) > _MAX_NUMERIC_MAGNITUDE
            or abs(item.relative_return_side) > _MAX_NUMERIC_MAGNITUDE
            for item in boundaries.price_confirmation
        )
        or len({item.completed_bar_at for item in boundaries.price_confirmation}) != 2
        or tuple(item.completed_bar_at for item in boundaries.price_confirmation)
        != tuple(sorted(item.completed_bar_at for item in boundaries.price_confirmation))
        or any(
            value is not None and not _is_utc(value)
            for value in (
                boundaries.short_call_close_at,
                boundaries.weekend_close_at,
                boundaries.contest_end_at,
            )
        )
        or boundaries.weekend_close_at is None
        or boundaries.contest_end_at is None
    ):
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "BOUNDARY_EVIDENCE_INVALID")
    freshness = check_freshness(
        trusted_at,
        (
            FreshnessInput(FreshnessKind.COMPLETED_BAR, boundaries.observed_at),
            *(
                FreshnessInput(FreshnessKind.COMPLETED_BAR, item.completed_bar_at)
                for item in boundaries.price_confirmation
            ),
        ),
    )
    if not freshness.complete:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, freshness.failures[0])


def _current_vertical_max_loss(context: RetainedLifecycleContext) -> Decimal:
    return _vertical_max_loss(context.expected_positions, _cumulative_cashflow(context))


def _cumulative_cashflow(context: RetainedLifecycleContext) -> Decimal:
    return sum((item.cashflow for item in context.lifecycle_transitions), Decimal(0))


def _vertical_max_loss(
    positions: tuple[RetainedOptionPosition, RetainedOptionPosition],
    cumulative_cashflow: Decimal,
) -> Decimal:
    parsed = _parse_vertical(positions)
    width_cashflow = (
        (parsed[1].strike - parsed[0].strike) * abs(positions[0].signed_quantity) * Decimal(100)
    )
    lower_is_long = positions[0].signed_quantity > 0
    debit = lower_is_long if parsed[0].right == "C" else not lower_is_long
    worst_settlement = Decimal(0) if debit else -width_cashflow
    return max(Decimal(0), -(cumulative_cashflow + worst_settlement))


def _acquisition_manifest_hash(
    context: RetainedLifecycleContext,
    observation: LifecycleProviderObservation,
    clusters: tuple[SourceCluster, ...],
    classifications: tuple[EvidenceClassification, ...],
) -> str:
    material = _manifest_value(
        {
            "domain": "alphadecay.lifecycle-acquisition-manifest.v1",
            "context": context,
            "observation": observation,
            "clusters": clusters,
            "classifications": classifications,
        }
    )
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ACQUISITION_MANIFEST_TOO_LARGE")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_value(value: object, *, _depth: int = 0, _items: list[int] | None = None) -> object:
    if _depth > _MAX_MANIFEST_DEPTH:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ACQUISITION_MANIFEST_TOO_DEEP")
    if _items is None:
        _items = [0]
    _items[0] += 1
    if _items[0] > _MAX_MANIFEST_ITEMS:
        raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ACQUISITION_MANIFEST_TOO_LARGE")
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _manifest_value(model_dump(mode="json"), _depth=_depth + 1, _items=_items)
    if is_dataclass(value) and not isinstance(value, type):
        return _manifest_value(asdict(value), _depth=_depth + 1, _items=_items)
    if isinstance(value, dict):
        return {
            str(key): _manifest_value(item, _depth=_depth + 1, _items=_items)
            for key, item in value.items()
        }
    if isinstance(value, tuple | list):
        return [_manifest_value(item, _depth=_depth + 1, _items=_items) for item in value]
    if isinstance(value, StrEnum | UUID):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite() or abs(value) > _MAX_NUMERIC_MAGNITUDE:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ACQUISITION_NUMERIC_INVALID")
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return _timedelta_microseconds(value)
    if isinstance(value, str):
        if len(value) > _MAX_TEXT_LENGTH:
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ACQUISITION_TEXT_TOO_LONG")
        return value
    if isinstance(value, int):
        if abs(value) > int(_MAX_NUMERIC_MAGNITUDE):
            raise AcquisitionFailure(AcquisitionKind.LIFECYCLE, "ACQUISITION_NUMERIC_INVALID")
        return value
    if isinstance(value, bool) or value is None:
        return value
    raise ValueError("ACQUISITION_MANIFEST_VALUE_INVALID")
