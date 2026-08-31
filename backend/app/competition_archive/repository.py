from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.domain.option_contract_symbol import (
    NON_STANDARD_CONTRACT_UNSUPPORTED,
    OptionContractSymbol,
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)
from backend.app.persistence.agent_authority import (
    agent_input_material,
    agent_result_material,
    canonical_agent_hash,
)
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AssessmentCertificateRow,
    CompetitionRecordPublicationRow,
    ExecutionCertificateRow,
    ExecutionIntentRow,
    LifecycleObservationBindingRow,
    LifecycleObservationManifestRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
    SubmissionBaselineRow,
    ThesisVersionRow,
)

from .models import (
    AssessmentEventProjection,
    CompetitionRecord,
    CompetitionRecordKind,
    ExecutionEventProjection,
    ExposureProjection,
    NoTradeProjection,
    PositionDirection,
    PositionProjection,
    PositionState,
    PublicAssessmentAction,
    PublicAssessmentReason,
    SpreadProjection,
    ThesisProjection,
    canonical_hash,
    canonical_json,
    canonical_value,
    utc_text,
    validate_projection,
)

_HASH_DOMAIN = "alphadecay.competition-record-publication.v1"
_SOURCE_DOMAIN = "alphadecay.competition-record-source.v1"
_PUBLIC_ID_DOMAIN = "alphadecay.competition-record-public-id.v1"
_PUBLICATION_LOCK_ID = 6_748_832_221
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ASSESSMENT_CATEGORIES = {
    "HOLD_CERTIFIED": (
        PublicAssessmentAction.HOLD,
        PublicAssessmentReason.POSITION_REVIEWED,
    ),
    "CLOSE_RISK_ONLY": (
        PublicAssessmentAction.CLOSE,
        PublicAssessmentReason.RISK_REDUCTION,
    ),
    "CLOSE_APPROVED": (
        PublicAssessmentAction.CLOSE,
        PublicAssessmentReason.THESIS_CHANGED,
    ),
    "ROLL_APPROVED": (
        PublicAssessmentAction.ROLL,
        PublicAssessmentReason.POSITION_ADJUSTMENT,
    ),
    "NO_ACTION": (
        PublicAssessmentAction.NO_ACTION,
        PublicAssessmentReason.DATA_INCOMPLETE,
    ),
}


class CompetitionRecordNotEligible(RuntimeError):
    pass


class CompetitionArchiveIntegrityError(RuntimeError):
    pass


class EmptyCompetitionArchiveReader:
    def records(self) -> tuple[CompetitionRecord, ...]:
        return ()


class UnavailableCompetitionArchiveReader:
    def records(self) -> tuple[CompetitionRecord, ...]:
        raise RuntimeError("competition record database is unavailable")


class SQLAlchemyCompetitionArchiveRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def publish_eligible(self) -> tuple[CompetitionRecord, ...]:
        with self._sessions() as session:
            candidates = [
                (_utc(boundary), CompetitionRecordKind.NO_TRADE, decision_id)
                for decision_id, boundary in session.execute(
                    select(AgentDecisionRow.decision_id, AgentDecisionRow.decision_boundary).where(
                        AgentDecisionRow.account_role == "SUBMISSION",
                        AgentDecisionRow.decision_kind == "OPPORTUNITY",
                        AgentDecisionRow.outcome == "NO_TRADE",
                        AgentDecisionRow.reason_code == "CALIBRATION_BINDING_NO_TRADE",
                        AgentDecisionRow.autonomy_authorized.is_(False),
                        AgentDecisionRow.thesis_version_id.is_(None),
                    )
                )
            ]
            candidates.extend(
                (_utc(activated_at), CompetitionRecordKind.POSITION, position_id)
                for position_id, activated_at in session.execute(
                    select(
                        ManagedLifecyclePositionRow.managed_position_id,
                        ManagedLifecyclePositionRow.activated_at,
                    ).where(ManagedLifecyclePositionRow.account_role == "SUBMISSION")
                )
            )

        for _occurred_at, kind, source_id in sorted(
            candidates, key=lambda item: (item[0], item[1].value, str(item[2]))
        ):
            if kind is CompetitionRecordKind.NO_TRADE:
                self.publish_no_trade(source_id)
            else:
                self.publish_position(source_id)
        return self.records()

    def publish_no_trade(self, decision_id: UUID) -> CompetitionRecord:
        try:
            with self._sessions.begin() as session:
                _lock_lane(session)
                decision = session.get(AgentDecisionRow, decision_id)
                if decision is None:
                    raise CompetitionRecordNotEligible(
                        "competition no-trade decision was not found"
                    )
                input_snapshot = session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
                _validate_no_trade_authority(decision, input_snapshot)
                _validate_decision_hashes(session, decision, input_snapshot)
                baseline = _submission_baseline(session, decision.account_fingerprint)
                assert input_snapshot is not None

                source_hash = _source_hash(
                    CompetitionRecordKind.NO_TRADE,
                    baseline,
                    decision_hashes=(decision.result_hash,),
                )
                existing = _by_source_hash(session, source_hash)
                if existing is not None:
                    return _record_from_row(existing, session)
                predecessor = _latest_verified_row(session)
                published_at = _next_publication_time(session, predecessor)
                public_id = _public_id(CompetitionRecordKind.NO_TRADE, decision.result_hash)
                projection = NoTradeProjection(
                    public_record_id=public_id,
                    decided_at=_utc(decision.decision_boundary),
                    observed_at=_utc(input_snapshot.observed_at),
                )
                record = _build_record(
                    projection=projection,
                    occurred_at=_utc(decision.decision_boundary),
                    published_at=published_at,
                    predecessor_hash=(
                        None if predecessor is None else predecessor.publication_hash
                    ),
                    source_authority_hash=source_hash,
                )
                session.add(
                    _publication_row(
                        record,
                        source_authority_hash=source_hash,
                        source_decision_id=decision.decision_id,
                    )
                )
                return record
        except IntegrityError as exc:
            with self._sessions() as session:
                row = session.scalar(
                    select(CompetitionRecordPublicationRow).where(
                        CompetitionRecordPublicationRow.source_decision_id == decision_id
                    )
                )
                if row is not None:
                    return _record_from_row(row, session)
            raise CompetitionArchiveIntegrityError(
                "competition no-trade publication conflicted"
            ) from exc

    def publish_position(self, managed_position_id: UUID) -> CompetitionRecord:
        try:
            with self._sessions.begin() as session:
                _lock_lane(session)
                position = session.get(ManagedLifecyclePositionRow, managed_position_id)
                if position is None or position.account_role != "SUBMISSION":
                    raise CompetitionRecordNotEligible("submission position was not found")
                baseline = _submission_baseline(session, position.account_fingerprint)
                thesis = session.get(ThesisVersionRow, position.thesis_version_id)
                transitions = tuple(
                    session.scalars(
                        select(ManagedPositionTransitionRow)
                        .where(
                            ManagedPositionTransitionRow.managed_position_id == managed_position_id
                        )
                        .order_by(ManagedPositionTransitionRow.transition_sequence)
                    )
                )
                snapshots = tuple(
                    session.scalars(
                        select(ManagedPositionSnapshotRow)
                        .where(
                            ManagedPositionSnapshotRow.managed_position_id == managed_position_id
                        )
                        .order_by(ManagedPositionSnapshotRow.accepted_at)
                    )
                )
                _validate_position_authority(position, thesis, transitions, snapshots)
                assert thesis is not None
                snapshot_by_transition = {item.transition_id: item for item in snapshots}
                execution_events = tuple(
                    _transition_event(
                        session,
                        transition,
                        snapshot_by_transition,
                        underlying=thesis.underlying,
                    )
                    for transition in transitions
                )
                assessment_events, assessment_hashes = _assessment_events(session, position, thesis)
                source_hash = _source_hash(
                    CompetitionRecordKind.POSITION,
                    baseline,
                    position_hashes=(
                        thesis.thesis_hash,
                        *(item.transition_hash for item in transitions),
                        *(item.snapshot_hash for item in snapshots),
                        *assessment_hashes,
                        snapshots[-1].position_fingerprint,
                    ),
                    closed_at=position.closed_at,
                )
                existing = _by_source_hash(session, source_hash)
                if existing is not None:
                    return _record_from_row(existing, session)

                events = sorted(
                    (*execution_events, *assessment_events),
                    key=lambda item: (
                        item.occurred_at,
                        0 if isinstance(item, ExecutionEventProjection) else 1,
                        item.action,
                    ),
                )
                predecessor = _latest_verified_row(session)
                published_at = _next_publication_time(session, predecessor)
                public_id = _public_id(
                    CompetitionRecordKind.POSITION, transitions[0].transition_hash
                )
                latest_transition = transitions[-1]
                latest_certificate = _execution_certificate(session, latest_transition)
                opening_spread = execution_events[0].spread_after
                if opening_spread is None:
                    raise CompetitionRecordNotEligible("position opening spread is unavailable")
                current_spread = execution_events[-1].spread_after
                state = (
                    PositionState.CLOSED
                    if latest_transition.action == "CLOSE"
                    else PositionState.OPEN
                )
                as_of = max(
                    _utc(snapshots[-1].accepted_at),
                    _utc(latest_certificate.created_at),
                    *(event.occurred_at for event in events),
                )
                projection = PositionProjection(
                    public_record_id=public_id,
                    state=state,
                    underlying=thesis.underlying,
                    opening_spread=opening_spread,
                    current_spread=current_spread,
                    opened_at=_utc(position.activated_at),
                    as_of=_utc(as_of),
                    closed_at=None if position.closed_at is None else _utc(position.closed_at),
                    thesis=ThesisProjection(
                        direction=_direction(thesis.intended_exposure),
                        volatility_view=thesis.volatility_view,
                        target_at=_utc(thesis.target_at),
                    ),
                    events=tuple(events),
                    current_exposure=_exposure(latest_certificate.actual_exposure),
                    execution_status=latest_certificate.execution_status,
                )
                record = _build_record(
                    projection=projection,
                    occurred_at=_utc(position.activated_at),
                    published_at=published_at,
                    predecessor_hash=(
                        None if predecessor is None else predecessor.publication_hash
                    ),
                    source_authority_hash=source_hash,
                )
                session.add(
                    _publication_row(
                        record,
                        source_authority_hash=source_hash,
                        source_managed_position_id=position.managed_position_id,
                    )
                )
                return record
        except IntegrityError as exc:
            with self._sessions() as session:
                rows = tuple(
                    session.scalars(
                        select(CompetitionRecordPublicationRow)
                        .where(
                            CompetitionRecordPublicationRow.source_managed_position_id
                            == managed_position_id
                        )
                        .order_by(CompetitionRecordPublicationRow.published_at.desc())
                    )
                )
                if rows:
                    return _record_from_row(rows[0], session)
            raise CompetitionArchiveIntegrityError(
                "competition position publication conflicted"
            ) from exc

    def records(self) -> tuple[CompetitionRecord, ...]:
        with self._sessions() as session:
            rows = tuple(
                session.scalars(
                    select(CompetitionRecordPublicationRow).order_by(
                        CompetitionRecordPublicationRow.published_at,
                        CompetitionRecordPublicationRow.publication_id,
                    )
                )
            )
            _verify_chain(rows, session)
            return tuple(_record_from_row(row, session) for row in rows)


def _submission_baseline(session: Session, account_fingerprint: str) -> SubmissionBaselineRow:
    accounts = tuple(
        session.scalars(select(AccountRoleRow).where(AccountRoleRow.role == "SUBMISSION"))
    )
    baselines = tuple(
        session.scalars(
            select(SubmissionBaselineRow).where(SubmissionBaselineRow.account_role == "SUBMISSION")
        )
    )
    if len(accounts) != 1 or len(baselines) != 1:
        raise CompetitionRecordNotEligible("one sealed submission baseline is required")
    account, baseline = accounts[0], baselines[0]
    valid = (
        account.account_fingerprint == account_fingerprint
        and baseline.account_fingerprint == account.account_fingerprint
        and baseline.equity == Decimal("100000")
        and baseline.contaminated is False
        and all(
            _HASH.fullmatch(value)
            for value in (
                account.account_fingerprint,
                baseline.positions_hash,
                baseline.orders_hash,
                baseline.activities_hash,
            )
        )
    )
    if not valid:
        raise CompetitionRecordNotEligible("submission baseline is not clean and current")
    return baseline


def _validate_no_trade_authority(
    decision: AgentDecisionRow, input_snapshot: AgentInputSnapshotRow | None
) -> None:
    valid = (
        input_snapshot is not None
        and decision.account_role == "SUBMISSION"
        and input_snapshot.account_role == "SUBMISSION"
        and decision.account_fingerprint == input_snapshot.account_fingerprint
        and decision.decision_kind == "OPPORTUNITY"
        and input_snapshot.decision_kind == "OPPORTUNITY"
        and _utc(decision.decision_boundary) == _utc(input_snapshot.decision_boundary)
        and decision.outcome == "NO_TRADE"
        and decision.reason_code == "CALIBRATION_BINDING_NO_TRADE"
        and decision.autonomy_authorized is False
        and decision.thesis_version_id is None
        and input_snapshot.thesis_version_id is None
    )
    if not valid:
        raise CompetitionRecordNotEligible("decision is not a publishable submission no-trade")


def _validate_decision_hashes(
    session: Session,
    decision: AgentDecisionRow,
    snapshot: AgentInputSnapshotRow | None,
) -> None:
    if snapshot is None:
        raise CompetitionRecordNotEligible("decision input authority is unavailable")
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
    authorization_id: UUID | None = None
    intent_id: UUID | None = None
    intent_digest: str | None = None
    certificate = session.scalar(
        select(AssessmentCertificateRow).where(
            AssessmentCertificateRow.agent_decision_id == decision.decision_id
        )
    )
    if certificate is not None:
        authorization_id = certificate.certificate_id
        intent = session.scalar(
            select(ExecutionIntentRow).where(
                ExecutionIntentRow.assessment_certificate_id == certificate.certificate_id
            )
        )
        if intent is not None:
            intent_id = intent.intent_id
            intent_digest = intent.intent_digest
    expected_result = canonical_agent_hash(
        agent_result_material(
            input_hash=expected_input,
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
    if snapshot.input_hash != expected_input or decision.result_hash != expected_result:
        raise CompetitionRecordNotEligible("decision hash authority is inconsistent")


def _validate_position_authority(
    position: ManagedLifecyclePositionRow,
    thesis: ThesisVersionRow | None,
    transitions: Sequence[ManagedPositionTransitionRow],
    snapshots: Sequence[ManagedPositionSnapshotRow],
) -> None:
    valid = (
        thesis is not None
        and thesis.account_role == "SUBMISSION"
        and thesis.thesis_version_id == position.thesis_version_id
        and thesis.underlying.isupper()
        and len(transitions) > 0
        and transitions[0].transition_sequence == 0
        and transitions[0].action == "ENTRY"
        and all(item.transition_sequence == index for index, item in enumerate(transitions))
        and len(snapshots) == len(transitions)
        and {item.transition_id for item in snapshots}
        == {item.transition_id for item in transitions}
        and snapshots[-1].snapshot_id == position.current_snapshot_id
        and snapshots[-1].position_fingerprint == position.active_position_fingerprint
        and _utc(position.activated_at) == _utc(transitions[0].occurred_at)
        and (
            (position.closed_at is None and transitions[-1].action != "CLOSE")
            or (
                position.closed_at is not None
                and transitions[-1].action == "CLOSE"
                and _utc(position.closed_at) == _utc(transitions[-1].occurred_at)
            )
        )
    )
    if not valid:
        raise CompetitionRecordNotEligible("position lifecycle authority is incomplete")


def _transition_event(
    session: Session,
    transition: ManagedPositionTransitionRow,
    snapshot_by_transition: dict[UUID, ManagedPositionSnapshotRow],
    *,
    underlying: str,
) -> ExecutionEventProjection:
    snapshot = snapshot_by_transition.get(transition.transition_id)
    if snapshot is None:
        raise CompetitionRecordNotEligible("position transition snapshot is unavailable")
    intent = _intent_for_transition(session, transition)
    certificate = _execution_certificate(session, transition)
    if intent.action != transition.action or certificate.execution_status != "FILLED":
        raise CompetitionRecordNotEligible("position execution authority is inconsistent")
    inventory = _inventory(snapshot.normalized_inventory)
    prior_inventory: tuple[tuple[str, int], ...] = ()
    if transition.predecessor_transition_id is not None:
        predecessor_snapshot = snapshot_by_transition.get(transition.predecessor_transition_id)
        if predecessor_snapshot is None:
            raise CompetitionRecordNotEligible("position predecessor snapshot is unavailable")
        prior_inventory = _inventory(predecessor_snapshot.normalized_inventory)
    if transition.action == "ENTRY":
        _validate_intent_vertical(intent, inventory, (), opening=True, underlying=underlying)
        category = "POSITION_OPENED"
        state = PositionState.OPEN
        spread = _spread_from_inventory(inventory, underlying=underlying)
    elif transition.action == "ROLL":
        _validate_intent_vertical(
            intent,
            inventory,
            prior_inventory,
            opening=True,
            underlying=underlying,
        )
        category = "POSITION_ROLLED"
        state = PositionState.OPEN
        spread = _spread_from_inventory(inventory, underlying=underlying)
    else:
        _validate_intent_vertical(
            intent,
            (),
            prior_inventory,
            opening=False,
            underlying=underlying,
        )
        if inventory:
            raise CompetitionRecordNotEligible("closed position retains option inventory")
        category = "POSITION_CLOSED"
        state = PositionState.CLOSED
        spread = None
    return ExecutionEventProjection(
        action=transition.action,
        occurred_at=_utc(transition.occurred_at),
        reason_category=category,
        cashflow_usd=transition.cashflow_contribution,
        execution_status="FILLED",
        resulting_state=state,
        spread_after=spread,
    )


def _assessment_events(
    session: Session,
    position: ManagedLifecyclePositionRow,
    thesis: ThesisVersionRow,
    *,
    through: datetime | None = None,
) -> tuple[tuple[AssessmentEventProjection, ...], tuple[str, ...]]:
    query = (
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
    if through is not None:
        query = query.where(AgentDecisionRow.decision_boundary <= through)
    rows = tuple(session.scalars(query))
    events: list[AssessmentEventProjection] = []
    hashes: list[str] = []
    for decision in rows:
        snapshot = session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
        _validate_decision_hashes(session, decision, snapshot)
        category = _ASSESSMENT_CATEGORIES.get(decision.outcome)
        if category is None:
            raise CompetitionRecordNotEligible("assessment outcome has no public category")
        action, reason = category
        events.append(
            AssessmentEventProjection(
                action=action,
                occurred_at=_utc(decision.decision_boundary),
                reason_category=reason,
            )
        )
        hashes.append(decision.result_hash)
    return tuple(events), tuple(hashes)


def _intent_for_transition(
    session: Session, transition: ManagedPositionTransitionRow
) -> ExecutionIntentRow:
    intent = session.get(ExecutionIntentRow, transition.execution_intent_id)
    if intent is None or intent.account_role != "SUBMISSION" or intent.state != "TERMINAL":
        raise CompetitionRecordNotEligible("position intent is unavailable")
    return intent


def _execution_certificate(
    session: Session, transition: ManagedPositionTransitionRow
) -> ExecutionCertificateRow:
    certificate = session.get(ExecutionCertificateRow, transition.execution_certificate_id)
    if (
        certificate is None
        or certificate.execution_intent_id != transition.execution_intent_id
        or certificate.execution_status != "FILLED"
    ):
        raise CompetitionRecordNotEligible("position execution certificate is unavailable")
    return certificate


def _inventory(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, list):
        raise CompetitionRecordNotEligible("position inventory is unavailable")
    output: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "kind",
            "symbol",
            "signed_quantity",
            "multiplier",
        }:
            raise CompetitionRecordNotEligible("position inventory is unavailable")
        symbol = item.get("symbol")
        try:
            quantity = int(str(item.get("signed_quantity")))
            multiplier = Decimal(str(item.get("multiplier")))
        except (ValueError, ArithmeticError):
            raise CompetitionRecordNotEligible("position inventory is unavailable") from None
        if (
            item.get("kind") != "OPTION"
            or not isinstance(symbol, str)
            or _standard_contract(symbol) is None
            or quantity == 0
            or multiplier != 100
        ):
            raise CompetitionRecordNotEligible("position inventory is unavailable")
        output.append((symbol, quantity))
    return tuple(sorted(output))


def _validate_intent_vertical(
    intent: ExecutionIntentRow,
    resulting_inventory: Sequence[tuple[str, int]],
    prior_inventory: Sequence[tuple[str, int]],
    *,
    opening: bool,
    underlying: str,
) -> None:
    if (
        not isinstance(intent.legs, list)
        or not all(isinstance(leg, dict) for leg in intent.legs)
        or intent.quantity <= 0
    ):
        raise CompetitionRecordNotEligible("position option structure is unavailable")
    allowed = {"BUY_TO_OPEN", "SELL_TO_OPEN"} if opening else {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}
    candidate_legs = [leg for leg in intent.legs if leg.get("intent") in allowed]
    if len(candidate_legs) != 2:
        raise CompetitionRecordNotEligible("position is not a two-leg vertical")
    expected: list[tuple[str, int]] = []
    sides: set[str] = set()
    for leg in candidate_legs:
        if not isinstance(leg, dict) or not {"symbol", "intent", "ratio"}.issubset(leg):
            raise CompetitionRecordNotEligible("position option structure is unavailable")
        symbol = leg["symbol"]
        side = leg["intent"]
        try:
            ratio = int(str(leg["ratio"]))
        except ValueError:
            raise CompetitionRecordNotEligible("position option ratio is unavailable") from None
        if not isinstance(symbol, str) or _standard_contract(symbol) is None or ratio != 1:
            raise CompetitionRecordNotEligible("position option ratio is unavailable")
        sides.add(str(side))
        if opening:
            signed = intent.quantity if side == "BUY_TO_OPEN" else -intent.quantity
            expected.append((symbol, signed))
    if len(sides) != 2:
        raise CompetitionRecordNotEligible("position option sides are not opposing")
    spread = _spread_from_symbols(
        [
            (str(leg["symbol"]), 1 if str(leg["intent"]).startswith("BUY") else -1)
            for leg in candidate_legs
        ],
        intent.quantity,
        underlying=underlying,
    )
    if opening and tuple(sorted(expected)) != tuple(sorted(resulting_inventory)):
        raise CompetitionRecordNotEligible("position inventory does not match its vertical")
    if spread.quantity != intent.quantity:
        raise CompetitionRecordNotEligible("position quantity is inconsistent")
    if intent.action in {"ROLL", "CLOSE"}:
        closing = [
            leg for leg in intent.legs if leg.get("intent") in {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}
        ]
        if len(closing) != 2:
            raise CompetitionRecordNotEligible("position closing vertical is unavailable")
        expected_prior: list[tuple[str, int]] = []
        for leg in closing:
            symbol = leg.get("symbol")
            side = leg.get("intent")
            try:
                ratio = int(str(leg.get("ratio")))
            except ValueError:
                raise CompetitionRecordNotEligible(
                    "position closing ratio is unavailable"
                ) from None
            if not isinstance(symbol, str) or _standard_contract(symbol) is None or ratio != 1:
                raise CompetitionRecordNotEligible("position closing ratio is unavailable")
            expected_prior.append(
                (symbol, -intent.quantity if side == "BUY_TO_CLOSE" else intent.quantity)
            )
        _spread_from_symbols(expected_prior, intent.quantity, underlying=underlying)
        if tuple(sorted(expected_prior)) != tuple(sorted(prior_inventory)):
            raise CompetitionRecordNotEligible(
                "position closing legs do not match the prior spread"
            )


def _spread_from_inventory(
    inventory: Sequence[tuple[str, int]], *, underlying: str
) -> SpreadProjection:
    if len(inventory) != 2:
        raise CompetitionRecordNotEligible("position is not a two-leg vertical")
    quantities = {abs(quantity) for _symbol, quantity in inventory}
    if len(quantities) != 1 or sum(1 for _symbol, quantity in inventory if quantity > 0) != 1:
        raise CompetitionRecordNotEligible("position vertical quantities are inconsistent")
    return _spread_from_symbols(inventory, quantities.pop(), underlying=underlying)


def _spread_from_symbols(
    legs: Sequence[tuple[str, int]], quantity: int, *, underlying: str
) -> SpreadProjection:
    parsed: list[tuple[str, str, str, Decimal, int]] = []
    for symbol, signed_quantity in legs:
        contract = _standard_contract(symbol)
        if contract is None:
            raise CompetitionRecordNotEligible("position option structure is unavailable")
        parsed.append(
            (
                contract.root_symbol,
                contract.expiration_date.strftime("%y%m%d"),
                contract.right,
                contract.strike_price,
                signed_quantity,
            )
        )
    if (
        len({item[:3] for item in parsed}) != 1
        or parsed[0][0] != underlying
        or parsed[0][3] == parsed[1][3]
    ):
        raise CompetitionRecordNotEligible("position legs are not one vertical")
    long_leg = next((item for item in parsed if item[4] > 0), None)
    short_leg = next((item for item in parsed if item[4] < 0), None)
    if long_leg is None or short_leg is None:
        raise CompetitionRecordNotEligible("position option sides are not opposing")
    return SpreadProjection(
        underlying=underlying,
        option_type="CALL" if long_leg[2] == "C" else "PUT",
        expiration=datetime.strptime(long_leg[1], "%y%m%d").date(),
        long_strike=long_leg[3],
        short_strike=short_leg[3],
        quantity=quantity,
    )


def _standard_contract(symbol: object) -> OptionContractSymbol | None:
    try:
        return parse_standard_option_contract_symbol(symbol)
    except OptionContractSymbolError as error:
        if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
            raise CompetitionRecordNotEligible(error.code) from error
        return None


def _direction(exposure: object) -> PositionDirection:
    if not isinstance(exposure, dict):
        raise CompetitionRecordNotEligible("thesis direction is unavailable")
    try:
        delta = Decimal(str(exposure["delta"]))
    except (KeyError, ValueError, ArithmeticError):
        raise CompetitionRecordNotEligible("thesis direction is unavailable") from None
    if not delta.is_finite():
        raise CompetitionRecordNotEligible("thesis direction is unavailable")
    if delta > 0:
        return PositionDirection.BULLISH
    if delta < 0:
        return PositionDirection.BEARISH
    return PositionDirection.NEUTRAL


def _exposure(value: object) -> ExposureProjection | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value).issubset(
        {"delta", "gamma", "theta_per_day", "vega_per_iv_point"}
    ):
        raise CompetitionRecordNotEligible("current exposure is unavailable")
    try:
        return ExposureProjection.model_validate(value)
    except ValidationError as exc:
        raise CompetitionRecordNotEligible("current exposure is unavailable") from exc


def _source_hash(
    kind: CompetitionRecordKind,
    baseline: SubmissionBaselineRow,
    *,
    decision_hashes: Sequence[str] = (),
    position_hashes: Sequence[str] = (),
    closed_at: datetime | None = None,
) -> str:
    values = (*decision_hashes, *position_hashes)
    if any(_HASH.fullmatch(value) is None for value in values):
        raise CompetitionRecordNotEligible("competition source hash is unavailable")
    return canonical_hash(
        {
            "domain": _SOURCE_DOMAIN,
            "record_kind": kind.value,
            "baseline": {
                "baseline_id": str(baseline.baseline_id),
                "account_fingerprint": baseline.account_fingerprint,
                "captured_at": utc_text(_utc(baseline.captured_at)),
                "positions_hash": baseline.positions_hash,
                "orders_hash": baseline.orders_hash,
                "activities_hash": baseline.activities_hash,
            },
            "decision_hashes": list(decision_hashes),
            "position_hashes": list(position_hashes),
            "closed_at": None if closed_at is None else utc_text(_utc(closed_at)),
        }
    )


def _build_record(
    *,
    projection: NoTradeProjection | PositionProjection,
    occurred_at: datetime,
    published_at: datetime,
    predecessor_hash: str | None,
    source_authority_hash: str,
) -> CompetitionRecord:
    projection_value = canonical_value(projection)
    validated = validate_projection(projection_value)
    projection_hash = canonical_hash(validated)
    publication_hash = canonical_hash(
        {
            "domain": _HASH_DOMAIN,
            "public_record_id": validated.public_record_id,
            "source_authority_hash": source_authority_hash,
            "projection_hash": projection_hash,
            "published_at": utc_text(published_at),
            "predecessor_hash": predecessor_hash,
        }
    )
    payload = dict(projection_value) | {
        "published_at": utc_text(published_at),
        "publication_hash": publication_hash,
        "predecessor_hash": predecessor_hash,
    }
    return CompetitionRecord(
        kind=CompetitionRecordKind(validated.record_kind),
        public_record_id=validated.public_record_id,
        occurred_at=occurred_at,
        published_at=published_at,
        payload=payload,
        projection_hash=projection_hash,
        publication_hash=publication_hash,
        predecessor_hash=predecessor_hash,
    )


def _publication_row(
    record: CompetitionRecord,
    *,
    source_authority_hash: str,
    source_decision_id: UUID | None = None,
    source_managed_position_id: UUID | None = None,
) -> CompetitionRecordPublicationRow:
    return CompetitionRecordPublicationRow(
        publication_id=uuid5(NAMESPACE_URL, record.publication_hash),
        record_kind=record.kind.value,
        account_role="SUBMISSION",
        source_decision_id=source_decision_id,
        source_managed_position_id=source_managed_position_id,
        public_record_id=record.public_record_id,
        source_authority_hash=source_authority_hash,
        occurred_at=record.occurred_at,
        published_at=record.published_at,
        payload_text=record.payload_text,
        projection_hash=record.projection_hash,
        publication_hash=record.publication_hash,
        predecessor_hash=record.predecessor_hash,
    )


def _record_from_row(
    row: CompetitionRecordPublicationRow, session: Session | None = None
) -> CompetitionRecord:
    try:
        payload = json.loads(row.payload_text)
        if canonical_json(payload) != row.payload_text:
            raise CompetitionArchiveIntegrityError("competition record JSON is not canonical")
        if set(payload) < {"published_at", "publication_hash", "predecessor_hash"}:
            raise ValueError("payload envelope is incomplete")
        projection_value = dict(payload)
        publication_hash = projection_value.pop("publication_hash")
        predecessor_hash = projection_value.pop("predecessor_hash")
        published_at_text = projection_value.pop("published_at")
        projection = validate_projection(projection_value)
        if session is not None:
            _verify_row_source(session, row, projection)
        expected = _build_record(
            projection=projection,
            occurred_at=_utc(row.occurred_at),
            published_at=_utc(row.published_at),
            predecessor_hash=row.predecessor_hash,
            source_authority_hash=row.source_authority_hash,
        )
    except CompetitionArchiveIntegrityError:
        raise
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise CompetitionArchiveIntegrityError("competition record payload is invalid") from exc
    if (
        published_at_text != utc_text(_utc(row.published_at))
        or publication_hash != row.publication_hash
        or predecessor_hash != row.predecessor_hash
        or expected.public_record_id != row.public_record_id
        or expected.kind.value != row.record_kind
        or expected.projection_hash != row.projection_hash
        or expected.publication_hash != row.publication_hash
        or expected.payload_text != row.payload_text
    ):
        raise CompetitionArchiveIntegrityError("competition record integrity check failed")
    return expected


def _by_source_hash(session: Session, source_hash: str) -> CompetitionRecordPublicationRow | None:
    return session.scalar(
        select(CompetitionRecordPublicationRow).where(
            CompetitionRecordPublicationRow.source_authority_hash == source_hash
        )
    )


def _latest_verified_row(session: Session) -> CompetitionRecordPublicationRow | None:
    rows = tuple(
        session.scalars(
            select(CompetitionRecordPublicationRow).order_by(
                CompetitionRecordPublicationRow.published_at,
                CompetitionRecordPublicationRow.publication_id,
            )
        )
    )
    _verify_chain(rows, session)
    return rows[-1] if rows else None


def _verify_chain(
    rows: Iterable[CompetitionRecordPublicationRow], session: Session | None = None
) -> None:
    predecessor: str | None = None
    for row in rows:
        if row.predecessor_hash != predecessor:
            raise CompetitionArchiveIntegrityError("competition record chain is broken")
        _record_from_row(row, session)
        predecessor = row.publication_hash


def _verify_row_source(
    session: Session,
    row: CompetitionRecordPublicationRow,
    projection: NoTradeProjection | PositionProjection,
) -> None:
    try:
        if row.record_kind == CompetitionRecordKind.NO_TRADE.value:
            if row.source_decision_id is None or row.source_managed_position_id is not None:
                raise CompetitionArchiveIntegrityError("competition no-trade source is invalid")
            decision = session.get(AgentDecisionRow, row.source_decision_id)
            if decision is None:
                raise CompetitionArchiveIntegrityError("competition no-trade source is missing")
            snapshot = session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
            _validate_no_trade_authority(decision, snapshot)
            _validate_decision_hashes(session, decision, snapshot)
            baseline = _submission_baseline(session, decision.account_fingerprint)
            expected_hash = _source_hash(
                CompetitionRecordKind.NO_TRADE,
                baseline,
                decision_hashes=(decision.result_hash,),
            )
            expected_public_id = _public_id(CompetitionRecordKind.NO_TRADE, decision.result_hash)
        elif row.record_kind == CompetitionRecordKind.POSITION.value:
            if row.source_managed_position_id is None or row.source_decision_id is not None:
                raise CompetitionArchiveIntegrityError("competition position source is invalid")
            position = session.get(ManagedLifecyclePositionRow, row.source_managed_position_id)
            if position is None:
                raise CompetitionArchiveIntegrityError("competition position source is missing")
            baseline = _submission_baseline(session, position.account_fingerprint)
            thesis = session.get(ThesisVersionRow, position.thesis_version_id)
            if not isinstance(projection, PositionProjection):
                raise CompetitionArchiveIntegrityError(
                    "competition position projection kind is invalid"
                )
            through = _utc(projection.as_of)
            transitions = tuple(
                session.scalars(
                    select(ManagedPositionTransitionRow)
                    .where(
                        ManagedPositionTransitionRow.managed_position_id
                        == position.managed_position_id
                    )
                    .where(ManagedPositionTransitionRow.occurred_at <= through)
                    .order_by(ManagedPositionTransitionRow.transition_sequence)
                )
            )
            snapshots = tuple(
                session.scalars(
                    select(ManagedPositionSnapshotRow)
                    .join(
                        ManagedPositionTransitionRow,
                        ManagedPositionTransitionRow.transition_id
                        == ManagedPositionSnapshotRow.transition_id,
                    )
                    .where(
                        ManagedPositionSnapshotRow.managed_position_id
                        == position.managed_position_id
                    )
                    .where(ManagedPositionTransitionRow.occurred_at <= through)
                    .order_by(ManagedPositionSnapshotRow.accepted_at)
                )
            )
            if (
                thesis is None
                or thesis.account_role != "SUBMISSION"
                or not transitions
                or transitions[0].action != "ENTRY"
                or len(transitions) != len(snapshots)
                or any(item.transition_sequence != index for index, item in enumerate(transitions))
            ):
                raise CompetitionArchiveIntegrityError(
                    "competition historical position source is invalid"
                )
            _events, assessment_hashes = _assessment_events(
                session, position, thesis, through=through
            )
            expected_hash = _source_hash(
                CompetitionRecordKind.POSITION,
                baseline,
                position_hashes=(
                    thesis.thesis_hash,
                    *(item.transition_hash for item in transitions),
                    *(item.snapshot_hash for item in snapshots),
                    *assessment_hashes,
                    snapshots[-1].position_fingerprint,
                ),
                closed_at=(
                    _utc(projection.closed_at) if projection.closed_at is not None else None
                ),
            )
            expected_public_id = _public_id(
                CompetitionRecordKind.POSITION, transitions[0].transition_hash
            )
        else:
            raise CompetitionArchiveIntegrityError("competition record kind is invalid")
    except CompetitionRecordNotEligible as exc:
        raise CompetitionArchiveIntegrityError(
            "competition record source authority is invalid"
        ) from exc
    if row.source_authority_hash != expected_hash or row.public_record_id != expected_public_id:
        raise CompetitionArchiveIntegrityError(
            "competition record source authority is inconsistent"
        )


def _public_id(kind: CompetitionRecordKind, stable_source_hash: str) -> str:
    return canonical_hash(
        {"domain": _PUBLIC_ID_DOMAIN, "kind": kind.value, "source": stable_source_hash}
    )


def _next_publication_time(
    session: Session, predecessor: CompetitionRecordPublicationRow | None
) -> datetime:
    published_at = _database_now(session)
    if predecessor is not None and published_at <= _utc(predecessor.published_at):
        return _utc(predecessor.published_at) + timedelta(microseconds=1)
    return published_at


def _database_now(session: Session) -> datetime:
    function = (
        func.clock_timestamp
        if session.bind is not None and session.bind.dialect.name == "postgresql"
        else func.current_timestamp
    )
    value = session.scalar(select(function()))
    if not isinstance(value, datetime):
        raise RuntimeError("competition archive database clock is unavailable")
    return _utc(value)


def _lock_lane(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _PUBLICATION_LOCK_ID},
        )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
