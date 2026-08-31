from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import DateTime, Numeric, String, func, literal, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1.models import canonical_decimal
from backend.app.domain.option_contract_symbol import (
    NON_STANDARD_CONTRACT_UNSUPPORTED,
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)
from backend.app.persistence.sqlalchemy_models import (
    AccountReconciliationStateRow,
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AlpacaMarketSessionRow,
    AttemptObservationRow,
    BrokerMutationPermitRow,
    EntryApprovalCertificateRow,
    EntryMaterializationJobRow,
    ExecutionCertificateRow,
    ExecutionIntentRow,
    LifecycleLaunchAuthorityRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
    OrderAttemptRow,
    ThesisVersionRow,
    WholeAccountReconciliationRow,
)

from .contracts import LifecycleLaunchAuthority
from .fingerprint import option_position_fingerprint

_HASH = re.compile(r"^[0-9a-f]{64}$")
_FINALIZATION_CHECKS = (
    "TERMINAL",
    "REMAINDER_ABSENT",
    "WHOLE_ACCOUNT_RECONCILED",
)


class EntryMaterializationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SQLAlchemyEntryMaterializer:
    def __init__(self, sessions: sessionmaker) -> None:
        self._sessions = sessions

    def prepare(
        self,
        *,
        execution_intent_id: UUID,
        launch_authority: LifecycleLaunchAuthority,
        prepared_at: datetime,
    ) -> None:
        if (
            not isinstance(execution_intent_id, UUID)
            or not isinstance(launch_authority, LifecycleLaunchAuthority)
            or prepared_at.tzinfo is None
            or prepared_at.utcoffset() is None
        ):
            raise EntryMaterializationError("ENTRY_MATERIALIZATION_INPUT_INVALID")
        prepared_at = prepared_at.astimezone(UTC)
        values = _job_values(execution_intent_id, launch_authority, prepared_at)
        try:
            with self._sessions.begin() as session:
                intent = session.get(
                    ExecutionIntentRow,
                    execution_intent_id,
                    with_for_update=True,
                )
                approval = (
                    session.get(EntryApprovalCertificateRow, intent.entry_approval_id)
                    if intent is not None and intent.entry_approval_id is not None
                    else None
                )
                account = (
                    session.get(AccountRoleRow, intent.account_role) if intent is not None else None
                )
                decision = (
                    session.get(AgentDecisionRow, approval.agent_decision_id)
                    if approval is not None and approval.agent_decision_id is not None
                    else None
                )
                snapshot = (
                    session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
                    if decision is not None
                    else None
                )
                thesis = (
                    session.get(ThesisVersionRow, approval.thesis_version_id)
                    if approval is not None
                    else None
                )
                if (
                    intent is None
                    or approval is None
                    or account is None
                    or intent.action != "ENTRY"
                    or intent.state != "APPROVED"
                    or intent.account_role != "DEVELOPMENT"
                    or intent.assessment_certificate_id is not None
                    or approval.account_role != "DEVELOPMENT"
                    or decision is None
                    or snapshot is None
                    or thesis is None
                    or decision.decision_kind != "OPPORTUNITY"
                    or decision.outcome != "ENTRY_APPROVED"
                    or not decision.autonomy_authorized
                    or decision.account_role != "DEVELOPMENT"
                    or decision.account_fingerprint != account.account_fingerprint
                    or decision.thesis_version_id != thesis.thesis_version_id
                    or snapshot.thesis_version_id != thesis.thesis_version_id
                    or snapshot.account_role != "DEVELOPMENT"
                    or snapshot.account_fingerprint != account.account_fingerprint
                    or snapshot.decision_kind != "OPPORTUNITY"
                    or _utc(decision.decision_boundary) > _utc(thesis.frozen_at)
                    or _utc(thesis.frozen_at) > _utc(snapshot.observed_at)
                    or _utc(thesis.frozen_at) > _utc(decision.created_at)
                    or approval.policy_hash != launch_authority.entry_policy_hash
                    or intent.policy_hash != launch_authority.entry_policy_hash
                    or prepared_at < _utc(launch_authority.entry_boundary_at)
                ):
                    raise EntryMaterializationError("ENTRY_MATERIALIZATION_PREPARATION_INVALID")
                values.update(
                    entry_approval_id=approval.approval_id,
                    account_role="DEVELOPMENT",
                    account_fingerprint=account.account_fingerprint,
                )
                values["job_hash"] = _materialization_job_hash(session, values)
                existing = session.get(EntryMaterializationJobRow, execution_intent_id)
                if existing is not None:
                    if _prepared_job_row_values(existing) != values or not _job_hash_valid(
                        session, existing
                    ):
                        raise EntryMaterializationError("ENTRY_MATERIALIZATION_JOB_CONFLICT")
                    return
                session.add(EntryMaterializationJobRow(**values))
                session.flush()
        except EntryMaterializationError:
            raise
        except (ArithmeticError, KeyError, SQLAlchemyError, TypeError, ValueError) as error:
            raise EntryMaterializationError("ENTRY_MATERIALIZATION_PREPARATION_INVALID") from error

    def recover_pending(
        self,
        *,
        account_role: str,
        account_fingerprint: str,
    ) -> tuple[UUID, ...]:
        self._validate_recovery_authority(account_role, account_fingerprint)
        with self._sessions() as session:
            pending = session.execute(
                select(
                    EntryMaterializationJobRow.execution_intent_id,
                    ExecutionCertificateRow.certificate_id,
                    ExecutionCertificateRow.execution_status,
                )
                .outerjoin(
                    ExecutionCertificateRow,
                    ExecutionCertificateRow.execution_intent_id
                    == EntryMaterializationJobRow.execution_intent_id,
                )
                .where(
                    EntryMaterializationJobRow.account_role == account_role,
                    EntryMaterializationJobRow.account_fingerprint == account_fingerprint,
                    EntryMaterializationJobRow.completed_at.is_(None),
                )
                .order_by(EntryMaterializationJobRow.prepared_at)
            ).all()
        recovered: list[UUID] = []
        for intent_id, certificate_id, status in pending:
            with self._sessions() as session:
                job = session.get(EntryMaterializationJobRow, intent_id)
                if job is None or not _job_hash_valid(session, job):
                    raise EntryMaterializationError("ENTRY_MATERIALIZATION_JOB_INVALID")
            if certificate_id is None:
                continue
            if status == "FILLED":
                with self._sessions() as session:
                    job = session.get(EntryMaterializationJobRow, intent_id)
                    if job is None:
                        raise EntryMaterializationError("ENTRY_MATERIALIZATION_JOB_INVALID")
                    launch = _launch_from_job(job)
                recovered.append(
                    self.materialize(
                        execution_certificate_id=certificate_id,
                        launch_authority=launch,
                    )
                )
            else:
                self._resolve_without_materialization(intent_id, certificate_id, status)
        return tuple(recovered)

    def pending_execution_intents(
        self,
        *,
        account_role: str,
        account_fingerprint: str,
    ) -> tuple[UUID, ...]:
        self._validate_recovery_authority(account_role, account_fingerprint)
        with self._sessions() as session:
            rows = (
                session.execute(
                    select(EntryMaterializationJobRow)
                    .outerjoin(
                        ExecutionCertificateRow,
                        ExecutionCertificateRow.execution_intent_id
                        == EntryMaterializationJobRow.execution_intent_id,
                    )
                    .where(
                        EntryMaterializationJobRow.account_role == account_role,
                        EntryMaterializationJobRow.account_fingerprint == account_fingerprint,
                        EntryMaterializationJobRow.completed_at.is_(None),
                        ExecutionCertificateRow.certificate_id.is_(None),
                    )
                    .order_by(EntryMaterializationJobRow.prepared_at)
                )
                .scalars()
                .all()
            )
            if len(rows) > 1 or any(not _job_hash_valid(session, row) for row in rows):
                raise EntryMaterializationError("ENTRY_MATERIALIZATION_JOB_INVALID")
            return tuple(row.execution_intent_id for row in rows)

    def _validate_recovery_authority(
        self,
        account_role: str,
        account_fingerprint: str,
    ) -> None:
        if account_role != "DEVELOPMENT" or _HASH.fullmatch(account_fingerprint) is None:
            raise EntryMaterializationError("ENTRY_MATERIALIZATION_RECOVERY_AUTHORITY_INVALID")
        with self._sessions() as session:
            account = session.get(AccountRoleRow, account_role)
            if account is None or account.account_fingerprint != account_fingerprint:
                raise EntryMaterializationError("ENTRY_MATERIALIZATION_RECOVERY_AUTHORITY_INVALID")

    def _resolve_without_materialization(
        self,
        intent_id: UUID,
        certificate_id: UUID,
        status: str,
    ) -> None:
        if status not in {
            "REJECTED",
            "CANCELED",
            "EXPIRED",
            "REPLACED",
        }:
            raise EntryMaterializationError("ENTRY_MATERIALIZATION_TERMINAL_STATUS_INVALID")
        with self._sessions.begin() as session:
            job = session.get(EntryMaterializationJobRow, intent_id, with_for_update=True)
            certificate = session.get(ExecutionCertificateRow, certificate_id)
            intent = session.get(ExecutionIntentRow, intent_id)
            if (
                job is None
                or certificate is None
                or intent is None
                or not _job_hash_valid(session, job)
                or certificate.execution_intent_id != intent_id
                or certificate.execution_status != status
                or certificate.entry_approval_id != job.entry_approval_id
                or certificate.assessment_certificate_id is not None
                or intent.entry_approval_id != job.entry_approval_id
                or intent.assessment_certificate_id is not None
                or job.account_role != "DEVELOPMENT"
            ):
                raise EntryMaterializationError("ENTRY_MATERIALIZATION_JOB_INVALID")
            if job.completed_at is not None:
                if job.terminal_status != status or job.managed_position_id is not None:
                    raise EntryMaterializationError("ENTRY_MATERIALIZATION_JOB_CONFLICT")
                return
            job.terminal_status = status
            job.completed_at = _database_now(session)
            session.flush()

    def materialize(
        self,
        *,
        execution_certificate_id: UUID,
        launch_authority: LifecycleLaunchAuthority,
    ) -> UUID:
        if not isinstance(execution_certificate_id, UUID) or not isinstance(
            launch_authority, LifecycleLaunchAuthority
        ):
            raise EntryMaterializationError("ENTRY_MATERIALIZATION_INPUT_INVALID")
        try:
            with self._sessions.begin() as session:
                certificate = session.scalar(
                    select(ExecutionCertificateRow)
                    .where(ExecutionCertificateRow.certificate_id == execution_certificate_id)
                    .with_for_update()
                )
                if certificate is None:
                    raise EntryMaterializationError("ENTRY_CERTIFICATE_NOT_FOUND")
                intent = session.scalar(
                    select(ExecutionIntentRow)
                    .where(ExecutionIntentRow.intent_id == certificate.execution_intent_id)
                    .with_for_update()
                )
                if intent is None:
                    raise EntryMaterializationError("ENTRY_INTENT_NOT_FOUND")
                job = session.get(
                    EntryMaterializationJobRow,
                    intent.intent_id,
                    with_for_update=True,
                )
                if (
                    job is None
                    or _launch_from_job(job) != launch_authority
                    or not _job_hash_valid(session, job)
                ):
                    raise EntryMaterializationError("ENTRY_MATERIALIZATION_JOB_INVALID")
                account = session.scalar(
                    select(AccountRoleRow)
                    .where(AccountRoleRow.role == intent.account_role)
                    .with_for_update()
                )
                approval = (
                    session.get(EntryApprovalCertificateRow, intent.entry_approval_id)
                    if intent.entry_approval_id is not None
                    else None
                )
                thesis = (
                    session.get(ThesisVersionRow, approval.thesis_version_id)
                    if approval is not None
                    else None
                )
                decision = (
                    session.get(AgentDecisionRow, approval.agent_decision_id)
                    if approval is not None and approval.agent_decision_id is not None
                    else None
                )
                reconciliation = (
                    session.get(
                        WholeAccountReconciliationRow,
                        certificate.reconciliation_id,
                    )
                    if certificate.reconciliation_id is not None
                    else None
                )
                attempts = session.scalars(
                    select(OrderAttemptRow)
                    .where(OrderAttemptRow.execution_intent_id == intent.intent_id)
                    .order_by(OrderAttemptRow.attempt_ordinal)
                    .with_for_update()
                ).all()
                state = (
                    session.scalar(
                        select(AccountReconciliationStateRow).where(
                            AccountReconciliationStateRow.authority_reconciliation_id
                            == reconciliation.reconciliation_id
                        )
                    )
                    if reconciliation is not None
                    else None
                )
                permit = (
                    session.get(BrokerMutationPermitRow, state.authority_permit_id)
                    if state is not None and state.authority_permit_id is not None
                    else None
                )
                observations = session.scalars(
                    select(AttemptObservationRow)
                    .where(AttemptObservationRow.execution_intent_id == intent.intent_id)
                    .order_by(
                        AttemptObservationRow.observation_sequence,
                        AttemptObservationRow.observation_id,
                    )
                    .with_for_update()
                ).all()
                observation = (
                    observations[-1]
                    if state is not None
                    and observations
                    and observations[-1].observation_id == state.authority_observation_id
                    else None
                )
                market_sessions = (
                    session.scalars(
                        select(AlpacaMarketSessionRow)
                        .where(
                            AlpacaMarketSessionRow.open_at <= reconciliation.accepted_at,
                            AlpacaMarketSessionRow.close_at >= reconciliation.accepted_at,
                        )
                        .with_for_update()
                    ).all()
                    if reconciliation is not None
                    else []
                )
                session_row = market_sessions[0] if len(market_sessions) == 1 else None
                inventory, activities, cashflow, fingerprint = _validate_lineage(
                    certificate=certificate,
                    intent=intent,
                    account=account,
                    approval=approval,
                    thesis=thesis,
                    decision=decision,
                    reconciliation=reconciliation,
                    state=state,
                    permit=permit,
                    observation=observation,
                    market_session=session_row,
                    attempts=attempts,
                    launch=launch_authority,
                )
                if (
                    job.entry_approval_id != approval.approval_id
                    or job.account_role != "DEVELOPMENT"
                    or job.account_fingerprint != account.account_fingerprint
                ):
                    raise EntryMaterializationError("ENTRY_MATERIALIZATION_JOB_INVALID")
                ids = _materialization_ids(certificate.certificate_id)
                existing = session.scalar(
                    select(ManagedLifecyclePositionRow).where(
                        (ManagedLifecyclePositionRow.managed_position_id == ids.position)
                        | (
                            ManagedLifecyclePositionRow.entry_execution_certificate_id
                            == certificate.certificate_id
                        )
                        | (
                            (ManagedLifecyclePositionRow.account_role == "DEVELOPMENT")
                            & (ManagedLifecyclePositionRow.closed_at.is_(None))
                        )
                    )
                )
                if existing is not None:
                    _validate_existing(
                        session,
                        existing=existing,
                        ids=ids,
                        certificate=certificate,
                        intent=intent,
                        approval=approval,
                        thesis=thesis,
                        reconciliation=reconciliation,
                        state=state,
                        market_session=session_row,
                        inventory=inventory,
                        activities=activities,
                        cashflow=cashflow,
                        fingerprint=fingerprint,
                        launch=launch_authority,
                    )
                    _complete_job(job, existing.managed_position_id, _database_now(session))
                    return existing.managed_position_id

                transition_values = {
                    "transition_id": ids.transition,
                    "managed_position_id": ids.position,
                    "predecessor_transition_id": None,
                    "transition_sequence": 0,
                    "action": "ENTRY",
                    "execution_intent_id": intent.intent_id,
                    "execution_certificate_id": certificate.certificate_id,
                    "post_reconciliation_id": reconciliation.reconciliation_id,
                    "fill_activity_manifest": activities,
                    "fill_activity_manifest_hash": _json_hash(session, activities),
                    "cashflow_contribution": cashflow,
                    "resulting_position_fingerprint": fingerprint,
                    "occurred_at": _utc(reconciliation.accepted_at),
                    "market_session_id": session_row.market_session_id,
                }
                transition_hash = _row_hash(session, transition_values)
                snapshot_values = {
                    "snapshot_id": ids.snapshot,
                    "managed_position_id": ids.position,
                    "predecessor_snapshot_id": None,
                    "transition_id": ids.transition,
                    "reconciliation_id": reconciliation.reconciliation_id,
                    "reconciliation_state_id": state.state_id,
                    "normalized_inventory": inventory,
                    "inventory_hash": _json_hash(session, inventory),
                    "activity_manifest": reconciliation.sweep_payload["activities"],
                    "activity_manifest_hash": _json_hash(
                        session, reconciliation.sweep_payload["activities"]
                    ),
                    "cumulative_cashflow": cashflow,
                    "rolls_on_trading_day": 0,
                    "market_session_id": session_row.market_session_id,
                    "position_fingerprint": fingerprint,
                    "accepted_at": _utc(reconciliation.accepted_at),
                }
                snapshot_hash = _row_hash(session, snapshot_values)
                session.add(
                    ManagedLifecyclePositionRow(
                        managed_position_id=ids.position,
                        account_role="DEVELOPMENT",
                        account_fingerprint=account.account_fingerprint,
                        entry_execution_certificate_id=certificate.certificate_id,
                        entry_intent_id=intent.intent_id,
                        entry_approval_id=approval.approval_id,
                        thesis_version_id=thesis.thesis_version_id,
                        entry_reconciliation_id=reconciliation.reconciliation_id,
                        current_reconciliation_state_id=state.state_id,
                        current_snapshot_id=ids.snapshot,
                        active_position_fingerprint=fingerprint,
                        activated_at=_utc(reconciliation.accepted_at),
                        closed_at=None,
                    )
                )
                session.flush()
                session.add(
                    ManagedPositionTransitionRow(
                        **transition_values,
                        transition_hash=transition_hash,
                    )
                )
                session.flush()
                session.add(
                    ManagedPositionSnapshotRow(
                        **snapshot_values,
                        snapshot_hash=snapshot_hash,
                    )
                )
                session.flush()
                session.add(
                    LifecycleLaunchAuthorityRow(
                        managed_position_id=ids.position,
                        beta60=launch_authority.beta60,
                        benchmark_symbol=launch_authority.benchmark_symbol,
                        entry_boundary_at=launch_authority.entry_boundary_at,
                        entry_policy_hash=launch_authority.entry_policy_hash,
                        underlying_source_hash=launch_authority.underlying_source_hash,
                        benchmark_source_hash=launch_authority.benchmark_source_hash,
                        completed_bar_source_hash=launch_authority.completed_bar_source_hash,
                        created_at=_utc(thesis.frozen_at),
                    )
                )
                session.flush()
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                _complete_job(job, ids.position, _database_now(session))
                return ids.position
        except EntryMaterializationError:
            raise
        except (ArithmeticError, KeyError, SQLAlchemyError, TypeError, ValueError) as error:
            raise EntryMaterializationError("ENTRY_MATERIALIZATION_LINEAGE_INVALID") from error


class _MaterializationIds:
    def __init__(self, certificate_id: UUID) -> None:
        self.position = uuid5(NAMESPACE_URL, f"alphadecay:managed-position:{certificate_id}")
        self.transition = uuid5(
            NAMESPACE_URL, f"alphadecay:managed-position-entry:{certificate_id}"
        )
        self.snapshot = uuid5(
            NAMESPACE_URL, f"alphadecay:managed-position-snapshot:{certificate_id}"
        )


def _materialization_ids(certificate_id: UUID) -> _MaterializationIds:
    return _MaterializationIds(certificate_id)


def _job_values(
    execution_intent_id: UUID,
    launch: LifecycleLaunchAuthority,
    prepared_at: datetime,
) -> dict[str, object]:
    return {
        "execution_intent_id": execution_intent_id,
        "beta60": launch.beta60,
        "benchmark_symbol": launch.benchmark_symbol,
        "entry_boundary_at": launch.entry_boundary_at,
        "entry_policy_hash": launch.entry_policy_hash,
        "underlying_source_hash": launch.underlying_source_hash,
        "benchmark_source_hash": launch.benchmark_source_hash,
        "completed_bar_source_hash": launch.completed_bar_source_hash,
        "prepared_at": prepared_at,
        "managed_position_id": None,
        "terminal_status": None,
        "completed_at": None,
    }


def _prepared_job_row_values(row: EntryMaterializationJobRow) -> dict[str, object]:
    values = {
        "execution_intent_id": row.execution_intent_id,
        "entry_approval_id": row.entry_approval_id,
        "account_role": row.account_role,
        "account_fingerprint": row.account_fingerprint,
        "beta60": row.beta60,
        "benchmark_symbol": row.benchmark_symbol,
        "entry_boundary_at": _utc(row.entry_boundary_at),
        "entry_policy_hash": row.entry_policy_hash,
        "underlying_source_hash": row.underlying_source_hash,
        "benchmark_source_hash": row.benchmark_source_hash,
        "completed_bar_source_hash": row.completed_bar_source_hash,
        "prepared_at": _utc(row.prepared_at),
        "managed_position_id": None,
        "terminal_status": None,
        "completed_at": None,
    }
    values["job_hash"] = row.job_hash
    return values


def _job_hash_valid(session, row: EntryMaterializationJobRow) -> bool:
    prepared = _prepared_job_row_values(row)
    recorded = prepared.pop("job_hash")
    return recorded == _materialization_job_hash(session, prepared)


def _materialization_job_hash(session, values: dict[str, object]) -> str:
    return _row_hash(session, values)


def _launch_from_job(row: EntryMaterializationJobRow) -> LifecycleLaunchAuthority:
    return LifecycleLaunchAuthority(
        beta60=Decimal(row.beta60),
        benchmark_symbol=row.benchmark_symbol,
        entry_boundary_at=_utc(row.entry_boundary_at),
        entry_policy_hash=row.entry_policy_hash,
        underlying_source_hash=row.underlying_source_hash,
        benchmark_source_hash=row.benchmark_source_hash,
        completed_bar_source_hash=row.completed_bar_source_hash,
    )


def _complete_job(
    job: EntryMaterializationJobRow,
    managed_position_id: UUID,
    completed_at: datetime,
) -> None:
    completed_at = _utc(completed_at)
    if job.completed_at is not None:
        if job.managed_position_id != managed_position_id:
            raise EntryMaterializationError("ENTRY_MATERIALIZATION_JOB_CONFLICT")
        return
    job.managed_position_id = managed_position_id
    job.terminal_status = "FILLED"
    job.completed_at = completed_at


def _database_now(session) -> datetime:
    value = session.scalar(
        select(
            func.clock_timestamp()
            if session.bind is not None and session.bind.dialect.name == "postgresql"
            else func.current_timestamp()
        )
    )
    if not isinstance(value, datetime):
        raise EntryMaterializationError("ENTRY_MATERIALIZATION_CLOCK_INVALID")
    return _utc(value)


def _validate_lineage(
    *,
    certificate,
    intent,
    account,
    approval,
    thesis,
    decision,
    reconciliation,
    state,
    permit,
    observation,
    market_session,
    attempts,
    launch,
):
    if (
        intent.action != "ENTRY"
        or intent.state != "TERMINAL"
        or not intent.first_fill_consumed
        or intent.account_role != "DEVELOPMENT"
        or account is None
        or account.role != "DEVELOPMENT"
        or approval is None
        or thesis is None
        or decision is None
        or reconciliation is None
        or state is None
        or market_session is None
        or certificate.execution_status != "FILLED"
        or certificate.certificate_id
        != uuid5(NAMESPACE_URL, f"alphadecay:execution:{intent.intent_digest}")
        or certificate.entry_approval_id != approval.approval_id
        or certificate.assessment_certificate_id is not None
        or certificate.reconciliation_id != reconciliation.reconciliation_id
        or tuple(certificate.reconciliation_checks) != _FINALIZATION_CHECKS
        or intent.entry_approval_id != approval.approval_id
        or intent.assessment_certificate_id is not None
        or intent.fingerprint != approval.book_fingerprint
        or intent.envelope_hash != approval.envelope_hash
        or Decimal(intent.approved_max_loss) != Decimal(approval.approved_max_loss)
        or intent.quantity != approval.quantity
        or approval.thesis_version_id != thesis.thesis_version_id
        or approval.agent_decision_id != decision.decision_id
        or not approval.valid
        or any(
            value != "DEVELOPMENT"
            for value in (
                approval.account_role,
                thesis.account_role,
                decision.account_role,
                reconciliation.account_role,
                state.account_role,
            )
        )
        or any(
            value != account.account_fingerprint
            for value in (
                decision.account_fingerprint,
                reconciliation.account_fingerprint,
                state.account_fingerprint,
            )
        )
        or any(
            value != thesis.policy_hash
            for value in (
                intent.policy_hash,
                approval.policy_hash,
                decision.policy_hash,
                launch.entry_policy_hash,
            )
        )
        or decision.thesis_version_id != thesis.thesis_version_id
        or decision.decision_kind != "OPPORTUNITY"
        or decision.outcome != "ENTRY_APPROVED"
        or not decision.autonomy_authorized
        or reconciliation.execution_intent_id != intent.intent_id
        or reconciliation.intent_digest != intent.intent_digest
        or certificate.reconciliation_hash != reconciliation.reconciliation_hash
        or certificate.last_observation_hash
        != (observation.observation_hash if observation is not None else None)
        or not reconciliation.safe
        or reconciliation.block_codes
        or state.authority_reconciliation_id != reconciliation.reconciliation_id
        or state.accepted_at != reconciliation.accepted_at
        or state.expected_positions != reconciliation.sweep_payload.get("final_positions")
        or state.known_activities != reconciliation.sweep_payload.get("activities")
        or state.expected_open_orders
        != reconciliation.expectation_payload.get("expected_open_orders")
        or Decimal(state.expected_cash)
        != Decimal(str(reconciliation.expectation_payload.get("expected_cash")))
        or state.authority_permit_id != (permit.permit_id if permit is not None else None)
        or state.authority_observation_id
        != (observation.observation_id if observation is not None else None)
        or state.authority_permit_request_hash
        != (permit.request_hash if permit is not None else None)
        or _utc(decision.decision_boundary) > _utc(thesis.frozen_at)
        or _utc(launch.entry_boundary_at) > _utc(thesis.frozen_at)
        or _utc(thesis.frozen_at) > _utc(reconciliation.accepted_at)
    ):
        raise EntryMaterializationError("ENTRY_LINEAGE_INVALID")
    if not attempts or tuple(item.attempt_ordinal for item in attempts) != tuple(
        range(len(attempts))
    ):
        raise EntryMaterializationError("ENTRY_ATTEMPT_LINEAGE_INVALID")
    clients = tuple(item.client_order_id for item in attempts)
    if tuple(certificate.attempt_ids) != clients or any(not value for value in clients):
        raise EntryMaterializationError("ENTRY_ATTEMPT_LINEAGE_INVALID")
    if attempts[-1].state != "FILLED" or attempts[-1].filled_quantity != intent.quantity:
        raise EntryMaterializationError("ENTRY_ATTEMPT_NOT_FILLED")
    if any(
        item.execution_intent_id != intent.intent_id or item.quantity != intent.quantity
        for item in attempts
    ) or any(item.filled_quantity for item in attempts[:-1]):
        raise EntryMaterializationError("ENTRY_ATTEMPT_LINEAGE_INVALID")
    final_attempt = attempts[-1]
    expected_observation = {
        "intent_id": str(intent.intent_id),
        "ordinal": final_attempt.attempt_ordinal,
        "client_order_id": final_attempt.client_order_id,
        "request_hash": final_attempt.request_hash,
        "state": final_attempt.state,
        "provider_order_id": final_attempt.provider_order_id,
        "filled_quantity": final_attempt.filled_quantity,
        "quantity": final_attempt.quantity,
    }
    if (
        permit is None
        or observation is None
        or reconciliation.attempt_ordinal != final_attempt.attempt_ordinal
        or reconciliation.request_hash != final_attempt.request_hash
        or reconciliation.purpose != permit.mutation_kind
        or permit.execution_intent_id != intent.intent_id
        or permit.intent_digest != intent.intent_digest
        or permit.attempt_ordinal != final_attempt.attempt_ordinal
        or permit.request_hash != final_attempt.request_hash
        or permit.state != "CONSUMED"
        or final_attempt.broker_permit_id != permit.permit_id
        or observation.permit_id != permit.permit_id
        or observation.attempt_id != final_attempt.attempt_id
        or observation.attempt_ordinal != final_attempt.attempt_ordinal
        or not observation.provider_present
        or not isinstance(observation.observed_payload, dict)
        or any(
            observation.observed_payload.get(key) != value
            for key, value in expected_observation.items()
        )
        or Decimal(str(observation.observed_payload.get("fill_cash_flow")))
        != Decimal(final_attempt.fill_cash_flow)
    ):
        raise EntryMaterializationError("ENTRY_FINALIZATION_AUTHORITY_INVALID")
    activities = reconciliation.sweep_payload.get("activities")
    inventory = reconciliation.sweep_payload.get("final_positions")
    if not isinstance(activities, list) or not isinstance(inventory, list):
        raise EntryMaterializationError("ENTRY_RECONCILIATION_PAYLOAD_INVALID")
    fill_activities = sorted(
        (
            item
            for item in activities
            if isinstance(item, dict) and item.get("activity_type") in {"OPTRD", "FILL"}
        ),
        key=lambda item: str(item.get("activity_id_hash", "")),
    )
    filled_attempts = [item for item in attempts if item.filled_quantity > 0]
    if not _activities_match_attempts(fill_activities, filled_attempts, intent.legs):
        raise EntryMaterializationError("ENTRY_ACTIVITY_LINEAGE_INCOMPLETE")
    normalized_inventory, fingerprint = _validate_vertical(inventory, intent, thesis)
    cashflow = sum(
        (item.fill_cash_flow or Decimal(0)) for item in attempts if item.filled_quantity > 0
    )
    return normalized_inventory, fill_activities, cashflow, fingerprint


def _activities_match_attempts(activities, attempts, legs) -> bool:
    expected: dict[tuple[str, str, str], Decimal] = {}
    for attempt in attempts:
        if not attempt.provider_order_id:
            return False
        for leg in legs:
            if leg.get("intent") not in {"BUY_TO_OPEN", "SELL_TO_OPEN"}:
                return False
            key = (
                attempt.client_order_id,
                attempt.provider_order_id,
                str(leg.get("symbol", "")),
            )
            try:
                expected[key] = expected.get(key, Decimal(0)) + Decimal(
                    attempt.filled_quantity
                    * int(leg["ratio"])
                    * (1 if leg["intent"] == "BUY_TO_OPEN" else -1)
                )
            except (KeyError, TypeError, ValueError):
                return False

    observed: dict[tuple[str, str, str], Decimal] = {}
    activity_hashes: set[str] = set()
    for activity in activities:
        activity_hash = activity.get("activity_id_hash")
        key = (
            activity.get("client_order_id"),
            activity.get("provider_order_id"),
            activity.get("symbol"),
        )
        try:
            quantity = Decimal(str(activity["signed_quantity"]))
        except (KeyError, TypeError, ValueError):
            return False
        if (
            not isinstance(activity_hash, str)
            or _HASH.fullmatch(activity_hash) is None
            or activity_hash in activity_hashes
            or key not in expected
            or not quantity.is_finite()
            or quantity == 0
        ):
            return False
        activity_hashes.add(activity_hash)
        observed[key] = observed.get(key, Decimal(0)) + quantity
    return observed == expected


def _validate_vertical(inventory, intent, thesis):
    if len(inventory) != 2:
        raise EntryMaterializationError("ENTRY_INVENTORY_NOT_VERTICAL")
    parsed = []
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {
            "kind",
            "symbol",
            "signed_quantity",
            "multiplier",
        }:
            raise EntryMaterializationError("ENTRY_INVENTORY_NOT_VERTICAL")
        try:
            contract = parse_standard_option_contract_symbol(
                item.get("symbol"),
                underlying_symbol=thesis.underlying,
            )
            quantity = Decimal(str(item["signed_quantity"]))
            multiplier = item["multiplier"]
        except OptionContractSymbolError as error:
            if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
                raise EntryMaterializationError(error.code) from error
            raise EntryMaterializationError("ENTRY_INVENTORY_NOT_VERTICAL") from error
        except (KeyError, TypeError, ValueError):
            raise EntryMaterializationError("ENTRY_INVENTORY_NOT_VERTICAL") from None
        if (
            item["kind"] != "OPTION"
            or type(multiplier) is not int
            or multiplier != 100
            or not quantity.is_finite()
            or quantity == 0
            or quantity != quantity.to_integral_value()
        ):
            raise EntryMaterializationError("ENTRY_INVENTORY_NOT_VERTICAL")
        parsed.append((contract, quantity, str(item["symbol"]), multiplier))
    if (
        [item[2] for item in parsed] != sorted(item[2] for item in parsed)
        or parsed[0][0].root_symbol != parsed[1][0].root_symbol
        or parsed[0][0].expiration_date != parsed[1][0].expiration_date
        or parsed[0][0].right != parsed[1][0].right
        or parsed[0][0].strike_price >= parsed[1][0].strike_price
        or {parsed[0][1] > 0, parsed[1][1] > 0} != {True, False}
        or abs(parsed[0][1]) != abs(parsed[1][1])
        or abs(parsed[0][1]) != intent.quantity
    ):
        raise EntryMaterializationError("ENTRY_INVENTORY_NOT_VERTICAL")
    expected = {
        str(item["symbol"]): Decimal(intent.quantity * int(item["ratio"]))
        * (1 if item["intent"] == "BUY_TO_OPEN" else -1)
        for item in intent.legs
        if item.get("intent") in {"BUY_TO_OPEN", "SELL_TO_OPEN"}
    }
    if len(expected) != 2 or any(
        expected.get(symbol) != quantity for _, quantity, symbol, _ in parsed
    ):
        raise EntryMaterializationError("ENTRY_INVENTORY_INTENT_MISMATCH")
    normalized = [
        {
            "kind": "OPTION",
            "symbol": symbol,
            "signed_quantity": canonical_decimal(quantity),
            "multiplier": multiplier,
        }
        for _, quantity, symbol, multiplier in parsed
    ]
    return normalized, option_position_fingerprint(
        tuple((item["symbol"], Decimal(item["signed_quantity"]), 100) for item in normalized)
    )


def _validate_existing(session, **values) -> None:
    existing = values["existing"]
    ids = values["ids"]
    transition = session.get(ManagedPositionTransitionRow, ids.transition)
    snapshot = session.get(ManagedPositionSnapshotRow, ids.snapshot)
    launch_row = session.get(LifecycleLaunchAuthorityRow, ids.position)
    launch = values["launch"]
    transition_values = {
        "transition_id": ids.transition,
        "managed_position_id": ids.position,
        "predecessor_transition_id": None,
        "transition_sequence": 0,
        "action": "ENTRY",
        "execution_intent_id": values["intent"].intent_id,
        "execution_certificate_id": values["certificate"].certificate_id,
        "post_reconciliation_id": values["reconciliation"].reconciliation_id,
        "fill_activity_manifest": values["activities"],
        "fill_activity_manifest_hash": _json_hash(session, values["activities"]),
        "cashflow_contribution": values["cashflow"],
        "resulting_position_fingerprint": values["fingerprint"],
        "occurred_at": _utc(values["reconciliation"].accepted_at),
        "market_session_id": values["market_session"].market_session_id,
    }
    snapshot_values = {
        "snapshot_id": ids.snapshot,
        "managed_position_id": ids.position,
        "predecessor_snapshot_id": None,
        "transition_id": ids.transition,
        "reconciliation_id": values["reconciliation"].reconciliation_id,
        "reconciliation_state_id": values["state"].state_id,
        "normalized_inventory": values["inventory"],
        "inventory_hash": _json_hash(session, values["inventory"]),
        "activity_manifest": values["reconciliation"].sweep_payload["activities"],
        "activity_manifest_hash": _json_hash(
            session, values["reconciliation"].sweep_payload["activities"]
        ),
        "cumulative_cashflow": values["cashflow"],
        "rolls_on_trading_day": 0,
        "market_session_id": values["market_session"].market_session_id,
        "position_fingerprint": values["fingerprint"],
        "accepted_at": _utc(values["reconciliation"].accepted_at),
    }
    expected = (
        ids.position,
        "DEVELOPMENT",
        values["reconciliation"].account_fingerprint,
        values["certificate"].certificate_id,
        values["intent"].intent_id,
        values["approval"].approval_id,
        values["thesis"].thesis_version_id,
        values["reconciliation"].reconciliation_id,
        values["state"].state_id,
        ids.snapshot,
        values["fingerprint"],
        _utc(values["reconciliation"].accepted_at),
        None,
    )
    durable = (
        existing.managed_position_id,
        existing.account_role,
        existing.account_fingerprint,
        existing.entry_execution_certificate_id,
        existing.entry_intent_id,
        existing.entry_approval_id,
        existing.thesis_version_id,
        existing.entry_reconciliation_id,
        existing.current_reconciliation_state_id,
        existing.current_snapshot_id,
        existing.active_position_fingerprint,
        _utc(existing.activated_at),
        existing.closed_at,
    )
    if durable != expected or transition is None or snapshot is None or launch_row is None:
        raise EntryMaterializationError("ENTRY_MATERIALIZATION_CONFLICT")
    if (
        transition.managed_position_id != ids.position
        or transition.predecessor_transition_id is not None
        or transition.transition_sequence != 0
        or transition.action != "ENTRY"
        or transition.execution_intent_id != values["intent"].intent_id
        or transition.execution_certificate_id != values["certificate"].certificate_id
        or transition.post_reconciliation_id != values["reconciliation"].reconciliation_id
        or transition.fill_activity_manifest != values["activities"]
        or transition.fill_activity_manifest_hash != _json_hash(session, values["activities"])
        or Decimal(transition.cashflow_contribution) != values["cashflow"]
        or transition.resulting_position_fingerprint != values["fingerprint"]
        or _utc(transition.occurred_at) != _utc(values["reconciliation"].accepted_at)
        or transition.market_session_id != values["market_session"].market_session_id
        or transition.transition_hash != _row_hash(session, transition_values)
        or snapshot.managed_position_id != ids.position
        or snapshot.predecessor_snapshot_id is not None
        or snapshot.transition_id != ids.transition
        or snapshot.reconciliation_id != values["reconciliation"].reconciliation_id
        or snapshot.reconciliation_state_id != values["state"].state_id
        or snapshot.normalized_inventory != values["inventory"]
        or snapshot.inventory_hash != _json_hash(session, values["inventory"])
        or snapshot.activity_manifest != values["reconciliation"].sweep_payload["activities"]
        or snapshot.activity_manifest_hash
        != _json_hash(session, values["reconciliation"].sweep_payload["activities"])
        or Decimal(snapshot.cumulative_cashflow) != values["cashflow"]
        or snapshot.rolls_on_trading_day != 0
        or snapshot.market_session_id != values["market_session"].market_session_id
        or snapshot.position_fingerprint != values["fingerprint"]
        or _utc(snapshot.accepted_at) != _utc(values["reconciliation"].accepted_at)
        or snapshot.snapshot_hash != _row_hash(session, snapshot_values)
        or launch_row.beta60 != launch.beta60
        or launch_row.benchmark_symbol != launch.benchmark_symbol
        or _utc(launch_row.entry_boundary_at) != launch.entry_boundary_at
        or launch_row.entry_policy_hash != launch.entry_policy_hash
        or launch_row.underlying_source_hash != launch.underlying_source_hash
        or launch_row.benchmark_source_hash != launch.benchmark_source_hash
        or launch_row.completed_bar_source_hash != launch.completed_bar_source_hash
        or _utc(launch_row.created_at) != _utc(values["thesis"].frozen_at)
    ):
        raise EntryMaterializationError("ENTRY_MATERIALIZATION_CONFLICT")


def _json_hash(session, value) -> str:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return session.scalar(select(func.lifecycle_json_hash(literal(value, JSONB))))
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _row_hash(session, values: dict[str, object]) -> str:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        arguments = []
        for key, value in values.items():
            arguments.extend((key, _typed_literal(value)))
        return session.scalar(select(func.lifecycle_json_hash(func.jsonb_build_object(*arguments))))
    serializable = {
        key: (
            _utc(value).isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, UUID)
            else canonical_decimal(value)
            if isinstance(value, Decimal)
            else value
        )
        for key, value in values.items()
    }
    return _json_hash(session, serializable)


def _typed_literal(value):
    if value is None:
        return literal(None, String)
    if isinstance(value, datetime):
        return literal(_utc(value), DateTime(timezone=True))
    if isinstance(value, Decimal):
        return literal(value, Numeric(18, 6))
    if isinstance(value, dict | list):
        return literal(value, JSONB)
    return literal(value)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
