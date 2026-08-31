from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.api.auth import OwnerSessionManager
from backend.app.contracts.v1 import (
    OwnerModelProvider,
    ProviderSettingsResponse,
    ProviderSettingsUpdateRequest,
)
from backend.app.main import app
from backend.app.provider_settings import ProviderSettingsRepositoryError

ORIGIN = "https://alphadecay.example"
ACCESS_CODE = "owner-access-code-fixture"
ROUTE = "/api/owner/provider-settings"


class RecordingProviderSettings:
    def __init__(self) -> None:
        self.current = ProviderSettingsResponse(configured=False)
        self.requests: list[ProviderSettingsUpdateRequest] = []
        self.error: Exception | None = None

    def status(self) -> ProviderSettingsResponse:
        self._raise_if_configured()
        return self.current

    def replace(self, request: ProviderSettingsUpdateRequest) -> ProviderSettingsResponse:
        self._raise_if_configured()
        self.requests.append(request)
        self.current = ProviderSettingsResponse(
            configured=True,
            provider=request.provider,
            endpoint=(
                "https://generativelanguage.googleapis.com"
                if request.provider is OwnerModelProvider.GEMINI
                else request.endpoint
            ),
            model=request.model,
            generation=1,
        )
        return self.current

    def clear(self) -> ProviderSettingsResponse:
        self._raise_if_configured()
        self.current = ProviderSettingsResponse(configured=False)
        return self.current

    def _raise_if_configured(self) -> None:
        if self.error is not None:
            raise self.error


@pytest.fixture
def client() -> Iterator[tuple[TestClient, RecordingProviderSettings]]:
    fields = ("owner_session_manager", "owner_provider_settings_service")
    original = {name: getattr(app.state, name, None) for name in fields}
    service = RecordingProviderSettings()
    app.state.owner_session_manager = OwnerSessionManager(
        access_code=ACCESS_CODE,
        signing_secret="s" * 32,
        allowed_origin=ORIGIN,
    )
    app.state.owner_provider_settings_service = service
    try:
        with TestClient(app, base_url=ORIGIN) as test_client:
            yield test_client, service
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


def test_owner_can_replace_read_and_clear_provider_without_key_disclosure(client) -> None:
    test_client, service = client
    headers = owner_headers(test_client)
    secret = "owner-provider-secret-value"

    replaced = test_client.put(
        ROUTE,
        headers=headers,
        json={
            "provider": "GEMINI",
            "model": "gemini-3.7-flash",
            "api_key": secret,
        },
    )

    assert replaced.status_code == 200
    assert replaced.headers["Cache-Control"] == "no-store"
    assert secret not in replaced.text
    assert replaced.json() == {
        "schema_version": "v1",
        "configured": True,
        "provider": "GEMINI",
        "endpoint": "https://generativelanguage.googleapis.com",
        "model": "gemini-3.7-flash",
        "generation": 1,
    }
    assert service.requests[0].api_key.get_secret_value() == secret
    assert secret not in repr(service.requests[0])

    status = test_client.get(ROUTE, headers=headers)
    assert status.status_code == 200
    assert status.json() == replaced.json()
    assert status.headers["Cache-Control"] == "no-store"

    browser_status = test_client.get(
        ROUTE,
        headers={
            "Referer": f"{ORIGIN}/replay",
            "X-CSRF-Token": headers["X-CSRF-Token"],
        },
    )
    assert browser_status.status_code == 200
    assert browser_status.json() == replaced.json()

    cleared = test_client.delete(ROUTE, headers=headers)
    assert cleared.status_code == 200
    assert cleared.json() == {
        "schema_version": "v1",
        "configured": False,
        "provider": None,
        "endpoint": None,
        "model": None,
        "generation": None,
    }
    assert cleared.headers["Cache-Control"] == "no-store"


def test_provider_settings_routes_require_owner_session_and_reject_extra_input(client) -> None:
    test_client, service = client
    headers = owner_headers(test_client)

    missing_auth = test_client.get(ROUTE)
    assert missing_auth.status_code == 422
    assert missing_auth.headers["Cache-Control"] == "no-store"
    assert test_client.put(ROUTE, json={}).status_code == 422
    assert test_client.delete(ROUTE).status_code == 422
    extra_query = test_client.get(ROUTE + "?provider=GEMINI", headers=headers)
    assert extra_query.status_code == 422
    assert extra_query.headers["Cache-Control"] == "no-store"
    assert test_client.request("DELETE", ROUTE, headers=headers, json={}).status_code == 422
    invalid = test_client.put(
        ROUTE,
        headers=headers,
        json={
            "provider": "GEMINI",
            "model": "gemini-3.7-flash",
            "api_key": "must-not-be-echoed",
            "endpoint": "https://example.com/v1",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "PROVIDER_SETTINGS_INPUT_REJECTED"}
    assert "must-not-be-echoed" not in invalid.text
    assert service.requests == []


@pytest.mark.parametrize(
    "referer",
    [
        None,
        "https://other.example/replay",
        "https://alphadecay.example.evil.invalid/replay",
        "https://alphadecay.example@evil.invalid/replay",
        "http://alphadecay.example/replay",
        "not-a-url",
        "https://[invalid/replay",
    ],
)
def test_provider_settings_read_rejects_missing_or_cross_origin_referer(
    client, referer: str | None
) -> None:
    test_client, _service = client
    headers = owner_headers(test_client)
    request_headers = {"X-CSRF-Token": headers["X-CSRF-Token"]}
    if referer is not None:
        request_headers["Referer"] = referer

    response = test_client.get(ROUTE, headers=request_headers)

    assert response.status_code in {403, 422}
    assert response.headers["Cache-Control"] == "no-store"


def test_provider_settings_read_does_not_use_referer_to_relax_explicit_origin(client) -> None:
    test_client, _service = client
    headers = owner_headers(test_client)

    response = test_client.get(
        ROUTE,
        headers={
            "Origin": "https://other.example",
            "Referer": f"{ORIGIN}/replay",
            "X-CSRF-Token": headers["X-CSRF-Token"],
        },
    )

    assert response.status_code == 403


def test_provider_settings_repository_failures_are_generic(client) -> None:
    test_client, service = client
    headers = owner_headers(test_client)
    service.error = ProviderSettingsRepositoryError("internal-storage-detail")

    for method in (test_client.get, test_client.delete):
        response = method(ROUTE, headers=headers)
        assert response.status_code == 503
        assert response.json() == {"detail": "PROVIDER_SETTINGS_UNAVAILABLE"}
        assert "internal-storage-detail" not in response.text
        assert response.headers["Cache-Control"] == "no-store"

    service.error = RuntimeError("unexpected-storage-detail")
    unexpected = test_client.put(
        ROUTE,
        headers=headers,
        json={
            "provider": "GEMINI",
            "model": "model",
            "api_key": "write-only-key",
        },
    )
    assert unexpected.status_code == 503
    assert unexpected.json() == {"detail": "PROVIDER_SETTINGS_UNAVAILABLE"}
    assert "unexpected-storage-detail" not in unexpected.text
    assert "write-only-key" not in unexpected.text
    assert unexpected.headers["Cache-Control"] == "no-store"


def test_provider_settings_response_rejects_partial_unconfigured_metadata() -> None:
    with pytest.raises(ValidationError):
        ProviderSettingsResponse(
            configured=False,
            provider=OwnerModelProvider.GEMINI,
            endpoint="https://generativelanguage.googleapis.com/v1beta",
            model="model",
        )


def test_provider_settings_openapi_declares_owner_boundary(client) -> None:
    test_client, _service = client
    operations = test_client.get("/openapi.json").json()["paths"][ROUTE]
    owner_inputs = {
        "origin",
        "X-CSRF-Token",
        "__Host-alphadecay_session",
        "__Host-alphadecay_csrf",
    }

    assert "requestBody" in operations["put"]
    assert "requestBody" not in operations["get"]
    assert "requestBody" not in operations["delete"]
    assert {parameter["name"] for parameter in operations["get"]["parameters"]} == (
        owner_inputs | {"referer"}
    )
    for method in ("put", "delete"):
        operation = operations[method]
        assert {parameter["name"] for parameter in operation["parameters"]} == owner_inputs
        assert {"403", "422", "503"} <= set(operation["responses"])
    assert {"403", "422", "503"} <= set(operations["get"]["responses"])
