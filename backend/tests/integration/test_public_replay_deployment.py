from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)


def test_replay_only_health_identifies_the_deployed_build(monkeypatch) -> None:
    monkeypatch.setenv("APP_RUNTIME_CONFIG_REQUIRED", "false")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a" * 40)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "v1",
        "status": "ok",
        "build": "a" * 40,
        "runtime_mode": "REPLAY_ONLY",
    }


def test_replay_only_publication_routes_return_clean_empty_states(monkeypatch) -> None:
    monkeypatch.setenv("APP_RUNTIME_CONFIG_REQUIRED", "false")

    proof = client.get("/api/proof")
    record = client.get("/api/competition-record")

    assert proof.status_code == 200
    assert proof.json()["publication_status"] == "NOT_PUBLISHED"
    assert record.status_code == 200
    assert record.json() == {
        "schema_version": "v1",
        "publication_status": "NOT_PUBLISHED",
        "records": [],
    }


def test_replay_only_openapi_lists_only_anonymous_operations(monkeypatch) -> None:
    monkeypatch.setenv("APP_RUNTIME_CONFIG_REQUIRED", "false")

    schema = client.get("/openapi.json").json()
    operations = [
        operation
        for path_item in schema["paths"].values()
        for operation in path_item.values()
    ]

    assert set(schema["paths"]) == {
        "/api/competition-record",
        "/api/health",
        "/api/proof",
        "/api/replays/{scenario}",
    }
    assert all(operation["tags"] == ["Anonymous"] for operation in operations)
    assert "/api/owner/runs" not in schema["paths"]
    assert "/api/internal/scheduler/tick" not in schema["paths"]
    assert "OwnerModelProvider" not in schema["components"]["schemas"]
    assert "SessionCreateRequest" not in schema["components"]["schemas"]


def test_public_responses_have_baseline_browser_headers(monkeypatch) -> None:
    monkeypatch.setenv("APP_RUNTIME_CONFIG_REQUIRED", "false")

    response = client.get("/api/health")

    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]


def test_swagger_policy_keeps_its_required_inline_bootstrap(monkeypatch) -> None:
    monkeypatch.setenv("APP_RUNTIME_CONFIG_REQUIRED", "false")

    response = client.get("/docs")

    assert response.status_code == 200
    assert "'unsafe-inline'" in response.headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
