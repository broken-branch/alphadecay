from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.alpaca.opportunity import OpportunitySnapshotRequest
from backend.app.contracts.v1 import AccountRole
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineSeal,
    OpportunityEvidenceError,
    OpportunityObservationSpec,
    OpportunityPlanSpec,
    SQLAlchemyOpportunityEvidenceRepository,
    _baseline_material,
    _hash,
    _plain_hash,
    _plan_material,
    opportunity_plan_digest,
)
from backend.app.persistence.runtime import apply_migrations, discover_migrations, verify_schema
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    Base,
    DevelopmentOpportunityBaselineRow,
    DevelopmentOpportunityPlanRow,
    OpportunityObservationManifestRow,
)
from backend.app.policy.opportunity import OpportunityPolicy

ACCOUNT = "a" * 64
NOW = datetime(2026, 8, 30, 15, tzinfo=UTC)
MIGRATIONS = Path(__file__).parents[3] / "migrations"
POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"


def _policy() -> OpportunityPolicy:
    return OpportunityPolicy(
        version="test-v1",
        opportunity_key="ACME_EARNINGS",
        underlying="ACME",
        selected_decision_boundary=NOW + timedelta(days=1, hours=2),
        last_entry_boundary=NOW + timedelta(days=1, hours=3),
        maximum_decision_delay=timedelta(minutes=5),
        maximum_underlying_age=timedelta(minutes=2),
        maximum_catalyst_age=timedelta(days=1),
        maximum_option_quote_age=timedelta(minutes=1),
        maximum_leg_quote_skew=timedelta(seconds=5),
        minimum_vwap_distance=Decimal("0.01"),
        maximum_vwap_distance=Decimal("0.05"),
        minimum_relative_return=Decimal("0.01"),
        minimum_beta=Decimal("0.1"),
        maximum_beta=Decimal("3"),
        required_trend_hits=3,
        maximum_first_reaction=Decimal("0.20"),
        minimum_catalyst_score=50,
        minimum_candidate_score=50,
        minimum_dte=7,
        maximum_dte=45,
        maximum_relative_spread=Decimal("0.25"),
        minimum_debit_width_fraction=Decimal("0.1"),
        maximum_debit_width_fraction=Decimal("0.8"),
        minimum_credit_width_fraction=Decimal("0.1"),
        maximum_position_loss=Decimal("1250"),
        maximum_equity_risk_fraction=Decimal("0.02"),
        maximum_lifetime_entries=3,
        maximum_lifetime_risk=Decimal("3000"),
        equity_floor=Decimal("90000"),
        maximum_quantity=2,
    )


def _plan() -> OpportunityPlanSpec:
    return OpportunityPlanSpec(
        opportunity_key="ACME_EARNINGS",
        version=1,
        underlying="ACME",
        event_session=date(2026, 8, 28),
        pre_event_session=date(2026, 8, 27),
        reaction_session=date(2026, 8, 31),
        signal_session=date(2026, 8, 31),
        daily_start_session=date(2026, 6, 1),
        allowed_event_codes=("RESULTS", "GUIDANCE"),
        evidence_window_start=NOW,
        evidence_window_end=NOW + timedelta(days=1),
        policy=_policy(),
        request_contract=OpportunitySnapshotRequest(
            account_role=AccountRole.DEVELOPMENT,
            expected_account_fingerprint=ACCOUNT,
            underlying="ACME",
            benchmark="QQQ",
            decision_boundary=NOW + timedelta(days=1, hours=2),
            minimum_expiry=date(2026, 9, 8),
            maximum_expiry=date(2026, 9, 18),
            minimum_strike=Decimal("50"),
            maximum_strike=Decimal("150"),
        ),
        thesis_code="POST_EVENT_CONTINUATION",
        thesis_target_contract={"target_kind": "session_close"},
        exposure_limit_contract={"shape": "defined_risk_vertical"},
        invalidation_codes=("RELATIVE_STRENGTH_LOST", "CATALYST_CONTRADICTED"),
        frozen_at=NOW,
    )


def _baseline(plan_id) -> OpportunityBaselineSeal:
    return OpportunityBaselineSeal(
        plan_id=plan_id,
        account_fingerprint=ACCOUNT,
        account_source_hash="1" * 64,
        positions_manifest=(),
        positions_source_hash="2" * 64,
        orders_manifest=(),
        orders_source_hash="3" * 64,
        activity_manifest=({"activity_id": "prior-1"},),
        activity_source_hash="4" * 64,
        book_hash="5" * 64,
        history_hash="6" * 64,
        captured_at=NOW + timedelta(minutes=1),
    )


def _observation(plan, baseline) -> OpportunityObservationSpec:
    return OpportunityObservationSpec(
        plan_id=plan.plan_id,
        baseline_id=baseline.baseline_id,
        account_fingerprint=ACCOUNT,
        policy_hash=plan.policy_hash,
        request_hash="7" * 64,
        snapshot_hash="8" * 64,
        calendar_hash="9" * 64,
        daily_hash="a" * 64,
        intraday_hash="b" * 64,
        signal_authority_hash="3" * 64,
        halt_hash="c" * 64,
        catalyst_hash="d" * 64,
        greek_hash="e" * 64,
        account_hash="f" * 64,
        activity_hash="0" * 64,
        budget_hash="1" * 64,
        prior_decision_hash="2" * 64,
        trusted_at=NOW + timedelta(minutes=2),
        evaluated_at=NOW + timedelta(minutes=3),
    )


def _sqlite_repository():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    return SQLAlchemyOpportunityEvidenceRepository(sessions), sessions, engine


def test_persists_complete_hash_bound_lineage_and_exact_replay() -> None:
    repository, sessions, engine = _sqlite_repository()
    plan = repository.freeze_plan(_plan())
    baseline = repository.seal_baseline(_baseline(plan.plan_id))
    observation = repository.append_observation(_observation(plan, baseline))

    assert repository.freeze_plan(_plan()) == plan
    assert repository.seal_baseline(_baseline(plan.plan_id)) == baseline
    assert repository.append_observation(_observation(plan, baseline)) == observation
    with sessions() as session:
        plan_row = session.get(DevelopmentOpportunityPlanRow, plan.plan_id)
        baseline_row = session.get(DevelopmentOpportunityBaselineRow, baseline.baseline_id)
        manifest_row = session.get(OpportunityObservationManifestRow, observation.observation_id)
        assert plan_row is not None and plan_row.benchmark_symbol == "QQQ"
        assert baseline_row is not None and baseline_row.activity_manifest
        assert baseline_row.positions_complete and baseline_row.orders_complete
        assert manifest_row is not None and manifest_row.policy_hash == plan.policy_hash
    engine.dispose()


def test_loads_complete_typed_authority_after_restart() -> None:
    repository, sessions, engine = _sqlite_repository()
    expected_plan = _plan()
    plan = repository.freeze_plan(expected_plan)
    expected_baseline = _baseline(plan.plan_id)
    baseline = repository.seal_baseline(expected_baseline)
    expected_observation = _observation(plan, baseline)
    observation = repository.append_observation(expected_observation)

    restarted = SQLAlchemyOpportunityEvidenceRepository(sessions)
    loaded_plan = restarted.load_plan(expected_plan.opportunity_key)
    loaded_baseline = restarted.load_baseline(plan.plan_id)
    loaded_observation = restarted.load_observation(plan.plan_id)

    assert loaded_plan is not None
    assert loaded_plan.spec == expected_plan
    assert loaded_plan.persisted == plan
    assert loaded_baseline is not None
    assert loaded_baseline.seal == expected_baseline
    assert loaded_baseline.persisted == baseline
    assert loaded_observation is not None
    assert loaded_observation.spec == expected_observation
    assert loaded_observation.persisted == observation
    engine.dispose()


def test_loads_exact_or_latest_plan_and_keeps_missing_authority_absent() -> None:
    repository, _, engine = _sqlite_repository()
    first_spec = _plan()
    first = repository.freeze_plan(first_spec)
    second_spec = replace(
        first_spec,
        version=2,
        frozen_at=NOW + timedelta(minutes=1),
        evidence_window_start=NOW + timedelta(minutes=1),
    )
    second = repository.freeze_plan(second_spec)

    loaded_first = repository.load_plan(first_spec.opportunity_key, version=1)
    loaded_latest = repository.load_plan(first_spec.opportunity_key)
    assert loaded_first is not None and loaded_first.persisted == first
    assert loaded_latest is not None and loaded_latest.persisted == second
    assert repository.load_plan("MISSING_EVENT") is None
    assert repository.load_baseline(first.plan_id) is None
    assert repository.load_observation(first.plan_id) is None
    assert repository.load_baseline(uuid4()) is None
    assert repository.load_observation(uuid4()) is None
    engine.dispose()


def test_plan_version_sequences_are_independent_by_executable_role() -> None:
    repository, sessions, engine = _sqlite_repository()
    submission_account = "b" * 64
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.SUBMISSION.value,
                account_fingerprint=submission_account,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    development_one = _plan()
    submission_one = replace(
        development_one,
        account_role=AccountRole.SUBMISSION,
        request_contract=replace(
            development_one.request_contract,
            account_role=AccountRole.SUBMISSION,
            expected_account_fingerprint=submission_account,
        ),
    )
    development_two = replace(
        development_one,
        version=2,
        frozen_at=NOW + timedelta(minutes=1),
        evidence_window_start=NOW + timedelta(minutes=1),
    )
    submission_two = replace(
        submission_one,
        version=2,
        frozen_at=NOW + timedelta(minutes=1),
        evidence_window_start=NOW + timedelta(minutes=1),
    )

    assert repository.freeze_plan(development_one).version == 1
    assert repository.freeze_plan(submission_one).version == 1
    assert repository.freeze_plan(development_two).version == 2
    assert repository.freeze_plan(submission_two).version == 2
    assert (
        repository.load_plan(
            development_one.opportunity_key,
            account_role=AccountRole.DEVELOPMENT,
        ).persisted.version
        == 2
    )
    assert (
        repository.load_plan(
            submission_one.opportunity_key,
            account_role=AccountRole.SUBMISSION,
        ).persisted.version
        == 2
    )
    engine.dispose()


def test_load_revalidates_account_payload_hash_and_chronology_authority() -> None:
    repository, sessions, engine = _sqlite_repository()
    plan = repository.freeze_plan(_plan())
    baseline = repository.seal_baseline(_baseline(plan.plan_id))
    observation = repository.append_observation(_observation(plan, baseline))

    with sessions.begin() as session:
        row = session.get(DevelopmentOpportunityPlanRow, plan.plan_id)
        assert row is not None
        row.request_contract = {**row.request_contract, "maximum_contracts": 999}
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_VERSION_CONFLICT"):
        repository.load_plan(_plan().opportunity_key)

    with sessions.begin() as session:
        row = session.get(DevelopmentOpportunityPlanRow, plan.plan_id)
        assert row is not None
        row.request_contract = dict(row.plan_material["request_contract"])
        account = session.get(AccountRoleRow, AccountRole.DEVELOPMENT.value)
        assert account is not None
        account.account_fingerprint = "f" * 64
    with pytest.raises(OpportunityEvidenceError, match="DEVELOPMENT_ACCOUNT_MISMATCH"):
        repository.load_baseline(plan.plan_id)

    with sessions.begin() as session:
        account = session.get(AccountRoleRow, AccountRole.DEVELOPMENT.value)
        assert account is not None
        account.account_fingerprint = ACCOUNT
        row = session.get(OpportunityObservationManifestRow, observation.observation_id)
        assert row is not None
        row.trusted_at = NOW
        row.observation_material = {
            **row.observation_material,
            "trusted_at": NOW.isoformat(),
        }
        row.manifest_hash = _hash("alphadecay.opportunity.observation.v1", row.observation_material)
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_OBSERVATION_CONFLICT"):
        repository.load_observation(plan.plan_id)
    engine.dispose()


@pytest.mark.parametrize("version", [0, -1, True, "1"])
def test_rejects_invalid_read_lookups(version) -> None:
    repository, _, engine = _sqlite_repository()
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_LOOKUP_INVALID"):
        repository.load_plan("ACME_EARNINGS", version=version)
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_LOOKUP_INVALID"):
        repository.load_plan("not valid")
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_BASELINE_LOOKUP_INVALID"):
        repository.load_baseline("not-a-uuid")
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_OBSERVATION_LOOKUP_INVALID"):
        repository.load_observation("not-a-uuid")
    engine.dispose()


def test_rejects_plan_version_baseline_and_observation_substitution() -> None:
    repository, _, engine = _sqlite_repository()
    plan = repository.freeze_plan(_plan())
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_VERSION_CONFLICT"):
        repository.freeze_plan(replace(_plan(), thesis_code="OTHER_THESIS"))

    baseline = repository.seal_baseline(_baseline(plan.plan_id))
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_BASELINE_CONFLICT"):
        repository.seal_baseline(replace(_baseline(plan.plan_id), history_hash="f" * 64))

    repository.append_observation(_observation(plan, baseline))
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_OBSERVATION_CONFLICT"):
        repository.append_observation(replace(_observation(plan, baseline), catalyst_hash="3" * 64))
    engine.dispose()


def test_rejects_incomplete_history_wrong_account_and_chronology() -> None:
    repository, _, engine = _sqlite_repository()
    plan = repository.freeze_plan(_plan())
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_BASELINE_INCOMPLETE"):
        repository.seal_baseline(replace(_baseline(plan.plan_id), activity_complete=False))
    with pytest.raises(OpportunityEvidenceError, match="DEVELOPMENT_ACCOUNT_MISMATCH"):
        repository.seal_baseline(replace(_baseline(plan.plan_id), account_fingerprint="f" * 64))
    baseline = repository.seal_baseline(_baseline(plan.plan_id))
    with pytest.raises(
        OpportunityEvidenceError, match="OPPORTUNITY_OBSERVATION_CHRONOLOGY_INVALID"
    ):
        repository.append_observation(replace(_observation(plan, baseline), trusted_at=NOW))
    engine.dispose()


def test_rejects_non_utc_and_invalid_plan_contracts() -> None:
    repository, _, engine = _sqlite_repository()
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_TIME_INVALID"):
        repository.freeze_plan(replace(_plan(), frozen_at=NOW.replace(tzinfo=None)))
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_INVALID"):
        repository.freeze_plan(
            replace(
                _plan(),
                request_contract=replace(_plan().request_contract, benchmark="SPY"),
            )
        )
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_INVALID"):
        repository.freeze_plan(replace(_plan(), invalidation_codes=()))
    with pytest.raises(OpportunityEvidenceError, match="DEVELOPMENT_ACCOUNT_MISMATCH"):
        repository.freeze_plan(
            replace(
                _plan(),
                request_contract=replace(
                    _plan().request_contract, expected_account_fingerprint="f" * 64
                ),
            )
        )
    engine.dispose()


def test_acquisition_plan_authority_is_validated_and_hash_bound() -> None:
    repository, _, engine = _sqlite_repository()
    plan = _plan()
    digests = {
        opportunity_plan_digest(plan),
        opportunity_plan_digest(
            replace(plan, daily_start_session=plan.daily_start_session + timedelta(days=1))
        ),
        opportunity_plan_digest(replace(plan, allowed_event_codes=("RESULTS",))),
        opportunity_plan_digest(
            replace(plan, evidence_window_start=plan.evidence_window_start + timedelta(minutes=1))
        ),
        opportunity_plan_digest(
            replace(plan, evidence_window_end=plan.evidence_window_end - timedelta(minutes=1))
        ),
    }
    assert len(digests) == 5

    invalid_specs = (
        replace(plan, daily_start_session=plan.pre_event_session),
        replace(plan, daily_start_session=date(2026, 5, 31)),
        replace(plan, daily_start_session=date(2026, 4, 1)),
        replace(plan, allowed_event_codes=()),
        replace(plan, allowed_event_codes=("RESULTS", "RESULTS")),
        replace(plan, allowed_event_codes=("not-a-code",)),
        replace(plan, evidence_window_start=plan.frozen_at - timedelta(microseconds=1)),
        replace(
            plan,
            evidence_window_end=(
                plan.policy.selected_decision_boundary + timedelta(microseconds=1)
            ),
        ),
    )
    for invalid in invalid_specs:
        with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_INVALID"):
            repository.freeze_plan(invalid)
    engine.dispose()


def test_rejects_plan_version_gaps_and_non_increasing_freeze_time() -> None:
    repository, _, engine = _sqlite_repository()
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_VERSION_INVALID"):
        repository.freeze_plan(replace(_plan(), version=2))
    repository.freeze_plan(_plan())
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_VERSION_INVALID"):
        repository.freeze_plan(
            replace(
                _plan(),
                version=3,
                frozen_at=NOW + timedelta(minutes=2),
                evidence_window_start=NOW + timedelta(minutes=2),
            )
        )
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_PLAN_VERSION_INVALID"):
        repository.freeze_plan(replace(_plan(), version=2))
    engine.dispose()


def test_exact_replay_revalidates_stored_payloads_and_nested_inputs_are_copied() -> None:
    repository, sessions, engine = _sqlite_repository()
    plan = repository.freeze_plan(_plan())
    mutable_detail = {"state": "settled"}
    mutable_activity = {"activity_id": "prior-1", "detail": mutable_detail}
    seal = replace(_baseline(plan.plan_id), activity_manifest=(mutable_activity,))
    baseline = repository.seal_baseline(seal)
    mutable_detail["state"] = "changed"
    with sessions.begin() as session:
        row = session.get(DevelopmentOpportunityBaselineRow, baseline.baseline_id)
        assert row is not None
        assert row.activity_manifest[0]["detail"] == {"state": "settled"}
        row.baseline_material = {**row.baseline_material, "history_hash": "f" * 64}
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_BASELINE_CONFLICT"):
        repository.seal_baseline(
            replace(
                _baseline(plan.plan_id),
                activity_manifest=({"activity_id": "prior-1", "detail": {"state": "settled"}},),
            )
        )
    engine.dispose()


def test_rejects_baseline_before_plan_and_cross_plan_observation() -> None:
    repository, _, engine = _sqlite_repository()
    plan = repository.freeze_plan(_plan())
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID"):
        repository.seal_baseline(
            replace(_baseline(plan.plan_id), captured_at=NOW - timedelta(seconds=1))
        )
    baseline = repository.seal_baseline(_baseline(plan.plan_id))
    other_policy = replace(_policy(), opportunity_key="OTHER_EVENT", underlying="OTHER")
    other_plan = repository.freeze_plan(
        replace(
            _plan(),
            opportunity_key="OTHER_EVENT",
            underlying="OTHER",
            policy=other_policy,
            request_contract=replace(_plan().request_contract, underlying="OTHER"),
        )
    )
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_OBSERVATION_LINEAGE_INVALID"):
        repository.append_observation(
            replace(_observation(plan, baseline), plan_id=other_plan.plan_id)
        )
    engine.dispose()


def _postgres_engine():
    url = os.getenv(POSTGRES_URL_ENV)
    if not url:
        pytest.skip(f"{POSTGRES_URL_ENV} is not configured")
    return create_engine(url)


def test_postgres_fresh_upgrade_replay_and_immutability() -> None:
    engine = _postgres_engine()
    migrations = discover_migrations(MIGRATIONS)
    assert [migration.version for migration in migrations] == list(range(1, 27))
    assert migrations[19].sha256 == (
        "62dd83dcd3fe2392cbd5ee31330e8218776e9b8e8a57afc106c18c8f05627cc9"
    )
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    apply_migrations(engine, migrations[:-1])
    apply_migrations(engine, migrations)
    apply_migrations(engine, migrations)
    verify_schema(engine)

    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    repository = SQLAlchemyOpportunityEvidenceRepository(sessions)
    plan = repository.freeze_plan(_plan())
    baseline = repository.seal_baseline(_baseline(plan.plan_id))
    observation = repository.append_observation(_observation(plan, baseline))
    assert repository.append_observation(_observation(plan, baseline)) == observation
    loaded_plan = repository.load_plan(_plan().opportunity_key)
    loaded_baseline = repository.load_baseline(plan.plan_id)
    loaded_observation = repository.load_observation(plan.plan_id)
    assert loaded_plan is not None and loaded_plan.spec == _plan()
    assert loaded_baseline is not None and loaded_baseline.seal == _baseline(plan.plan_id)
    assert loaded_observation is not None
    assert loaded_observation.spec == _observation(plan, baseline)

    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT opportunity_evidence_hash("
                "'alphadecay.opportunity.plan.v1', plan_material) = plan_hash "
                "FROM development_opportunity_plans WHERE plan_id = :id"
            ),
            {"id": plan.plan_id},
        ).scalar_one()
        assert connection.execute(
            text(
                "SELECT opportunity_evidence_hash("
                "'alphadecay.opportunity.baseline.v1', baseline_material) = baseline_hash "
                "FROM development_opportunity_baselines WHERE baseline_id = :id"
            ),
            {"id": baseline.baseline_id},
        ).scalar_one()
        assert connection.execute(
            text(
                "SELECT opportunity_evidence_hash("
                "'alphadecay.opportunity.observation.v1', observation_material) = manifest_hash "
                "FROM opportunity_observation_manifests WHERE observation_id = :id"
            ),
            {"id": observation.observation_id},
        ).scalar_one()

    mutations = (
        (
            "UPDATE development_opportunity_plans SET thesis_code = 'OTHER' WHERE plan_id = :id",
            plan.plan_id,
        ),
        ("DELETE FROM development_opportunity_plans WHERE plan_id = :id", plan.plan_id),
        (
            "UPDATE development_opportunity_baselines SET history_hash = :hash "
            "WHERE baseline_id = :id",
            baseline.baseline_id,
        ),
        (
            "DELETE FROM development_opportunity_baselines WHERE baseline_id = :id",
            baseline.baseline_id,
        ),
        (
            "UPDATE opportunity_observation_manifests SET catalyst_hash = :hash "
            "WHERE observation_id = :id",
            observation.observation_id,
        ),
        (
            "DELETE FROM opportunity_observation_manifests WHERE observation_id = :id",
            observation.observation_id,
        ),
    )
    for statement, row_id in mutations:
        with (
            engine.connect() as connection,
            pytest.raises(DBAPIError, match="DEVELOPMENT_OPPORTUNITY_EVIDENCE_IMMUTABLE"),
        ):
            connection.execute(text(statement), {"hash": "4" * 64, "id": row_id})
    engine.dispose()


def test_postgres_rejects_unbound_and_semantically_invalid_request_contracts() -> None:
    engine = _postgres_engine()
    migrations = discover_migrations(MIGRATIONS)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    apply_migrations(engine, migrations)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    repository = SQLAlchemyOpportunityEvidenceRepository(sessions)
    repository.freeze_plan(_plan())

    forged_spec = replace(
        _plan(),
        version=2,
        frozen_at=NOW + timedelta(minutes=1),
        evidence_window_start=NOW + timedelta(minutes=1),
    )
    original, policy, valid_request, thesis_target, exposure_limit = _plan_material(forged_spec)
    invalid_requests = (
        ({"tampered": True}, "f" * 64),
        (
            {**valid_request, "maximum_contracts": 999},
            _plain_hash({**valid_request, "maximum_contracts": 999}),
        ),
    )
    for request, request_hash in invalid_requests:
        material = {
            **original,
            "request_contract": request,
            "request_contract_hash": request_hash,
        }
        forged = DevelopmentOpportunityPlanRow(
            plan_id=uuid4(),
            opportunity_key=forged_spec.opportunity_key,
            version=2,
            account_role=AccountRole.DEVELOPMENT.value,
            underlying=forged_spec.underlying,
            benchmark_symbol="QQQ",
            event_session=forged_spec.event_session,
            pre_event_session=forged_spec.pre_event_session,
            reaction_session=forged_spec.reaction_session,
            signal_session=forged_spec.signal_session,
            daily_start_session=forged_spec.daily_start_session,
            allowed_event_codes=list(forged_spec.allowed_event_codes),
            evidence_window_start=forged_spec.evidence_window_start,
            evidence_window_end=forged_spec.evidence_window_end,
            policy_payload=policy,
            policy_hash=material["policy_hash"],
            request_contract=request,
            request_contract_hash=request_hash,
            thesis_code=forged_spec.thesis_code,
            thesis_target_contract=thesis_target,
            thesis_target_hash=material["thesis_target_hash"],
            exposure_limit_contract=exposure_limit,
            exposure_limit_hash=material["exposure_limit_hash"],
            invalidation_codes=list(forged_spec.invalidation_codes),
            frozen_at=forged_spec.frozen_at,
            plan_material=material,
            plan_hash=_hash("alphadecay.opportunity.plan.v1", material),
        )
        with (
            sessions.begin() as session,
            pytest.raises(DBAPIError, match="DEVELOPMENT_OPPORTUNITY_PLAN_INVALID"),
        ):
            session.add(forged)
            session.flush()
    engine.dispose()


def test_postgres_concurrent_exact_replay_creates_one_lineage() -> None:
    engine = _postgres_engine()
    migrations = discover_migrations(MIGRATIONS)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    apply_migrations(engine, migrations)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    repository = SQLAlchemyOpportunityEvidenceRepository(sessions)
    barrier = Barrier(2)

    def persist_lineage():
        barrier.wait()
        plan = repository.freeze_plan(_plan())
        baseline = repository.seal_baseline(_baseline(plan.plan_id))
        observation = repository.append_observation(_observation(plan, baseline))
        return plan, baseline, observation

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(persist_lineage), pool.submit(persist_lineage))
        results = tuple(future.result() for future in futures)
    assert results[0] == results[1]
    with sessions() as session:
        assert session.query(DevelopmentOpportunityPlanRow).count() == 1
        assert session.query(DevelopmentOpportunityBaselineRow).count() == 1
        assert session.query(OpportunityObservationManifestRow).count() == 1
    engine.dispose()


def test_postgres_rejects_submission_rows_and_cross_account_substitution() -> None:
    engine = _postgres_engine()
    migrations = discover_migrations(MIGRATIONS)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    apply_migrations(engine, migrations)
    verify_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO account_roles "
                "(role, account_fingerprint, equity, autonomous_enabled) "
                "VALUES ('DEVELOPMENT', :development, 100000, false), "
                "('SUBMISSION', :submission, 100000, false)"
            ),
            {"development": ACCOUNT, "submission": "f" * 64},
        )
    sessions = sessionmaker(engine, expire_on_commit=False)
    repository = SQLAlchemyOpportunityEvidenceRepository(sessions)
    plan = repository.freeze_plan(_plan())
    submission_seal = replace(_baseline(plan.plan_id), account_fingerprint="f" * 64)
    material, positions, orders, activity = _baseline_material(submission_seal)
    material = {**material, "account_role": AccountRole.SUBMISSION.value}
    with sessions.begin() as session, pytest.raises(DBAPIError):
        session.add(
            DevelopmentOpportunityBaselineRow(
                baseline_id=uuid4(),
                plan_id=plan.plan_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint="f" * 64,
                account_source_hash=submission_seal.account_source_hash,
                positions_manifest=positions,
                positions_source_hash=submission_seal.positions_source_hash,
                positions_complete=True,
                orders_manifest=orders,
                orders_source_hash=submission_seal.orders_source_hash,
                orders_complete=True,
                activity_manifest=activity,
                activity_source_hash=submission_seal.activity_source_hash,
                activity_complete=True,
                book_hash=submission_seal.book_hash,
                history_hash=submission_seal.history_hash,
                captured_at=submission_seal.captured_at,
                baseline_material=material,
                baseline_hash=_hash("alphadecay.opportunity.baseline.v1", material),
            )
        )
        session.flush()
    with pytest.raises(OpportunityEvidenceError, match="DEVELOPMENT_ACCOUNT_MISMATCH"):
        repository.seal_baseline(replace(_baseline(plan.plan_id), account_fingerprint="f" * 64))
    engine.dispose()
