from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from backend.app.contracts.v1 import AccountRole
from backend.app.execution import (
    Actor,
    AssessmentCertificate,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionBlocked,
    ExecutionCertificate,
    OrderLegIntent,
    intent_digest,
    order_envelope_hash,
)
from backend.app.execution.models import ExecutionIntent, IntentState
from backend.app.policy import (
    AssessmentInput,
    ExecutionDecision,
    OpportunityDecisionRecord,
    OpportunityInput,
    OpportunityOutcome,
    evaluate_assessment,
    evaluate_opportunity,
)
from backend.app.policy.evaluation import PolicyResult

from .acquisition import (
    AccountAuthorityPort,
    AcquisitionFailure,
    AcquisitionKind,
    AgentAcquisitionPort,
    AuthorizationIntentProposal,
    CalibrationBinding,
    CalibrationBindingPort,
    DecisionAcquisition,
    LifecycleAcquisition,
    LifecycleLaunchAuthority,
    ObservedPaperAccountAuthority,
    OpportunityAcquisition,
    OpportunityNoTradeAcquisition,
    PermanentAccountLatch,
    TrustedClockPort,
)


@dataclass(frozen=True)
class AgentDecision:
    code: str
    decided_at: datetime
    thesis_version_id: UUID | None = None
    calibration: CalibrationBinding | None = None
    opportunity: OpportunityDecisionRecord | None = None
    lifecycle: PolicyResult | None = None
    provider_failure_code: str | None = None
    provider_failure_kind: AcquisitionKind | None = None
    normalized_input: OpportunityInput | AssessmentInput | None = None


@dataclass(frozen=True)
class AgentTick:
    tick_id: UUID
    reservation_token: UUID
    authority: ObservedPaperAccountAuthority
    actor: Actor
    trusted_at: datetime


@dataclass(frozen=True)
class PersistedAgentDecision:
    decision: AgentDecision
    approved_intent: ExecutionIntent | None


@dataclass(frozen=True)
class AgentRunResult:
    tick_id: UUID
    terminal_code: str
    decision: AgentDecision
    approved_intent_id: UUID | None
    execution_certificate_id: UUID | None
    proof_hash: str


class AgentDecisionRepository(Protocol):
    def begin_tick(
        self,
        authority: ObservedPaperAccountAuthority,
        actor: Actor,
        trusted_at: datetime,
    ) -> AgentTick | AgentRunResult:
        """Atomically claim the durable tick or return its completed result.

        An adapter must never return a new ``AgentTick`` for an already claimed
        account/time/actor key. Concurrent or restarted callers must receive the
        same durable suppression or terminal result before acquisition can run.
        The returned tick carries the durable reservation token required by later
        persistence and completion; adapters must not recover it from local state.
        """
        ...

    def permanent_latch(self, authority: ObservedPaperAccountAuthority) -> PermanentAccountLatch:
        """Read the permanent account latch; this port intentionally has no clear."""
        ...

    def persist_decision(
        self,
        tick: AgentTick,
        decision: AgentDecision,
        proposal: AuthorizationIntentProposal | None,
    ) -> PersistedAgentDecision:
        """Commit the decision and optional authorization/intent atomically.

        ``approved_intent`` is returned only when that exact proposal was durably
        committed as approved by the same transaction.
        """
        ...

    def complete_tick(
        self,
        tick: AgentTick,
        terminal_code: str,
        certificate: ExecutionCertificate | None,
    ) -> AgentRunResult:
        """Use the tick reservation to persist a terminal result and proof once."""
        ...


class RuntimeExecutionPort(Protocol):
    def execute(self, intent_id: UUID, actor: Actor, now: datetime) -> ExecutionCertificate: ...


class RuntimeCompositionPort(Protocol):
    execution: RuntimeExecutionPort


class EntryMaterializationPort(Protocol):
    def prepare(
        self,
        *,
        execution_intent_id: UUID,
        launch_authority: LifecycleLaunchAuthority,
        prepared_at: datetime,
    ) -> None: ...

    def materialize(
        self,
        *,
        execution_certificate_id: UUID,
        launch_authority: LifecycleLaunchAuthority,
    ) -> UUID: ...

    def recover_pending(
        self,
        *,
        account_role: str,
        account_fingerprint: str,
    ) -> tuple[UUID, ...]: ...

    def pending_execution_intents(
        self,
        *,
        account_role: str,
        account_fingerprint: str,
    ) -> tuple[UUID, ...]: ...


class LifecycleTerminalMaterializationPort(Protocol):
    def materialize(self, *, execution_certificate_id: UUID) -> UUID: ...


class AgentRunService:
    def __init__(
        self,
        *,
        account_authority: AccountAuthorityPort,
        clock: TrustedClockPort,
        calibration: CalibrationBindingPort,
        acquisition: AgentAcquisitionPort,
        decisions: AgentDecisionRepository,
        runtime: RuntimeCompositionPort,
        server_autonomy_enabled: bool,
        submission_opportunity_enabled: bool = False,
        entry_materializer: EntryMaterializationPort | None = None,
        lifecycle_terminal_materializer: LifecycleTerminalMaterializationPort | None = None,
    ) -> None:
        self._account_authority = account_authority
        self._clock = clock
        self._calibration = calibration
        self._acquisition = acquisition
        self._decisions = decisions
        self._runtime = runtime
        self._server_autonomy_enabled = server_autonomy_enabled
        self._submission_opportunity_enabled = submission_opportunity_enabled
        self._entry_materializer = entry_materializer
        self._lifecycle_terminal_materializer = lifecycle_terminal_materializer

    async def run(self, actor: Actor) -> AgentRunResult:
        authority = self._account_authority.observe()
        trusted_at = self._clock.now()
        if trusted_at.tzinfo is None or trusted_at.utcoffset() != timedelta(0):
            raise ValueError("TRUSTED_TIME_MUST_BE_UTC")
        submission_authorized = (
            authority.role is AccountRole.SUBMISSION
            and self._submission_opportunity_enabled
            and self._server_autonomy_enabled
            and authority.persistent_autonomy_enabled
            and actor is Actor.SCHEDULER
        )
        executable_role = authority.role is AccountRole.DEVELOPMENT or submission_authorized
        if executable_role and self._entry_materializer is not None:
            try:
                self._entry_materializer.recover_pending(
                    account_role=authority.role.value,
                    account_fingerprint=authority.account_fingerprint,
                )
                pending = self._entry_materializer.pending_execution_intents(
                    account_role=authority.role.value,
                    account_fingerprint=authority.account_fingerprint,
                )
                if len(pending) > 1:
                    raise ExecutionBlocked("ENTRY_EXECUTION_RECOVERY_CONFLICT")
                if pending and actor is not Actor.SCHEDULER:
                    raise ExecutionBlocked("ENTRY_EXECUTION_RECOVERY_SCHEDULER_REQUIRED")
                for intent_id in pending:
                    certificate = self._runtime.execution.execute(intent_id, actor, trusted_at)
                    if certificate.intent_id != intent_id:
                        raise ExecutionBlocked("ENTRY_EXECUTION_RECOVERY_CERTIFICATE_MISMATCH")
                if pending:
                    self._entry_materializer.recover_pending(
                        account_role=authority.role.value,
                        account_fingerprint=authority.account_fingerprint,
                    )
                    if self._entry_materializer.pending_execution_intents(
                        account_role=authority.role.value,
                        account_fingerprint=authority.account_fingerprint,
                    ):
                        raise ExecutionBlocked("ENTRY_EXECUTION_RECOVERY_UNRESOLVED")
            except RuntimeError as error:
                raise ExecutionBlocked("ENTRY_MATERIALIZATION_RECOVERY_FAILED") from error
        started = self._decisions.begin_tick(authority, actor, trusted_at)
        if isinstance(started, AgentRunResult):
            return started
        latch = self._decisions.permanent_latch(authority)
        if authority.role is AccountRole.SUBMISSION and not submission_authorized:
            binding = self._calibration.binding_for(authority)
            if (
                binding.account_role is not authority.role
                or binding.account_fingerprint != authority.account_fingerprint
            ):
                raise ValueError("CALIBRATION_BINDING_AUTHORITY_MISMATCH")
            decision = AgentDecision(
                code="CALIBRATION_BINDING_NO_TRADE",
                decided_at=binding.sealed_at,
                calibration=binding,
            )
            self._decisions.persist_decision(started, decision, None)
            terminal_code = "ACCOUNT_PERMANENTLY_LATCHED" if latch.latched else decision.code
            return self._decisions.complete_tick(started, terminal_code, None)
        if latch.latched:
            decision = AgentDecision(
                code="ACCOUNT_PERMANENTLY_LATCHED",
                decided_at=trusted_at,
            )
            self._decisions.persist_decision(started, decision, None)
            return self._decisions.complete_tick(started, decision.code, None)
        try:
            acquisition = await self._acquisition.acquire(
                authority,
                trusted_at,
                started.tick_id,
                actor=actor,
            )
        except AcquisitionFailure as error:
            code = (
                "PROVIDER_FAILURE_NO_TRADE"
                if error.kind is AcquisitionKind.OPPORTUNITY
                else "PROVIDER_FAILURE_NO_ACTION"
            )
            decision = AgentDecision(
                code=code,
                decided_at=trusted_at,
                provider_failure_code=error.code,
                provider_failure_kind=error.kind,
            )
            self._decisions.persist_decision(started, decision, None)
            return self._decisions.complete_tick(started, decision.code, None)
        if isinstance(acquisition, OpportunityNoTradeAcquisition):
            return self._run_opportunity_no_trade(started, acquisition)
        if isinstance(acquisition, OpportunityAcquisition):
            return self._run_opportunity(started, acquisition)
        return self._run_lifecycle(started, acquisition)

    def _run_opportunity_no_trade(
        self,
        tick: AgentTick,
        acquisition: OpportunityNoTradeAcquisition,
    ) -> AgentRunResult:
        result = evaluate_opportunity(acquisition.policy, acquisition.values)
        if result != acquisition.decision or result.outcome is not OpportunityOutcome.NO_TRADE:
            raise ValueError("OPPORTUNITY_NO_TRADE_ACQUISITION_INVALID")
        decision = AgentDecision(
            code=result.outcome.value,
            decided_at=tick.trusted_at,
            opportunity=result,
            normalized_input=acquisition.values,
        )
        self._decisions.persist_decision(tick, decision, None)
        return self._decisions.complete_tick(tick, decision.code, None)

    def _run_opportunity(
        self,
        tick: AgentTick,
        acquisition: OpportunityAcquisition,
    ) -> AgentRunResult:
        result = evaluate_opportunity(acquisition.policy, acquisition.values)
        decision = AgentDecision(
            code=result.outcome.value,
            decided_at=tick.trusted_at,
            thesis_version_id=acquisition.thesis_version_id,
            opportunity=result,
            normalized_input=acquisition.values,
        )
        autonomous_scheduler = (
            tick.actor is Actor.SCHEDULER
            and self._server_autonomy_enabled
            and tick.authority.persistent_autonomy_enabled
        )
        proposal = (
            acquisition.proposal
            if result.outcome is OpportunityOutcome.ENTRY_APPROVED and autonomous_scheduler
            else None
        )
        if proposal is not None and not _valid_entry_proposal(tick, result, acquisition, proposal):
            proposal = None
        persisted = self._decisions.persist_decision(tick, decision, proposal)
        if result.outcome is not OpportunityOutcome.ENTRY_APPROVED or not autonomous_scheduler:
            return self._decisions.complete_tick(tick, decision.code, None)
        if proposal is None:
            return self._decisions.complete_tick(tick, "ENTRY_APPROVED_WITHOUT_INTENT", None)
        if persisted.approved_intent != proposal.intent:
            return self._decisions.complete_tick(tick, "APPROVED_INTENT_MISMATCH", None)
        if acquisition.launch_authority is None or self._entry_materializer is None:
            return self._decisions.complete_tick(
                tick,
                "ENTRY_MATERIALIZATION_PREPARATION_FAILED",
                None,
            )
        try:
            self._entry_materializer.prepare(
                execution_intent_id=persisted.approved_intent.intent_id,
                launch_authority=acquisition.launch_authority,
                prepared_at=tick.trusted_at,
            )
        except RuntimeError:
            return self._decisions.complete_tick(
                tick,
                "ENTRY_MATERIALIZATION_PREPARATION_FAILED",
                None,
            )
        return self._dispatch_entry(
            tick,
            persisted.approved_intent,
            acquisition.launch_authority,
        )

    def _run_lifecycle(
        self,
        tick: AgentTick,
        acquisition: DecisionAcquisition,
    ) -> AgentRunResult:
        if not isinstance(acquisition, LifecycleAcquisition):
            raise TypeError("AGENT_ACQUISITION_TYPE_INVALID")
        result = evaluate_assessment(acquisition.values)
        decision = AgentDecision(
            code=result.execution_decision.value,
            decided_at=tick.trusted_at,
            thesis_version_id=acquisition.thesis_version_id,
            lifecycle=result,
            normalized_input=acquisition.values,
        )
        approved = result.execution_decision in {
            ExecutionDecision.CLOSE_APPROVED,
            ExecutionDecision.CLOSE_RISK_ONLY,
            ExecutionDecision.ROLL_APPROVED,
        }
        autonomous_scheduler = (
            tick.actor is Actor.SCHEDULER
            and self._server_autonomy_enabled
            and tick.authority.persistent_autonomy_enabled
        )
        proposal = acquisition.proposal if approved and autonomous_scheduler else None
        if proposal is not None and not _valid_lifecycle_proposal(
            tick, result, acquisition, proposal
        ):
            proposal = None
        persisted = self._decisions.persist_decision(tick, decision, proposal)
        if not approved or not autonomous_scheduler:
            return self._decisions.complete_tick(tick, decision.code, None)
        if proposal is None:
            return self._decisions.complete_tick(tick, "ACTION_APPROVED_WITHOUT_INTENT", None)
        if persisted.approved_intent != proposal.intent:
            return self._decisions.complete_tick(tick, "APPROVED_INTENT_MISMATCH", None)
        return self._dispatch_lifecycle(tick, persisted.approved_intent)

    def _dispatch_lifecycle(self, tick: AgentTick, intent: ExecutionIntent) -> AgentRunResult:
        try:
            certificate = self._runtime.execution.execute(
                intent.intent_id,
                tick.actor,
                tick.trusted_at,
            )
        except ExecutionBlocked:
            return self._decisions.complete_tick(tick, "EXECUTION_BLOCKED", None)
        if certificate.execution_status != "FILLED":
            return self._decisions.complete_tick(
                tick,
                certificate.execution_status,
                certificate,
            )
        if (
            self._lifecycle_terminal_materializer is None
            or certificate.intent_id != intent.intent_id
            or certificate.entry_approval_id is not None
            or certificate.assessment_certificate_id != intent.envelope.authorization_certificate_id
            or intent.envelope.action not in {ExecutionAction.CLOSE, ExecutionAction.ROLL}
        ):
            return self._decisions.complete_tick(
                tick,
                "LIFECYCLE_FILLED_MATERIALIZATION_FAILED",
                certificate,
            )
        try:
            self._lifecycle_terminal_materializer.materialize(
                execution_certificate_id=certificate.certificate_id,
            )
        except RuntimeError:
            return self._decisions.complete_tick(
                tick,
                "LIFECYCLE_FILLED_MATERIALIZATION_FAILED",
                certificate,
            )
        return self._decisions.complete_tick(tick, certificate.execution_status, certificate)

    def _dispatch_entry(
        self,
        tick: AgentTick,
        intent: ExecutionIntent,
        launch_authority: LifecycleLaunchAuthority | None,
    ) -> AgentRunResult:
        try:
            certificate = self._runtime.execution.execute(
                intent.intent_id,
                tick.actor,
                tick.trusted_at,
            )
        except ExecutionBlocked:
            return self._decisions.complete_tick(tick, "EXECUTION_BLOCKED", None)
        if certificate.execution_status != "FILLED":
            return self._decisions.complete_tick(
                tick,
                certificate.execution_status,
                certificate,
            )
        if (
            launch_authority is None
            or self._entry_materializer is None
            or certificate.intent_id != intent.intent_id
            or certificate.entry_approval_id != intent.envelope.authorization_certificate_id
            or certificate.assessment_certificate_id is not None
        ):
            return self._decisions.complete_tick(
                tick,
                "ENTRY_FILLED_MATERIALIZATION_FAILED",
                certificate,
            )
        try:
            self._entry_materializer.materialize(
                execution_certificate_id=certificate.certificate_id,
                launch_authority=launch_authority,
            )
        except RuntimeError:
            return self._decisions.complete_tick(
                tick,
                "ENTRY_FILLED_MATERIALIZATION_FAILED",
                certificate,
            )
        return self._decisions.complete_tick(tick, certificate.execution_status, certificate)


def _valid_entry_proposal(
    tick: AgentTick,
    decision: OpportunityDecisionRecord,
    acquisition: OpportunityAcquisition,
    proposal: AuthorizationIntentProposal,
) -> bool:
    authorization = proposal.authorization
    intent = proposal.intent
    launch_authority = acquisition.launch_authority
    candidate = acquisition.values.candidate
    envelope = intent.envelope
    expected_legs = (
        tuple(OrderLegIntent(leg.symbol, leg.intent, leg.ratio) for leg in candidate.legs)
        if candidate is not None
        else ()
    )
    return bool(
        isinstance(authorization, EntryApprovalAuthorization)
        and isinstance(launch_authority, LifecycleLaunchAuthority)
        and launch_authority.entry_policy_hash == decision.policy_hash
        and launch_authority.entry_boundary_at <= tick.trusted_at
        and authorization.thesis_version_id == acquisition.thesis_version_id
        and candidate is not None
        and decision.quantity is not None
        and decision.approved_max_loss is not None
        and intent.state is IntentState.APPROVED
        and intent.digest == intent_digest(envelope)
        and _intent_is_unclaimed(intent)
        and intent.account_role is tick.authority.role
        and envelope.action is ExecutionAction.ENTRY
        and envelope.legs == expected_legs
        and envelope.event_key == decision.opportunity_key
        and envelope.trading_day == decision.decision_boundary.date()
        and envelope.authorization_certificate_id == authorization.approval_id
        and envelope.account_fingerprint == tick.authority.account_fingerprint
        and envelope.position_or_book_fingerprint == decision.book_fingerprint
        and envelope.policy_hash == decision.policy_hash == authorization.policy_hash
        and envelope.quantity == decision.quantity == authorization.quantity
        and envelope.approved_max_loss
        == decision.approved_max_loss
        == authorization.approved_max_loss
        and envelope.minimum_limit == candidate.approved_limit
        and envelope.maximum_limit == candidate.approved_limit
        and authorization.account_role is tick.authority.role
        and authorization.valid
        and authorization.book_fingerprint == decision.book_fingerprint
        and authorization.envelope_hash == order_envelope_hash(envelope)
        and authorization.valid_from <= tick.trusted_at < authorization.expires_at
    )


def _valid_lifecycle_proposal(
    tick: AgentTick,
    result: PolicyResult,
    acquisition: LifecycleAcquisition,
    proposal: AuthorizationIntentProposal,
) -> bool:
    authorization = proposal.authorization
    intent = proposal.intent
    envelope = intent.envelope
    expected_action = {
        ExecutionDecision.CLOSE_APPROVED: ExecutionAction.CLOSE,
        ExecutionDecision.CLOSE_RISK_ONLY: ExecutionAction.CLOSE,
        ExecutionDecision.ROLL_APPROVED: ExecutionAction.ROLL,
    }.get(result.execution_decision)
    eligible_alternatives = tuple(
        alternative
        for alternative in result.response.alternatives
        if expected_action is not None
        and alternative.eligible
        and alternative.action.value == expected_action.value
    )
    return bool(
        isinstance(authorization, AssessmentCertificate)
        and authorization.thesis_version_id == acquisition.thesis_version_id
        and expected_action is not None
        and len(eligible_alternatives) == 1
        and intent.state is IntentState.APPROVED
        and intent.digest == intent_digest(envelope)
        and _intent_is_unclaimed(intent)
        and intent.account_role is tick.authority.role
        and envelope.action is expected_action
        and authorization.action is expected_action
        and authorization.assessment_id == result.response.assessment_id
        and authorization.expected_after_exposure == eligible_alternatives[0].expected_exposure
        and envelope.authorization_certificate_id == authorization.certificate_id
        and envelope.account_fingerprint == tick.authority.account_fingerprint
        and envelope.position_or_book_fingerprint == authorization.position_fingerprint
        and envelope.policy_hash == result.response.policy_hash == authorization.policy_hash
        and envelope.quantity == authorization.quantity
        and envelope.approved_max_loss == authorization.approved_max_loss
        and authorization.account_role is tick.authority.role
        and authorization.valid
        and authorization.envelope_hash == order_envelope_hash(envelope)
        and authorization.created_at <= tick.trusted_at < authorization.expires_at
    )


def _intent_is_unclaimed(intent: ExecutionIntent) -> bool:
    return bool(
        intent.claimed_by is None
        and intent.claimed_at is None
        and not intent.first_fill_consumed
        and intent.claim_token is None
        and intent.claim_generation == 0
        and intent.execution_epoch == 0
        and intent.heartbeat_at is None
        and intent.lease_expires_at is None
    )
