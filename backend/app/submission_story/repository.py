from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.competition_archive.models import AssessmentEventProjection
from backend.app.competition_archive.repository import (
    CompetitionRecordNotEligible,
    _assessment_events,
    _spread_from_symbols,
    _submission_baseline,
    _transition_event,
    _validate_decision_hashes,
    _validate_position_authority,
)
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import ExecutionBlocked, intent_digest, order_envelope_hash
from backend.app.execution.order_status import (
    BrokerOrderPhase,
    broker_order_phase,
    broker_state_matches_fill,
)
from backend.app.experiment_lineage import (
    ExperimentExecutionLineage,
    optional_experiment_execution_lineage,
)
from backend.app.persistence.agent_authority import (
    agent_input_material,
    agent_result_material,
    canonical_agent_hash,
)
from backend.app.persistence.agent_codec import decode_agent_value
from backend.app.persistence.agent_repository import _envelope_from_json
from backend.app.persistence.opportunity_evidence import (
    OpportunityEvidenceError,
    SQLAlchemyOpportunityEvidenceRepository,
)
from backend.app.persistence.sqlalchemy_models import (
    AccountReconciliationStateRow,
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AssessmentCertificateRow,
    AttemptObservationRow,
    BrokerMutationPermitRow,
    DevelopmentOpportunityPlanRow,
    EntryApprovalCertificateRow,
    ExecutionCertificateRow,
    ExecutionIntentRow,
    LifecycleObservationBindingRow,
    LifecycleObservationManifestRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
    OrderAttemptRow,
    ThesisVersionRow,
)
from backend.app.persistence.sqlalchemy_repository import _attempt_observation_from_row
from backend.app.policy import OpportunityInput, OpportunityOutcome, evaluate_opportunity
from backend.app.services.agent import AgentDecision

from .models import (
    AccountImpactSummary,
    EntryExecutionSummary,
    LifecycleAssessmentSummary,
    ManagementPolicySummary,
    OrderAttemptSummary,
    OrderLifecycleSummary,
    PriceSummary,
    ProviderRetryAuditSummary,
    PublicSpreadSummary,
    PublicStrategySummary,
    PublicSubmissionStoryPreview,
    RiskLimitSummary,
    SelectedSpreadSummary,
    StrategySummary,
    SubmissionDecisionStory,
    TerminalOutcomeSummary,
)

_LOGGER = logging.getLogger(__name__)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(
    r"(?:\b[0-9a-f]{64}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'])/(?:[^\s\"']+)")
_RISK_DESCRIPTION = "Defined-risk options limits stayed binding throughout this paper decision."
_OPPORTUNITY_AUDIT_OUTCOMES = {
    "OPPORTUNITY_DECISION_PENDING",
    "PROVIDER_FAILURE_NO_TRADE",
}


class SubmissionStoryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SQLAlchemySubmissionStoryRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory
        self._opportunity = SQLAlchemyOpportunityEvidenceRepository(session_factory)

    def latest(self) -> SubmissionDecisionStory:
        try:
            with self._sessions() as session:
                decision = self._latest_decision(session)
                snapshot = session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
                _submission_baseline(session, decision.account_fingerprint)
                account = _submission_account(session, decision.account_fingerprint)
                if _is_calibration_no_trade(decision):
                    plan = self._calibration_plan_row(session, decision, snapshot)
                    strategy = _strategy(plan)
                    _validate_calibration_decision(decision, snapshot)
                    story = _calibration_no_trade_story(strategy, decision)
                    opportunity_values: OpportunityInput | None = None
                else:
                    agent, values = _typed_decision(decision, snapshot)
                    plan = self._plan_row(session, decision, values)
                    strategy = _strategy(plan)
                    _validate_decision_authority(decision, snapshot, agent, values)
                    if values.opportunity_key != plan.opportunity_key:
                        raise SubmissionStoryError("SUBMISSION_STORY_PLAN_MISMATCH")
                    if decision.outcome == OpportunityOutcome.NO_TRADE.value:
                        _require_no_trade_lineage(session, decision)
                        story = _no_trade_story(strategy, decision, agent, values)
                    elif decision.outcome == OpportunityOutcome.ENTRY_APPROVED.value:
                        story = self._entry_story(
                            session,
                            strategy=strategy,
                            decision=decision,
                            snapshot=snapshot,
                            agent=agent,
                            values=values,
                            account=account,
                        )
                    else:
                        raise SubmissionStoryError("SUBMISSION_STORY_OUTCOME_UNSUPPORTED")
                    opportunity_values = values
                plan_id = plan.plan_id
                story = story.model_copy(
                    update={
                        "experiment_execution_lineage": _experiment_lineage(decision),
                        "provider_retry_audit": _provider_retry_audit(session, decision),
                    }
                )

            if opportunity_values is None:
                _validate_plan_baseline_lineage(
                    self._opportunity,
                    plan_id=plan_id,
                    strategy=strategy,
                    decision=decision,
                    expected_policy_hash=plan.policy_hash,
                )
            else:
                _validate_opportunity_lineage(
                    self._opportunity,
                    plan_id=plan_id,
                    strategy=strategy,
                    decision=decision,
                    agent=agent,
                    values=opportunity_values,
                )
            _assert_private_story_safe(story)
            return story
        except SubmissionStoryError:
            raise
        except (
            CompetitionRecordNotEligible,
            ExecutionBlocked,
            OpportunityEvidenceError,
            TypeError,
            ValueError,
        ):
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID") from None

    def _latest_decision(self, session: Session) -> AgentDecisionRow:
        positions = tuple(
            session.scalars(
                select(ManagedLifecyclePositionRow)
                .where(ManagedLifecyclePositionRow.account_role == AccountRole.SUBMISSION.value)
                .order_by(
                    ManagedLifecyclePositionRow.activated_at.desc(),
                    ManagedLifecyclePositionRow.managed_position_id.desc(),
                )
            )
        )
        if positions:
            newest_activation = _utc(positions[0].activated_at)
            if sum(_utc(row.activated_at) == newest_activation for row in positions) != 1:
                raise SubmissionStoryError("SUBMISSION_STORY_AMBIGUOUS")
            approval = session.get(EntryApprovalCertificateRow, positions[0].entry_approval_id)
            decision = (
                None
                if approval is None
                else session.get(AgentDecisionRow, approval.agent_decision_id)
            )
            if (
                decision is None
                or decision.account_role != AccountRole.SUBMISSION.value
                or decision.decision_kind != "OPPORTUNITY"
                or decision.outcome != OpportunityOutcome.ENTRY_APPROVED.value
            ):
                raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
            return decision
        rows = tuple(
            session.scalars(
                select(AgentDecisionRow)
                .where(
                    AgentDecisionRow.account_role == AccountRole.SUBMISSION.value,
                    AgentDecisionRow.decision_kind == "OPPORTUNITY",
                )
                .order_by(
                    AgentDecisionRow.decision_boundary.desc(),
                    AgentDecisionRow.created_at.desc(),
                    AgentDecisionRow.decision_id.desc(),
                )
            )
        )
        policy_rows = tuple(row for row in rows if row.outcome not in _OPPORTUNITY_AUDIT_OUTCOMES)
        if not policy_rows:
            raise SubmissionStoryError("SUBMISSION_STORY_MISSING")
        newest = _utc(policy_rows[0].decision_boundary)
        if sum(_utc(row.decision_boundary) == newest for row in policy_rows) != 1:
            raise SubmissionStoryError("SUBMISSION_STORY_AMBIGUOUS")
        return policy_rows[0]

    def _plan_row(
        self,
        session: Session,
        decision: AgentDecisionRow,
        values: OpportunityInput,
    ) -> DevelopmentOpportunityPlanRow:
        rows = tuple(
            session.scalars(
                select(DevelopmentOpportunityPlanRow).where(
                    DevelopmentOpportunityPlanRow.account_role == AccountRole.SUBMISSION.value,
                    DevelopmentOpportunityPlanRow.opportunity_key == values.opportunity_key,
                    DevelopmentOpportunityPlanRow.policy_hash == decision.policy_hash,
                )
            )
        )
        if len(rows) != 1:
            code = "SUBMISSION_STORY_MISSING" if not rows else "SUBMISSION_STORY_AMBIGUOUS"
            raise SubmissionStoryError(code)
        return rows[0]

    def _calibration_plan_row(
        self,
        session: Session,
        decision: AgentDecisionRow,
        snapshot: AgentInputSnapshotRow | None,
    ) -> DevelopmentOpportunityPlanRow:
        if snapshot is None:
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
        machine_hash = snapshot.normalized_payload.get("machine_binding_hash")
        calibration_hash = snapshot.normalized_payload.get("calibration_hash")
        if (
            not isinstance(machine_hash, str)
            or not isinstance(calibration_hash, str)
            or calibration_hash != decision.policy_hash
        ):
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
        rows = tuple(
            row
            for row in session.scalars(
                select(DevelopmentOpportunityPlanRow).where(
                    DevelopmentOpportunityPlanRow.account_role == AccountRole.SUBMISSION.value
                )
            )
            if _calibration_machine_hash(row.policy_hash, decision, snapshot) == machine_hash
        )
        if len(rows) != 1:
            code = "SUBMISSION_STORY_MISSING" if not rows else "SUBMISSION_STORY_AMBIGUOUS"
            raise SubmissionStoryError(code)
        return rows[0]

    def _entry_story(
        self,
        session: Session,
        *,
        strategy: StrategySummary,
        decision: AgentDecisionRow,
        snapshot: AgentInputSnapshotRow | None,
        agent: AgentDecision,
        values: OpportunityInput,
        account: AccountRoleRow,
    ) -> SubmissionDecisionStory:
        approval = _one_or_none(
            tuple(
                session.scalars(
                    select(EntryApprovalCertificateRow).where(
                        EntryApprovalCertificateRow.agent_decision_id == decision.decision_id
                    )
                )
            ),
            missing="SUBMISSION_STORY_LINEAGE_INCOMPLETE",
        )
        intent = _one_or_none(
            tuple(
                session.scalars(
                    select(ExecutionIntentRow).where(
                        ExecutionIntentRow.entry_approval_id == approval.approval_id
                    )
                )
            ),
            missing="SUBMISSION_STORY_LINEAGE_INCOMPLETE",
        )
        thesis = session.get(ThesisVersionRow, approval.thesis_version_id)
        spread = _validate_entry_authority(
            decision=decision,
            snapshot=snapshot,
            agent=agent,
            values=values,
            approval=approval,
            intent=intent,
            thesis=thesis,
        )
        certificates = tuple(
            session.scalars(
                select(ExecutionCertificateRow).where(
                    ExecutionCertificateRow.execution_intent_id == intent.intent_id
                )
            )
        )
        if len(certificates) > 1:
            raise SubmissionStoryError("SUBMISSION_STORY_AMBIGUOUS")
        positions = tuple(
            session.scalars(
                select(ManagedLifecyclePositionRow).where(
                    ManagedLifecyclePositionRow.entry_approval_id == approval.approval_id
                )
            )
        )
        if len(positions) > 1:
            raise SubmissionStoryError("SUBMISSION_STORY_AMBIGUOUS")
        attempts = tuple(
            session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == intent.intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal)
            )
        )
        _validate_attempts(intent, attempts)
        if not certificates:
            if positions:
                raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
            return _unterminated_entry_story(
                strategy,
                decision,
                agent,
                values,
                spread,
                intent=intent,
                attempts=attempts,
                account=account,
            )

        certificate = certificates[0]
        _validate_entry_certificate(certificate, intent, approval, attempts)
        if certificate.execution_status != "FILLED":
            if positions or intent.state != "TERMINAL":
                raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
            return _terminal_entry_story(
                strategy,
                decision,
                agent,
                values,
                spread,
                certificate=certificate,
                account=account,
                attempts=attempts,
            )
        if len(positions) != 1:
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
        story = _position_story(
            session,
            strategy=strategy,
            decision=decision,
            agent=agent,
            values=values,
            spread=spread,
            approval_id=approval.approval_id,
            intent_id=intent.intent_id,
            entry_certificate=certificate,
            position=positions[0],
            account=account,
            entry_attempts=attempts,
        )
        assert thesis is not None
        return story.model_copy(
            update={
                "management_policy": ManagementPolicySummary(
                    mandatory_close_at=_utc(thesis.target_at)
                )
            }
        )


def _strategy(plan: DevelopmentOpportunityPlanRow) -> StrategySummary:
    return StrategySummary(
        name=plan.opportunity_key,
        version=plan.version,
        underlying=plan.underlying,
        frozen_at=_utc(plan.frozen_at),
    )


def _submission_account(session: Session, fingerprint: str) -> AccountRoleRow:
    rows = tuple(session.scalars(select(AccountRoleRow).where(AccountRoleRow.role == "SUBMISSION")))
    if len(rows) != 1 or rows[0].account_fingerprint != fingerprint:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    return rows[0]


def _experiment_lineage(
    row: (
        AgentDecisionRow
        | AssessmentCertificateRow
        | EntryApprovalCertificateRow
        | ManagedLifecyclePositionRow
    ),
) -> ExperimentExecutionLineage | None:
    return optional_experiment_execution_lineage(
        row.experiment_id,
        row.experiment_source_definition_hash,
        row.experiment_protocol_hash,
    )


def _provider_retry_audit(
    session: Session,
    decision: AgentDecisionRow,
) -> tuple[ProviderRetryAuditSummary, ...]:
    terminal_snapshot = session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
    if terminal_snapshot is None or not isinstance(terminal_snapshot.normalized_payload, dict):
        return ()
    machine_binding_hash = terminal_snapshot.normalized_payload.get("machine_binding_hash")
    calibration_hash = terminal_snapshot.normalized_payload.get("calibration_hash")
    if not (
        isinstance(machine_binding_hash, str)
        and isinstance(calibration_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", machine_binding_hash)
        and re.fullmatch(r"[0-9a-f]{64}", calibration_hash)
    ):
        return ()
    rows = tuple(
        session.scalars(
            select(AgentDecisionRow)
            .where(
                AgentDecisionRow.account_role == AccountRole.SUBMISSION.value,
                AgentDecisionRow.account_fingerprint == decision.account_fingerprint,
                AgentDecisionRow.decision_kind == "OPPORTUNITY",
                AgentDecisionRow.outcome.in_(_OPPORTUNITY_AUDIT_OUTCOMES),
                AgentDecisionRow.created_at <= decision.created_at,
            )
            .order_by(AgentDecisionRow.created_at, AgentDecisionRow.decision_id)
        )
    )
    output: list[ProviderRetryAuditSummary] = []
    for row in rows:
        snapshot = session.get(AgentInputSnapshotRow, row.input_snapshot_id)
        if snapshot is None or not isinstance(snapshot.normalized_payload, dict):
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
        try:
            typed_input = decode_agent_value(dict(snapshot.normalized_payload["typed"]))
            typed_decision = decode_agent_value(dict(row.result_payload["typed"]))
        except (AttributeError, KeyError, TypeError, ValueError):
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID") from None
        if not isinstance(typed_decision, AgentDecision):
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
        authority = typed_decision.submission_authority
        failure_kind = typed_decision.provider_failure_kind
        failures = (
            (row.reason_code != row.outcome),
            (row.autonomy_authorized),
            (row.thesis_version_id is not None),
            (snapshot.account_role != AccountRole.SUBMISSION.value),
            (snapshot.account_fingerprint != decision.account_fingerprint),
            (snapshot.decision_kind != "OPPORTUNITY"),
            (_utc(snapshot.decision_boundary) != _utc(row.decision_boundary)),
            (_utc(snapshot.observed_at) != _utc(row.decision_boundary)),
            (_utc(row.created_at) < _utc(snapshot.observed_at)),
            (
                typed_input
                != {
                    "code": row.outcome,
                    "provider_failure_code": typed_decision.provider_failure_code,
                    "provider_failure_kind": "OPPORTUNITY",
                }
            ),
            (typed_decision.code != row.outcome),
            (_utc(typed_decision.decided_at) != _utc(snapshot.observed_at)),
            (typed_decision.calibration is not None),
            (typed_decision.opportunity is not None),
            (typed_decision.lifecycle is not None),
            (typed_decision.normalized_input is not None),
            (typed_decision.thesis_version_id is not None),
            (typed_decision.experiment_lineage is not None),
            (not typed_decision.provider_failure_code),
            (failure_kind is None),
            (failure_kind.value != "OPPORTUNITY"),
            (
                row.policy_hash
                != hashlib.sha256(
                    json.dumps(typed_input, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            ),
            (authority is None),
            (authority.account_role is not AccountRole.SUBMISSION),
            (authority.account_fingerprint != decision.account_fingerprint),
            # A binding re-sealed later in the session is still valid evidence for its own tick,
            # so each retry row is checked against the binding its snapshot recorded.
            (
                authority.machine_binding_hash
                != snapshot.normalized_payload.get("machine_binding_hash")
            ),
            (authority.calibration_hash != snapshot.normalized_payload.get("calibration_hash")),
            (not _HEX64.fullmatch(str(snapshot.normalized_payload.get("machine_binding_hash")))),
            (not _HEX64.fullmatch(str(snapshot.normalized_payload.get("calibration_hash")))),
            (_utc(snapshot.observed_at) < _utc(authority.sealed_at)),
        )
        failed = [index for index, check in enumerate(failures) if check]
        if failed:
            _LOGGER.warning(
                "submission story provider retry audit invalid: decision %s failed checks %s",
                row.created_at,
                failed,
            )
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
        _validate_decision_hashes(session, row, snapshot)
        output.append(
            ProviderRetryAuditSummary(
                status=row.outcome,
                recorded_at=_utc(row.created_at),
            )
        )
    return tuple(output)


def _validate_attempts(intent: ExecutionIntentRow, attempts: tuple[OrderAttemptRow, ...]) -> None:
    for index, attempt in enumerate(attempts):
        if (
            attempt.execution_intent_id != intent.intent_id
            or attempt.attempt_ordinal != index
            or attempt.quantity != intent.quantity
            or not 0 <= attempt.filled_quantity <= attempt.quantity
            or (index == 0 and attempt.replaces_attempt_id is not None)
            or (index > 0 and attempt.replaces_attempt_id != attempts[index - 1].attempt_id)
            or (index < len(attempts) - 1 and attempt.filled_quantity != 0)
        ):
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
        if attempt.state == "PREPARED":
            if attempt.provider_order_id is not None or attempt.filled_quantity != 0:
                raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
            continue
        try:
            valid_fill = broker_state_matches_fill(
                attempt.state, attempt.filled_quantity, attempt.quantity
            )
        except ValueError:
            if attempt.state != "ASSIGNMENT_LOCKED":
                raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID") from None
            valid_fill = True
        if not valid_fill:
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")


def _order_lifecycle(attempts: Sequence[object], *, missing: str) -> OrderLifecycleSummary:
    summaries = tuple(
        OrderAttemptSummary(
            ordinal=int(attempt.attempt_ordinal),
            state=str(attempt.state),
            filled_quantity=int(attempt.filled_quantity),
            quantity=int(attempt.quantity),
        )
        for attempt in attempts
    )
    return OrderLifecycleSummary(
        recording_status="RECORDED" if summaries else missing,
        attempts=summaries,
    )


def _validate_entry_certificate(
    certificate: ExecutionCertificateRow,
    intent: ExecutionIntentRow,
    approval: EntryApprovalCertificateRow,
    attempts: tuple[OrderAttemptRow, ...],
) -> None:
    if not attempts:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
    terminal = attempts[-1]
    status = certificate.execution_status
    zero_fill_statuses = {"REJECTED", "CANCELED", "EXPIRED", "REPLACED", "UNFILLED"}
    partial_statuses = {
        "PARTIAL_CANCELED_RECONCILED": "CANCELED",
        "PARTIAL_EXPIRED_RECONCILED": "EXPIRED",
        "PARTIAL_REPLACED_RECONCILED": "REPLACED",
    }
    valid_status = (
        (
            status == "FILLED"
            and terminal.state in {"FILLED", "CALCULATED"}
            and terminal.filled_quantity == terminal.quantity
        )
        or (
            status in zero_fill_statuses
            and terminal.state in {"REJECTED", "CANCELED", "EXPIRED", "REPLACED"}
            and terminal.filled_quantity == 0
        )
        or (
            status in partial_statuses
            and terminal.state == partial_statuses[status]
            and 0 < terminal.filled_quantity < terminal.quantity
        )
        or (status == "ASSIGNMENT_LOCKED" and terminal.state == "ASSIGNMENT_LOCKED")
        or (
            status == "RECONCILIATION_MISMATCH"
            and terminal.state
            in {"FILLED", "CALCULATED", "REJECTED", "CANCELED", "EXPIRED", "REPLACED"}
        )
    )
    valid = (
        certificate.execution_intent_id == intent.intent_id
        and certificate.entry_approval_id == approval.approval_id
        and certificate.assessment_certificate_id is None
        and tuple(certificate.attempt_ids) == tuple(attempt.client_order_id for attempt in attempts)
        and intent.state == "TERMINAL"
        and certificate.reconciliation_id is not None
        and certificate.reconciliation_hash is not None
        and certificate.last_observation_hash is not None
        and tuple(certificate.reconciliation_checks)
        == ("TERMINAL", "REMAINDER_ABSENT", "WHOLE_ACCOUNT_RECONCILED")
        and not (status == "FILLED" and certificate.actual_exposure is None)
        and not (status in zero_fill_statuses and certificate.actual_exposure is not None)
        and valid_status
    )
    if not valid:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")


def _entry_execution_summary(
    session: Session,
    *,
    decision: AgentDecisionRow,
    intent_id: UUID,
    certificate: ExecutionCertificateRow,
    position: ManagedLifecyclePositionRow,
    entry_transition: ManagedPositionTransitionRow,
    attempts: tuple[OrderAttemptRow, ...],
) -> EntryExecutionSummary:
    terminal_attempt = attempts[-1]
    permit = (
        None
        if terminal_attempt.broker_permit_id is None
        else session.get(BrokerMutationPermitRow, terminal_attempt.broker_permit_id)
    )
    observations = tuple(
        session.scalars(
            select(AttemptObservationRow)
            .where(AttemptObservationRow.attempt_id == terminal_attempt.attempt_id)
            .order_by(AttemptObservationRow.observation_sequence)
        )
    )
    matching_observations: list[AttemptObservationRow] = []
    for row in observations:
        observed = _attempt_observation_from_row(row)
        observed_attempt = observed.observed_attempt
        if (
            observed_attempt is not None
            and observed_attempt.state == terminal_attempt.state
            and observed_attempt.filled_quantity == terminal_attempt.filled_quantity
            and observed_attempt.quantity == terminal_attempt.quantity
        ):
            matching_observations.append(row)
    reconciliation_rows = tuple(
        session.scalars(
            select(AccountReconciliationStateRow).where(
                AccountReconciliationStateRow.account_role == "SUBMISSION",
                AccountReconciliationStateRow.authority_reconciliation_id
                == certificate.reconciliation_id,
            )
        )
    )
    if (
        permit is None
        or permit.execution_intent_id != intent_id
        or permit.attempt_ordinal != terminal_attempt.attempt_ordinal
        or permit.mutation_kind
        != ("SUBMIT" if terminal_attempt.attempt_ordinal == 0 else "REPLACE")
        or permit.state != "CONSUMED"
        or permit.dispatch_acquired_at is None
        or not matching_observations
        or terminal_attempt.filled_quantity != terminal_attempt.quantity
        or terminal_attempt.state not in {"FILLED", "CALCULATED"}
        or len(reconciliation_rows) != 1
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    fill_observation = matching_observations[0]
    reconciliation = reconciliation_rows[0]
    submitted_at = _utc(permit.dispatch_acquired_at)
    try:
        fill_times = tuple(
            _utc(datetime.fromisoformat(str(item["occurred_at"])))
            for item in entry_transition.fill_activity_manifest
            if item.get("activity_type") == "FILL"
            and item.get("time_quality") == "EXACT_TRANSACTION_TIME"
        )
    except (KeyError, TypeError, ValueError):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID") from None
    filled_at = max(fill_times) if fill_times else None
    reconciled_at = _utc(certificate.created_at)
    if (
        fill_observation.permit_id != permit.permit_id
        or fill_observation.execution_intent_id != intent_id
        or fill_observation.attempt_ordinal != terminal_attempt.attempt_ordinal
        or reconciliation.account_fingerprint != decision.account_fingerprint
        or reconciliation.accepted_at != certificate.created_at
        or position.entry_reconciliation_id != certificate.reconciliation_id
        or entry_transition.action != "ENTRY"
        or filled_at is None
        or len(fill_times) != len(entry_transition.fill_activity_manifest)
        or not submitted_at <= filled_at <= reconciled_at
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    return EntryExecutionSummary(
        submitted_at=submitted_at,
        filled_at=filled_at,
        reconciled_at=reconciled_at,
        reconciliation_sequence=reconciliation.sequence,
    )


def _unterminated_entry_story(
    strategy: StrategySummary,
    decision: AgentDecisionRow,
    agent: AgentDecision,
    values: OpportunityInput,
    spread: SelectedSpreadSummary,
    *,
    intent: ExecutionIntentRow,
    attempts: tuple[OrderAttemptRow, ...],
    account: AccountRoleRow,
) -> SubmissionDecisionStory:
    if intent.state == "TERMINAL":
        if attempts:
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
        # The approved claim expired before any broker write and was resolved by the operator.
        return _nonterminal_story(
            strategy,
            decision,
            agent,
            values,
            spread,
            status="ORDER_ACTIVITY_NOT_RECORDED",
            outcome="APPROVED_UNFILLED",
            impact_status="NO_MUTATION_AUTHORIZED",
            description=(
                "Entry was approved, but execution stopped before any broker write; "
                "the claim expired and was resolved without an order."
            ),
            attempts=attempts,
        )
    if account.recovery_pending:
        return _nonterminal_story(
            strategy,
            decision,
            agent,
            values,
            spread,
            status="RECOVERY_PENDING",
            outcome="RECOVERY",
            impact_status="RECOVERY_PENDING",
            description="Persisted recovery remains pending; broker effect is not recorded.",
            attempts=attempts,
        )
    if account.execution_locked:
        return _nonterminal_story(
            strategy,
            decision,
            agent,
            values,
            spread,
            status="PERMANENTLY_UNSAFE",
            outcome="PERMANENTLY_UNSAFE",
            impact_status="PERMANENTLY_UNSAFE",
            description="A permanent execution latch is recorded; no further broker write is safe.",
            attempts=attempts,
        )
    if not attempts or attempts[-1].state == "PREPARED":
        status = "ORDER_ACTIVITY_NOT_RECORDED"
        description = "An entry was approved, but broker order activity was not recorded."
    else:
        terminal = attempts[-1]
        if 0 < terminal.filled_quantity < terminal.quantity:
            return _nonterminal_story(
                strategy,
                decision,
                agent,
                values,
                spread,
                status="PARTIAL_FILL_UNRECONCILED",
                outcome="PARTIALLY_FILLED",
                impact_status="BROKER_EFFECT_NOT_RECORDED",
                description=(
                    "A partial simulated fill is recorded, but reconciled account impact is not."
                ),
                attempts=attempts,
            )
        phase = broker_order_phase(terminal.state)
        if phase is BrokerOrderPhase.TERMINAL:
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
        status = "ORDER_LOOKUP_ONLY" if phase is BrokerOrderPhase.LOOKUP_ONLY else "ORDER_WORKING"
        description = "A zero-fill simulated paper order remains under bounded observation."
    return _nonterminal_story(
        strategy,
        decision,
        agent,
        values,
        spread,
        status=status,
        outcome="APPROVED_UNFILLED",
        impact_status="BROKER_EFFECT_NOT_RECORDED",
        description=description,
        attempts=attempts,
    )


def _terminal_entry_story(
    strategy: StrategySummary,
    decision: AgentDecisionRow,
    agent: AgentDecision,
    values: OpportunityInput,
    spread: SelectedSpreadSummary,
    *,
    certificate: ExecutionCertificateRow,
    account: AccountRoleRow,
    attempts: tuple[OrderAttemptRow, ...],
) -> SubmissionDecisionStory:
    status = certificate.execution_status
    partial = status.startswith("PARTIAL_")
    unsafe = status in {"ASSIGNMENT_LOCKED", "RECONCILIATION_MISMATCH"} or partial
    if unsafe:
        expected_lock = {
            "ASSIGNMENT_LOCKED": "ASSIGNMENT_SUSPECTED",
            "RECONCILIATION_MISMATCH": "RECONCILIATION_MISMATCH",
        }.get(status, "UNMANAGED_PARTIAL_EXPOSURE")
        if (
            not account.execution_locked
            or account.recovery_pending
            or getattr(account, "execution_lock_reason", expected_lock) != expected_lock
        ):
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
        return _nonterminal_story(
            strategy,
            decision,
            agent,
            values,
            spread,
            status=("PARTIAL_FILL_RECONCILED_UNSAFE" if partial else "PERMANENTLY_UNSAFE"),
            outcome=("PARTIALLY_FILLED" if partial else "PERMANENTLY_UNSAFE"),
            impact_status=("PARTIAL_FILL_RECONCILED_UNSAFE" if partial else "PERMANENTLY_UNSAFE"),
            description=(
                "A terminal partial simulated fill was reconciled and permanently latched unsafe."
                if partial
                else "Terminal reconciliation recorded a permanent unsafe execution latch."
            ),
            attempts=attempts,
            entry_execution_status=status,
            terminal=TerminalOutcomeSummary(
                scope="ENTRY",
                certificate_recording_status="RECORDED",
                certificate_status=status,
                certificate_time=_utc(certificate.created_at),
                outcome_status=("PARTIALLY_FILLED" if partial else "PERMANENTLY_UNSAFE"),
                outcome_time=_utc(certificate.created_at),
            ),
        )
    terminal_status = {
        "REJECTED": "ENTRY_REJECTED",
        "CANCELED": "ENTRY_CANCELED",
        "EXPIRED": "ENTRY_EXPIRED",
        "REPLACED": "ENTRY_REPLACED",
        "UNFILLED": "ENTRY_UNFILLED",
    }.get(status)
    if terminal_status is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    return _nonterminal_story(
        strategy,
        decision,
        agent,
        values,
        spread,
        status=terminal_status,
        outcome="APPROVED_UNFILLED",
        impact_status="TERMINAL_ZERO_FILL_RECONCILED",
        description=f"The simulated paper entry ended {status.lower()} with zero fill.",
        cashflow_status="NOT_APPLICABLE",
        attempts=attempts,
        entry_execution_status=status,
        terminal=TerminalOutcomeSummary(
            scope="ENTRY",
            certificate_recording_status="RECORDED",
            certificate_status=status,
            certificate_time=_utc(certificate.created_at),
            outcome_status="APPROVED_UNFILLED",
            outcome_time=_utc(certificate.created_at),
        ),
    )


def _is_calibration_no_trade(decision: AgentDecisionRow) -> bool:
    return (
        decision.outcome == "NO_TRADE"
        and decision.reason_code == "CALIBRATION_BINDING_NO_TRADE"
        and decision.thesis_version_id is None
        and decision.autonomy_authorized is False
    )


def _calibration_machine_hash(
    policy_hash: str,
    decision: AgentDecisionRow,
    snapshot: AgentInputSnapshotRow,
) -> str:
    material = json.dumps(
        {
            "domain": "alphadecay.calibration-machine-binding.v1",
            "account_role": AccountRole.SUBMISSION.value,
            "account_fingerprint": decision.account_fingerprint,
            "decision_code": "CALIBRATION_BINDING_NO_TRADE",
            "policy_hash": policy_hash,
            "calibration_hash": decision.policy_hash,
            "decision_boundary": _utc(decision.decision_boundary).isoformat(),
            "sealed_at": _utc(snapshot.observed_at).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _validate_calibration_decision(
    decision: AgentDecisionRow,
    snapshot: AgentInputSnapshotRow | None,
) -> None:
    if snapshot is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
    expected_input = canonical_agent_hash(
        agent_input_material(
            account_role=snapshot.account_role,
            account_fingerprint=snapshot.account_fingerprint,
            decision_kind=snapshot.decision_kind,
            decision_boundary=_utc(snapshot.decision_boundary),
            observed_at=_utc(snapshot.observed_at),
            normalized_input=snapshot.normalized_payload,
            thesis_version_id=snapshot.thesis_version_id,
        )
    )
    valid = (
        snapshot.account_role == AccountRole.SUBMISSION.value
        and snapshot.account_fingerprint == decision.account_fingerprint
        and snapshot.decision_kind == decision.decision_kind == "OPPORTUNITY"
        and _utc(snapshot.decision_boundary) == _utc(decision.decision_boundary)
        and snapshot.thesis_version_id is None
        and snapshot.input_hash == expected_input
        and decision.result_payload == {}
    )
    if not valid:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    _validate_result_hash(decision, snapshot.input_hash, None, None, None)


def _typed_decision(
    decision: AgentDecisionRow,
    snapshot: AgentInputSnapshotRow | None,
) -> tuple[AgentDecision, OpportunityInput]:
    if snapshot is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
    try:
        decoded = decode_agent_value(dict(decision.result_payload["typed"]))
    except (KeyError, TypeError, ValueError):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID") from None
    if (
        not isinstance(decoded, AgentDecision)
        or decoded.opportunity is None
        or not isinstance(decoded.normalized_input, OpportunityInput)
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    return decoded, decoded.normalized_input


def _validate_decision_authority(
    decision: AgentDecisionRow,
    snapshot: AgentInputSnapshotRow | None,
    agent: AgentDecision,
    values: OpportunityInput,
) -> None:
    opportunity = agent.opportunity
    if opportunity is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    if snapshot is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
    expected_input = canonical_agent_hash(
        agent_input_material(
            account_role=snapshot.account_role,
            account_fingerprint=snapshot.account_fingerprint,
            decision_kind=snapshot.decision_kind,
            decision_boundary=_utc(snapshot.decision_boundary),
            observed_at=_utc(snapshot.observed_at),
            normalized_input=snapshot.normalized_payload,
            thesis_version_id=snapshot.thesis_version_id,
        )
    )
    valid = (
        decision.account_role == AccountRole.SUBMISSION.value
        and snapshot.account_role == AccountRole.SUBMISSION.value
        and decision.account_fingerprint == snapshot.account_fingerprint
        and decision.decision_kind == snapshot.decision_kind == "OPPORTUNITY"
        and _utc(decision.decision_boundary) == _utc(snapshot.decision_boundary)
        and _utc(opportunity.decision_boundary) == _utc(decision.decision_boundary)
        and opportunity.outcome.value == decision.outcome == agent.code
        and opportunity.reason_codes
        and opportunity.reason_codes[0].value == decision.reason_code
        and opportunity.policy_hash == decision.policy_hash
        and opportunity.opportunity_key == values.opportunity_key
        and agent.thesis_version_id == decision.thesis_version_id
        and agent.experiment_lineage == _experiment_lineage(decision)
        and _utc(agent.decided_at) == _utc(values.evaluated_at)
        and _utc(values.observed_decision_boundary) == _utc(decision.decision_boundary)
        and _utc(snapshot.observed_at) == _utc(values.evaluated_at)
        and _utc(decision.created_at) >= _utc(snapshot.observed_at)
        and values.account.account_role is AccountRole.SUBMISSION
        and snapshot.input_hash == expected_input
    )
    if not valid:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")


def _require_no_trade_lineage(session: Session, decision: AgentDecisionRow) -> None:
    approvals = tuple(
        session.scalars(
            select(EntryApprovalCertificateRow).where(
                EntryApprovalCertificateRow.agent_decision_id == decision.decision_id
            )
        )
    )
    if approvals or decision.autonomy_authorized or decision.thesis_version_id is not None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    snapshot = session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
    if snapshot is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
    _validate_result_hash(decision, snapshot.input_hash, None, None, None)


def _validate_entry_authority(
    *,
    decision: AgentDecisionRow,
    snapshot: AgentInputSnapshotRow | None,
    agent: AgentDecision,
    values: OpportunityInput,
    approval: EntryApprovalCertificateRow,
    intent: ExecutionIntentRow,
    thesis: ThesisVersionRow | None,
) -> SelectedSpreadSummary:
    opportunity = agent.opportunity
    candidate = values.candidate
    if snapshot is None or opportunity is None or candidate is None or thesis is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
    try:
        envelope = _envelope_from_json(intent.envelope_payload)
    except (KeyError, TypeError, ValueError):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID") from None
    typed_legs = tuple((leg.symbol, leg.intent.value, leg.ratio) for leg in candidate.legs)
    stored_legs = tuple(
        (str(leg.get("symbol")), str(leg.get("intent")), int(str(leg.get("ratio"))))
        for leg in intent.legs
    )
    decision_lineage = _experiment_lineage(decision)
    approval_lineage = _experiment_lineage(approval)
    checks = (
        (approval.account_role == intent.account_role == thesis.account_role == "SUBMISSION"),
        (approval.agent_decision_id == decision.decision_id),
        (approval.thesis_version_id == decision.thesis_version_id == thesis.thesis_version_id),
        (snapshot.thesis_version_id == thesis.thesis_version_id),
        (decision.autonomy_authorized),
        (approval.valid),
        (_utc(approval.valid_from) <= _utc(decision.created_at) < _utc(approval.expires_at)),
        (_valid_entry_thesis_chronology(decision, snapshot, thesis)),
        (opportunity.outcome is OpportunityOutcome.ENTRY_APPROVED),
        (opportunity.strategy is candidate.strategy),
        (opportunity.quantity == candidate.quantity == approval.quantity == intent.quantity),
        (opportunity.approved_max_loss == approval.approved_max_loss == intent.approved_max_loss),
        (decision.policy_hash == approval.policy_hash == intent.policy_hash == thesis.policy_hash),
        (approval.envelope_hash == intent.envelope_hash),
        (approval.book_fingerprint == intent.fingerprint == opportunity.book_fingerprint),
        (intent.action == "ENTRY"),
        (intent.entry_approval_id == approval.approval_id),
        (intent.assessment_certificate_id is None),
        (intent.minimum_limit <= candidate.approved_limit <= intent.maximum_limit),
        (typed_legs == stored_legs),
        (envelope.authorization_certificate_id == approval.approval_id),
        (envelope.account_fingerprint == decision.account_fingerprint),
        (envelope.position_or_book_fingerprint == intent.fingerprint),
        (envelope.policy_hash == intent.policy_hash),
        (envelope.event_key == opportunity.opportunity_key),
        (envelope.quantity == intent.quantity),
        (
            envelope.minimum_limit == intent.minimum_limit
            and envelope.maximum_limit == intent.maximum_limit
        ),
        (envelope.approved_max_loss == intent.approved_max_loss),
        (order_envelope_hash(envelope) == intent.envelope_hash),
        (intent_digest(envelope) == intent.intent_digest),
        (approval_lineage == decision_lineage),
    )
    valid = all(checks)
    if not valid:
        _LOGGER.warning(
            "submission story lineage invalid: failed checks %s",
            [index for index, check in enumerate(checks) if not check],
        )
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    _validate_result_hash(
        decision,
        snapshot.input_hash,
        approval.approval_id,
        intent.intent_id,
        intent.intent_digest,
    )
    signed_legs = tuple(
        (
            str(leg["symbol"]),
            intent.quantity if str(leg["intent"]) == "BUY_TO_OPEN" else -intent.quantity,
        )
        for leg in intent.legs
    )
    spread = _spread_from_symbols(signed_legs, intent.quantity, underlying=thesis.underlying)
    order_type = "NET_DEBIT_LIMIT" if intent.minimum_limit > 0 else "NET_CREDIT_LIMIT"
    return SelectedSpreadSummary(
        option_type=spread.option_type,
        expiration=spread.expiration,
        long_strike=spread.long_strike,
        short_strike=spread.short_strike,
        quantity=spread.quantity,
        price=PriceSummary(
            order_type=order_type,
            limit_per_share=abs(intent.minimum_limit),
        ),
    )


def _valid_entry_thesis_chronology(
    decision: AgentDecisionRow,
    snapshot: AgentInputSnapshotRow,
    thesis: ThesisVersionRow,
) -> bool:
    boundary = _utc(decision.decision_boundary)
    frozen_at = _utc(thesis.frozen_at)
    observed_at = _utc(snapshot.observed_at)
    created_at = _utc(decision.created_at)
    target_at = _utc(thesis.target_at)
    return boundary <= frozen_at <= observed_at <= created_at and frozen_at < target_at


def _validate_result_hash(
    decision: AgentDecisionRow,
    input_hash: str,
    approval_id: UUID | None,
    intent_id: UUID | None,
    digest: str | None,
) -> None:
    expected = canonical_agent_hash(
        agent_result_material(
            input_hash=input_hash,
            outcome=decision.outcome,
            reason_code=decision.reason_code,
            policy_hash=decision.policy_hash,
            thesis_version_id=decision.thesis_version_id,
            result_payload=decision.result_payload,
            authorization_id=approval_id,
            intent_id=intent_id,
            intent_digest=digest,
            autonomy_authorized=decision.autonomy_authorized,
            experiment_lineage=optional_experiment_execution_lineage(
                decision.experiment_id,
                decision.experiment_source_definition_hash,
                decision.experiment_protocol_hash,
            ),
        )
    )
    if decision.result_hash != expected or decision.decision_id != uuid5(
        NAMESPACE_URL, f"alphadecay:agent-decision:{expected}"
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")


def _position_story(
    session: Session,
    *,
    strategy: StrategySummary,
    decision: AgentDecisionRow,
    agent: AgentDecision,
    values: OpportunityInput,
    spread: SelectedSpreadSummary,
    approval_id: UUID,
    intent_id: UUID,
    entry_certificate: ExecutionCertificateRow,
    position: ManagedLifecyclePositionRow,
    account: AccountRoleRow,
    entry_attempts: tuple[OrderAttemptRow, ...],
) -> SubmissionDecisionStory:
    thesis = session.get(ThesisVersionRow, position.thesis_version_id)
    transitions = tuple(
        session.scalars(
            select(ManagedPositionTransitionRow)
            .where(ManagedPositionTransitionRow.managed_position_id == position.managed_position_id)
            .order_by(ManagedPositionTransitionRow.transition_sequence)
        )
    )
    snapshots = tuple(
        session.scalars(
            select(ManagedPositionSnapshotRow)
            .where(ManagedPositionSnapshotRow.managed_position_id == position.managed_position_id)
            .order_by(ManagedPositionSnapshotRow.accepted_at)
        )
    )
    if (
        position.account_role != "SUBMISSION"
        or position.account_fingerprint != decision.account_fingerprint
        or position.entry_approval_id != approval_id
        or position.entry_intent_id != intent_id
        or position.entry_execution_certificate_id != entry_certificate.certificate_id
        or _experiment_lineage(position) != _experiment_lineage(decision)
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    _validate_position_authority(position, thesis, transitions, snapshots)
    if thesis is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
    by_transition = {item.transition_id: item for item in snapshots}
    events = tuple(
        _transition_event(session, transition, by_transition, underlying=thesis.underlying)
        for transition in transitions
    )
    assessment_events, _hashes = _assessment_events(session, position, thesis)
    assessments = _lifecycle_assessment_summaries(
        session,
        position=position,
        thesis=thesis,
        projections=assessment_events,
        expected_lineage=_experiment_lineage(position),
    )
    assessments = tuple(
        sorted(
            (
                *assessments,
                *_lifecycle_provider_failure_summaries(
                    session,
                    position=position,
                ),
            ),
            key=lambda item: item.assessed_at,
        )
    )
    _validate_position_timeline(events, assessment_events)
    if events[0].spread_after is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
    opening = events[0].spread_after
    if (
        opening.option_type != spread.option_type
        or opening.expiration != spread.expiration
        or opening.long_strike != spread.long_strike
        or opening.short_strike != spread.short_strike
        or opening.quantity != spread.quantity
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    latest = snapshots[-1]
    closed = position.closed_at is not None
    entry_execution = _entry_execution_summary(
        session,
        decision=decision,
        intent_id=intent_id,
        certificate=entry_certificate,
        position=position,
        entry_transition=transitions[0],
        attempts=entry_attempts,
    )
    if account.recovery_pending:
        return _nonterminal_story(
            strategy,
            decision,
            agent,
            values,
            spread,
            status="RECOVERY_PENDING",
            outcome="RECOVERY",
            impact_status="RECOVERY_PENDING",
            description="Persisted recovery remains pending for the reconciled paper position.",
            reconciled_cashflow=latest.cumulative_cashflow,
            cashflow_status="RECONCILED",
            attempts=entry_attempts,
            entry_execution_status="FILLED",
            entry_execution=entry_execution,
            lifecycle_assessments=assessments,
        )
    if account.execution_locked:
        return _nonterminal_story(
            strategy,
            decision,
            agent,
            values,
            spread,
            status="PERMANENTLY_UNSAFE",
            outcome="PERMANENTLY_UNSAFE",
            impact_status="PERMANENTLY_UNSAFE",
            description="A permanent execution latch is recorded for the paper position.",
            reconciled_cashflow=latest.cumulative_cashflow,
            cashflow_status="RECONCILED",
            attempts=entry_attempts,
            entry_execution_status="FILLED",
            entry_execution=entry_execution,
            lifecycle_assessments=assessments,
        )
    exit_state = (
        None
        if closed
        else _current_exit_state(
            session,
            position=position,
            thesis=thesis,
            transition_intent_ids={item.execution_intent_id for item in transitions},
        )
    )
    if exit_state is not None:
        return _filled_story(
            strategy,
            decision,
            agent,
            values,
            spread,
            closed=False,
            reconciled_cashflow=latest.cumulative_cashflow,
            assessment_present=True,
            rolled=any(event.action == "ROLL" for event in events),
            lifecycle_status=exit_state.status,
            lifecycle_outcome=exit_state.outcome,
            impact_description=exit_state.description,
            attempts=entry_attempts,
            entry_execution=entry_execution,
            lifecycle_assessments=assessments,
            exit_order_lifecycle=exit_state.order_lifecycle,
            terminal=exit_state.terminal,
        )
    if closed:
        exit_order_lifecycle, terminal = _closed_exit_evidence(
            session,
            transition=transitions[-1],
            outcome_time=position.closed_at,
            expected_lineage=_experiment_lineage(position),
        )
    else:
        exit_order_lifecycle = OrderLifecycleSummary(recording_status="NOT_APPLICABLE")
        terminal = TerminalOutcomeSummary(
            scope="ENTRY",
            certificate_recording_status="RECORDED",
            certificate_status="FILLED",
            certificate_time=_utc(entry_certificate.created_at),
            outcome_status="FILLED_OPEN",
            outcome_time=_utc(position.activated_at),
        )
    return _filled_story(
        strategy,
        decision,
        agent,
        values,
        spread,
        closed=closed,
        reconciled_cashflow=latest.cumulative_cashflow,
        assessment_present=bool(assessments),
        rolled=any(event.action == "ROLL" for event in events),
        attempts=entry_attempts,
        entry_execution=entry_execution,
        lifecycle_assessments=assessments,
        exit_order_lifecycle=exit_order_lifecycle,
        terminal=terminal,
    )


def _lifecycle_assessment_summaries(
    session: Session,
    *,
    position: ManagedLifecyclePositionRow,
    thesis: ThesisVersionRow,
    projections: Sequence[AssessmentEventProjection],
    expected_lineage: ExperimentExecutionLineage | None,
) -> tuple[LifecycleAssessmentSummary, ...]:
    rows = tuple(
        session.scalars(
            select(AgentDecisionRow)
            .join(
                AgentInputSnapshotRow,
                AgentInputSnapshotRow.snapshot_id == AgentDecisionRow.input_snapshot_id,
            )
            .join(
                LifecycleObservationBindingRow,
                LifecycleObservationBindingRow.agent_input_snapshot_id
                == AgentInputSnapshotRow.snapshot_id,
            )
            .join(
                LifecycleObservationManifestRow,
                LifecycleObservationManifestRow.manifest_id
                == LifecycleObservationBindingRow.manifest_id,
            )
            .where(
                AgentDecisionRow.account_role == "SUBMISSION",
                AgentDecisionRow.account_fingerprint == position.account_fingerprint,
                AgentDecisionRow.decision_kind == "ASSESSMENT",
                AgentDecisionRow.thesis_version_id == thesis.thesis_version_id,
                AgentInputSnapshotRow.account_role == "SUBMISSION",
                AgentInputSnapshotRow.account_fingerprint == position.account_fingerprint,
                AgentInputSnapshotRow.decision_kind == "ASSESSMENT",
                AgentInputSnapshotRow.thesis_version_id == thesis.thesis_version_id,
                LifecycleObservationManifestRow.managed_position_id == position.managed_position_id,
            )
            .order_by(AgentDecisionRow.decision_boundary, AgentDecisionRow.decision_id)
        )
    )
    if len(rows) != len(projections):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    output: list[LifecycleAssessmentSummary] = []
    for row, projection in zip(rows, projections, strict=True):
        if (
            _utc(row.decision_boundary) != _utc(projection.occurred_at)
            or _experiment_lineage(row) != expected_lineage
        ):
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
        output.append(
            LifecycleAssessmentSummary(
                action=projection.action.value,
                reason_code=row.reason_code,
                assessed_at=_utc(row.decision_boundary),
            )
        )
    return tuple(output)


def _lifecycle_provider_failure_summaries(
    session: Session,
    *,
    position: ManagedLifecyclePositionRow,
) -> tuple[LifecycleAssessmentSummary, ...]:
    query = (
        select(AgentDecisionRow)
        .join(
            AgentInputSnapshotRow,
            AgentInputSnapshotRow.snapshot_id == AgentDecisionRow.input_snapshot_id,
        )
        .where(
            AgentDecisionRow.account_role == "SUBMISSION",
            AgentDecisionRow.account_fingerprint == position.account_fingerprint,
            AgentDecisionRow.decision_kind == "ASSESSMENT",
            AgentDecisionRow.thesis_version_id.is_(None),
            AgentDecisionRow.outcome == "PROVIDER_FAILURE_NO_ACTION",
            AgentDecisionRow.decision_boundary >= position.activated_at,
            AgentInputSnapshotRow.account_role == "SUBMISSION",
            AgentInputSnapshotRow.account_fingerprint == position.account_fingerprint,
            AgentInputSnapshotRow.decision_kind == "ASSESSMENT",
            AgentInputSnapshotRow.thesis_version_id.is_(None),
        )
        .order_by(AgentDecisionRow.decision_boundary, AgentDecisionRow.decision_id)
    )
    if position.closed_at is not None:
        query = query.where(AgentDecisionRow.decision_boundary <= position.closed_at)
    output: list[LifecycleAssessmentSummary] = []
    for row in session.scalars(query):
        snapshot = session.get(AgentInputSnapshotRow, row.input_snapshot_id)
        _validate_decision_hashes(session, row, snapshot)
        if _experiment_lineage(row) is not None or row.autonomy_authorized:
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
        output.append(
            LifecycleAssessmentSummary(
                action="NO_ACTION",
                reason_code=row.reason_code,
                assessed_at=_utc(row.created_at),
            )
        )
    return tuple(output)


@dataclass(frozen=True)
class _ExitState:
    status: str
    outcome: str
    description: str
    order_lifecycle: OrderLifecycleSummary
    terminal: TerminalOutcomeSummary


def _closed_exit_evidence(
    session: Session,
    *,
    transition: ManagedPositionTransitionRow,
    outcome_time: datetime | None,
    expected_lineage: ExperimentExecutionLineage | None,
) -> tuple[OrderLifecycleSummary, TerminalOutcomeSummary]:
    if transition.action != "CLOSE" or outcome_time is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    intent = session.get(ExecutionIntentRow, transition.execution_intent_id)
    certificate = session.get(
        ExecutionCertificateRow,
        transition.execution_certificate_id,
    )
    authorization = (
        None
        if intent is None or intent.assessment_certificate_id is None
        else session.get(AssessmentCertificateRow, intent.assessment_certificate_id)
    )
    attempts = (
        ()
        if intent is None
        else tuple(
            session.scalars(
                select(OrderAttemptRow)
                .where(OrderAttemptRow.execution_intent_id == intent.intent_id)
                .order_by(OrderAttemptRow.attempt_ordinal)
            )
        )
    )
    if intent is not None:
        _validate_attempts(intent, attempts)
    terminal_attempt = attempts[-1] if attempts else None
    if (
        intent is None
        or authorization is None
        or certificate is None
        or terminal_attempt is None
        or intent.action != "CLOSE"
        or intent.state != "TERMINAL"
        or authorization.action != "CLOSE"
        or _experiment_lineage(authorization) != expected_lineage
        or certificate.execution_intent_id != intent.intent_id
        or certificate.entry_approval_id is not None
        or certificate.assessment_certificate_id != authorization.certificate_id
        or certificate.execution_status != "FILLED"
        or tuple(certificate.attempt_ids) != tuple(attempt.client_order_id for attempt in attempts)
        or tuple(certificate.reconciliation_checks)
        != ("TERMINAL", "REMAINDER_ABSENT", "WHOLE_ACCOUNT_RECONCILED")
        or certificate.reconciliation_id is None
        or certificate.reconciliation_hash is None
        or certificate.last_observation_hash is None
        or certificate.actual_exposure is None
        or terminal_attempt.state not in {"FILLED", "CALCULATED"}
        or terminal_attempt.filled_quantity != terminal_attempt.quantity
        or _utc(certificate.created_at) < _utc(transition.occurred_at)
        or _utc(transition.occurred_at) != _utc(outcome_time)
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    return (
        _order_lifecycle(attempts, missing="NOT_RECORDED"),
        TerminalOutcomeSummary(
            scope="EXIT",
            certificate_recording_status="RECORDED",
            certificate_status=certificate.execution_status,
            certificate_time=_utc(certificate.created_at),
            outcome_status="CLOSED",
            outcome_time=_utc(outcome_time),
        ),
    )


def _current_exit_state(
    session: Session,
    *,
    position: ManagedLifecyclePositionRow,
    thesis: ThesisVersionRow,
    transition_intent_ids: set[UUID],
) -> _ExitState | None:
    rows: list[tuple[ExecutionIntentRow, AssessmentCertificateRow]] = []
    for intent in session.scalars(
        select(ExecutionIntentRow).where(
            ExecutionIntentRow.account_role == "SUBMISSION",
            ExecutionIntentRow.assessment_certificate_id.is_not(None),
            ExecutionIntentRow.fingerprint == position.active_position_fingerprint,
        )
    ):
        if intent.intent_id in transition_intent_ids:
            continue
        authorization = session.get(AssessmentCertificateRow, intent.assessment_certificate_id)
        if authorization is None or authorization.thesis_version_id != thesis.thesis_version_id:
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
        rows.append((intent, authorization))
    if not rows:
        return None
    if len(rows) != 1:
        raise SubmissionStoryError("SUBMISSION_STORY_AMBIGUOUS")
    intent, authorization = rows[0]
    try:
        envelope = _envelope_from_json(intent.envelope_payload)
    except (KeyError, TypeError, ValueError):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID") from None
    decision = (
        None
        if authorization.agent_decision_id is None
        else session.get(AgentDecisionRow, authorization.agent_decision_id)
    )
    snapshot = (
        None if decision is None else session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
    )
    valid = (
        decision is not None
        and decision.account_role == "SUBMISSION"
        and decision.account_fingerprint == position.account_fingerprint
        and decision.decision_kind == "ASSESSMENT"
        and decision.thesis_version_id == thesis.thesis_version_id
        and authorization.account_role == "SUBMISSION"
        and authorization.action == intent.action
        and authorization.action in {"CLOSE", "ROLL"}
        and authorization.position_fingerprint == intent.fingerprint
        and authorization.envelope_hash == intent.envelope_hash
        and authorization.approved_max_loss == intent.approved_max_loss
        and authorization.quantity == intent.quantity
        and authorization.policy_hash == intent.policy_hash == thesis.policy_hash
        and _experiment_lineage(authorization) == _experiment_lineage(position)
        and _experiment_lineage(decision) == _experiment_lineage(position)
        and authorization.valid
        and _utc(authorization.created_at) <= _utc(decision.created_at)
        and _utc(decision.created_at) < _utc(authorization.expires_at)
        and intent.entry_approval_id is None
        and intent.assessment_certificate_id == authorization.certificate_id
        and envelope.authorization_certificate_id == authorization.certificate_id
        and envelope.account_fingerprint == position.account_fingerprint
        and envelope.position_or_book_fingerprint == intent.fingerprint
        and envelope.action.value == intent.action
        and order_envelope_hash(envelope) == intent.envelope_hash
        and intent_digest(envelope) == intent.intent_digest
    )
    if not valid:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    _validate_decision_hashes(session, decision, snapshot)
    attempts = tuple(
        session.scalars(
            select(OrderAttemptRow)
            .where(OrderAttemptRow.execution_intent_id == intent.intent_id)
            .order_by(OrderAttemptRow.attempt_ordinal)
        )
    )
    _validate_attempts(intent, attempts)
    certificates = tuple(
        session.scalars(
            select(ExecutionCertificateRow).where(
                ExecutionCertificateRow.execution_intent_id == intent.intent_id
            )
        )
    )
    if len(certificates) > 1:
        raise SubmissionStoryError("SUBMISSION_STORY_AMBIGUOUS")
    if not certificates:
        if intent.state == "TERMINAL":
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
        if not attempts or attempts[-1].state == "PREPARED":
            return _ExitState(
                status="EXIT_ACTIVITY_NOT_RECORDED",
                outcome="FILLED_OPEN",
                description=(
                    "The simulated position is reconciled; broker activity for its approved exit "
                    "is not recorded."
                ),
                order_lifecycle=_order_lifecycle(attempts, missing="NOT_RECORDED"),
                terminal=TerminalOutcomeSummary(
                    scope="EXIT",
                    certificate_recording_status="NOT_RECORDED",
                    outcome_status="FILLED_OPEN",
                    outcome_time=_utc(decision.decision_boundary),
                ),
            )
        if attempts[-1].filled_quantity:
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
        phase = broker_order_phase(attempts[-1].state)
        if phase is BrokerOrderPhase.TERMINAL:
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
        status = "EXIT_LOOKUP_ONLY" if phase is BrokerOrderPhase.LOOKUP_ONLY else "EXIT_WORKING"
        return _ExitState(
            status=status,
            outcome="EXIT_WORKING",
            description=(
                "The simulated position is reconciled while its zero-fill paper exit remains "
                "working."
            ),
            order_lifecycle=_order_lifecycle(attempts, missing="NOT_RECORDED"),
            terminal=TerminalOutcomeSummary(
                scope="EXIT",
                certificate_recording_status="NOT_RECORDED",
                outcome_status="EXIT_WORKING",
                outcome_time=_utc(decision.decision_boundary),
            ),
        )
    certificate = certificates[0]
    if certificate.execution_status == "FILLED":
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
    if not attempts or intent.state != "TERMINAL":
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")
    if (
        certificate.entry_approval_id is not None
        or certificate.assessment_certificate_id != authorization.certificate_id
        or tuple(certificate.attempt_ids) != tuple(attempt.client_order_id for attempt in attempts)
        or certificate.reconciliation_id is None
        or certificate.reconciliation_hash is None
        or certificate.last_observation_hash is None
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    terminal_status = {
        "REJECTED": "EXIT_REJECTED",
        "CANCELED": "EXIT_CANCELED",
        "EXPIRED": "EXIT_EXPIRED",
        "REPLACED": "EXIT_REPLACED",
        "UNFILLED": "EXIT_UNFILLED",
    }.get(certificate.execution_status)
    expected_attempt_states = {
        "REJECTED": {"REJECTED"},
        "CANCELED": {"CANCELED"},
        "EXPIRED": {"EXPIRED"},
        "REPLACED": {"REPLACED"},
        "UNFILLED": {"REJECTED", "CANCELED", "EXPIRED", "REPLACED"},
    }
    if (
        terminal_status is None
        or attempts[-1].filled_quantity != 0
        or attempts[-1].state not in expected_attempt_states[certificate.execution_status]
        or tuple(certificate.reconciliation_checks)
        != ("TERMINAL", "REMAINDER_ABSENT", "WHOLE_ACCOUNT_RECONCILED")
        or certificate.actual_exposure is not None
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    return _ExitState(
        status=terminal_status,
        outcome="FILLED_OPEN",
        description=(
            "The simulated exit ended with zero fill; the reconciled paper position remains open."
        ),
        order_lifecycle=_order_lifecycle(attempts, missing="NOT_RECORDED"),
        terminal=TerminalOutcomeSummary(
            scope="EXIT",
            certificate_recording_status="RECORDED",
            certificate_status=certificate.execution_status,
            certificate_time=_utc(certificate.created_at),
            outcome_status="FILLED_OPEN",
            outcome_time=_utc(certificate.created_at),
        ),
    )


def _filled_story(
    strategy: StrategySummary,
    decision: AgentDecisionRow,
    agent: AgentDecision,
    values: OpportunityInput,
    spread: SelectedSpreadSummary,
    *,
    closed: bool,
    reconciled_cashflow: Decimal,
    assessment_present: bool,
    rolled: bool = False,
    lifecycle_status: str | None = None,
    lifecycle_outcome: str | None = None,
    impact_description: str | None = None,
    attempts: Sequence[object] = (),
    entry_execution: EntryExecutionSummary | None = None,
    lifecycle_assessments: tuple[LifecycleAssessmentSummary, ...] = (),
    exit_order_lifecycle: OrderLifecycleSummary | None = None,
    terminal: TerminalOutcomeSummary | None = None,
) -> SubmissionDecisionStory:
    status = lifecycle_status or ("MANAGED_POSITION_CLOSED" if closed else "FILLED_POSITION_OPEN")
    outcome = lifecycle_outcome or ("CLOSED" if closed else "FILLED_OPEN")
    impact_status = (
        "RECONCILED_SIMULATED_POSITION_CLOSED" if closed else "RECONCILED_SIMULATED_POSITION_OPEN"
    )
    description = impact_description or (
        "Reconciled simulated fills opened and later closed the paper position."
        if closed
        else "Reconciled simulated fills opened the paper position; it remains managed and open."
    )
    what_changed = _position_next_steps(closed, assessment_present, rolled)
    if exit_order_lifecycle is None:
        exit_order_lifecycle = OrderLifecycleSummary(recording_status="NOT_APPLICABLE")
    if terminal is None:
        terminal = TerminalOutcomeSummary(
            scope="EXIT" if closed else "ENTRY",
            certificate_recording_status="NOT_RECORDED",
            outcome_status=outcome,
            outcome_time=_utc(decision.decision_boundary),
        )
    return SubmissionDecisionStory(
        strategy=strategy,
        decision_time=_utc(decision.created_at),
        decision_reason_codes=_decision_reasons(agent),
        why_selected=_why_selected(agent),
        alternatives_rejected=_alternatives(closed=closed),
        evidence_used=_evidence_used(values),
        risk_limits=RiskLimitSummary(
            description=_RISK_DESCRIPTION,
            maximum_loss_usd=agent.opportunity.approved_max_loss if agent.opportunity else None,
            numeric_limit_status="RECORDED",
        ),
        selected_spread=spread,
        entry_order_lifecycle=_order_lifecycle(attempts, missing="NOT_RECORDED"),
        entry_execution=entry_execution,
        lifecycle_assessments=lifecycle_assessments,
        exit_order_lifecycle=exit_order_lifecycle,
        entry_execution_status="FILLED",
        order_lifecycle_status=status,
        account_impact=AccountImpactSummary(
            status=impact_status,
            description=description,
            reconciled_cashflow_usd=reconciled_cashflow,
            cashflow_status="RECONCILED",
            pnl_status="REALIZED_RECONCILED" if closed else "NOT_RECORDED",
            realized_pnl_usd=reconciled_cashflow if closed else None,
            realized_pnl_status="CERTIFIED" if closed else "UNAVAILABLE",
        ),
        outcome=outcome,
        terminal=terminal,
        what_changed_next=what_changed,
    )


def _no_trade_story(
    strategy: StrategySummary,
    decision: AgentDecisionRow,
    agent: AgentDecision,
    values: OpportunityInput,
) -> SubmissionDecisionStory:
    return SubmissionDecisionStory(
        strategy=strategy,
        decision_time=_utc(decision.created_at),
        decision_reason_codes=_decision_reasons(agent),
        why_selected=_why_selected(agent),
        alternatives_rejected=_alternatives(closed=False),
        evidence_used=_evidence_used(values),
        risk_limits=RiskLimitSummary(
            description=_RISK_DESCRIPTION,
            maximum_loss_usd=None,
            numeric_limit_status="NOT_APPLICABLE",
        ),
        selected_spread=None,
        entry_order_lifecycle=OrderLifecycleSummary(recording_status="NOT_APPLICABLE"),
        entry_execution_status="NOT_APPLICABLE",
        order_lifecycle_status="NO_ORDER_AUTHORIZED",
        account_impact=AccountImpactSummary(
            status="NO_MUTATION_AUTHORIZED",
            description="This binding decision authorized no broker mutation.",
            cashflow_status="NOT_APPLICABLE",
        ),
        outcome="NO_TRADE",
        terminal=TerminalOutcomeSummary(
            scope="DECISION",
            certificate_recording_status="NOT_APPLICABLE",
            outcome_status="NO_TRADE",
            outcome_time=_utc(decision.decision_boundary),
        ),
        what_changed_next=(
            "The durable lineage ends at the binding decision; later broker outcome is not "
            "inferred.",
        ),
    )


def _calibration_no_trade_story(
    strategy: StrategySummary,
    decision: AgentDecisionRow,
) -> SubmissionDecisionStory:
    return SubmissionDecisionStory(
        strategy=strategy,
        decision_time=_utc(decision.created_at),
        decision_reason_codes=(decision.reason_code,),
        why_selected=(
            "The sealed machine binding matched this exact frozen SUBMISSION strategy and version.",
            "The persisted calibration result required the agent to stop before market "
            "acquisition.",
        ),
        alternatives_rejected=_alternatives(closed=False),
        evidence_used=(
            "The sealed calibration and policy machine binding.",
            "The frozen SUBMISSION plan and complete clean paper account baseline.",
        ),
        risk_limits=RiskLimitSummary(
            description=_RISK_DESCRIPTION,
            maximum_loss_usd=None,
            numeric_limit_status="NOT_APPLICABLE",
        ),
        selected_spread=None,
        entry_order_lifecycle=OrderLifecycleSummary(recording_status="NOT_APPLICABLE"),
        entry_execution_status="NOT_APPLICABLE",
        order_lifecycle_status="NO_ORDER_AUTHORIZED",
        account_impact=AccountImpactSummary(
            status="NO_MUTATION_AUTHORIZED",
            description="The binding refusal authorized no provider acquisition or paper order.",
            cashflow_status="NOT_APPLICABLE",
        ),
        outcome="NO_TRADE",
        terminal=TerminalOutcomeSummary(
            scope="DECISION",
            certificate_recording_status="NOT_APPLICABLE",
            outcome_status="NO_TRADE",
            outcome_time=_utc(decision.decision_boundary),
        ),
        what_changed_next=(
            "The durable lineage ends at the sealed refusal; later broker outcome is not inferred.",
        ),
    )


def _approved_story(
    strategy: StrategySummary,
    decision: AgentDecisionRow,
    agent: AgentDecision,
    values: OpportunityInput,
    spread: SelectedSpreadSummary,
    status: str,
) -> SubmissionDecisionStory:
    return _nonterminal_story(
        strategy,
        decision,
        agent,
        values,
        spread,
        status=status,
        outcome="APPROVED_UNFILLED",
        impact_status="BROKER_EFFECT_NOT_RECORDED",
        description="Reconciled broker effect is not recorded for this approved entry.",
    )


def _nonterminal_story(
    strategy: StrategySummary,
    decision: AgentDecisionRow,
    agent: AgentDecision,
    values: OpportunityInput,
    spread: SelectedSpreadSummary,
    *,
    status: str,
    outcome: str,
    impact_status: str,
    description: str,
    reconciled_cashflow: Decimal | None = None,
    cashflow_status: str = "NOT_RECORDED",
    attempts: Sequence[object] = (),
    entry_execution_status: str = "NOT_RECORDED",
    entry_execution: EntryExecutionSummary | None = None,
    lifecycle_assessments: tuple[LifecycleAssessmentSummary, ...] = (),
    exit_order_lifecycle: OrderLifecycleSummary | None = None,
    terminal: TerminalOutcomeSummary | None = None,
) -> SubmissionDecisionStory:
    if exit_order_lifecycle is None:
        exit_order_lifecycle = OrderLifecycleSummary(recording_status="NOT_APPLICABLE")
    if terminal is None:
        terminal = TerminalOutcomeSummary(
            scope="ENTRY",
            certificate_recording_status="NOT_RECORDED",
            outcome_status=outcome,
            outcome_time=_utc(decision.decision_boundary),
        )
    return SubmissionDecisionStory(
        strategy=strategy,
        decision_time=_utc(decision.created_at),
        decision_reason_codes=_decision_reasons(agent),
        why_selected=_why_selected(agent),
        alternatives_rejected=_alternatives(closed=False),
        evidence_used=_evidence_used(values),
        risk_limits=RiskLimitSummary(
            description=_RISK_DESCRIPTION,
            maximum_loss_usd=(agent.opportunity.approved_max_loss if agent.opportunity else None),
            numeric_limit_status="RECORDED",
        ),
        selected_spread=spread,
        entry_order_lifecycle=_order_lifecycle(attempts, missing="NOT_RECORDED"),
        entry_execution=entry_execution,
        lifecycle_assessments=lifecycle_assessments,
        exit_order_lifecycle=exit_order_lifecycle,
        entry_execution_status=entry_execution_status,
        order_lifecycle_status=status,
        account_impact=AccountImpactSummary(
            status=impact_status,
            description=description,
            reconciled_cashflow_usd=reconciled_cashflow,
            cashflow_status=cashflow_status,
        ),
        outcome=outcome,
        terminal=terminal,
        what_changed_next=(
            "Only the persisted lifecycle state is reported; no later fill or outcome is inferred.",
        ),
    )


def _why_selected(agent: AgentDecision) -> tuple[str, ...]:
    opportunity = agent.opportunity
    if opportunity is None:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    if opportunity.outcome is OpportunityOutcome.NO_TRADE:
        return (
            "This exact frozen SUBMISSION strategy and version produced the persisted decision.",
            f"The deterministic policy stopped on {opportunity.reason_codes[0].value}.",
        )
    direction = opportunity.direction.value if opportunity.direction is not None else "supported"
    strategy = opportunity.strategy.value if opportunity.strategy is not None else "vertical"
    return (
        "This exact frozen SUBMISSION strategy and version produced the persisted decision.",
        f"The top ranked {strategy} matched the {direction} decision "
        "after every entry gate passed.",
    )


def _decision_reasons(agent: AgentDecision) -> tuple[str, ...]:
    opportunity = agent.opportunity
    if opportunity is None or not opportunity.reason_codes:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    return tuple(reason.value for reason in opportunity.reason_codes)


def _alternatives(*, closed: bool) -> tuple[str, ...]:
    del closed
    return ("Named rejected alternatives were not recorded in the decision authority.",)


def _evidence_used(values: OpportunityInput) -> tuple[str, ...]:
    items = [
        "Completed market session and underlying signal evidence.",
        "Bounded catalyst classifications and market trading status.",
        "Clean SUBMISSION baseline, paper account book, and risk budget authority.",
    ]
    if values.candidate is not None:
        items.append("Indicative option quotes, structure checks, and verified Greek units.")
    return tuple(items)


def _position_next_steps(
    closed: bool,
    assessment_present: bool,
    rolled: bool,
) -> tuple[str, ...]:
    prefix = (
        ("A later managed roll replaced the opening spread under the same frozen thesis.",)
        if rolled
        else ()
    )
    if closed:
        if assessment_present:
            return prefix + (
                "Later persisted lifecycle evidence authorized the managed close.",
                "The terminal fill was reconciled and the simulated paper position was closed.",
            )
        return prefix + (
            "The terminal fill was reconciled and the simulated paper position was closed.",
        )
    if assessment_present:
        return prefix + (
            "Later persisted lifecycle evidence reviewed the open simulated paper position.",
        )
    return prefix + ("The reconciled simulated paper position remained open for lifecycle review.",)


def _validate_position_timeline(
    execution_events: Sequence[object],
    assessment_events: Sequence[object],
) -> None:
    timeline = sorted(
        (*execution_events, *assessment_events),
        key=lambda event: (
            event.occurred_at,
            0 if getattr(event, "event_kind", None) == "EXECUTION" else 1,
        ),
    )
    open_position = False
    entry_seen = False
    for event in timeline:
        if getattr(event, "event_kind", None) == "ASSESSMENT":
            if not open_position:
                raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
            continue
        action = getattr(event, "action", None)
        if action == "ENTRY":
            if entry_seen or open_position:
                raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
            entry_seen = True
            open_position = True
        elif action == "ROLL":
            if not open_position:
                raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
        elif action == "CLOSE":
            if not open_position:
                raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
            open_position = False
        else:
            raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")
    if not entry_seen:
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INCOMPLETE")


def _validate_opportunity_lineage(
    repository: SQLAlchemyOpportunityEvidenceRepository,
    *,
    plan_id: UUID,
    strategy: StrategySummary,
    decision: AgentDecisionRow,
    agent: AgentDecision,
    values: OpportunityInput,
) -> None:
    plan = repository.load_plan(
        strategy.name,
        version=strategy.version,
        account_role=AccountRole.SUBMISSION,
    )
    baseline = repository.load_baseline(plan_id, account_role=AccountRole.SUBMISSION)
    observation = repository.load_observation(plan_id, account_role=AccountRole.SUBMISSION)
    recomputed = None if plan is None else evaluate_opportunity(plan.spec.policy, values)
    if (
        plan is None
        or baseline is None
        or observation is None
        or plan.persisted.plan_id != plan_id
        or plan.spec.underlying != strategy.underlying
        or plan.spec.policy.opportunity_key != strategy.name
        or plan.persisted.policy_hash != decision.policy_hash
        or baseline.seal.account_role is not AccountRole.SUBMISSION
        or observation.spec.account_role is not AccountRole.SUBMISSION
        or baseline.persisted.submission_baseline_id is None
        or baseline.seal.account_fingerprint != decision.account_fingerprint
        or observation.spec.account_fingerprint != decision.account_fingerprint
        or observation.spec.policy_hash != decision.policy_hash
        or _utc(observation.spec.evaluated_at) != _utc(values.evaluated_at)
        or agent.opportunity != recomputed
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")


def _validate_plan_baseline_lineage(
    repository: SQLAlchemyOpportunityEvidenceRepository,
    *,
    plan_id: UUID,
    strategy: StrategySummary,
    decision: AgentDecisionRow,
    expected_policy_hash: str,
) -> None:
    plan = repository.load_plan(
        strategy.name,
        version=strategy.version,
        account_role=AccountRole.SUBMISSION,
    )
    baseline = repository.load_baseline(plan_id, account_role=AccountRole.SUBMISSION)
    if (
        plan is None
        or baseline is None
        or plan.persisted.plan_id != plan_id
        or plan.spec.underlying != strategy.underlying
        or plan.persisted.policy_hash != expected_policy_hash
        or baseline.seal.account_role is not AccountRole.SUBMISSION
        or baseline.persisted.submission_baseline_id is None
        or baseline.seal.account_fingerprint != decision.account_fingerprint
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_LINEAGE_INVALID")


def build_public_preview(story: SubmissionDecisionStory) -> PublicSubmissionStoryPreview:
    summaries = {
        "NO_TRADE": (
            "The paper only agent evaluated the frozen strategy and stopped without selecting "
            "a spread or sending an order."
        ),
        "APPROVED_UNFILLED": (
            "The paper only agent authorized one defined risk options spread. The persisted "
            "lifecycle does not record a reconciled fill."
        ),
        "PARTIALLY_FILLED": (
            "The paper-only lifecycle records a partial simulated fill and does not infer a "
            "complete position or profit and loss."
        ),
        "FILLED_OPEN": (
            "The paper only agent opened and reconciled one simulated defined risk options spread; "
            "the position remains open."
        ),
        "EXIT_WORKING": (
            "The reconciled simulated position remains open while one persisted paper exit is "
            "still working."
        ),
        "CLOSED": (
            "The paper only agent opened, managed, and closed one reconciled simulated options "
            "position."
        ),
        "RECOVERY": (
            "The persisted paper lifecycle is in recovery. No later fill, account effect, or "
            "outcome is inferred."
        ),
        "PERMANENTLY_UNSAFE": (
            "The persisted paper lifecycle is permanently latched unsafe and cannot authorize "
            "another broker write."
        ),
    }
    preview = PublicSubmissionStoryPreview(
        strategy=PublicStrategySummary(),
        decision_time=story.decision_time,
        summary=summaries[story.outcome],
        why_selected=_public_why_selected(story),
        alternatives_rejected=story.alternatives_rejected,
        alternatives_recording=story.alternatives_recording,
        evidence_used=story.evidence_used,
        risk_limits=(
            "A defined risk options limit remained binding; private numeric entry parameters are "
            "omitted."
        ),
        selected_spread=(None if story.selected_spread is None else PublicSpreadSummary()),
        order_lifecycle_status=story.order_lifecycle_status,
        account_impact=story.account_impact.description,
        pnl_status="NOT_RECORDED",
        outcome=story.outcome,
        what_changed_next=story.what_changed_next,
    )
    _assert_public_preview_safe(preview)
    return preview


def _public_why_selected(story: SubmissionDecisionStory) -> tuple[str, ...]:
    if story.outcome == "NO_TRADE":
        return (
            story.why_selected[0],
            "The deterministic policy stopped when one required entry gate did not pass.",
        )
    return story.why_selected


def _assert_private_story_safe(story: SubmissionDecisionStory) -> None:
    payload = story.model_dump(mode="json")
    payload.pop("experiment_execution_lineage", None)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if _IDENTIFIER.search(text) or _ABSOLUTE_PATH.search(text):
        raise SubmissionStoryError("SUBMISSION_STORY_REDACTION_FAILED")


def _assert_public_preview_safe(preview: PublicSubmissionStoryPreview) -> None:
    text = preview.model_dump_json()
    if (
        _IDENTIFIER.search(text)
        or _ABSOLUTE_PATH.search(text)
        or "maximum_loss_usd" in text
        or "limit_per_share" in text
        or "long_strike" in text
        or "short_strike" in text
    ):
        raise SubmissionStoryError("SUBMISSION_STORY_REDACTION_FAILED")


def _one_or_none[T](values: tuple[T, ...], *, missing: str) -> T:
    if not values:
        raise SubmissionStoryError(missing)
    if len(values) != 1:
        raise SubmissionStoryError("SUBMISSION_STORY_AMBIGUOUS")
    return values[0]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
