import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import CheckConstraint, create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.contracts.v1 import AccountRole, GreekExposure, PositionIntent
from backend.app.execution import (
    Actor,
    AssessmentCertificate,
    BrokerResult,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionBlocked,
    ExecutionCertificate,
    FrozenThesisVersion,
    OrderEnvelope,
    OrderLegIntent,
    Reconciliation,
    attempt_request_hash,
    client_order_id,
    intent_digest,
    order_envelope_hash,
)
from backend.app.execution.models import ExecutionIntent, IntentState, OrderAttempt
from backend.app.order_limits import EntryBudgetLimits
from backend.app.persistence import AgentDecisionRepository, SQLAlchemyExecutionRepository
from backend.app.persistence.agent_authority import (
    agent_input_material,
    agent_result_material,
    canonical_agent_hash,
)
from backend.app.persistence.sqlalchemy_models import (
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AgentTickRow,
    AssessmentCertificateRow,
    AttemptObservationRow,
    Base,
    EntryApprovalCertificateRow,
    ExecutionIntentRow,
    OrderAttemptRow,
)
from backend.app.services import ExecutionService

NOW = datetime(2026, 9, 3, 16, 0, tzinfo=UTC)
INTENT_ID = UUID("00000000-0000-0000-0000-000000000301")
AUTH_ID = UUID("00000000-0000-0000-0000-000000000302")
MIGRATIONS = Path(__file__).parents[3] / "migrations"
POLICY_HASH = "a" * 64
ENTRY_LIMITS = EntryBudgetLimits(
    policy_hash=POLICY_HASH,
    equity_floor=Decimal("99000"),
    maximum_lifetime_entries=3,
    maximum_lifetime_risk=Decimal("1500"),
    maximum_position_loss=Decimal("800"),
    maximum_entry_quantity=4,
)
DEVELOPMENT_ENTRY_LIMITS = EntryBudgetLimits(
    policy_hash="d" * 64,
    equity_floor=Decimal("99000"),
    maximum_lifetime_entries=3,
    maximum_lifetime_risk=Decimal("1500"),
    maximum_position_loss=Decimal("800"),
    maximum_entry_quantity=4,
)
ROLL_AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-000000000308")
ROLL_SESSION_ID = UUID("00000000-0000-0000-0000-000000000309")


def _claim(
    repo: SQLAlchemyExecutionRepository,
    intent_id: UUID,
    actor: Actor,
    *,
    now: datetime,
):
    return repo.claim_intent(
        intent_id,
        actor,
        now=now,
        account_role=AccountRole.SUBMISSION,
        account_fingerprint="sql-account-fingerprint",
    )


def _execution_service(repository, broker, preflight) -> ExecutionService:
    return ExecutionService(
        repository,
        broker,
        preflight,
        account_role=AccountRole.SUBMISSION,
        account_fingerprint="sql-account-fingerprint",
    )


def envelope() -> OrderEnvelope:
    return OrderEnvelope(
        action=ExecutionAction.ENTRY,
        authorization_certificate_id=AUTH_ID,
        policy_hash=POLICY_HASH,
        account_fingerprint="sql-account-fingerprint",
        position_or_book_fingerprint="sql-book-fingerprint",
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        quantity=2,
        minimum_limit=Decimal("1.00"),
        maximum_limit=Decimal("1.50"),
        approved_max_loss=Decimal("700"),
        event_key="PANW-2026-09-03",
        trading_day=date(2026, 9, 3),
    )


def lifecycle_envelope() -> OrderEnvelope:
    return OrderEnvelope(
        action=ExecutionAction.CLOSE,
        authorization_certificate_id=UUID("00000000-0000-0000-0000-000000000304"),
        policy_hash=POLICY_HASH,
        account_fingerprint="sql-account-fingerprint",
        position_or_book_fingerprint="sql-book-fingerprint",
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.SELL_TO_CLOSE, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.BUY_TO_CLOSE, 1),
        ),
        quantity=2,
        minimum_limit=Decimal("1.00"),
        maximum_limit=Decimal("1.50"),
        approved_max_loss=Decimal("700"),
        event_key="PANW-2026-09-03",
        trading_day=date(2026, 9, 3),
    )


def roll_envelope(
    *,
    authorization_certificate_id: UUID = ROLL_AUTHORIZATION_ID,
    market_session_id: UUID = ROLL_SESSION_ID,
) -> OrderEnvelope:
    first = lifecycle_envelope()
    return replace(
        first,
        action=ExecutionAction.ROLL,
        authorization_certificate_id=authorization_certificate_id,
        legs=first.legs
        + (
            OrderLegIntent("DEMO261016C00105000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO261016C00110000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        event_key="PANW-2026-09-04",
        trading_day=date(2026, 9, 4),
        market_session_id=market_session_id,
        quoted_relative_spread=Decimal("0.05"),
        maximum_relative_spread=Decimal("0.25"),
        incremental_debit=Decimal("100"),
        maximum_incremental_debit=Decimal("500"),
    )


def add_lifecycle_assessment(
    repo: SQLAlchemyExecutionRepository,
    order: OrderEnvelope,
    *,
    assessment_id: UUID,
) -> None:
    repo.add_assessment_certificate(
        AssessmentCertificate(
            certificate_id=order.authorization_certificate_id,
            assessment_id=assessment_id,
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            account_role=AccountRole.SUBMISSION,
            action=order.action,
            position_fingerprint=order.position_or_book_fingerprint,
            envelope_hash=order_envelope_hash(order),
            approved_max_loss=order.approved_max_loss,
            quantity=order.quantity,
            expected_after_exposure=None,
            policy_hash=order.policy_hash,
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )


def repository(
    *,
    entry_limits: EntryBudgetLimits = ENTRY_LIMITS,
) -> SQLAlchemyExecutionRepository:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    return SQLAlchemyExecutionRepository(
        sessionmaker(engine, expire_on_commit=False),
        entry_limits=entry_limits,
    )


def file_repository(path: Path) -> SQLAlchemyExecutionRepository:
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    return SQLAlchemyExecutionRepository(
        sessionmaker(engine, expire_on_commit=False),
        entry_limits=ENTRY_LIMITS,
    )


def add_test_thesis(repo: SQLAlchemyExecutionRepository, policy_hash: str) -> None:
    repo.add_thesis_version(
        FrozenThesisVersion(
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            thesis_id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            account_role=AccountRole.SUBMISSION,
            version=1,
            thesis_hash="f" * 64,
            policy_hash=policy_hash,
            underlying="DEMO",
            thesis_code="TEST_THESIS",
            frozen_at=NOW - timedelta(days=1),
            target_at=NOW + timedelta(days=1),
            intended_exposure={},
            exposure_limits={},
            volatility_view="NEUTRAL",
            entry_atm_iv=Decimal("0.4"),
            approved_max_loss=Decimal("700"),
            portfolio_risk_cap=Decimal("700"),
            invalidation_codes=("TEST_INVALIDATION",),
            thesis_payload={"frozen": True},
            created_at=NOW - timedelta(days=1),
        )
    )


def test_frozen_thesis_payload_round_trips_without_loss() -> None:
    repo = repository()
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint="sql-account-fingerprint",
        equity=Decimal("100000"),
        autonomous_enabled=False,
    )
    add_test_thesis(repo, POLICY_HASH)

    thesis = repo.get_thesis_version(UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"))

    assert thesis.thesis_id == UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    assert thesis.policy_hash == POLICY_HASH
    assert thesis.invalidation_codes == ("TEST_INVALIDATION",)
    assert thesis.thesis_payload == {"frozen": True}


def prepare_entry(
    repo: SQLAlchemyExecutionRepository,
    approval: EntryApprovalAuthorization | None = None,
    *,
    capture_baseline: bool = True,
) -> OrderEnvelope:
    order = envelope()
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint=order.account_fingerprint,
        equity=Decimal("100000"),
        autonomous_enabled=True,
    )
    add_test_thesis(repo, order.policy_hash)
    repo.set_autonomous_enabled(AccountRole.SUBMISSION, True, actor=Actor.OWNER)
    if capture_baseline:
        repo.capture_baseline(
            role=AccountRole.SUBMISSION,
            fingerprint=order.account_fingerprint,
            equity=Decimal("100000"),
            captured_at=NOW,
            positions_hash="positions",
            orders_hash="orders",
            activities_hash="activities",
        )
    repo.add_entry_approval(
        approval
        or EntryApprovalAuthorization(
            approval_id=AUTH_ID,
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            account_role=AccountRole.SUBMISSION,
            policy_hash=order.policy_hash,
            book_fingerprint=order.position_or_book_fingerprint,
            envelope_hash=order_envelope_hash(order),
            approved_max_loss=order.approved_max_loss,
            quantity=order.quantity,
            valid_from=datetime(2020, 1, 1, tzinfo=UTC),
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, order)
    _attach_test_entry_origin(repo._sessions, order, INTENT_ID)
    return order


def _attach_test_entry_origin(sessions, order: OrderEnvelope, intent_id: UUID) -> None:
    """Attach a durable origin so execution tests exercise post-claim behavior."""
    boundary = NOW - timedelta(minutes=1)
    thesis_version_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    normalized = {"fixture": "sql_repository_entry"}
    input_hash = canonical_agent_hash(
        agent_input_material(
            account_role=AccountRole.SUBMISSION.value,
            account_fingerprint=order.account_fingerprint,
            decision_kind="OPPORTUNITY",
            decision_boundary=boundary,
            observed_at=boundary,
            normalized_input=normalized,
            thesis_version_id=thesis_version_id,
        )
    )
    snapshot_id = uuid5(NAMESPACE_URL, f"sql-test-agent-input:{intent_id}")
    decision_id = uuid5(NAMESPACE_URL, f"sql-test-agent-decision:{intent_id}")
    tick_id = uuid5(NAMESPACE_URL, f"sql-test-agent-tick:{intent_id}")
    result_payload = {"fixture": "sql_repository_entry"}
    result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash=input_hash,
            outcome="ENTRY_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash=order.policy_hash,
            thesis_version_id=thesis_version_id,
            result_payload=result_payload,
            authorization_id=order.authorization_certificate_id,
            intent_id=intent_id,
            intent_digest=intent_digest(order),
            autonomy_authorized=True,
        )
    )
    with sessions.begin() as session:
        session.execute(text("PRAGMA ignore_check_constraints=ON"))
        tick = AgentTickRow(
            tick_id=tick_id,
            account_role=AccountRole.SUBMISSION.value,
            account_fingerprint=order.account_fingerprint,
            tick_key=f"fixture:{intent_id}",
            tick_boundary=boundary,
            actor="SCHEDULER",
            status="RESERVED",
            reservation_token=uuid5(NAMESPACE_URL, f"sql-test-agent-reservation:{intent_id}"),
            created_at=boundary,
        )
        session.add(tick)
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=snapshot_id,
                thesis_version_id=thesis_version_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint=order.account_fingerprint,
                decision_kind="OPPORTUNITY",
                decision_boundary=boundary,
                observed_at=boundary,
                normalized_payload=normalized,
                input_hash=input_hash,
                created_at=boundary,
            )
        )
        session.flush()
        session.add(
            AgentDecisionRow(
                decision_id=decision_id,
                thesis_version_id=thesis_version_id,
                origin_tick_id=tick_id,
                input_snapshot_id=snapshot_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint=order.account_fingerprint,
                decision_kind="OPPORTUNITY",
                outcome="ENTRY_APPROVED",
                reason_code="POLICY_APPROVED",
                policy_hash=order.policy_hash,
                result_payload=result_payload,
                result_hash=result_hash,
                autonomy_authorized=True,
                decision_boundary=boundary,
                created_at=boundary,
            )
        )
        session.flush()
        tick.decision_id = decision_id
        approval = session.get(EntryApprovalCertificateRow, order.authorization_certificate_id)
        assert approval is not None
        approval.agent_decision_id = decision_id
        session.flush()
        session.execute(text("PRAGMA ignore_check_constraints=OFF"))


def prepare_development_entries(
    repo: SQLAlchemyExecutionRepository,
    *,
    count: int = 2,
) -> tuple[ExecutionIntent, ...]:
    account_fingerprint = "a" * 64
    policy_hash = "d" * 64
    now = datetime.now(UTC)
    repo.register_account(
        role=AccountRole.DEVELOPMENT,
        fingerprint=account_fingerprint,
        equity=Decimal("100000"),
        autonomous_enabled=False,
    )
    repo.set_autonomous_enabled(AccountRole.DEVELOPMENT, True, actor=Actor.OWNER)
    agent_repository = AgentDecisionRepository(repo._sessions, server_autonomy_enabled=True)
    intents: list[ExecutionIntent] = []
    for ordinal in range(1, count + 1):
        boundary = (now - timedelta(minutes=ordinal)).replace(second=0, microsecond=0)
        thesis_version_id = uuid5(NAMESPACE_URL, f"postgres-claim-thesis-version:{ordinal}")
        repo.add_thesis_version(
            FrozenThesisVersion(
                thesis_version_id=thesis_version_id,
                thesis_id=uuid5(NAMESPACE_URL, f"postgres-claim-thesis:{ordinal}"),
                account_role=AccountRole.DEVELOPMENT,
                version=ordinal,
                thesis_hash=f"{ordinal:064x}",
                policy_hash=policy_hash,
                underlying="DEMO",
                thesis_code="POSTGRES_CLAIM_TEST",
                frozen_at=boundary,
                target_at=now + timedelta(days=1),
                intended_exposure={},
                exposure_limits={},
                volatility_view="NEUTRAL",
                entry_atm_iv=Decimal("0.4"),
                approved_max_loss=Decimal("700"),
                portfolio_risk_cap=Decimal("700"),
                invalidation_codes=("TEST_INVALIDATION",),
                thesis_payload={"fixture": "postgres_claim", "ordinal": ordinal},
                created_at=boundary,
            )
        )
        authorization_id = uuid5(NAMESPACE_URL, f"postgres-claim-authorization:{ordinal}")
        intent_id = uuid5(NAMESPACE_URL, f"postgres-claim-intent:{ordinal}")
        order = replace(
            envelope(),
            authorization_certificate_id=authorization_id,
            policy_hash=policy_hash,
            account_fingerprint=account_fingerprint,
            event_key=f"POSTGRES-CLAIM-{ordinal}",
        )
        authorization = EntryApprovalAuthorization(
            approval_id=authorization_id,
            thesis_version_id=thesis_version_id,
            account_role=AccountRole.DEVELOPMENT,
            policy_hash=policy_hash,
            book_fingerprint=order.position_or_book_fingerprint,
            envelope_hash=order_envelope_hash(order),
            approved_max_loss=order.approved_max_loss,
            quantity=order.quantity,
            valid_from=datetime(2020, 1, 1, tzinfo=UTC),
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        tick = agent_repository.reserve_tick(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=account_fingerprint,
            actor="SCHEDULER",
            trusted_at=boundary,
            tick_key=f"postgres-claim:{ordinal}",
        )
        assert tick.reservation_token is not None
        agent_repository.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=account_fingerprint,
            decision_kind="OPPORTUNITY",
            decision_boundary=boundary,
            observed_at=boundary,
            normalized_input={"fixture": "postgres_claim", "ordinal": ordinal},
            outcome="ENTRY_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash=policy_hash,
            result_payload={"fixture": "postgres_claim"},
            thesis_version_id=thesis_version_id,
            authorization=authorization,
            envelope=order,
            intent_id=intent_id,
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )
        intent = repo.get_intent(intent_id)
        assert intent is not None
        intents.append(intent)
    return tuple(intents)


def prepare_lifecycle_pair(
    repo: SQLAlchemyExecutionRepository,
) -> tuple[ExecutionIntent, ExecutionIntent, OrderEnvelope, OrderEnvelope]:
    first = lifecycle_envelope()
    second = roll_envelope()
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint=first.account_fingerprint,
        equity=Decimal("100000"),
        autonomous_enabled=False,
    )
    add_test_thesis(repo, first.policy_hash)
    for order, assessment_id in (
        (first, UUID("00000000-0000-0000-0000-000000000311")),
        (second, UUID("00000000-0000-0000-0000-000000000312")),
    ):
        add_lifecycle_assessment(repo, order, assessment_id=assessment_id)
    first_intent = repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, first)
    second_intent = repo.approve_intent(
        UUID("00000000-0000-0000-0000-000000000313"),
        AccountRole.SUBMISSION,
        second,
    )
    _attach_test_lifecycle_origin(repo._sessions, first, first_intent.intent_id, ordinal=1)
    _attach_test_lifecycle_origin(repo._sessions, second, second_intent.intent_id, ordinal=2)
    _attach_test_managed_position(repo._sessions, first)
    return first_intent, second_intent, first, second


def _attach_test_lifecycle_origin(
    sessions, order: OrderEnvelope, intent_id: UUID, *, ordinal: int
) -> None:
    boundary = NOW - timedelta(minutes=ordinal)
    thesis_version_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    normalized = {"fixture": "sql_repository_lifecycle", "ordinal": ordinal}
    input_hash = canonical_agent_hash(
        agent_input_material(
            account_role=AccountRole.SUBMISSION.value,
            account_fingerprint=order.account_fingerprint,
            decision_kind="ASSESSMENT",
            decision_boundary=boundary,
            observed_at=boundary,
            normalized_input=normalized,
            thesis_version_id=thesis_version_id,
        )
    )
    snapshot_id = uuid5(NAMESPACE_URL, f"sql-test-agent-input:{intent_id}")
    decision_id = uuid5(NAMESPACE_URL, f"sql-test-agent-decision:{intent_id}")
    tick_id = uuid5(NAMESPACE_URL, f"sql-test-agent-tick:{intent_id}")
    outcome = "ROLL_APPROVED" if order.action is ExecutionAction.ROLL else "CLOSE_APPROVED"
    result_payload = {"fixture": "sql_repository_lifecycle"}
    result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash=input_hash,
            outcome=outcome,
            reason_code="POLICY_APPROVED",
            policy_hash=order.policy_hash,
            thesis_version_id=thesis_version_id,
            result_payload=result_payload,
            authorization_id=order.authorization_certificate_id,
            intent_id=intent_id,
            intent_digest=intent_digest(order),
            autonomy_authorized=True,
        )
    )
    with sessions.begin() as session:
        session.execute(text("PRAGMA ignore_check_constraints=ON"))
        tick = AgentTickRow(
            tick_id=tick_id,
            account_role=AccountRole.SUBMISSION.value,
            account_fingerprint=order.account_fingerprint,
            tick_key=f"fixture:{intent_id}",
            tick_boundary=boundary,
            actor="SCHEDULER",
            status="RESERVED",
            reservation_token=uuid5(NAMESPACE_URL, f"sql-test-agent-reservation:{intent_id}"),
            created_at=boundary,
        )
        session.add(tick)
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=snapshot_id,
                thesis_version_id=thesis_version_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint=order.account_fingerprint,
                decision_kind="ASSESSMENT",
                decision_boundary=boundary,
                observed_at=boundary,
                normalized_payload=normalized,
                input_hash=input_hash,
                created_at=boundary,
            )
        )
        session.flush()
        session.add(
            AgentDecisionRow(
                decision_id=decision_id,
                thesis_version_id=thesis_version_id,
                origin_tick_id=tick_id,
                input_snapshot_id=snapshot_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint=order.account_fingerprint,
                decision_kind="ASSESSMENT",
                outcome=outcome,
                reason_code="POLICY_APPROVED",
                policy_hash=order.policy_hash,
                result_payload=result_payload,
                result_hash=result_hash,
                autonomy_authorized=True,
                decision_boundary=boundary,
                created_at=boundary,
            )
        )
        session.flush()
        tick.decision_id = decision_id
        certificate = session.get(AssessmentCertificateRow, order.authorization_certificate_id)
        assert certificate is not None
        certificate.agent_decision_id = decision_id
        session.flush()
        session.execute(text("PRAGMA ignore_check_constraints=OFF"))


def _attach_test_managed_position(sessions, order: OrderEnvelope) -> None:
    managed_id = uuid5(NAMESPACE_URL, "sql-test-managed-position")
    snapshot_id = uuid5(NAMESPACE_URL, "sql-test-managed-snapshot")
    bind = sessions.kw["bind"]
    with bind.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
    try:
        with sessions.begin() as session:
            session.execute(
                text(
                    "INSERT INTO managed_lifecycle_positions "
                    "(managed_position_id, account_role, account_fingerprint, "
                    "entry_execution_certificate_id, entry_intent_id, entry_approval_id, "
                    "thesis_version_id, entry_reconciliation_id, "
                    "current_reconciliation_state_id, current_snapshot_id, "
                    "active_position_fingerprint, activated_at, closed_at) "
                    "VALUES (:managed_id, 'SUBMISSION', :fingerprint, :certificate_id, "
                    ":intent_id, :approval_id, :thesis_id, :reconciliation_id, "
                    ":state_id, NULL, :position_fingerprint, :activated_at, NULL)"
                ),
                {
                    "managed_id": managed_id.hex,
                    "fingerprint": order.account_fingerprint,
                    "certificate_id": uuid5(NAMESPACE_URL, "sql-test-entry-certificate").hex,
                    "intent_id": uuid5(NAMESPACE_URL, "sql-test-entry-intent").hex,
                    "approval_id": uuid5(NAMESPACE_URL, "sql-test-entry-approval").hex,
                    "thesis_id": UUID("ffffffff-ffff-ffff-ffff-ffffffffffff").hex,
                    "reconciliation_id": uuid5(NAMESPACE_URL, "sql-test-entry-reconciliation").hex,
                    "state_id": uuid5(NAMESPACE_URL, "sql-test-reconciliation-state").hex,
                    "position_fingerprint": order.position_or_book_fingerprint,
                    "activated_at": NOW - timedelta(days=1),
                },
            )
            session.execute(
                text(
                    "INSERT INTO managed_position_snapshots "
                    "(snapshot_id, managed_position_id, predecessor_snapshot_id, "
                    "transition_id, reconciliation_id, reconciliation_state_id, "
                    "normalized_inventory, inventory_hash, activity_manifest, "
                    "activity_manifest_hash, cumulative_cashflow, rolls_on_trading_day, "
                    "market_session_id, position_fingerprint, accepted_at, snapshot_hash) "
                    "VALUES (:snapshot_id, :managed_id, NULL, :transition_id, "
                    ":reconciliation_id, :state_id, '[]', :inventory_hash, '[]', "
                    ":activity_hash, 0, 0, :market_session_id, :position_fingerprint, "
                    ":accepted_at, :snapshot_hash)"
                ),
                {
                    "snapshot_id": snapshot_id.hex,
                    "managed_id": managed_id.hex,
                    "transition_id": uuid5(NAMESPACE_URL, "sql-test-entry-transition").hex,
                    "reconciliation_id": uuid5(
                        NAMESPACE_URL, "sql-test-snapshot-reconciliation"
                    ).hex,
                    "state_id": uuid5(NAMESPACE_URL, "sql-test-reconciliation-state").hex,
                    "inventory_hash": "1" * 64,
                    "activity_hash": "2" * 64,
                    "market_session_id": uuid5(NAMESPACE_URL, "sql-test-market-session").hex,
                    "position_fingerprint": order.position_or_book_fingerprint,
                    "accepted_at": NOW - timedelta(days=1),
                    "snapshot_hash": "3" * 64,
                },
            )
            session.execute(
                text(
                    "UPDATE managed_lifecycle_positions SET current_snapshot_id=:snapshot_id "
                    "WHERE managed_position_id=:managed_id"
                ),
                {"snapshot_id": snapshot_id.hex, "managed_id": managed_id.hex},
            )
    finally:
        with bind.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


def test_sql_execution_rejects_replay_and_new_accounts_start_non_autonomous() -> None:
    repo = repository()
    with pytest.raises(ExecutionBlocked, match="REPLAY_EXECUTION_FORBIDDEN"):
        repo.register_account(
            role=AccountRole.REPLAY,
            fingerprint="replay-fingerprint",
            equity=Decimal("100000"),
            autonomous_enabled=False,
        )

    order = envelope()
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint=order.account_fingerprint,
        equity=Decimal("100000"),
        autonomous_enabled=True,
    )
    add_test_thesis(repo, order.policy_hash)
    repo.capture_baseline(
        role=AccountRole.SUBMISSION,
        fingerprint=order.account_fingerprint,
        equity=Decimal("100000"),
        captured_at=NOW,
        positions_hash="positions",
        orders_hash="orders",
        activities_hash="activities",
    )
    repo.add_entry_approval(
        EntryApprovalAuthorization(
            approval_id=AUTH_ID,
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            account_role=AccountRole.SUBMISSION,
            policy_hash=order.policy_hash,
            book_fingerprint=order.position_or_book_fingerprint,
            envelope_hash=order_envelope_hash(order),
            approved_max_loss=order.approved_max_loss,
            quantity=order.quantity,
            valid_from=datetime(2020, 1, 1, tzinfo=UTC),
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, order)

    with pytest.raises(ExecutionBlocked, match="AUTONOMOUS_DISABLED"):
        _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)


def test_sanitized_fresh_migration_chain_pins_0001_and_stays_contiguous() -> None:
    original = (MIGRATIONS / "0001_execution_lineage.sql").read_bytes()
    assert sha256(original).hexdigest() == (
        "33217971754cb597cd40270a08e08a4d1a387447d3457c6b72ec2eaaa22ccce1"
    )
    migration = (MIGRATIONS / "0002_account_execution_lock.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS execution_locked" in migration
    assert "ck_account_execution_lock" in migration
    assert "uq_claimed_execution_lease" in migration


def test_attempt_observation_metadata_enforces_bounded_ordinal() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in AttemptObservationRow.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert checks["ck_attempt_observation_ordinal"] == "attempt_ordinal BETWEEN 0 AND 3"


def test_sql_claim_reconstructs_envelope_and_reserves_entry_atomically() -> None:
    repo = repository()
    order = prepare_entry(repo)

    claimed = _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)

    assert claimed.state == IntentState.CLAIMED
    assert claimed.envelope == order
    assert repo.get_intent(INTENT_ID) == claimed
    budget = repo.get_entry_budget(AccountRole.SUBMISSION)
    assert budget.reserved_intent_id == INTENT_ID
    assert budget.reserved_risk == Decimal("700")


@pytest.mark.parametrize(
    ("limits", "code"),
    [
        (replace(ENTRY_LIMITS, policy_hash="b" * 64), "ENTRY_POLICY_AUTHORITY_MISMATCH"),
        (replace(ENTRY_LIMITS, maximum_entry_quantity=1), "ENTRY_QUANTITY_EXHAUSTED"),
        (
            replace(ENTRY_LIMITS, maximum_position_loss=Decimal("600")),
            "ENTRY_POSITION_RISK_EXHAUSTED",
        ),
    ],
)
def test_sql_entry_claim_enforces_the_injected_policy_limits(
    limits: EntryBudgetLimits,
    code: str,
) -> None:
    repo = repository(entry_limits=limits)
    prepare_entry(repo)

    with pytest.raises(ExecutionBlocked, match=code):
        _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)


@pytest.mark.parametrize(
    ("role", "fingerprint", "code"),
    [
        (AccountRole.DEVELOPMENT, "development-account", "ACCOUNT_ROLE_MISMATCH"),
        (AccountRole.SUBMISSION, "wrong-submission-account", "ACCOUNT_FINGERPRINT_MISMATCH"),
    ],
)
def test_sql_claim_rejects_cross_role_or_account_before_claim_state(
    role: AccountRole,
    fingerprint: str,
    code: str,
) -> None:
    repo = repository()
    prepare_entry(repo)

    with pytest.raises(ExecutionBlocked, match=code):
        repo.claim_intent(
            INTENT_ID,
            Actor.SCHEDULER,
            now=NOW,
            account_role=role,
            account_fingerprint=fingerprint,
        )

    assert repo.get_intent(INTENT_ID).state is IntentState.APPROVED
    assert repo.get_entry_budget(AccountRole.SUBMISSION).reserved_intent_id is None
    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is False


def test_claims_receive_repository_fencing_and_a_bounded_lease() -> None:
    repo = repository()
    first_intent, second_intent, _, _ = prepare_lifecycle_pair(repo)

    first = _claim(repo, first_intent.intent_id, Actor.OWNER, now=NOW)

    assert isinstance(first.claim_token, UUID)
    assert first.claim_generation == 1
    assert first.execution_epoch == 0
    assert first.heartbeat_at == first.claimed_at
    assert first.lease_expires_at == first.claimed_at + timedelta(seconds=30)

    repo.release_unsubmitted_claim(first.intent_id)
    second = _claim(repo, second_intent.intent_id, Actor.OWNER, now=NOW)

    assert isinstance(second.claim_token, UUID)
    assert second.claim_token != first.claim_token
    assert second.claim_generation == 2
    assert second.execution_epoch == first.execution_epoch


def test_sql_intent_digest_is_idempotent_across_requested_ids() -> None:
    repo = repository()
    order = prepare_entry(repo)

    duplicate = repo.approve_intent(
        UUID("00000000-0000-0000-0000-000000000306"),
        AccountRole.SUBMISSION,
        order,
    )

    assert duplicate.intent_id == INTENT_ID
    assert duplicate.envelope == order


def test_sql_authorization_failure_rolls_back_claim_and_reservation() -> None:
    repo = repository()
    order = envelope()
    prepare_entry(
        repo,
        EntryApprovalAuthorization(
            approval_id=AUTH_ID,
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            account_role=AccountRole.SUBMISSION,
            policy_hash="policy-v9",
            book_fingerprint=order.position_or_book_fingerprint,
            envelope_hash=order_envelope_hash(order),
            approved_max_loss=order.approved_max_loss,
            quantity=order.quantity,
            valid_from=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=1),
        ),
    )

    with pytest.raises(ExecutionBlocked, match="AUTHORIZATION_POLICY_MISMATCH"):
        _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)

    intent = repo.get_intent(INTENT_ID)
    assert intent.state == IntentState.APPROVED
    assert repo.attempts_for(intent.intent_id) == ()
    assert repo.get_entry_budget(AccountRole.SUBMISSION).reserved_intent_id is None


def test_sql_double_claim_leaves_one_claim_and_no_attempt() -> None:
    repo = repository()
    prepare_entry(repo)

    _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)
    with pytest.raises(ExecutionBlocked, match="INTENT_ALREADY_CLAIMED"):
        _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)

    assert repo.get_intent(INTENT_ID).state == IntentState.CLAIMED
    assert repo.attempts_for(INTENT_ID) == ()


def test_sql_execution_lease_allows_only_one_claimed_intent_across_actions() -> None:
    repo = repository()
    first_intent, second_intent, _, _ = prepare_lifecycle_pair(repo)

    _claim(repo, first_intent.intent_id, Actor.OWNER, now=NOW)
    with pytest.raises(ExecutionBlocked, match="ACCOUNT_EXECUTION_LEASE_ACTIVE"):
        _claim(repo, second_intent.intent_id, Actor.OWNER, now=NOW)

    repo.release_unsubmitted_claim(first_intent.intent_id)
    assert _claim(repo, second_intent.intent_id, Actor.OWNER, now=NOW).state == (
        IntentState.CLAIMED
    )


def test_lifecycle_claim_rejects_a_different_active_position_identity() -> None:
    repo = repository()
    first_intent, _, _, _ = prepare_lifecycle_pair(repo)
    with repo._sessions.begin() as session:
        session.execute(
            text("UPDATE managed_lifecycle_positions SET active_position_fingerprint=:fingerprint"),
            {"fingerprint": "other-position-fingerprint"},
        )

    with pytest.raises(ExecutionBlocked, match="LIFECYCLE_POSITION_ORIGIN_MISMATCH"):
        _claim(repo, first_intent.intent_id, Actor.OWNER, now=NOW)

    assert repo.get_intent(first_intent.intent_id).state is IntentState.APPROVED


@pytest.mark.parametrize("terminal_outcome", ("CANCELED", "REJECTED", "EXPIRED"))
def test_terminal_roll_attempt_still_consumes_the_position_session_fence(
    terminal_outcome: str,
) -> None:
    assert terminal_outcome in {"CANCELED", "REJECTED", "EXPIRED"}
    repo = repository()
    _, first_intent, _, first_order = prepare_lifecycle_pair(repo)
    with repo._sessions.begin() as session:
        row = session.get(ExecutionIntentRow, first_intent.intent_id)
        assert row is not None
        row.state = IntentState.TERMINAL.value
        session.add(
            OrderAttemptRow(
                attempt_id=uuid4(),
                broker_permit_id=None,
                execution_intent_id=first_intent.intent_id,
                attempt_ordinal=0,
                client_order_id=f"roll-{terminal_outcome.lower()}-{uuid4().hex[:12]}",
                provider_order_id=f"provider-{uuid4().hex}",
                state=terminal_outcome,
                request_hash="e" * 64,
                limit_price=Decimal("1.00"),
                quote_hash=None,
                quote_source_timestamps=[],
                quote_retrieved_at=None,
                timing_authority_at=None,
                prior_request_hash=None,
                replaces_attempt_id=None,
                filled_quantity=0,
                quantity=first_order.quantity,
                fill_cash_flow=None,
            )
        )

    second = roll_envelope(
        authorization_certificate_id=uuid5(
            NAMESPACE_URL,
            f"roll-session-fence:{terminal_outcome}",
        )
    )
    add_lifecycle_assessment(
        repo,
        second,
        assessment_id=uuid5(NAMESPACE_URL, f"roll-session-assessment:{terminal_outcome}"),
    )

    with pytest.raises(ExecutionBlocked, match="INTENT_UNIQUENESS_CONFLICT"):
        repo.approve_intent(uuid4(), AccountRole.SUBMISSION, second)

    assert repo.get_intent(first_intent.intent_id).envelope == first_order


def test_roll_position_session_fence_allows_the_next_session() -> None:
    repo = repository()
    _, first_intent, _, _ = prepare_lifecycle_pair(repo)
    with repo._sessions.begin() as session:
        row = session.get(ExecutionIntentRow, first_intent.intent_id)
        assert row is not None
        row.state = IntentState.TERMINAL.value

    next_session = roll_envelope(
        authorization_certificate_id=uuid4(),
        market_session_id=uuid4(),
    )
    add_lifecycle_assessment(repo, next_session, assessment_id=uuid4())

    approved = repo.approve_intent(uuid4(), AccountRole.SUBMISSION, next_session)

    assert approved.envelope.market_session_id == next_session.market_session_id


def test_roll_relative_spread_authority_round_trips_at_decimal_precision() -> None:
    repo = repository()
    order = replace(
        roll_envelope(),
        quoted_relative_spread=Decimal("0.0263157895"),
        maximum_relative_spread=Decimal("0.0263157895"),
    )
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint=order.account_fingerprint,
        equity=Decimal("100000"),
        autonomous_enabled=False,
    )
    add_test_thesis(repo, order.policy_hash)
    add_lifecycle_assessment(repo, order, assessment_id=uuid4())

    approved = repo.approve_intent(uuid4(), AccountRole.SUBMISSION, order)

    assert repo.get_intent(approved.intent_id).envelope == order


def test_roll_relative_spread_authority_rejects_unpersistable_precision() -> None:
    with pytest.raises(ValueError, match="ROLL_AUTHORITY_OUT_OF_BOUNDS"):
        replace(
            roll_envelope(),
            quoted_relative_spread=Decimal(1) / Decimal(38),
            maximum_relative_spread=Decimal(1) / Decimal(38),
        )


def test_concurrent_distinct_roll_ticks_mint_at_most_one_intent(tmp_path: Path) -> None:
    repo = file_repository(tmp_path / "roll-fence.sqlite3")
    seed = lifecycle_envelope()
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint=seed.account_fingerprint,
        equity=Decimal("100000"),
        autonomous_enabled=False,
    )
    add_test_thesis(repo, seed.policy_hash)
    orders = (
        roll_envelope(authorization_certificate_id=uuid4()),
        roll_envelope(authorization_certificate_id=uuid4()),
    )
    for order in orders:
        add_lifecycle_assessment(repo, order, assessment_id=uuid4())
    barrier = Barrier(2)

    def approve(order: OrderEnvelope) -> str:
        barrier.wait()
        try:
            repo.approve_intent(uuid4(), AccountRole.SUBMISSION, order)
        except ExecutionBlocked as error:
            return str(error)
        return "APPROVED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(approve, orders))

    assert outcomes.count("APPROVED") == 1
    assert outcomes.count("INTENT_UNIQUENESS_CONFLICT") == 1


def test_sql_account_refresh_cannot_reenable_autonomy() -> None:
    repo = repository()
    order = prepare_entry(repo)
    repo.set_autonomous_enabled(AccountRole.SUBMISSION, False, actor=Actor.OWNER)

    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint=order.account_fingerprint,
        equity=Decimal("100000"),
        autonomous_enabled=True,
    )

    with pytest.raises(ExecutionBlocked, match="AUTONOMOUS_DISABLED"):
        _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)


def test_sql_bare_attempt_and_finalization_methods_are_retired() -> None:
    repo = repository()
    order = prepare_entry(repo)
    digest = intent_digest(order)
    identifier = client_order_id(order.trading_day, order.action, digest, 0)
    attempt = OrderAttempt(
        INTENT_ID,
        0,
        identifier,
        attempt_request_hash(digest, 0, identifier, order.minimum_limit, None),
        "PREPARED",
        quantity=order.quantity,
    )

    with pytest.raises(ExecutionBlocked, match="BROKER_PERMIT_REQUIRED"):
        repo.add_attempt(attempt)
    with pytest.raises(ExecutionBlocked, match="BROKER_PERMIT_REQUIRED"):
        repo.replace_attempt(attempt)

    claim = _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)
    candidate = ExecutionCertificate(
        certificate_id=uuid5(NAMESPACE_URL, f"alphadecay:execution:{digest}"),
        intent_id=INTENT_ID,
        entry_approval_id=AUTH_ID,
        assessment_certificate_id=None,
        execution_status="UNFILLED",
        attempt_ids=(identifier,),
        actual_exposure=None,
        reconciliation_checks=(
            "TERMINAL",
            "REMAINDER_ABSENT",
            "WHOLE_ACCOUNT_RECONCILED",
        ),
        created_at=NOW,
    )
    reconciliation = Reconciliation(
        terminal=True,
        remainder_absent=True,
        matches_expected=True,
        assignment_suspected=False,
        actual_exposure=None,
    )

    with pytest.raises(ExecutionBlocked, match="WHOLE_ACCOUNT_RECONCILIATION_REQUIRED"):
        repo.finalize_execution(candidate, reconciliation, "UNFILLED")

    assert repo.get_intent(INTENT_ID) == claim
    assert repo.attempts_for(INTENT_ID) == ()
    assert repo.get_entry_budget(AccountRole.SUBMISSION).entries_used == 0
    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is False


@pytest.mark.parametrize(
    ("gate", "actor", "code"),
    [
        ("owner", Actor.OWNER, "OWNER_ENTRY_FORBIDDEN"),
        ("autonomy", Actor.SCHEDULER, "AUTONOMOUS_DISABLED"),
        ("baseline", Actor.SCHEDULER, "SUBMISSION_BASELINE_REQUIRED"),
        ("contamination", Actor.SCHEDULER, "SUBMISSION_BASELINE_CONTAMINATED"),
        ("equity", Actor.SCHEDULER, "ENTRY_EQUITY_FLOOR"),
    ],
)
def test_sql_claim_safety_gate_rolls_back_all_claim_state(
    gate: str, actor: Actor, code: str
) -> None:
    repo = repository()
    prepare_entry(repo, capture_baseline=gate != "baseline")
    if gate == "autonomy":
        repo.set_autonomous_enabled(AccountRole.SUBMISSION, False, actor=Actor.OWNER)
    elif gate == "contamination":
        repo.observe_account_adjustment(AccountRole.SUBMISSION, "TRANSFER")
    elif gate == "equity":
        repo.register_account(
            role=AccountRole.SUBMISSION,
            fingerprint=envelope().account_fingerprint,
            equity=Decimal("99000"),
            autonomous_enabled=True,
        )

    with pytest.raises(ExecutionBlocked, match=code):
        _claim(repo, INTENT_ID, actor, now=NOW)

    assert repo.get_intent(INTENT_ID).state == IntentState.APPROVED
    assert repo.get_entry_budget(AccountRole.SUBMISSION).reserved_intent_id is None
    assert repo.attempts_for(INTENT_ID) == ()


@pytest.mark.skipif(
    "ALPHADECAY_TEST_POSTGRES_URL" not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
def test_postgres_runtime_upgrade_applies_0001_then_0002_idempotently() -> None:
    engine = create_engine(os.environ["ALPHADECAY_TEST_POSTGRES_URL"])
    if engine.dialect.name != "postgresql":
        pytest.fail("ALPHADECAY_TEST_POSTGRES_URL must use PostgreSQL")
    schema = f"execution_migration_{uuid4().hex}"
    migration_0001 = (MIGRATIONS / "0001_execution_lineage.sql").read_text()
    migration_0002 = (MIGRATIONS / "0002_account_execution_lock.sql").read_text()
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        with engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.exec_driver_sql(migration_0001)
        for _ in range(2):
            with engine.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                connection.exec_driver_sql(migration_0002)
        with engine.begin() as connection:
            columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema AND table_name = 'account_roles'"
                    ),
                    {"schema": schema},
                ).scalars()
            )
            constraints = set(
                connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE connamespace = CAST(:schema AS regnamespace)"
                    ),
                    {"schema": schema},
                ).scalars()
            )
            indexes = set(
                connection.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = :schema"),
                    {"schema": schema},
                ).scalars()
            )
        assert {
            "execution_locked",
            "execution_lock_reason",
            "execution_locked_at",
        } <= columns
        assert "ck_account_execution_lock" in constraints
        assert "uq_claimed_execution_lease" in indexes
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.mark.skipif(
    "ALPHADECAY_TEST_POSTGRES_URL" not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
@pytest.mark.skip(reason="active-intent uniqueness makes the distinct-intent premise invalid")
def test_postgres_distinct_intent_claim_race_has_one_account_lease_winner() -> None:
    engine = create_engine(os.environ["ALPHADECAY_TEST_POSTGRES_URL"])
    if engine.dialect.name != "postgresql":
        pytest.fail("ALPHADECAY_TEST_POSTGRES_URL must use PostgreSQL")
    schema = f"execution_lease_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        isolated_engine = engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(isolated_engine)
        repo = SQLAlchemyExecutionRepository(
            sessionmaker(isolated_engine, expire_on_commit=False),
            entry_limits=DEVELOPMENT_ENTRY_LIMITS,
        )
        first, second = prepare_development_entries(repo)
        barrier = Barrier(2)

        def claim(intent_id: UUID) -> tuple[UUID, str]:
            barrier.wait()
            try:
                status = repo.claim_intent(
                    intent_id,
                    Actor.SCHEDULER,
                    now=NOW,
                    account_role=AccountRole.DEVELOPMENT,
                    account_fingerprint="a" * 64,
                ).state.value
            except ExecutionBlocked as error:
                status = str(error)
            return intent_id, status

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(claim, (first.intent_id, second.intent_id)))
        assert sorted(status for _, status in outcomes) == [
            "ACCOUNT_EXECUTION_LEASE_ACTIVE",
            "CLAIMED",
        ]
        loser_id = next(intent_id for intent_id, status in outcomes if status != "CLAIMED")
        with pytest.raises(ExecutionBlocked, match="ACCOUNT_EXECUTION_LEASE_ACTIVE"):
            repo.claim_intent(
                loser_id,
                Actor.SCHEDULER,
                now=NOW,
                account_role=AccountRole.DEVELOPMENT,
                account_fingerprint="a" * 64,
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.mark.skipif(
    "ALPHADECAY_TEST_POSTGRES_URL" not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
@pytest.mark.skip(reason="active-intent uniqueness makes the distinct-intent premise invalid")
def test_postgres_claim_does_not_wait_on_preclaimed_intent_row_lock() -> None:
    engine = create_engine(os.environ["ALPHADECAY_TEST_POSTGRES_URL"])
    if engine.dialect.name != "postgresql":
        pytest.fail("ALPHADECAY_TEST_POSTGRES_URL must use PostgreSQL")
    schema = f"execution_lease_liveness_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        isolated_engine = engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(isolated_engine)
        repo = SQLAlchemyExecutionRepository(
            sessionmaker(isolated_engine, expire_on_commit=False),
            entry_limits=DEVELOPMENT_ENTRY_LIMITS,
        )
        first, second = prepare_development_entries(repo)
        repo.claim_intent(
            first.intent_id,
            Actor.SCHEDULER,
            now=NOW,
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint="a" * 64,
        )

        blocker = isolated_engine.connect()
        blocker_transaction = blocker.begin()
        blocker.execute(
            select(ExecutionIntentRow)
            .where(ExecutionIntentRow.intent_id == first.intent_id)
            .with_for_update()
        )
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            repo.claim_intent,
            second.intent_id,
            Actor.SCHEDULER,
            now=NOW,
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint="a" * 64,
        )
        try:
            with pytest.raises(ExecutionBlocked, match="ACCOUNT_EXECUTION_LEASE_ACTIVE"):
                future.result(timeout=3)
        finally:
            blocker_transaction.rollback()
            blocker.close()
            executor.shutdown(wait=True)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.mark.skipif(
    "ALPHADECAY_TEST_POSTGRES_URL" not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
def test_postgres_concurrent_claim_has_exactly_one_winner() -> None:
    engine = create_engine(os.environ["ALPHADECAY_TEST_POSTGRES_URL"])
    if engine.dialect.name != "postgresql":
        pytest.fail("ALPHADECAY_TEST_POSTGRES_URL must use PostgreSQL")
    schema = f"execution_lineage_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        isolated_engine = engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(isolated_engine)
        repo = SQLAlchemyExecutionRepository(
            sessionmaker(isolated_engine, expire_on_commit=False),
            entry_limits=DEVELOPMENT_ENTRY_LIMITS,
        )
        first = prepare_development_entries(repo, count=1)[0]
        barrier = Barrier(2)

        def claim() -> str:
            barrier.wait()
            try:
                return repo.claim_intent(
                    first.intent_id,
                    Actor.SCHEDULER,
                    now=NOW,
                    account_role=AccountRole.DEVELOPMENT,
                    account_fingerprint="a" * 64,
                ).state.value
            except ExecutionBlocked as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim) for _ in range(2)]
            outcomes = [future.result() for future in futures]
        assert sorted(outcomes) == ["CLAIMED", "INTENT_ALREADY_CLAIMED"]
        assert repo.get_entry_budget(AccountRole.DEVELOPMENT).reserved_intent_id == first.intent_id
        assert repo.attempts_for(first.intent_id) == ()
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


class FilledBroker:
    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        return BrokerResult("sql-broker-order", "FILLED", request.quantity, request.quantity)

    def reconcile(self, client_id: str) -> Reconciliation:
        return Reconciliation(
            terminal=True,
            remainder_absent=True,
            matches_expected=True,
            assignment_suspected=False,
            actual_exposure=GreekExposure(
                delta=Decimal("50"),
                gamma=Decimal("2"),
                theta_per_day=Decimal("-4"),
                vega_per_iv_point=Decimal("4"),
            ),
        )


class StablePreflight:
    def current_fingerprint(self, action: ExecutionAction) -> str:
        return "sql-book-fingerprint"


def test_sql_execution_service_requires_reconciliation_state_before_broker_write() -> None:
    repo = repository()
    prepare_entry(repo)
    broker = FilledBroker()

    with pytest.raises(ExecutionBlocked, match="RECONCILIATION_STATE_REQUIRED"):
        _execution_service(repo, broker, StablePreflight()).execute(INTENT_ID, Actor.SCHEDULER, NOW)

    assert repo.get_intent(INTENT_ID).state == IntentState.CLAIMED
    assert repo.attempts_for(INTENT_ID) == ()
