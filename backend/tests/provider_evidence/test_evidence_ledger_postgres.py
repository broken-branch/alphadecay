from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import (
    EvidenceClassification,
    EvidenceRelation,
    GreekExposure,
    SourceCluster,
    ThesisCreateRequest,
    ThesisResponse,
)
from backend.app.evidence.classifier import (
    EvidenceClassifier,
    EvidenceUnavailable,
    GeminiRequest,
)
from backend.app.evidence.repository import (
    EvidenceClassificationInProgress,
    EvidenceLease,
    EvidenceLedgerError,
    SQLAlchemyEvidenceLedger,
    StoredEvidenceClassifications,
)
from backend.app.persistence.runtime import apply_migrations, discover_migrations

POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"
MODEL = "gemini-3.7-flash"

pytestmark = pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)


def _cluster() -> SourceCluster:
    return SourceCluster(
        cluster_id="cluster-1",
        source_ids=("source-1",),
        headline="Issuer narrowed its outlook.",
        observed_at=datetime(2026, 8, 28, 15, 30, tzinfo=UTC),
        source_tier="PRIMARY",
    )


def _thesis() -> ThesisResponse:
    return ThesisResponse(
        thesis_id=UUID("00000000-0000-0000-0000-000000000001"),
        version=1,
        frozen=True,
        thesis_hash="thesis-hash",
        thesis=ThesisCreateRequest(
            underlying="TEST",
            thesis_code="REVENUE_OUTLOOK_IMPROVING",
            invalidation_codes=("inv-guidance",),
            intended_exposure=GreekExposure(
                delta=Decimal("0"),
                gamma=Decimal("0"),
                theta_per_day=Decimal("0"),
                vega_per_iv_point=Decimal("0"),
            ),
            source_policy_hash="source-policy-hash",
        ),
    )


def _classification() -> EvidenceClassification:
    return EvidenceClassification(
        cluster_id="cluster-1",
        source_ids=("source-1",),
        event_code="GUIDANCE",
        relation=EvidenceRelation.CONTRADICTS,
        materiality=3,
        relevance=Decimal("0.9"),
        confidence=Decimal("0.8"),
        source_tier="PRIMARY",
        invalidates=True,
        invalidation_condition_id="inv-guidance",
    )


def _response(*, valid: bool) -> str:
    return json.dumps(
        {
            "classifications": [
                {
                    "cluster_id": "cluster-1",
                    "source_ids": ["source-1" if valid else "invented"],
                    "event_code": "GUIDANCE",
                    "relation": "CONTRADICTS",
                    "materiality": 3,
                    "relevance": 0.9,
                    "confidence": 0.8,
                    "invalidation_condition_id": "inv-guidance",
                }
            ]
        }
    )


class _FixtureGemini:
    def __init__(self, outcomes: list[str | Exception]) -> None:
        self._outcomes = outcomes
        self.requests: list[GeminiRequest] = []

    def generate(self, request: GeminiRequest) -> str:
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def postgres_engine():
    database_url = os.environ[POSTGRES_URL_ENV]
    admin_engine = create_engine(database_url)
    if admin_engine.dialect.name != "postgresql":
        pytest.fail(f"{POSTGRES_URL_ENV} must use PostgreSQL")
    schema = f"model_ledger_{uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        database_url,
        connect_args={"startup_params": {"search_path": schema}},
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def _migrate(postgres_engine) -> SQLAlchemyEvidenceLedger:
    apply_migrations(postgres_engine, discover_migrations(MIGRATIONS))
    return SQLAlchemyEvidenceLedger(sessionmaker(postgres_engine, expire_on_commit=False))


def test_postgres_migration_0010_applies_after_0009_and_restarts(postgres_engine) -> None:
    migrations = discover_migrations(MIGRATIONS)
    assert [migration.version for migration in migrations] == list(range(1, 27))

    apply_migrations(postgres_engine, migrations[:9])
    with postgres_engine.connect() as connection:
        assert connection.execute(
            text("SELECT array_agg(version ORDER BY version) FROM alphadecay_schema_migrations")
        ).scalar_one() == list(range(1, 10))

    apply_migrations(postgres_engine, migrations)
    postgres_engine.dispose()
    apply_migrations(postgres_engine, migrations)
    with postgres_engine.connect() as connection:
        assert connection.execute(
            text("SELECT array_agg(version ORDER BY version) FROM alphadecay_schema_migrations")
        ).scalar_one() == list(range(1, 27))
        assert connection.execute(
            text("SELECT request_count, hard_limit FROM model_call_budgets WHERE model = :model"),
            {"model": MODEL},
        ).one() == (0, 50)


def test_postgres_concurrent_budget_boundary_and_trigger_immutability(postgres_engine) -> None:
    ledger = _migrate(postgres_engine)
    for expected in range(1, 50):
        assert ledger.reserve_model_request(MODEL) == expected
    barrier = Barrier(2)

    def reserve() -> int | str:
        barrier.wait(timeout=3)
        try:
            return ledger.reserve_model_request(MODEL)
        except EvidenceLedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _: reserve(), range(2)))

    assert sorted(outcomes, key=str) == [50, "MODEL_CALL_BUDGET_EXHAUSTED"]
    assert ledger.model_request_count(MODEL) == 50
    with (
        pytest.raises(DBAPIError, match="model call budget rows are immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE model_call_budgets SET request_count = request_count - 1 "
                "WHERE model = :model"
            ),
            {"model": MODEL},
        )
    with (
        pytest.raises(DBAPIError, match="model call budget rows are immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            text("DELETE FROM model_call_budgets WHERE model = :model"),
            {"model": MODEL},
        )


def test_postgres_same_hash_completion_order_restart_and_immutability(postgres_engine) -> None:
    ledger = _migrate(postgres_engine)
    evidence_hash = "a" * 64
    barrier = Barrier(2)

    def acquire():
        barrier.wait(timeout=3)
        return ledger.acquire(evidence_hash)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(executor.map(lambda _: acquire(), range(2)))

    leases = tuple(claim for claim in claims if isinstance(claim, EvidenceLease))
    pending = tuple(
        claim for claim in claims if isinstance(claim, EvidenceClassificationInProgress)
    )
    assert len(leases) == 1
    assert len(pending) == 1
    lease = leases[0]

    with (
        pytest.raises(DBAPIError, match="invalid evidence completion transition"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE evidence_classification_claims "
                "SET state = 'COMPLETED', lease_owner = NULL, lease_expires_at = NULL "
                "WHERE evidence_hash = :evidence_hash"
            ),
            {"evidence_hash": evidence_hash},
        )

    ledger.complete(lease, (_classification(),))
    postgres_engine.dispose()
    restarted = SQLAlchemyEvidenceLedger(sessionmaker(postgres_engine, expire_on_commit=False))
    stored = restarted.acquire(evidence_hash)
    assert isinstance(stored, StoredEvidenceClassifications)
    assert stored.classifications == (_classification(),)

    with (
        pytest.raises(DBAPIError, match="completed evidence classifications are append-only"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE evidence_classifications SET classification_hash = repeat('b', 64) "
                "WHERE evidence_hash = :evidence_hash"
            ),
            {"evidence_hash": evidence_hash},
        )
    with (
        pytest.raises(DBAPIError, match="completed evidence classifications are append-only"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            text("DELETE FROM evidence_classifications WHERE evidence_hash = :evidence_hash"),
            {"evidence_hash": evidence_hash},
        )
    with (
        pytest.raises(DBAPIError, match="completed evidence ownership is immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE evidence_classification_claims SET updated_at = clock_timestamp() "
                "WHERE evidence_hash = :evidence_hash"
            ),
            {"evidence_hash": evidence_hash},
        )
    with (
        pytest.raises(DBAPIError, match="evidence ownership rows cannot be deleted"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            text("DELETE FROM evidence_classification_claims WHERE evidence_hash = :evidence_hash"),
            {"evidence_hash": evidence_hash},
        )


def test_postgres_stale_generation_cannot_replace_newer_completion(postgres_engine) -> None:
    apply_migrations(postgres_engine, discover_migrations(MIGRATIONS))
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    ledger = SQLAlchemyEvidenceLedger(sessions, lease_ttl=timedelta(milliseconds=20))
    evidence_hash = "c" * 64
    first = ledger.acquire(evidence_hash)
    assert isinstance(first, EvidenceLease)

    deadline = time.monotonic() + 3
    while True:
        with postgres_engine.connect() as connection:
            expired = connection.execute(
                text(
                    "SELECT lease_expires_at <= clock_timestamp() "
                    "FROM evidence_classification_claims WHERE evidence_hash = :evidence_hash"
                ),
                {"evidence_hash": evidence_hash},
            ).scalar_one()
        if expired:
            break
        if time.monotonic() >= deadline:
            pytest.fail("PostgreSQL lease did not expire before the test deadline")
        time.sleep(0.002)

    takeover_ledger = SQLAlchemyEvidenceLedger(sessions)
    second = takeover_ledger.acquire(evidence_hash)
    assert isinstance(second, EvidenceLease)
    assert second.generation == first.generation + 1
    takeover_ledger.complete(second, (_classification(),))

    with pytest.raises(EvidenceLedgerError, match="MODEL_CLASSIFICATION_LEASE_LOST"):
        takeover_ledger.complete(first, (_classification().model_copy(update={"materiality": 1}),))
    stored = takeover_ledger.acquire(evidence_hash)
    assert isinstance(stored, StoredEvidenceClassifications)
    assert stored.classifications == (_classification(),)


def test_postgres_failure_releases_claim_but_retains_request_count(postgres_engine) -> None:
    ledger = _migrate(postgres_engine)
    invalid = _FixtureGemini([_response(valid=False), _response(valid=False)])
    with pytest.raises(EvidenceUnavailable, match="EVIDENCE_UNKNOWN_SOURCE_ID"):
        EvidenceClassifier(invalid, ledger=ledger).classify(_thesis(), (_cluster(),))

    assert ledger.model_request_count(MODEL) == 2
    valid = _FixtureGemini([_response(valid=True)])
    result = EvidenceClassifier(valid, ledger=ledger).classify(_thesis(), (_cluster(),))

    assert result == (_classification(),)
    assert len(valid.requests) == 1
    assert ledger.model_request_count(MODEL) == 3
