from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.domain.option_contract_symbol import (
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)
from backend.app.execution.order_status import broker_state_matches_fill
from backend.app.experiment_lineage import (
    ExperimentExecutionLineage,
    optional_experiment_execution_lineage,
)
from backend.app.persistence.sqlalchemy_models import (
    AgentDecisionRow,
    AssessmentCertificateRow,
    CompetitionRecordPublicationRow,
    EntryApprovalCertificateRow,
    ExecutionCertificateRow,
    ExecutionIntentRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
    OrderAttemptRow,
)

from .performance import (
    ContractQuantity,
    ExperimentDecisionEvidence,
    ExperimentPerformanceEvidenceError,
    ExperimentPerformanceProjection,
    FillAttemptEvidence,
    LifecycleTransitionEvidence,
    PositionLifecycleEvidence,
    project_experiment_performance,
)
from .repository import (
    ExperimentRegistryError,
    SQLAlchemyExperimentRegistry,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")


class SQLAlchemyExperimentPerformanceReader:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions
        self._registry = SQLAlchemyExperimentRegistry(sessions)

    def project(
        self,
        lineage: ExperimentExecutionLineage,
    ) -> ExperimentPerformanceProjection:
        self._verify_compiled_lineage(lineage)
        try:
            with self._sessions() as session:
                decisions = self._decisions(session, lineage)
                positions = self._positions(session, lineage)
        except ExperimentPerformanceEvidenceError:
            raise
        except SQLAlchemyError as error:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_READ_FAILED"
            ) from error
        return project_experiment_performance(
            lineage=lineage,
            decisions=decisions,
            positions=positions,
        )

    def project_for_experiment(self, experiment_id: UUID) -> ExperimentPerformanceProjection | None:
        try:
            compiled = self._registry.read_compiled(experiment_id)
        except ExperimentRegistryError as error:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_READ_FAILED"
            ) from error
        if compiled is None:
            return None
        return self.project(
            ExperimentExecutionLineage(
                experiment_id=compiled.experiment_id,
                source_definition_hash=compiled.source_definition_hash,
                protocol_hash=compiled.protocol_hash,
            )
        )

    def project_published(self, experiment_id: UUID) -> ExperimentPerformanceProjection | None:
        projection = self.project_for_experiment(experiment_id)
        if projection is None:
            return None
        try:
            with self._sessions() as session:
                published = session.scalar(
                    select(CompetitionRecordPublicationRow.publication_id)
                    .outerjoin(
                        AgentDecisionRow,
                        CompetitionRecordPublicationRow.source_decision_id
                        == AgentDecisionRow.decision_id,
                    )
                    .outerjoin(
                        ManagedLifecyclePositionRow,
                        CompetitionRecordPublicationRow.source_managed_position_id
                        == ManagedLifecyclePositionRow.managed_position_id,
                    )
                    .where(
                        or_(
                            AgentDecisionRow.experiment_id == experiment_id,
                            ManagedLifecyclePositionRow.experiment_id == experiment_id,
                        )
                    )
                    .limit(1)
                )
        except SQLAlchemyError as error:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_READ_FAILED"
            ) from error
        return projection if published is not None else None

    def _verify_compiled_lineage(self, lineage: ExperimentExecutionLineage) -> None:
        try:
            compiled = self._registry.read_compiled(lineage.experiment_id)
        except ExperimentRegistryError as error:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_LINEAGE_INCOMPLETE"
            ) from error
        if compiled is None:
            raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LINEAGE_INCOMPLETE")
        if (
            compiled.source_definition_hash != lineage.source_definition_hash
            or compiled.protocol_hash != lineage.protocol_hash
        ):
            raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LINEAGE_MISMATCH")

    @staticmethod
    def _decisions(
        session: Session,
        lineage: ExperimentExecutionLineage,
    ) -> tuple[ExperimentDecisionEvidence, ...]:
        rows = tuple(
            session.scalars(
                select(AgentDecisionRow)
                .where(AgentDecisionRow.experiment_id == lineage.experiment_id)
                .order_by(AgentDecisionRow.decision_boundary, AgentDecisionRow.decision_id)
            )
        )
        return tuple(
            ExperimentDecisionEvidence(
                decision_id=row.decision_id,
                lineage=_require_lineage(row, lineage),
                occurred_at=row.decision_boundary,
            )
            for row in rows
        )

    @staticmethod
    def _positions(
        session: Session,
        lineage: ExperimentExecutionLineage,
    ) -> tuple[PositionLifecycleEvidence, ...]:
        rows = tuple(
            session.scalars(
                select(ManagedLifecyclePositionRow)
                .where(ManagedLifecyclePositionRow.experiment_id == lineage.experiment_id)
                .order_by(
                    ManagedLifecyclePositionRow.activated_at,
                    ManagedLifecyclePositionRow.managed_position_id,
                )
            )
        )
        return tuple(_position_evidence(session, row, lineage) for row in rows)


def _position_evidence(
    session: Session,
    position: ManagedLifecyclePositionRow,
    lineage: ExperimentExecutionLineage,
) -> PositionLifecycleEvidence:
    position_lineage = _require_lineage(position, lineage)
    approval = session.get(EntryApprovalCertificateRow, position.entry_approval_id)
    if approval is None or approval.agent_decision_id is None:
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_DECISION_LINEAGE_INCOMPLETE"
        )
    approval_lineage = _require_lineage(approval, lineage)
    if approval_lineage != position_lineage:
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LINEAGE_MISMATCH")

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
    snapshot_by_transition = {item.transition_id: item for item in snapshots}
    if (
        not transitions
        or len(snapshot_by_transition) != len(snapshots)
        or len(snapshots) != len(transitions)
    ):
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LIFECYCLE_INCOMPLETE")

    evidence: list[LifecycleTransitionEvidence] = []
    previous_snapshot_id: UUID | None = None
    for transition in transitions:
        snapshot = snapshot_by_transition.get(transition.transition_id)
        if snapshot is None:
            raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LIFECYCLE_INCOMPLETE")
        if (
            snapshot.predecessor_snapshot_id != previous_snapshot_id
            or snapshot.managed_position_id != position.managed_position_id
            or snapshot.reconciliation_id != transition.post_reconciliation_id
            or snapshot.market_session_id != transition.market_session_id
            or snapshot.position_fingerprint != transition.resulting_position_fingerprint
            or snapshot.accepted_at != transition.occurred_at
        ):
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_LIFECYCLE_CHRONOLOGY_INVALID"
            )
        evidence.append(
            _transition_evidence(
                session,
                transition,
                snapshot,
                position,
                approval,
                lineage,
            )
        )
        previous_snapshot_id = snapshot.snapshot_id

    last_snapshot = snapshot_by_transition[transitions[-1].transition_id]
    if (
        position.entry_execution_certificate_id != transitions[0].execution_certificate_id
        or position.entry_intent_id != transitions[0].execution_intent_id
        or position.entry_reconciliation_id != transitions[0].post_reconciliation_id
        or position.current_snapshot_id != last_snapshot.snapshot_id
        or position.current_reconciliation_state_id != last_snapshot.reconciliation_state_id
        or position.active_position_fingerprint != last_snapshot.position_fingerprint
    ):
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LIFECYCLE_INCOMPLETE")

    return PositionLifecycleEvidence(
        managed_position_id=position.managed_position_id,
        lineage=position_lineage,
        entry_decision_id=approval.agent_decision_id,
        entry_approval_id=approval.approval_id,
        entry_approval_lineage=approval_lineage,
        defined_maximum_risk=Decimal(approval.approved_max_loss),
        activated_at=position.activated_at,
        closed_at=position.closed_at,
        transitions=tuple(evidence),
    )


def _transition_evidence(
    session: Session,
    transition: ManagedPositionTransitionRow,
    snapshot: ManagedPositionSnapshotRow,
    position: ManagedLifecyclePositionRow,
    entry_approval: EntryApprovalCertificateRow,
    lineage: ExperimentExecutionLineage,
) -> LifecycleTransitionEvidence:
    intent = session.get(ExecutionIntentRow, transition.execution_intent_id)
    certificate = session.get(ExecutionCertificateRow, transition.execution_certificate_id)
    if intent is None or certificate is None:
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE")
    if transition.action == "ENTRY":
        authorization = entry_approval
        authorization_id = entry_approval.approval_id
        decision_id = entry_approval.agent_decision_id
        authorization_matches = (
            intent.entry_approval_id == authorization_id
            and intent.assessment_certificate_id is None
            and certificate.entry_approval_id == authorization_id
            and certificate.assessment_certificate_id is None
        )
    else:
        if intent.assessment_certificate_id is None:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_DECISION_LINEAGE_INCOMPLETE"
            )
        assessment = session.get(
            AssessmentCertificateRow,
            intent.assessment_certificate_id,
        )
        if assessment is None or assessment.agent_decision_id is None:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_DECISION_LINEAGE_INCOMPLETE"
            )
        authorization = assessment
        authorization_id = assessment.certificate_id
        decision_id = assessment.agent_decision_id
        authorization_matches = (
            assessment.action == transition.action
            and assessment.position_fingerprint
            == _predecessor_fingerprint(session, transition, position)
            and intent.entry_approval_id is None
            and certificate.entry_approval_id is None
            and certificate.assessment_certificate_id == authorization_id
        )
    if decision_id is None:
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_DECISION_LINEAGE_INCOMPLETE"
        )
    authorization_lineage = _require_lineage(authorization, lineage)
    if (
        not authorization_matches
        or not authorization.valid
        or intent.account_role != position.account_role
        or intent.action != transition.action
        or intent.policy_hash != authorization.policy_hash
        or intent.quantity != authorization.quantity
        or Decimal(intent.approved_max_loss) != Decimal(authorization.approved_max_loss)
        or intent.state != "TERMINAL"
        or not intent.first_fill_consumed
        or certificate.execution_intent_id != intent.intent_id
        or certificate.execution_status != "FILLED"
        or certificate.reconciliation_id != transition.post_reconciliation_id
        or certificate.reconciliation_hash is None
        or certificate.last_observation_hash is None
    ):
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE")

    attempts = tuple(
        session.scalars(
            select(OrderAttemptRow)
            .where(OrderAttemptRow.execution_intent_id == intent.intent_id)
            .order_by(OrderAttemptRow.attempt_ordinal, OrderAttemptRow.attempt_id)
        )
    )
    attempt_ids = _certificate_attempt_ids(certificate.attempt_ids)
    if tuple(item.attempt_id for item in attempts) != attempt_ids:
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE")
    for attempt in attempts:
        try:
            state_matches = broker_state_matches_fill(
                attempt.state,
                attempt.filled_quantity,
                attempt.quantity,
            )
        except ValueError:
            state_matches = False
        if not state_matches:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE"
            )

    activity = _activity_contracts(transition.fill_activity_manifest, attempts, intent)
    inventory = _inventory_contracts(snapshot.normalized_inventory)
    return LifecycleTransitionEvidence(
        transition_id=transition.transition_id,
        predecessor_transition_id=transition.predecessor_transition_id,
        sequence=transition.transition_sequence,
        action=transition.action,
        decision_id=decision_id,
        authorization_id=authorization_id,
        authorization_lineage=authorization_lineage,
        occurred_at=transition.occurred_at,
        intent_action=intent.action,
        authorized_quantity=intent.quantity,
        execution_status=certificate.execution_status,
        certificate_attempt_ids=attempt_ids,
        attempts=tuple(
            FillAttemptEvidence(
                attempt_id=item.attempt_id,
                attempt_ordinal=item.attempt_ordinal,
                requested_quantity=item.quantity,
                filled_quantity=item.filled_quantity,
                fill_cash_flow=(
                    None if item.fill_cash_flow is None else Decimal(item.fill_cash_flow)
                ),
            )
            for item in attempts
        ),
        reconciled_contract_activity=activity,
        resulting_inventory=inventory,
        cash_flow=Decimal(transition.cashflow_contribution),
        snapshot_cumulative_cash_flow=Decimal(snapshot.cumulative_cashflow),
    )


def _predecessor_fingerprint(
    session: Session,
    transition: ManagedPositionTransitionRow,
    position: ManagedLifecyclePositionRow,
) -> str:
    if transition.predecessor_transition_id is None:
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_LIFECYCLE_CHRONOLOGY_INVALID"
        )
    predecessor = session.get(
        ManagedPositionTransitionRow,
        transition.predecessor_transition_id,
    )
    if predecessor is None or predecessor.managed_position_id != position.managed_position_id:
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_LIFECYCLE_CHRONOLOGY_INVALID"
        )
    return predecessor.resulting_position_fingerprint


def _require_lineage(
    row: object,
    expected: ExperimentExecutionLineage,
) -> ExperimentExecutionLineage:
    try:
        lineage = optional_experiment_execution_lineage(
            getattr(row, "experiment_id", None),
            getattr(row, "experiment_source_definition_hash", None),
            getattr(row, "experiment_protocol_hash", None),
        )
    except ValueError as error:
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_LINEAGE_INCOMPLETE"
        ) from error
    if lineage is None:
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LINEAGE_INCOMPLETE")
    if lineage != expected:
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_LINEAGE_MISMATCH")
    return lineage


def _certificate_attempt_ids(value: object) -> tuple[UUID, ...]:
    if not isinstance(value, list):
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE")
    try:
        attempt_ids = tuple(UUID(item) for item in value if isinstance(item, str))
    except ValueError as error:
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE"
        ) from error
    if len(attempt_ids) != len(value) or len(set(attempt_ids)) != len(attempt_ids):
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE")
    return attempt_ids


def _activity_contracts(
    value: object,
    attempts: tuple[OrderAttemptRow, ...],
    intent: ExecutionIntentRow,
) -> tuple[ContractQuantity, ...]:
    if not isinstance(value, list) or not value:
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
        )
    expected = _expected_activity(attempts, intent)
    activity_hashes: set[str] = set()
    observed: dict[tuple[str, str, str], int] = {}
    output: list[ContractQuantity] = []
    for item in value:
        if not isinstance(item, dict):
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
            )
        activity_hash = item.get("activity_id_hash")
        symbol = item.get("symbol")
        identity = (
            item.get("client_order_id"),
            item.get("provider_order_id"),
            symbol,
        )
        if (
            not isinstance(activity_hash, str)
            or _HASH.fullmatch(activity_hash) is None
            or activity_hash in activity_hashes
            or item.get("activity_type") not in {"OPTRD", "FILL"}
            or identity not in expected
        ):
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
            )
        activity_hashes.add(activity_hash)
        contract = _contract_quantity(symbol, item.get("signed_quantity"), 100)
        observed[identity] = observed.get(identity, 0) + contract.signed_quantity
        output.append(contract)
    if observed != expected:
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
        )
    return tuple(output)


def _expected_activity(
    attempts: tuple[OrderAttemptRow, ...],
    intent: ExecutionIntentRow,
) -> dict[tuple[str, str, str], int]:
    if not isinstance(intent.legs, list):
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
        )
    direction = {
        "BUY_TO_OPEN": 1,
        "BUY_TO_CLOSE": 1,
        "SELL_TO_OPEN": -1,
        "SELL_TO_CLOSE": -1,
    }
    expected: dict[tuple[str, str, str], int] = {}
    for attempt in attempts:
        if attempt.filled_quantity == 0:
            continue
        if not attempt.provider_order_id:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE"
            )
        for leg in intent.legs:
            if not isinstance(leg, dict):
                raise ExperimentPerformanceEvidenceError(
                    "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
                )
            symbol = leg.get("symbol")
            leg_intent = leg.get("intent")
            ratio = leg.get("ratio")
            try:
                parsed_symbol = parse_standard_option_contract_symbol(symbol).symbol
                parsed_ratio = int(str(ratio))
            except (OptionContractSymbolError, TypeError, ValueError) as error:
                raise ExperimentPerformanceEvidenceError(
                    "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
                ) from error
            if leg_intent not in direction or parsed_ratio <= 0:
                raise ExperimentPerformanceEvidenceError(
                    "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
                )
            key = (attempt.client_order_id, attempt.provider_order_id, parsed_symbol)
            expected[key] = expected.get(key, 0) + (
                attempt.filled_quantity * parsed_ratio * direction[leg_intent]
            )
    if not expected:
        raise ExperimentPerformanceEvidenceError("EXPERIMENT_PERFORMANCE_FILL_LINEAGE_INCOMPLETE")
    return expected


def _inventory_contracts(value: object) -> tuple[ContractQuantity, ...]:
    if not isinstance(value, list):
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
        )
    output: list[ContractQuantity] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "kind",
            "symbol",
            "signed_quantity",
            "multiplier",
        }:
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
            )
        if item.get("kind") != "OPTION":
            raise ExperimentPerformanceEvidenceError(
                "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
            )
        output.append(
            _contract_quantity(
                item.get("symbol"),
                item.get("signed_quantity"),
                item.get("multiplier"),
            )
        )
    return tuple(output)


def _contract_quantity(symbol: object, quantity: object, multiplier: object) -> ContractQuantity:
    try:
        parsed_symbol = parse_standard_option_contract_symbol(symbol).symbol
        parsed_quantity = Decimal(str(quantity))
        parsed_multiplier = Decimal(str(multiplier))
    except (OptionContractSymbolError, InvalidOperation, ValueError) as error:
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
        ) from error
    if (
        not parsed_quantity.is_finite()
        or parsed_quantity != parsed_quantity.to_integral_value()
        or parsed_quantity == 0
        or parsed_multiplier != 100
    ):
        raise ExperimentPerformanceEvidenceError(
            "EXPERIMENT_PERFORMANCE_CONTRACT_LINEAGE_INCOMPLETE"
        )
    return ContractQuantity(
        symbol=parsed_symbol,
        signed_quantity=int(parsed_quantity),
        multiplier=100,
    )
