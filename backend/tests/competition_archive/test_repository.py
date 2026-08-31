import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.competition_archive import (
    CompetitionArchiveIntegrityError,
    CompetitionRecordNotEligible,
    SQLAlchemyCompetitionArchiveRepository,
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
    AgentTickRow,
    Base,
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

BOUNDARY = datetime(2026, 8, 30, 15, tzinfo=UTC)
FINGERPRINT = "a" * 64


def repository() -> tuple[SQLAlchemyCompetitionArchiveRepository, sessionmaker]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role="SUBMISSION",
                account_fingerprint=FINGERPRINT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
        session.add(
            SubmissionBaselineRow(
                baseline_id=uuid4(),
                account_role="SUBMISSION",
                account_fingerprint=FINGERPRINT,
                equity=Decimal("100000"),
                captured_at=BOUNDARY - timedelta(days=2),
                positions_hash="1" * 64,
                orders_hash="2" * 64,
                activities_hash="3" * 64,
                contaminated=False,
            )
        )
    return SQLAlchemyCompetitionArchiveRepository(sessions), sessions


def file_repository(
    directory: Path,
) -> tuple[SQLAlchemyCompetitionArchiveRepository, sessionmaker]:
    engine = create_engine(
        f"sqlite+pysqlite:///{directory / 'archive.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role="SUBMISSION",
                account_fingerprint=FINGERPRINT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
        session.add(
            SubmissionBaselineRow(
                baseline_id=uuid4(),
                account_role="SUBMISSION",
                account_fingerprint=FINGERPRINT,
                equity=Decimal("100000"),
                captured_at=BOUNDARY - timedelta(days=2),
                positions_hash="1" * 64,
                orders_hash="2" * 64,
                activities_hash="3" * 64,
                contaminated=False,
            )
        )
    return SQLAlchemyCompetitionArchiveRepository(sessions), sessions


def add_no_trade(
    sessions: sessionmaker,
    *,
    boundary: datetime = BOUNDARY,
    role: str = "SUBMISSION",
    result_hash: str = "b" * 64,
) -> UUID:
    tick_id = uuid4()
    snapshot_id = uuid4()
    decision_id = uuid4()
    fingerprint = FINGERPRINT if role == "SUBMISSION" else "e" * 64
    normalized = {
        "private_candidate_material": "not public",
        "calibration_hash": "6" * 64,
        "machine_binding_hash": result_hash,
    }
    input_hash = canonical_agent_hash(
        agent_input_material(
            account_role=role,
            account_fingerprint=fingerprint,
            decision_kind="OPPORTUNITY",
            decision_boundary=boundary,
            observed_at=boundary + timedelta(seconds=3),
            normalized_input=normalized,
            thesis_version_id=None,
        )
    )
    result_payload = {"private_threshold": "not public"}
    authoritative_result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash=input_hash,
            outcome="NO_TRADE",
            reason_code="CALIBRATION_BINDING_NO_TRADE",
            policy_hash="d" * 64,
            thesis_version_id=None,
            result_payload=result_payload,
            authorization_id=None,
            intent_id=None,
            intent_digest=None,
            autonomy_authorized=False,
        )
    )
    with sessions.begin() as session:
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=snapshot_id,
                thesis_version_id=None,
                account_role=role,
                account_fingerprint=fingerprint,
                decision_kind="OPPORTUNITY",
                decision_boundary=boundary,
                observed_at=boundary + timedelta(seconds=3),
                normalized_payload=normalized,
                input_hash=input_hash,
                created_at=boundary + timedelta(seconds=3),
            )
        )
        session.add(
            AgentTickRow(
                tick_id=tick_id,
                account_role=role,
                account_fingerprint=fingerprint,
                tick_key=f"archive:{decision_id}",
                tick_boundary=boundary,
                actor="SCHEDULER",
                status="RESERVED",
                reservation_token=uuid4(),
                terminal_code=None,
                decision_id=None,
                execution_certificate_id=None,
                proof_hash=None,
                created_at=boundary,
                completed_at=None,
            )
        )
        session.flush()
        session.add(
            AgentDecisionRow(
                decision_id=decision_id,
                thesis_version_id=None,
                origin_tick_id=tick_id,
                input_snapshot_id=snapshot_id,
                account_role=role,
                account_fingerprint=fingerprint,
                decision_kind="OPPORTUNITY",
                outcome="NO_TRADE",
                reason_code="CALIBRATION_BINDING_NO_TRADE",
                policy_hash="d" * 64,
                result_payload=result_payload,
                result_hash=authoritative_result_hash,
                autonomy_authorized=False,
                decision_boundary=boundary,
                created_at=boundary + timedelta(seconds=3),
            )
        )
    return decision_id


def option_inventory(
    long_symbol: str, short_symbol: str, quantity: int = 1
) -> list[dict[str, object]]:
    return [
        {
            "kind": "OPTION",
            "symbol": long_symbol,
            "signed_quantity": str(quantity),
            "multiplier": 100,
        },
        {
            "kind": "OPTION",
            "symbol": short_symbol,
            "signed_quantity": str(-quantity),
            "multiplier": 100,
        },
    ]


def add_position(sessions: sessionmaker) -> dict[str, UUID]:
    ids = {
        name: uuid4()
        for name in ("thesis", "intent", "certificate", "position", "transition", "snapshot")
    }
    opened_at = BOUNDARY + timedelta(hours=1)
    with sessions.begin() as session:
        session.add(
            ThesisVersionRow(
                thesis_version_id=ids["thesis"],
                thesis_id=uuid4(),
                account_role="SUBMISSION",
                version=1,
                origin_hash="1" * 64,
                thesis_hash="2" * 64,
                policy_hash="3" * 64,
                underlying="SPY",
                thesis_code="PRIVATE_EVENT_SELECTOR",
                frozen_at=BOUNDARY,
                target_at=BOUNDARY + timedelta(days=2),
                intended_exposure={"delta": "45"},
                exposure_limits={"private_limit": "never public"},
                volatility_view="NEUTRAL",
                entry_atm_iv=Decimal("0.25"),
                approved_max_loss=Decimal("500"),
                portfolio_risk_cap=Decimal("1000"),
                invalidation_codes=["PRIVATE_INVALIDATION"],
                thesis_payload={"raw_evidence": "never public"},
                created_at=BOUNDARY,
            )
        )
        session.add(
            ExecutionIntentRow(
                intent_id=ids["intent"],
                account_role="SUBMISSION",
                intent_digest="4" * 64,
                action="ENTRY",
                policy_hash="3" * 64,
                event_key="private-event-key",
                trading_day=date(2026, 8, 30),
                entry_approval_id=uuid4(),
                assessment_certificate_id=None,
                fingerprint="5" * 64,
                envelope_hash="6" * 64,
                envelope_payload={"provider": "private"},
                legs=[
                    {
                        "symbol": "SPY260904C00400000",
                        "intent": "BUY_TO_OPEN",
                        "ratio": 1,
                    },
                    {
                        "symbol": "SPY260904C00405000",
                        "intent": "SELL_TO_OPEN",
                        "ratio": 1,
                    },
                ],
                quantity=1,
                minimum_limit=Decimal("1"),
                maximum_limit=Decimal("1.2"),
                approved_max_loss=Decimal("500"),
                state="TERMINAL",
                claimed_by=None,
                claimed_at=None,
                claim_token=None,
                claim_generation=0,
                execution_epoch=0,
                heartbeat_at=None,
                lease_expires_at=None,
                first_fill_consumed=True,
            )
        )
        session.add(
            ExecutionCertificateRow(
                certificate_id=ids["certificate"],
                execution_intent_id=ids["intent"],
                entry_approval_id=uuid4(),
                assessment_certificate_id=None,
                execution_status="FILLED",
                attempt_ids=[str(uuid4())],
                actual_exposure={"delta": "44", "theta_per_day": "-3.1"},
                reconciliation_checks=["BOOK_MATCHED"],
                created_at=opened_at,
                reconciliation_id=None,
                reconciliation_hash=None,
                last_observation_hash=None,
            )
        )
        session.add(
            ManagedLifecyclePositionRow(
                managed_position_id=ids["position"],
                account_role="SUBMISSION",
                account_fingerprint=FINGERPRINT,
                entry_execution_certificate_id=ids["certificate"],
                entry_intent_id=ids["intent"],
                entry_approval_id=uuid4(),
                thesis_version_id=ids["thesis"],
                entry_reconciliation_id=uuid4(),
                current_reconciliation_state_id=uuid4(),
                current_snapshot_id=ids["snapshot"],
                active_position_fingerprint="7" * 64,
                activated_at=opened_at,
                closed_at=None,
            )
        )
        session.add(
            ManagedPositionTransitionRow(
                transition_id=ids["transition"],
                managed_position_id=ids["position"],
                predecessor_transition_id=None,
                transition_sequence=0,
                action="ENTRY",
                execution_intent_id=ids["intent"],
                execution_certificate_id=ids["certificate"],
                post_reconciliation_id=uuid4(),
                fill_activity_manifest=[],
                fill_activity_manifest_hash="8" * 64,
                cashflow_contribution=Decimal("-110"),
                resulting_position_fingerprint="7" * 64,
                occurred_at=opened_at,
                market_session_id=uuid4(),
                transition_hash="9" * 64,
            )
        )
        session.add(
            ManagedPositionSnapshotRow(
                snapshot_id=ids["snapshot"],
                managed_position_id=ids["position"],
                predecessor_snapshot_id=None,
                transition_id=ids["transition"],
                reconciliation_id=uuid4(),
                reconciliation_state_id=uuid4(),
                normalized_inventory=option_inventory("SPY260904C00400000", "SPY260904C00405000"),
                inventory_hash="a" * 64,
                activity_manifest=[],
                activity_manifest_hash="b" * 64,
                cumulative_cashflow=Decimal("-110"),
                rolls_on_trading_day=0,
                market_session_id=uuid4(),
                position_fingerprint="7" * 64,
                accepted_at=opened_at + timedelta(minutes=1),
                snapshot_hash="c" * 64,
            )
        )
    return ids


def add_roll_or_close(
    sessions: sessionmaker,
    ids: dict[str, UUID],
    *,
    action: str,
    sequence: int,
) -> None:
    intent_id = uuid4()
    certificate_id = uuid4()
    transition_id = uuid4()
    snapshot_id = uuid4()
    assessment_id = uuid4()
    occurred_at = BOUNDARY + timedelta(hours=1, minutes=10 * sequence)
    if sequence == 1:
        prior_long = "SPY260904C00400000"
        prior_short = "SPY260904C00405000"
        predecessor_transition = ids["transition"]
        predecessor_snapshot = ids["snapshot"]
    else:
        prior_long = "SPY260911C00410000"
        prior_short = "SPY260911C00415000"
        predecessor_transition = ids["latest_transition"]
        predecessor_snapshot = ids["latest_snapshot"]
    closing_legs = [
        {"symbol": prior_long, "intent": "SELL_TO_CLOSE", "ratio": 1},
        {"symbol": prior_short, "intent": "BUY_TO_CLOSE", "ratio": 1},
    ]
    opening_legs = [
        {"symbol": "SPY260911C00410000", "intent": "BUY_TO_OPEN", "ratio": 1},
        {"symbol": "SPY260911C00415000", "intent": "SELL_TO_OPEN", "ratio": 1},
    ]
    legs = closing_legs + (opening_legs if action == "ROLL" else [])
    inventory = (
        option_inventory("SPY260911C00410000", "SPY260911C00415000") if action == "ROLL" else []
    )
    fingerprint = ("d" if action == "ROLL" else "f") * 64
    with sessions.begin() as session:
        session.add(
            ExecutionIntentRow(
                intent_id=intent_id,
                account_role="SUBMISSION",
                intent_digest=("d" if action == "ROLL" else "e") * 64,
                action=action,
                policy_hash="3" * 64,
                event_key="private-event-key",
                trading_day=date(2026, 8, 30),
                entry_approval_id=None,
                assessment_certificate_id=assessment_id,
                fingerprint=("7" if sequence == 1 else "d") * 64,
                envelope_hash=("e" if action == "ROLL" else "f") * 64,
                envelope_payload={"provider": "private"},
                legs=legs,
                quantity=1,
                minimum_limit=Decimal("0.1"),
                maximum_limit=Decimal("0.3"),
                approved_max_loss=Decimal("500"),
                market_session_id=uuid4() if action == "ROLL" else None,
                quoted_relative_spread=(Decimal("0.05") if action == "ROLL" else None),
                maximum_relative_spread=(Decimal("0.25") if action == "ROLL" else None),
                incremental_debit=(Decimal("100") if action == "ROLL" else None),
                maximum_incremental_debit=(Decimal("500") if action == "ROLL" else None),
                state="TERMINAL",
                claimed_by=None,
                claimed_at=None,
                claim_token=None,
                claim_generation=0,
                execution_epoch=0,
                heartbeat_at=None,
                lease_expires_at=None,
                first_fill_consumed=True,
            )
        )
        session.add(
            ExecutionCertificateRow(
                certificate_id=certificate_id,
                execution_intent_id=intent_id,
                entry_approval_id=None,
                assessment_certificate_id=assessment_id,
                execution_status="FILLED",
                attempt_ids=[str(uuid4())],
                actual_exposure=(
                    {"delta": "35", "theta_per_day": "-2"} if action == "ROLL" else None
                ),
                reconciliation_checks=["BOOK_MATCHED"],
                created_at=occurred_at,
                reconciliation_id=None,
                reconciliation_hash=None,
                last_observation_hash=None,
            )
        )
        session.add(
            ManagedPositionTransitionRow(
                transition_id=transition_id,
                managed_position_id=ids["position"],
                predecessor_transition_id=predecessor_transition,
                transition_sequence=sequence,
                action=action,
                execution_intent_id=intent_id,
                execution_certificate_id=certificate_id,
                post_reconciliation_id=uuid4(),
                fill_activity_manifest=[],
                fill_activity_manifest_hash=("4" if action == "ROLL" else "5") * 64,
                cashflow_contribution=Decimal("20"),
                resulting_position_fingerprint=fingerprint,
                occurred_at=occurred_at,
                market_session_id=uuid4(),
                transition_hash=("a" if action == "ROLL" else "b") * 64,
            )
        )
        session.add(
            ManagedPositionSnapshotRow(
                snapshot_id=snapshot_id,
                managed_position_id=ids["position"],
                predecessor_snapshot_id=predecessor_snapshot,
                transition_id=transition_id,
                reconciliation_id=uuid4(),
                reconciliation_state_id=uuid4(),
                normalized_inventory=inventory,
                inventory_hash=("6" if action == "ROLL" else "7") * 64,
                activity_manifest=[],
                activity_manifest_hash=("8" if action == "ROLL" else "9") * 64,
                cumulative_cashflow=Decimal("-90" if action == "ROLL" else "-70"),
                rolls_on_trading_day=1,
                market_session_id=uuid4(),
                position_fingerprint=fingerprint,
                accepted_at=occurred_at + timedelta(minutes=1),
                snapshot_hash=("e" if action == "ROLL" else "f") * 64,
            )
        )
        position = session.get(ManagedLifecyclePositionRow, ids["position"])
        assert position is not None
        position.current_snapshot_id = snapshot_id
        position.current_reconciliation_state_id = uuid4()
        position.active_position_fingerprint = fingerprint
        position.closed_at = occurred_at if action == "CLOSE" else None
    ids["latest_transition"] = transition_id
    ids["latest_snapshot"] = snapshot_id


def add_hold_assessment(sessions: sessionmaker, ids: dict[str, UUID]) -> None:
    boundary = BOUNDARY + timedelta(hours=1, minutes=5)
    tick_id, input_id, decision_id, manifest_id = (uuid4() for _ in range(4))
    normalized = {"private_observation": "never public"}
    input_hash = canonical_agent_hash(
        agent_input_material(
            account_role="SUBMISSION",
            account_fingerprint=FINGERPRINT,
            decision_kind="ASSESSMENT",
            decision_boundary=boundary,
            observed_at=boundary,
            normalized_input=normalized,
            thesis_version_id=ids["thesis"],
        )
    )
    result_payload = {"private_rationale": "never public"}
    result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash=input_hash,
            outcome="HOLD_CERTIFIED",
            reason_code="HOLD_CERTIFIED",
            policy_hash="3" * 64,
            thesis_version_id=ids["thesis"],
            result_payload=result_payload,
            authorization_id=None,
            intent_id=None,
            intent_digest=None,
            autonomy_authorized=False,
        )
    )
    with sessions.begin() as session:
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=input_id,
                thesis_version_id=ids["thesis"],
                account_role="SUBMISSION",
                account_fingerprint=FINGERPRINT,
                decision_kind="ASSESSMENT",
                decision_boundary=boundary,
                observed_at=boundary,
                normalized_payload=normalized,
                input_hash=input_hash,
                created_at=boundary,
            )
        )
        session.add(
            AgentTickRow(
                tick_id=tick_id,
                account_role="SUBMISSION",
                account_fingerprint=FINGERPRINT,
                tick_key=f"assessment:{decision_id}",
                tick_boundary=boundary,
                actor="OWNER",
                status="RESERVED",
                reservation_token=uuid4(),
                terminal_code=None,
                decision_id=None,
                execution_certificate_id=None,
                proof_hash=None,
                created_at=boundary,
                completed_at=None,
            )
        )
        session.flush()
        session.add(
            AgentDecisionRow(
                decision_id=decision_id,
                thesis_version_id=ids["thesis"],
                origin_tick_id=tick_id,
                input_snapshot_id=input_id,
                account_role="SUBMISSION",
                account_fingerprint=FINGERPRINT,
                decision_kind="ASSESSMENT",
                outcome="HOLD_CERTIFIED",
                reason_code="HOLD_CERTIFIED",
                policy_hash="3" * 64,
                result_payload=result_payload,
                result_hash=result_hash,
                autonomy_authorized=False,
                decision_boundary=boundary,
                created_at=boundary,
            )
        )
        session.add(
            LifecycleObservationManifestRow(
                manifest_id=manifest_id,
                manifest_hash="0" * 64,
                agent_input_snapshot_id=input_id,
                account_observation_id=None,
                managed_position_id=ids["position"],
                managed_snapshot_id=ids["snapshot"],
                reconciliation_id=None,
                greek_authority_id=uuid4(),
                sweep_hash="1" * 64,
                account_manifest={},
                activity_manifest=[],
                option_manifest=[],
                atm_iv_manifest={},
                underlying_manifest={},
                boundary_manifest={},
                research_manifest=[],
                source_authority_manifest=None,
                observed_at=boundary,
                created_at=boundary,
            )
        )
        session.add(
            LifecycleObservationBindingRow(
                binding_id=uuid4(),
                manifest_id=manifest_id,
                agent_input_snapshot_id=input_id,
                created_at=boundary,
            )
        )


def test_no_trade_publication_is_sanitized_canonical_and_idempotent() -> None:
    repo, sessions = repository()
    decision_id = add_no_trade(sessions)

    first = repo.publish_no_trade(decision_id)
    repeated = repo.publish_no_trade(decision_id)

    assert repeated == first
    assert first.payload["status"] == "NO_TRADE"
    assert first.payload["reason_category"] == "STRATEGY_NOT_READY"
    assert first.payload_text == json.dumps(first.payload, sort_keys=True, separators=(",", ":"))
    text = first.payload_text
    for private_value in (
        str(decision_id),
        FINGERPRINT,
        "private_candidate_material",
        "private_threshold",
        "source_authority_hash",
    ):
        assert private_value not in text
    assert repo.records() == (first,)


def test_archive_forms_a_verified_append_only_hash_chain() -> None:
    repo, sessions = repository()
    first_id = add_no_trade(sessions)
    first = repo.publish_no_trade(first_id)
    second_id = add_no_trade(
        sessions,
        boundary=BOUNDARY + timedelta(minutes=1),
        result_hash="e" * 64,
    )
    second = repo.publish_no_trade(second_id)

    assert second.predecessor_hash == first.publication_hash
    assert repo.records() == (first, second)

    with sessions.begin() as session:
        row = session.scalar(
            select(CompetitionRecordPublicationRow).where(
                CompetitionRecordPublicationRow.public_record_id == first.public_record_id
            )
        )
        assert row is not None
        row.payload_text += " "
    with pytest.raises(CompetitionArchiveIntegrityError, match="canonical"):
        repo.records()


def test_selector_free_publisher_discovers_eligible_submission_authority() -> None:
    repo, sessions = repository()
    no_trade_id = add_no_trade(sessions)
    position_ids = add_position(sessions)

    records = repo.publish_eligible()

    assert tuple(record.kind for record in records) == ("NO_TRADE", "POSITION")
    assert repo.publish_no_trade(no_trade_id) == records[0]
    assert repo.publish_position(position_ids["position"]) == records[1]
    assert repo.publish_eligible() == records


def test_selector_free_publisher_ignores_development_authority() -> None:
    repo, sessions = repository()
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role="DEVELOPMENT",
                account_fingerprint="e" * 64,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    add_no_trade(sessions, role="DEVELOPMENT")

    assert repo.publish_eligible() == ()


def test_selector_free_publisher_rejects_a_selected_forged_result_hash() -> None:
    repo, sessions = repository()
    forged_id = add_no_trade(sessions)
    with sessions.begin() as session:
        forged = session.get(AgentDecisionRow, forged_id)
        assert forged is not None
        forged.result_hash = "f" * 64

    with pytest.raises(CompetitionRecordNotEligible, match="hash authority"):
        repo.publish_eligible()


@pytest.mark.parametrize("defect", ("missing", "contaminated", "mismatched"))
def test_selector_free_publisher_rejects_an_invalid_selected_baseline(defect: str) -> None:
    repo, sessions = repository()
    add_no_trade(sessions)
    with sessions.begin() as session:
        baseline = session.scalar(select(SubmissionBaselineRow))
        assert baseline is not None
        if defect == "missing":
            session.delete(baseline)
        elif defect == "contaminated":
            baseline.contaminated = True
        else:
            baseline.account_fingerprint = "f" * 64

    with pytest.raises(CompetitionRecordNotEligible, match="baseline"):
        repo.publish_eligible()


def test_selector_free_publisher_rejects_an_incomplete_selected_position() -> None:
    repo, sessions = repository()
    position_ids = add_position(sessions)
    with sessions.begin() as session:
        snapshot = session.get(ManagedPositionSnapshotRow, position_ids["snapshot"])
        assert snapshot is not None
        session.delete(snapshot)

    with pytest.raises(CompetitionRecordNotEligible, match="lifecycle authority"):
        repo.publish_eligible()


def test_no_trade_rejects_non_submission_authority() -> None:
    repo, sessions = repository()
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role="DEVELOPMENT",
                account_fingerprint="e" * 64,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    decision_id = add_no_trade(sessions, role="DEVELOPMENT")

    with pytest.raises(CompetitionRecordNotEligible, match="not a publishable"):
        repo.publish_no_trade(decision_id)


def test_position_projection_uses_sanitized_lifecycle_authority() -> None:
    repo, sessions = repository()
    thesis_id = uuid4()
    intent_id = uuid4()
    certificate_id = uuid4()
    position_id = uuid4()
    transition_id = uuid4()
    snapshot_id = uuid4()
    activated_at = BOUNDARY + timedelta(hours=1)
    with sessions.begin() as session:
        session.add(
            ThesisVersionRow(
                thesis_version_id=thesis_id,
                thesis_id=uuid4(),
                account_role="SUBMISSION",
                version=1,
                origin_hash="1" * 64,
                thesis_hash="2" * 64,
                policy_hash="3" * 64,
                underlying="SPY",
                thesis_code="PRIVATE_EVENT_SELECTOR",
                frozen_at=BOUNDARY,
                target_at=BOUNDARY + timedelta(days=2),
                intended_exposure={
                    "delta": "45",
                    "gamma": "1.2",
                    "theta_per_day": "-3",
                    "vega_per_iv_point": "4",
                },
                exposure_limits={"private_limit": "never public"},
                volatility_view="NEUTRAL",
                entry_atm_iv=Decimal("0.25"),
                approved_max_loss=Decimal("500"),
                portfolio_risk_cap=Decimal("1000"),
                invalidation_codes=["PRIVATE_INVALIDATION"],
                thesis_payload={"raw_evidence": "never public"},
                created_at=BOUNDARY,
            )
        )
        session.add(
            ExecutionIntentRow(
                intent_id=intent_id,
                account_role="SUBMISSION",
                intent_digest="4" * 64,
                action="ENTRY",
                policy_hash="3" * 64,
                event_key="private-event-key",
                trading_day=date(2026, 8, 30),
                entry_approval_id=uuid4(),
                assessment_certificate_id=None,
                fingerprint="5" * 64,
                envelope_hash="6" * 64,
                envelope_payload={"provider": "private"},
                legs=[
                    {
                        "symbol": "SPY260904C00400000",
                        "intent": "BUY_TO_OPEN",
                        "ratio": 1,
                    },
                    {
                        "symbol": "SPY260904C00405000",
                        "intent": "SELL_TO_OPEN",
                        "ratio": 1,
                    },
                ],
                quantity=1,
                minimum_limit=Decimal("1.00"),
                maximum_limit=Decimal("1.20"),
                approved_max_loss=Decimal("500"),
                state="TERMINAL",
                claimed_by=None,
                claimed_at=None,
                claim_token=None,
                claim_generation=0,
                execution_epoch=0,
                heartbeat_at=None,
                lease_expires_at=None,
                first_fill_consumed=True,
            )
        )
        session.add(
            ExecutionCertificateRow(
                certificate_id=certificate_id,
                execution_intent_id=intent_id,
                entry_approval_id=uuid4(),
                assessment_certificate_id=None,
                execution_status="FILLED",
                attempt_ids=[str(uuid4())],
                actual_exposure={"delta": "44", "theta_per_day": "-3.1"},
                reconciliation_checks=["BOOK_MATCHED"],
                created_at=activated_at,
                reconciliation_id=None,
                reconciliation_hash=None,
                last_observation_hash=None,
            )
        )
        session.add(
            ManagedLifecyclePositionRow(
                managed_position_id=position_id,
                account_role="SUBMISSION",
                account_fingerprint=FINGERPRINT,
                entry_execution_certificate_id=certificate_id,
                entry_intent_id=intent_id,
                entry_approval_id=uuid4(),
                thesis_version_id=thesis_id,
                entry_reconciliation_id=uuid4(),
                current_reconciliation_state_id=uuid4(),
                current_snapshot_id=snapshot_id,
                active_position_fingerprint="7" * 64,
                activated_at=activated_at,
                closed_at=None,
            )
        )
        session.add(
            ManagedPositionTransitionRow(
                transition_id=transition_id,
                managed_position_id=position_id,
                predecessor_transition_id=None,
                transition_sequence=0,
                action="ENTRY",
                execution_intent_id=intent_id,
                execution_certificate_id=certificate_id,
                post_reconciliation_id=uuid4(),
                fill_activity_manifest=[{"provider_activity_id": "private"}],
                fill_activity_manifest_hash="8" * 64,
                cashflow_contribution=Decimal("-110"),
                resulting_position_fingerprint="7" * 64,
                occurred_at=activated_at,
                market_session_id=uuid4(),
                transition_hash="9" * 64,
            )
        )
        session.add(
            ManagedPositionSnapshotRow(
                snapshot_id=snapshot_id,
                managed_position_id=position_id,
                predecessor_snapshot_id=None,
                transition_id=transition_id,
                reconciliation_id=uuid4(),
                reconciliation_state_id=uuid4(),
                normalized_inventory=[
                    {
                        "kind": "OPTION",
                        "symbol": "SPY260904C00400000",
                        "signed_quantity": "1",
                        "multiplier": 100,
                    },
                    {
                        "kind": "OPTION",
                        "symbol": "SPY260904C00405000",
                        "signed_quantity": "-1",
                        "multiplier": 100,
                    },
                ],
                inventory_hash="a" * 64,
                activity_manifest=[{"provider_activity_id": "private"}],
                activity_manifest_hash="b" * 64,
                cumulative_cashflow=Decimal("-110"),
                rolls_on_trading_day=0,
                market_session_id=uuid4(),
                position_fingerprint="7" * 64,
                accepted_at=activated_at + timedelta(minutes=1),
                snapshot_hash="c" * 64,
            )
        )

    record = repo.publish_position(position_id)
    repeated = repo.publish_position(position_id)

    assert repeated == record
    assert record.payload["state"] == "OPEN"
    assert record.payload["underlying"] == "SPY"
    assert record.payload["current_spread"] == {
        "structure": "VERTICAL",
        "underlying": "SPY",
        "option_type": "CALL",
        "expiration": "2026-09-04",
        "long_strike": "400",
        "short_strike": "405",
        "quantity": 1,
    }
    assert record.payload["thesis"]["direction"] == "BULLISH"
    assert record.payload["events"] == [
        {
            "event_kind": "EXECUTION",
            "action": "ENTRY",
            "occurred_at": "2026-08-30T16:00:00Z",
            "reason_category": "POSITION_OPENED",
            "cashflow_usd": "-110.000000",
            "execution_status": "FILLED",
            "resulting_state": "OPEN",
            "spread_after": record.payload["current_spread"],
        }
    ]
    text = record.payload_text
    for private_value in (
        str(position_id),
        "SPY260904C00400000",
        "private-event-key",
        "PRIVATE_EVENT_SELECTOR",
        "private_limit",
        "PRIVATE_INVALIDATION",
        "provider_activity_id",
        "account_fingerprint",
    ):
        assert private_value not in text


@pytest.mark.parametrize("defect", ("missing", "contaminated", "substituted"))
def test_publication_rejects_a_missing_or_untrusted_submission_baseline(defect: str) -> None:
    repo, sessions = repository()
    decision_id = add_no_trade(sessions)
    with sessions.begin() as session:
        baseline = session.scalar(select(SubmissionBaselineRow))
        assert baseline is not None
        if defect == "missing":
            session.delete(baseline)
        elif defect == "contaminated":
            baseline.contaminated = True
        else:
            baseline.account_fingerprint = "f" * 64

    with pytest.raises(CompetitionRecordNotEligible, match="baseline"):
        repo.publish_no_trade(decision_id)


def test_publication_recomputes_agent_hashes_from_source_rows() -> None:
    repo, sessions = repository()
    decision_id = add_no_trade(sessions)
    with sessions.begin() as session:
        decision = session.get(AgentDecisionRow, decision_id)
        assert decision is not None
        snapshot = session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
        assert snapshot is not None
        snapshot.normalized_payload = {"forged": True}

    with pytest.raises(CompetitionRecordNotEligible, match="hash authority"):
        repo.publish_no_trade(decision_id)


@pytest.mark.parametrize("tamper", ("key_whitespace", "published_at"))
def test_reader_rejects_forged_schema_and_exact_timestamp_text(tamper: str) -> None:
    repo, sessions = repository()
    record = repo.publish_no_trade(add_no_trade(sessions))
    with sessions.begin() as session:
        row = session.scalar(
            select(CompetitionRecordPublicationRow).where(
                CompetitionRecordPublicationRow.public_record_id == record.public_record_id
            )
        )
        assert row is not None
        payload = json.loads(row.payload_text)
        if tamper == "key_whitespace":
            payload["status "] = payload.pop("status")
        else:
            payload["published_at"] = "2026-08-30T15:00:00.000000Z"
        row.payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    with pytest.raises(CompetitionArchiveIntegrityError):
        repo.records()


def test_reader_revalidates_the_baseline_after_restart() -> None:
    repo, sessions = repository()
    repo.publish_no_trade(add_no_trade(sessions))
    restarted = SQLAlchemyCompetitionArchiveRepository(sessions)
    assert len(restarted.records()) == 1
    with sessions.begin() as session:
        baseline = session.scalar(select(SubmissionBaselineRow))
        assert baseline is not None
        baseline.contaminated = True
    with pytest.raises(CompetitionArchiveIntegrityError, match="source authority"):
        restarted.records()


def test_concurrent_publication_returns_one_immutable_record(tmp_path: Path) -> None:
    repo, sessions = file_repository(tmp_path)
    decision_id = add_no_trade(sessions)

    with ThreadPoolExecutor(max_workers=2) as executor:
        records = tuple(executor.map(lambda _item: repo.publish_no_trade(decision_id), range(2)))

    assert records[0] == records[1]
    assert repo.records() == (records[0],)


def test_position_rejects_a_non_vertical_or_mismatched_quantity() -> None:
    repo, sessions = repository()
    ids = add_position(sessions)
    with sessions.begin() as session:
        intent = session.get(ExecutionIntentRow, ids["intent"])
        assert intent is not None
        intent.legs = [
            {"symbol": "SPY260904C00400000", "intent": "BUY_TO_OPEN", "ratio": 1},
            {"symbol": "SPY260904C00400000", "intent": "SELL_TO_OPEN", "ratio": 1},
        ]

    with pytest.raises(CompetitionRecordNotEligible, match="vertical"):
        repo.publish_position(ids["position"])


def test_position_rejects_adjusted_contract_with_stable_reason() -> None:
    repo, sessions = repository()
    ids = add_position(sessions)
    with sessions.begin() as session:
        intent = session.get(ExecutionIntentRow, ids["intent"])
        snapshot = session.get(ManagedPositionSnapshotRow, ids["snapshot"])
        assert intent is not None
        assert snapshot is not None
        intent.legs = [
            {
                "symbol": "SPY1260904C00400000",
                "intent": "BUY_TO_OPEN",
                "ratio": 1,
            },
            {
                "symbol": "SPY1260904C00405000",
                "intent": "SELL_TO_OPEN",
                "ratio": 1,
            },
        ]
        snapshot.normalized_inventory = option_inventory(
            "SPY1260904C00400000",
            "SPY1260904C00405000",
        )

    with pytest.raises(CompetitionRecordNotEligible) as raised:
        repo.publish_position(ids["position"])

    assert str(raised.value) == "NON_STANDARD_CONTRACT_UNSUPPORTED"


def test_assessment_history_uses_reviewed_categories_without_raw_reasons() -> None:
    repo, sessions = repository()
    ids = add_position(sessions)
    add_hold_assessment(sessions, ids)

    record = repo.publish_position(ids["position"])

    assessment = record.payload["events"][1]
    assert assessment == {
        "event_kind": "ASSESSMENT",
        "action": "HOLD",
        "occurred_at": "2026-08-30T16:05:00Z",
        "reason_category": "POSITION_REVIEWED",
    }
    assert "private_rationale" not in record.payload_text
    assert "HOLD_CERTIFIED" not in record.payload_text


def test_roll_and_close_publish_evolving_versions_under_one_public_id() -> None:
    repo, sessions = repository()
    ids = add_position(sessions)
    opened = repo.publish_position(ids["position"])

    add_roll_or_close(sessions, ids, action="ROLL", sequence=1)
    rolled = repo.publish_position(ids["position"])
    assert rolled.public_record_id == opened.public_record_id
    assert rolled.publication_hash != opened.publication_hash
    assert rolled.payload["current_spread"]["expiration"] == "2026-09-11"
    assert rolled.payload["current_spread"]["long_strike"] == "410"
    assert rolled.payload["events"][-1]["action"] == "ROLL"
    assert rolled.payload["current_exposure"]["delta"] == "35"

    add_roll_or_close(sessions, ids, action="CLOSE", sequence=2)
    closed = repo.publish_position(ids["position"])
    assert closed.public_record_id == opened.public_record_id
    assert closed.payload["state"] == "CLOSED"
    assert closed.payload["current_spread"] is None
    assert closed.payload["current_exposure"] is None
    assert closed.payload["events"][-1]["action"] == "CLOSE"
    assert closed.payload["as_of"] >= closed.payload["events"][-1]["occurred_at"]
    assert [item.public_record_id for item in repo.records()[-3:]] == [
        opened.public_record_id,
        opened.public_record_id,
        opened.public_record_id,
    ]
