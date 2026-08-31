from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import AccountRole
from backend.app.lifecycle.repository import (
    LifecyclePersistenceError,
    LifecycleResearchSource,
    SQLAlchemyLifecycleRepository,
)
from backend.app.persistence.sqlalchemy_models import (
    AgentInputSnapshotRow,
    Base,
    GreekAuthorityVersionRow,
    LifecycleAccountObservationRow,
    LifecycleLaunchAuthorityRow,
    LifecycleObservationManifestRow,
    LifecycleSourceObservationRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
    ThesisVersionRow,
)
from backend.app.services import ObservedPaperAccountAuthority
from backend.tests.runtime_composition.test_development_acquisition import (
    FINGERPRINT,
    NOW,
    classification,
    cluster,
    context,
    observation,
)


def repository(account_role: AccountRole = AccountRole.DEVELOPMENT):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    retained = context(account_role=account_role)
    with sessions.begin() as session:
        session.add(
            ManagedLifecyclePositionRow(
                managed_position_id=retained.managed_position_id,
                account_role=account_role.value,
                account_fingerprint=FINGERPRINT,
                entry_execution_certificate_id=UUID(int=801),
                entry_intent_id=UUID(int=802),
                entry_approval_id=UUID(int=803),
                thesis_version_id=retained.thesis_version_id,
                entry_reconciliation_id=UUID(int=804),
                current_reconciliation_state_id=UUID(int=805),
                current_snapshot_id=retained.current_snapshot_id,
                active_position_fingerprint=retained.position_fingerprint,
                activated_at=datetime(2026, 8, 28, tzinfo=UTC),
                closed_at=None,
            )
        )
        session.add(
            ManagedPositionTransitionRow(
                transition_id=UUID(int=806),
                managed_position_id=retained.managed_position_id,
                predecessor_transition_id=None,
                transition_sequence=0,
                action="ENTRY",
                execution_intent_id=UUID(int=802),
                execution_certificate_id=UUID(int=801),
                post_reconciliation_id=UUID(int=804),
                fill_activity_manifest=[],
                fill_activity_manifest_hash="1" * 64,
                cashflow_contribution=retained.lifecycle_transitions[0].cashflow,
                resulting_position_fingerprint=retained.position_fingerprint,
                occurred_at=retained.lifecycle_origin_at,
                market_session_id=retained.lifecycle_transitions[0].market_session_id,
                transition_hash="2" * 64,
            )
        )
        session.add(
            ManagedPositionSnapshotRow(
                snapshot_id=retained.current_snapshot_id,
                managed_position_id=retained.managed_position_id,
                predecessor_snapshot_id=None,
                transition_id=UUID(int=806),
                reconciliation_id=UUID(int=804),
                reconciliation_state_id=UUID(int=805),
                normalized_inventory=[
                    {
                        "kind": "OPTION",
                        "symbol": item.symbol,
                        "signed_quantity": str(item.signed_quantity),
                        "multiplier": item.multiplier,
                    }
                    for item in retained.expected_positions
                ],
                inventory_hash=retained.position_fingerprint,
                activity_manifest=[],
                activity_manifest_hash="3" * 64,
                cumulative_cashflow=retained.lifecycle_transitions[0].cashflow,
                rolls_on_trading_day=0,
                market_session_id=retained.lifecycle_transitions[0].market_session_id,
                position_fingerprint=retained.position_fingerprint,
                accepted_at=retained.lifecycle_origin_at,
                snapshot_hash="4" * 64,
            )
        )
        session.add(
            ThesisVersionRow(
                thesis_version_id=retained.thesis_version_id,
                thesis_id=UUID(int=807),
                account_role=account_role.value,
                version=1,
                origin_hash=retained.thesis.thesis_hash,
                thesis_hash=retained.thesis.thesis_hash,
                policy_hash=retained.policy_hash,
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
                invalidation_codes=[],
                thesis_payload=retained.thesis.model_dump(mode="json"),
                created_at=retained.thesis_frozen_at,
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
                authority_hash="5" * 64,
                created_at=retained.greek_authority.effective_at,
            )
        )
        session.add(
            LifecycleLaunchAuthorityRow(
                managed_position_id=retained.managed_position_id,
                beta60=retained.launch_authority.beta60,
                benchmark_symbol=retained.launch_authority.benchmark_symbol,
                entry_boundary_at=retained.launch_authority.entry_boundary_at,
                entry_policy_hash=retained.launch_authority.entry_policy_hash,
                underlying_source_hash=retained.launch_authority.underlying_source_hash,
                benchmark_source_hash=retained.launch_authority.benchmark_source_hash,
                completed_bar_source_hash=retained.launch_authority.completed_bar_source_hash,
                created_at=retained.thesis_frozen_at,
            )
        )
    return SQLAlchemyLifecycleRepository(sessions), sessions, engine


def authorize_research(repo: SQLAlchemyLifecycleRepository, *, retained=None) -> None:
    retained = retained or context()
    payload = {"headline": "Issuer guidance remains unchanged"}
    result_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    repo.persist_research_sources(
        retained,
        (
            LifecycleResearchSource(
                logical_source_id="source-1",
                source_kind="MCP_NEWS",
                request_hash="6" * 64,
                result_hash=result_hash,
                normalized_payload=payload,
                observed_at=NOW - timedelta(seconds=4),
                retrieved_at=NOW - timedelta(seconds=2),
                source_hash="9" * 64,
            ),
        ),
        NOW,
    )


def persist(
    repo: SQLAlchemyLifecycleRepository, *, retained=None, research_authorized=True
) -> None:
    retained = retained or context()
    if research_authorized:
        authorize_research(repo, retained=retained)
    repo.persist(
        context=retained,
        observation=observation(),
        clusters=(cluster(),),
        classifications=(classification(),),
        manifest_id=UUID(int=901),
        manifest_hash="a" * 64,
        trusted_at=NOW,
    )


def test_validated_development_manifest_is_persisted_idempotently() -> None:
    repo, sessions, engine = repository()

    persist(repo)
    persist(repo)

    with sessions() as session:
        manifest = session.get(LifecycleObservationManifestRow, UUID(int=901))
        assert manifest is not None
        assert manifest.agent_input_snapshot_id is None
        assert manifest.reconciliation_id is None
        assert manifest.account_observation_id is not None
        assert manifest.source_authority_manifest is not None
        research = next(
            item
            for item in manifest.source_authority_manifest
            if item["external_source_id"] == "source-1"
        )
        assert research["request_hash"] == "6" * 64
        assert research["source_hash"] == "9" * 64
        assert session.scalar(select(LifecycleAccountObservationRow)) is not None
    engine.dispose()


def test_exact_active_development_context_is_reconstructed() -> None:
    repo, _sessions, engine = repository()

    loaded = repo.load(
        ObservedPaperAccountAuthority(AccountRole.DEVELOPMENT, FINGERPRINT, True, False)
    )

    assert loaded.managed_position_id == context().managed_position_id
    assert loaded.current_snapshot_id == context().current_snapshot_id
    assert loaded.thesis_version_id == context().thesis_version_id
    assert loaded.launch_authority == context().launch_authority
    engine.dispose()


@pytest.mark.parametrize(
    "retained",
    (
        context(account_role=AccountRole.SUBMISSION),
        context(account_fingerprint="9" * 64),
        context(thesis_version_id=UUID(int=999)),
        context(current_snapshot_id=UUID(int=998)),
        context(position_fingerprint="8" * 64),
    ),
)
def test_manifest_rejects_role_account_thesis_snapshot_or_position_substitution(retained) -> None:
    repo, sessions, engine = repository()

    with pytest.raises(LifecyclePersistenceError):
        persist(repo, retained=retained)

    with sessions() as session:
        assert session.scalar(select(LifecycleObservationManifestRow)) is None
    engine.dispose()


def test_manifest_identity_conflict_is_rejected() -> None:
    repo, _sessions, engine = repository()
    persist(repo)

    with pytest.raises(LifecyclePersistenceError, match="MANIFEST_ID_CONFLICT"):
        repo.persist(
            context=context(),
            observation=replace(observation(), atm_iv=observation().atm_iv),
            clusters=(cluster(),),
            classifications=(classification(),),
            manifest_id=UUID(int=901),
            manifest_hash="b" * 64,
            trusted_at=NOW,
        )
    engine.dispose()


def test_manifest_binds_to_one_exact_development_assessment_input() -> None:
    repo, sessions, engine = repository()
    persist(repo)
    input_id = UUID(int=902)
    with sessions.begin() as session:
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=input_id,
                thesis_version_id=context().thesis_version_id,
                account_role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=FINGERPRINT,
                decision_kind="ASSESSMENT",
                decision_boundary=NOW,
                observed_at=NOW,
                normalized_payload={
                    "acquisition_manifest_id": str(UUID(int=901)),
                    "acquisition_manifest_hash": "a" * 64,
                },
                input_hash="f" * 64,
                created_at=NOW,
            )
        )

    repo.bind_input(UUID(int=901), input_id, NOW)
    with pytest.raises(LifecyclePersistenceError, match="MANIFEST_BINDING_INVALID"):
        repo.bind_input(UUID(int=901), UUID(int=903), NOW)
    engine.dispose()


def test_submission_manifest_binds_to_exact_submission_assessment_after_restart() -> None:
    repo, sessions, engine = repository(AccountRole.SUBMISSION)
    retained = context(account_role=AccountRole.SUBMISSION)
    persist(repo, retained=retained)
    input_id = UUID(int=905)
    with sessions.begin() as session:
        session.add(
            AgentInputSnapshotRow(
                snapshot_id=input_id,
                thesis_version_id=retained.thesis_version_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint=FINGERPRINT,
                decision_kind="ASSESSMENT",
                decision_boundary=NOW,
                observed_at=NOW,
                normalized_payload={
                    "acquisition_manifest_id": str(UUID(int=901)),
                    "acquisition_manifest_hash": "a" * 64,
                },
                input_hash="e" * 64,
                created_at=NOW,
            )
        )

    restarted = SQLAlchemyLifecycleRepository(sessions)
    restarted.bind_input(UUID(int=901), input_id, NOW)
    loaded = restarted.load(
        ObservedPaperAccountAuthority(AccountRole.SUBMISSION, FINGERPRINT, True, False)
    )
    assert loaded.account_role is AccountRole.SUBMISSION
    assert loaded.lifecycle_transitions[-1].action == "ENTRY"
    engine.dispose()


def test_migration_replaces_the_market_session_unavailable_guard() -> None:
    sql = Path("migrations/0014_lifecycle_provider_authority.sql").read_text()

    assert "DROP TRIGGER alpaca_market_session_unavailable_guard" in sql
    assert "DROP FUNCTION alpaca_market_session_unavailable_guard" in sql


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("request_hash", "8" * 64),
        ("result_hash", "8" * 64),
        ("account_role", AccountRole.DEVELOPMENT.value),
        ("account_fingerprint", "8" * 64),
        ("observed_at", NOW - timedelta(seconds=20)),
        ("retrieved_at", NOW - timedelta(seconds=19)),
        ("normalized_payload", {"substituted": True}),
    ),
)
def test_source_hash_reuse_rejects_changed_immutable_authority(field, changed) -> None:
    repo, sessions, engine = repository()
    persist(repo)
    with sessions.begin() as session:
        source = session.scalar(
            select(LifecycleSourceObservationRow).where(
                LifecycleSourceObservationRow.source_kind == "OPTION_SNAPSHOT"
            )
        )
        assert source is not None
        setattr(source, field, changed)

    changed_sweep = replace(
        observation().sweep,
        retrieval_started_at=observation().sweep.retrieval_started_at - timedelta(microseconds=1),
    )
    with pytest.raises(LifecyclePersistenceError, match="SOURCE_HASH_CONFLICT"):
        repo.persist(
            context=context(),
            observation=replace(observation(), sweep=changed_sweep),
            clusters=(cluster(),),
            classifications=(classification(),),
            manifest_id=UUID(int=904),
            manifest_hash="c" * 64,
            trusted_at=NOW,
        )
    engine.dispose()


def test_manifest_rejects_research_without_durable_source_authority() -> None:
    repo, _sessions, engine = repository()

    with pytest.raises(LifecyclePersistenceError, match="RESEARCH_SOURCE_AUTHORITY_MISSING"):
        persist(repo, research_authorized=False)
    engine.dispose()
