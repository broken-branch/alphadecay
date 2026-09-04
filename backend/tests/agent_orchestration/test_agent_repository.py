from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.execution import Actor, ExecutionBlocked, order_envelope_hash
from backend.app.execution.models import (
    AssessmentCertificate,
    EntryApprovalAuthorization,
    ExecutionAction,
    FrozenThesisVersion,
    OrderEnvelope,
    OrderLegIntent,
)
from backend.app.experiment_lineage import (
    ExperimentExecutionLineage,
    optional_experiment_execution_lineage,
)
from backend.app.experiments.repository import SQLAlchemyExperimentRegistry
from backend.app.order_limits import EntryBudgetLimits
from backend.app.persistence.agent_codec import encode_agent_value
from backend.app.persistence.agent_repository import AgentDecisionRepository
from backend.app.persistence.agent_service_repository import SQLAlchemyAgentServiceRepository
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    Base,
    CompetitionEntryBudgetRow,
    EntryApprovalCertificateRow,
    ExecutionIntentRow,
    ThesisVersionRow,
)
from backend.app.persistence.sqlalchemy_repository import SQLAlchemyExecutionRepository
from backend.app.policy import evaluate_opportunity
from backend.app.services import AgentDecision
from backend.tests.agent_orchestration.test_agent_run_service import approved_opportunity_bundle
from backend.tests.experiments.fixtures import compile_request, reviewed_request

FINGERPRINT = "a" * 64
BOUNDARY = datetime(2026, 8, 29, 16, tzinfo=UTC)
ENTRY_LIMITS = EntryBudgetLimits(
    policy_hash="c" * 64,
    equity_floor=Decimal("99000"),
    maximum_lifetime_entries=3,
    maximum_lifetime_risk=Decimal("1500"),
    maximum_position_loss=Decimal("800"),
    maximum_entry_quantity=4,
)


class FixedDatabaseClock:
    def now(self, _session: object) -> datetime:
        return BOUNDARY + timedelta(minutes=1)


def repository(
    *,
    role: AccountRole = AccountRole.SUBMISSION,
    autonomous_enabled: bool = False,
    server_autonomy_enabled: bool = False,
) -> tuple[AgentDecisionRepository, sessionmaker]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=role.value,
                account_fingerprint=FINGERPRINT,
                equity=Decimal("100000"),
                autonomous_enabled=autonomous_enabled,
            )
        )
        session.add(CompetitionEntryBudgetRow(account_role=role.value))
    repository = AgentDecisionRepository(
        sessions,
        database_clock=FixedDatabaseClock(),
        server_autonomy_enabled=server_autonomy_enabled,
    )
    repository.add_thesis_version(
        FrozenThesisVersion(
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            thesis_id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            account_role=role,
            version=1,
            thesis_hash="f" * 64,
            policy_hash="c" * 64,
            underlying="SPY",
            thesis_code="TEST_THESIS",
            frozen_at=BOUNDARY,
            target_at=BOUNDARY + timedelta(days=1),
            intended_exposure={},
            exposure_limits={},
            volatility_view="NEUTRAL",
            entry_atm_iv=Decimal("0.4"),
            approved_max_loss=Decimal("500"),
            portfolio_risk_cap=Decimal("500"),
            invalidation_codes=("TEST_INVALIDATION",),
            thesis_payload={"frozen": True},
            created_at=BOUNDARY,
        )
    )
    return (
        repository,
        sessions,
    )


def entry_proposal() -> tuple[EntryApprovalAuthorization, OrderEnvelope, UUID]:
    approval_id = uuid4()
    intent_id = uuid4()
    envelope = OrderEnvelope(
        action=ExecutionAction.ENTRY,
        authorization_certificate_id=approval_id,
        policy_hash="c" * 64,
        account_fingerprint=FINGERPRINT,
        position_or_book_fingerprint="d" * 64,
        legs=(
            OrderLegIntent("SPY260904P00400000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("SPY260904P00395000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        quantity=1,
        minimum_limit=Decimal("0.10"),
        maximum_limit=Decimal("0.20"),
        approved_max_loss=Decimal("500"),
        event_key="development-rehearsal",
        trading_day=date(2026, 8, 29),
    )
    authorization = EntryApprovalAuthorization(
        approval_id=approval_id,
        thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        account_role=AccountRole.DEVELOPMENT,
        policy_hash=envelope.policy_hash,
        book_fingerprint=envelope.position_or_book_fingerprint,
        envelope_hash=order_envelope_hash(envelope),
        approved_max_loss=envelope.approved_max_loss,
        quantity=envelope.quantity,
        valid_from=BOUNDARY - timedelta(hours=1),
        expires_at=BOUNDARY + timedelta(days=1),
    )
    return authorization, envelope, intent_id


def reserved_tick(
    repo: AgentDecisionRepository,
    role: AccountRole,
    *,
    actor: str = "SCHEDULER",
    boundary: datetime = BOUNDARY,
):
    return repo.reserve_tick(
        account_role=role,
        account_fingerprint=FINGERPRINT,
        actor=actor,
        trusted_at=boundary,
        tick_key=f"{role.value}:{actor}:{boundary.isoformat()}",
    )


def compiled_experiment_lineage(sessions: sessionmaker) -> ExperimentExecutionLineage:
    registry = SQLAlchemyExperimentRegistry(sessions)
    source = registry.create(reviewed_request(), created_at=BOUNDARY)
    compiled = registry.compile(
        source.experiment_id,
        compile_request(source.definition_hash),
        created_at=BOUNDARY,
    )
    return ExperimentExecutionLineage(
        experiment_id=source.experiment_id,
        source_definition_hash=source.definition_hash,
        protocol_hash=compiled.protocol_hash,
    )


def test_experiment_lineage_optional_boundary_rejects_partial_identity() -> None:
    assert optional_experiment_execution_lineage(None, None, None) is None
    with pytest.raises(ValueError, match="EXPERIMENT_EXECUTION_LINEAGE_INCOMPLETE"):
        optional_experiment_execution_lineage(uuid4(), "6" * 64, None)


def test_submission_calibration_no_trade_is_machine_binding_and_idempotent() -> None:
    repo, _ = repository()
    tick = reserved_tick(repo, AccountRole.SUBMISSION)

    first = repo.record_submission_calibration_no_trade(
        account_fingerprint=FINGERPRINT,
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={
            "calendar": "OPEN",
            "calibration_hash": "b" * 64,
            "machine_binding_hash": "d" * 64,
        },
        policy_hash="c" * 64,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )
    repeated = repo.record_submission_calibration_no_trade(
        account_fingerprint=FINGERPRINT,
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={
            "calendar": "OPEN",
            "calibration_hash": "b" * 64,
            "machine_binding_hash": "d" * 64,
        },
        policy_hash="c" * 64,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )

    assert repeated == first
    assert first.account_role is AccountRole.SUBMISSION
    assert first.outcome == "NO_TRADE"
    assert first.reason_code == "CALIBRATION_BINDING_NO_TRADE"
    assert first.intent_id is None
    assert repo.get_decision(first.decision_id) == first


def test_decision_and_exact_authorization_intent_are_atomic_and_immutable() -> None:
    repo, sessions = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    authorization, envelope, intent_id = entry_proposal()
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    recorded = repo.record_decision(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={"candidate": "SPY", "score": "0.8"},
        outcome="ENTRY_APPROVED",
        reason_code="POLICY_APPROVED",
        policy_hash=envelope.policy_hash,
        result_payload={"max_loss": "500"},
        thesis_version_id=authorization.thesis_version_id,
        authorization=authorization,
        envelope=envelope,
        intent_id=intent_id,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )

    assert recorded.intent_id == intent_id
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentInputSnapshotRow)) == 1
        assert session.scalar(select(func.count()).select_from(AgentDecisionRow)) == 1
        assert session.scalar(select(func.count()).select_from(EntryApprovalCertificateRow)) == 1
        assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 1
        snapshot = session.scalar(select(AgentInputSnapshotRow))
        decision = session.scalar(select(AgentDecisionRow))
        assert snapshot.thesis_version_id == authorization.thesis_version_id
        assert decision.thesis_version_id == authorization.thesis_version_id

    with pytest.raises(ExecutionBlocked, match="AGENT_INPUT_BOUNDARY_CONFLICT"):
        repo.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input={"candidate": "QQQ", "score": "0.8"},
            outcome="ENTRY_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash=envelope.policy_hash,
            result_payload={"max_loss": "500"},
            thesis_version_id=authorization.thesis_version_id,
            authorization=authorization,
            envelope=envelope,
            intent_id=intent_id,
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )


def test_experiment_decision_binds_compiled_lineage_through_entry_approval() -> None:
    repo, sessions = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    lineage = compiled_experiment_lineage(sessions)
    authorization, envelope, intent_id = entry_proposal()
    authorization = replace(authorization, experiment_lineage=lineage)
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    recorded = repo.record_decision(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={"candidate": "SPY", "score": "0.8"},
        outcome="ENTRY_APPROVED",
        reason_code="POLICY_APPROVED",
        policy_hash=envelope.policy_hash,
        result_payload={"max_loss": "500"},
        thesis_version_id=authorization.thesis_version_id,
        authorization=authorization,
        envelope=envelope,
        intent_id=intent_id,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
        experiment_lineage=lineage,
    )

    assert recorded.experiment_lineage == lineage
    assert repo.get_decision(recorded.decision_id).experiment_lineage == lineage
    with sessions() as session:
        decision = session.get(AgentDecisionRow, recorded.decision_id)
        approval = session.get(EntryApprovalCertificateRow, authorization.approval_id)
        assert decision is not None and approval is not None
        assert decision.experiment_id == lineage.experiment_id
        assert decision.experiment_source_definition_hash == lineage.source_definition_hash
        assert decision.experiment_protocol_hash == lineage.protocol_hash
        assert approval.experiment_id == lineage.experiment_id
        assert approval.experiment_source_definition_hash == lineage.source_definition_hash
        assert approval.experiment_protocol_hash == lineage.protocol_hash

    with pytest.raises(IntegrityError), sessions.begin() as session:
        approval = session.get(EntryApprovalCertificateRow, authorization.approval_id)
        assert approval is not None
        approval.experiment_protocol_hash = "9" * 64


def test_experiment_decision_rejects_mismatched_approval_lineage() -> None:
    repo, sessions = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    lineage = compiled_experiment_lineage(sessions)
    authorization, envelope, intent_id = entry_proposal()
    authorization = replace(
        authorization,
        experiment_lineage=replace(lineage, protocol_hash="9" * 64),
    )
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    with pytest.raises(ExecutionBlocked, match="EXPERIMENT_EXECUTION_LINEAGE_MISMATCH"):
        repo.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input={"candidate": "SPY"},
            outcome="ENTRY_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash=envelope.policy_hash,
            result_payload={},
            thesis_version_id=authorization.thesis_version_id,
            authorization=authorization,
            envelope=envelope,
            intent_id=intent_id,
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
            experiment_lineage=lineage,
        )


def test_experiment_decision_rejects_unregistered_compiled_lineage() -> None:
    repo, _ = repository(role=AccountRole.DEVELOPMENT)
    lineage = ExperimentExecutionLineage(
        experiment_id=uuid4(),
        source_definition_hash="8" * 64,
        protocol_hash="9" * 64,
    )
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    with pytest.raises(ExecutionBlocked, match="EXPERIMENT_EXECUTION_LINEAGE_INVALID"):
        repo.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input={"candidate": "SPY"},
            outcome="NO_TRADE",
            reason_code="POLICY_REJECTED",
            policy_hash="c" * 64,
            result_payload={},
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
            experiment_lineage=lineage,
        )


def test_submission_order_preview_renders_exact_durable_order_and_authority() -> None:
    repo, sessions = repository(
        role=AccountRole.SUBMISSION,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    bundle = approved_opportunity_bundle()
    values = replace(
        bundle.values,
        account=replace(bundle.values.account, account_role=AccountRole.SUBMISSION),
    )
    opportunity = evaluate_opportunity(bundle.policy, values)
    assert bundle.proposal is not None
    authorization = replace(
        bundle.proposal.authorization,
        account_role=AccountRole.SUBMISSION,
        thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        valid_from=BOUNDARY,
        expires_at=BOUNDARY + timedelta(minutes=5),
    )
    envelope = replace(
        bundle.proposal.intent.envelope,
        authorization_certificate_id=authorization.approval_id,
    )
    authorization = replace(authorization, envelope_hash=order_envelope_hash(envelope))
    with sessions.begin() as session:
        thesis = session.get(ThesisVersionRow, authorization.thesis_version_id)
        assert thesis is not None
        thesis.policy_hash = envelope.policy_hash
    tick = reserved_tick(repo, AccountRole.SUBMISSION)
    agent_decision = AgentDecision(
        code="ENTRY_APPROVED",
        decided_at=BOUNDARY,
        thesis_version_id=authorization.thesis_version_id,
        opportunity=opportunity,
        normalized_input=values,
    )
    repo.record_decision(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={"typed": encode_agent_value(values)},
        outcome="ENTRY_APPROVED",
        reason_code="ENTRY_APPROVED",
        policy_hash=envelope.policy_hash,
        result_payload={"typed": encode_agent_value(agent_decision)},
        thesis_version_id=authorization.thesis_version_id,
        authorization=authorization,
        envelope=envelope,
        intent_id=bundle.proposal.intent.intent_id,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )
    preview = SQLAlchemyAgentServiceRepository(
        repo, server_autonomy_enabled=True
    ).submission_order_preview(bundle.proposal.intent.intent_id)

    assert preview.thesis_code == "TEST_THESIS"
    assert preview.strategy == "BULL_CALL_DEBIT"
    assert preview.reason_codes == ("ENTRY_APPROVED",)
    assert preview.risk_cap == Decimal("500")
    assert preview.legs == envelope.legs
    assert preview.quantity == 4
    assert preview.limit_price == Decimal("1.20")
    assert preview.maximum_loss == Decimal("480.00")
    assert preview.account_role is AccountRole.SUBMISSION
    assert preview.account_fingerprint == FINGERPRINT
    assert preview.book_fingerprint == FINGERPRINT
    assert preview.approval_id == authorization.approval_id
    assert preview.policy_hash == opportunity.policy_hash
    assert preview.envelope_hash == order_envelope_hash(envelope)
    assert len(preview.decision_result_hash) == 64


def test_submission_order_preview_rejects_current_account_authority_drift() -> None:
    repo, sessions = repository(
        role=AccountRole.SUBMISSION,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    bundle = approved_opportunity_bundle()
    values = replace(
        bundle.values,
        account=replace(bundle.values.account, account_role=AccountRole.SUBMISSION),
    )
    opportunity = evaluate_opportunity(bundle.policy, values)
    assert bundle.proposal is not None
    authorization = replace(
        bundle.proposal.authorization,
        account_role=AccountRole.SUBMISSION,
        thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        valid_from=BOUNDARY,
        expires_at=BOUNDARY + timedelta(minutes=5),
    )
    envelope = replace(
        bundle.proposal.intent.envelope,
        authorization_certificate_id=authorization.approval_id,
    )
    authorization = replace(authorization, envelope_hash=order_envelope_hash(envelope))
    with sessions.begin() as session:
        thesis = session.get(ThesisVersionRow, authorization.thesis_version_id)
        assert thesis is not None
        thesis.policy_hash = envelope.policy_hash
    tick = reserved_tick(repo, AccountRole.SUBMISSION)
    repo.record_decision(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={"typed": encode_agent_value(values)},
        outcome="ENTRY_APPROVED",
        reason_code="ENTRY_APPROVED",
        policy_hash=envelope.policy_hash,
        result_payload={
            "typed": encode_agent_value(
                AgentDecision(
                    code="ENTRY_APPROVED",
                    decided_at=BOUNDARY,
                    thesis_version_id=authorization.thesis_version_id,
                    opportunity=opportunity,
                    normalized_input=values,
                )
            )
        },
        thesis_version_id=authorization.thesis_version_id,
        authorization=authorization,
        envelope=envelope,
        intent_id=bundle.proposal.intent.intent_id,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )
    with sessions.begin() as session:
        account = session.get(AccountRoleRow, AccountRole.SUBMISSION.value)
        assert account is not None
        account.account_fingerprint = "b" * 64

    with pytest.raises(ExecutionBlocked, match="SUBMISSION_ORDER_PREVIEW_AUTHORITY_INVALID"):
        SQLAlchemyAgentServiceRepository(
            repo, server_autonomy_enabled=True
        ).submission_order_preview(bundle.proposal.intent.intent_id)


def test_opportunity_decision_rejects_a_thesis_frozen_after_its_boundary() -> None:
    repo, sessions = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    with sessions.begin() as session:
        thesis = session.get(
            ThesisVersionRow,
            UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        )
        assert thesis is not None
        thesis.frozen_at = BOUNDARY + timedelta(seconds=1)
        thesis.target_at = BOUNDARY + timedelta(days=1)
    authorization, envelope, intent_id = entry_proposal()
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    with pytest.raises(
        ExecutionBlocked,
        match="AGENT_DECISION_THESIS_AUTHORITY_INVALID",
    ):
        repo.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input={"candidate": "SPY"},
            outcome="ENTRY_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash=envelope.policy_hash,
            result_payload={},
            thesis_version_id=authorization.thesis_version_id,
            authorization=authorization,
            envelope=envelope,
            intent_id=intent_id,
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )


def test_decision_rejects_a_missing_thesis_authority() -> None:
    repo, _ = repository(role=AccountRole.DEVELOPMENT)
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    with pytest.raises(ExecutionBlocked, match="THESIS_AUTHORITY_MISMATCH"):
        repo.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input={"candidate": "SPY"},
            outcome="NO_TRADE",
            reason_code="POLICY_REJECTED",
            policy_hash="c" * 64,
            result_payload={},
            thesis_version_id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )


def test_non_equal_thesis_time_order_accepts_opportunity_and_assessment() -> None:
    opportunity_repo, opportunity_sessions = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    with opportunity_sessions.begin() as session:
        thesis = session.get(
            ThesisVersionRow,
            UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        )
        assert thesis is not None
        thesis.frozen_at = BOUNDARY + timedelta(seconds=30)
    authorization, envelope, intent_id = entry_proposal()
    opportunity_tick = reserved_tick(opportunity_repo, AccountRole.DEVELOPMENT)
    opportunity = opportunity_repo.record_decision(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY + timedelta(seconds=45),
        normalized_input={"candidate": "SPY"},
        outcome="ENTRY_APPROVED",
        reason_code="POLICY_APPROVED",
        policy_hash=envelope.policy_hash,
        result_payload={},
        thesis_version_id=authorization.thesis_version_id,
        authorization=authorization,
        envelope=envelope,
        intent_id=intent_id,
        tick_id=opportunity_tick.tick_id,
        reservation_token=opportunity_tick.reservation_token,
    )
    assert opportunity.intent_id == intent_id

    assessment_repo, assessment_sessions = repository(role=AccountRole.DEVELOPMENT)
    with assessment_sessions.begin() as session:
        thesis = session.get(
            ThesisVersionRow,
            UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        )
        assert thesis is not None
        thesis.frozen_at = BOUNDARY - timedelta(seconds=30)
    assessment_tick = reserved_tick(assessment_repo, AccountRole.DEVELOPMENT)
    assessment = assessment_repo.record_decision(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=FINGERPRINT,
        decision_kind="ASSESSMENT",
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={"position": "SPY"},
        outcome="NO_ACTION",
        reason_code="NO_ACTION",
        policy_hash="c" * 64,
        result_payload={},
        thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        tick_id=assessment_tick.tick_id,
        reservation_token=assessment_tick.reservation_token,
    )
    assert assessment.intent_id is None


@pytest.mark.parametrize("mismatch", ("account", "policy"))
def test_authorized_intent_reconstructs_exact_thesis_authority(mismatch: str) -> None:
    repo, sessions = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    other_role = AccountRole.SUBMISSION if mismatch == "account" else AccountRole.DEVELOPMENT
    if mismatch == "account":
        with sessions.begin() as session:
            session.add(
                AccountRoleRow(
                    role=AccountRole.SUBMISSION.value,
                    account_fingerprint="9" * 64,
                    equity=Decimal("100000"),
                    autonomous_enabled=False,
                )
            )
    other_thesis_id = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    repo.add_thesis_version(
        FrozenThesisVersion(
            thesis_version_id=other_thesis_id,
            thesis_id=UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
            account_role=other_role,
            version=2 if other_role is AccountRole.DEVELOPMENT else 1,
            thesis_hash="8" * 64,
            policy_hash="7" * 64 if mismatch == "policy" else "c" * 64,
            underlying="SPY",
            thesis_code="OTHER_THESIS",
            frozen_at=BOUNDARY - timedelta(days=1),
            target_at=BOUNDARY + timedelta(days=1),
            intended_exposure={},
            exposure_limits={},
            volatility_view="NEUTRAL",
            entry_atm_iv=Decimal("0.4"),
            approved_max_loss=Decimal("500"),
            portfolio_risk_cap=Decimal("500"),
            invalidation_codes=("TEST_INVALIDATION",),
            thesis_payload={"frozen": True},
            created_at=BOUNDARY - timedelta(days=1),
        )
    )
    authorization, envelope, intent_id = entry_proposal()
    authorization = replace(authorization, thesis_version_id=other_thesis_id)
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    with pytest.raises(ExecutionBlocked, match="THESIS_AUTHORITY_MISMATCH"):
        repo.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input={"candidate": "SPY"},
            outcome="ENTRY_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash=envelope.policy_hash,
            result_payload={},
            thesis_version_id=other_thesis_id,
            authorization=authorization,
            envelope=envelope,
            intent_id=intent_id,
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )


def test_lifecycle_authority_requires_the_active_managed_position_identity() -> None:
    repo, _ = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    certificate_id = uuid4()
    envelope = OrderEnvelope(
        action=ExecutionAction.CLOSE,
        authorization_certificate_id=certificate_id,
        policy_hash="c" * 64,
        account_fingerprint=FINGERPRINT,
        position_or_book_fingerprint="d" * 64,
        legs=(
            OrderLegIntent("SPY260904P00400000", PositionIntent.SELL_TO_CLOSE, 1),
            OrderLegIntent("SPY260904P00395000", PositionIntent.BUY_TO_CLOSE, 1),
        ),
        quantity=1,
        minimum_limit=Decimal("0.10"),
        maximum_limit=Decimal("0.20"),
        approved_max_loss=Decimal("500"),
        event_key="development-close",
        trading_day=date(2026, 8, 29),
    )
    authorization = AssessmentCertificate(
        certificate_id=certificate_id,
        assessment_id=uuid4(),
        thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        account_role=AccountRole.DEVELOPMENT,
        action=ExecutionAction.CLOSE,
        position_fingerprint=envelope.position_or_book_fingerprint,
        envelope_hash=order_envelope_hash(envelope),
        approved_max_loss=envelope.approved_max_loss,
        quantity=envelope.quantity,
        expected_after_exposure=None,
        policy_hash=envelope.policy_hash,
        created_at=BOUNDARY - timedelta(minutes=1),
        expires_at=BOUNDARY + timedelta(minutes=5),
    )
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    with pytest.raises(ExecutionBlocked, match="LIFECYCLE_POSITION_AUTHORITY_MISMATCH"):
        repo.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            decision_kind="ASSESSMENT",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input={"position": "SPY"},
            outcome="CLOSE_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash=envelope.policy_hash,
            result_payload={},
            thesis_version_id=authorization.thesis_version_id,
            authorization=authorization,
            envelope=envelope,
            intent_id=uuid4(),
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )


def _persist_authorized_entry(
    repo: AgentDecisionRepository,
) -> tuple[EntryApprovalAuthorization, UUID]:
    authorization, envelope, intent_id = entry_proposal()
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)
    repo.record_decision(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=FINGERPRINT,
        decision_kind="OPPORTUNITY",
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={"candidate": "SPY"},
        outcome="ENTRY_APPROVED",
        reason_code="POLICY_APPROVED",
        policy_hash=envelope.policy_hash,
        result_payload={"max_loss": "500"},
        thesis_version_id=authorization.thesis_version_id,
        authorization=authorization,
        envelope=envelope,
        intent_id=intent_id,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )
    return authorization, intent_id


def test_claim_reconstructs_the_exact_durable_agent_origin() -> None:
    repo, sessions = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    _, intent_id = _persist_authorized_entry(repo)

    claimed = SQLAlchemyExecutionRepository(
        sessions,
        trusted_clock=FixedDatabaseClock(),
        entry_limits=ENTRY_LIMITS,
    ).claim_intent(
        intent_id,
        Actor.SCHEDULER,
        now=BOUNDARY + timedelta(minutes=1),
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=FINGERPRINT,
    )

    assert claimed.intent_id == intent_id


@pytest.mark.parametrize(
    "corruption",
    ("detached", "different_thesis", "other_policy", "other_account"),
)
def test_claim_rejects_corrupted_durable_agent_origin(corruption: str) -> None:
    repo, sessions = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    authorization, intent_id = _persist_authorized_entry(repo)
    with sessions.begin() as session:
        approval = session.get(EntryApprovalCertificateRow, authorization.approval_id)
        decision = session.scalar(select(AgentDecisionRow))
        snapshot = session.scalar(select(AgentInputSnapshotRow))
        assert approval is not None and decision is not None and snapshot is not None
        if corruption == "detached":
            approval.agent_decision_id = None
        elif corruption == "different_thesis":
            other_id = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
            session.add(
                ThesisVersionRow(
                    thesis_version_id=other_id,
                    thesis_id=UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
                    account_role=AccountRole.DEVELOPMENT.value,
                    version=2,
                    origin_hash="8" * 64,
                    thesis_hash="8" * 64,
                    policy_hash="c" * 64,
                    underlying="SPY",
                    thesis_code="OTHER_THESIS",
                    frozen_at=BOUNDARY - timedelta(days=1),
                    target_at=BOUNDARY + timedelta(days=1),
                    intended_exposure={},
                    exposure_limits={},
                    volatility_view="NEUTRAL",
                    entry_atm_iv=Decimal("0.4"),
                    approved_max_loss=Decimal("500"),
                    portfolio_risk_cap=Decimal("500"),
                    invalidation_codes=["TEST_INVALIDATION"],
                    thesis_payload={"frozen": True},
                    created_at=BOUNDARY - timedelta(days=1),
                )
            )
            session.flush()
            approval.thesis_version_id = other_id
            decision.thesis_version_id = other_id
            snapshot.thesis_version_id = other_id
        elif corruption == "other_policy":
            decision.policy_hash = "7" * 64
        else:
            snapshot.account_fingerprint = "9" * 64

    with pytest.raises(ExecutionBlocked, match="AUTHORIZATION_DECISION_ORIGIN_MISMATCH"):
        SQLAlchemyExecutionRepository(
            sessions,
            trusted_clock=FixedDatabaseClock(),
            entry_limits=ENTRY_LIMITS,
        ).claim_intent(
            intent_id,
            Actor.SCHEDULER,
            now=BOUNDARY + timedelta(minutes=1),
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
        )


def test_submission_opportunity_cannot_persist_authority() -> None:
    repo, _ = repository()
    tick = reserved_tick(repo, AccountRole.SUBMISSION)

    with pytest.raises(ExecutionBlocked, match="SUBMISSION_CALIBRATION_NO_TRADE_REQUIRED"):
        repo.record_decision(
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input={"candidate": "SPY"},
            outcome="ENTRY_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash="c" * 64,
            result_payload={},
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )


def test_development_no_trade_cannot_create_entry_authority() -> None:
    repo, _ = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    authorization, envelope, intent_id = entry_proposal()
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    with pytest.raises(ExecutionBlocked, match="ENTRY_AUTHORIZATION_KIND_MISMATCH"):
        repo.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input={"candidate": "SPY"},
            outcome="NO_TRADE",
            reason_code="POLICY_REJECTED",
            policy_hash=envelope.policy_hash,
            result_payload={},
            thesis_version_id=authorization.thesis_version_id,
            authorization=authorization,
            envelope=envelope,
            intent_id=intent_id,
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )


@pytest.mark.parametrize(
    ("persistent_autonomy", "server_autonomy", "expected"),
    (
        (False, True, "AGENT_AUTHORITY_AUTONOMY_DISABLED"),
        (True, False, "AGENT_AUTHORITY_SERVER_GATE_DISABLED"),
    ),
)
def test_authority_creation_rechecks_both_autonomy_gates_atomically(
    persistent_autonomy: bool, server_autonomy: bool, expected: str
) -> None:
    repo, sessions = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=persistent_autonomy,
        server_autonomy_enabled=server_autonomy,
    )
    authorization, envelope, intent_id = entry_proposal()
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    with pytest.raises(ExecutionBlocked, match=expected):
        repo.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY + timedelta(seconds=10),
            normalized_input={"candidate": "SPY"},
            outcome="ENTRY_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash=envelope.policy_hash,
            result_payload={},
            thesis_version_id=authorization.thesis_version_id,
            authorization=authorization,
            envelope=envelope,
            intent_id=intent_id,
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentInputSnapshotRow)) == 0
        assert session.scalar(select(func.count()).select_from(AgentDecisionRow)) == 0
        assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0


def test_latch_race_cannot_create_authorization_or_intent() -> None:
    repo, sessions = repository(
        role=AccountRole.DEVELOPMENT,
        autonomous_enabled=True,
        server_autonomy_enabled=True,
    )
    with sessions.begin() as session:
        account = session.get(AccountRoleRow, AccountRole.DEVELOPMENT.value)
        assert account is not None
        account.execution_locked = True
        account.execution_lock_reason = "RECONCILIATION_MISMATCH"
        account.execution_locked_at = BOUNDARY
        account.execution_lock_id = uuid4()
        account.execution_lock_generation = 1
    authorization, envelope, intent_id = entry_proposal()
    tick = reserved_tick(repo, AccountRole.DEVELOPMENT)

    with pytest.raises(ExecutionBlocked, match="AGENT_AUTHORITY_ACCOUNT_LATCHED"):
        repo.record_decision(
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY + timedelta(seconds=10),
            normalized_input={"candidate": "SPY"},
            outcome="ENTRY_APPROVED",
            reason_code="POLICY_APPROVED",
            policy_hash=envelope.policy_hash,
            result_payload={},
            thesis_version_id=authorization.thesis_version_id,
            authorization=authorization,
            envelope=envelope,
            intent_id=intent_id,
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
        )

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentDecisionRow)) == 0
        assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0


def test_tick_boundary_is_idempotent_and_conflicts_fail_closed() -> None:
    repo, _ = repository()
    tick = reserved_tick(repo, AccountRole.SUBMISSION)
    decision = repo.record_submission_calibration_no_trade(
        account_fingerprint=FINGERPRINT,
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={
            "calendar": "OPEN",
            "calibration_hash": "b" * 64,
            "machine_binding_hash": "d" * 64,
        },
        policy_hash="c" * 64,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )

    first = repo.complete_tick(
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
        terminal_code="DECISION_RECORDED",
        decision_id=decision.decision_id,
        execution_certificate_id=None,
    )
    assert (
        repo.complete_tick(
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
            terminal_code="DECISION_RECORDED",
            decision_id=decision.decision_id,
            execution_certificate_id=None,
        )
        == first
    )

    owner_tick = reserved_tick(
        repo, AccountRole.SUBMISSION, actor="OWNER", boundary=BOUNDARY - timedelta(minutes=1)
    )
    with pytest.raises(ExecutionBlocked, match="AGENT_TICK_DECISION_MISMATCH"):
        repo.complete_tick(
            tick_id=owner_tick.tick_id,
            reservation_token=owner_tick.reservation_token,
            terminal_code="FILLED",
            decision_id=decision.decision_id,
            execution_certificate_id=None,
        )


def test_tick_is_reserved_before_work_and_completed_with_proof() -> None:
    repo, sessions = repository()

    reservation = repo.reserve_tick(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
        actor="SCHEDULER",
        trusted_at=BOUNDARY,
        tick_key="submission:2026-08-29T16:00:00Z",
    )
    duplicate = repo.reserve_tick(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=FINGERPRINT,
        actor="SCHEDULER",
        trusted_at=BOUNDARY,
        tick_key="submission:2026-08-29T16:00:00Z",
    )

    assert reservation.accepted is True
    assert reservation.reservation_token is not None
    assert reservation.completed is False
    assert duplicate.accepted is False
    assert duplicate.reservation_token is None
    decision = repo.record_submission_calibration_no_trade(
        account_fingerprint=FINGERPRINT,
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={
            "calendar": "OPEN",
            "calibration_hash": "b" * 64,
            "machine_binding_hash": "d" * 64,
        },
        policy_hash="c" * 64,
        tick_id=reservation.tick_id,
        reservation_token=reservation.reservation_token,
    )
    completed = repo.complete_tick(
        tick_id=reservation.tick_id,
        reservation_token=reservation.reservation_token,
        terminal_code="CALIBRATION_BINDING_NO_TRADE",
        decision_id=decision.decision_id,
        execution_certificate_id=None,
    )

    assert completed.completed is True
    assert completed.terminal_code == "CALIBRATION_BINDING_NO_TRADE"
    assert completed.proof_hash is not None
    assert len(completed.proof_hash) == 64
    restarted = AgentDecisionRepository(sessions, database_clock=FixedDatabaseClock())
    assert (
        restarted.reserve_tick(
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
            actor="SCHEDULER",
            trusted_at=BOUNDARY,
            tick_key="submission:2026-08-29T16:00:00Z",
        )
        == completed
    )


def test_linked_no_trade_decision_cannot_claim_filled_without_certificate() -> None:
    repo, _ = repository()
    tick = reserved_tick(repo, AccountRole.SUBMISSION)
    decision = repo.record_submission_calibration_no_trade(
        account_fingerprint=FINGERPRINT,
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={
            "calibration_hash": "b" * 64,
            "machine_binding_hash": "d" * 64,
        },
        policy_hash="c" * 64,
        tick_id=tick.tick_id,
        reservation_token=tick.reservation_token,
    )

    with pytest.raises(ExecutionBlocked, match="AGENT_TICK_CERTIFICATE_REQUIRED"):
        repo.complete_tick(
            tick_id=tick.tick_id,
            reservation_token=tick.reservation_token,
            terminal_code="FILLED",
            decision_id=decision.decision_id,
            execution_certificate_id=None,
        )


def test_account_authority_read_preserves_autonomy_off_and_latch_state() -> None:
    repo, _ = repository()

    authority = repo.get_account_authority(AccountRole.SUBMISSION, account_fingerprint=FINGERPRINT)

    assert authority.autonomous_enabled is False
    assert authority.execution_locked is False
    assert authority.recovery_pending is False


def test_normalized_json_key_order_does_not_change_decision_identity() -> None:
    first_repo, _ = repository()
    second_repo, _ = repository()
    first_tick = reserved_tick(first_repo, AccountRole.SUBMISSION)
    second_tick = reserved_tick(second_repo, AccountRole.SUBMISSION)

    first = first_repo.record_submission_calibration_no_trade(
        account_fingerprint=FINGERPRINT,
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={
            "calendar": "OPEN",
            "calibration_hash": "b" * 64,
            "machine_binding_hash": "d" * 64,
        },
        policy_hash="c" * 64,
        tick_id=first_tick.tick_id,
        reservation_token=first_tick.reservation_token,
    )
    second = second_repo.record_submission_calibration_no_trade(
        account_fingerprint=FINGERPRINT,
        decision_boundary=BOUNDARY,
        observed_at=BOUNDARY,
        normalized_input={
            "machine_binding_hash": "d" * 64,
            "calibration_hash": "b" * 64,
            "calendar": "OPEN",
        },
        policy_hash="c" * 64,
        tick_id=second_tick.tick_id,
        reservation_token=second_tick.reservation_token,
    )

    assert second.input_hash == first.input_hash
    assert second.result_hash == first.result_hash
    assert second.decision_id == first.decision_id
