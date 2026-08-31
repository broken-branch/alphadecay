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

from backend.app.contracts.v1 import PositionIntent
from backend.app.contracts.v1.models import canonical_decimal
from backend.app.domain.option_contract_symbol import (
    NON_STANDARD_CONTRACT_UNSUPPORTED,
    OptionContractSymbol,
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)
from backend.app.execution import intent_digest, order_envelope_hash
from backend.app.execution.models import ExecutionAction, OrderEnvelope, OrderLegIntent
from backend.app.persistence.sqlalchemy_models import (
    AccountReconciliationStateRow,
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AlpacaMarketSessionRow,
    AssessmentCertificateRow,
    AttemptObservationRow,
    BrokerMutationPermitRow,
    ExecutionCertificateRow,
    ExecutionIntentRow,
    LifecycleAccountObservationRow,
    LifecycleObservationBindingRow,
    LifecycleObservationManifestRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
    OrderAttemptRow,
    ThesisVersionRow,
    WholeAccountReconciliationRow,
)

from .fingerprint import option_position_fingerprint

_HASH = re.compile(r"^[0-9a-f]{64}$")
_FINALIZATION_CHECKS = (
    "TERMINAL",
    "REMAINDER_ABSENT",
    "WHOLE_ACCOUNT_RECONCILED",
)


class LifecycleTerminalMaterializationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SQLAlchemyLifecycleTerminalMaterializer:
    def __init__(self, sessions: sessionmaker) -> None:
        self._sessions = sessions

    def materialize(self, *, execution_certificate_id: UUID) -> UUID:
        if not isinstance(execution_certificate_id, UUID):
            raise LifecycleTerminalMaterializationError("LIFECYCLE_MATERIALIZATION_INPUT_INVALID")
        try:
            with self._sessions.begin() as session:
                certificate = session.scalar(
                    select(ExecutionCertificateRow)
                    .where(ExecutionCertificateRow.certificate_id == execution_certificate_id)
                    .with_for_update()
                )
                if certificate is None:
                    raise LifecycleTerminalMaterializationError("LIFECYCLE_CERTIFICATE_NOT_FOUND")
                if certificate.execution_status != "FILLED":
                    raise LifecycleTerminalMaterializationError(
                        "LIFECYCLE_EXECUTION_NOT_FULLY_FILLED"
                    )
                intent = session.scalar(
                    select(ExecutionIntentRow)
                    .where(ExecutionIntentRow.intent_id == certificate.execution_intent_id)
                    .with_for_update()
                )
                assessment = (
                    session.get(AssessmentCertificateRow, certificate.assessment_certificate_id)
                    if certificate.assessment_certificate_id is not None
                    else None
                )
                decision = (
                    session.get(AgentDecisionRow, assessment.agent_decision_id)
                    if assessment is not None and assessment.agent_decision_id is not None
                    else None
                )
                input_snapshot = (
                    session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
                    if decision is not None
                    else None
                )
                binding = (
                    session.scalar(
                        select(LifecycleObservationBindingRow).where(
                            LifecycleObservationBindingRow.agent_input_snapshot_id
                            == input_snapshot.snapshot_id
                        )
                    )
                    if input_snapshot is not None
                    else None
                )
                manifest = (
                    session.get(LifecycleObservationManifestRow, binding.manifest_id)
                    if binding is not None
                    else None
                )
                account_observation = (
                    session.get(
                        LifecycleAccountObservationRow,
                        manifest.account_observation_id,
                    )
                    if manifest is not None and manifest.account_observation_id is not None
                    else None
                )
                ids = _terminal_materialization_ids(certificate.certificate_id)
                existing_transition = session.get(ManagedPositionTransitionRow, ids.transition)
                if existing_transition is not None:
                    position = session.get(
                        ManagedLifecyclePositionRow,
                        existing_transition.managed_position_id,
                    )
                    predecessor = session.get(
                        ManagedPositionTransitionRow,
                        existing_transition.predecessor_transition_id,
                    )
                    predecessor_snapshot = (
                        session.scalar(
                            select(ManagedPositionSnapshotRow).where(
                                ManagedPositionSnapshotRow.transition_id
                                == predecessor.transition_id
                            )
                        )
                        if predecessor is not None
                        else None
                    )
                else:
                    positions = (
                        session.scalars(
                            select(ManagedLifecyclePositionRow)
                            .where(
                                ManagedLifecyclePositionRow.account_role == "DEVELOPMENT",
                                ManagedLifecyclePositionRow.closed_at.is_(None),
                                ManagedLifecyclePositionRow.active_position_fingerprint
                                == (assessment.position_fingerprint if assessment else ""),
                            )
                            .with_for_update()
                        ).all()
                        if assessment is not None
                        else []
                    )
                    position = positions[0] if len(positions) == 1 else None
                    predecessor_snapshot = (
                        session.get(ManagedPositionSnapshotRow, position.current_snapshot_id)
                        if position is not None
                        else None
                    )
                    predecessor = (
                        session.get(
                            ManagedPositionTransitionRow,
                            predecessor_snapshot.transition_id,
                        )
                        if predecessor_snapshot is not None
                        else None
                    )
                account = (
                    session.get(AccountRoleRow, position.account_role)
                    if position is not None
                    else None
                )
                predecessor_state = (
                    session.get(
                        AccountReconciliationStateRow,
                        predecessor_snapshot.reconciliation_state_id,
                    )
                    if predecessor_snapshot is not None
                    else None
                )
                thesis = (
                    session.get(ThesisVersionRow, position.thesis_version_id)
                    if position is not None
                    else None
                )
                reconciliation = (
                    session.get(WholeAccountReconciliationRow, certificate.reconciliation_id)
                    if certificate.reconciliation_id is not None
                    else None
                )
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
                attempts = (
                    session.scalars(
                        select(OrderAttemptRow)
                        .where(OrderAttemptRow.execution_intent_id == intent.intent_id)
                        .order_by(OrderAttemptRow.attempt_ordinal)
                        .with_for_update()
                    ).all()
                    if intent is not None
                    else []
                )
                observations = (
                    session.scalars(
                        select(AttemptObservationRow)
                        .where(AttemptObservationRow.execution_intent_id == intent.intent_id)
                        .order_by(
                            AttemptObservationRow.observation_sequence,
                            AttemptObservationRow.observation_id,
                        )
                        .with_for_update()
                    ).all()
                    if intent is not None
                    else []
                )
                observation = (
                    observations[-1]
                    if state is not None
                    and observations
                    and observations[-1].observation_id == state.authority_observation_id
                    else None
                )
                market_sessions = (
                    session.scalars(
                        select(AlpacaMarketSessionRow).where(
                            AlpacaMarketSessionRow.open_at <= reconciliation.accepted_at,
                            AlpacaMarketSessionRow.close_at >= reconciliation.accepted_at,
                        )
                    ).all()
                    if reconciliation is not None
                    else []
                )
                market_session = market_sessions[0] if len(market_sessions) == 1 else None
                inventory, activities, cashflow, fingerprint = _validate_terminal_lineage(
                    certificate=certificate,
                    intent=intent,
                    assessment=assessment,
                    decision=decision,
                    input_snapshot=input_snapshot,
                    binding=binding,
                    manifest=manifest,
                    account_observation=account_observation,
                    position=position,
                    predecessor=predecessor,
                    predecessor_snapshot=predecessor_snapshot,
                    predecessor_state=predecessor_state,
                    account=account,
                    thesis=thesis,
                    reconciliation=reconciliation,
                    state=state,
                    permit=permit,
                    observation=observation,
                    attempts=attempts,
                    market_session=market_session,
                )
                transition_values = {
                    "transition_id": ids.transition,
                    "managed_position_id": position.managed_position_id,
                    "predecessor_transition_id": predecessor.transition_id,
                    "transition_sequence": predecessor.transition_sequence + 1,
                    "action": intent.action,
                    "execution_intent_id": intent.intent_id,
                    "execution_certificate_id": certificate.certificate_id,
                    "post_reconciliation_id": reconciliation.reconciliation_id,
                    "fill_activity_manifest": activities,
                    "fill_activity_manifest_hash": _json_hash(session, activities),
                    "cashflow_contribution": cashflow,
                    "resulting_position_fingerprint": fingerprint,
                    "occurred_at": _utc(reconciliation.accepted_at),
                    "market_session_id": market_session.market_session_id,
                }
                rolls = predecessor_snapshot.rolls_on_trading_day
                if predecessor_snapshot.market_session_id != market_session.market_session_id:
                    rolls = 0
                if intent.action == "ROLL":
                    rolls += 1
                snapshot_values = {
                    "snapshot_id": ids.snapshot,
                    "managed_position_id": position.managed_position_id,
                    "predecessor_snapshot_id": predecessor_snapshot.snapshot_id,
                    "transition_id": ids.transition,
                    "reconciliation_id": reconciliation.reconciliation_id,
                    "reconciliation_state_id": state.state_id,
                    "normalized_inventory": inventory,
                    "inventory_hash": _json_hash(session, inventory),
                    "activity_manifest": reconciliation.sweep_payload["activities"],
                    "activity_manifest_hash": _json_hash(
                        session, reconciliation.sweep_payload["activities"]
                    ),
                    "cumulative_cashflow": Decimal(predecessor_snapshot.cumulative_cashflow)
                    + cashflow,
                    "rolls_on_trading_day": rolls,
                    "market_session_id": market_session.market_session_id,
                    "position_fingerprint": fingerprint,
                    "accepted_at": _utc(reconciliation.accepted_at),
                }
                if existing_transition is not None:
                    _validate_existing_terminal(
                        session,
                        position=position,
                        transition=existing_transition,
                        snapshot=session.get(ManagedPositionSnapshotRow, ids.snapshot),
                        transition_values=transition_values,
                        snapshot_values=snapshot_values,
                    )
                    return ids.transition
                session.add(
                    ManagedPositionTransitionRow(
                        **transition_values,
                        transition_hash=_row_hash(session, transition_values),
                    )
                )
                session.flush()
                session.add(
                    ManagedPositionSnapshotRow(
                        **snapshot_values,
                        snapshot_hash=_row_hash(session, snapshot_values),
                    )
                )
                session.flush()
                position.current_reconciliation_state_id = state.state_id
                position.current_snapshot_id = ids.snapshot
                position.active_position_fingerprint = fingerprint
                position.closed_at = (
                    _utc(reconciliation.accepted_at) if intent.action == "CLOSE" else None
                )
                session.flush()
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                return ids.transition
        except LifecycleTerminalMaterializationError:
            raise
        except (ArithmeticError, KeyError, SQLAlchemyError, TypeError, ValueError) as error:
            raise LifecycleTerminalMaterializationError(
                "LIFECYCLE_MATERIALIZATION_LINEAGE_INVALID"
            ) from error


class _TerminalMaterializationIds:
    def __init__(self, certificate_id: UUID) -> None:
        self.transition = uuid5(
            NAMESPACE_URL, f"alphadecay:managed-position-terminal:{certificate_id}"
        )
        self.snapshot = uuid5(
            NAMESPACE_URL, f"alphadecay:managed-position-terminal-snapshot:{certificate_id}"
        )


def _terminal_materialization_ids(certificate_id: UUID) -> _TerminalMaterializationIds:
    return _TerminalMaterializationIds(certificate_id)


def _validate_terminal_lineage(
    *,
    certificate,
    intent,
    assessment,
    decision,
    input_snapshot,
    binding,
    manifest,
    account_observation,
    position,
    predecessor,
    predecessor_snapshot,
    predecessor_state,
    account,
    thesis,
    reconciliation,
    state,
    permit,
    observation,
    attempts,
    market_session,
):
    expected_outcomes = {
        "CLOSE": {"CLOSE_APPROVED", "CLOSE_RISK_ONLY"},
        "ROLL": {"ROLL_APPROVED"},
    }
    if (
        intent is None
        or assessment is None
        or decision is None
        or input_snapshot is None
        or binding is None
        or manifest is None
        or account_observation is None
        or position is None
        or predecessor is None
        or predecessor_snapshot is None
        or predecessor_state is None
        or account is None
        or thesis is None
        or reconciliation is None
        or state is None
        or permit is None
        or observation is None
        or market_session is None
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_LINEAGE_INCOMPLETE")
    if (
        intent.action not in expected_outcomes
        or intent.state != "TERMINAL"
        or not intent.first_fill_consumed
        or intent.account_role != "DEVELOPMENT"
        or certificate.certificate_id
        != uuid5(NAMESPACE_URL, f"alphadecay:execution:{intent.intent_digest}")
        or certificate.execution_intent_id != intent.intent_id
        or certificate.entry_approval_id is not None
        or certificate.assessment_certificate_id != assessment.certificate_id
        or certificate.reconciliation_id != reconciliation.reconciliation_id
        or certificate.reconciliation_hash != reconciliation.reconciliation_hash
        or certificate.last_observation_hash != observation.observation_hash
        or certificate.actual_exposure != assessment.expected_after_exposure
        or tuple(certificate.reconciliation_checks) != _FINALIZATION_CHECKS
        or intent.entry_approval_id is not None
        or intent.assessment_certificate_id != assessment.certificate_id
        or intent.action != assessment.action
        or intent.fingerprint != assessment.position_fingerprint
        or intent.envelope_hash != assessment.envelope_hash
        or Decimal(intent.approved_max_loss) != Decimal(assessment.approved_max_loss)
        or intent.quantity != assessment.quantity
        or (intent.action == "ROLL") != (assessment.expected_after_exposure is not None)
        or not assessment.valid
        or assessment.account_role != "DEVELOPMENT"
        or assessment.thesis_version_id != thesis.thesis_version_id
        or assessment.policy_hash != thesis.policy_hash
        or assessment.policy_hash != intent.policy_hash
        or assessment.agent_decision_id != decision.decision_id
        or decision.input_snapshot_id != input_snapshot.snapshot_id
        or decision.thesis_version_id != thesis.thesis_version_id
        or decision.account_role != "DEVELOPMENT"
        or decision.account_fingerprint != position.account_fingerprint
        or decision.decision_kind != "ASSESSMENT"
        or decision.decision_boundary != input_snapshot.decision_boundary
        or decision.outcome not in expected_outcomes[intent.action]
        or decision.policy_hash != thesis.policy_hash
        or not decision.autonomy_authorized
        or input_snapshot.thesis_version_id != thesis.thesis_version_id
        or input_snapshot.account_role != "DEVELOPMENT"
        or input_snapshot.account_fingerprint != position.account_fingerprint
        or input_snapshot.decision_kind != "ASSESSMENT"
        or binding.agent_input_snapshot_id != input_snapshot.snapshot_id
        or binding.manifest_id != manifest.manifest_id
        or manifest.managed_position_id != position.managed_position_id
        or manifest.managed_snapshot_id != predecessor_snapshot.snapshot_id
        or manifest.agent_input_snapshot_id is not None
        or manifest.reconciliation_id is not None
        or account_observation.managed_position_id != position.managed_position_id
        or account_observation.managed_snapshot_id != predecessor_snapshot.snapshot_id
        or account_observation.account_role != "DEVELOPMENT"
        or account_observation.account_fingerprint != position.account_fingerprint
        or account_observation.sweep_hash != manifest.sweep_hash
        or input_snapshot.normalized_payload.get("acquisition_manifest_id")
        != str(manifest.manifest_id)
        or input_snapshot.normalized_payload.get("acquisition_manifest_hash")
        != manifest.manifest_hash
        or position.account_role != "DEVELOPMENT"
        or position.account_fingerprint != account.account_fingerprint
        or account.role != "DEVELOPMENT"
        or position.thesis_version_id != thesis.thesis_version_id
        or predecessor.managed_position_id != position.managed_position_id
        or predecessor_snapshot.managed_position_id != position.managed_position_id
        or predecessor_snapshot.transition_id != predecessor.transition_id
        or predecessor_snapshot.position_fingerprint != assessment.position_fingerprint
        or reconciliation.account_role != "DEVELOPMENT"
        or reconciliation.account_fingerprint != position.account_fingerprint
        or reconciliation.execution_intent_id != intent.intent_id
        or reconciliation.intent_digest != intent.intent_digest
        or not reconciliation.safe
        or reconciliation.block_codes
        or state.account_role != "DEVELOPMENT"
        or state.account_fingerprint != position.account_fingerprint
        or state.authority_reconciliation_id != reconciliation.reconciliation_id
        or state.predecessor_state_id != predecessor_snapshot.reconciliation_state_id
        or state.sequence != predecessor_state.sequence + 1
        or state.accepted_at != reconciliation.accepted_at
        or state.expected_positions != reconciliation.sweep_payload.get("final_positions")
        or state.expected_open_orders
        != reconciliation.expectation_payload.get("expected_open_orders")
        or state.known_activities != reconciliation.sweep_payload.get("activities")
        or Decimal(state.expected_cash)
        != Decimal(str(reconciliation.expectation_payload.get("expected_cash")))
        or state.authority_permit_id != permit.permit_id
        or state.authority_observation_id != observation.observation_id
        or state.authority_permit_request_hash != permit.request_hash
        or reconciliation.attempt_ordinal != permit.attempt_ordinal
        or reconciliation.request_hash != permit.request_hash
        or reconciliation.purpose != permit.mutation_kind
        or permit.execution_intent_id != intent.intent_id
        or permit.intent_digest != intent.intent_digest
        or permit.state != "CONSUMED"
        or observation.permit_id != permit.permit_id
        or observation.execution_intent_id != intent.intent_id
        or not observation.provider_present
        or _utc(permit.issued_at) > _utc(permit.dispatch_acquired_at)
        or _utc(assessment.created_at) > _utc(permit.issued_at)
        or _utc(permit.dispatch_acquired_at) >= _utc(assessment.expires_at)
        or _utc(permit.dispatch_acquired_at) > _utc(permit.consumed_at)
        or _utc(permit.consumed_at) > _utc(observation.observed_at)
        or _utc(observation.observed_at)
        > _payload_time(reconciliation.sweep_payload.get("retrieval_started_at"))
        or _payload_time(reconciliation.sweep_payload.get("retrieval_started_at"))
        > _payload_time(reconciliation.sweep_payload.get("retrieval_completed_at"))
        or _payload_time(reconciliation.sweep_payload.get("retrieval_completed_at"))
        > _utc(reconciliation.accepted_at)
        or _utc(predecessor_snapshot.accepted_at) > _utc(input_snapshot.decision_boundary)
        or _utc(input_snapshot.decision_boundary) > _utc(input_snapshot.observed_at)
        or _utc(input_snapshot.observed_at) > _utc(decision.created_at)
        or _utc(decision.created_at) > _utc(assessment.created_at)
        or _utc(assessment.created_at) > _utc(reconciliation.accepted_at)
        or _utc(certificate.created_at) < _utc(reconciliation.accepted_at)
        or _utc(assessment.created_at) < _utc(market_session.open_at)
        or _utc(assessment.created_at) > _utc(market_session.close_at)
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_LINEAGE_INVALID")
    _validate_intent_envelope(intent, assessment, position)
    if not attempts or tuple(item.attempt_ordinal for item in attempts) != tuple(
        range(len(attempts))
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_ATTEMPT_LINEAGE_INVALID")
    if tuple(certificate.attempt_ids) != tuple(item.client_order_id for item in attempts):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_ATTEMPT_LINEAGE_INVALID")
    final_attempt = attempts[-1]
    if (
        final_attempt.state != "FILLED"
        or final_attempt.filled_quantity != intent.quantity
        or final_attempt.broker_permit_id != permit.permit_id
        or observation.attempt_id != final_attempt.attempt_id
        or observation.attempt_ordinal != final_attempt.attempt_ordinal
        or any(
            item.execution_intent_id != intent.intent_id or item.quantity != intent.quantity
            for item in attempts
        )
        or any(item.filled_quantity for item in attempts[:-1])
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_ATTEMPT_LINEAGE_INVALID")
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
        not isinstance(observation.observed_payload, dict)
        or any(
            observation.observed_payload.get(key) != value
            for key, value in expected_observation.items()
        )
        or Decimal(str(observation.observed_payload.get("fill_cash_flow")))
        != Decimal(final_attempt.fill_cash_flow)
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_FINALIZATION_INVALID")
    all_activities = reconciliation.sweep_payload.get("activities")
    raw_inventory = reconciliation.sweep_payload.get("final_positions")
    if not isinstance(all_activities, list) or not isinstance(raw_inventory, list):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_RECONCILIATION_INVALID")
    prior_hashes = _validate_prior_activity_history(
        predecessor_snapshot.activity_manifest,
        all_activities,
    )
    activities = sorted(
        (
            item
            for item in all_activities
            if isinstance(item, dict)
            and item.get("activity_type") in {"OPTRD", "FILL"}
            and item.get("activity_id_hash") not in prior_hashes
        ),
        key=lambda item: str(item.get("activity_id_hash", "")),
    )
    _validate_terminal_legs(predecessor_snapshot.normalized_inventory, intent, thesis)
    if not _lifecycle_activities_match_attempts(
        activities,
        [item for item in attempts if item.filled_quantity > 0],
        intent.legs,
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_ACTIVITY_LINEAGE_INCOMPLETE")
    if any(
        _payload_time(item.get("occurred_at")) <= _utc(predecessor_snapshot.accepted_at)
        or _payload_time(item.get("occurred_at")) > _utc(reconciliation.accepted_at)
        for item in activities
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_ACTIVITY_TIME_INVALID")
    inventory, fingerprint = _validate_terminal_inventory(raw_inventory, intent, thesis)
    cashflow = sum(
        (item.fill_cash_flow or Decimal(0)) for item in attempts if item.filled_quantity > 0
    )
    return inventory, activities, cashflow, fingerprint


def _validate_intent_envelope(intent, assessment, position) -> None:
    payload = intent.envelope_payload
    if not isinstance(payload, dict) or set(payload) != {
        "action",
        "authorization_certificate_id",
        "policy_hash",
        "account_fingerprint",
        "position_or_book_fingerprint",
        "legs",
        "quantity",
        "minimum_limit",
        "maximum_limit",
        "approved_max_loss",
        "event_key",
        "trading_day",
        "market_session_id",
        "quoted_relative_spread",
        "maximum_relative_spread",
        "incremental_debit",
        "maximum_incremental_debit",
    }:
        raise LifecycleTerminalMaterializationError("LIFECYCLE_INTENT_ENVELOPE_INVALID")
    try:
        raw_legs = payload["legs"]
        if not isinstance(raw_legs, list):
            raise TypeError
        legs = tuple(
            OrderLegIntent(
                symbol=str(item["symbol"]),
                intent=PositionIntent(str(item["intent"])),
                ratio=int(item["ratio"]),
            )
            for item in raw_legs
            if isinstance(item, dict)
        )
        if len(legs) != len(raw_legs):
            raise TypeError
        envelope = OrderEnvelope(
            action=ExecutionAction(str(payload["action"])),
            authorization_certificate_id=UUID(str(payload["authorization_certificate_id"])),
            policy_hash=str(payload["policy_hash"]),
            account_fingerprint=str(payload["account_fingerprint"]),
            position_or_book_fingerprint=str(payload["position_or_book_fingerprint"]),
            legs=legs,
            quantity=int(payload["quantity"]),
            minimum_limit=Decimal(str(payload["minimum_limit"])),
            maximum_limit=Decimal(str(payload["maximum_limit"])),
            approved_max_loss=Decimal(str(payload["approved_max_loss"])),
            event_key=str(payload["event_key"]),
            trading_day=datetime.fromisoformat(str(payload["trading_day"])).date(),
            market_session_id=(
                UUID(str(payload["market_session_id"]))
                if payload["market_session_id"] is not None
                else None
            ),
            quoted_relative_spread=_optional_decimal(payload["quoted_relative_spread"]),
            maximum_relative_spread=_optional_decimal(payload["maximum_relative_spread"]),
            incremental_debit=_optional_decimal(payload["incremental_debit"]),
            maximum_incremental_debit=_optional_decimal(payload["maximum_incremental_debit"]),
        )
    except (KeyError, TypeError, ValueError):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_INTENT_ENVELOPE_INVALID") from None
    canonical_payload = {
        "action": envelope.action.value,
        "authorization_certificate_id": str(envelope.authorization_certificate_id),
        "policy_hash": envelope.policy_hash,
        "account_fingerprint": envelope.account_fingerprint,
        "position_or_book_fingerprint": envelope.position_or_book_fingerprint,
        "legs": [
            {"symbol": leg.symbol, "intent": leg.intent.value, "ratio": leg.ratio}
            for leg in envelope.legs
        ],
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
    if (
        payload != canonical_payload
        or envelope.action.value != intent.action
        or envelope.authorization_certificate_id != assessment.certificate_id
        or envelope.policy_hash != intent.policy_hash
        or envelope.account_fingerprint != position.account_fingerprint
        or envelope.position_or_book_fingerprint != intent.fingerprint
        or list(raw_legs) != intent.legs
        or envelope.quantity != intent.quantity
        or envelope.minimum_limit != Decimal(intent.minimum_limit)
        or envelope.maximum_limit != Decimal(intent.maximum_limit)
        or envelope.approved_max_loss != Decimal(intent.approved_max_loss)
        or envelope.market_session_id != intent.market_session_id
        or envelope.quoted_relative_spread != intent.quoted_relative_spread
        or envelope.maximum_relative_spread != intent.maximum_relative_spread
        or envelope.incremental_debit != intent.incremental_debit
        or envelope.maximum_incremental_debit != intent.maximum_incremental_debit
        or envelope.event_key != intent.event_key
        or envelope.trading_day != intent.trading_day
        or order_envelope_hash(envelope) != intent.envelope_hash
        or intent_digest(envelope) != intent.intent_digest
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_INTENT_ENVELOPE_INVALID")


def _optional_decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _decimal_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _validate_prior_activity_history(prior, current) -> set[str]:
    if not isinstance(prior, list):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_ACTIVITY_HISTORY_INVALID")
    prior_by_hash: dict[str, dict[str, object]] = {}
    current_by_hash: dict[str, dict[str, object]] = {}
    for collection, target in ((prior, prior_by_hash), (current, current_by_hash)):
        for item in collection:
            if not isinstance(item, dict):
                raise LifecycleTerminalMaterializationError("LIFECYCLE_ACTIVITY_HISTORY_INVALID")
            activity_hash = item.get("activity_id_hash")
            if (
                not isinstance(activity_hash, str)
                or _HASH.fullmatch(activity_hash) is None
                or activity_hash in target
            ):
                raise LifecycleTerminalMaterializationError("LIFECYCLE_ACTIVITY_HISTORY_INVALID")
            target[activity_hash] = item
    if any(current_by_hash.get(key) != value for key, value in prior_by_hash.items()):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_ACTIVITY_HISTORY_INVALID")
    return set(prior_by_hash)


def _validate_terminal_legs(predecessor_inventory, intent, thesis) -> None:
    close_legs = [
        item for item in intent.legs if item.get("intent") in {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}
    ]
    open_legs = [
        item for item in intent.legs if item.get("intent") in {"BUY_TO_OPEN", "SELL_TO_OPEN"}
    ]
    if (
        len(close_legs) != 2
        or (intent.action == "CLOSE" and open_legs)
        or (intent.action == "ROLL" and len(open_legs) != 2)
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_LEGS_INVALID")
    expected_close = {}
    old_contracts = []
    old_ratios = []
    for item in predecessor_inventory:
        match = _parse_occ(str(item.get("symbol", "")))
        try:
            quantity = Decimal(str(item["signed_quantity"]))
            ratio = abs(quantity) / Decimal(intent.quantity)
        except (KeyError, TypeError, ValueError):
            raise LifecycleTerminalMaterializationError("LIFECYCLE_LEGS_INVALID") from None
        if (
            match is None
            or item.get("kind") != "OPTION"
            or item.get("multiplier") != 100
            or ratio <= 0
            or ratio != ratio.to_integral_value()
        ):
            raise LifecycleTerminalMaterializationError("LIFECYCLE_LEGS_INVALID")
        expected_close[str(item["symbol"])] = (
            "SELL_TO_CLOSE" if quantity > 0 else "BUY_TO_CLOSE",
            int(ratio),
        )
        old_ratios.append(int(ratio))
        old_contracts.append(match)
    actual_close = {
        str(item.get("symbol")): (item.get("intent"), item.get("ratio")) for item in close_legs
    }
    if (
        len(expected_close) != 2
        or actual_close != expected_close
        or len(set(old_ratios)) != 1
        or old_contracts[0].strike_price == old_contracts[1].strike_price
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_LEGS_INVALID")
    if intent.action == "CLOSE":
        return
    new_contracts = [_parse_occ(str(item.get("symbol", ""))) for item in open_legs]
    try:
        open_ratios = [int(item["ratio"]) for item in open_legs]
    except (KeyError, TypeError, ValueError):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_LEGS_INVALID") from None
    if (
        any(item is None for item in new_contracts)
        or len(set(open_ratios)) != 1
        or open_ratios[0] != old_ratios[0]
        or new_contracts[0].strike_price == new_contracts[1].strike_price
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_LEGS_INVALID")
    contracts = old_contracts + new_contracts
    if (
        any(item.root_symbol != thesis.underlying for item in contracts)
        or len({item.right for item in contracts}) != 1
        or len({item.expiration_date for item in old_contracts}) != 1
        or len({item.expiration_date for item in new_contracts}) != 1
        or new_contracts[0].expiration_date <= old_contracts[0].expiration_date
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_LEGS_INVALID")


def _lifecycle_activities_match_attempts(activities, attempts, legs) -> bool:
    expected: dict[tuple[str, str, str], Decimal] = {}
    direction = {
        "BUY_TO_OPEN": 1,
        "BUY_TO_CLOSE": 1,
        "SELL_TO_OPEN": -1,
        "SELL_TO_CLOSE": -1,
    }
    for attempt in attempts:
        if not attempt.provider_order_id:
            return False
        for leg in legs:
            if leg.get("intent") not in direction:
                return False
            try:
                key = (
                    attempt.client_order_id,
                    attempt.provider_order_id,
                    str(leg["symbol"]),
                )
                expected[key] = expected.get(key, Decimal(0)) + Decimal(
                    attempt.filled_quantity * int(leg["ratio"]) * direction[str(leg["intent"])]
                )
            except (KeyError, TypeError, ValueError):
                return False
    observed: dict[tuple[str, str, str], Decimal] = {}
    hashes: set[str] = set()
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
            or activity_hash in hashes
            or key not in expected
            or not quantity.is_finite()
            or quantity == 0
        ):
            return False
        hashes.add(activity_hash)
        observed[key] = observed.get(key, Decimal(0)) + quantity
    return observed == expected


def _validate_terminal_inventory(inventory, intent, thesis):
    if intent.action == "CLOSE":
        if inventory:
            raise LifecycleTerminalMaterializationError("CLOSE_INVENTORY_NOT_EMPTY")
        return [], option_position_fingerprint(())
    open_legs = [
        item for item in intent.legs if item.get("intent") in {"BUY_TO_OPEN", "SELL_TO_OPEN"}
    ]
    expected = {
        str(item["symbol"]): Decimal(intent.quantity * int(item["ratio"]))
        * (1 if item["intent"] == "BUY_TO_OPEN" else -1)
        for item in open_legs
    }
    if len(expected) != 2 or len(inventory) != 2:
        raise LifecycleTerminalMaterializationError("ROLL_INVENTORY_INVALID")
    normalized = []
    parsed = []
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {
            "kind",
            "symbol",
            "signed_quantity",
            "multiplier",
        }:
            raise LifecycleTerminalMaterializationError("ROLL_INVENTORY_INVALID")
        match = _parse_occ(str(item.get("symbol", "")))
        quantity = Decimal(str(item.get("signed_quantity")))
        if (
            item.get("kind") != "OPTION"
            or match is None
            or item.get("multiplier") != 100
            or type(item.get("multiplier")) is not int
            or expected.get(str(item.get("symbol"))) != quantity
        ):
            raise LifecycleTerminalMaterializationError("ROLL_INVENTORY_INVALID")
        parsed.append((match, quantity, str(item["symbol"])))
        normalized.append(
            {
                "kind": "OPTION",
                "symbol": str(item["symbol"]),
                "signed_quantity": canonical_decimal(quantity),
                "multiplier": 100,
            }
        )
    if (
        [item[2] for item in parsed] != sorted(item[2] for item in parsed)
        or parsed[0][0].root_symbol != parsed[1][0].root_symbol
        or parsed[0][0].root_symbol != thesis.underlying
        or parsed[0][0].expiration_date != parsed[1][0].expiration_date
        or parsed[0][0].right != parsed[1][0].right
        or {parsed[0][1] > 0, parsed[1][1] > 0} != {True, False}
        or abs(parsed[0][1]) != abs(parsed[1][1])
        or parsed[0][0].strike_price == parsed[1][0].strike_price
    ):
        raise LifecycleTerminalMaterializationError("ROLL_INVENTORY_INVALID")
    return normalized, option_position_fingerprint(
        tuple((item["symbol"], Decimal(item["signed_quantity"]), 100) for item in normalized)
    )


def _parse_occ(symbol: str) -> OptionContractSymbol | None:
    try:
        return parse_standard_option_contract_symbol(symbol)
    except OptionContractSymbolError as error:
        if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
            raise LifecycleTerminalMaterializationError(error.code) from error
        return None


def _validate_existing_terminal(
    session,
    *,
    position,
    transition,
    snapshot,
    transition_values,
    snapshot_values,
) -> None:
    if snapshot is None:
        raise LifecycleTerminalMaterializationError("LIFECYCLE_MATERIALIZATION_CONFLICT")
    expected_transition = tuple(_comparable(value) for value in transition_values.values()) + (
        _row_hash(session, transition_values),
    )
    durable_transition = tuple(
        _comparable(getattr(transition, key)) for key in transition_values
    ) + (transition.transition_hash,)
    expected_projection = tuple(_comparable(value) for value in snapshot_values.values()) + (
        _row_hash(session, snapshot_values),
    )
    durable_projection = tuple(_comparable(getattr(snapshot, key)) for key in snapshot_values) + (
        snapshot.snapshot_hash,
    )
    expected_closed = (
        transition_values["occurred_at"] if transition_values["action"] == "CLOSE" else None
    )
    if (
        durable_transition != expected_transition
        or durable_projection != expected_projection
        or position.current_snapshot_id != snapshot.snapshot_id
        or position.current_reconciliation_state_id != snapshot.reconciliation_state_id
        or position.active_position_fingerprint != snapshot.position_fingerprint
        or (
            (_utc(position.closed_at) if position.closed_at is not None else None)
            != expected_closed
        )
    ):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_MATERIALIZATION_CONFLICT")


def _comparable(value):
    return _utc(value) if hasattr(value, "tzinfo") else value


def _payload_time(value) -> datetime:
    if not isinstance(value, str):
        raise LifecycleTerminalMaterializationError("LIFECYCLE_RECONCILIATION_TIME_INVALID")
    return _utc(datetime.fromisoformat(value))


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
            if isinstance(value, UUID | Decimal)
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
