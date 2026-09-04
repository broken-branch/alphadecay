from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.experiments.windows import SQLAlchemyExperimentWindowReader
from backend.app.main import app
from backend.app.persistence.sqlalchemy_models import Base, DevelopmentOpportunityPlanRow
from backend.app.policy.opportunity import STRUCTURAL_BULLISH_PILOT_ID

BOUNDARY = datetime(2026, 9, 2, 13, 50, tzinfo=UTC)


@contextmanager
def _client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    prior = getattr(app.state, "experiment_window_reader", None)
    app.state.experiment_window_reader = SQLAlchemyExperimentWindowReader(sessions)
    try:
        with TestClient(app) as client:
            yield client, sessions
    finally:
        if prior is None:
            delattr(app.state, "experiment_window_reader")
        else:
            app.state.experiment_window_reader = prior
        engine.dispose()


def _plan() -> DevelopmentOpportunityPlanRow:
    return DevelopmentOpportunityPlanRow(
        plan_id=uuid4(),
        opportunity_key=STRUCTURAL_BULLISH_PILOT_ID,
        version=1,
        account_role="SUBMISSION",
        underlying="SPY",
        benchmark_symbol="QQQ",
        event_session=date(2026, 8, 28),
        pre_event_session=date(2026, 8, 27),
        reaction_session=date(2026, 9, 1),
        signal_session=date(2026, 9, 2),
        daily_start_session=date(2026, 8, 1),
        allowed_event_codes=["MACRO"],
        evidence_window_start=BOUNDARY - timedelta(days=1),
        evidence_window_end=BOUNDARY,
        policy_payload={
            "selected_decision_boundary": BOUNDARY.isoformat(),
            "last_entry_boundary": (BOUNDARY + timedelta(minutes=35)).isoformat(),
            "minimum_dte": 30,
            "maximum_dte": 45,
        },
        policy_hash="a" * 64,
        request_contract={},
        request_contract_hash="b" * 64,
        thesis_code=STRUCTURAL_BULLISH_PILOT_ID,
        thesis_target_contract={},
        thesis_target_hash="c" * 64,
        exposure_limit_contract={},
        exposure_limit_hash="d" * 64,
        invalidation_codes=["THESIS_BROKEN"],
        frozen_at=BOUNDARY - timedelta(days=1),
        plan_material={},
        plan_hash="e" * 64,
    )


def test_anonymous_windows_route_is_no_store_and_redacted() -> None:
    with _client() as (client, sessions):
        with sessions.begin() as session:
            session.add(_plan())
        response = client.get("/api/experiments/windows")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["schema_version"] == "v2"
    assert body["windows"] == [
        {
            "schema_version": "v2",
            "plan_version": 1,
            "protocol": {
                "schema_version": "v2",
                "name": "SPY structural bullish beta pilot",
                "summary": (
                    "Bullish direction fixed before the window; one bull call debit spread, "
                    "30–45 days to expiry, $4 wide, with defined risk."
                ),
            },
            "frozen_at": "2026-09-01T13:50:00Z",
            "decision_boundary": "2026-09-02T13:50:00Z",
            "entry_window": {
                "schema_version": "v2",
                "opens_at": "2026-09-02T13:50:00Z",
                "closes_at": "2026-09-02T14:25:00Z",
            },
            "terminal_decision": None,
            "lifecycle": None,
            "status": "ABORTED",
            "aborted_reason": "runtime never started",
            "tick_outcome_code": None,
            "tick_outcome_text": None,
            "collapsed_versions": [1],
        }
    ]
    assert "plan_id" not in response.text
    assert "policy_hash" not in response.text
    assert "opportunity_key" not in response.text


def test_windows_route_rejects_selectors_and_is_in_openapi() -> None:
    with _client() as (client, _):
        rejected = client.get("/api/experiments/windows?version=1")
        operation = client.get("/openapi.json").json()["paths"]["/api/experiments/windows"]["get"]

    assert rejected.status_code == 422
    assert rejected.headers["Cache-Control"] == "no-store"
    assert rejected.json() == {"detail": "EXPERIMENT_WINDOWS_INPUT_REJECTED"}
    assert operation["operationId"] == "anonymous_experiment_windows_read"
    assert operation["tags"] == ["Anonymous"]
