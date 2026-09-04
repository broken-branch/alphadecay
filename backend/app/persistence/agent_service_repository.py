from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from backend.app.contracts.v1 import AccountRole, Action, PositionIntent
from backend.app.execution import Actor, ExecutionAction, ExecutionBlocked
from backend.app.lifecycle.structural_pilot import STRUCTURAL_CLOSE_REASONS
from backend.app.policy import (
    AssessmentInput,
    ExecutionDecision,
    OpportunityInput,
    OpportunityOutcome,
)
from backend.app.policy.opportunity import structural_pilot_profile
from backend.app.services.acquisition import (
    AcquisitionKind,
    AuthorizationIntentProposal,
    ObservedPaperAccountAuthority,
    PermanentAccountLatch,
)
from backend.app.services.agent import (
    AgentDecision,
    AgentRunResult,
    AgentTick,
    PersistedAgentDecision,
    SubmissionOrderPreview,
)

from .agent_codec import decode_agent_value, encode_agent_value
from .agent_repository import AgentDecisionRepository


class SQLAlchemyAgentServiceRepository:
    def __init__(
        self,
        repository: AgentDecisionRepository,
        *,
        server_autonomy_enabled: bool,
    ) -> None:
        if repository.server_autonomy_enabled is not server_autonomy_enabled:
            raise ValueError("AGENT_AUTONOMY_CONFIGURATION_MISMATCH")
        self._repository = repository
        self._server_autonomy_enabled = server_autonomy_enabled

    def begin_tick(
        self,
        authority: ObservedPaperAccountAuthority,
        actor: Actor,
        trusted_at: datetime,
    ) -> AgentTick | AgentRunResult:
        boundary = _five_minute_boundary(trusted_at)
        key = f"{authority.role.value}:{actor.value}:{boundary.isoformat()}"
        persisted = self._repository.reserve_tick(
            account_role=authority.role,
            account_fingerprint=authority.account_fingerprint,
            actor=actor.value,
            trusted_at=boundary,
            tick_key=key,
        )
        if persisted.completed:
            return self._completed_result(persisted.tick_id)
        if not persisted.accepted or persisted.reservation_token is None:
            raise ExecutionBlocked("AGENT_TICK_IN_PROGRESS")
        return AgentTick(
            tick_id=persisted.tick_id,
            reservation_token=persisted.reservation_token,
            authority=authority,
            actor=actor,
            trusted_at=trusted_at,
        )

    def permanent_latch(self, authority: ObservedPaperAccountAuthority) -> PermanentAccountLatch:
        persisted = self._repository.get_account_authority(
            authority.role,
            account_fingerprint=authority.account_fingerprint,
        )
        return PermanentAccountLatch(
            persisted.execution_locked,
            persisted.execution_lock_reason if persisted.execution_locked else None,
        )

    def persist_decision(
        self,
        tick: AgentTick,
        decision: AgentDecision,
        proposal: AuthorizationIntentProposal | None,
    ) -> PersistedAgentDecision:
        kind, boundary, observed_at, normalized, policy_hash = _decision_material(decision)
        authorization = proposal.authorization if proposal else None
        intent = proposal.intent if proposal else None
        outcome = "NO_TRADE" if decision.calibration is not None else decision.code
        reason_code = _decision_reason_code(decision)
        normalized_payload = {"typed": encode_agent_value(normalized)}
        manifest_id = None
        if isinstance(normalized, AssessmentInput) and (
            normalized.acquisition_manifest_id is not None
            or normalized.acquisition_manifest_hash is not None
        ):
            if (
                normalized.acquisition_manifest_id is None
                or normalized.acquisition_manifest_hash is None
            ):
                raise ValueError("LIFECYCLE_INPUT_BINDING_REQUIRED")
            manifest_id = normalized.acquisition_manifest_id
            normalized_payload.update(
                acquisition_manifest_id=str(manifest_id),
                acquisition_manifest_hash=normalized.acquisition_manifest_hash,
            )
        if decision.calibration is not None:
            normalized_payload.update(
                machine_binding_hash=decision.calibration.machine_binding_hash,
                calibration_hash=decision.calibration.calibration_hash,
            )
        if decision.submission_authority is not None:
            normalized_payload.update(
                machine_binding_hash=decision.submission_authority.machine_binding_hash,
                calibration_hash=decision.submission_authority.calibration_hash,
            )
        persisted = self._repository.record_decision(
            account_role=tick.authority.role,
            account_fingerprint=tick.authority.account_fingerprint,
            decision_kind=kind,
            decision_boundary=boundary,
            observed_at=observed_at,
            normalized_input=normalized_payload,
            outcome=outcome,
            reason_code=reason_code,
            policy_hash=policy_hash,
            result_payload={"typed": encode_agent_value(decision)},
            experiment_lineage=decision.experiment_lineage,
            thesis_version_id=decision.thesis_version_id,
            authorization=authorization,
            envelope=intent.envelope if intent else None,
            intent_id=intent.intent_id if intent else None,
            lifecycle_manifest_id=manifest_id,
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )
        approved = (
            intent if intent is not None and persisted.intent_id == intent.intent_id else None
        )
        return PersistedAgentDecision(decision=decision, approved_intent=approved)

    def complete_tick(
        self,
        tick: AgentTick,
        terminal_code: str,
        certificate,
    ) -> AgentRunResult:
        persisted_tick = self._repository.get_tick(tick.tick_id)
        if persisted_tick is None or persisted_tick.decision_id is None:
            raise ValueError("AGENT_TICK_DECISION_MISSING")
        completed = self._repository.complete_tick(
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
            terminal_code=terminal_code,
            decision_id=persisted_tick.decision_id,
            execution_certificate_id=certificate.certificate_id if certificate else None,
        )
        return self._completed_result(completed.tick_id)

    def submission_order_preview(self, intent_id: UUID) -> SubmissionOrderPreview:
        stored = self._repository.submission_order_preview(intent_id)
        try:
            decision = decode_agent_value(dict(stored.result_payload["typed"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ExecutionBlocked("SUBMISSION_ORDER_PREVIEW_DECISION_INVALID") from error
        opportunity = decision.opportunity if isinstance(decision, AgentDecision) else None
        lifecycle = decision.lifecycle if isinstance(decision, AgentDecision) else None
        values = decision.normalized_input if isinstance(decision, AgentDecision) else None
        candidate = values.candidate if isinstance(values, OpportunityInput) else None
        candidate_legs = (
            tuple((leg.symbol, leg.intent, leg.ratio) for leg in candidate.legs)
            if candidate is not None
            else ()
        )
        stored_legs = tuple((leg.symbol, leg.intent, leg.ratio) for leg in stored.legs)
        if not isinstance(decision, AgentDecision):
            raise ExecutionBlocked("SUBMISSION_ORDER_PREVIEW_DECISION_INVALID")
        entry_valid = bool(
            stored.action is ExecutionAction.ENTRY
            and opportunity is not None
            and opportunity.outcome is OpportunityOutcome.ENTRY_APPROVED
            and opportunity.strategy is not None
            and opportunity.reason_codes
            and isinstance(values, OpportunityInput)
            and candidate is not None
            and decision.code == OpportunityOutcome.ENTRY_APPROVED.value
            and values.account.account_role is AccountRole.SUBMISSION
            and values.account.book_fingerprint == stored.book_fingerprint
            and opportunity.book_fingerprint == stored.book_fingerprint
            and opportunity.reason_codes[0].value == stored.reason_code
            and opportunity.strategy is candidate.strategy
            and candidate_legs == stored_legs
            and candidate.quantity == stored.quantity
            and candidate.approved_limit == stored.limit_price
            and (
                candidate.maximum_limit is None
                or candidate.maximum_limit >= candidate.approved_limit
            )
            and opportunity.quantity == stored.quantity
            and opportunity.approved_max_loss == stored.maximum_loss
            and opportunity.policy_hash == stored.intent_policy_hash
        )
        lifecycle_valid = bool(
            stored.action is ExecutionAction.CLOSE
            and lifecycle is not None
            and isinstance(values, AssessmentInput)
            and lifecycle.response.action is Action.CLOSE
            and lifecycle.execution_decision
            in {ExecutionDecision.CLOSE_APPROVED, ExecutionDecision.CLOSE_RISK_ONLY}
            and decision.code == lifecycle.execution_decision.value
            and lifecycle.response.rationale_code == stored.reason_code
            and lifecycle.response.policy_hash == stored.intent_policy_hash
            and values.hard_gates.strategy_close_reason == stored.reason_code
            and stored.reason_code in STRUCTURAL_CLOSE_REASONS
            and structural_pilot_profile(stored.thesis_code) is not None
            and len(stored.legs) == 2
            and {leg.intent for leg in stored.legs}
            == {PositionIntent.BUY_TO_CLOSE, PositionIntent.SELL_TO_CLOSE}
            and all(leg.ratio == 1 for leg in stored.legs)
        )
        if (
            decision.thesis_version_id != stored.thesis_version_id
            or stored.maximum_loss > stored.thesis_risk_cap
            or not (entry_valid or lifecycle_valid)
        ):
            raise ExecutionBlocked("SUBMISSION_ORDER_PREVIEW_DECISION_INVALID")
        strategy = opportunity.strategy.value if entry_valid else "CLOSE_VERTICAL"
        reason_codes = (
            tuple(reason.value for reason in opportunity.reason_codes)
            if entry_valid
            else (lifecycle.response.rationale_code,)
        )
        return SubmissionOrderPreview(
            intent_id=stored.intent_id,
            thesis_version_id=stored.thesis_version_id,
            thesis_code=stored.thesis_code,
            strategy=strategy,
            reason_codes=reason_codes,
            risk_cap=stored.thesis_risk_cap,
            legs=stored.legs,
            quantity=stored.quantity,
            limit_price=stored.limit_price,
            maximum_loss=stored.maximum_loss,
            account_role=AccountRole.SUBMISSION,
            decision_id=stored.decision_id,
            approval_id=stored.approval_id,
            account_fingerprint=stored.account_fingerprint,
            book_fingerprint=stored.book_fingerprint,
            policy_hash=stored.intent_policy_hash,
            envelope_hash=stored.envelope_hash,
            decision_result_hash=stored.decision_result_hash,
            intent_digest=stored.intent_digest,
            created_at=stored.created_at,
            experiment_lineage=stored.experiment_lineage,
        )

    def pending_submission_lifecycle_intents(
        self,
        authority: ObservedPaperAccountAuthority,
    ) -> tuple[UUID, ...]:
        if authority.role is not AccountRole.SUBMISSION or authority.paper is not True:
            raise ExecutionBlocked("SUBMISSION_LIFECYCLE_RECOVERY_AUTHORITY_INVALID")
        return self._repository.pending_submission_lifecycle_intents(authority.account_fingerprint)

    def _completed_result(self, tick_id) -> AgentRunResult:
        tick = self._repository.get_tick(tick_id)
        if (
            tick is None
            or not tick.completed
            or tick.decision_id is None
            or tick.proof_hash is None
        ):
            raise ValueError("AGENT_TICK_NOT_COMPLETED")
        persisted = self._repository.get_decision(tick.decision_id)
        if persisted is None:
            raise ValueError("AGENT_DECISION_MISSING")
        decision = decode_agent_value(dict(persisted.result_payload["typed"]))
        if not isinstance(decision, AgentDecision):
            raise ValueError("AGENT_DECISION_CODEC_MISMATCH")
        return AgentRunResult(
            tick_id=tick.tick_id,
            terminal_code=str(tick.terminal_code),
            decision=decision,
            approved_intent_id=persisted.intent_id,
            execution_certificate_id=tick.execution_certificate_id,
            proof_hash=tick.proof_hash,
        )


def _five_minute_boundary(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("TRUSTED_TIME_MUST_BE_UTC")
    utc = value.astimezone(UTC)
    return utc.replace(minute=utc.minute - utc.minute % 5, second=0, microsecond=0)


def _decision_material(decision: AgentDecision):
    if decision.calibration is not None:
        return (
            "OPPORTUNITY",
            decision.calibration.decision_boundary,
            decision.calibration.sealed_at,
            decision.calibration,
            decision.calibration.calibration_hash,
        )
    if decision.opportunity is not None and decision.normalized_input is not None:
        return (
            "OPPORTUNITY",
            decision.opportunity.decision_boundary,
            decision.decided_at,
            decision.normalized_input,
            decision.opportunity.policy_hash,
        )
    if decision.lifecycle is not None and decision.normalized_input is not None:
        return (
            "ASSESSMENT",
            decision.decided_at,
            decision.decided_at,
            decision.normalized_input,
            decision.lifecycle.response.policy_hash,
        )
    failure_kind = decision.provider_failure_kind
    payload = {
        "code": decision.code,
        "provider_failure_code": decision.provider_failure_code,
        "provider_failure_kind": failure_kind.value if failure_kind is not None else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return (
        "OPPORTUNITY" if failure_kind is AcquisitionKind.OPPORTUNITY else "ASSESSMENT",
        decision.decided_at,
        decision.decided_at,
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )


def _decision_reason_code(decision: AgentDecision) -> str:
    if decision.calibration is not None:
        return decision.code
    if decision.opportunity is not None:
        if not decision.opportunity.reason_codes:
            raise ValueError("OPPORTUNITY_REASON_CODE_REQUIRED")
        return decision.opportunity.reason_codes[0].value
    if decision.lifecycle is not None:
        return decision.lifecycle.response.rationale_code
    return decision.code
