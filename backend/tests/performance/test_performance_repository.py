import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from inspect import signature
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.contracts.v1 import (
    AccountRole,
    BaselineStatus,
    MeasurementStatus,
    PerformanceFailureCode,
    PerformancePoint,
)
from backend.app.performance import (
    FINAL_PERFORMANCE_BOUNDARY,
    FINAL_PUBLICATION_NOT_BEFORE,
    NoEligiblePerformanceSnapshot,
    PerformanceProofIntegrityError,
    PerformanceSnapshot,
)
from backend.app.performance import repository as performance_repository
from backend.app.performance.repository import SQLAlchemyPerformanceRepository
from backend.app.persistence.runtime import apply_migrations, discover_migrations
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    Base,
    PerformancePublicationRow,
    SubmissionBaselineRow,
)

MIGRATIONS = Path(__file__).parents[3] / "migrations"

SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000501")
BASELINE_ID = UUID("00000000-0000-0000-0000-000000000500")


def repository() -> SQLAlchemyPerformanceRepository:
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
                account_fingerprint="f" * 64,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
        session.add(
            SubmissionBaselineRow(
                baseline_id=BASELINE_ID,
                account_role="SUBMISSION",
                account_fingerprint="f" * 64,
                equity=Decimal("100000"),
                captured_at=datetime(2026, 8, 28, 15, 0, tzinfo=UTC),
                positions_hash="1" * 64,
                orders_hash="2" * 64,
                activities_hash="3" * 64,
                contaminated=False,
            )
        )
    return SQLAlchemyPerformanceRepository(sessions)


def snapshot(
    *,
    snapshot_id: UUID = SNAPSHOT_ID,
    boundary_key: str = "2026-08-28T17:00:00Z",
    scheduled_for: datetime = datetime(2026, 8, 28, 17, 0, tzinfo=UTC),
    submission_baseline_id: UUID | None = BASELINE_ID,
    baseline_status: BaselineStatus = BaselineStatus.CLEAN,
    account_fingerprint: str = "f" * 64,
) -> PerformanceSnapshot:
    point = PerformancePoint(
        scheduled_for=scheduled_for,
        attempted_at=scheduled_for,
        measured_at=scheduled_for,
        status=MeasurementStatus.COMPLETE,
        failure_code=None,
        current_equity_usd=Decimal("100125.50"),
        account_equity_change_usd=Decimal("125.50"),
        account_equity_return_pct=Decimal("0.1255"),
        reconciled_lifecycle_cashflow_usd=Decimal("100.00"),
        open_position_liquidation_pnl_usd=Decimal("25.50"),
        simulator_limitations_code="ALPACA_PAPER_SIMULATION",
    )
    return PerformanceSnapshot.create(
        snapshot_id=snapshot_id,
        submission_baseline_id=submission_baseline_id,
        boundary_key=boundary_key,
        point=point,
        baseline_status=baseline_status,
        account_fingerprint=account_fingerprint,
        positions_manifest_hash="a" * 64,
        orders_manifest_hash="b" * 64,
        activities_manifest_hash="c" * 64,
    )


def test_empty_repository_returns_a_strict_unpublished_proof() -> None:
    proof = repository().latest_publication()

    assert proof.publication_status == "NOT_PUBLISHED"
    assert proof.point is None
    assert proof.linked_certificate_ids == ()


def test_capture_authority_comes_from_the_exact_registered_role_and_baseline() -> None:
    repo = repository()

    authority = repo.performance_capture_authority(AccountRole.SUBMISSION)

    assert authority.role == AccountRole.SUBMISSION
    assert authority.account_fingerprint == "f" * 64
    assert authority.baseline_id == BASELINE_ID
    assert authority.baseline_status == BaselineStatus.CLEAN
    assert authority.baseline_equity == Decimal("100000")
    assert authority.baseline_captured_at == datetime(2026, 8, 28, 15, 0, tzinfo=UTC)

    with repo._sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint="d" * 64,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )

    development = repo.performance_capture_authority(AccountRole.DEVELOPMENT)
    assert development.account_fingerprint == "d" * 64
    assert development.baseline_id is None
    assert development.baseline_status == BaselineStatus.NOT_CAPTURED
    assert development.baseline_equity is None
    assert development.baseline_captured_at is None


def test_snapshot_is_append_only_and_boundary_is_unique() -> None:
    repo = repository()
    original = snapshot()
    repo.append_snapshot(original)

    assert repo.get_snapshot(original.snapshot_id) == original
    repo.append_snapshot(original)
    with pytest.raises(ValueError, match="snapshot already exists"):
        repo.append_snapshot(
            snapshot(
                snapshot_id=original.snapshot_id,
                boundary_key="2026-08-28T17:15:00Z",
                scheduled_for=datetime(2026, 8, 28, 17, 15, tzinfo=UTC),
            )
        )
    with pytest.raises(ValueError, match="boundary already captured"):
        repo.append_snapshot(snapshot(snapshot_id=UUID("00000000-0000-0000-0000-000000000503")))


def test_snapshot_must_match_the_registered_submission_account_and_baseline() -> None:
    repo = repository()

    with pytest.raises(ValueError, match="account fingerprint"):
        repo.append_snapshot(snapshot(account_fingerprint="e" * 64))
    with pytest.raises(ValueError, match="baseline does not match"):
        repo.append_snapshot(
            snapshot(submission_baseline_id=UUID("00000000-0000-0000-0000-000000000599"))
        )

    with repo._sessions.begin() as session:
        baseline = session.get(SubmissionBaselineRow, BASELINE_ID)
        assert baseline is not None
        baseline.contaminated = True
    with pytest.raises(ValueError, match="status is not authoritative"):
        repo.append_snapshot(snapshot())


def test_publication_uses_latest_eligible_snapshot_and_forms_a_hash_chain() -> None:
    repo = repository()
    first = snapshot()
    second = snapshot(
        snapshot_id=UUID("00000000-0000-0000-0000-000000000503"),
        boundary_key="2026-08-28T17:30:00Z",
        scheduled_for=datetime(2026, 8, 28, 17, 30, tzinfo=UTC),
    )
    repo.append_snapshot(first)
    published_first = repo.publish_latest_eligible()
    repo.append_snapshot(second)
    published_second = repo.publish_latest_eligible()

    assert published_first.point == first.point
    assert published_second.point == second.point
    assert published_second.predecessor_hash == published_first.publication_hash
    assert repo.latest_publication() == published_second
    serialized = repo.latest_publication().model_dump(mode="json")
    payload_text = repo.latest_publication_text()
    assert payload_text == json.dumps(serialized, sort_keys=True, separators=(",", ":"))
    assert serialized["publication_hash"] == published_second.publication_hash
    assert "account_fingerprint" not in serialized
    assert "manifest" not in str(serialized)


@pytest.mark.parametrize(
    "defect", ("contaminated", "substituted", "missing", "fingerprint_mismatch")
)
def test_publication_revalidates_current_clean_submission_baseline(defect: str) -> None:
    repo = repository()
    repo.append_snapshot(snapshot())
    with repo._sessions.begin() as session:
        baseline = session.get(SubmissionBaselineRow, BASELINE_ID)
        assert baseline is not None
        if defect == "contaminated":
            baseline.contaminated = True
        elif defect == "substituted":
            session.delete(baseline)
            session.flush()
            session.add(
                SubmissionBaselineRow(
                    baseline_id=UUID("00000000-0000-0000-0000-000000000598"),
                    account_role="SUBMISSION",
                    account_fingerprint="f" * 64,
                    equity=Decimal("100000"),
                    captured_at=datetime(2026, 8, 28, 15, 0, tzinfo=UTC),
                    positions_hash="1" * 64,
                    orders_hash="2" * 64,
                    activities_hash="3" * 64,
                    contaminated=False,
                )
            )
        elif defect == "missing":
            session.delete(baseline)
        else:
            baseline.account_fingerprint = "e" * 64

    with pytest.raises(PerformanceProofIntegrityError, match="clean and current"):
        repo.publish_latest_eligible()


def test_publisher_has_no_caller_selected_snapshot_boundary_or_time() -> None:
    parameters = signature(SQLAlchemyPerformanceRepository.publish_latest_eligible).parameters

    assert tuple(parameters) == ("self",)


def test_final_boundary_key_and_time_are_bidirectional() -> None:
    with pytest.raises(ValueError, match="key and time"):
        snapshot(
            boundary_key="WRONG_FINAL_KEY",
            scheduled_for=FINAL_PERFORMANCE_BOUNDARY,
        )
    with pytest.raises(ValueError, match="key and time"):
        snapshot(boundary_key="FINAL_2026-09-04T14:30:00Z")


def test_reader_rejects_noncanonical_or_tampered_publication_text() -> None:
    repo = repository()
    repo.append_snapshot(snapshot())
    repo.publish_latest_eligible()
    with repo._sessions.begin() as session:
        row = session.scalar(select(PerformancePublicationRow))
        assert row is not None
        row.payload_text += " "

    with pytest.raises(PerformanceProofIntegrityError, match="canonical"):
        repo.latest_publication()
    repo.append_snapshot(
        snapshot(
            snapshot_id=UUID("00000000-0000-0000-0000-000000000505"),
            boundary_key="2026-08-28T17:30:00Z",
            scheduled_for=datetime(2026, 8, 28, 17, 30, tzinfo=UTC),
        )
    )
    with pytest.raises(PerformanceProofIntegrityError, match="canonical"):
        repo.publish_latest_eligible()


def test_missing_final_boundary_publishes_a_sentinel_instead_of_earlier_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository()
    repo.append_snapshot(snapshot())
    earlier = repo.publish_latest_eligible()
    monkeypatch.setattr(
        performance_repository,
        "_database_now",
        lambda _session: FINAL_PERFORMANCE_BOUNDARY,
    )

    with pytest.raises(PerformanceProofIntegrityError, match="final performance proof"):
        repo.latest_publication()
    with pytest.raises(NoEligiblePerformanceSnapshot, match="grace period"):
        repo.publish_latest_eligible()

    monkeypatch.setattr(
        performance_repository,
        "_database_now",
        lambda _session: FINAL_PUBLICATION_NOT_BEFORE,
    )

    proof = repo.publish_latest_eligible()

    assert proof.point is not None
    assert proof.point.scheduled_for == FINAL_PERFORMANCE_BOUNDARY
    assert proof.point.status == MeasurementStatus.MISSING
    assert proof.point.failure_code == PerformanceFailureCode.CAPTURE_NOT_STARTED
    assert proof.predecessor_hash == earlier.publication_hash


def test_final_boundary_publishes_the_exact_persisted_missing_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository()
    point = PerformancePoint(
        scheduled_for=FINAL_PERFORMANCE_BOUNDARY,
        attempted_at=FINAL_PERFORMANCE_BOUNDARY,
        measured_at=None,
        status=MeasurementStatus.MISSING,
        failure_code=PerformanceFailureCode.CAPTURE_NOT_STARTED,
        current_equity_usd=None,
        account_equity_change_usd=None,
        account_equity_return_pct=None,
        reconciled_lifecycle_cashflow_usd=None,
        open_position_liquidation_pnl_usd=None,
        simulator_limitations_code="ALPACA_PAPER_SIMULATION",
    )
    final = PerformanceSnapshot.create(
        snapshot_id=UUID("00000000-0000-0000-0000-000000000504"),
        submission_baseline_id=BASELINE_ID,
        boundary_key="FINAL_2026-09-04T14:30:00Z",
        point=point,
        baseline_status=BaselineStatus.CLEAN,
        account_fingerprint="f" * 64,
        positions_manifest_hash=None,
        orders_manifest_hash=None,
        activities_manifest_hash=None,
    )
    repo.append_snapshot(final)
    monkeypatch.setattr(
        performance_repository,
        "_database_now",
        lambda _session: FINAL_PERFORMANCE_BOUNDARY,
    )

    with pytest.raises(NoEligiblePerformanceSnapshot, match="grace period"):
        repo.publish_latest_eligible()
    monkeypatch.setattr(
        performance_repository,
        "_database_now",
        lambda _session: FINAL_PUBLICATION_NOT_BEFORE,
    )

    proof = repo.publish_latest_eligible()

    assert proof.point == point
    assert proof.point.status == MeasurementStatus.MISSING
    assert proof.point.current_equity_usd is None


@pytest.mark.skipif(
    "ALPHADECAY_TEST_POSTGRES_URL" not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
def test_postgres_migration_enforces_append_only_and_snapshot_bound_publications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(os.environ["ALPHADECAY_TEST_POSTGRES_URL"])
    if engine.dialect.name != "postgresql":
        pytest.fail("ALPHADECAY_TEST_POSTGRES_URL must use PostgreSQL")
    schema = f"performance_migration_{uuid4().hex}"
    scoped_engine = None
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        scoped_engine = create_engine(
            os.environ["ALPHADECAY_TEST_POSTGRES_URL"],
            connect_args={"startup_params": {"search_path": schema}},
        )
        apply_migrations(scoped_engine, discover_migrations(MIGRATIONS))
        with scoped_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO account_roles "
                    "(role, account_fingerprint, equity, autonomous_enabled) "
                    "VALUES ('SUBMISSION', :fingerprint, 100000, false)"
                ),
                {"fingerprint": "f" * 64},
            )
            connection.execute(
                text(
                    "INSERT INTO submission_baselines ("
                    "baseline_id, account_role, account_fingerprint, equity, captured_at, "
                    "positions_hash, orders_hash, activities_hash, contaminated) VALUES ("
                    ":baseline_id, 'SUBMISSION', :fingerprint, 100000, :captured_at, "
                    ":positions_hash, :orders_hash, :activities_hash, false)"
                ),
                {
                    "baseline_id": BASELINE_ID,
                    "fingerprint": "f" * 64,
                    "captured_at": datetime(2026, 8, 28, 15, 0, tzinfo=UTC),
                    "positions_hash": "1" * 64,
                    "orders_hash": "2" * 64,
                    "activities_hash": "3" * 64,
                },
            )
        postgres_repo = SQLAlchemyPerformanceRepository(
            sessionmaker(scoped_engine, expire_on_commit=False)
        )
        final_point = PerformancePoint(
            scheduled_for=FINAL_PERFORMANCE_BOUNDARY,
            attempted_at=FINAL_PERFORMANCE_BOUNDARY,
            measured_at=None,
            status=MeasurementStatus.MISSING,
            failure_code=PerformanceFailureCode.CAPTURE_NOT_STARTED,
            current_equity_usd=None,
            account_equity_change_usd=None,
            account_equity_return_pct=None,
            reconciled_lifecycle_cashflow_usd=None,
            open_position_liquidation_pnl_usd=None,
            simulator_limitations_code="ALPACA_PAPER_SIMULATION",
        )
        postgres_repo.append_snapshot(
            PerformanceSnapshot.create(
                snapshot_id=SNAPSHOT_ID,
                submission_baseline_id=BASELINE_ID,
                boundary_key="FINAL_2026-09-04T14:30:00Z",
                point=final_point,
                baseline_status=BaselineStatus.CLEAN,
                account_fingerprint="f" * 64,
                positions_manifest_hash=None,
                orders_manifest_hash=None,
                activities_manifest_hash=None,
            )
        )
        with (
            pytest.raises(DBAPIError, match="append-only"),
            engine.begin() as connection,
        ):
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(
                text(
                    "UPDATE competition_performance_snapshots "
                    "SET boundary_key = 'CHANGED' WHERE snapshot_id = :snapshot_id"
                ),
                {"snapshot_id": SNAPSHOT_ID},
            )
        with (
            pytest.raises(DBAPIError, match="payload does not match"),
            engine.begin() as connection,
        ):
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(
                text(
                    "INSERT INTO competition_performance_publications ("
                    "publication_id, snapshot_id, boundary_scheduled_for, published_at, "
                    "payload_text, projection_hash, publication_hash) VALUES ("
                    ":publication_id, :snapshot_id, :boundary, :published_at, "
                    "'{}', :projection_hash, :publication_hash)"
                ),
                {
                    "publication_id": uuid4(),
                    "snapshot_id": SNAPSHOT_ID,
                    "boundary": FINAL_PERFORMANCE_BOUNDARY,
                    "published_at": FINAL_PUBLICATION_NOT_BEFORE,
                    "projection_hash": "b" * 64,
                    "publication_hash": "c" * 64,
                },
            )
        monkeypatch.setattr(
            performance_repository,
            "_database_now",
            lambda _session: FINAL_PUBLICATION_NOT_BEFORE,
        )

        proof = postgres_repo.publish_latest_eligible()

        assert proof.point == final_point
        assert proof == postgres_repo.latest_publication()
    finally:
        if scoped_engine is not None:
            scoped_engine.dispose()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
