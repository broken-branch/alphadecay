from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, delete, event, update
from sqlalchemy.exc import IntegrityError
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
    ModelTransientError,
)
from backend.app.evidence.repository import (
    EvidenceLease,
    EvidenceLedgerError,
    SQLAlchemyEvidenceLedger,
    StoredEvidenceClassifications,
)
from backend.app.persistence.sqlalchemy_models import (
    Base,
    EvidenceClassificationClaimRow,
    EvidenceClassificationRow,
    ModelCallBudgetRow,
)

MODEL = "gemini-3.7-flash"


def _cluster(*, headline: str = "Issuer narrowed its outlook.") -> SourceCluster:
    return SourceCluster(
        cluster_id="cluster-1",
        source_ids=("source-1",),
        headline=headline,
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


def _response(*, valid: bool = True) -> str:
    source_id = "source-1" if valid else "invented"
    return json.dumps(
        {
            "classifications": [
                {
                    "cluster_id": "cluster-1",
                    "source_ids": [source_id],
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


class FixtureGemini:
    def __init__(self, outcomes: list[str | Exception]) -> None:
        self.outcomes = outcomes
        self.requests: list[GeminiRequest] = []

    def generate(self, request: GeminiRequest) -> str:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _engine(path: Path):
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def _ledger(
    path: Path,
    *,
    request_count: int | None = None,
    lease_ttl: timedelta = timedelta(seconds=35),
) -> tuple[object, SQLAlchemyEvidenceLedger]:
    engine = _engine(path)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    if request_count is not None:
        with sessions.begin() as session:
            session.add(
                ModelCallBudgetRow(
                    model=MODEL,
                    request_count=request_count,
                    hard_limit=50,
                )
            )
    return engine, SQLAlchemyEvidenceLedger(sessions, lease_ttl=lease_ttl)


def test_completed_classification_is_reused_exactly_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "restart.db"
    engine, ledger = _ledger(database, request_count=0)
    first_transport = FixtureGemini([_response()])
    expected = EvidenceClassifier(first_transport, ledger=ledger).classify(_thesis(), (_cluster(),))
    engine.dispose()

    restarted_engine, restarted_ledger = _ledger(database)
    restarted_transport = FixtureGemini([])
    reused = EvidenceClassifier(restarted_transport, ledger=restarted_ledger).classify(
        _thesis(), (_cluster(),)
    )

    assert reused == expected
    assert restarted_transport.requests == []
    assert restarted_ledger.model_request_count(MODEL) == 1
    restarted_engine.dispose()


def test_sqlite_naive_lease_timestamp_completes_with_aware_repository_clock(
    tmp_path: Path,
) -> None:
    engine, ledger = _ledger(tmp_path / "sqlite-timestamp.db", request_count=0)
    lease = ledger.acquire("9" * 64)
    assert isinstance(lease, EvidenceLease)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        claim = session.get(EvidenceClassificationClaimRow, lease.evidence_hash)
        assert claim is not None
        assert claim.lease_expires_at is not None
        assert claim.lease_expires_at.tzinfo is None

    ledger.complete(lease, (_classification(),))

    stored = ledger.acquire(lease.evidence_hash)
    assert isinstance(stored, StoredEvidenceClassifications)
    assert stored.classifications == (_classification(),)
    engine.dispose()


def test_concurrent_independent_clients_have_one_provider_owner(tmp_path: Path) -> None:
    database = tmp_path / "single-flight.db"
    first_engine, first_ledger = _ledger(database, request_count=0)
    second_engine, second_ledger = _ledger(database)
    provider_started = threading.Event()
    provider_release = threading.Event()

    class BlockingGemini(FixtureGemini):
        def generate(self, request: GeminiRequest) -> str:
            self.requests.append(request)
            provider_started.set()
            assert provider_release.wait(timeout=3)
            return _response()

    owner_transport = BlockingGemini([])
    owner_classifier = EvidenceClassifier(owner_transport, ledger=first_ledger)
    other_transport = FixtureGemini([_response()])
    other_classifier = EvidenceClassifier(other_transport, ledger=second_ledger)
    owner_result: list[tuple[EvidenceClassification, ...]] = []
    owner_errors: list[Exception] = []

    def classify_as_owner() -> None:
        try:
            owner_result.append(owner_classifier.classify(_thesis(), (_cluster(),)))
        except Exception as error:
            owner_errors.append(error)

    owner = threading.Thread(target=classify_as_owner)
    owner.start()
    assert provider_started.wait(timeout=3)
    with pytest.raises(EvidenceUnavailable, match="MODEL_CLASSIFICATION_IN_PROGRESS"):
        other_classifier.classify(_thesis(), (_cluster(),))
    provider_release.set()
    owner.join(timeout=3)

    assert not owner.is_alive()
    assert owner_errors == []
    assert owner_result == [(_classification(),)]
    assert len(owner_transport.requests) == 1
    assert other_transport.requests == []
    assert first_ledger.model_request_count(MODEL) == 1
    first_engine.dispose()
    second_engine.dispose()


def test_exact_global_budget_boundary_is_49_then_50_then_blocked(tmp_path: Path) -> None:
    engine, ledger = _ledger(tmp_path / "budget.db", request_count=49)
    allowed_transport = FixtureGemini([_response()])
    EvidenceClassifier(allowed_transport, ledger=ledger).classify(_thesis(), (_cluster(),))

    blocked_transport = FixtureGemini([_response()])
    with pytest.raises(EvidenceUnavailable, match="MODEL_CALL_BUDGET_EXHAUSTED"):
        EvidenceClassifier(blocked_transport, ledger=ledger).classify(
            _thesis(), (_cluster(headline="A distinct evidence set."),)
        )

    assert len(allowed_transport.requests) == 1
    assert blocked_transport.requests == []
    assert ledger.model_request_count(MODEL) == 50
    engine.dispose()


def test_concurrent_budget_reservations_cannot_overspend_fifty(tmp_path: Path) -> None:
    database = tmp_path / "budget-race.db"
    first_engine, first_ledger = _ledger(database, request_count=49)
    second_engine, second_ledger = _ledger(database)
    barrier = threading.Barrier(2)
    results: list[int | str] = []

    def reserve(ledger: SQLAlchemyEvidenceLedger) -> None:
        barrier.wait(timeout=3)
        try:
            results.append(ledger.reserve_model_request(MODEL))
        except EvidenceLedgerError as error:
            results.append(error.code)

    first = threading.Thread(target=reserve, args=(first_ledger,))
    second = threading.Thread(target=reserve, args=(second_ledger,))
    first.start()
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    assert sorted(results, key=str) == [50, "MODEL_CALL_BUDGET_EXHAUSTED"]
    assert first_ledger.model_request_count(MODEL) == 50
    first_engine.dispose()
    second_engine.dispose()


def test_transient_retry_and_schema_repair_each_consume_budget(tmp_path: Path) -> None:
    engine, ledger = _ledger(tmp_path / "attempts.db", request_count=0)
    transport = FixtureGemini([ModelTransientError(), _response(valid=False), _response()])
    classifier = EvidenceClassifier(transport, ledger=ledger, sleeper=lambda _: None)

    assert classifier.classify(_thesis(), (_cluster(),)) == (_classification(),)
    assert len(transport.requests) == 3
    assert ledger.model_request_count(MODEL) == 3
    engine.dispose()


def test_expired_owner_is_fenced_from_newer_completion(tmp_path: Path) -> None:
    engine, ledger = _ledger(tmp_path / "fence.db", request_count=0)
    first = ledger.acquire("a" * 64)
    assert isinstance(first, EvidenceLease)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.execute(
            update(EvidenceClassificationClaimRow)
            .where(EvidenceClassificationClaimRow.evidence_hash == first.evidence_hash)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    takeover_ledger = SQLAlchemyEvidenceLedger(sessions)

    second = takeover_ledger.acquire("a" * 64)
    assert isinstance(second, EvidenceLease)
    assert second.generation > first.generation
    takeover_ledger.complete(second, (_classification(),))

    with pytest.raises(EvidenceLedgerError, match="MODEL_CLASSIFICATION_LEASE_LOST"):
        takeover_ledger.complete(first, (_classification().model_copy(update={"materiality": 1}),))

    stored = takeover_ledger.acquire("a" * 64)
    assert isinstance(stored, StoredEvidenceClassifications)
    assert stored.classifications == (_classification(),)
    engine.dispose()


def test_failed_completion_releases_ownership_without_caching(tmp_path: Path) -> None:
    engine, ledger = _ledger(tmp_path / "failed.db", request_count=0)
    failed_transport = FixtureGemini([_response(valid=False), _response(valid=False)])
    with pytest.raises(EvidenceUnavailable, match="EVIDENCE_UNKNOWN_SOURCE_ID"):
        EvidenceClassifier(failed_transport, ledger=ledger).classify(_thesis(), (_cluster(),))

    retry_transport = FixtureGemini([_response()])
    result = EvidenceClassifier(retry_transport, ledger=ledger).classify(_thesis(), (_cluster(),))

    assert result == (_classification(),)
    assert len(retry_transport.requests) == 1
    assert ledger.model_request_count(MODEL) == 3
    engine.dispose()


def test_payload_is_hash_validated_and_database_completion_is_immutable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "integrity.db"
    engine, ledger = _ledger(database, request_count=0)
    lease = ledger.acquire("b" * 64)
    assert isinstance(lease, EvidenceLease)
    ledger.complete(lease, (_classification(),))

    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        stored = session.get(EvidenceClassificationRow, "b" * 64)
        assert stored is not None
        serialized = json.dumps(stored.classifications_payload)
        assert "headline" not in serialized
        assert "http://" not in serialized
        assert "https://" not in serialized
    with sessions.begin() as session:
        session.execute(
            update(EvidenceClassificationRow)
            .where(EvidenceClassificationRow.evidence_hash == "b" * 64)
            .values(classification_hash="c" * 64)
        )

    with pytest.raises(EvidenceLedgerError, match="MODEL_CLASSIFICATION_INTEGRITY_ERROR"):
        ledger.acquire("b" * 64)

    migration = (Path(__file__).parents[3] / "migrations/0010_model_call_ledger.sql").read_text()
    assert "evidence_classifications_append_only" in migration
    assert "model call budget rows are immutable except for reservations" in migration
    engine.dispose()


def test_arbitrary_url_identifier_is_rejected_before_transport_or_storage(tmp_path: Path) -> None:
    engine, ledger = _ledger(tmp_path / "url.db", request_count=0)
    transport = FixtureGemini([_response()])
    unsafe = _cluster().model_copy(update={"source_ids": ("https://example.invalid/raw",)})

    with pytest.raises(EvidenceUnavailable, match="EVIDENCE_IDENTIFIER_INVALID"):
        EvidenceClassifier(transport, ledger=ledger).classify(_thesis(), (unsafe,))

    assert transport.requests == []
    assert ledger.model_request_count(MODEL) == 0
    engine.dispose()


def test_sqlite_foreign_keys_reject_classification_without_claim(tmp_path: Path) -> None:
    engine, _ledger_instance = _ledger(tmp_path / "foreign-key.db", request_count=0)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            EvidenceClassificationRow(
                evidence_hash="d" * 64,
                classifications_payload=[],
                classification_hash="e" * 64,
                completed_generation=1,
                completed_at=datetime.now(UTC),
            )
        )

    with sessions.begin() as session:
        session.add_all(
            [
                EvidenceClassificationClaimRow(
                    evidence_hash="f" * 64,
                    state="COMPLETED",
                    generation=1,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=datetime.now(UTC),
                ),
                EvidenceClassificationRow(
                    evidence_hash="f" * 64,
                    classifications_payload=[],
                    classification_hash="0" * 64,
                    completed_generation=1,
                    completed_at=datetime.now(UTC),
                ),
            ]
        )
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.execute(
            delete(EvidenceClassificationClaimRow).where(
                EvidenceClassificationClaimRow.evidence_hash == "f" * 64
            )
        )
    engine.dispose()


def test_empty_evidence_is_restart_deterministic_without_ledger_or_model_call(
    tmp_path: Path,
) -> None:
    engine, ledger = _ledger(tmp_path / "empty.db", request_count=0)
    transport = FixtureGemini([])

    assert EvidenceClassifier(transport, ledger=ledger).classify(_thesis(), ()) == ()
    assert EvidenceClassifier(transport, ledger=ledger).classify(_thesis(), ()) == ()
    assert transport.requests == []
    assert ledger.model_request_count(MODEL) == 0
    engine.dispose()
