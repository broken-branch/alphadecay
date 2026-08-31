import json

from fastapi.testclient import TestClient

from backend.app.contracts.v1 import CompetitionPerformanceProofResponse, ReplayResponse
from backend.app.main import app
from backend.app.performance import EmptyPerformanceProofReader

client = TestClient(app)


def test_public_replay_route_runs_canonical_policy() -> None:
    response = client.post("/api/replays/THETA_TAKEOVER")

    assert response.status_code == 200
    replay = ReplayResponse.model_validate(response.json())
    assert replay.scenario == "THETA_TAKEOVER"
    assert replay.assessment.action == "ROLL"
    assert len(replay.assessment_hash) == 64
    assert replay.execution_enabled is False


def test_public_replay_route_exposes_stale_quote_block_without_execution() -> None:
    response = client.post("/api/replays/STALE_QUOTE")

    assert response.status_code == 200
    replay = ReplayResponse.model_validate(response.json())
    assert replay.assessment.quality == "STALE"
    assert replay.assessment.action == "NO_ACTION"
    assert replay.assessment.components is None
    assert replay.certificate.expected_after_exposure is None
    assert replay.certificate.attempts == ()
    assert replay.certificate.execution_state == "NOT_REQUESTED"
    assert replay.execution_enabled is False


def test_unknown_replay_scenario_keeps_the_stable_public_error() -> None:
    response = client.post("/api/replays/NOT_A_SCENARIO")

    assert response.status_code == 404
    assert response.json()["detail"] == "UNKNOWN_REPLAY_SCENARIO"


def test_public_proof_stays_empty_until_offline_publication() -> None:
    original = app.state.performance_proof_reader
    app.state.performance_proof_reader = EmptyPerformanceProofReader()
    try:
        response = client.get("/api/proof")
    finally:
        app.state.performance_proof_reader = original

    assert response.status_code == 200
    proof = CompetitionPerformanceProofResponse.model_validate(response.json())
    assert proof.publication_status == "NOT_PUBLISHED"
    assert proof.point is None
    assert proof.linked_certificate_ids == ()
    assert response.text == json.dumps(response.json(), sort_keys=True, separators=(",", ":"))


def test_public_proof_failure_is_not_reported_as_unpublished() -> None:
    class BrokenReader:
        def latest_publication_text(self) -> str:
            raise RuntimeError("database unavailable")

    original = app.state.performance_proof_reader
    app.state.performance_proof_reader = BrokenReader()
    try:
        response = client.get("/api/proof")
    finally:
        app.state.performance_proof_reader = original

    assert response.status_code == 503
    assert response.json() == {"detail": "PERFORMANCE_PROOF_UNAVAILABLE"}
