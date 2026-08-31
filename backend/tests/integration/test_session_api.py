from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app.api.auth import OwnerSessionManager, SessionAuthError
from backend.app.contracts.v1 import CompetitionPerformanceProofResponse
from backend.app.main import app
from backend.app.performance import NoEligiblePerformanceSnapshot

ORIGIN = "https://alphadecay.example"
ACCESS_CODE = "owner-access-code-fixture"
SESSION_SECRET = "s" * 32
NOW = datetime(2026, 8, 28, 22, 0, tzinfo=UTC)


@pytest.fixture
def session_manager() -> OwnerSessionManager:
    return OwnerSessionManager(
        access_code=ACCESS_CODE,
        signing_secret=SESSION_SECRET,
        allowed_origin=ORIGIN,
        now=lambda: NOW,
    )


@pytest.fixture
def client(session_manager: OwnerSessionManager):
    original = getattr(app.state, "owner_session_manager", None)
    app.state.owner_session_manager = session_manager
    try:
        with TestClient(app, base_url=ORIGIN) as test_client:
            yield test_client
    finally:
        if original is None:
            del app.state.owner_session_manager
        else:
            app.state.owner_session_manager = original


def test_owner_login_sets_short_lived_secure_cookies(client: TestClient) -> None:
    response = client.post(
        "/api/session",
        headers={"Origin": ORIGIN},
        json={"access_code": ACCESS_CODE},
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert response.json()["expires_at"] == "2026-08-28T22:15:00Z"
    cookies = response.headers.get_list("set-cookie")
    assert any(
        cookie.startswith("__Host-alphadecay_session=")
        and "HttpOnly" in cookie
        and "Secure" in cookie
        and "SameSite=strict" in cookie
        and "Path=/" in cookie
        and "Max-Age=900" in cookie
        for cookie in cookies
    )
    assert any(
        cookie.startswith("__Host-alphadecay_csrf=")
        and "HttpOnly" not in cookie
        and "Secure" in cookie
        and "SameSite=strict" in cookie
        and "Path=/" in cookie
        and "Max-Age=900" in cookie
        for cookie in cookies
    )
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("origin", "access_code", "status", "code"),
    [
        ("https://other.example", ACCESS_CODE, 403, "ORIGIN_REJECTED"),
        (ORIGIN, "incorrect-access-code", 401, "AUTHENTICATION_FAILED"),
        (ORIGIN, "é" * 16, 401, "AUTHENTICATION_FAILED"),
    ],
)
def test_owner_login_fails_closed(
    client: TestClient,
    origin: str | None,
    access_code: str,
    status: int,
    code: str,
) -> None:
    headers = {"Origin": origin} if origin else {}
    response = client.post(
        "/api/session",
        headers=headers,
        json={"access_code": access_code},
    )

    assert response.status_code == status
    assert response.json() == {"detail": code}
    assert not response.headers.get_list("set-cookie")


def test_owner_login_requires_origin_header(client: TestClient) -> None:
    response = client.post(
        "/api/session",
        json={"access_code": ACCESS_CODE},
    )

    assert response.status_code == 422


def test_non_ascii_origin_is_rejected_without_server_error(client: TestClient) -> None:
    response = client.post(
        "/api/session",
        headers=[(b"origin", b"https://\xff.example")],
        json={"access_code": ACCESS_CODE},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "ORIGIN_REJECTED"}


def test_login_attempts_are_bounded(client: TestClient) -> None:
    for _ in range(5):
        response = client.post(
            "/api/session",
            headers={"Origin": ORIGIN},
            json={"access_code": "incorrect-access-code"},
        )
        assert response.status_code == 401

    response = client.post(
        "/api/session",
        headers={"Origin": ORIGIN},
        json={"access_code": ACCESS_CODE},
    )

    assert response.status_code == 429
    assert response.json() == {"detail": "AUTHENTICATION_RATE_LIMITED"}


def test_logout_requires_bound_csrf_and_exact_origin(client: TestClient) -> None:
    login = client.post(
        "/api/session",
        headers={"Origin": ORIGIN},
        json={"access_code": ACCESS_CODE},
    )
    csrf = login.cookies["__Host-alphadecay_csrf"]

    missing = client.delete("/api/session", headers={"Origin": ORIGIN})
    assert missing.status_code == 422

    wrong_origin = client.delete(
        "/api/session",
        headers={"Origin": "https://other.example", "X-CSRF-Token": csrf},
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"detail": "ORIGIN_REJECTED"}

    response = client.delete(
        "/api/session",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "v1",
        "authenticated": False,
        "expires_at": None,
    }
    assert all("Max-Age=0" in cookie for cookie in response.headers.get_list("set-cookie"))
    assert response.headers["Cache-Control"] == "no-store"
    with pytest.raises(SessionAuthError, match="SESSION_INVALID"):
        client.app.state.owner_session_manager.verify(
            login.cookies["__Host-alphadecay_session"],
            csrf,
        )


def test_non_ascii_csrf_is_rejected_without_server_error(client: TestClient) -> None:
    login = client.post(
        "/api/session",
        headers={"Origin": ORIGIN},
        json={"access_code": ACCESS_CODE},
    )
    assert login.status_code == 200

    response = client.delete(
        "/api/session",
        headers=[(b"origin", ORIGIN.encode()), (b"x-csrf-token", b"\xff")],
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "CSRF_REJECTED"}


def test_session_signature_expiry_and_csrf_binding(session_manager: OwnerSessionManager) -> None:
    token, csrf, expires_at = session_manager.create(ACCESS_CODE)
    assert expires_at == NOW + timedelta(minutes=15)
    session_manager.verify(token, csrf)

    with pytest.raises(SessionAuthError, match="SESSION_INVALID"):
        session_manager.verify(f"{token[:-1]}x", csrf)
    with pytest.raises(SessionAuthError, match="CSRF_REJECTED"):
        session_manager.verify(token, "wrong-csrf-token")

    expired_manager = OwnerSessionManager(
        access_code=ACCESS_CODE,
        signing_secret=SESSION_SECRET,
        allowed_origin=ORIGIN,
        now=lambda: NOW + timedelta(minutes=16),
    )
    with pytest.raises(SessionAuthError, match="SESSION_EXPIRED"):
        expired_manager.verify(token, csrf)


def test_new_login_revokes_the_previous_session(session_manager: OwnerSessionManager) -> None:
    first_token, first_csrf, _ = session_manager.create(ACCESS_CODE)
    second_token, second_csrf, _ = session_manager.create(ACCESS_CODE)

    with pytest.raises(SessionAuthError, match="SESSION_INVALID"):
        session_manager.verify(first_token, first_csrf)
    session_manager.verify(second_token, second_csrf)


def test_owner_proof_publication_has_no_caller_selected_input(client: TestClient) -> None:
    class Publisher:
        calls = 0

        def publish_latest_eligible(self) -> CompetitionPerformanceProofResponse:
            self.calls += 1
            return CompetitionPerformanceProofResponse(
                publication_status="NOT_PUBLISHED",
                baseline_status=None,
                published_at=None,
                point=None,
                linked_certificate_ids=(),
                publication_hash=None,
                predecessor_hash=None,
            )

    publisher = Publisher()
    original = getattr(app.state, "performance_publisher", None)
    app.state.performance_publisher = publisher
    try:
        unauthenticated = client.post("/api/owner/proof/publications")
        assert unauthenticated.status_code == 422

        login = client.post(
            "/api/session",
            headers={"Origin": ORIGIN},
            json={"access_code": ACCESS_CODE},
        )
        csrf = login.cookies["__Host-alphadecay_csrf"]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}

        with_body = client.post(
            "/api/owner/proof/publications",
            headers=headers,
            json={"boundary_key": "caller-selected"},
        )
        assert with_body.status_code == 422
        assert with_body.json() == {"detail": "REQUEST_BODY_NOT_ALLOWED"}
        assert publisher.calls == 0

        published = client.post("/api/owner/proof/publications", headers=headers)
        assert published.status_code == 200
        assert published.json()["publication_status"] == "NOT_PUBLISHED"
        assert published.headers["Cache-Control"] == "no-store"
        assert publisher.calls == 1
    finally:
        if original is None:
            del app.state.performance_publisher
        else:
            app.state.performance_publisher = original


def test_owner_proof_publication_reports_no_eligible_snapshot(client: TestClient) -> None:
    class Publisher:
        def publish_latest_eligible(self) -> CompetitionPerformanceProofResponse:
            raise NoEligiblePerformanceSnapshot("none")

    original = getattr(app.state, "performance_publisher", None)
    app.state.performance_publisher = Publisher()
    try:
        login = client.post(
            "/api/session",
            headers={"Origin": ORIGIN},
            json={"access_code": ACCESS_CODE},
        )
        response = client.post(
            "/api/owner/proof/publications",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": login.cookies["__Host-alphadecay_csrf"],
            },
        )
    finally:
        if original is None:
            del app.state.performance_publisher
        else:
            app.state.performance_publisher = original

    assert response.status_code == 409
    assert response.json() == {"detail": "NO_ELIGIBLE_PERFORMANCE_SNAPSHOT"}


def test_expired_old_token_does_not_revoke_newer_session() -> None:
    clock = [NOW]
    manager = OwnerSessionManager(
        access_code=ACCESS_CODE,
        signing_secret=SESSION_SECRET,
        allowed_origin=ORIGIN,
        now=lambda: clock[0],
    )
    first_token, first_csrf, _ = manager.create(ACCESS_CODE)
    clock[0] += timedelta(minutes=1)
    second_token, second_csrf, _ = manager.create(ACCESS_CODE)
    clock[0] = NOW + timedelta(minutes=15, seconds=1)

    with pytest.raises(SessionAuthError, match="SESSION_EXPIRED"):
        manager.verify(first_token, first_csrf)
    manager.verify(second_token, second_csrf)


def test_concurrent_failed_logins_share_one_bounded_window(
    session_manager: OwnerSessionManager,
) -> None:
    def attempt(_: int) -> str:
        try:
            session_manager.create("incorrect-access-code")
        except SessionAuthError as error:
            return str(error)
        return "UNEXPECTED_SUCCESS"

    with ThreadPoolExecutor(max_workers=10) as executor:
        outcomes = tuple(executor.map(attempt, range(10)))

    assert outcomes.count("AUTHENTICATION_FAILED") == 5
    assert outcomes.count("AUTHENTICATION_RATE_LIMITED") == 5


def test_openapi_declares_owner_inputs_and_failures(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]["/api/session"]

    assert {parameter["name"] for parameter in paths["post"]["parameters"]} == {"origin"}
    assert all(parameter["required"] is True for parameter in paths["post"]["parameters"])
    assert {"401", "403", "429", "503"} <= set(paths["post"]["responses"])
    assert {parameter["name"] for parameter in paths["delete"]["parameters"]} == {
        "origin",
        "X-CSRF-Token",
        "__Host-alphadecay_session",
        "__Host-alphadecay_csrf",
    }
    assert all(parameter["required"] is True for parameter in paths["delete"]["parameters"])
    assert {"403", "503"} <= set(paths["delete"]["responses"])
