from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.app.api.auth import OwnerSessionManager, SchedulerAuthenticator
from backend.app.execution import Actor, ExecutionBlocked
from backend.app.main import app

ORIGIN = "https://alphadecay.example"
ACCESS_CODE = "owner-access-code-fixture"
TICK_ID = UUID("00000000-0000-0000-0000-000000000901")
SCHEDULER_TOKEN = "t" * 32


class RecordingAgentRuns:
    def __init__(self) -> None:
        self.actors: list[Actor] = []

    async def run(self, actor: Actor) -> object:
        self.actors.append(actor)
        return SimpleNamespace(
            tick_id=TICK_ID,
            terminal_code="CALIBRATION_BINDING_NO_TRADE",
        )


class RecordingAutonomy:
    def __init__(self) -> None:
        self.actors: list[Actor] = []

    def enable(self, actor: Actor) -> object:
        self.actors.append(actor)
        return SimpleNamespace(
            role="DEVELOPMENT",
            server_enabled=True,
            account_enabled=True,
            effective=True,
        )

    def disable(self, actor: Actor) -> object:
        self.actors.append(actor)
        return SimpleNamespace(
            role="DEVELOPMENT",
            server_enabled=True,
            account_enabled=False,
            effective=False,
        )

    def status(self) -> object:
        return SimpleNamespace(
            role="DEVELOPMENT",
            server_enabled=True,
            account_enabled=False,
            effective=False,
        )


@pytest.fixture
def client():
    original = {
        name: getattr(app.state, name, None)
        for name in (
            "owner_session_manager",
            "scheduler_authenticator",
            "agent_run_service",
            "account_autonomy_service",
        )
    }
    runs = RecordingAgentRuns()
    autonomy = RecordingAutonomy()
    app.state.owner_session_manager = OwnerSessionManager(
        access_code=ACCESS_CODE,
        signing_secret="s" * 32,
        allowed_origin=ORIGIN,
    )
    app.state.scheduler_authenticator = SchedulerAuthenticator(SCHEDULER_TOKEN)
    app.state.agent_run_service = runs
    app.state.account_autonomy_service = autonomy
    try:
        with TestClient(app, base_url=ORIGIN) as test_client:
            yield test_client, runs, autonomy
    finally:
        for name, value in original.items():
            if value is None:
                delattr(app.state, name)
            else:
                setattr(app.state, name, value)


def owner_headers(client: TestClient) -> dict[str, str]:
    login = client.post(
        "/api/session",
        headers={"Origin": ORIGIN},
        json={"access_code": ACCESS_CODE},
    )
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": login.cookies["__Host-alphadecay_csrf"],
    }


def test_owner_run_has_no_caller_selected_input(client) -> None:
    test_client, runs, _autonomy = client
    headers = owner_headers(test_client)

    assert test_client.post("/api/owner/runs").status_code == 422
    assert test_client.post("/api/owner/runs?symbol=NVDA", headers=headers).status_code == 422
    assert test_client.post("/api/owner/runs", headers=headers, json={}).status_code == 422
    response = test_client.post("/api/owner/runs", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "v1",
        "tick_id": str(TICK_ID),
        "accepted": True,
        "code": "CALIBRATION_BINDING_NO_TRADE",
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert runs.actors == [Actor.OWNER]


def test_scheduler_tick_requires_exact_bearer_and_no_input(client) -> None:
    test_client, runs, _autonomy = client
    route = "/api/internal/scheduler/tick"

    for authorization in (None, "", "bearer " + SCHEDULER_TOKEN, "Bearer wrong"):
        headers = {"Authorization": authorization} if authorization is not None else {}
        response = test_client.post(route, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "SCHEDULER_AUTHENTICATION_FAILED"}
    headers = {"Authorization": "Bearer " + SCHEDULER_TOKEN}
    assert test_client.post(route + "?intent_id=chosen", headers=headers).status_code == 422
    assert test_client.post(route, headers=headers, json={}).status_code == 422
    response = test_client.post(route, headers=headers)

    assert response.status_code == 200
    assert response.json()["code"] == "CALIBRATION_BINDING_NO_TRADE"
    assert response.headers["Cache-Control"] == "no-store"
    assert runs.actors == [Actor.SCHEDULER]


def test_agent_routes_are_selector_free_in_openapi(client) -> None:
    test_client, _runs, _autonomy = client
    paths = test_client.get("/openapi.json").json()["paths"]
    owner = paths["/api/owner/runs"]["post"]
    scheduler = paths["/api/internal/scheduler/tick"]["post"]
    enable = paths["/api/owner/autonomy/enable"]["post"]
    disable = paths["/api/owner/autonomy/disable"]["post"]
    status = paths["/api/owner/autonomy"]["get"]

    assert "requestBody" not in owner
    assert "requestBody" not in scheduler
    assert "requestBody" not in enable
    assert "requestBody" not in disable
    assert {parameter["name"] for parameter in owner["parameters"]} == {
        "origin",
        "X-CSRF-Token",
        "__Host-alphadecay_session",
        "__Host-alphadecay_csrf",
    }
    assert {parameter["name"] for parameter in scheduler["parameters"]} == {"authorization"}
    owner_parameters = {
        "origin",
        "X-CSRF-Token",
        "__Host-alphadecay_session",
        "__Host-alphadecay_csrf",
    }
    assert {parameter["name"] for parameter in enable["parameters"]} == owner_parameters
    assert {parameter["name"] for parameter in disable["parameters"]} == owner_parameters
    assert {parameter["name"] for parameter in status["parameters"]} == owner_parameters


@pytest.mark.parametrize(
    ("operation", "account_enabled"),
    (("enable", True), ("disable", False)),
)
def test_owner_can_change_account_autonomy_without_selecting_an_order(
    client,
    operation: str,
    account_enabled: bool,
) -> None:
    test_client, _runs, autonomy = client
    route = f"/api/owner/autonomy/{operation}"
    headers = owner_headers(test_client)

    assert test_client.post(route).status_code == 422
    assert test_client.post(route + "?account=DEVELOPMENT", headers=headers).status_code == 422
    assert test_client.post(route, headers=headers, json={}).status_code == 422
    response = test_client.post(route, headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "v1",
        "role": "DEVELOPMENT",
        "server_enabled": True,
        "account_enabled": account_enabled,
        "effective": account_enabled,
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert autonomy.actors == [Actor.OWNER]


def test_owner_can_read_account_autonomy_status(client) -> None:
    test_client, _runs, _autonomy = client
    route = "/api/owner/autonomy"
    headers = owner_headers(test_client)

    assert test_client.get(route).status_code == 422
    assert test_client.get(route + "?account=DEVELOPMENT", headers=headers).status_code == 422
    response = test_client.get(route, headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "v1",
        "role": "DEVELOPMENT",
        "server_enabled": True,
        "account_enabled": False,
        "effective": False,
    }
    assert response.headers["Cache-Control"] == "no-store"


def test_enable_autonomy_reports_a_closed_server_gate(client) -> None:
    test_client, _runs, _autonomy = client
    headers = owner_headers(test_client)

    class DisabledAutonomy:
        def enable(self, _actor: Actor) -> object:
            raise ExecutionBlocked("SERVER_AUTONOMY_DISABLED")

    app.state.account_autonomy_service = DisabledAutonomy()
    response = test_client.post("/api/owner/autonomy/enable", headers=headers)

    assert response.status_code == 409
    assert response.json() == {"detail": "SERVER_AUTONOMY_DISABLED"}
    assert response.headers["Cache-Control"] == "no-store"
