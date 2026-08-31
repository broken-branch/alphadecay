from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from backend.app.competition_archive import CompetitionRecord, EmptyCompetitionArchiveReader
from backend.app.competition_archive.models import (
    CompetitionRecordKind,
    ExecutionEventProjection,
    ExposureProjection,
    NoTradeProjection,
    PositionDirection,
    PositionProjection,
    PositionState,
    SpreadProjection,
    ThesisProjection,
    canonical_hash,
)
from backend.app.competition_archive.repository import _build_record
from backend.app.contracts.v1 import CompetitionRecordResponse
from backend.app.main import app

client = TestClient(app)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def test_public_competition_record_is_empty_until_a_record_is_published() -> None:
    original = app.state.competition_archive_reader
    app.state.competition_archive_reader = EmptyCompetitionArchiveReader()
    try:
        response = client.get("/api/competition-record")
    finally:
        app.state.competition_archive_reader = original

    assert response.status_code == 200
    record = CompetitionRecordResponse.model_validate(response.json())
    assert record.publication_status == "NOT_PUBLISHED"
    assert record.records == ()


def test_public_competition_record_returns_only_sanitized_public_fields() -> None:
    decided_at = datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    domain_record = CompetitionRecord(
        kind=CompetitionRecordKind.NO_TRADE,
        public_record_id=HASH_A,
        occurred_at=decided_at,
        published_at=decided_at + timedelta(minutes=1),
        payload=NoTradeProjection(
            public_record_id=HASH_A,
            decided_at=decided_at,
            observed_at=decided_at + timedelta(seconds=10),
        ).model_dump(mode="json"),
        projection_hash=HASH_B,
        publication_hash=HASH_C,
        predecessor_hash=None,
    )

    class Reader:
        def records(self) -> tuple[CompetitionRecord, ...]:
            return (domain_record,)

    original = app.state.competition_archive_reader
    app.state.competition_archive_reader = Reader()
    try:
        response = client.get("/api/competition-record")
    finally:
        app.state.competition_archive_reader = original

    assert response.status_code == 200
    result = CompetitionRecordResponse.model_validate(response.json())
    assert result.publication_status == "PUBLISHED"
    assert result.records[0].payload.record_kind == CompetitionRecordKind.NO_TRADE
    assert response.json() == {
        "schema_version": "v1",
        "publication_status": "PUBLISHED",
        "records": [
            {
                "schema_version": "v1",
                "kind": "NO_TRADE",
                "public_record_id": HASH_A,
                "occurred_at": "2026-08-31T14:30:00Z",
                "published_at": "2026-08-31T14:31:00Z",
                "payload": {
                    "schema_version": "v1",
                    "record_kind": "NO_TRADE",
                    "public_record_id": HASH_A,
                    "status": "NO_TRADE",
                    "reason_category": "STRATEGY_NOT_READY",
                    "decided_at": "2026-08-31T14:30:00Z",
                    "observed_at": "2026-08-31T14:30:10Z",
                    "paper_trading": True,
                },
                "projection_hash": HASH_B,
                "publication_hash": HASH_C,
                "predecessor_hash": None,
            }
        ],
    }
    body = response.text.lower()
    assert "account" not in body
    assert "source" not in body
    assert "fingerprint" not in body


def test_public_competition_record_serves_the_exact_hash_bound_projection() -> None:
    opened_at = datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    spread = SpreadProjection(
        structure="VERTICAL",
        underlying="ACME",
        option_type="CALL",
        expiration="2026-09-18",
        long_strike=Decimal("130.000000"),
        short_strike=Decimal("135.000000"),
        quantity=1,
    )
    projection = PositionProjection(
        public_record_id=HASH_A,
        state=PositionState.OPEN,
        underlying="ACME",
        opening_spread=spread,
        current_spread=spread,
        opened_at=opened_at,
        as_of=opened_at + timedelta(minutes=5),
        closed_at=None,
        thesis=ThesisProjection(
            direction=PositionDirection.BULLISH,
            volatility_view="LONG",
            target_at=opened_at + timedelta(days=7),
        ),
        events=(
            ExecutionEventProjection(
                action="ENTRY",
                occurred_at=opened_at,
                reason_category="POSITION_OPENED",
                cashflow_usd=Decimal("-235.000000"),
                execution_status="FILLED",
                resulting_state=PositionState.OPEN,
                spread_after=spread,
            ),
        ),
        current_exposure=ExposureProjection(
            delta=Decimal("45.000000000"),
            gamma=None,
            theta_per_day=Decimal("-5.000000000"),
            vega_per_iv_point=Decimal("4.000000000"),
        ),
        execution_status="FILLED",
    )
    domain_record = _build_record(
        projection=projection,
        occurred_at=opened_at,
        published_at=opened_at + timedelta(minutes=6),
        predecessor_hash=None,
        source_authority_hash=HASH_D,
    )

    class Reader:
        def records(self) -> tuple[CompetitionRecord, ...]:
            return (domain_record,)

    original = app.state.competition_archive_reader
    app.state.competition_archive_reader = Reader()
    try:
        response = client.get("/api/competition-record")
    finally:
        app.state.competition_archive_reader = original

    assert response.status_code == 200
    served = response.json()["records"][0]
    assert not {"published_at", "publication_hash", "predecessor_hash"} & set(served["payload"])
    assert canonical_hash(served["payload"]) == served["projection_hash"]
    assert served["payload"]["opening_spread"]["long_strike"] == "130.000000"


def test_public_competition_record_reports_database_failure_as_unavailable() -> None:
    class BrokenReader:
        def records(self) -> tuple[CompetitionRecord, ...]:
            raise RuntimeError("database unavailable")

    original = app.state.competition_archive_reader
    app.state.competition_archive_reader = BrokenReader()
    try:
        response = client.get("/api/competition-record")
    finally:
        app.state.competition_archive_reader = original

    assert response.status_code == 503
    assert response.json() == {"detail": "COMPETITION_RECORD_UNAVAILABLE"}


def test_public_competition_record_rejects_a_broken_publication_chain() -> None:
    payload = NoTradeProjection(
        public_record_id=HASH_A,
        decided_at=datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
        observed_at=datetime(2026, 8, 31, 14, 31, tzinfo=UTC),
    ).model_dump(mode="json")
    records = (
        CompetitionRecord(
            kind=CompetitionRecordKind.NO_TRADE,
            public_record_id=HASH_A,
            occurred_at=datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
            published_at=datetime(2026, 8, 31, 14, 32, tzinfo=UTC),
            payload=payload,
            projection_hash=HASH_B,
            publication_hash=HASH_C,
            predecessor_hash=HASH_D,
        ),
    )

    class Reader:
        def records(self) -> tuple[CompetitionRecord, ...]:
            return records

    original = app.state.competition_archive_reader
    app.state.competition_archive_reader = Reader()
    try:
        response = client.get("/api/competition-record")
    finally:
        app.state.competition_archive_reader = original

    assert response.status_code == 503
    assert response.json() == {"detail": "COMPETITION_RECORD_UNAVAILABLE"}
