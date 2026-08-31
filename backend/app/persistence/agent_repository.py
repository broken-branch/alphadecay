from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.contracts.v1 import AccountRole, GreekExposure
from backend.app.execution import ExecutionBlocked, intent_digest, order_envelope_hash
from backend.app.execution.models import (
    AssessmentCertificate,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionIntent,
    FrozenThesisVersion,
    IntentState,
    OrderEnvelope,
)

from .agent_authority import agent_input_material, agent_result_material, canonical_agent_hash
from .authorization import AuthorizationValues, validate_authorization
from .sqlalchemy_models import (
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AgentTickRow,
    AssessmentCertificateRow,
    EntryApprovalCertificateRow,
    ExecutionCertificateRow,
    ExecutionIntentRow,
    LifecycleObservationBindingRow,
    LifecycleObservationManifestRow,
    ManagedLifecyclePositionRow,
    ThesisVersionRow,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")


class TrustedDatabaseClock(Protocol):
    def now(self, session: Session) -> datetime: ...


class SQLAlchemyTrustedDatabaseClock:
    def now(self, session: Session) -> datetime:
        value = session.scalar(select(func.current_timestamp()))
        if not isinstance(value, datetime):
            raise RuntimeError("DATABASE_CLOCK_UNAVAILABLE")
        return _utc(value)


@dataclass(frozen=True)
class PersistedAgentDecision:
    decision_id: UUID
    input_snapshot_id: UUID
    thesis_version_id: UUID | None
    account_role: AccountRole
    account_fingerprint: str
    decision_kind: str
    decision_boundary: datetime
    observed_at: datetime
    outcome: str
    reason_code: str
    policy_hash: str
    normalized_input: dict[str, object]
    result_payload: dict[str, object]
    input_hash: str
    result_hash: str
    autonomy_authorized: bool
    intent_id: UUID | None
    created_at: datetime


@dataclass(frozen=True)
class PersistedAgentTick:
    tick_id: UUID
    account_role: AccountRole
    account_fingerprint: str
    tick_key: str
    tick_boundary: datetime
    actor: str
    accepted: bool
    reservation_token: UUID | None
    completed: bool
    terminal_code: str | None
    decision_id: UUID | None
    execution_certificate_id: UUID | None
    proof_hash: str | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class PersistedAccountAuthority:
    account_role: AccountRole
    account_fingerprint: str
    autonomous_enabled: bool
    execution_locked: bool
    execution_lock_reason: str | None
    execution_lock_id: UUID | None
    execution_lock_generation: int
    execution_epoch: int
    claim_generation: int
    recovery_pending: bool


class AgentDecisionRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        database_clock: TrustedDatabaseClock | None = None,
        server_autonomy_enabled: bool = False,
    ) -> None:
        self._sessions = session_factory
        self._clock = database_clock or SQLAlchemyTrustedDatabaseClock()
        self._server_autonomy_enabled = server_autonomy_enabled

    def add_thesis_version(self, thesis: FrozenThesisVersion) -> None:
        try:
            with self._sessions.begin() as session:
                session.add(
                    ThesisVersionRow(
                        thesis_version_id=thesis.thesis_version_id,
                        thesis_id=thesis.thesis_id,
                        account_role=thesis.account_role.value,
                        version=thesis.version,
                        origin_hash=thesis.origin_hash or thesis.thesis_hash,
                        thesis_hash=thesis.thesis_hash,
                        policy_hash=thesis.policy_hash,
                        underlying=thesis.underlying,
                        thesis_code=thesis.thesis_code,
                        frozen_at=_utc(thesis.frozen_at),
                        target_at=_utc(thesis.target_at),
                        intended_exposure=thesis.intended_exposure,
                        exposure_limits=thesis.exposure_limits,
                        volatility_view=thesis.volatility_view,
                        entry_atm_iv=thesis.entry_atm_iv,
                        approved_max_loss=thesis.approved_max_loss,
                        portfolio_risk_cap=thesis.portfolio_risk_cap,
                        invalidation_codes=list(thesis.invalidation_codes),
                        thesis_payload=thesis.thesis_payload,
                        created_at=_utc(thesis.created_at),
                    )
                )
        except IntegrityError as error:
            raise ExecutionBlocked("THESIS_VERSION_IMMUTABLE") from error

    @property
    def server_autonomy_enabled(self) -> bool:
        return self._server_autonomy_enabled

    def record_submission_calibration_no_trade(
        self,
        *,
        account_fingerprint: str,
        decision_boundary: datetime,
        observed_at: datetime,
        normalized_input: dict[str, object],
        policy_hash: str,
        tick_id: UUID,
        reservation_token: UUID,
    ) -> PersistedAgentDecision:
        return self.record_decision(
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=account_fingerprint,
            decision_kind="OPPORTUNITY",
            decision_boundary=decision_boundary,
            observed_at=observed_at,
            normalized_input=normalized_input,
            outcome="NO_TRADE",
            reason_code="CALIBRATION_BINDING_NO_TRADE",
            policy_hash=policy_hash,
            result_payload={},
            tick_id=tick_id,
            reservation_token=reservation_token,
        )

    def record_decision(
        self,
        *,
        account_role: AccountRole,
        account_fingerprint: str,
        decision_kind: str,
        decision_boundary: datetime,
        observed_at: datetime,
        normalized_input: dict[str, object],
        outcome: str,
        reason_code: str,
        policy_hash: str,
        result_payload: dict[str, object],
        thesis_version_id: UUID | None = None,
        authorization: EntryApprovalAuthorization | AssessmentCertificate | None = None,
        envelope: OrderEnvelope | None = None,
        intent_id: UUID | None = None,
        lifecycle_manifest_id: UUID | None = None,
        tick_id: UUID,
        reservation_token: UUID,
    ) -> PersistedAgentDecision:
        _validate_decision_values(
            account_role=account_role,
            account_fingerprint=account_fingerprint,
            decision_kind=decision_kind,
            decision_boundary=decision_boundary,
            observed_at=observed_at,
            normalized_input=normalized_input,
            outcome=outcome,
            reason_code=reason_code,
            policy_hash=policy_hash,
            result_payload=result_payload,
            thesis_version_id=thesis_version_id,
            authorization=authorization,
            envelope=envelope,
            intent_id=intent_id,
            server_autonomy_enabled=self._server_autonomy_enabled,
        )
        boundary = _utc(decision_boundary)
        observed = _utc(observed_at)
        input_hash = canonical_agent_hash(
            agent_input_material(
                account_role=account_role.value,
                account_fingerprint=account_fingerprint,
                decision_kind=decision_kind,
                decision_boundary=boundary,
                observed_at=observed,
                normalized_input=normalized_input,
                thesis_version_id=thesis_version_id,
            )
        )
        result_hash = canonical_agent_hash(
            agent_result_material(
                input_hash=input_hash,
                outcome=outcome,
                reason_code=reason_code,
                policy_hash=policy_hash,
                thesis_version_id=thesis_version_id,
                result_payload=result_payload,
                authorization_id=_authorization_id(authorization) if authorization else None,
                intent_id=intent_id,
                intent_digest=intent_digest(envelope) if envelope else None,
                autonomy_authorized=authorization is not None,
            )
        )
        snapshot_id = uuid5(NAMESPACE_URL, f"alphadecay:agent-input:{input_hash}")
        decision_id = uuid5(NAMESPACE_URL, f"alphadecay:agent-decision:{result_hash}")
        if lifecycle_manifest_id is not None and (
            decision_kind != "ASSESSMENT"
            or normalized_input.get("acquisition_manifest_id") != str(lifecycle_manifest_id)
            or not _HASH.fullmatch(str(normalized_input.get("acquisition_manifest_hash", "")))
        ):
            raise ExecutionBlocked("LIFECYCLE_INPUT_BINDING_INVALID")

        with self._sessions.begin() as session:
            account = session.get(AccountRoleRow, account_role.value, with_for_update=True)
            if account is None or account.account_fingerprint != account_fingerprint:
                raise ExecutionBlocked("AGENT_DECISION_ACCOUNT_MISMATCH")
            created_at = self._clock.now(session)
            if observed > created_at:
                raise ExecutionBlocked("AGENT_INPUT_FROM_FUTURE")
            if thesis_version_id is not None:
                thesis = session.get(ThesisVersionRow, thesis_version_id)
                if (
                    thesis is None
                    or thesis.account_role != account_role.value
                    or thesis.policy_hash != policy_hash
                ):
                    raise ExecutionBlocked("THESIS_AUTHORITY_MISMATCH")
                frozen_at = _utc(thesis.frozen_at)
                invalid_time = (
                    boundary > frozen_at if decision_kind == "OPPORTUNITY" else frozen_at > boundary
                )
                if invalid_time or frozen_at > observed or frozen_at > created_at:
                    raise ExecutionBlocked("AGENT_DECISION_THESIS_AUTHORITY_INVALID")
            tick = session.get(AgentTickRow, tick_id, with_for_update=True)
            if (
                tick is None
                or tick.reservation_token != reservation_token
                or tick.status != "RESERVED"
                or tick.account_role != account_role.value
                or tick.account_fingerprint != account_fingerprint
            ):
                raise ExecutionBlocked("AGENT_DECISION_TICK_MISMATCH")
            if authorization is not None:
                if tick.actor != "SCHEDULER":
                    raise ExecutionBlocked("AGENT_AUTHORITY_SCHEDULER_REQUIRED")
                if account.execution_locked or account.recovery_pending:
                    raise ExecutionBlocked("AGENT_AUTHORITY_ACCOUNT_LATCHED")
                if not account.autonomous_enabled:
                    raise ExecutionBlocked("AGENT_AUTHORITY_AUTONOMY_DISABLED")
            existing_input = session.scalar(
                select(AgentInputSnapshotRow)
                .where(
                    AgentInputSnapshotRow.account_role == account_role.value,
                    AgentInputSnapshotRow.decision_kind == decision_kind,
                    AgentInputSnapshotRow.decision_boundary == boundary,
                )
                .with_for_update()
            )
            if existing_input is not None:
                if existing_input.input_hash != input_hash:
                    raise ExecutionBlocked("AGENT_INPUT_BOUNDARY_CONFLICT")
                existing = session.scalar(
                    select(AgentDecisionRow).where(
                        AgentDecisionRow.input_snapshot_id == existing_input.snapshot_id
                    )
                )
                if existing is None or existing.result_hash != result_hash:
                    raise ExecutionBlocked("AGENT_DECISION_CONFLICT")
                if tick.decision_id not in {None, existing.decision_id}:
                    raise ExecutionBlocked("AGENT_TICK_DECISION_CONFLICT")
                if lifecycle_manifest_id is not None:
                    _bind_lifecycle_input(
                        session,
                        lifecycle_manifest_id,
                        existing_input,
                        created_at,
                    )
                tick.decision_id = existing.decision_id
                session.flush()
                return self._decision_from_rows(session, existing_input, existing)

            snapshot = AgentInputSnapshotRow(
                snapshot_id=snapshot_id,
                thesis_version_id=thesis_version_id,
                account_role=account_role.value,
                account_fingerprint=account_fingerprint,
                decision_kind=decision_kind,
                decision_boundary=boundary,
                observed_at=observed,
                normalized_payload=normalized_input,
                input_hash=input_hash,
                created_at=created_at,
            )
            decision = AgentDecisionRow(
                decision_id=decision_id,
                thesis_version_id=thesis_version_id,
                origin_tick_id=tick_id,
                input_snapshot_id=snapshot_id,
                account_role=account_role.value,
                account_fingerprint=account_fingerprint,
                decision_kind=decision_kind,
                outcome=outcome,
                reason_code=reason_code,
                policy_hash=policy_hash,
                result_payload=result_payload,
                result_hash=result_hash,
                autonomy_authorized=authorization is not None,
                decision_boundary=boundary,
                created_at=created_at,
            )
            session.add(snapshot)
            session.flush()
            session.add(decision)
            session.flush()
            tick.decision_id = decision_id
            if authorization is not None and envelope is not None and intent_id is not None:
                self._add_authorized_intent(
                    session=session,
                    decision_id=decision_id,
                    account_role=account_role,
                    authorization=authorization,
                    envelope=envelope,
                    intent_id=intent_id,
                    now=created_at,
                )
            if lifecycle_manifest_id is not None:
                _bind_lifecycle_input(
                    session,
                    lifecycle_manifest_id,
                    snapshot,
                    created_at,
                )
            session.flush()
            return self._decision_from_rows(session, snapshot, decision)

    def get_decision(self, decision_id: UUID) -> PersistedAgentDecision | None:
        with self._sessions() as session:
            decision = session.get(AgentDecisionRow, decision_id)
            if decision is None:
                return None
            snapshot = session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
            if snapshot is None:
                raise RuntimeError("AGENT_INPUT_MISSING")
            return self._decision_from_rows(session, snapshot, decision)

    def reserve_tick(
        self,
        *,
        account_role: AccountRole,
        account_fingerprint: str,
        actor: str,
        trusted_at: datetime,
        tick_key: str,
    ) -> PersistedAgentTick:
        boundary = _aware(trusted_at, "TICK_BOUNDARY_INVALID")
        actor_value = getattr(actor, "value", actor)
        if account_role not in {AccountRole.SUBMISSION, AccountRole.DEVELOPMENT}:
            raise ExecutionBlocked("AGENT_TICK_ROLE_INVALID")
        if actor_value not in {"OWNER", "SCHEDULER"} or not 1 <= len(tick_key) <= 128:
            raise ExecutionBlocked("AGENT_TICK_INVALID")
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(AgentTickRow)
                    .where(
                        AgentTickRow.account_role == account_role.value,
                        AgentTickRow.tick_key == tick_key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    return _existing_tick(
                        existing,
                        account_fingerprint=account_fingerprint,
                        actor=str(actor_value),
                        boundary=boundary,
                    )
                account = session.get(AccountRoleRow, account_role.value, with_for_update=True)
                if account is None or account.account_fingerprint != account_fingerprint:
                    raise ExecutionBlocked("AGENT_TICK_ACCOUNT_MISMATCH")
                trusted_now = self._clock.now(session)
                if boundary > trusted_now:
                    raise ExecutionBlocked("AGENT_TICK_FROM_FUTURE")
                tick_id = uuid5(
                    NAMESPACE_URL,
                    f"alphadecay:agent-tick:{account_role.value}:{tick_key}",
                )
                row = AgentTickRow(
                    tick_id=tick_id,
                    account_role=account_role.value,
                    account_fingerprint=account_fingerprint,
                    tick_key=tick_key,
                    tick_boundary=boundary,
                    actor=str(actor_value),
                    status="RESERVED",
                    reservation_token=uuid4(),
                    terminal_code=None,
                    decision_id=None,
                    execution_certificate_id=None,
                    proof_hash=None,
                    created_at=trusted_now,
                    completed_at=None,
                )
                session.add(row)
                session.flush()
                return _tick_from_row(row, accepted=True, include_reservation=True)
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(AgentTickRow).where(
                        AgentTickRow.account_role == account_role.value,
                        AgentTickRow.tick_key == tick_key,
                    )
                )
                if existing is None:
                    raise
                return _existing_tick(
                    existing,
                    account_fingerprint=account_fingerprint,
                    actor=str(actor_value),
                    boundary=boundary,
                )

    def complete_tick(
        self,
        *,
        tick_id: UUID,
        reservation_token: UUID,
        terminal_code: str,
        decision_id: UUID | None,
        execution_certificate_id: UUID | None,
    ) -> PersistedAgentTick:
        if not terminal_code:
            raise ExecutionBlocked("AGENT_TICK_COMPLETION_INVALID")
        with self._sessions.begin() as session:
            row = session.get(AgentTickRow, tick_id, with_for_update=True)
            if row is None or row.reservation_token != reservation_token:
                raise ExecutionBlocked("AGENT_TICK_RESERVATION_MISMATCH")
            if row.decision_id is None or row.decision_id != decision_id:
                raise ExecutionBlocked("AGENT_TICK_DECISION_MISMATCH")
            decision_result_hash = None
            if decision_id is not None:
                decision = session.get(AgentDecisionRow, decision_id)
                if decision is None or decision.account_role != row.account_role:
                    raise ExecutionBlocked("AGENT_TICK_DECISION_MISMATCH")
                decision_result_hash = decision.result_hash
            if execution_certificate_id is not None and (
                decision_id is None
                or not _certificate_matches_decision(
                    session,
                    execution_certificate_id,
                    decision_id,
                    terminal_code,
                )
            ):
                raise ExecutionBlocked("AGENT_TICK_CERTIFICATE_MISMATCH")
            if execution_certificate_id is None and terminal_code in {
                "FILLED",
                "REJECTED",
                "CANCELED",
                "EXPIRED",
                "REPLACED",
                "PARTIAL_CANCELED_RECONCILED",
                "PARTIAL_EXPIRED_RECONCILED",
                "PARTIAL_REPLACED_RECONCILED",
                "ENTRY_FILLED_MATERIALIZATION_FAILED",
                "LIFECYCLE_FILLED_MATERIALIZATION_FAILED",
            }:
                raise ExecutionBlocked("AGENT_TICK_CERTIFICATE_REQUIRED")
            linked_intent_id = _decision_intent_id(session, decision_id)
            if (
                execution_certificate_id is None
                and linked_intent_id is not None
                and terminal_code
                not in {
                    "EXECUTION_BLOCKED",
                    "APPROVED_INTENT_MISMATCH",
                    "ENTRY_APPROVED_WITHOUT_INTENT",
                    "ACTION_APPROVED_WITHOUT_INTENT",
                    "ENTRY_MATERIALIZATION_PREPARATION_FAILED",
                }
            ):
                raise ExecutionBlocked("AGENT_TICK_CERTIFICATE_REQUIRED")
            if execution_certificate_id is not None and linked_intent_id is None:
                raise ExecutionBlocked("AGENT_TICK_CERTIFICATE_UNAUTHORIZED")
            proof_hash = _tick_proof_hash(
                tick_id=tick_id,
                account_role=AccountRole(row.account_role),
                account_fingerprint=row.account_fingerprint,
                tick_key=row.tick_key,
                tick_boundary=_utc(row.tick_boundary),
                actor=row.actor,
                terminal_code=terminal_code,
                decision_id=decision_id,
                decision_result_hash=decision_result_hash,
                execution_certificate_id=execution_certificate_id,
            )
            if row.status == "COMPLETED":
                if (
                    row.terminal_code != terminal_code
                    or row.decision_id != decision_id
                    or row.execution_certificate_id != execution_certificate_id
                    or row.proof_hash != proof_hash
                ):
                    raise ExecutionBlocked("AGENT_TICK_COMPLETION_CONFLICT")
                return _tick_from_row(row, accepted=False, include_reservation=False)
            row.status = "COMPLETED"
            row.terminal_code = terminal_code
            row.decision_id = decision_id
            row.execution_certificate_id = execution_certificate_id
            row.proof_hash = proof_hash
            row.completed_at = self._clock.now(session)
            session.flush()
            return _tick_from_row(row, accepted=False, include_reservation=False)

    def get_account_authority(
        self,
        account_role: AccountRole,
        *,
        account_fingerprint: str | None = None,
    ) -> PersistedAccountAuthority:
        if account_role not in {AccountRole.SUBMISSION, AccountRole.DEVELOPMENT}:
            raise ExecutionBlocked("AGENT_ACCOUNT_ROLE_INVALID")
        with self._sessions() as session:
            row = session.get(AccountRoleRow, account_role.value)
            if row is None or (
                account_fingerprint is not None and row.account_fingerprint != account_fingerprint
            ):
                raise ExecutionBlocked("AGENT_ACCOUNT_MISMATCH")
            return PersistedAccountAuthority(
                account_role=account_role,
                account_fingerprint=row.account_fingerprint,
                autonomous_enabled=row.autonomous_enabled,
                execution_locked=row.execution_locked,
                execution_lock_reason=row.execution_lock_reason,
                execution_lock_id=row.execution_lock_id,
                execution_lock_generation=row.execution_lock_generation,
                execution_epoch=row.execution_epoch,
                claim_generation=row.claim_generation,
                recovery_pending=row.recovery_pending,
            )

    def get_tick(self, tick_id: UUID) -> PersistedAgentTick | None:
        with self._sessions() as session:
            row = session.get(AgentTickRow, tick_id)
            return (
                None
                if row is None
                else _tick_from_row(row, accepted=False, include_reservation=False)
            )

    def _add_authorized_intent(
        self,
        *,
        session: Session,
        decision_id: UUID,
        account_role: AccountRole,
        authorization: EntryApprovalAuthorization | AssessmentCertificate,
        envelope: OrderEnvelope,
        intent_id: UUID,
        now: datetime,
    ) -> None:
        decision = session.get(AgentDecisionRow, decision_id)
        snapshot = (
            session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
            if decision is not None
            else None
        )
        thesis = session.get(ThesisVersionRow, authorization.thesis_version_id)
        if (
            decision is None
            or snapshot is None
            or thesis is None
            or decision.thesis_version_id != authorization.thesis_version_id
            or snapshot.thesis_version_id != authorization.thesis_version_id
            or decision.account_role != account_role.value
            or snapshot.account_role != account_role.value
            or decision.account_fingerprint != envelope.account_fingerprint
            or snapshot.account_fingerprint != envelope.account_fingerprint
            or thesis.account_role != account_role.value
            or decision.policy_hash != envelope.policy_hash
            or thesis.policy_hash != envelope.policy_hash
        ):
            raise ExecutionBlocked("THESIS_AUTHORITY_MISMATCH")
        digest = intent_digest(envelope)
        intent = ExecutionIntent(
            intent_id=intent_id,
            account_role=account_role,
            envelope=envelope,
            digest=digest,
            state=IntentState.APPROVED,
        )
        if isinstance(authorization, EntryApprovalAuthorization):
            values = AuthorizationValues(
                account_role=authorization.account_role,
                policy_hash=authorization.policy_hash,
                fingerprint=authorization.book_fingerprint,
                envelope_hash=authorization.envelope_hash,
                approved_max_loss=authorization.approved_max_loss,
                quantity=authorization.quantity,
                valid=authorization.valid,
                valid_from=_utc(authorization.valid_from),
                expires_at=_utc(authorization.expires_at),
            )
            entry_id: UUID | None = authorization.approval_id
            assessment_id: UUID | None = None
            session.add(
                EntryApprovalCertificateRow(
                    approval_id=authorization.approval_id,
                    thesis_version_id=authorization.thesis_version_id,
                    agent_decision_id=decision_id,
                    account_role=authorization.account_role.value,
                    policy_hash=authorization.policy_hash,
                    book_fingerprint=authorization.book_fingerprint,
                    envelope_hash=authorization.envelope_hash,
                    approved_max_loss=authorization.approved_max_loss,
                    quantity=authorization.quantity,
                    valid_from=_utc(authorization.valid_from),
                    expires_at=_utc(authorization.expires_at),
                    valid=authorization.valid,
                )
            )
        else:
            managed_position = session.scalar(
                select(ManagedLifecyclePositionRow)
                .where(
                    ManagedLifecyclePositionRow.account_role == account_role.value,
                    ManagedLifecyclePositionRow.closed_at.is_(None),
                )
                .with_for_update()
            )
            if (
                managed_position is None
                or managed_position.account_fingerprint != envelope.account_fingerprint
                or managed_position.thesis_version_id != authorization.thesis_version_id
                or managed_position.active_position_fingerprint
                != authorization.position_fingerprint
            ):
                raise ExecutionBlocked("LIFECYCLE_POSITION_AUTHORITY_MISMATCH")
            if envelope.action is ExecutionAction.ROLL:
                prior_roll = session.scalar(
                    select(ExecutionIntentRow.intent_id).where(
                        ExecutionIntentRow.account_role == account_role.value,
                        ExecutionIntentRow.action == ExecutionAction.ROLL.value,
                        ExecutionIntentRow.fingerprint == envelope.position_or_book_fingerprint,
                        ExecutionIntentRow.market_session_id == envelope.market_session_id,
                    )
                )
                if prior_roll is not None:
                    raise ExecutionBlocked("ROLL_SESSION_ALREADY_USED")
            values = AuthorizationValues(
                account_role=authorization.account_role,
                policy_hash=authorization.policy_hash,
                fingerprint=authorization.position_fingerprint,
                envelope_hash=authorization.envelope_hash,
                approved_max_loss=authorization.approved_max_loss,
                quantity=authorization.quantity,
                valid=authorization.valid,
                valid_from=_utc(authorization.created_at),
                expires_at=_utc(authorization.expires_at),
            )
            entry_id = None
            assessment_id = authorization.certificate_id
            session.add(
                AssessmentCertificateRow(
                    certificate_id=authorization.certificate_id,
                    thesis_version_id=authorization.thesis_version_id,
                    agent_decision_id=decision_id,
                    assessment_id=authorization.assessment_id,
                    account_role=authorization.account_role.value,
                    action=authorization.action.value,
                    position_fingerprint=authorization.position_fingerprint,
                    envelope_hash=authorization.envelope_hash,
                    approved_max_loss=authorization.approved_max_loss,
                    quantity=authorization.quantity,
                    expected_after_exposure=_exposure_to_json(
                        authorization.expected_after_exposure
                    ),
                    policy_hash=authorization.policy_hash,
                    created_at=_utc(authorization.created_at),
                    expires_at=_utc(authorization.expires_at),
                    valid=authorization.valid,
                )
            )
        validate_authorization(intent, values, now)
        session.add(
            ExecutionIntentRow(
                intent_id=intent_id,
                account_role=account_role.value,
                intent_digest=digest,
                action=envelope.action.value,
                policy_hash=envelope.policy_hash,
                event_key=envelope.event_key,
                trading_day=envelope.trading_day,
                entry_approval_id=entry_id,
                assessment_certificate_id=assessment_id,
                fingerprint=envelope.position_or_book_fingerprint,
                envelope_hash=order_envelope_hash(envelope),
                envelope_payload=_envelope_to_json(envelope),
                legs=_legs_to_json(envelope),
                quantity=envelope.quantity,
                minimum_limit=envelope.minimum_limit,
                maximum_limit=envelope.maximum_limit,
                approved_max_loss=envelope.approved_max_loss,
                market_session_id=envelope.market_session_id,
                quoted_relative_spread=envelope.quoted_relative_spread,
                maximum_relative_spread=envelope.maximum_relative_spread,
                incremental_debit=envelope.incremental_debit,
                maximum_incremental_debit=envelope.maximum_incremental_debit,
                state=IntentState.APPROVED.value,
                claimed_by=None,
                claimed_at=None,
                claim_token=None,
                claim_generation=0,
                execution_epoch=0,
                heartbeat_at=None,
                lease_expires_at=None,
                first_fill_consumed=False,
            )
        )

    def _decision_from_rows(
        self, session: Session, snapshot: AgentInputSnapshotRow, decision: AgentDecisionRow
    ) -> PersistedAgentDecision:
        if snapshot.thesis_version_id != decision.thesis_version_id:
            raise RuntimeError("AGENT_DECISION_THESIS_MISMATCH")
        entry_id = session.scalar(
            select(EntryApprovalCertificateRow.approval_id).where(
                EntryApprovalCertificateRow.agent_decision_id == decision.decision_id
            )
        )
        assessment_id = session.scalar(
            select(AssessmentCertificateRow.certificate_id).where(
                AssessmentCertificateRow.agent_decision_id == decision.decision_id
            )
        )
        intent_id = None
        if entry_id is not None:
            intent_id = session.scalar(
                select(ExecutionIntentRow.intent_id).where(
                    ExecutionIntentRow.entry_approval_id == entry_id
                )
            )
        elif assessment_id is not None:
            intent_id = session.scalar(
                select(ExecutionIntentRow.intent_id).where(
                    ExecutionIntentRow.assessment_certificate_id == assessment_id
                )
            )
        return PersistedAgentDecision(
            decision_id=decision.decision_id,
            input_snapshot_id=snapshot.snapshot_id,
            thesis_version_id=snapshot.thesis_version_id,
            account_role=AccountRole(decision.account_role),
            account_fingerprint=decision.account_fingerprint,
            decision_kind=decision.decision_kind,
            decision_boundary=_utc(decision.decision_boundary),
            observed_at=_utc(snapshot.observed_at),
            outcome=decision.outcome,
            reason_code=decision.reason_code,
            policy_hash=decision.policy_hash,
            normalized_input=dict(snapshot.normalized_payload),
            result_payload=dict(decision.result_payload),
            input_hash=snapshot.input_hash,
            result_hash=decision.result_hash,
            autonomy_authorized=decision.autonomy_authorized,
            intent_id=intent_id,
            created_at=_utc(decision.created_at),
        )


def _validate_decision_values(
    *,
    account_role: AccountRole,
    account_fingerprint: str,
    decision_kind: str,
    decision_boundary: datetime,
    observed_at: datetime,
    normalized_input: dict[str, object],
    outcome: str,
    reason_code: str,
    policy_hash: str,
    result_payload: dict[str, object],
    thesis_version_id: UUID | None,
    authorization: EntryApprovalAuthorization | AssessmentCertificate | None,
    envelope: OrderEnvelope | None,
    intent_id: UUID | None,
    server_autonomy_enabled: bool,
) -> None:
    if account_role not in {AccountRole.SUBMISSION, AccountRole.DEVELOPMENT}:
        raise ExecutionBlocked("AGENT_DECISION_ROLE_INVALID")
    if not _HASH.fullmatch(account_fingerprint) or not _HASH.fullmatch(policy_hash):
        raise ExecutionBlocked("AGENT_DECISION_HASH_INVALID")
    if decision_kind not in {"OPPORTUNITY", "ASSESSMENT"}:
        raise ExecutionBlocked("AGENT_DECISION_KIND_INVALID")
    boundary = _aware(decision_boundary, "DECISION_BOUNDARY_INVALID")
    observed = _aware(observed_at, "OBSERVED_AT_INVALID")
    if observed < boundary:
        raise ExecutionBlocked("AGENT_INPUT_BEFORE_BOUNDARY")
    if not outcome or not reason_code:
        raise ExecutionBlocked("AGENT_DECISION_RESULT_INVALID")
    canonical_agent_hash(normalized_input)
    canonical_agent_hash(result_payload)
    supplied = (authorization is not None, envelope is not None, intent_id is not None)
    if any(supplied) and not all(supplied):
        raise ExecutionBlocked("AGENT_AUTHORITY_INCOMPLETE")
    if authorization is not None and not server_autonomy_enabled:
        raise ExecutionBlocked("AGENT_AUTHORITY_SERVER_GATE_DISABLED")
    if account_role is AccountRole.SUBMISSION and decision_kind == "OPPORTUNITY":
        if outcome != "NO_TRADE" or reason_code != "CALIBRATION_BINDING_NO_TRADE" or any(supplied):
            raise ExecutionBlocked("SUBMISSION_CALIBRATION_NO_TRADE_REQUIRED")
        if not _HASH.fullmatch(str(normalized_input.get("machine_binding_hash", ""))) or not (
            _HASH.fullmatch(str(normalized_input.get("calibration_hash", "")))
        ):
            raise ExecutionBlocked("SUBMISSION_CALIBRATION_BINDING_INVALID")
    if authorization is None or envelope is None:
        return
    if thesis_version_id is None:
        raise ExecutionBlocked("AGENT_THESIS_AUTHORITY_REQUIRED")
    if authorization.thesis_version_id != thesis_version_id:
        raise ExecutionBlocked("AUTHORIZATION_THESIS_MISMATCH")
    if authorization.account_role is not account_role:
        raise ExecutionBlocked("AUTHORIZATION_ACCOUNT_ROLE_MISMATCH")
    if envelope.account_fingerprint != account_fingerprint:
        raise ExecutionBlocked("AUTHORIZATION_ACCOUNT_MISMATCH")
    if isinstance(authorization, EntryApprovalAuthorization):
        if (
            decision_kind != "OPPORTUNITY"
            or outcome != "ENTRY_APPROVED"
            or envelope.action is not ExecutionAction.ENTRY
        ):
            raise ExecutionBlocked("ENTRY_AUTHORIZATION_KIND_MISMATCH")
        if account_role is not AccountRole.DEVELOPMENT:
            raise ExecutionBlocked("SUBMISSION_ENTRY_DISABLED")
        if envelope.authorization_certificate_id != authorization.approval_id:
            raise ExecutionBlocked("AUTHORIZATION_ID_MISMATCH")
    else:
        expected_outcomes = {
            ExecutionAction.CLOSE: {"CLOSE_APPROVED", "CLOSE_RISK_ONLY"},
            ExecutionAction.ROLL: {"ROLL_APPROVED"},
        }
        if (
            decision_kind != "ASSESSMENT"
            or envelope.action is not authorization.action
            or outcome not in expected_outcomes.get(envelope.action, set())
        ):
            raise ExecutionBlocked("ASSESSMENT_AUTHORIZATION_KIND_MISMATCH")
        if envelope.authorization_certificate_id != authorization.certificate_id:
            raise ExecutionBlocked("AUTHORIZATION_ID_MISMATCH")


def _authorization_id(
    authorization: EntryApprovalAuthorization | AssessmentCertificate,
) -> UUID:
    if isinstance(authorization, EntryApprovalAuthorization):
        return authorization.approval_id
    return authorization.certificate_id


def _bind_lifecycle_input(
    session: Session,
    manifest_id: UUID,
    input_snapshot: AgentInputSnapshotRow,
    created_at: datetime,
) -> None:
    manifest = session.get(LifecycleObservationManifestRow, manifest_id)
    position = (
        session.get(ManagedLifecyclePositionRow, manifest.managed_position_id)
        if manifest is not None
        else None
    )
    existing = session.scalar(
        select(LifecycleObservationBindingRow).where(
            (LifecycleObservationBindingRow.manifest_id == manifest_id)
            | (LifecycleObservationBindingRow.agent_input_snapshot_id == input_snapshot.snapshot_id)
        )
    )
    if existing is not None:
        if (
            existing.manifest_id == manifest_id
            and existing.agent_input_snapshot_id == input_snapshot.snapshot_id
        ):
            return
        raise ExecutionBlocked("LIFECYCLE_INPUT_BINDING_CONFLICT")
    if (
        manifest is None
        or position is None
        or manifest.agent_input_snapshot_id is not None
        or manifest.reconciliation_id is not None
        or input_snapshot.decision_kind != "ASSESSMENT"
        or input_snapshot.account_role != AccountRole.DEVELOPMENT.value
        or input_snapshot.account_role != position.account_role
        or input_snapshot.account_fingerprint != position.account_fingerprint
        or input_snapshot.thesis_version_id != position.thesis_version_id
        or input_snapshot.normalized_payload.get("acquisition_manifest_id") != str(manifest_id)
        or input_snapshot.normalized_payload.get("acquisition_manifest_hash")
        != manifest.manifest_hash
    ):
        raise ExecutionBlocked("LIFECYCLE_INPUT_BINDING_INVALID")
    session.add(
        LifecycleObservationBindingRow(
            binding_id=uuid5(
                NAMESPACE_URL,
                f"alphadecay:lifecycle-binding:{manifest_id}:{input_snapshot.snapshot_id}",
            ),
            manifest_id=manifest_id,
            agent_input_snapshot_id=input_snapshot.snapshot_id,
            created_at=_utc(created_at),
        )
    )


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionBlocked(code)
    return value.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _legs_to_json(envelope: OrderEnvelope) -> list[dict[str, object]]:
    return [
        {"symbol": leg.symbol, "intent": leg.intent.value, "ratio": leg.ratio}
        for leg in envelope.legs
    ]


def _envelope_to_json(envelope: OrderEnvelope) -> dict[str, object]:
    return {
        "action": envelope.action.value,
        "authorization_certificate_id": str(envelope.authorization_certificate_id),
        "policy_hash": envelope.policy_hash,
        "account_fingerprint": envelope.account_fingerprint,
        "position_or_book_fingerprint": envelope.position_or_book_fingerprint,
        "legs": _legs_to_json(envelope),
        "quantity": envelope.quantity,
        "minimum_limit": str(envelope.minimum_limit),
        "maximum_limit": str(envelope.maximum_limit),
        "approved_max_loss": str(envelope.approved_max_loss),
        "event_key": envelope.event_key,
        "trading_day": envelope.trading_day.isoformat(),
        "market_session_id": (
            str(envelope.market_session_id) if envelope.market_session_id is not None else None
        ),
        "quoted_relative_spread": _decimal_or_none(envelope.quoted_relative_spread),
        "maximum_relative_spread": _decimal_or_none(envelope.maximum_relative_spread),
        "incremental_debit": _decimal_or_none(envelope.incremental_debit),
        "maximum_incremental_debit": _decimal_or_none(envelope.maximum_incremental_debit),
    }


def _decimal_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _exposure_to_json(exposure: GreekExposure | None) -> dict[str, str] | None:
    if exposure is None:
        return None
    return {
        "delta": str(exposure.delta),
        "gamma": str(exposure.gamma),
        "theta_per_day": str(exposure.theta_per_day),
        "vega_per_iv_point": str(exposure.vega_per_iv_point),
    }


def _certificate_matches_decision(
    session: Session,
    certificate_id: UUID,
    decision_id: UUID,
    terminal_code: str,
) -> bool:
    certificate = session.get(ExecutionCertificateRow, certificate_id)
    materialization_failure = terminal_code in {
        "ENTRY_FILLED_MATERIALIZATION_FAILED",
        "LIFECYCLE_FILLED_MATERIALIZATION_FAILED",
    }
    accepted_status = "FILLED" if materialization_failure else terminal_code
    if certificate is None or certificate.execution_status != accepted_status:
        return False
    intent = session.get(ExecutionIntentRow, certificate.execution_intent_id)
    if intent is None:
        return False
    if intent.entry_approval_id is not None:
        authorization = session.get(EntryApprovalCertificateRow, intent.entry_approval_id)
        origin_matches = (
            intent.action == "ENTRY"
            and certificate.entry_approval_id == intent.entry_approval_id
            and certificate.assessment_certificate_id is None
        )
    else:
        authorization = session.get(AssessmentCertificateRow, intent.assessment_certificate_id)
        origin_matches = (
            authorization is not None
            and intent.action in {"CLOSE", "ROLL"}
            and authorization.action == intent.action
            and certificate.entry_approval_id is None
            and certificate.assessment_certificate_id == intent.assessment_certificate_id
        )
    return (
        authorization is not None
        and authorization.agent_decision_id == decision_id
        and origin_matches
        and (
            not materialization_failure
            or (terminal_code == "ENTRY_FILLED_MATERIALIZATION_FAILED" and intent.action == "ENTRY")
            or (
                terminal_code == "LIFECYCLE_FILLED_MATERIALIZATION_FAILED"
                and intent.action in {"CLOSE", "ROLL"}
            )
        )
    )


def _decision_intent_id(session: Session, decision_id: UUID) -> UUID | None:
    entry_id = session.scalar(
        select(EntryApprovalCertificateRow.approval_id).where(
            EntryApprovalCertificateRow.agent_decision_id == decision_id
        )
    )
    if entry_id is not None:
        return session.scalar(
            select(ExecutionIntentRow.intent_id).where(
                ExecutionIntentRow.entry_approval_id == entry_id
            )
        )
    assessment_id = session.scalar(
        select(AssessmentCertificateRow.certificate_id).where(
            AssessmentCertificateRow.agent_decision_id == decision_id
        )
    )
    if assessment_id is None:
        return None
    return session.scalar(
        select(ExecutionIntentRow.intent_id).where(
            ExecutionIntentRow.assessment_certificate_id == assessment_id
        )
    )


def _tick_proof_hash(
    *,
    tick_id: UUID,
    account_role: AccountRole,
    account_fingerprint: str,
    tick_key: str,
    tick_boundary: datetime,
    actor: str,
    terminal_code: str,
    decision_id: UUID | None,
    decision_result_hash: str | None,
    execution_certificate_id: UUID | None,
) -> str:
    return canonical_agent_hash(
        {
            "domain": "alphadecay.agent-tick-proof.v1",
            "tick_id": str(tick_id),
            "account_role": account_role.value,
            "account_fingerprint": account_fingerprint,
            "tick_key": tick_key,
            "tick_boundary": _utc(tick_boundary).isoformat(),
            "actor": actor,
            "terminal_code": terminal_code,
            "decision_id": str(decision_id) if decision_id else None,
            "decision_result_hash": decision_result_hash,
            "execution_certificate_id": (
                str(execution_certificate_id) if execution_certificate_id else None
            ),
        }
    )


def _existing_tick(
    row: AgentTickRow, *, account_fingerprint: str, actor: str, boundary: datetime
) -> PersistedAgentTick:
    if (
        row.account_fingerprint != account_fingerprint
        or row.actor != actor
        or _utc(row.tick_boundary) != boundary
    ):
        raise ExecutionBlocked("AGENT_TICK_CONFLICT")
    return _tick_from_row(row, accepted=False, include_reservation=False)


def _tick_from_row(
    row: AgentTickRow,
    *,
    accepted: bool,
    include_reservation: bool,
) -> PersistedAgentTick:
    return PersistedAgentTick(
        tick_id=row.tick_id,
        account_role=AccountRole(row.account_role),
        account_fingerprint=row.account_fingerprint,
        tick_key=row.tick_key,
        tick_boundary=_utc(row.tick_boundary),
        actor=row.actor,
        accepted=accepted,
        reservation_token=row.reservation_token if include_reservation else None,
        completed=row.status == "COMPLETED",
        terminal_code=row.terminal_code,
        decision_id=row.decision_id,
        execution_certificate_id=row.execution_certificate_id,
        proof_hash=row.proof_hash,
        created_at=_utc(row.created_at),
        completed_at=_utc(row.completed_at) if row.completed_at else None,
    )
