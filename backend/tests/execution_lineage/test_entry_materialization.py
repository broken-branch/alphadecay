from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import AccountRole
from backend.app.experiment_lineage import ExperimentExecutionLineage
from backend.app.lifecycle.materialization import (
    EntryMaterializationError,
    SQLAlchemyEntryMaterializer,
    _final_reconciliation_request_hash,
    _materialization_job_hash,
    _validate_vertical,
)
from backend.app.lifecycle.repository import SQLAlchemyLifecycleRepository
from backend.app.persistence.sqlalchemy_models import (
    AccountReconciliationStateRow,
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AgentTickRow,
    AlpacaMarketSessionRow,
    AttemptObservationRow,
    Base,
    BrokerMutationPermitRow,
    CompiledExperimentVersionRow,
    EntryApprovalCertificateRow,
    EntryMaterializationJobRow,
    ExecutionCertificateRow,
    ExecutionIntentRow,
    GreekAuthorityVersionRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
    OrderAttemptRow,
    ReviewedExperimentDefinitionRow,
    ThesisVersionRow,
    WholeAccountReconciliationRow,
)
from backend.app.services import ObservedPaperAccountAuthority
from backend.tests.runtime_composition.test_development_acquisition import (
    FINGERPRINT,
    POLICY_HASH,
    context,
)

ENTRY_AT = datetime(2026, 8, 25, 15, tzinfo=UTC)
CERTIFICATE_ID = uuid5(NAMESPACE_URL, f"alphadecay:execution:{'4' * 64}")
INTENT_ID = UUID("60000000-0000-0000-0000-000000000001")
APPROVAL_ID = UUID("50000000-0000-0000-0000-000000000001")
RECONCILIATION_ID = UUID("40000000-0000-0000-0000-000000000001")
STATE_ID = UUID("30000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("20000000-0000-0000-0000-000000000001")
DECISION_ID = UUID("10000000-0000-0000-0000-000000000001")
ATTEMPT_ID = UUID("80000000-0000-0000-0000-000000000001")
PERMIT_ID = UUID("81000000-0000-0000-0000-000000000001")
OBSERVATION_ID = UUID("82000000-0000-0000-0000-000000000001")
CLIENT_ID = str(uuid5(NAMESPACE_URL, "test-fixture-client-order-a0"))
PROVIDER_ID = str(uuid5(NAMESPACE_URL, "test-fixture-provider-order-a0"))
ACTIVITY_HASHES = ("e" * 64, "f" * 64)
FINAL_RECONCILIATION_REQUEST_HASH = _final_reconciliation_request_hash(
    "4" * 64,
    0,
    "0" * 64,
)
EXPERIMENT_LINEAGE = ExperimentExecutionLineage(
    experiment_id=UUID("90000000-0000-0000-0000-000000000001"),
    source_definition_hash="6" * 64,
    protocol_hash="7" * 64,
)


def test_entry_materializer_rejects_adjusted_contract_with_stable_reason() -> None:
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

    with pytest.raises(EntryMaterializationError) as raised:
        _validate_vertical(
            inventory,
            SimpleNamespace(quantity=1),
            SimpleNamespace(underlying="PANW"),
        )

    assert str(raised.value) == "NON_STANDARD_CONTRACT_UNSUPPORTED"


def _repository(
    account_role: AccountRole = AccountRole.DEVELOPMENT,
    experiment_lineage: ExperimentExecutionLineage | None = None,
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    retained = context()
    inventory = [
        {
            "kind": "OPTION",
            "symbol": item.symbol,
            "signed_quantity": str(int(item.signed_quantity)),
            "multiplier": item.multiplier,
        }
        for item in retained.expected_positions
    ]
    activities = [
        {
            "activity_id_hash": activity_hash,
            "activity_type": "OPTRD",
            "occurred_at": ENTRY_AT.isoformat(),
            "symbol": item["symbol"],
            "signed_quantity": item["signed_quantity"],
            "provider_order_id": PROVIDER_ID,
            "client_order_id": CLIENT_ID,
            "time_quality": "EXACT_TRANSACTION_TIME",
            "provider_activity_type": "fill",
        }
        for activity_hash, item in zip(ACTIVITY_HASHES, inventory, strict=True)
    ]
    with sessions.begin() as session:
        if experiment_lineage is not None:
            session.add(
                ReviewedExperimentDefinitionRow(
                    experiment_id=experiment_lineage.experiment_id,
                    version=1,
                    definition_hash=experiment_lineage.source_definition_hash,
                    lifecycle_state="REVIEWED",
                    payload_text="{}",
                    created_at=ENTRY_AT,
                )
            )
            session.add(
                CompiledExperimentVersionRow(
                    experiment_id=experiment_lineage.experiment_id,
                    source_version=1,
                    compiled_version=1,
                    source_definition_hash=experiment_lineage.source_definition_hash,
                    protocol_hash=experiment_lineage.protocol_hash,
                    lifecycle_state="COMPILED",
                    arm_state="NOT_ARMED",
                    automation_state="OFF",
                    execution_eligible=False,
                    payload_text="{}",
                    created_at=ENTRY_AT,
                )
            )
        session.add(
            AccountRoleRow(
                role=account_role.value,
                account_fingerprint=FINGERPRINT,
                equity=Decimal("100000"),
                autonomous_enabled=True,
            )
        )
        session.add(
            ThesisVersionRow(
                thesis_version_id=retained.thesis_version_id,
                thesis_id=uuid5(NAMESPACE_URL, "test-fixture-901"),
                account_role=account_role.value,
                version=1,
                origin_hash=retained.thesis.thesis_hash,
                thesis_hash=retained.thesis.thesis_hash,
                policy_hash=POLICY_HASH,
                underlying=retained.thesis.thesis.underlying,
                thesis_code="TEST",
                frozen_at=retained.thesis_frozen_at,
                target_at=retained.target_at,
                intended_exposure=retained.thesis.thesis.intended_exposure.model_dump(mode="json"),
                exposure_limits={
                    "delta_low": str(retained.delta_low),
                    "delta_high": str(retained.delta_high),
                    "vega_low": str(retained.vega_low),
                    "vega_high": str(retained.vega_high),
                    "maximum_daily_theta": str(retained.maximum_daily_theta),
                    "minimum_dte": retained.minimum_dte,
                    "maximum_dte": retained.maximum_dte,
                    "maximum_relative_spread": str(retained.maximum_relative_spread),
                    "liquidity_authority_hash": retained.liquidity_authority_hash,
                },
                volatility_view=retained.volatility_view.value,
                entry_atm_iv=retained.entry_atm_iv,
                approved_max_loss=retained.approved_max_loss,
                portfolio_risk_cap=retained.portfolio_risk_cap,
                invalidation_codes=["GUIDANCE_REVERSED"],
                thesis_payload=retained.thesis.model_dump(mode="json"),
                created_at=retained.thesis_frozen_at,
            )
        )
        session.add(
            AgentTickRow(
                tick_id=uuid5(NAMESPACE_URL, "test-fixture-902"),
                account_role=account_role.value,
                account_fingerprint=FINGERPRINT,
                tick_key="entry-materialization-fixture",
                tick_boundary=ENTRY_AT,
                actor="SCHEDULER",
                status="RESERVED",
                reservation_token=uuid5(NAMESPACE_URL, "test-fixture-reservation-904"),
                terminal_code=None,
                decision_id=DECISION_ID,
                execution_certificate_id=None,
                proof_hash=None,
                created_at=ENTRY_AT,
                completed_at=None,
            )
        )
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=uuid5(NAMESPACE_URL, "test-fixture-903"),
                thesis_version_id=retained.thesis_version_id,
                account_role=account_role.value,
                account_fingerprint=FINGERPRINT,
                decision_kind="OPPORTUNITY",
                decision_boundary=retained.launch_authority.entry_boundary_at,
                observed_at=retained.thesis_frozen_at,
                normalized_payload={},
                input_hash="0" * 64,
                created_at=retained.thesis_frozen_at,
            )
        )
        session.add(
            AgentDecisionRow(
                decision_id=DECISION_ID,
                thesis_version_id=retained.thesis_version_id,
                origin_tick_id=uuid5(NAMESPACE_URL, "test-fixture-902"),
                input_snapshot_id=uuid5(NAMESPACE_URL, "test-fixture-903"),
                account_role=account_role.value,
                account_fingerprint=FINGERPRINT,
                decision_kind="OPPORTUNITY",
                outcome="ENTRY_APPROVED",
                reason_code="ENTRY_APPROVED",
                policy_hash=POLICY_HASH,
                result_payload={},
                result_hash="1" * 64,
                autonomy_authorized=True,
                decision_boundary=retained.launch_authority.entry_boundary_at,
                created_at=retained.thesis_frozen_at,
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
            EntryApprovalCertificateRow(
                approval_id=APPROVAL_ID,
                thesis_version_id=retained.thesis_version_id,
                agent_decision_id=DECISION_ID,
                account_role=account_role.value,
                policy_hash=POLICY_HASH,
                book_fingerprint="2" * 64,
                envelope_hash="3" * 64,
                approved_max_loss=Decimal("500"),
                quantity=1,
                valid_from=ENTRY_AT - timedelta(minutes=1),
                expires_at=ENTRY_AT + timedelta(minutes=1),
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
        legs = [
            {"symbol": inventory[0]["symbol"], "intent": "BUY_TO_OPEN", "ratio": 1},
            {"symbol": inventory[1]["symbol"], "intent": "SELL_TO_OPEN", "ratio": 1},
        ]
        session.add(
            ExecutionIntentRow(
                intent_id=INTENT_ID,
                account_role=account_role.value,
                intent_digest="4" * 64,
                action="ENTRY",
                policy_hash=POLICY_HASH,
                event_key="entry-test",
                trading_day=ENTRY_AT.date(),
                entry_approval_id=APPROVAL_ID,
                assessment_certificate_id=None,
                fingerprint="2" * 64,
                envelope_hash="3" * 64,
                envelope_payload={},
                legs=legs,
                quantity=1,
                minimum_limit=Decimal("1"),
                maximum_limit=Decimal("2"),
                approved_max_loss=Decimal("500"),
                state="TERMINAL",
                first_fill_consumed=True,
            )
        )
        job_values = {
            "execution_intent_id": INTENT_ID,
            "entry_approval_id": APPROVAL_ID,
            "account_role": account_role.value,
            "account_fingerprint": FINGERPRINT,
            "beta60": retained.launch_authority.beta60,
            "benchmark_symbol": retained.launch_authority.benchmark_symbol,
            "entry_boundary_at": retained.launch_authority.entry_boundary_at,
            "entry_policy_hash": retained.launch_authority.entry_policy_hash,
            "underlying_source_hash": retained.launch_authority.underlying_source_hash,
            "benchmark_source_hash": retained.launch_authority.benchmark_source_hash,
            "completed_bar_source_hash": retained.launch_authority.completed_bar_source_hash,
            "prepared_at": retained.thesis_frozen_at,
            "managed_position_id": None,
            "terminal_status": None,
            "completed_at": None,
        }
        session.add(
            EntryMaterializationJobRow(
                **job_values,
                job_hash=_materialization_job_hash(session, job_values),
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
                request_hash="5" * 64,
                quote_source_timestamps=[],
                filled_quantity=1,
                quantity=1,
                fill_cash_flow=Decimal("-500"),
            )
        )
        expectation = {
            "purpose": "SUBMIT",
            "intent_id": str(INTENT_ID),
            "intent_digest": "4" * 64,
            "attempt_ordinal": 0,
            "request_hash": FINAL_RECONCILIATION_REQUEST_HASH,
            "expected_open_orders": [],
            "expected_cash": "99500",
        }
        sweep = {
            "final_positions": inventory,
            "activities": activities,
            "retrieval_started_at": ENTRY_AT.isoformat(),
            "retrieval_completed_at": ENTRY_AT.isoformat(),
        }
        session.add(
            WholeAccountReconciliationRow(
                reconciliation_id=RECONCILIATION_ID,
                reconciliation_hash="6" * 64,
                expectation_hash="7" * 64,
                execution_intent_id=INTENT_ID,
                intent_digest="4" * 64,
                account_role=account_role.value,
                account_fingerprint=FINGERPRINT,
                purpose="SUBMIT",
                attempt_ordinal=0,
                request_hash=FINAL_RECONCILIATION_REQUEST_HASH,
                accepted_at=ENTRY_AT,
                expectation_payload=expectation,
                sweep_payload=sweep,
                positions_manifest_hash="8" * 64,
                orders_manifest_hash="9" * 64,
                activities_manifest_hash="a" * 64,
                safe=True,
                block_codes=[],
            )
        )
        session.add(
            AccountReconciliationStateRow(
                state_id=STATE_ID,
                account_role=account_role.value,
                sequence=2,
                account_fingerprint=FINGERPRINT,
                baseline_id=uuid5(NAMESPACE_URL, "test-fixture-904"),
                baseline_captured_at=ENTRY_AT - timedelta(days=1),
                accepted_at=ENTRY_AT,
                expected_cash=Decimal("99500"),
                expected_positions=inventory,
                expected_open_orders=[],
                known_activities=activities,
                activity_complete_through=ENTRY_AT,
                resolved_activity_hashes=list(ACTIVITY_HASHES),
                predecessor_state_id=uuid5(NAMESPACE_URL, "test-fixture-905"),
                authority_reconciliation_id=RECONCILIATION_ID,
                authority_permit_id=PERMIT_ID,
                authority_observation_id=OBSERVATION_ID,
                authority_permit_request_hash="5" * 64,
                transition_hash="b" * 64,
                state_hash="c" * 64,
            )
        )
        session.add(
            BrokerMutationPermitRow(
                permit_id=PERMIT_ID,
                reconciliation_id=RECONCILIATION_ID,
                execution_intent_id=INTENT_ID,
                intent_digest="4" * 64,
                claim_token=uuid5(NAMESPACE_URL, "test-fixture-claim-906"),
                claim_generation=1,
                execution_epoch=0,
                mutation_kind="SUBMIT",
                attempt_ordinal=0,
                permit_generation=1,
                request_hash="5" * 64,
                limit_price=Decimal("1.50"),
                quote_source_timestamps=[],
                issued_at=ENTRY_AT - timedelta(minutes=2),
                expires_at=ENTRY_AT + timedelta(minutes=1),
                state="CONSUMED",
                dispatch_nonce=uuid5(NAMESPACE_URL, "test-fixture-907"),
                dispatch_acquired_at=ENTRY_AT - timedelta(minutes=1),
                consumed_at=ENTRY_AT,
                outcome_hash="6" * 64,
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
                    "request_hash": "5" * 64,
                    "state": "FILLED",
                    "replaces_client_order_id": None,
                    "provider_order_id": PROVIDER_ID,
                    "filled_quantity": 1,
                    "quantity": 1,
                    "fill_cash_flow": "-500",
                },
                observed_at=ENTRY_AT,
                observation_hash="0" * 64,
            )
        )
        session.add(
            AlpacaMarketSessionRow(
                market_session_id=SESSION_ID,
                session_date=ENTRY_AT.date(),
                open_at=ENTRY_AT - timedelta(hours=1),
                close_at=ENTRY_AT + timedelta(hours=5),
                source_hash="d" * 64,
                request_hash="e" * 64,
                retrieved_at=ENTRY_AT - timedelta(hours=2),
                source_payload={},
                session_hash="f" * 64,
                created_at=ENTRY_AT - timedelta(hours=1),
            )
        )
        session.add(
            ExecutionCertificateRow(
                certificate_id=CERTIFICATE_ID,
                execution_intent_id=INTENT_ID,
                entry_approval_id=APPROVAL_ID,
                assessment_certificate_id=None,
                execution_status="FILLED",
                attempt_ids=[CLIENT_ID],
                actual_exposure=None,
                reconciliation_checks=[
                    "TERMINAL",
                    "REMAINDER_ABSENT",
                    "WHOLE_ACCOUNT_RECONCILED",
                ],
                created_at=ENTRY_AT,
                reconciliation_id=RECONCILIATION_ID,
                reconciliation_hash="6" * 64,
                last_observation_hash="0" * 64,
            )
        )
        session.add(
            GreekAuthorityVersionRow(
                authority_id=retained.greek_authority.authority_id,
                version=retained.greek_authority.version,
                effective_at=retained.greek_authority.effective_at,
                timestamp_contract_hash=retained.greek_authority.timestamp_contract_hash,
                units_contract_hash=retained.greek_authority.units_source_hash,
                authority_payload={},
                authority_hash="0" * 63 + "1",
                created_at=retained.greek_authority.effective_at,
            )
        )
    return sessions, engine, retained


def test_terminal_entry_materializes_once_and_loads_retained_context() -> None:
    sessions, engine, retained = _repository()
    materializer = SQLAlchemyEntryMaterializer(sessions)

    first = materializer.materialize(
        execution_certificate_id=CERTIFICATE_ID,
        launch_authority=retained.launch_authority,
    )
    second = materializer.materialize(
        execution_certificate_id=CERTIFICATE_ID,
        launch_authority=retained.launch_authority,
    )

    assert first == second
    loaded = SQLAlchemyLifecycleRepository(sessions).load(
        ObservedPaperAccountAuthority(
            AccountRole.DEVELOPMENT,
            FINGERPRINT,
            True,
            True,
        )
    )
    assert loaded.managed_position_id == first
    assert loaded.position_fingerprint == retained.position_fingerprint
    assert loaded.expected_positions == retained.expected_positions
    with sessions() as session:
        assert session.scalars(select(ManagedLifecyclePositionRow)).all().__len__() == 1
        assert session.scalars(select(ManagedPositionTransitionRow)).all().__len__() == 1
        assert session.scalars(select(ManagedPositionSnapshotRow)).all().__len__() == 1
    engine.dispose()


def test_terminal_entry_carries_exact_experiment_lineage_to_managed_position() -> None:
    sessions, engine, retained = _repository(experiment_lineage=EXPERIMENT_LINEAGE)

    materialized_id = SQLAlchemyEntryMaterializer(sessions).materialize(
        execution_certificate_id=CERTIFICATE_ID,
        launch_authority=retained.launch_authority,
    )

    with sessions() as session:
        managed = session.get(ManagedLifecyclePositionRow, materialized_id)
        assert managed is not None
        assert managed.experiment_id == EXPERIMENT_LINEAGE.experiment_id
        assert (
            managed.experiment_source_definition_hash == EXPERIMENT_LINEAGE.source_definition_hash
        )
        assert managed.experiment_protocol_hash == EXPERIMENT_LINEAGE.protocol_hash
    engine.dispose()


def test_submission_entry_fill_materializes_for_exact_account_role() -> None:
    sessions, engine, retained = _repository(AccountRole.SUBMISSION)

    materialized_id = SQLAlchemyEntryMaterializer(sessions).materialize(
        execution_certificate_id=CERTIFICATE_ID,
        launch_authority=retained.launch_authority,
    )

    loaded = SQLAlchemyLifecycleRepository(sessions).load(
        ObservedPaperAccountAuthority(
            AccountRole.SUBMISSION,
            FINGERPRINT,
            True,
            True,
        )
    )
    assert loaded.managed_position_id == materialized_id
    engine.dispose()


def test_submission_entry_prepares_for_exact_account_role() -> None:
    sessions, engine, retained = _repository(AccountRole.SUBMISSION)
    with sessions.begin() as session:
        session.delete(session.get(EntryMaterializationJobRow, INTENT_ID))
        intent = session.get(ExecutionIntentRow, INTENT_ID)
        assert intent is not None
        intent.state = "APPROVED"
        intent.first_fill_consumed = False

    SQLAlchemyEntryMaterializer(sessions).prepare(
        execution_intent_id=INTENT_ID,
        launch_authority=retained.launch_authority,
        prepared_at=retained.thesis_frozen_at,
    )

    with sessions() as session:
        job = session.get(EntryMaterializationJobRow, INTENT_ID)
        assert job is not None
        assert job.account_role == AccountRole.SUBMISSION.value
        assert job.account_fingerprint == FINGERPRINT
    engine.dispose()


def test_submission_nonfilled_entry_resolves_for_exact_account_role() -> None:
    sessions, engine, _ = _repository(AccountRole.SUBMISSION)
    with sessions.begin() as session:
        certificate = session.get(ExecutionCertificateRow, CERTIFICATE_ID)
        assert certificate is not None
        certificate.execution_status = "CANCELED"

    materializer = SQLAlchemyEntryMaterializer(sessions)
    assert (
        materializer.recover_pending(
            account_role=AccountRole.SUBMISSION.value,
            account_fingerprint=FINGERPRINT,
        )
        == ()
    )
    with sessions() as session:
        job = session.get(EntryMaterializationJobRow, INTENT_ID)
        assert job is not None
        assert job.terminal_status == "CANCELED"
        assert job.managed_position_id is None
    engine.dispose()


def test_submission_entry_rejects_cross_role_lineage() -> None:
    sessions, engine, retained = _repository(AccountRole.SUBMISSION)
    with sessions.begin() as session:
        decision = session.get(AgentDecisionRow, DECISION_ID)
        assert decision is not None
        decision.account_role = AccountRole.DEVELOPMENT.value

    with pytest.raises(EntryMaterializationError, match="ENTRY_LINEAGE_INVALID"):
        SQLAlchemyEntryMaterializer(sessions).materialize(
            execution_certificate_id=CERTIFICATE_ID,
            launch_authority=retained.launch_authority,
        )
    engine.dispose()


def test_materialization_job_is_prepared_before_dispatch_and_recovers_after_fill() -> None:
    sessions, engine, retained = _repository()
    with sessions.begin() as session:
        session.delete(session.get(EntryMaterializationJobRow, INTENT_ID))
        intent = session.get(ExecutionIntentRow, INTENT_ID)
        assert intent is not None
        intent.state = "APPROVED"
        intent.first_fill_consumed = False
    materializer = SQLAlchemyEntryMaterializer(sessions)
    materializer.prepare(
        execution_intent_id=INTENT_ID,
        launch_authority=retained.launch_authority,
        prepared_at=retained.thesis_frozen_at,
    )
    materializer.prepare(
        execution_intent_id=INTENT_ID,
        launch_authority=retained.launch_authority,
        prepared_at=retained.thesis_frozen_at,
    )
    with sessions.begin() as session:
        intent = session.get(ExecutionIntentRow, INTENT_ID)
        assert intent is not None
        intent.state = "TERMINAL"
        intent.first_fill_consumed = True

    with pytest.raises(
        EntryMaterializationError,
        match="ENTRY_MATERIALIZATION_RECOVERY_AUTHORITY_INVALID",
    ):
        materializer.recover_pending(
            account_role="DEVELOPMENT",
            account_fingerprint="b" * 64,
        )
    recovered = materializer.recover_pending(
        account_role="DEVELOPMENT",
        account_fingerprint=FINGERPRINT,
    )

    assert len(recovered) == 1
    loaded = SQLAlchemyLifecycleRepository(sessions).load(
        ObservedPaperAccountAuthority(
            AccountRole.DEVELOPMENT,
            FINGERPRINT,
            True,
            True,
        )
    )
    assert loaded.managed_position_id == recovered[0]
    with sessions() as session:
        job = session.get(EntryMaterializationJobRow, INTENT_ID)
        assert job is not None
        assert job.managed_position_id == recovered[0]
        assert job.terminal_status == "FILLED"
        assert job.completed_at is not None
        completed_at = (
            job.completed_at.replace(tzinfo=UTC)
            if job.completed_at.tzinfo is None
            else job.completed_at.astimezone(UTC)
        )
        assert completed_at >= ENTRY_AT
    assert (
        materializer.recover_pending(
            account_role="DEVELOPMENT",
            account_fingerprint=FINGERPRINT,
        )
        == ()
    )
    engine.dispose()


@pytest.mark.parametrize(
    "status",
    (
        "CANCELED",
        "REPLACED",
    ),
)
def test_terminal_nonfilled_entry_resolves_job_without_managed_position(
    status: str,
) -> None:
    sessions, engine, _ = _repository()
    with sessions.begin() as session:
        certificate = session.get(ExecutionCertificateRow, CERTIFICATE_ID)
        assert certificate is not None
        certificate.execution_status = status

    materializer = SQLAlchemyEntryMaterializer(sessions)
    assert (
        materializer.recover_pending(
            account_role="DEVELOPMENT",
            account_fingerprint=FINGERPRINT,
        )
        == ()
    )
    with sessions() as session:
        job = session.get(EntryMaterializationJobRow, INTENT_ID)
        assert job is not None
        assert job.terminal_status == status
        assert job.managed_position_id is None
        assert job.completed_at is not None
        assert session.scalars(select(ManagedLifecyclePositionRow)).all() == []
    engine.dispose()


@pytest.mark.parametrize(
    "status",
    (
        "PARTIAL_CANCELED_RECONCILED",
        "PARTIAL_EXPIRED_RECONCILED",
        "PARTIAL_REPLACED_RECONCILED",
    ),
)
def test_partial_entry_terminal_never_resolves_as_no_position(status: str) -> None:
    sessions, engine, _ = _repository()
    with sessions.begin() as session:
        certificate = session.get(ExecutionCertificateRow, CERTIFICATE_ID)
        assert certificate is not None
        certificate.execution_status = status

    with pytest.raises(
        EntryMaterializationError, match="ENTRY_MATERIALIZATION_TERMINAL_STATUS_INVALID"
    ):
        SQLAlchemyEntryMaterializer(sessions).recover_pending(
            account_role="DEVELOPMENT",
            account_fingerprint=FINGERPRINT,
        )
    engine.dispose()


def test_terminal_unsubmitted_job_is_not_returned_for_execution_recovery() -> None:
    sessions, engine, _ = _repository()
    with sessions.begin() as session:
        session.delete(session.get(ExecutionCertificateRow, CERTIFICATE_ID))

    materializer = SQLAlchemyEntryMaterializer(sessions)
    assert (
        materializer.recover_pending(
            account_role="DEVELOPMENT",
            account_fingerprint=FINGERPRINT,
        )
        == ()
    )
    assert (
        materializer.pending_execution_intents(
            account_role="DEVELOPMENT",
            account_fingerprint=FINGERPRINT,
        )
        == ()
    )
    engine.dispose()


def test_prepared_job_hash_substitution_blocks_recovery() -> None:
    sessions, engine, _ = _repository()
    with sessions.begin() as session:
        job = session.get(EntryMaterializationJobRow, INTENT_ID)
        assert job is not None
        job.job_hash = "9" * 64

    with pytest.raises(EntryMaterializationError, match="ENTRY_MATERIALIZATION_JOB_INVALID"):
        SQLAlchemyEntryMaterializer(sessions).recover_pending(
            account_role="DEVELOPMENT",
            account_fingerprint=FINGERPRINT,
        )
    engine.dispose()


@pytest.mark.parametrize(
    ("mutate", "code"),
    (
        (
            lambda session: setattr(
                session.get(ExecutionCertificateRow, CERTIFICATE_ID),
                "execution_status",
                "CANCELED",
            ),
            "ENTRY_LINEAGE_INVALID",
        ),
        (
            lambda session: setattr(
                session.get(ExecutionCertificateRow, CERTIFICATE_ID),
                "attempt_ids",
                ["substituted-client-fixture"],
            ),
            "ENTRY_ATTEMPT_LINEAGE_INVALID",
        ),
        (
            lambda session: setattr(
                session.get(WholeAccountReconciliationRow, RECONCILIATION_ID),
                "sweep_payload",
                {
                    **session.get(WholeAccountReconciliationRow, RECONCILIATION_ID).sweep_payload,
                    "activities": [],
                },
            ),
            "ENTRY_LINEAGE_INVALID",
        ),
        (
            lambda session: setattr(
                session.get(ExecutionIntentRow, INTENT_ID),
                "account_role",
                "SUBMISSION",
            ),
            "ENTRY_LINEAGE_INVALID",
        ),
        (
            lambda session: setattr(
                session.get(WholeAccountReconciliationRow, RECONCILIATION_ID),
                "account_fingerprint",
                "f" * 64,
            ),
            "ENTRY_LINEAGE_INVALID",
        ),
        (
            lambda session: setattr(
                session.get(EntryApprovalCertificateRow, APPROVAL_ID),
                "thesis_version_id",
                uuid5(NAMESPACE_URL, "test-fixture-999"),
            ),
            "ENTRY_LINEAGE_INVALID",
        ),
        (
            lambda session: setattr(
                session.get(ExecutionCertificateRow, CERTIFICATE_ID),
                "entry_approval_id",
                uuid5(NAMESPACE_URL, "test-fixture-998"),
            ),
            "ENTRY_LINEAGE_INVALID",
        ),
        (
            lambda session: setattr(
                session.get(OrderAttemptRow, ATTEMPT_ID),
                "quantity",
                2,
            ),
            "ENTRY_ATTEMPT_LINEAGE_INVALID",
        ),
        (
            lambda session: setattr(
                session.get(EntryApprovalCertificateRow, APPROVAL_ID),
                "quantity",
                2,
            ),
            "ENTRY_LINEAGE_INVALID",
        ),
        (
            lambda session: setattr(
                session.get(ExecutionCertificateRow, CERTIFICATE_ID),
                "reconciliation_hash",
                "7" * 64,
            ),
            "ENTRY_LINEAGE_INVALID",
        ),
        (
            lambda session: setattr(
                session.get(WholeAccountReconciliationRow, RECONCILIATION_ID),
                "sweep_payload",
                {
                    **session.get(WholeAccountReconciliationRow, RECONCILIATION_ID).sweep_payload,
                    "final_positions": session.get(
                        WholeAccountReconciliationRow, RECONCILIATION_ID
                    ).sweep_payload["final_positions"][:1],
                },
            ),
            "ENTRY_LINEAGE_INVALID",
        ),
    ),
)
def test_invalid_terminal_lineage_is_rejected(mutate, code: str) -> None:
    sessions, engine, retained = _repository()
    with sessions.begin() as session:
        mutate(session)

    with pytest.raises(EntryMaterializationError, match=code):
        SQLAlchemyEntryMaterializer(sessions).materialize(
            execution_certificate_id=CERTIFICATE_ID,
            launch_authority=retained.launch_authority,
        )
    engine.dispose()


def test_exact_replay_rejects_launch_authority_substitution() -> None:
    sessions, engine, retained = _repository()
    materializer = SQLAlchemyEntryMaterializer(sessions)
    materializer.materialize(
        execution_certificate_id=CERTIFICATE_ID,
        launch_authority=retained.launch_authority,
    )
    changed = retained.launch_authority.__class__(
        beta60=Decimal("1.30"),
        benchmark_symbol="QQQ",
        entry_boundary_at=retained.launch_authority.entry_boundary_at,
        entry_policy_hash=retained.launch_authority.entry_policy_hash,
        underlying_source_hash=retained.launch_authority.underlying_source_hash,
        benchmark_source_hash=retained.launch_authority.benchmark_source_hash,
        completed_bar_source_hash=retained.launch_authority.completed_bar_source_hash,
    )

    with pytest.raises(EntryMaterializationError, match="ENTRY_MATERIALIZATION_JOB_INVALID"):
        materializer.materialize(
            execution_certificate_id=CERTIFICATE_ID,
            launch_authority=changed,
        )
    engine.dispose()


def test_fill_activity_provider_substitution_is_rejected() -> None:
    sessions, engine, retained = _repository()
    with sessions.begin() as session:
        reconciliation = session.get(WholeAccountReconciliationRow, RECONCILIATION_ID)
        state = session.get(AccountReconciliationStateRow, STATE_ID)
        assert reconciliation is not None
        assert state is not None
        changed = [
            {
                **item,
                "provider_order_id": str(
                    uuid5(NAMESPACE_URL, "test-fixture-provider-order-substitution")
                ),
            }
            for item in reconciliation.sweep_payload["activities"]
        ]
        reconciliation.sweep_payload = {
            **reconciliation.sweep_payload,
            "activities": changed,
        }
        state.known_activities = changed

    with pytest.raises(EntryMaterializationError, match="ENTRY_ACTIVITY_LINEAGE_INCOMPLETE"):
        SQLAlchemyEntryMaterializer(sessions).materialize(
            execution_certificate_id=CERTIFICATE_ID,
            launch_authority=retained.launch_authority,
        )
    engine.dispose()


def test_zero_fill_replacement_predecessor_does_not_require_fill_activities() -> None:
    sessions, engine, retained = _repository()
    predecessor_id = UUID("80000000-0000-0000-0000-000000000000")
    predecessor_client_reference = str(uuid5(NAMESPACE_URL, "test-fixture-client-order-a0"))
    final_client_reference = str(uuid5(NAMESPACE_URL, "test-fixture-client-order-a1"))
    with sessions.begin() as session:
        final = session.get(OrderAttemptRow, ATTEMPT_ID)
        certificate = session.get(ExecutionCertificateRow, CERTIFICATE_ID)
        reconciliation = session.get(WholeAccountReconciliationRow, RECONCILIATION_ID)
        state = session.get(AccountReconciliationStateRow, STATE_ID)
        permit = session.get(BrokerMutationPermitRow, PERMIT_ID)
        observation = session.get(AttemptObservationRow, OBSERVATION_ID)
        assert all(
            value is not None
            for value in (final, certificate, reconciliation, state, permit, observation)
        )
        final.attempt_ordinal = 1
        final.client_order_id = final_client_reference
        final.replaces_attempt_id = predecessor_id
        session.add(
            OrderAttemptRow(
                attempt_id=predecessor_id,
                broker_permit_id=None,
                execution_intent_id=INTENT_ID,
                attempt_ordinal=0,
                client_order_id=predecessor_client_reference,
                provider_order_id=str(
                    uuid5(NAMESPACE_URL, "test-fixture-provider-order-a0-unfilled")
                ),
                state="CANCELED",
                request_hash="7" * 64,
                quote_source_timestamps=[],
                filled_quantity=0,
                quantity=1,
                fill_cash_flow=None,
            )
        )
        certificate.attempt_ids = [predecessor_client_reference, final_client_reference]
        reconciliation.attempt_ordinal = 1
        reconciliation.purpose = "REPLACE"
        reconciliation.expectation_payload = {
            **reconciliation.expectation_payload,
            "purpose": "REPLACE",
            "attempt_ordinal": 1,
            "request_hash": _final_reconciliation_request_hash(
                "4" * 64,
                1,
                "0" * 64,
            ),
        }
        reconciliation.request_hash = reconciliation.expectation_payload["request_hash"]
        reconciliation.sweep_payload = {
            **reconciliation.sweep_payload,
            "activities": [
                {**item, "client_order_id": final_client_reference}
                for item in reconciliation.sweep_payload["activities"]
            ],
        }
        state.known_activities = reconciliation.sweep_payload["activities"]
        permit.mutation_kind = "REPLACE"
        permit.attempt_ordinal = 1
        observation.attempt_ordinal = 1
        observation.observed_payload = {
            **observation.observed_payload,
            "ordinal": 1,
            "client_order_id": final_client_reference,
            "replaces_client_order_id": predecessor_client_reference,
        }

    SQLAlchemyEntryMaterializer(sessions).materialize(
        execution_certificate_id=CERTIFICATE_ID,
        launch_authority=retained.launch_authority,
    )
    engine.dispose()
