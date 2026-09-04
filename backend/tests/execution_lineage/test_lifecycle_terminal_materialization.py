from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from pg8000.dbapi import ProgrammingError as PGProgrammingError
from sqlalchemy import create_engine, event, insert, inspect, select, text
from sqlalchemy.exc import DatabaseError as SQLDatabaseError
from sqlalchemy.exc import ProgrammingError as SQLProgrammingError

from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.execution import ExecutionBlocked, intent_digest, order_envelope_hash
from backend.app.execution.models import ExecutionAction, OrderEnvelope, OrderLegIntent
from backend.app.experiment_lineage import ExperimentExecutionLineage
from backend.app.lifecycle.fingerprint import option_position_fingerprint
from backend.app.lifecycle.materialization import SQLAlchemyEntryMaterializer
from backend.app.lifecycle.repository import (
    LifecyclePersistenceError,
    SQLAlchemyLifecycleRepository,
)
from backend.app.lifecycle.terminal_materialization import (
    LifecycleTerminalMaterializationError,
    SQLAlchemyLifecycleTerminalMaterializer,
    _final_reconciliation_request_hash,
    _validate_terminal_inventory,
)
from backend.app.persistence.agent_repository import AgentDecisionRepository
from backend.app.persistence.runtime import apply_migrations, discover_migrations
from backend.app.persistence.sqlalchemy_models import (
    AccountReconciliationStateRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AgentTickRow,
    AssessmentCertificateRow,
    AttemptObservationRow,
    Base,
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
from backend.app.services import ObservedPaperAccountAuthority
from backend.tests.execution_lineage.test_entry_materialization import (
    CERTIFICATE_ID as ENTRY_CERTIFICATE_ID,
)
from backend.tests.execution_lineage.test_entry_materialization import (
    ENTRY_AT,
    EXPERIMENT_LINEAGE,
    _repository,
)
from backend.tests.execution_lineage.test_entry_materialization import (
    STATE_ID as ENTRY_STATE_ID,
)
from backend.tests.runtime_composition.test_development_acquisition import (
    FINGERPRINT,
    POLICY_HASH,
)

TERMINAL_AT = ENTRY_AT + timedelta(minutes=12)
ASSESSMENT_ID = UUID("90000000-0000-0000-0000-000000000001")
ASSESSMENT_CERTIFICATE_ID = UUID("90000000-0000-0000-0000-000000000002")
INPUT_ID = UUID("90000000-0000-0000-0000-000000000003")
DECISION_ID = UUID("90000000-0000-0000-0000-000000000004")
MANIFEST_ID = UUID("90000000-0000-0000-0000-000000000005")
ACCOUNT_OBSERVATION_ID = UUID("90000000-0000-0000-0000-000000000013")
BINDING_ID = UUID("90000000-0000-0000-0000-000000000006")
INTENT_ID = UUID("90000000-0000-0000-0000-000000000007")
ATTEMPT_ID = UUID("90000000-0000-0000-0000-000000000008")
PERMIT_ID = UUID("90000000-0000-0000-0000-000000000009")
OBSERVATION_ID = UUID("90000000-0000-0000-0000-000000000010")
RECONCILIATION_ID = UUID("90000000-0000-0000-0000-000000000011")
STATE_ID = UUID("90000000-0000-0000-0000-000000000012")
CLIENT_ID = str(uuid5(NAMESPACE_URL, "test-fixture-lifecycle-client-order-a0"))
PROVIDER_ID = str(uuid5(NAMESPACE_URL, "test-fixture-lifecycle-provider-order-a0"))
POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"


def test_terminal_materializer_rejects_adjusted_roll_inventory_with_stable_reason() -> None:
    legs = [
        {"symbol": "PANW1260918C00300000", "intent": "BUY_TO_OPEN", "ratio": 1},
        {"symbol": "PANW1260918C00310000", "intent": "SELL_TO_OPEN", "ratio": 1},
    ]
    inventory = [
        {
            "kind": "OPTION",
            "symbol": "PANW1260918C00300000",
            "signed_quantity": "1",
            "multiplier": 100,
        },
        {
            "kind": "OPTION",
            "symbol": "PANW1260918C00310000",
            "signed_quantity": "-1",
            "multiplier": 100,
        },
    ]

    with pytest.raises(LifecycleTerminalMaterializationError) as raised:
        _validate_terminal_inventory(
            inventory,
            SimpleNamespace(action="ROLL", quantity=1, legs=legs),
            SimpleNamespace(underlying="PANW"),
        )

    assert str(raised.value) == "NON_STANDARD_CONTRACT_UNSUPPORTED"


class _TerminalClock:
    def now(self, _session) -> datetime:
        return TERMINAL_AT + timedelta(minutes=1)


def _copy_fixture_rows(source, target) -> None:
    target_schema = inspect(target)
    for table in Base.metadata.sorted_tables:
        if not target_schema.has_table(table.name):
            continue
        target_columns = {column["name"] for column in target_schema.get_columns(table.name)}
        rows = [
            {key: value for key, value in dict(row._mapping).items() if key in target_columns}
            for row in source.execute(select(table))
        ]
        if rows:
            target.execute(insert(table), rows)


def _seed_terminal(
    action: str,
    account_role: AccountRole = AccountRole.DEVELOPMENT,
    experiment_lineage: ExperimentExecutionLineage | None = None,
):
    sessions, engine, retained = _repository(account_role, experiment_lineage)
    materialized_position = SQLAlchemyEntryMaterializer(sessions).materialize(
        execution_certificate_id=ENTRY_CERTIFICATE_ID,
        launch_authority=retained.launch_authority,
    )
    with sessions.begin() as session:
        position = session.get(ManagedLifecyclePositionRow, materialized_position)
        predecessor = session.get(ManagedPositionSnapshotRow, position.current_snapshot_id)
        prior_state = session.get(AccountReconciliationStateRow, ENTRY_STATE_ID)
        prior_inventory = predecessor.normalized_inventory
        prior_activities = predecessor.activity_manifest
        if action == "CLOSE":
            legs = [
                {
                    "symbol": item["symbol"],
                    "intent": "SELL_TO_CLOSE"
                    if Decimal(item["signed_quantity"]) > 0
                    else "BUY_TO_CLOSE",
                    "ratio": 1,
                }
                for item in prior_inventory
            ]
            inventory = []
        else:
            next_symbols = (
                "NVDA260925C00170000",
                "NVDA260925C00180000",
            )
            close_legs = [
                {
                    "symbol": item["symbol"],
                    "intent": "SELL_TO_CLOSE"
                    if Decimal(item["signed_quantity"]) > 0
                    else "BUY_TO_CLOSE",
                    "ratio": 1,
                }
                for item in prior_inventory
            ]
            open_legs = [
                {"symbol": next_symbols[0], "intent": "BUY_TO_OPEN", "ratio": 1},
                {"symbol": next_symbols[1], "intent": "SELL_TO_OPEN", "ratio": 1},
            ]
            legs = close_legs + open_legs
            inventory = [
                {
                    "kind": "OPTION",
                    "symbol": next_symbols[0],
                    "signed_quantity": "1",
                    "multiplier": 100,
                },
                {
                    "kind": "OPTION",
                    "symbol": next_symbols[1],
                    "signed_quantity": "-1",
                    "multiplier": 100,
                },
            ]
        event_key = f"lifecycle-{action.lower()}"
        envelope = OrderEnvelope(
            action=ExecutionAction(action),
            authorization_certificate_id=ASSESSMENT_CERTIFICATE_ID,
            policy_hash=POLICY_HASH,
            account_fingerprint=FINGERPRINT,
            position_or_book_fingerprint=predecessor.position_fingerprint,
            legs=tuple(
                OrderLegIntent(
                    symbol=item["symbol"],
                    intent=PositionIntent(item["intent"]),
                    ratio=item["ratio"],
                )
                for item in legs
            ),
            quantity=1,
            minimum_limit=Decimal("-5"),
            maximum_limit=Decimal("5"),
            approved_max_loss=Decimal("500"),
            event_key=event_key,
            trading_day=TERMINAL_AT.date(),
            market_session_id=(
                uuid5(NAMESPACE_URL, "test-fixture-market-session-220")
                if action == "ROLL"
                else None
            ),
            quoted_relative_spread=(Decimal("0.05") if action == "ROLL" else None),
            maximum_relative_spread=(Decimal("0.25") if action == "ROLL" else None),
            incremental_debit=(Decimal("100") if action == "ROLL" else None),
            maximum_incremental_debit=(Decimal("500") if action == "ROLL" else None),
        )
        digest = intent_digest(envelope)
        envelope_hash = order_envelope_hash(envelope)
        certificate_id = uuid5(NAMESPACE_URL, f"alphadecay:execution:{digest}")
        envelope_payload = {
            "action": action,
            "authorization_certificate_id": str(ASSESSMENT_CERTIFICATE_ID),
            "policy_hash": POLICY_HASH,
            "account_fingerprint": FINGERPRINT,
            "position_or_book_fingerprint": predecessor.position_fingerprint,
            "legs": legs,
            "quantity": 1,
            "minimum_limit": "-5",
            "maximum_limit": "5",
            "approved_max_loss": "500",
            "event_key": event_key,
            "trading_day": TERMINAL_AT.date().isoformat(),
            "market_session_id": str(envelope.market_session_id) if action == "ROLL" else None,
            "quoted_relative_spread": "0.05" if action == "ROLL" else None,
            "maximum_relative_spread": "0.25" if action == "ROLL" else None,
            "incremental_debit": "100" if action == "ROLL" else None,
            "maximum_incremental_debit": "500" if action == "ROLL" else None,
        }
        activities = []
        direction = {
            "BUY_TO_OPEN": 1,
            "BUY_TO_CLOSE": 1,
            "SELL_TO_OPEN": -1,
            "SELL_TO_CLOSE": -1,
        }
        for index, leg in enumerate(legs):
            activities.append(
                {
                    "activity_id_hash": f"{index + 1:x}" * 64,
                    "activity_type": "OPTRD",
                    "occurred_at": TERMINAL_AT.isoformat(),
                    "symbol": leg["symbol"],
                    "signed_quantity": str(direction[leg["intent"]]),
                    "provider_order_id": PROVIDER_ID,
                    "client_order_id": CLIENT_ID,
                    "time_quality": "EXACT_TRANSACTION_TIME",
                    "provider_activity_type": "fill",
                }
            )
        manifest_hash = "1" * 64
        session.add(
            LifecycleAccountObservationRow(
                observation_id=ACCOUNT_OBSERVATION_ID,
                managed_position_id=materialized_position,
                managed_snapshot_id=predecessor.snapshot_id,
                account_role=account_role.value,
                account_fingerprint=FINGERPRINT,
                sweep_payload={},
                sweep_hash="2" * 64,
                retrieval_started_at=TERMINAL_AT - timedelta(minutes=6),
                retrieval_completed_at=TERMINAL_AT - timedelta(minutes=5),
                accepted_at=TERMINAL_AT - timedelta(minutes=5),
            )
        )
        session.add(
            LifecycleObservationManifestRow(
                manifest_id=MANIFEST_ID,
                manifest_hash=manifest_hash,
                agent_input_snapshot_id=None,
                account_observation_id=ACCOUNT_OBSERVATION_ID,
                managed_position_id=materialized_position,
                managed_snapshot_id=predecessor.snapshot_id,
                reconciliation_id=None,
                greek_authority_id=retained.greek_authority.authority_id,
                sweep_hash="2" * 64,
                account_manifest={},
                activity_manifest=prior_activities,
                option_manifest=[],
                atm_iv_manifest={},
                underlying_manifest={},
                boundary_manifest={},
                research_manifest=[],
                source_authority_manifest=None,
                observed_at=TERMINAL_AT - timedelta(minutes=5),
                created_at=TERMINAL_AT - timedelta(minutes=5),
            )
        )
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=INPUT_ID,
                thesis_version_id=retained.thesis_version_id,
                account_role=account_role.value,
                account_fingerprint=FINGERPRINT,
                decision_kind="ASSESSMENT",
                decision_boundary=TERMINAL_AT - timedelta(minutes=6),
                observed_at=TERMINAL_AT - timedelta(minutes=5),
                normalized_payload={
                    "acquisition_manifest_id": str(MANIFEST_ID),
                    "acquisition_manifest_hash": manifest_hash,
                },
                input_hash="3" * 64,
                created_at=TERMINAL_AT - timedelta(minutes=5),
            )
        )
        session.add(
            LifecycleObservationBindingRow(
                binding_id=BINDING_ID,
                manifest_id=MANIFEST_ID,
                agent_input_snapshot_id=INPUT_ID,
                created_at=TERMINAL_AT - timedelta(minutes=4),
            )
        )
        outcome = "CLOSE_APPROVED" if action == "CLOSE" else "ROLL_APPROVED"
        expected_exposure = (
            None
            if action == "CLOSE"
            else {
                "delta": "30",
                "gamma": "2",
                "theta_per_day": "-3",
                "vega_per_iv_point": "5",
            }
        )
        session.add(
            AgentDecisionRow(
                decision_id=DECISION_ID,
                thesis_version_id=retained.thesis_version_id,
                origin_tick_id=uuid5(NAMESPACE_URL, "test-fixture-terminal-tick-202"),
                input_snapshot_id=INPUT_ID,
                account_role=account_role.value,
                account_fingerprint=FINGERPRINT,
                decision_kind="ASSESSMENT",
                outcome=outcome,
                reason_code=outcome,
                policy_hash=POLICY_HASH,
                result_payload={},
                result_hash="4" * 64,
                autonomy_authorized=True,
                decision_boundary=TERMINAL_AT - timedelta(minutes=6),
                created_at=TERMINAL_AT - timedelta(minutes=4),
                experiment_id=(
                    experiment_lineage.experiment_id if experiment_lineage is not None else None
                ),
                experiment_source_definition_hash=(
                    experiment_lineage.source_definition_hash
                    if experiment_lineage is not None
                    else None
                ),
                experiment_protocol_hash=(
                    experiment_lineage.protocol_hash if experiment_lineage is not None else None
                ),
            )
        )
        session.add(
            AssessmentCertificateRow(
                certificate_id=ASSESSMENT_CERTIFICATE_ID,
                thesis_version_id=retained.thesis_version_id,
                agent_decision_id=DECISION_ID,
                assessment_id=ASSESSMENT_ID,
                account_role=account_role.value,
                action=action,
                position_fingerprint=predecessor.position_fingerprint,
                envelope_hash=envelope_hash,
                approved_max_loss=Decimal("500"),
                quantity=1,
                expected_after_exposure=expected_exposure,
                policy_hash=POLICY_HASH,
                created_at=TERMINAL_AT - timedelta(minutes=3),
                expires_at=TERMINAL_AT + timedelta(minutes=3),
                valid=True,
                experiment_id=(
                    experiment_lineage.experiment_id if experiment_lineage is not None else None
                ),
                experiment_source_definition_hash=(
                    experiment_lineage.source_definition_hash
                    if experiment_lineage is not None
                    else None
                ),
                experiment_protocol_hash=(
                    experiment_lineage.protocol_hash if experiment_lineage is not None else None
                ),
            )
        )
        session.add(
            ExecutionIntentRow(
                intent_id=INTENT_ID,
                account_role=account_role.value,
                intent_digest=digest,
                action=action,
                policy_hash=POLICY_HASH,
                event_key=event_key,
                trading_day=TERMINAL_AT.date(),
                entry_approval_id=None,
                assessment_certificate_id=ASSESSMENT_CERTIFICATE_ID,
                fingerprint=predecessor.position_fingerprint,
                envelope_hash=envelope_hash,
                envelope_payload=envelope_payload,
                legs=legs,
                quantity=1,
                minimum_limit=Decimal("-5"),
                maximum_limit=Decimal("5"),
                approved_max_loss=Decimal("500"),
                market_session_id=envelope.market_session_id,
                quoted_relative_spread=envelope.quoted_relative_spread,
                maximum_relative_spread=envelope.maximum_relative_spread,
                incremental_debit=envelope.incremental_debit,
                maximum_incremental_debit=envelope.maximum_incremental_debit,
                state="TERMINAL",
                first_fill_consumed=True,
            )
        )
        session.add(
            OrderAttemptRow(
                attempt_id=ATTEMPT_ID,
                broker_permit_id=PERMIT_ID,
                execution_intent_id=INTENT_ID,
                attempt_ordinal=0,
                client_order_id=CLIENT_ID,
                provider_order_id=PROVIDER_ID,
                state="FILLED",
                request_hash="6" * 64,
                quote_source_timestamps=[],
                filled_quantity=1,
                quantity=1,
                fill_cash_flow=Decimal("125"),
            )
        )
        expectation = {
            "purpose": "SUBMIT",
            "intent_id": str(INTENT_ID),
            "intent_digest": digest,
            "attempt_ordinal": 0,
            "request_hash": _final_reconciliation_request_hash(
                digest,
                0,
                "0" * 63 + "1",
            ),
            "expected_open_orders": [],
            "expected_cash": "99625",
        }
        sweep = {
            "final_positions": inventory,
            "activities": prior_activities + activities,
            "retrieval_started_at": TERMINAL_AT.isoformat(),
            "retrieval_completed_at": TERMINAL_AT.isoformat(),
        }
        session.add(
            WholeAccountReconciliationRow(
                reconciliation_id=RECONCILIATION_ID,
                reconciliation_hash="9" * 64,
                expectation_hash="a" * 64,
                execution_intent_id=INTENT_ID,
                intent_digest=digest,
                account_role=account_role.value,
                account_fingerprint=FINGERPRINT,
                purpose="SUBMIT",
                attempt_ordinal=0,
                request_hash=expectation["request_hash"],
                accepted_at=TERMINAL_AT,
                expectation_payload=expectation,
                sweep_payload=sweep,
                positions_manifest_hash="b" * 64,
                orders_manifest_hash="c" * 64,
                activities_manifest_hash="d" * 64,
                safe=True,
                block_codes=[],
            )
        )
        session.add(
            AccountReconciliationStateRow(
                state_id=STATE_ID,
                account_role=account_role.value,
                sequence=prior_state.sequence + 1,
                account_fingerprint=FINGERPRINT,
                baseline_id=prior_state.baseline_id,
                baseline_captured_at=prior_state.baseline_captured_at,
                accepted_at=TERMINAL_AT,
                expected_cash=Decimal("99625"),
                expected_positions=inventory,
                expected_open_orders=[],
                known_activities=prior_activities + activities,
                activity_complete_through=TERMINAL_AT,
                resolved_activity_hashes=[
                    item["activity_id_hash"] for item in prior_activities + activities
                ],
                predecessor_state_id=predecessor.reconciliation_state_id,
                authority_reconciliation_id=RECONCILIATION_ID,
                authority_permit_id=PERMIT_ID,
                authority_observation_id=OBSERVATION_ID,
                authority_permit_request_hash="6" * 64,
                transition_hash="e" * 64,
                state_hash="f" * 64,
            )
        )
        session.add(
            BrokerMutationPermitRow(
                permit_id=PERMIT_ID,
                reconciliation_id=RECONCILIATION_ID,
                execution_intent_id=INTENT_ID,
                intent_digest=digest,
                claim_token=uuid5(NAMESPACE_URL, "test-fixture-terminal-claim-203"),
                claim_generation=2,
                execution_epoch=0,
                mutation_kind="SUBMIT",
                attempt_ordinal=0,
                permit_generation=1,
                request_hash="6" * 64,
                limit_price=Decimal("1.25"),
                quote_source_timestamps=[],
                issued_at=TERMINAL_AT - timedelta(minutes=2),
                expires_at=TERMINAL_AT + timedelta(minutes=1),
                state="CONSUMED",
                dispatch_nonce=uuid5(NAMESPACE_URL, "test-fixture-terminal-dispatch-204"),
                dispatch_acquired_at=TERMINAL_AT - timedelta(minutes=1),
                consumed_at=TERMINAL_AT,
                outcome_hash="9" * 64,
            )
        )
        session.add(
            AttemptObservationRow(
                observation_id=OBSERVATION_ID,
                permit_id=PERMIT_ID,
                execution_intent_id=INTENT_ID,
                attempt_id=ATTEMPT_ID,
                attempt_ordinal=0,
                observation_sequence=1,
                source="DISPATCH_OUTCOME",
                provider_present=True,
                observed_payload={
                    "intent_id": str(INTENT_ID),
                    "ordinal": 0,
                    "client_order_id": CLIENT_ID,
                    "request_hash": "6" * 64,
                    "state": "FILLED",
                    "provider_order_id": PROVIDER_ID,
                    "filled_quantity": 1,
                    "quantity": 1,
                    "fill_cash_flow": "125",
                },
                observed_at=TERMINAL_AT,
                observation_hash="0" * 63 + "1",
            )
        )
        session.add(
            ExecutionCertificateRow(
                certificate_id=certificate_id,
                execution_intent_id=INTENT_ID,
                entry_approval_id=None,
                assessment_certificate_id=ASSESSMENT_CERTIFICATE_ID,
                execution_status="FILLED",
                attempt_ids=[CLIENT_ID],
                actual_exposure=expected_exposure,
                reconciliation_checks=[
                    "TERMINAL",
                    "REMAINDER_ABSENT",
                    "WHOLE_ACCOUNT_RECONCILED",
                ],
                created_at=TERMINAL_AT,
                reconciliation_id=RECONCILIATION_ID,
                reconciliation_hash="9" * 64,
                last_observation_hash="0" * 63 + "1",
            )
        )
    return sessions, engine, materialized_position, certificate_id, inventory


def _complete_failed_materialization_tick(sessions, certificate_id: UUID):
    tick_reference = uuid5(NAMESPACE_URL, "test-fixture-terminal-tick-202")
    reservation_token = uuid5(NAMESPACE_URL, "test-fixture-terminal-reservation-205")
    tick_boundary = TERMINAL_AT - timedelta(minutes=7)
    with sessions.begin() as session:
        session.add(
            AgentTickRow(
                tick_id=tick_reference,
                account_role="DEVELOPMENT",
                account_fingerprint=FINGERPRINT,
                tick_key="lifecycle-terminal-materialization-fixture",
                tick_boundary=tick_boundary,
                actor="SCHEDULER",
                status="RESERVED",
                reservation_token=reservation_token,
                terminal_code=None,
                decision_id=None,
                execution_certificate_id=None,
                proof_hash=None,
                created_at=tick_boundary,
                completed_at=None,
            )
        )
    with sessions.begin() as session:
        session.get(AgentTickRow, tick_reference).decision_id = DECISION_ID
    return AgentDecisionRepository(
        sessions,
        database_clock=_TerminalClock(),
        server_autonomy_enabled=True,
    ).complete_tick(
        tick_id=tick_reference,
        reservation_token=reservation_token,
        terminal_code="LIFECYCLE_FILLED_MATERIALIZATION_FAILED",
        decision_id=DECISION_ID,
        execution_certificate_id=certificate_id,
    )


@pytest.mark.parametrize("action", ("ROLL", "CLOSE"))
def test_sqlite_tick_retains_filled_lifecycle_certificate_when_materialization_fails(
    action: str,
) -> None:
    sessions, engine, _, certificate_id, _ = _seed_terminal(action)

    completed = _complete_failed_materialization_tick(sessions, certificate_id)

    assert completed.terminal_code == "LIFECYCLE_FILLED_MATERIALIZATION_FAILED"
    assert completed.execution_certificate_id == certificate_id
    engine.dispose()


@pytest.mark.parametrize("substitution", ("assessment", "action"))
def test_sqlite_tick_rejects_substituted_lifecycle_certificate_lineage(
    substitution: str,
) -> None:
    sessions, engine, _, certificate_id, _ = _seed_terminal("CLOSE")
    with sessions.begin() as session:
        if substitution == "assessment":
            session.get(ExecutionCertificateRow, certificate_id).assessment_certificate_id = UUID(
                "90000000-0000-0000-0000-000000000099"
            )
        else:
            session.get(AssessmentCertificateRow, ASSESSMENT_CERTIFICATE_ID).action = "ROLL"

    with pytest.raises(ExecutionBlocked, match="AGENT_TICK_CERTIFICATE_MISMATCH"):
        _complete_failed_materialization_tick(sessions, certificate_id)

    with sessions() as session:
        tick = session.get(AgentTickRow, uuid5(NAMESPACE_URL, "test-fixture-terminal-tick-202"))
        assert tick.status == "RESERVED"
        assert tick.execution_certificate_id is None
    engine.dispose()


@pytest.mark.parametrize("action", ("ROLL", "CLOSE"))
def test_filled_terminal_materialization_is_exactly_idempotent(action: str) -> None:
    sessions, engine, position_id, certificate_id, inventory = _seed_terminal(action)
    materializer = SQLAlchemyLifecycleTerminalMaterializer(sessions)

    first = materializer.materialize(execution_certificate_id=certificate_id)
    second = materializer.materialize(execution_certificate_id=certificate_id)

    assert first == second
    with sessions() as session:
        position = session.get(ManagedLifecyclePositionRow, position_id)
        transitions = session.scalars(
            select(ManagedPositionTransitionRow)
            .where(ManagedPositionTransitionRow.managed_position_id == position_id)
            .order_by(ManagedPositionTransitionRow.transition_sequence)
        ).all()
        snapshot = session.get(ManagedPositionSnapshotRow, position.current_snapshot_id)
        assert [item.action for item in transitions] == ["ENTRY", action]
        assert snapshot.normalized_inventory == inventory
        assert snapshot.cumulative_cashflow == Decimal("-375")
        closed_at = (
            position.closed_at.replace(tzinfo=UTC)
            if position.closed_at is not None and position.closed_at.tzinfo is None
            else position.closed_at
        )
        assert closed_at == (TERMINAL_AT if action == "CLOSE" else None)
        assert position.active_position_fingerprint == option_position_fingerprint(
            tuple(
                (
                    item["symbol"],
                    Decimal(item["signed_quantity"]),
                    item["multiplier"],
                )
                for item in inventory
            )
        )
    authority = ObservedPaperAccountAuthority(
        AccountRole.DEVELOPMENT,
        FINGERPRINT,
        True,
        True,
    )
    if action == "ROLL":
        loaded = SQLAlchemyLifecycleRepository(sessions).load(authority)
        assert loaded.current_snapshot_id == snapshot.snapshot_id
    else:
        with pytest.raises(LifecyclePersistenceError, match="ACTIVE_POSITION_NOT_UNIQUE"):
            SQLAlchemyLifecycleRepository(sessions).load(authority)
    engine.dispose()


def test_terminal_materialization_accepts_matching_experiment_lineage() -> None:
    sessions, engine, position_id, certificate_id, _ = _seed_terminal(
        "ROLL",
        experiment_lineage=EXPERIMENT_LINEAGE,
    )

    SQLAlchemyLifecycleTerminalMaterializer(sessions).materialize(
        execution_certificate_id=certificate_id
    )

    with sessions() as session:
        position = session.get(ManagedLifecyclePositionRow, position_id)
        assert position is not None
        assert position.experiment_id == EXPERIMENT_LINEAGE.experiment_id
        assert position.experiment_protocol_hash == EXPERIMENT_LINEAGE.protocol_hash
    engine.dispose()


def test_terminal_materialization_rejects_mismatched_experiment_lineage() -> None:
    sessions, engine, _, certificate_id, _ = _seed_terminal(
        "ROLL",
        experiment_lineage=EXPERIMENT_LINEAGE,
    )
    with sessions.begin() as session:
        assessment = session.get(AssessmentCertificateRow, ASSESSMENT_CERTIFICATE_ID)
        assert assessment is not None
        assessment.experiment_protocol_hash = "8" * 64

    with pytest.raises(
        LifecycleTerminalMaterializationError,
        match="LIFECYCLE_LINEAGE_INVALID",
    ):
        SQLAlchemyLifecycleTerminalMaterializer(sessions).materialize(
            execution_certificate_id=certificate_id
        )
    engine.dispose()


@pytest.mark.parametrize("action", ("ROLL", "CLOSE"))
def test_submission_lifecycle_fill_materializes_for_exact_account_role(action: str) -> None:
    sessions, engine, position_id, certificate_id, _ = _seed_terminal(
        action,
        AccountRole.SUBMISSION,
    )

    SQLAlchemyLifecycleTerminalMaterializer(sessions).materialize(
        execution_certificate_id=certificate_id
    )

    with sessions() as session:
        position = session.get(ManagedLifecyclePositionRow, position_id)
        assert position.account_role == AccountRole.SUBMISSION.value
        assert (position.closed_at is not None) == (action == "CLOSE")
        transitions = session.scalars(
            select(ManagedPositionTransitionRow)
            .where(ManagedPositionTransitionRow.managed_position_id == position_id)
            .order_by(ManagedPositionTransitionRow.transition_sequence)
        ).all()
        assert [item.action for item in transitions] == ["ENTRY", action]
    engine.dispose()


@pytest.mark.parametrize(
    "row",
    ("decision", "thesis", "predecessor-state", "predecessor-fingerprint"),
)
def test_submission_lifecycle_rejects_cross_role_lineage(row: str) -> None:
    sessions, engine, _, certificate_id, _ = _seed_terminal(
        "CLOSE",
        AccountRole.SUBMISSION,
    )
    with sessions.begin() as session:
        if row == "decision":
            target = session.get(AgentDecisionRow, DECISION_ID)
        elif row == "thesis":
            assessment = session.get(AssessmentCertificateRow, ASSESSMENT_CERTIFICATE_ID)
            assert assessment is not None
            target = session.get(ThesisVersionRow, assessment.thesis_version_id)
        else:
            position = session.scalar(select(ManagedLifecyclePositionRow))
            assert position is not None
            snapshot = session.get(ManagedPositionSnapshotRow, position.current_snapshot_id)
            assert snapshot is not None
            target = session.get(
                AccountReconciliationStateRow,
                snapshot.reconciliation_state_id,
            )
        assert target is not None
        if row == "predecessor-fingerprint":
            target.account_fingerprint = "f" * 64
        else:
            target.account_role = AccountRole.DEVELOPMENT.value

    with pytest.raises(
        LifecycleTerminalMaterializationError,
        match="LIFECYCLE_LINEAGE_INVALID",
    ):
        SQLAlchemyLifecycleTerminalMaterializer(sessions).materialize(
            execution_certificate_id=certificate_id
        )
    engine.dispose()


@pytest.mark.parametrize(
    "status",
    ("REJECTED", "CANCELED", "EXPIRED", "UNFILLED", "PARTIAL_CANCELED_RECONCILED"),
)
def test_nonfilled_terminal_certificate_never_mutates_managed_position(status: str) -> None:
    sessions, engine, position_id, certificate_id, _ = _seed_terminal("CLOSE")
    with sessions.begin() as session:
        session.get(ExecutionCertificateRow, certificate_id).execution_status = status
    with pytest.raises(
        LifecycleTerminalMaterializationError,
        match="LIFECYCLE_EXECUTION_NOT_FULLY_FILLED",
    ):
        SQLAlchemyLifecycleTerminalMaterializer(sessions).materialize(
            execution_certificate_id=certificate_id
        )
    with sessions() as session:
        position = session.get(ManagedLifecyclePositionRow, position_id)
        assert position.closed_at is None
        assert len(session.scalars(select(ManagedPositionTransitionRow)).all()) == 1
    engine.dispose()


@pytest.mark.parametrize(
    "mutate",
    (
        lambda session, certificate_id: setattr(
            session.get(AssessmentCertificateRow, ASSESSMENT_CERTIFICATE_ID),
            "envelope_hash",
            "a" * 64,
        ),
        lambda session, certificate_id: setattr(
            session.get(AgentDecisionRow, DECISION_ID), "policy_hash", "a" * 64
        ),
        lambda session, certificate_id: setattr(
            session.get(AgentInputSnapshotRow, INPUT_ID),
            "normalized_payload",
            {
                "acquisition_manifest_id": str(
                    uuid5(NAMESPACE_URL, "test-fixture-acquisition-manifest-999")
                ),
                "acquisition_manifest_hash": "1" * 64,
            },
        ),
        lambda session, certificate_id: setattr(
            session.get(WholeAccountReconciliationRow, RECONCILIATION_ID),
            "reconciliation_hash",
            "a" * 64,
        ),
        lambda session, certificate_id: setattr(
            session.get(BrokerMutationPermitRow, PERMIT_ID), "request_hash", "a" * 64
        ),
        lambda session, certificate_id: setattr(
            session.get(AttemptObservationRow, OBSERVATION_ID),
            "observation_hash",
            "a" * 64,
        ),
        lambda session, certificate_id: setattr(
            session.get(OrderAttemptRow, ATTEMPT_ID), "client_order_id", "changed-client"
        ),
        lambda session, certificate_id: setattr(
            session.get(ExecutionIntentRow, INTENT_ID), "envelope_payload", {}
        ),
        lambda session, certificate_id: setattr(
            session.get(ExecutionIntentRow, INTENT_ID),
            "envelope_payload",
            {
                **session.get(ExecutionIntentRow, INTENT_ID).envelope_payload,
                "quantity": "1",
            },
        ),
        lambda session, certificate_id: setattr(
            session.get(AgentDecisionRow, DECISION_ID),
            "decision_boundary",
            TERMINAL_AT - timedelta(minutes=4),
        ),
        lambda session, certificate_id: setattr(
            session.get(AssessmentCertificateRow, ASSESSMENT_CERTIFICATE_ID),
            "expected_after_exposure",
            None,
        ),
        lambda session, certificate_id: setattr(
            session.get(
                ManagedLifecyclePositionRow,
                session.scalar(select(ManagedLifecyclePositionRow.managed_position_id)),
            ),
            "account_fingerprint",
            "f" * 64,
        ),
    ),
    ids=(
        "assessment",
        "policy",
        "input",
        "reconciliation",
        "permit",
        "observation",
        "attempt",
        "intent-envelope",
        "intent-envelope-canonical-form",
        "decision-boundary",
        "roll-exposure",
        "account-position",
    ),
)
def test_terminal_lineage_substitution_fails_before_mutation(mutate) -> None:
    sessions, engine, position_id, certificate_id, _ = _seed_terminal("ROLL")
    with sessions.begin() as session:
        mutate(session, certificate_id)
    with pytest.raises(LifecycleTerminalMaterializationError):
        SQLAlchemyLifecycleTerminalMaterializer(sessions).materialize(
            execution_certificate_id=certificate_id
        )
    with sessions() as session:
        position = session.get(ManagedLifecyclePositionRow, position_id)
        assert position.current_snapshot_id is not None
        assert len(session.scalars(select(ManagedPositionTransitionRow)).all()) == 1
    engine.dispose()


def test_conflicting_replay_is_rejected() -> None:
    sessions, engine, _, certificate_id, _ = _seed_terminal("ROLL")
    materializer = SQLAlchemyLifecycleTerminalMaterializer(sessions)
    materializer.materialize(execution_certificate_id=certificate_id)
    with sessions.begin() as session:
        session.get(
            ManagedPositionTransitionRow,
            materializer.materialize(execution_certificate_id=certificate_id),
        ).cashflow_contribution = Decimal("126")
    with pytest.raises(
        LifecycleTerminalMaterializationError,
        match="LIFECYCLE_MATERIALIZATION_CONFLICT",
    ):
        materializer.materialize(execution_certificate_id=certificate_id)
    engine.dispose()


def test_expired_assessment_cannot_materialize_a_later_fill() -> None:
    sessions, engine, position_id, certificate_id, _ = _seed_terminal("CLOSE")
    with sessions.begin() as session:
        session.get(AssessmentCertificateRow, ASSESSMENT_CERTIFICATE_ID).expires_at = (
            TERMINAL_AT - timedelta(minutes=1)
        )
    with pytest.raises(LifecycleTerminalMaterializationError):
        SQLAlchemyLifecycleTerminalMaterializer(sessions).materialize(
            execution_certificate_id=certificate_id
        )
    with sessions() as session:
        position = session.get(ManagedLifecyclePositionRow, position_id)
        assert position.closed_at is None
        assert len(session.scalars(select(ManagedPositionTransitionRow)).all()) == 1
    engine.dispose()


def test_roll_cannot_materialize_an_unequal_ratio_spread() -> None:
    sessions, engine, position_id, certificate_id, inventory = _seed_terminal("ROLL")
    with sessions.begin() as session:
        intent = session.get(ExecutionIntentRow, INTENT_ID)
        intent.legs = [*intent.legs[:3], {**intent.legs[3], "ratio": 2}]
        inventory[1]["signed_quantity"] = "-2"
        reconciliation = session.get(WholeAccountReconciliationRow, RECONCILIATION_ID)
        reconciliation.sweep_payload = {
            **reconciliation.sweep_payload,
            "final_positions": inventory,
            "activities": [
                (
                    {**item, "signed_quantity": "-2"}
                    if item["symbol"] == intent.legs[3]["symbol"]
                    else item
                )
                for item in reconciliation.sweep_payload["activities"]
            ],
        }
        state = session.get(AccountReconciliationStateRow, STATE_ID)
        state.expected_positions = inventory
        state.known_activities = reconciliation.sweep_payload["activities"]
    with pytest.raises(LifecycleTerminalMaterializationError):
        SQLAlchemyLifecycleTerminalMaterializer(sessions).materialize(
            execution_certificate_id=certificate_id
        )
    with sessions() as session:
        position = session.get(ManagedLifecyclePositionRow, position_id)
        assert position.closed_at is None
        assert len(session.scalars(select(ManagedPositionTransitionRow)).all()) == 1
    engine.dispose()


def test_reconciliation_cannot_drop_predecessor_activity_history() -> None:
    sessions, engine, position_id, certificate_id, _ = _seed_terminal("ROLL")
    with sessions.begin() as session:
        reconciliation = session.get(WholeAccountReconciliationRow, RECONCILIATION_ID)
        predecessor = session.get(
            ManagedPositionSnapshotRow,
            session.get(ManagedLifecyclePositionRow, position_id).current_snapshot_id,
        )
        prior_hashes = {item["activity_id_hash"] for item in predecessor.activity_manifest}
        activities = [
            item
            for item in reconciliation.sweep_payload["activities"]
            if item.get("activity_id_hash") not in prior_hashes
        ]
        reconciliation.sweep_payload = {
            **reconciliation.sweep_payload,
            "activities": activities,
        }
        session.get(AccountReconciliationStateRow, STATE_ID).known_activities = activities
    with pytest.raises(LifecycleTerminalMaterializationError):
        SQLAlchemyLifecycleTerminalMaterializer(sessions).materialize(
            execution_certificate_id=certificate_id
        )
    with sessions() as session:
        position = session.get(ManagedLifecyclePositionRow, position_id)
        assert position.closed_at is None
        assert len(session.scalars(select(ManagedPositionTransitionRow)).all()) == 1
    engine.dispose()


def test_reconciliation_cannot_backdate_new_fill_activity() -> None:
    sessions, engine, position_id, certificate_id, _ = _seed_terminal("ROLL")
    with sessions.begin() as session:
        position = session.get(ManagedLifecyclePositionRow, position_id)
        predecessor = session.get(ManagedPositionSnapshotRow, position.current_snapshot_id)
        prior_hashes = {item["activity_id_hash"] for item in predecessor.activity_manifest}
        reconciliation = session.get(WholeAccountReconciliationRow, RECONCILIATION_ID)
        activities = [
            (
                {**item, "occurred_at": predecessor.accepted_at.isoformat()}
                if item.get("activity_id_hash") not in prior_hashes
                else item
            )
            for item in reconciliation.sweep_payload["activities"]
        ]
        reconciliation.sweep_payload = {
            **reconciliation.sweep_payload,
            "activities": activities,
        }
        session.get(AccountReconciliationStateRow, STATE_ID).known_activities = activities
    with pytest.raises(LifecycleTerminalMaterializationError):
        SQLAlchemyLifecycleTerminalMaterializer(sessions).materialize(
            execution_certificate_id=certificate_id
        )
    with sessions() as session:
        position = session.get(ManagedLifecyclePositionRow, position_id)
        assert position.closed_at is None
        assert len(session.scalars(select(ManagedPositionTransitionRow)).all()) == 1
    engine.dispose()


@pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
@pytest.mark.parametrize(
    ("position_lineage", "assessment_lineage"),
    (
        pytest.param(EXPERIMENT_LINEAGE, None, id="null-assessment-lineaged-position"),
        pytest.param(None, EXPERIMENT_LINEAGE, id="lineaged-assessment-null-position"),
    ),
)
def test_postgres_0033_rejects_assessment_position_lineage_mismatch(
    position_lineage: ExperimentExecutionLineage | None,
    assessment_lineage: ExperimentExecutionLineage | None,
) -> None:
    sqlite_sessions, sqlite_engine, retained = _repository(
        AccountRole.DEVELOPMENT,
        position_lineage,
    )
    SQLAlchemyEntryMaterializer(sqlite_sessions).materialize(
        execution_certificate_id=ENTRY_CERTIFICATE_ID,
        launch_authority=retained.launch_authority,
    )
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"experiment_assessment_lineage_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(os.environ[POSTGRES_URL_ENV])

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f'SET search_path TO "{schema}"')
        finally:
            cursor.close()

    try:
        apply_migrations(engine, discover_migrations(MIGRATIONS))
        with sqlite_sessions() as source, engine.begin() as target:
            target.exec_driver_sql("SET session_replication_role = replica")
            _copy_fixture_rows(source, target)
            target.exec_driver_sql("SET session_replication_role = origin")
        with (
            pytest.raises(
                SQLDatabaseError,
                match="EXPERIMENT_ASSESSMENT_POSITION_LINEAGE_INVALID",
            ),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "CREATE TEMP TABLE assessment_lineage_probe ("
                    "account_role varchar(16) NOT NULL, "
                    "position_fingerprint varchar(64) NOT NULL, "
                    "experiment_id uuid, "
                    "experiment_source_definition_hash varchar(64), "
                    "experiment_protocol_hash varchar(64))"
                )
            )
            connection.execute(
                text(
                    "CREATE CONSTRAINT TRIGGER assessment_lineage_probe_guard "
                    "AFTER INSERT ON assessment_lineage_probe "
                    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
                    "experiment_assessment_position_lineage_guard()"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO assessment_lineage_probe ("
                    "account_role, position_fingerprint, experiment_id, "
                    "experiment_source_definition_hash, experiment_protocol_hash) "
                    "VALUES (:account_role, :position_fingerprint, :experiment_id, "
                    ":source_definition_hash, :protocol_hash)"
                ),
                {
                    "account_role": AccountRole.DEVELOPMENT.value,
                    "position_fingerprint": retained.position_fingerprint,
                    "experiment_id": (
                        assessment_lineage.experiment_id if assessment_lineage is not None else None
                    ),
                    "source_definition_hash": (
                        assessment_lineage.source_definition_hash
                        if assessment_lineage is not None
                        else None
                    ),
                    "protocol_hash": (
                        assessment_lineage.protocol_hash if assessment_lineage is not None else None
                    ),
                },
            )
    finally:
        sqlite_engine.dispose()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
def test_postgres_0017_close_upgrade_and_delayed_terminal_materialization() -> None:
    action = "CLOSE"
    sqlite_sessions, sqlite_engine, position_id, certificate_id, inventory = _seed_terminal(action)
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = (
        f"terminal_materialization_{action.lower()}_{uuid5(NAMESPACE_URL, str(certificate_id)).hex}"
    )
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(os.environ[POSTGRES_URL_ENV])

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f'SET search_path TO "{schema}"')
        finally:
            cursor.close()

    migrations = discover_migrations(MIGRATIONS)
    try:
        apply_migrations(engine, migrations[:17])
        with sqlite_sessions() as source, engine.begin() as target:
            target.exec_driver_sql("SET session_replication_role = replica")
            _copy_fixture_rows(source, target)
            target.exec_driver_sql("SET session_replication_role = origin")
        sessions = type(sqlite_sessions)(engine, expire_on_commit=False)
        materializer = SQLAlchemyLifecycleTerminalMaterializer(sessions)
        apply_migrations(engine, migrations)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT array_agg(version ORDER BY version) FROM alphadecay_schema_migrations")
            ).scalar_one() == list(range(1, len(migrations) + 1))
        completed = _complete_failed_materialization_tick(sessions, certificate_id)
        assert completed.terminal_code == "LIFECYCLE_FILLED_MATERIALIZATION_FAILED"
        assert completed.execution_certificate_id == certificate_id
        transition_id = materializer.materialize(execution_certificate_id=certificate_id)
        assert materializer.materialize(execution_certificate_id=certificate_id) == transition_id
        with sessions() as session:
            position = session.get(ManagedLifecyclePositionRow, position_id)
            snapshot = session.get(ManagedPositionSnapshotRow, position.current_snapshot_id)
            assert snapshot.normalized_inventory == inventory
            assert (
                snapshot.accepted_at
                > session.get(
                    ManagedPositionSnapshotRow, snapshot.predecessor_snapshot_id
                ).accepted_at
            )
            assert (position.closed_at is not None) == (action == "CLOSE")
    finally:
        sqlite_engine.dispose()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
def test_postgres_fresh_install_and_restart_keep_lifecycle_tick_guard() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"terminal_materialization_fresh_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(os.environ[POSTGRES_URL_ENV])

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f'SET search_path TO "{schema}"')
        finally:
            cursor.close()

    migrations = discover_migrations(MIGRATIONS)
    try:
        apply_migrations(engine, migrations)
        apply_migrations(engine, migrations)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT array_agg(version ORDER BY version) FROM alphadecay_schema_migrations")
            ).scalar_one() == list(range(1, len(migrations) + 1))
            definition = connection.execute(
                text(
                    "SELECT pg_get_functiondef(oid) FROM pg_proc "
                    "WHERE proname='guard_agent_tick_transition' "
                    "AND pronamespace=current_schema()::regnamespace"
                )
            ).scalar_one()
        assert "LIFECYCLE_FILLED_MATERIALIZATION_FAILED" in definition
        assert "assessment_authorization.action = intent.action" in " ".join(definition.split())
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
@pytest.mark.parametrize(
    "terminal_code",
    (
        "REPLACED",
        "PARTIAL_CANCELED_RECONCILED",
        "PARTIAL_EXPIRED_RECONCILED",
        "PARTIAL_REPLACED_RECONCILED",
    ),
)
def test_postgres_agent_tick_guard_requires_certificate_for_new_terminal_statuses(
    terminal_code: str,
) -> None:
    sqlite_sessions, sqlite_engine, _, _, _ = _seed_terminal("ROLL")
    tick_reference = uuid5(NAMESPACE_URL, "test-fixture-terminal-tick-202")
    tick_boundary = TERMINAL_AT - timedelta(minutes=7)
    with sqlite_sessions.begin() as session:
        session.add(
            AgentTickRow(
                tick_id=tick_reference,
                account_role="DEVELOPMENT",
                account_fingerprint=FINGERPRINT,
                tick_key="terminal-status-guard-fixture",
                tick_boundary=tick_boundary,
                actor="SCHEDULER",
                status="RESERVED",
                reservation_token=uuid5(NAMESPACE_URL, "test-fixture-terminal-reservation-206"),
                terminal_code=None,
                decision_id=DECISION_ID,
                execution_certificate_id=None,
                proof_hash=None,
                created_at=tick_boundary,
                completed_at=None,
            )
        )

    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"terminal_status_guard_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(os.environ[POSTGRES_URL_ENV])

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f'SET search_path TO "{schema}"')
        finally:
            cursor.close()

    try:
        apply_migrations(engine, discover_migrations(MIGRATIONS))
        with sqlite_sessions() as source, engine.begin() as target:
            target.exec_driver_sql("SET session_replication_role = replica")
            _copy_fixture_rows(source, target)
            target.exec_driver_sql("SET session_replication_role = origin")
        with (
            engine.connect() as connection,
            pytest.raises(
                SQLProgrammingError,
                match="agent tick execution terminal requires certificate",
            ),
            connection.begin(),
        ):
            connection.execute(
                text(
                    "UPDATE agent_ticks SET status='COMPLETED', terminal_code=:code, "
                    "proof_hash=:proof, completed_at=:completed WHERE tick_id=:tick_id"
                ),
                {
                    "code": terminal_code,
                    "proof": "a" * 64,
                    "completed": TERMINAL_AT,
                    "tick_id": tick_reference,
                },
            )
    finally:
        sqlite_engine.dispose()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
def test_postgres_0018_upgrade_rejects_untrusted_terminal_history() -> None:
    sqlite_sessions, sqlite_engine, _, certificate_id, _ = _seed_terminal("ROLL")
    SQLAlchemyLifecycleTerminalMaterializer(sqlite_sessions).materialize(
        execution_certificate_id=certificate_id
    )
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"terminal_materialization_history_{certificate_id.hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(os.environ[POSTGRES_URL_ENV])

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f'SET search_path TO "{schema}"')
        finally:
            cursor.close()

    migrations = discover_migrations(MIGRATIONS)
    try:
        apply_migrations(engine, migrations[:17])
        with sqlite_sessions() as source, engine.begin() as target:
            target.exec_driver_sql("SET session_replication_role = replica")
            _copy_fixture_rows(source, target)
            target.exec_driver_sql("SET session_replication_role = origin")
        with pytest.raises(
            PGProgrammingError,
            match="LIFECYCLE_TERMINAL_CONTRACT_REQUIRES_ZERO_HISTORY",
        ):
            apply_migrations(engine, migrations)
    finally:
        sqlite_engine.dispose()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
