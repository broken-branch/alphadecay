from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from backend.app.lifecycle.fingerprint import option_position_fingerprint
from backend.app.persistence.runtime import discover_migrations
from backend.app.persistence.sqlalchemy_models import Base

MIGRATIONS = Path(__file__).parents[3] / "migrations"


def test_lifecycle_authority_migration_is_next_and_complete() -> None:
    migrations = discover_migrations(MIGRATIONS)
    assert [item.version for item in migrations] == list(range(1, 37))
    opportunity_persistence = migrations[20]
    assert opportunity_persistence.filename == "0021_opportunity_thesis_persistence.sql"
    assert opportunity_persistence.sha256 == (
        "c0605d86d13fa4fac062060a9408aecfe7e54e37c133a155ac64fca3be053333"
    )
    for required in (
        "signal_authority_hash",
        "OPPORTUNITY_SIGNAL_AUTHORITY_MIGRATION_REQUIRES_ZERO_OBSERVATIONS",
        "uq_thesis_origin_hash",
        "alphadecay.lifecycle.thesis.v2",
        "THESIS_PAYLOAD_HASH_MISMATCH",
    ):
        assert required in opportunity_persistence.sql
    plan_authority = migrations[21]
    assert plan_authority.filename == "0022_opportunity_acquisition_plan_authority.sql"
    for required in (
        "daily_start_session",
        "allowed_event_codes",
        "evidence_window_start",
        "evidence_window_end",
        "OPPORTUNITY_PLAN_AUTHORITY_MIGRATION_REQUIRES_ZERO_PLANS",
    ):
        assert required in plan_authority.sql
    assert migrations[22].filename == "0023_competition_record_archive.sql"
    assert migrations[23].filename == "0024_submission_opportunity_authority.sql"
    roll_authority = migrations[24]
    assert roll_authority.filename == "0025_lifecycle_roll_authority.sql"
    assert "existing roll intents cannot acquire frozen lifecycle authority" in roll_authority.sql
    for field in (
        "quoted_relative_spread",
        "maximum_relative_spread",
        "incremental_debit",
        "maximum_incremental_debit",
    ):
        assert f"{field} IS NOT NULL" in roll_authority.sql
    assert migrations[25].filename == "0026_order_status_terminal_outcomes.sql"
    structural_authority = migrations[27]
    assert structural_authority.filename == "0028_structural_pilot_policy_hash.sql"
    for required in (
        "opportunity_frozen_policy_hash",
        "SPY_STRUCTURAL_BULLISH_BETA_PILOT_V1",
        "opportunity_final_reconciliation_hash",
        "managed_position_snapshot_guard",
    ):
        assert required in structural_authority.sql
    migration = migrations[12]
    assert migration.filename == "0013_lifecycle_acquisition_authority.sql"
    for required in (
        "CREATE TABLE thesis_versions",
        "thesis_version_id uuid NOT NULL",
        "CREATE TABLE greek_authority_versions",
        "CREATE TABLE alpaca_market_sessions",
        "CREATE TABLE managed_lifecycle_positions",
        "CREATE TABLE managed_position_transitions",
        "CREATE TABLE managed_position_snapshots",
        "CREATE TABLE lifecycle_observation_manifests",
        "LIFECYCLE_AUTHORITY_REQUIRES_VERIFIED_ZERO_HISTORY",
        "IN ACCESS EXCLUSIVE MODE",
        "uq_active_managed_position_role",
        "market_session_id uuid NOT NULL",
        "managed_snapshot_id uuid NOT NULL",
        "lifecycle_json_hash",
        "ALPACA_MARKET_SESSION_PROVIDER_AUTHORITY_UNAVAILABLE",
        "LIFECYCLE_OBSERVATION_PROVIDER_AUTHORITY_UNAVAILABLE",
        "managed_position_transition_guard",
        "managed_position_snapshot_guard",
        "lifecycle_observation_manifest_append_only",
    ):
        assert required in migration.sql
    for historical_table in (
        "entry_approval_certificates",
        "assessment_certificates",
        "execution_intents",
        "order_attempts",
        "attempt_observations",
        "execution_certificates",
        "account_reconciliation_states",
        "whole_account_reconciliations",
        "broker_mutation_permits",
        "agent_input_snapshots",
        "agent_decisions",
        "agent_ticks",
        "submission_baselines",
        "competition_entry_budget",
        "evidence_classification_claims",
        "evidence_classifications",
    ):
        assert f"EXISTS (SELECT 1 FROM {historical_table})" in migration.sql
    assert "EXISTS (SELECT 1 FROM model_call_budgets WHERE request_count<>0)" in migration.sql

    provider_migration = migrations[13]
    assert provider_migration.filename == "0014_lifecycle_provider_authority.sql"
    for required in (
        "DROP TRIGGER alpaca_market_session_unavailable_guard",
        "CREATE TABLE lifecycle_source_observations",
        "CREATE TABLE lifecycle_account_observations",
        "CREATE TABLE lifecycle_launch_authorities",
        "CREATE TABLE lifecycle_observation_bindings",
        "source_authority_manifest jsonb",
        "alpaca_market_session_provider_guard",
        "lifecycle_provider_manifest_guard",
    ):
        assert required in provider_migration.sql

    repair_migration = migrations[15]
    assert repair_migration.filename == "0016_managed_position_lineage_contract.sql"
    for required in (
        "CREATE FUNCTION lifecycle_position_fingerprint",
        "MANAGED_POSITION_LINEAGE_CONTRACT_REQUIRES_ZERO_HISTORY",
        "IN ACCESS EXCLUSIVE MODE",
        "CREATE OR REPLACE FUNCTION managed_position_transition_guard",
        "CREATE OR REPLACE FUNCTION managed_position_snapshot_guard",
        "certificate.attempt_ids ? attempt.client_order_id",
        "lifecycle_position_fingerprint(expected_inventory)",
    ):
        assert required in repair_migration.sql
    assert "certificate.attempt_ids ? attempt.attempt_id::text" not in repair_migration.sql


def test_position_fingerprint_canonicalizes_integral_decimal_scale() -> None:
    canonical = (
        ("NVDA260918C00170000", Decimal("1"), 100),
        ("NVDA260918C00180000", Decimal("-1"), 100),
    )
    scaled = (
        ("NVDA260918C00170000", Decimal("1.0"), 100),
        ("NVDA260918C00180000", Decimal("-1.00"), 100),
    )

    assert option_position_fingerprint(scaled) == option_position_fingerprint(canonical)


def test_position_fingerprint_accepts_empty_closed_inventory() -> None:
    assert option_position_fingerprint(()) == (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    )


def test_sqlalchemy_metadata_exposes_lifecycle_authority_tables() -> None:
    assert {
        "thesis_versions",
        "greek_authority_versions",
        "alpaca_market_sessions",
        "managed_lifecycle_positions",
        "managed_position_transitions",
        "managed_position_snapshots",
        "lifecycle_observation_manifests",
        "lifecycle_source_observations",
        "lifecycle_account_observations",
        "lifecycle_launch_authorities",
        "lifecycle_observation_bindings",
    } <= set(Base.metadata.tables)
    approval = Base.metadata.tables["entry_approval_certificates"]
    assessment = Base.metadata.tables["assessment_certificates"]
    assert "thesis_version_id" in approval.c
    assert "thesis_version_id" in assessment.c
    assert approval.c.thesis_version_id.nullable is False
    assert assessment.c.thesis_version_id.nullable is False
    agent_input = Base.metadata.tables["agent_input_snapshots"]
    agent_decision = Base.metadata.tables["agent_decisions"]
    assert "thesis_version_id" in agent_input.c
    assert "thesis_version_id" in agent_decision.c
    assert "market_session_id" in Base.metadata.tables["managed_position_transitions"].c
    managed = Base.metadata.tables["managed_lifecycle_positions"]
    assert not any(
        constraint.__class__.__name__ == "UniqueConstraint"
        and tuple(column.name for column in constraint.columns) == ("account_role",)
        for constraint in managed.constraints
    )


@pytest.mark.parametrize(
    "null_field",
    (
        "quoted_relative_spread",
        "maximum_relative_spread",
        "incremental_debit",
        "maximum_incremental_debit",
    ),
)
def test_sqlite_rejects_roll_intent_with_null_numeric_authority(null_field: str) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    values = {
        "quoted_relative_spread": "0.05",
        "maximum_relative_spread": "0.25",
        "incremental_debit": "100",
        "maximum_incremental_debit": "500",
    }
    values[null_field] = None

    with (
        pytest.raises(IntegrityError, match="ck_intent_roll_authority"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                """
                    INSERT INTO execution_intents(
                        intent_id, account_role, intent_digest, action, policy_hash,
                        event_key, trading_day, assessment_certificate_id, fingerprint,
                        envelope_hash, envelope_payload, legs, quantity, minimum_limit,
                        maximum_limit, approved_max_loss, market_session_id,
                        quoted_relative_spread, maximum_relative_spread, incremental_debit,
                        maximum_incremental_debit, state, first_fill_consumed
                    ) VALUES (
                        '10000000000000000000000000000001', 'DEVELOPMENT',
                        '1111111111111111111111111111111111111111111111111111111111111111',
                        'ROLL',
                        '2222222222222222222222222222222222222222222222222222222222222222',
                        'ROLL-NULL-AUTHORITY', '2026-08-30',
                        '30000000000000000000000000000001',
                        '3333333333333333333333333333333333333333333333333333333333333333',
                        '4444444444444444444444444444444444444444444444444444444444444444',
                        '{}', '[]', 1, 1, 2, 500,
                        '40000000000000000000000000000001',
                        :quoted_relative_spread, :maximum_relative_spread,
                        :incremental_debit, :maximum_incremental_debit, 'APPROVED', 0
                    )
                    """
            ),
            values,
        )

    engine.dispose()
