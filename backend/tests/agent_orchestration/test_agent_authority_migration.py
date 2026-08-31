from pathlib import Path

from backend.app.execution.reconciliation import ReconciliationPurpose
from backend.app.persistence.runtime import discover_migrations

MIGRATIONS = Path(__file__).parents[3] / "migrations"


def test_agent_authority_migration_is_discovered_after_historical_chain() -> None:
    migrations = discover_migrations(MIGRATIONS)

    assert [item.version for item in migrations] == list(range(1, 27))
    assert migrations[11].filename == "0012_agent_decision_authority.sql"
    assert migrations[22].filename == "0023_competition_record_archive.sql"
    assert migrations[23].filename == "0024_submission_opportunity_authority.sql"


def test_order_status_migration_updates_status_authority_and_tick_guard() -> None:
    migration = (MIGRATIONS / "0026_order_status_terminal_outcomes.sql").read_text()

    assert "ck_entry_materialization_terminal_status" in migration
    assert "'REPLACED'" in migration
    assert "UNMANAGED_PARTIAL_EXPOSURE" in migration
    assert "BROKER_TRANSITION_STALLED" in migration
    assert "TARGETED_LOOKUP_FAILURE" in migration
    assert "AGENT_TICK_TERMINAL_STATUS_LIST_NOT_FOUND" in migration
    assert "'PARTIAL_CANCELED_RECONCILED'" in migration
    assert "'PARTIAL_EXPIRED_RECONCILED'" in migration
    assert "'PARTIAL_REPLACED_RECONCILED'" in migration


def test_entry_materialization_result_keeps_filled_certificate_authority() -> None:
    migration = (MIGRATIONS / "0017_entry_materialization_result.sql").read_text()

    assert "CREATE TABLE entry_materialization_jobs" in migration
    assert "ENTRY_MATERIALIZATION_JOB_AUTHORITY_INVALID" in migration
    assert "ENTRY_FILLED_MATERIALIZATION_FAILED" in migration
    assert "THEN 'FILLED'" in migration
    assert "certificate.entry_approval_id IS NOT NULL" in migration
    assert "certificate.entry_approval_id = intent.entry_approval_id" in migration
    assert "agent tick execution terminal requires certificate" in migration
    assert "ENTRY_MATERIALIZATION_PREPARATION_FAILED" in migration
    assert "NEW.job_hash IS DISTINCT FROM expected_job_hash" in migration


def test_entry_materialization_migration_enforces_decision_thesis_chronology() -> None:
    migration = (MIGRATIONS / "0017_entry_materialization_result.sql").read_text()

    assert "NEW.decision_kind='OPPORTUNITY'" in migration
    assert "NEW.decision_boundary > thesis.frozen_at" in migration
    assert "NEW.decision_kind='ASSESSMENT'" in migration
    assert "thesis.frozen_at > NEW.decision_boundary" in migration
    assert "thesis.account_role IS DISTINCT FROM NEW.account_role" in migration
    assert "thesis.policy_hash IS DISTINCT FROM NEW.policy_hash" in migration


def test_lifecycle_materialization_result_retains_exact_filled_certificate() -> None:
    migration = (MIGRATIONS / "0019_lifecycle_materialization_result.sql").read_text()
    normalized = " ".join(migration.split())

    assert "CREATE OR REPLACE FUNCTION guard_agent_tick_transition()" in migration
    assert "LIFECYCLE_FILLED_MATERIALIZATION_FAILED" in migration
    assert "certificate.execution_status = CASE" in migration
    assert "THEN 'FILLED'" in migration
    assert "certificate.entry_approval_id IS NULL" in migration
    assert "certificate.assessment_certificate_id = intent.assessment_certificate_id" in normalized
    assert "assessment_authorization.action = intent.action" in normalized
    assert "intent.action IN ('CLOSE', 'ROLL')" in migration


def test_baseline_initialization_has_no_live_lock_clear_alias() -> None:
    assert ReconciliationPurpose.BASELINE_INITIALIZATION.value == "BASELINE_INITIALIZATION"
    assert "LOCK_CLEAR" not in ReconciliationPurpose.__members__


def test_migration_permanently_latches_accounts_and_disables_legacy_recovery() -> None:
    migration = (MIGRATIONS / "0012_agent_decision_authority.sql").read_text()

    assert "OLD.execution_locked" in migration
    assert "NOT NEW.execution_locked" in migration
    assert "NEW.recovery_pending" in migration
    assert "BEFORE INSERT OR UPDATE OR DELETE ON recovery_cases" in migration
    assert "BEFORE INSERT OR UPDATE OR DELETE ON recovery_events" in migration
    assert "BEFORE INSERT OR UPDATE OR DELETE ON recovery_certificates" in migration
    assert "BASELINE_INITIALIZATION" in migration
    assert "UPDATE whole_account_reconciliations" in migration


def test_new_authority_records_are_append_only_and_submission_is_no_trade_only() -> None:
    migration = (MIGRATIONS / "0012_agent_decision_authority.sql").read_text()

    for table in ("agent_input_snapshots", "agent_decisions", "agent_ticks"):
        assert f"BEFORE UPDATE OR DELETE ON {table}" in migration
    assert "CALIBRATION_BINDING_NO_TRADE" in migration
    assert "machine_binding_hash" in migration
    assert "UNIQUE (account_role, decision_kind, decision_boundary)" in migration
    assert "UNIQUE (account_role, tick_key)" in migration
    assert "status = 'RESERVED' AND NEW.status = 'COMPLETED'" in migration
