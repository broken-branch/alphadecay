from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.contracts.v1 import AccountRole
from backend.app.execution import ExecutionBlocked, FrozenThesisVersion
from backend.app.policy import OpportunityDecisionRecord, OpportunityInput, OpportunityOutcome
from backend.app.services.agent import AgentDecision
from backend.app.services.development_acquisition import (
    DevelopmentRoute,
    DevelopmentRouteAuthority,
)
from backend.app.services.opportunity_selection import GreekUnitConvention

from .agent_authority import agent_input_material, agent_result_material, canonical_agent_hash
from .agent_codec import decode_agent_value
from .sqlalchemy_models import (
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AssessmentCertificateRow,
    CompetitionEntryBudgetRow,
    EntryApprovalCertificateRow,
    ExecutionIntentRow,
    GreekAuthorityVersionRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ThesisVersionRow,
)
from .sqlalchemy_repository import SQLAlchemyExecutionRepository

_HASH = re.compile(r"^[0-9a-f]{64}$")
_OPPORTUNITY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_THESIS_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_UNDERLYING = re.compile(r"^[A-Z]{1,6}$")


class OpportunityAuthorityError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EntryHistoryAuthority:
    account_fingerprint: str
    clean_equity: Decimal
    entries_used: int
    gross_approved_risk: Decimal
    reserved_intent_id: UUID | None
    reserved_risk: Decimal
    event_already_attempted: bool
    entry_intent_count: int
    authority_hash: str


@dataclass(frozen=True)
class PriorOpportunityDecisionAuthority:
    account_fingerprint: str
    opportunity_key: str
    decision_boundary: datetime
    outcome: OpportunityOutcome | None
    reason_code: str | None
    observed_at: datetime
    decision_id: UUID | None
    source_hash: str


@dataclass(frozen=True)
class GreekUnitAuthority:
    authority_id: UUID
    version: int
    effective_at: datetime
    convention: GreekUnitConvention
    evidence_hash: str
    authority_hash: str


class SQLAlchemyOpportunityAuthorityRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        account_role: AccountRole = AccountRole.DEVELOPMENT,
    ) -> None:
        if account_role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION):
            raise ValueError("OPPORTUNITY_ACCOUNT_ROLE_INVALID")
        self._sessions = session_factory
        self._account_role = account_role

    def load_development_route(
        self,
        *,
        account_role: AccountRole = AccountRole.DEVELOPMENT,
        expected_account_fingerprint: str,
    ) -> DevelopmentRouteAuthority:
        if account_role is not self._account_role:
            raise OpportunityAuthorityError("OPPORTUNITY_ACCOUNT_ROLE_MISMATCH")
        _digest(expected_account_fingerprint, "ACCOUNT_FINGERPRINT_INVALID")
        with self._sessions.begin() as session:
            _begin_authority_read(session)
            account = _account(session, self._account_role, expected_account_fingerprint)
            positions = session.scalars(
                select(ManagedLifecyclePositionRow)
                .where(
                    ManagedLifecyclePositionRow.account_role == self._account_role.value,
                    ManagedLifecyclePositionRow.closed_at.is_(None),
                )
                .order_by(ManagedLifecyclePositionRow.managed_position_id)
            ).all()
            if any(
                position.account_fingerprint != account.account_fingerprint
                for position in positions
            ):
                raise OpportunityAuthorityError("MANAGED_POSITION_ACCOUNT_MISMATCH")
            for position in positions:
                _digest(position.active_position_fingerprint, "POSITION_FINGERPRINT_INVALID")

            managed_position_id: UUID | None = None
            position_fingerprint: str | None = None
            if not positions:
                route = DevelopmentRoute.EMPTY
            elif len(positions) > 1:
                route = DevelopmentRoute.AMBIGUOUS
            else:
                route = DevelopmentRoute.MANAGED_POSITION
                position = positions[0]
                if position.current_snapshot_id is None:
                    raise OpportunityAuthorityError("MANAGED_POSITION_SNAPSHOT_MISSING")
                snapshot = session.get(ManagedPositionSnapshotRow, position.current_snapshot_id)
                if (
                    snapshot is None
                    or snapshot.managed_position_id != position.managed_position_id
                    or snapshot.position_fingerprint != position.active_position_fingerprint
                ):
                    raise OpportunityAuthorityError("MANAGED_POSITION_FINGERPRINT_MISMATCH")
                managed_position_id = position.managed_position_id
                position_fingerprint = position.active_position_fingerprint

            material = {
                "domain": "alphadecay.opportunity.route-authority.v1",
                "account_role": self._account_role.value,
                "account_fingerprint": account.account_fingerprint,
                "route": route.value,
                "active_positions": [
                    {
                        "managed_position_id": str(position.managed_position_id),
                        "position_fingerprint": position.active_position_fingerprint,
                        "current_snapshot_id": (
                            str(position.current_snapshot_id)
                            if position.current_snapshot_id is not None
                            else None
                        ),
                    }
                    for position in positions
                ],
            }
            return DevelopmentRouteAuthority(
                account_fingerprint=account.account_fingerprint,
                route=route,
                active_position_count=len(positions),
                managed_position_id=managed_position_id,
                position_fingerprint=position_fingerprint,
                authority_hash=_hash(material),
                account_role=self._account_role,
            )

    def load_entry_history(
        self,
        *,
        expected_account_fingerprint: str,
        event_key: str,
        trading_day: date,
    ) -> EntryHistoryAuthority:
        _digest(expected_account_fingerprint, "ACCOUNT_FINGERPRINT_INVALID")
        if (
            not isinstance(event_key, str)
            or not _OPPORTUNITY_KEY.fullmatch(event_key)
            or type(trading_day) is not date
        ):
            raise OpportunityAuthorityError("ENTRY_HISTORY_SCOPE_INVALID")
        with self._sessions.begin() as session:
            _begin_authority_read(session)
            account = _account(session, self._account_role, expected_account_fingerprint)
            budget = session.get(CompetitionEntryBudgetRow, self._account_role.value)
            if budget is None:
                raise OpportunityAuthorityError("ENTRY_BUDGET_MISSING")
            intents = session.scalars(
                select(ExecutionIntentRow)
                .where(
                    ExecutionIntentRow.account_role == self._account_role.value,
                    ExecutionIntentRow.action == "ENTRY",
                )
                .order_by(
                    ExecutionIntentRow.trading_day,
                    ExecutionIntentRow.event_key,
                    ExecutionIntentRow.intent_id,
                )
            ).all()
            _validate_entry_history(
                session,
                budget=budget,
                intents=intents,
                account_fingerprint=account.account_fingerprint,
                account_role=self._account_role,
            )
            matching = [
                intent
                for intent in intents
                if intent.event_key == event_key and intent.trading_day == trading_day
            ]
            if len(matching) > 1:
                raise OpportunityAuthorityError("ENTRY_EVENT_DAY_AMBIGUOUS")
            material = {
                "domain": "alphadecay.opportunity.entry-history-authority.v1",
                "account_role": self._account_role.value,
                "account_fingerprint": account.account_fingerprint,
                "equity": _decimal(account.equity),
                "budget": {
                    "entries_used": budget.entries_used,
                    "gross_approved_risk": _decimal(budget.gross_approved_risk),
                    "reserved_intent_id": (
                        str(budget.reserved_intent_id)
                        if budget.reserved_intent_id is not None
                        else None
                    ),
                    "reserved_risk": _decimal(budget.reserved_risk),
                },
                "event_key": event_key,
                "trading_day": trading_day.isoformat(),
                "intents": [_entry_intent_material(intent) for intent in intents],
            }
            return EntryHistoryAuthority(
                account_fingerprint=account.account_fingerprint,
                clean_equity=Decimal(account.equity),
                entries_used=budget.entries_used,
                gross_approved_risk=Decimal(budget.gross_approved_risk),
                reserved_intent_id=budget.reserved_intent_id,
                reserved_risk=Decimal(budget.reserved_risk),
                event_already_attempted=bool(matching),
                entry_intent_count=len(intents),
                authority_hash=_hash(material),
            )

    def load_prior_opportunity_decision(
        self,
        *,
        expected_account_fingerprint: str,
        expected_opportunity_key: str,
        decision_boundary: datetime,
        as_of: datetime,
    ) -> PriorOpportunityDecisionAuthority:
        _digest(expected_account_fingerprint, "ACCOUNT_FINGERPRINT_INVALID")
        if not isinstance(expected_opportunity_key, str) or not _OPPORTUNITY_KEY.fullmatch(
            expected_opportunity_key
        ):
            raise OpportunityAuthorityError("OPPORTUNITY_KEY_INVALID")
        boundary = _strict_utc(decision_boundary, "DECISION_BOUNDARY_INVALID")
        read_at = _strict_utc(as_of, "DECISION_READ_TIME_INVALID")
        if boundary > read_at:
            raise OpportunityAuthorityError("DECISION_BOUNDARY_IN_FUTURE")
        with self._sessions.begin() as session:
            _begin_authority_read(session)
            account = _account(session, self._account_role, expected_account_fingerprint)
            snapshots = session.scalars(
                select(AgentInputSnapshotRow).where(
                    AgentInputSnapshotRow.account_role == self._account_role.value,
                    AgentInputSnapshotRow.decision_kind == "OPPORTUNITY",
                    AgentInputSnapshotRow.decision_boundary == boundary,
                )
            ).all()
            if len(snapshots) > 1:
                raise OpportunityAuthorityError("PRIOR_DECISION_BOUNDARY_AMBIGUOUS")
            if not snapshots:
                material = {
                    "domain": "alphadecay.opportunity.prior-decision-authority.v1",
                    "account_role": self._account_role.value,
                    "account_fingerprint": account.account_fingerprint,
                    "opportunity_key": expected_opportunity_key,
                    "decision_boundary": boundary.isoformat(),
                    "observed_at": read_at.isoformat(),
                    "decision": None,
                }
                return PriorOpportunityDecisionAuthority(
                    account_fingerprint=account.account_fingerprint,
                    opportunity_key=expected_opportunity_key,
                    decision_boundary=boundary,
                    outcome=None,
                    reason_code=None,
                    observed_at=read_at,
                    decision_id=None,
                    source_hash=_hash(material),
                )
            snapshot = snapshots[0]
            decisions = session.scalars(
                select(AgentDecisionRow).where(
                    AgentDecisionRow.input_snapshot_id == snapshot.snapshot_id
                )
            ).all()
            if len(decisions) != 1:
                raise OpportunityAuthorityError("PRIOR_DECISION_LINEAGE_INCOMPLETE")
            decision = decisions[0]
            _validate_prior_decision(
                session,
                snapshot=snapshot,
                decision=decision,
                account_fingerprint=account.account_fingerprint,
                account_role=self._account_role,
                opportunity_key=expected_opportunity_key,
                boundary=boundary,
                as_of=read_at,
            )
            try:
                outcome = OpportunityOutcome(decision.outcome)
            except ValueError as error:
                raise OpportunityAuthorityError("PRIOR_DECISION_OUTCOME_INVALID") from error
            material = {
                "domain": "alphadecay.opportunity.prior-decision-authority.v1",
                "account_role": self._account_role.value,
                "account_fingerprint": account.account_fingerprint,
                "opportunity_key": expected_opportunity_key,
                "decision_boundary": boundary.isoformat(),
                "observed_at": read_at.isoformat(),
                "input_hash": snapshot.input_hash,
                "result_hash": decision.result_hash,
            }
            return PriorOpportunityDecisionAuthority(
                account_fingerprint=account.account_fingerprint,
                opportunity_key=expected_opportunity_key,
                decision_boundary=boundary,
                outcome=outcome,
                reason_code=decision.reason_code,
                observed_at=read_at,
                decision_id=decision.decision_id,
                source_hash=_hash(material),
            )

    def load_latest_greek_unit_authority(self, *, effective_at: datetime) -> GreekUnitAuthority:
        boundary = _strict_utc(effective_at, "GREEK_AUTHORITY_TIME_INVALID")
        with self._sessions.begin() as session:
            _begin_authority_read(session)
            rows = session.scalars(
                select(GreekAuthorityVersionRow)
                .where(GreekAuthorityVersionRow.effective_at <= boundary)
                .order_by(GreekAuthorityVersionRow.version)
            ).all()
            if not rows:
                raise OpportunityAuthorityError("GREEK_AUTHORITY_MISSING")
            if [row.version for row in rows] != list(range(1, rows[-1].version + 1)):
                raise OpportunityAuthorityError("GREEK_AUTHORITY_SEQUENCE_INVALID")
            for index, candidate in enumerate(rows):
                _validate_greek_authority(candidate, boundary)
                if index and _utc(rows[index - 1].effective_at) > _utc(candidate.effective_at):
                    raise OpportunityAuthorityError("GREEK_AUTHORITY_SEQUENCE_INVALID")
            row = rows[-1]
            material = {
                "domain": "alphadecay.opportunity.greek-unit-authority.v1",
                "authority_id": str(row.authority_id),
                "version": row.version,
                "effective_at": _utc(row.effective_at).isoformat(),
                "timestamp_contract_hash": row.timestamp_contract_hash,
                "units_contract_hash": row.units_contract_hash,
                "authority_payload": row.authority_payload,
                "stored_authority_hash": row.authority_hash,
                "created_at": _utc(row.created_at).isoformat(),
                "convention": GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1.value,
            }
            return GreekUnitAuthority(
                authority_id=row.authority_id,
                version=row.version,
                effective_at=_utc(row.effective_at),
                convention=GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1,
                evidence_hash=row.units_contract_hash,
                authority_hash=_hash(material),
            )

    def load_frozen_thesis(
        self,
        *,
        thesis_version_id: UUID,
        expected_account_fingerprint: str,
        expected_thesis_hash: str,
        expected_policy_hash: str,
        expected_underlying: str,
        as_of: datetime,
    ) -> FrozenThesisVersion:
        _digest(expected_account_fingerprint, "ACCOUNT_FINGERPRINT_INVALID")
        _digest(expected_thesis_hash, "THESIS_HASH_INVALID")
        _digest(expected_policy_hash, "POLICY_HASH_INVALID")
        read_at = _strict_utc(as_of, "THESIS_READ_TIME_INVALID")
        with self._sessions.begin() as session:
            _begin_authority_read(session)
            _account(session, self._account_role, expected_account_fingerprint)
            row = session.get(ThesisVersionRow, thesis_version_id)
            if row is None:
                raise OpportunityAuthorityError("THESIS_NOT_FOUND")
            if (
                row.account_role != self._account_role.value
                or row.thesis_hash != expected_thesis_hash
                or row.policy_hash != expected_policy_hash
                or row.underlying != expected_underlying
                or _utc(row.frozen_at) > read_at
                or _utc(row.created_at) > read_at
                or _utc(row.target_at) <= _utc(row.frozen_at)
            ):
                raise OpportunityAuthorityError("THESIS_AUTHORITY_MISMATCH")
            _validate_thesis(row, read_at)
            return FrozenThesisVersion(
                thesis_version_id=row.thesis_version_id,
                thesis_id=row.thesis_id,
                account_role=self._account_role,
                version=row.version,
                thesis_hash=row.thesis_hash,
                policy_hash=row.policy_hash,
                underlying=row.underlying,
                thesis_code=row.thesis_code,
                frozen_at=_utc(row.frozen_at),
                target_at=_utc(row.target_at),
                intended_exposure=dict(row.intended_exposure),
                exposure_limits=dict(row.exposure_limits),
                volatility_view=row.volatility_view,
                entry_atm_iv=Decimal(row.entry_atm_iv),
                approved_max_loss=Decimal(row.approved_max_loss),
                portfolio_risk_cap=Decimal(row.portfolio_risk_cap),
                invalidation_codes=tuple(row.invalidation_codes),
                thesis_payload=dict(row.thesis_payload),
                created_at=_utc(row.created_at),
                origin_hash=row.origin_hash,
            )


def _account(
    session: Session,
    account_role: AccountRole,
    expected_fingerprint: str,
) -> AccountRoleRow:
    account = session.get(AccountRoleRow, account_role.value)
    if account is None or account.account_fingerprint != expected_fingerprint:
        code = (
            "DEVELOPMENT_ACCOUNT_MISMATCH"
            if account_role is AccountRole.DEVELOPMENT
            else "OPPORTUNITY_ACCOUNT_MISMATCH"
        )
        raise OpportunityAuthorityError(code)
    if not Decimal(account.equity).is_finite() or Decimal(account.equity) <= 0:
        code = (
            "DEVELOPMENT_ACCOUNT_EQUITY_INVALID"
            if account_role is AccountRole.DEVELOPMENT
            else "OPPORTUNITY_ACCOUNT_EQUITY_INVALID"
        )
        raise OpportunityAuthorityError(code)
    return account


def _validate_entry_history(
    session: Session,
    *,
    budget: CompetitionEntryBudgetRow,
    intents: list[ExecutionIntentRow],
    account_fingerprint: str,
    account_role: AccountRole,
) -> None:
    for intent in intents:
        try:
            loaded = SQLAlchemyExecutionRepository._intent_from_row(session, intent)
        except (ExecutionBlocked, KeyError, TypeError, ValueError) as error:
            raise OpportunityAuthorityError("ENTRY_HISTORY_INVALID") from error
        approval = session.get(EntryApprovalCertificateRow, intent.entry_approval_id)
        if (
            loaded.account_role is not account_role
            or loaded.envelope.account_fingerprint != account_fingerprint
            or approval is None
            or approval.account_role != account_role.value
            or approval.policy_hash != intent.policy_hash
            or approval.book_fingerprint != intent.fingerprint
            or approval.envelope_hash != intent.envelope_hash
            or Decimal(approval.approved_max_loss) != Decimal(intent.approved_max_loss)
            or approval.quantity != intent.quantity
            or (intent.first_fill_consumed and intent.state != "TERMINAL")
        ):
            raise OpportunityAuthorityError("ENTRY_HISTORY_INVALID")
    consumed = [intent for intent in intents if intent.first_fill_consumed]
    if budget.entries_used != len(consumed) or Decimal(budget.gross_approved_risk) != sum(
        (Decimal(intent.approved_max_loss) for intent in consumed), Decimal(0)
    ):
        raise OpportunityAuthorityError("ENTRY_BUDGET_HISTORY_MISMATCH")
    if budget.reserved_intent_id is None:
        if Decimal(budget.reserved_risk) != 0 or any(
            intent.state == "CLAIMED" for intent in intents
        ):
            raise OpportunityAuthorityError("ENTRY_RESERVATION_MISMATCH")
        return
    reserved = [intent for intent in intents if intent.intent_id == budget.reserved_intent_id]
    if (
        len(reserved) != 1
        or reserved[0].state != "CLAIMED"
        or reserved[0].first_fill_consumed
        or Decimal(reserved[0].approved_max_loss) != Decimal(budget.reserved_risk)
        or sum(intent.state == "CLAIMED" for intent in intents) != 1
    ):
        raise OpportunityAuthorityError("ENTRY_RESERVATION_MISMATCH")


def _entry_intent_material(intent: ExecutionIntentRow) -> dict[str, object]:
    return {
        "intent_id": str(intent.intent_id),
        "intent_digest": intent.intent_digest,
        "policy_hash": intent.policy_hash,
        "event_key": intent.event_key,
        "trading_day": intent.trading_day.isoformat(),
        "fingerprint": intent.fingerprint,
        "envelope_hash": intent.envelope_hash,
        "approved_max_loss": _decimal(intent.approved_max_loss),
        "state": intent.state,
        "first_fill_consumed": intent.first_fill_consumed,
    }


def _validate_prior_decision(
    session: Session,
    *,
    snapshot: AgentInputSnapshotRow,
    decision: AgentDecisionRow,
    account_fingerprint: str,
    account_role: AccountRole,
    opportunity_key: str,
    boundary: datetime,
    as_of: datetime,
) -> None:
    observed_at = _utc(snapshot.observed_at)
    if not isinstance(snapshot.normalized_payload, dict) or not isinstance(
        decision.result_payload, dict
    ):
        raise OpportunityAuthorityError("PRIOR_DECISION_AUTHORITY_MISMATCH")
    typed_input, typed_decision = _decode_prior_decision_payloads(
        snapshot.normalized_payload,
        decision.result_payload,
    )
    payload_opportunity_key = (
        typed_input.opportunity_key
        if typed_input is not None
        else snapshot.normalized_payload.get("opportunity_key")
    )
    if (
        snapshot.account_fingerprint != account_fingerprint
        or decision.account_fingerprint != account_fingerprint
        or decision.account_role != account_role.value
        or decision.decision_kind != "OPPORTUNITY"
        or _utc(decision.decision_boundary) != boundary
        or observed_at > as_of
        or _utc(snapshot.created_at) > as_of
        or _utc(decision.created_at) > as_of
        or snapshot.thesis_version_id != decision.thesis_version_id
        or payload_opportunity_key != opportunity_key
        or not _REASON_CODE.fullmatch(decision.reason_code)
        or not _HASH.fullmatch(decision.policy_hash)
    ):
        raise OpportunityAuthorityError("PRIOR_DECISION_AUTHORITY_MISMATCH")
    if typed_decision is not None:
        record = typed_decision.opportunity
        if (
            typed_input is None
            or not isinstance(record, OpportunityDecisionRecord)
            or typed_decision.normalized_input != typed_input
            or typed_decision.thesis_version_id != decision.thesis_version_id
            or typed_decision.decided_at != observed_at
            or typed_decision.code != decision.outcome
            or typed_decision.calibration is not None
            or typed_decision.lifecycle is not None
            or typed_decision.provider_failure_code is not None
            or typed_decision.provider_failure_kind is not None
            or record.outcome.value != decision.outcome
            or not record.reason_codes
            or record.reason_codes[0].value != decision.reason_code
            or record.opportunity_key != opportunity_key
            or record.decision_boundary != boundary
            or record.policy_hash != decision.policy_hash
        ):
            raise OpportunityAuthorityError("PRIOR_DECISION_PAYLOAD_MISMATCH")
    input_hash = canonical_agent_hash(
        agent_input_material(
            account_role=account_role.value,
            account_fingerprint=account_fingerprint,
            decision_kind="OPPORTUNITY",
            decision_boundary=boundary,
            observed_at=observed_at,
            normalized_input=snapshot.normalized_payload,
            thesis_version_id=snapshot.thesis_version_id,
        )
    )
    if snapshot.input_hash != input_hash:
        raise OpportunityAuthorityError("PRIOR_DECISION_INPUT_HASH_MISMATCH")
    approvals = session.scalars(
        select(EntryApprovalCertificateRow).where(
            EntryApprovalCertificateRow.agent_decision_id == decision.decision_id
        )
    ).all()
    assessments = session.scalars(
        select(AssessmentCertificateRow).where(
            AssessmentCertificateRow.agent_decision_id == decision.decision_id
        )
    ).all()
    if len(approvals) > 1 or assessments:
        raise OpportunityAuthorityError("PRIOR_DECISION_AUTHORIZATION_AMBIGUOUS")
    authorization_id: UUID | None = None
    intent_id: UUID | None = None
    intent_digest: str | None = None
    if approvals:
        approval = approvals[0]
        if (
            approval.account_role != account_role.value
            or approval.policy_hash != decision.policy_hash
            or approval.thesis_version_id != decision.thesis_version_id
        ):
            raise OpportunityAuthorityError("PRIOR_DECISION_AUTHORIZATION_MISMATCH")
        authorization_id = approval.approval_id
        intents = session.scalars(
            select(ExecutionIntentRow).where(
                ExecutionIntentRow.entry_approval_id == authorization_id
            )
        ).all()
        if len(intents) != 1:
            raise OpportunityAuthorityError("PRIOR_DECISION_INTENT_MISSING")
        intent = intents[0]
        try:
            loaded = SQLAlchemyExecutionRepository._intent_from_row(session, intent)
        except (ExecutionBlocked, KeyError, TypeError, ValueError) as error:
            raise OpportunityAuthorityError("PRIOR_DECISION_INTENT_INVALID") from error
        if (
            loaded.envelope.account_fingerprint != account_fingerprint
            or approval.book_fingerprint != intent.fingerprint
            or approval.envelope_hash != intent.envelope_hash
            or Decimal(approval.approved_max_loss) != Decimal(intent.approved_max_loss)
            or approval.quantity != intent.quantity
        ):
            raise OpportunityAuthorityError("PRIOR_DECISION_INTENT_INVALID")
        intent_id = intent.intent_id
        intent_digest = intent.intent_digest
    if decision.autonomy_authorized != bool(approvals):
        raise OpportunityAuthorityError("PRIOR_DECISION_AUTHORIZATION_MISMATCH")
    result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash=input_hash,
            outcome=decision.outcome,
            reason_code=decision.reason_code,
            policy_hash=decision.policy_hash,
            thesis_version_id=decision.thesis_version_id,
            result_payload=decision.result_payload,
            authorization_id=authorization_id,
            intent_id=intent_id,
            intent_digest=intent_digest,
            autonomy_authorized=decision.autonomy_authorized,
        )
    )
    if decision.result_hash != result_hash:
        raise OpportunityAuthorityError("PRIOR_DECISION_RESULT_HASH_MISMATCH")


def _decode_prior_decision_payloads(
    normalized_payload: object,
    result_payload: object,
) -> tuple[OpportunityInput | None, AgentDecision | None]:
    if not isinstance(normalized_payload, dict) or not isinstance(result_payload, dict):
        return None, None
    normalized_typed = normalized_payload.get("typed")
    result_typed = result_payload.get("typed")
    if normalized_typed is None and result_typed is None:
        return None, None
    if not isinstance(normalized_typed, dict) or not isinstance(result_typed, dict):
        raise OpportunityAuthorityError("PRIOR_DECISION_PAYLOAD_MISMATCH")
    try:
        decoded_input = decode_agent_value(normalized_typed)
        decoded_decision = decode_agent_value(result_typed)
    except (TypeError, ValueError) as error:
        raise OpportunityAuthorityError("PRIOR_DECISION_PAYLOAD_MISMATCH") from error
    if not isinstance(decoded_input, OpportunityInput) or not isinstance(
        decoded_decision, AgentDecision
    ):
        raise OpportunityAuthorityError("PRIOR_DECISION_PAYLOAD_MISMATCH")
    return decoded_input, decoded_decision


def _validate_greek_authority(row: GreekAuthorityVersionRow, boundary: datetime) -> None:
    if (
        row.version <= 0
        or _utc(row.effective_at) > boundary
        or _utc(row.created_at) > boundary
        or _utc(row.created_at) < _utc(row.effective_at)
        or not isinstance(row.authority_payload, dict)
    ):
        raise OpportunityAuthorityError("GREEK_AUTHORITY_INVALID")
    for value in (
        row.timestamp_contract_hash,
        row.units_contract_hash,
        row.authority_hash,
    ):
        _digest(value, "GREEK_AUTHORITY_HASH_INVALID")


def _validate_thesis(row: ThesisVersionRow, as_of: datetime) -> None:
    frozen_at = _utc(row.frozen_at)
    created_at = _utc(row.created_at)
    decimals = (
        Decimal(row.entry_atm_iv),
        Decimal(row.approved_max_loss),
        Decimal(row.portfolio_risk_cap),
    )
    if (
        row.version <= 0
        or not _HASH.fullmatch(row.thesis_hash)
        or not _HASH.fullmatch(row.policy_hash)
        or not _UNDERLYING.fullmatch(row.underlying)
        or not _THESIS_CODE.fullmatch(row.thesis_code)
        or row.volatility_view not in {"LONG", "SHORT", "NEUTRAL"}
        or any(not value.is_finite() or value <= 0 for value in decimals)
        or Decimal(row.approved_max_loss) > Decimal(row.portfolio_risk_cap)
        or created_at < frozen_at
        or created_at > as_of
        or not isinstance(row.intended_exposure, dict)
        or not isinstance(row.exposure_limits, dict)
        or not isinstance(row.thesis_payload, dict)
        or not isinstance(row.invalidation_codes, list)
        or not 1 <= len(row.invalidation_codes) <= 32
        or any(
            not isinstance(item, str) or not _REASON_CODE.fullmatch(item)
            for item in row.invalidation_codes
        )
    ):
        raise OpportunityAuthorityError("THESIS_PAYLOAD_INVALID")
    _validate_json(row.intended_exposure, "THESIS_PAYLOAD_INVALID")
    _validate_json(row.exposure_limits, "THESIS_PAYLOAD_INVALID")
    _validate_json(row.thesis_payload, "THESIS_PAYLOAD_INVALID")
    _validate_json(row.invalidation_codes, "THESIS_PAYLOAD_INVALID")


def _begin_authority_read(session: Session) -> None:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


def _strict_utc(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OpportunityAuthorityError(code)
    normalized = value.astimezone(UTC)
    if normalized != value or value.utcoffset().total_seconds() != 0:
        raise OpportunityAuthorityError(code)
    return normalized


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(value: str, code: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise OpportunityAuthorityError(code)


def _decimal(value: Decimal) -> str:
    decimal = Decimal(value)
    if not decimal.is_finite():
        raise OpportunityAuthorityError("AUTHORITY_DECIMAL_INVALID")
    return format(decimal, "f")


def _validate_json(value: object, code: str) -> None:
    try:
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise OpportunityAuthorityError(code) from error


def _hash(value: object) -> str:
    _validate_json(value, "AUTHORITY_PAYLOAD_INVALID")
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
