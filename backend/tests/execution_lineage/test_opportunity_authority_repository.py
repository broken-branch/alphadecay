from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.execution import (
    ExecutionAction,
    OrderEnvelope,
    OrderLegIntent,
    intent_digest,
    order_envelope_hash,
)
from backend.app.persistence.agent_authority import (
    agent_input_material,
    agent_result_material,
    canonical_agent_hash,
)
from backend.app.persistence.opportunity_authority import (
    DevelopmentRoute,
    OpportunityAuthorityError,
    SQLAlchemyOpportunityAuthorityRepository,
)
from backend.app.persistence.runtime import apply_migrations, discover_migrations
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    AgentDecisionRow,
    AgentInputSnapshotRow,
    Base,
    CompetitionEntryBudgetRow,
    EntryApprovalCertificateRow,
    ExecutionIntentRow,
    GreekAuthorityVersionRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ThesisVersionRow,
)
from backend.app.policy import OpportunityOutcome
from backend.app.services.opportunity_selection import GreekUnitConvention

NOW = datetime(2026, 8, 30, 15, tzinfo=UTC)
BOUNDARY = NOW - timedelta(minutes=5)
TRADING_DAY = date(2026, 8, 30)
ACCOUNT = "a" * 64
POSITION = "b" * 64
POLICY = "c" * 64
THESIS = "d" * 64
POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"


def _repository():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                autonomous_enabled=True,
            )
        )
        session.add(
            CompetitionEntryBudgetRow(
                account_role=AccountRole.DEVELOPMENT.value,
                entries_used=0,
                gross_approved_risk=Decimal(0),
                reserved_intent_id=None,
                reserved_risk=Decimal(0),
            )
        )
    return SQLAlchemyOpportunityAuthorityRepository(sessions), sessions, engine


def _intent(
    value: int,
    *,
    event_key: str = "event-1",
    trading_day: date = TRADING_DAY,
    state: str = "TERMINAL",
    consumed: bool = False,
    risk: Decimal = Decimal("500"),
) -> tuple[ExecutionIntentRow, EntryApprovalCertificateRow]:
    approval_id = UUID(int=1000 + value)
    envelope = OrderEnvelope(
        action=ExecutionAction.ENTRY,
        authorization_certificate_id=approval_id,
        policy_hash=POLICY,
        account_fingerprint=ACCOUNT,
        position_or_book_fingerprint=POSITION,
        legs=(
            OrderLegIntent(
                "NVDA260918C00170000",
                PositionIntent.BUY_TO_OPEN,
                1,
            ),
            OrderLegIntent(
                "NVDA260918C00175000",
                PositionIntent.SELL_TO_OPEN,
                1,
            ),
        ),
        quantity=1,
        minimum_limit=Decimal("1"),
        maximum_limit=Decimal("1.10"),
        approved_max_loss=risk,
        event_key=event_key,
        trading_day=trading_day,
    )
    envelope_payload = {
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
    }
    intent = ExecutionIntentRow(
        intent_id=UUID(int=value),
        account_role=AccountRole.DEVELOPMENT.value,
        intent_digest=intent_digest(envelope),
        action="ENTRY",
        policy_hash=POLICY,
        event_key=event_key,
        trading_day=trading_day,
        entry_approval_id=approval_id,
        assessment_certificate_id=None,
        fingerprint=POSITION,
        envelope_hash=order_envelope_hash(envelope),
        envelope_payload=envelope_payload,
        legs=envelope_payload["legs"],
        quantity=1,
        minimum_limit=Decimal("1"),
        maximum_limit=Decimal("1.10"),
        approved_max_loss=risk,
        state=state,
        claimed_by="SCHEDULER" if state == "CLAIMED" else None,
        claimed_at=BOUNDARY if state == "CLAIMED" else None,
        claim_token=UUID(int=3000 + value) if state == "CLAIMED" else None,
        claim_generation=1 if state == "CLAIMED" else 0,
        execution_epoch=1 if state == "CLAIMED" else 0,
        heartbeat_at=BOUNDARY if state == "CLAIMED" else None,
        lease_expires_at=(BOUNDARY + timedelta(minutes=1) if state == "CLAIMED" else None),
        first_fill_consumed=consumed,
    )
    approval = EntryApprovalCertificateRow(
        approval_id=approval_id,
        thesis_version_id=UUID(int=2000 + value),
        agent_decision_id=None,
        account_role=AccountRole.DEVELOPMENT.value,
        policy_hash=POLICY,
        book_fingerprint=POSITION,
        envelope_hash=order_envelope_hash(envelope),
        approved_max_loss=risk,
        quantity=1,
        valid_from=BOUNDARY,
        expires_at=NOW + timedelta(minutes=1),
        valid=True,
    )
    return intent, approval


def _managed_position(
    value: int, fingerprint: str
) -> tuple[ManagedLifecyclePositionRow, ManagedPositionSnapshotRow]:
    position_id = UUID(int=value)
    snapshot_id = UUID(int=value + 100)
    position = ManagedLifecyclePositionRow(
        managed_position_id=position_id,
        account_role=AccountRole.DEVELOPMENT.value,
        account_fingerprint=ACCOUNT,
        entry_execution_certificate_id=UUID(int=value + 200),
        entry_intent_id=UUID(int=value + 300),
        entry_approval_id=UUID(int=value + 400),
        thesis_version_id=UUID(int=value + 500),
        entry_reconciliation_id=UUID(int=value + 600),
        current_reconciliation_state_id=UUID(int=value + 700),
        current_snapshot_id=snapshot_id,
        active_position_fingerprint=fingerprint,
        activated_at=NOW - timedelta(days=1),
        closed_at=None,
    )
    snapshot = ManagedPositionSnapshotRow(
        snapshot_id=snapshot_id,
        managed_position_id=position_id,
        predecessor_snapshot_id=None,
        transition_id=UUID(int=value + 800),
        reconciliation_id=UUID(int=value + 900),
        reconciliation_state_id=UUID(int=value + 700),
        normalized_inventory=[],
        inventory_hash=fingerprint,
        activity_manifest=[],
        activity_manifest_hash="8" * 64,
        cumulative_cashflow=Decimal("-100"),
        rolls_on_trading_day=0,
        market_session_id=UUID(int=value + 1000),
        position_fingerprint=fingerprint,
        accepted_at=NOW - timedelta(days=1),
        snapshot_hash=f"{value % 10}" * 64,
    )
    return position, snapshot


def test_route_distinguishes_empty_and_one_managed_position() -> None:
    repository, sessions, engine = _repository()

    empty = repository.load_development_route(expected_account_fingerprint=ACCOUNT)
    assert empty.route is DevelopmentRoute.EMPTY
    assert empty.active_position_count == 0

    position, snapshot = _managed_position(1, POSITION)
    with sessions.begin() as session:
        session.add_all((position, snapshot))

    active = repository.load_development_route(expected_account_fingerprint=ACCOUNT)
    assert active.route is DevelopmentRoute.MANAGED_POSITION
    assert active.managed_position_id == position.managed_position_id
    assert active.position_fingerprint == POSITION
    assert active.account_fingerprint == ACCOUNT
    assert active.position_fingerprint != active.account_fingerprint
    assert active.authority_hash != empty.authority_hash
    engine.dispose()


def test_route_exposes_ambiguous_state_without_selecting_a_position() -> None:
    repository, sessions, engine = _repository()
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_active_managed_position_role"))
    first = _managed_position(1, POSITION)
    second = _managed_position(2, "f" * 64)
    with sessions.begin() as session:
        session.add_all((*first, *second))

    authority = repository.load_development_route(expected_account_fingerprint=ACCOUNT)

    assert authority.route is DevelopmentRoute.AMBIGUOUS
    assert authority.active_position_count == 2
    assert authority.managed_position_id is None
    assert authority.position_fingerprint is None
    engine.dispose()


def test_route_rejects_account_substitution_and_snapshot_mismatch() -> None:
    repository, sessions, engine = _repository()
    with pytest.raises(OpportunityAuthorityError, match="DEVELOPMENT_ACCOUNT_MISMATCH"):
        repository.load_development_route(expected_account_fingerprint="1" * 64)

    position, snapshot = _managed_position(1, POSITION)
    snapshot.position_fingerprint = "1" * 64
    with sessions.begin() as session:
        session.add_all((position, snapshot))
    with pytest.raises(OpportunityAuthorityError, match="MANAGED_POSITION_FINGERPRINT_MISMATCH"):
        repository.load_development_route(expected_account_fingerprint=ACCOUNT)
    engine.dispose()


def test_route_rejects_hash_shaped_authority_failure_even_when_ambiguous() -> None:
    repository, sessions, engine = _repository()
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_active_managed_position_role"))
    first = _managed_position(1, POSITION)
    second = _managed_position(2, "not-a-position-fingerprint")
    with sessions.begin() as session:
        session.add_all((*first, *second))

    with pytest.raises(OpportunityAuthorityError, match="POSITION_FINGERPRINT_INVALID"):
        repository.load_development_route(expected_account_fingerprint=ACCOUNT)
    engine.dispose()


def test_entry_history_binds_budget_consumption_and_same_event_day() -> None:
    repository, sessions, engine = _repository()
    consumed = _intent(1, consumed=True)
    prior = _intent(2, event_key="other-event", trading_day=date(2026, 8, 29))
    with sessions.begin() as session:
        session.add_all((*consumed, *prior))
        budget = session.get(CompetitionEntryBudgetRow, AccountRole.DEVELOPMENT.value)
        assert budget is not None
        budget.entries_used = 1
        budget.gross_approved_risk = Decimal("500")

    authority = repository.load_entry_history(
        expected_account_fingerprint=ACCOUNT,
        event_key="event-1",
        trading_day=TRADING_DAY,
    )

    assert authority.entries_used == 1
    assert authority.gross_approved_risk == Decimal("500")
    assert authority.event_already_attempted is True
    assert authority.entry_intent_count == 2
    assert authority.clean_equity == Decimal("100000")
    engine.dispose()


def test_entry_history_rejects_counter_and_reservation_substitution() -> None:
    repository, sessions, engine = _repository()
    with sessions.begin() as session:
        session.add_all(_intent(1, consumed=True))
    with pytest.raises(OpportunityAuthorityError, match="ENTRY_BUDGET_HISTORY_MISMATCH"):
        repository.load_entry_history(
            expected_account_fingerprint=ACCOUNT,
            event_key="event-1",
            trading_day=TRADING_DAY,
        )

    with sessions.begin() as session:
        budget = session.get(CompetitionEntryBudgetRow, AccountRole.DEVELOPMENT.value)
        assert budget is not None
        budget.entries_used = 1
        budget.gross_approved_risk = Decimal("500")
        budget.reserved_intent_id = UUID(int=999)
        budget.reserved_risk = Decimal("500")
    with pytest.raises(OpportunityAuthorityError, match="ENTRY_RESERVATION_MISMATCH"):
        repository.load_entry_history(
            expected_account_fingerprint=ACCOUNT,
            event_key="event-1",
            trading_day=TRADING_DAY,
        )
    engine.dispose()


def test_entry_history_rejects_unbound_envelope_and_unreserved_claim() -> None:
    repository, sessions, engine = _repository()
    intent, approval = _intent(1)
    intent.envelope_payload = {**intent.envelope_payload, "account_fingerprint": "9" * 64}
    with sessions.begin() as session:
        session.add_all((intent, approval))
    with pytest.raises(OpportunityAuthorityError, match="ENTRY_HISTORY_INVALID"):
        repository.load_entry_history(
            expected_account_fingerprint=ACCOUNT,
            event_key="event-1",
            trading_day=TRADING_DAY,
        )

    with sessions.begin() as session:
        session.delete(intent)
        session.delete(approval)
    claimed, claimed_approval = _intent(2, state="CLAIMED")
    with sessions.begin() as session:
        session.add_all((claimed, claimed_approval))
    with pytest.raises(OpportunityAuthorityError, match="ENTRY_RESERVATION_MISMATCH"):
        repository.load_entry_history(
            expected_account_fingerprint=ACCOUNT,
            event_key="event-1",
            trading_day=TRADING_DAY,
        )
    engine.dispose()


def test_prior_decision_returns_hash_bound_absence_and_exact_record() -> None:
    repository, sessions, engine = _repository()
    missing = repository.load_prior_opportunity_decision(
        expected_account_fingerprint=ACCOUNT,
        expected_opportunity_key="event-1",
        decision_boundary=BOUNDARY,
        as_of=NOW,
    )
    assert missing.outcome is None
    assert missing.opportunity_key == "event-1"
    assert missing.observed_at == NOW
    assert len(missing.source_hash) == 64

    normalized_input = {"opportunity_key": "event-1"}
    result_payload = {"outcome": OpportunityOutcome.NO_TRADE.value}
    input_hash = canonical_agent_hash(
        agent_input_material(
            account_role=AccountRole.DEVELOPMENT.value,
            account_fingerprint=ACCOUNT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input=normalized_input,
            thesis_version_id=None,
        )
    )
    result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash=input_hash,
            outcome=OpportunityOutcome.NO_TRADE.value,
            reason_code="CATALYST_DATA_MISSING",
            policy_hash=POLICY,
            thesis_version_id=None,
            result_payload=result_payload,
            authorization_id=None,
            intent_id=None,
            intent_digest=None,
            autonomy_authorized=False,
        )
    )
    snapshot_id = UUID(int=500)
    decision_id = UUID(int=501)
    with sessions.begin() as session:
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=snapshot_id,
                thesis_version_id=None,
                account_role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                decision_kind="OPPORTUNITY",
                decision_boundary=BOUNDARY,
                observed_at=BOUNDARY,
                normalized_payload=normalized_input,
                input_hash=input_hash,
                created_at=BOUNDARY,
            )
        )
        session.add(
            AgentDecisionRow(
                decision_id=decision_id,
                thesis_version_id=None,
                origin_tick_id=UUID(int=502),
                input_snapshot_id=snapshot_id,
                account_role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                decision_kind="OPPORTUNITY",
                outcome=OpportunityOutcome.NO_TRADE.value,
                reason_code="CATALYST_DATA_MISSING",
                policy_hash=POLICY,
                result_payload=result_payload,
                result_hash=result_hash,
                autonomy_authorized=False,
                decision_boundary=BOUNDARY,
                created_at=BOUNDARY,
            )
        )

    found = repository.load_prior_opportunity_decision(
        expected_account_fingerprint=ACCOUNT,
        expected_opportunity_key="event-1",
        decision_boundary=BOUNDARY,
        as_of=NOW,
    )
    assert found.outcome is OpportunityOutcome.NO_TRADE
    assert found.decision_id == decision_id
    assert found.source_hash != missing.source_hash
    engine.dispose()


def test_prior_decision_recomputes_and_rejects_tampered_input() -> None:
    repository, sessions, engine = _repository()
    input_hash = canonical_agent_hash(
        agent_input_material(
            account_role=AccountRole.DEVELOPMENT.value,
            account_fingerprint=ACCOUNT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input={"opportunity_key": "event-1"},
            thesis_version_id=None,
        )
    )
    snapshot_id = UUID(int=600)
    with sessions.begin() as session:
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=snapshot_id,
                thesis_version_id=None,
                account_role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                decision_kind="OPPORTUNITY",
                decision_boundary=BOUNDARY,
                observed_at=BOUNDARY,
                normalized_payload={"opportunity_key": "event-1", "substituted": True},
                input_hash=input_hash,
                created_at=BOUNDARY,
            )
        )
        session.add(
            AgentDecisionRow(
                decision_id=UUID(int=601),
                thesis_version_id=None,
                origin_tick_id=UUID(int=602),
                input_snapshot_id=snapshot_id,
                account_role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                decision_kind="OPPORTUNITY",
                outcome=OpportunityOutcome.NO_TRADE.value,
                reason_code="CATALYST_DATA_MISSING",
                policy_hash=POLICY,
                result_payload={},
                result_hash="f" * 64,
                autonomy_authorized=False,
                decision_boundary=BOUNDARY,
                created_at=BOUNDARY,
            )
        )
    with pytest.raises(OpportunityAuthorityError, match="PRIOR_DECISION_INPUT_HASH_MISMATCH"):
        repository.load_prior_opportunity_decision(
            expected_account_fingerprint=ACCOUNT,
            expected_opportunity_key="event-1",
            decision_boundary=BOUNDARY,
            as_of=NOW,
        )
    engine.dispose()


def test_prior_decision_rejects_opportunity_substitution() -> None:
    repository, sessions, engine = _repository()
    normalized_input = {"opportunity_key": "other-event"}
    input_hash = canonical_agent_hash(
        agent_input_material(
            account_role=AccountRole.DEVELOPMENT.value,
            account_fingerprint=ACCOUNT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input=normalized_input,
            thesis_version_id=None,
        )
    )
    result_payload = {"outcome": OpportunityOutcome.NO_TRADE.value}
    result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash=input_hash,
            outcome=OpportunityOutcome.NO_TRADE.value,
            reason_code="CATALYST_DATA_MISSING",
            policy_hash=POLICY,
            thesis_version_id=None,
            result_payload=result_payload,
            authorization_id=None,
            intent_id=None,
            intent_digest=None,
            autonomy_authorized=False,
        )
    )
    snapshot_id = UUID(int=650)
    with sessions.begin() as session:
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=snapshot_id,
                thesis_version_id=None,
                account_role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                decision_kind="OPPORTUNITY",
                decision_boundary=BOUNDARY,
                observed_at=BOUNDARY,
                normalized_payload=normalized_input,
                input_hash=input_hash,
                created_at=BOUNDARY,
            )
        )
        session.add(
            AgentDecisionRow(
                decision_id=UUID(int=651),
                thesis_version_id=None,
                origin_tick_id=UUID(int=652),
                input_snapshot_id=snapshot_id,
                account_role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                decision_kind="OPPORTUNITY",
                outcome=OpportunityOutcome.NO_TRADE.value,
                reason_code="CATALYST_DATA_MISSING",
                policy_hash=POLICY,
                result_payload=result_payload,
                result_hash=result_hash,
                autonomy_authorized=False,
                decision_boundary=BOUNDARY,
                created_at=BOUNDARY,
            )
        )

    with pytest.raises(OpportunityAuthorityError, match="PRIOR_DECISION_AUTHORITY_MISMATCH"):
        repository.load_prior_opportunity_decision(
            expected_account_fingerprint=ACCOUNT,
            expected_opportunity_key="event-1",
            decision_boundary=BOUNDARY,
            as_of=NOW,
        )
    engine.dispose()


def test_prior_approved_decision_revalidates_exact_intent_lineage() -> None:
    repository, sessions, engine = _repository()
    intent, approval = _intent(1)
    decision_id = UUID(int=680)
    thesis_version_id = approval.thesis_version_id
    approval.agent_decision_id = decision_id
    normalized_input = {"opportunity_key": "event-1"}
    result_payload = {"outcome": OpportunityOutcome.ENTRY_APPROVED.value}
    input_hash = canonical_agent_hash(
        agent_input_material(
            account_role=AccountRole.DEVELOPMENT.value,
            account_fingerprint=ACCOUNT,
            decision_kind="OPPORTUNITY",
            decision_boundary=BOUNDARY,
            observed_at=BOUNDARY,
            normalized_input=normalized_input,
            thesis_version_id=thesis_version_id,
        )
    )
    result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash=input_hash,
            outcome=OpportunityOutcome.ENTRY_APPROVED.value,
            reason_code="ENTRY_APPROVED",
            policy_hash=POLICY,
            thesis_version_id=thesis_version_id,
            result_payload=result_payload,
            authorization_id=approval.approval_id,
            intent_id=intent.intent_id,
            intent_digest=intent.intent_digest,
            autonomy_authorized=True,
        )
    )
    snapshot_id = UUID(int=681)
    with sessions.begin() as session:
        session.add_all(
            (
                AgentInputSnapshotRow(
                    snapshot_id=snapshot_id,
                    thesis_version_id=thesis_version_id,
                    account_role=AccountRole.DEVELOPMENT.value,
                    account_fingerprint=ACCOUNT,
                    decision_kind="OPPORTUNITY",
                    decision_boundary=BOUNDARY,
                    observed_at=BOUNDARY,
                    normalized_payload=normalized_input,
                    input_hash=input_hash,
                    created_at=BOUNDARY,
                ),
                AgentDecisionRow(
                    decision_id=decision_id,
                    thesis_version_id=thesis_version_id,
                    origin_tick_id=UUID(int=682),
                    input_snapshot_id=snapshot_id,
                    account_role=AccountRole.DEVELOPMENT.value,
                    account_fingerprint=ACCOUNT,
                    decision_kind="OPPORTUNITY",
                    outcome=OpportunityOutcome.ENTRY_APPROVED.value,
                    reason_code="ENTRY_APPROVED",
                    policy_hash=POLICY,
                    result_payload=result_payload,
                    result_hash=result_hash,
                    autonomy_authorized=True,
                    decision_boundary=BOUNDARY,
                    created_at=BOUNDARY,
                ),
                approval,
                intent,
            )
        )

    found = repository.load_prior_opportunity_decision(
        expected_account_fingerprint=ACCOUNT,
        expected_opportunity_key="event-1",
        decision_boundary=BOUNDARY,
        as_of=NOW,
    )
    assert found.outcome is OpportunityOutcome.ENTRY_APPROVED
    assert found.decision_id == decision_id
    assert intent.fingerprint == POSITION
    assert intent.fingerprint != ACCOUNT

    with sessions.begin() as session:
        row = session.get(EntryApprovalCertificateRow, approval.approval_id)
        assert row is not None
        row.book_fingerprint = "9" * 64
    with pytest.raises(OpportunityAuthorityError, match="PRIOR_DECISION_INTENT_INVALID"):
        repository.load_prior_opportunity_decision(
            expected_account_fingerprint=ACCOUNT,
            expected_opportunity_key="event-1",
            decision_boundary=BOUNDARY,
            as_of=NOW,
        )
    engine.dispose()


def test_latest_effective_greek_authority_is_explicit_and_hash_bound() -> None:
    repository, sessions, engine = _repository()
    with sessions.begin() as session:
        for version, offset, marker in ((1, 2, "1"), (2, 1, "2")):
            session.add(
                GreekAuthorityVersionRow(
                    authority_id=UUID(int=700 + version),
                    version=version,
                    effective_at=NOW - timedelta(days=offset),
                    timestamp_contract_hash=marker * 64,
                    units_contract_hash=f"{version + 2:x}" * 64,
                    authority_payload={"version": version},
                    authority_hash=f"{version + 4:x}" * 64,
                    created_at=NOW - timedelta(days=offset),
                )
            )

    authority = repository.load_latest_greek_unit_authority(effective_at=NOW)

    assert authority.version == 2
    assert authority.convention is GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1
    assert authority.evidence_hash == "4" * 64
    assert len(authority.authority_hash) == 64
    engine.dispose()


def test_greek_authority_rejects_nonmonotonic_versions() -> None:
    repository, sessions, engine = _repository()
    with sessions.begin() as session:
        session.add_all(
            (
                GreekAuthorityVersionRow(
                    authority_id=UUID(int=801),
                    version=1,
                    effective_at=NOW - timedelta(days=1),
                    timestamp_contract_hash="1" * 64,
                    units_contract_hash="2" * 64,
                    authority_payload={},
                    authority_hash="3" * 64,
                    created_at=NOW - timedelta(days=1),
                ),
                GreekAuthorityVersionRow(
                    authority_id=UUID(int=802),
                    version=2,
                    effective_at=NOW - timedelta(days=2),
                    timestamp_contract_hash="4" * 64,
                    units_contract_hash="5" * 64,
                    authority_payload={},
                    authority_hash="6" * 64,
                    created_at=NOW - timedelta(days=2),
                ),
            )
        )
    with pytest.raises(OpportunityAuthorityError, match="GREEK_AUTHORITY_SEQUENCE_INVALID"):
        repository.load_latest_greek_unit_authority(effective_at=NOW)
    engine.dispose()


def test_greek_authority_rejects_missing_version_lineage() -> None:
    repository, sessions, engine = _repository()
    with sessions.begin() as session:
        session.add(
            GreekAuthorityVersionRow(
                authority_id=UUID(int=850),
                version=2,
                effective_at=NOW - timedelta(days=1),
                timestamp_contract_hash="1" * 64,
                units_contract_hash="2" * 64,
                authority_payload={},
                authority_hash="3" * 64,
                created_at=NOW - timedelta(days=1),
            )
        )
    with pytest.raises(OpportunityAuthorityError, match="GREEK_AUTHORITY_SEQUENCE_INVALID"):
        repository.load_latest_greek_unit_authority(effective_at=NOW)
    engine.dispose()


def test_frozen_thesis_read_requires_exact_account_policy_and_identity() -> None:
    repository, sessions, engine = _repository()
    thesis_id = UUID(int=900)
    with sessions.begin() as session:
        session.add(
            ThesisVersionRow(
                thesis_version_id=thesis_id,
                thesis_id=UUID(int=901),
                account_role=AccountRole.DEVELOPMENT.value,
                version=1,
                origin_hash=THESIS,
                thesis_hash=THESIS,
                policy_hash=POLICY,
                underlying="NVDA",
                thesis_code="EVENT_1",
                frozen_at=BOUNDARY,
                target_at=NOW + timedelta(days=7),
                intended_exposure={"delta": "positive"},
                exposure_limits={"maximum_daily_theta": "10"},
                volatility_view="NEUTRAL",
                entry_atm_iv=Decimal("0.4"),
                approved_max_loss=Decimal("500"),
                portfolio_risk_cap=Decimal("1000"),
                invalidation_codes=["PRICE_CONFIRMATION_BROKEN"],
                thesis_payload={"thesis_hash": THESIS},
                created_at=BOUNDARY,
            )
        )

    loaded = repository.load_frozen_thesis(
        thesis_version_id=thesis_id,
        expected_account_fingerprint=ACCOUNT,
        expected_thesis_hash=THESIS,
        expected_policy_hash=POLICY,
        expected_underlying="NVDA",
        as_of=NOW,
    )
    assert loaded.thesis_version_id == thesis_id
    assert loaded.account_role is AccountRole.DEVELOPMENT

    with pytest.raises(OpportunityAuthorityError, match="THESIS_AUTHORITY_MISMATCH"):
        repository.load_frozen_thesis(
            thesis_version_id=thesis_id,
            expected_account_fingerprint=ACCOUNT,
            expected_thesis_hash=THESIS,
            expected_policy_hash="f" * 64,
            expected_underlying="NVDA",
            as_of=NOW,
        )

    with sessions.begin() as session:
        row = session.get(ThesisVersionRow, thesis_id)
        assert row is not None
        row.created_at = BOUNDARY - timedelta(seconds=1)
    with pytest.raises(OpportunityAuthorityError, match="THESIS_PAYLOAD_INVALID"):
        repository.load_frozen_thesis(
            thesis_version_id=thesis_id,
            expected_account_fingerprint=ACCOUNT,
            expected_thesis_hash=THESIS,
            expected_policy_hash=POLICY,
            expected_underlying="NVDA",
            as_of=NOW,
        )
    engine.dispose()


def test_authority_reads_do_not_mutate_persistence() -> None:
    repository, sessions, engine = _repository()
    before: tuple[int, int]
    after: tuple[int, int]
    with sessions() as session:
        before = (
            session.query(AccountRoleRow).count(),
            session.query(CompetitionEntryBudgetRow).count(),
        )
    repository.load_development_route(expected_account_fingerprint=ACCOUNT)
    repository.load_entry_history(
        expected_account_fingerprint=ACCOUNT,
        event_key="event-1",
        trading_day=TRADING_DAY,
    )
    repository.load_prior_opportunity_decision(
        expected_account_fingerprint=ACCOUNT,
        expected_opportunity_key="event-1",
        decision_boundary=BOUNDARY,
        as_of=NOW,
    )
    with sessions() as session:
        after = (
            session.query(AccountRoleRow).count(),
            session.query(CompetitionEntryBudgetRow).count(),
        )
    assert after == before
    engine.dispose()


@pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
def test_postgres_migration_chain_supports_consistent_empty_authority_reads() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"opportunity_authority_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(os.environ[POSTGRES_URL_ENV])

    @event.listens_for(engine, "connect")
    def set_search_path(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        apply_migrations(engine, discover_migrations(MIGRATIONS))
        with sessions.begin() as session:
            session.add(
                AccountRoleRow(
                    role=AccountRole.DEVELOPMENT.value,
                    account_fingerprint=ACCOUNT,
                    equity=Decimal("100000"),
                    autonomous_enabled=True,
                )
            )
            session.add(
                CompetitionEntryBudgetRow(
                    account_role=AccountRole.DEVELOPMENT.value,
                    entries_used=0,
                    gross_approved_risk=Decimal(0),
                    reserved_intent_id=None,
                    reserved_risk=Decimal(0),
                )
            )
            session.add(
                GreekAuthorityVersionRow(
                    authority_id=UUID(int=9000),
                    version=1,
                    effective_at=BOUNDARY,
                    timestamp_contract_hash="1" * 64,
                    units_contract_hash="2" * 64,
                    authority_payload={"convention": "ALPACA_GOPRICEOPTIONS_RAW_V1"},
                    authority_hash="3" * 64,
                    created_at=BOUNDARY,
                )
            )
            session.add(
                ThesisVersionRow(
                    thesis_version_id=UUID(int=9001),
                    thesis_id=UUID(int=9002),
                    account_role=AccountRole.DEVELOPMENT.value,
                    version=1,
                    origin_hash="4" * 64,
                    thesis_hash="4" * 64,
                    policy_hash=POLICY,
                    underlying="NVDA",
                    thesis_code="EVENT_1",
                    frozen_at=BOUNDARY,
                    target_at=NOW + timedelta(days=7),
                    intended_exposure={"delta": "positive"},
                    exposure_limits={"maximum_daily_theta": "10"},
                    volatility_view="NEUTRAL",
                    entry_atm_iv=Decimal("0.4"),
                    approved_max_loss=Decimal("500"),
                    portfolio_risk_cap=Decimal("1000"),
                    invalidation_codes=["PRICE_CONFIRMATION_BROKEN"],
                    thesis_payload={"thesis_code": "EVENT_1"},
                    created_at=BOUNDARY,
                )
            )
        with sessions() as session:
            thesis_hash = session.scalar(
                select(ThesisVersionRow.thesis_hash).where(
                    ThesisVersionRow.thesis_version_id == UUID(int=9001)
                )
            )
        assert isinstance(thesis_hash, str)
        repository = SQLAlchemyOpportunityAuthorityRepository(sessions)
        route = repository.load_development_route(expected_account_fingerprint=ACCOUNT)
        history = repository.load_entry_history(
            expected_account_fingerprint=ACCOUNT,
            event_key="event-1",
            trading_day=TRADING_DAY,
        )
        prior = repository.load_prior_opportunity_decision(
            expected_account_fingerprint=ACCOUNT,
            expected_opportunity_key="event-1",
            decision_boundary=BOUNDARY,
            as_of=NOW,
        )
        greek = repository.load_latest_greek_unit_authority(effective_at=NOW)
        thesis = repository.load_frozen_thesis(
            thesis_version_id=UUID(int=9001),
            expected_account_fingerprint=ACCOUNT,
            expected_thesis_hash=thesis_hash,
            expected_policy_hash=POLICY,
            expected_underlying="NVDA",
            as_of=NOW,
        )
        assert route.route is DevelopmentRoute.EMPTY
        assert history.entry_intent_count == 0
        assert prior.outcome is None
        assert prior.observed_at == NOW
        assert greek.evidence_hash == "2" * 64
        assert thesis.thesis_hash == thesis_hash
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
