from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.auth import OwnerSessionManager
from backend.app.experiments.performance_reader import SQLAlchemyExperimentPerformanceReader
from backend.app.experiments.repository import SQLAlchemyExperimentRegistry
from backend.app.main import app
from backend.app.persistence.sqlalchemy_models import Base
from backend.tests.experiments.fixtures import (
    authorization_request,
    compile_request,
    reviewed_request,
)

ORIGIN = "https://alphadecay.example"


class _Clock:
    @staticmethod
    def now() -> datetime:
        return datetime(2026, 9, 1, 18, tzinfo=UTC)


class _Persistence:
    database_clock = _Clock()


@contextmanager
def _client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    prior = {
        name: getattr(app.state, name, None)
        for name in (
            "owner_session_manager",
            "experiment_registry",
            "experiment_performance_reader",
            "persistence",
        )
    }
    app.state.owner_session_manager = OwnerSessionManager(
        access_code="owner-access-code-fixture",
        signing_secret="dummy-signing-secret-placeholder-01",
        allowed_origin=ORIGIN,
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    app.state.experiment_registry = SQLAlchemyExperimentRegistry(sessions)
    app.state.experiment_performance_reader = SQLAlchemyExperimentPerformanceReader(sessions)
    app.state.persistence = _Persistence()
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            yield client
    finally:
        for name, value in prior.items():
            if value is None:
                delattr(app.state, name)
            else:
                setattr(app.state, name, value)
        engine.dispose()


def _payload():
    return reviewed_request().model_dump(mode="json")


def _headers(client: TestClient):
    login = client.post(
        "/api/session",
        headers={"Origin": ORIGIN},
        json={"access_code": "owner-access-code-fixture"},
    )
    return {"Origin": ORIGIN, "X-CSRF-Token": login.cookies["__Host-alphadecay_csrf"]}


def _create_compiled(client: TestClient, headers: dict[str, str]):
    source = client.post("/api/owner/experiments", headers=headers, json=_payload()).json()
    compiled = client.post(
        f"/api/owner/experiments/{source['experiment_id']}/compile",
        headers=headers,
        json=compile_request(source["definition_hash"]).model_dump(mode="json"),
    ).json()
    return source, compiled


def test_owner_creates_lists_and_reads_reviewed_definition_without_execution_authority() -> None:
    with _client() as client:
        headers = _headers(client)
        created = client.post("/api/owner/experiments", headers=headers, json=_payload())
        listed = client.get("/api/owner/experiments", headers=headers)
        read = client.get(
            f"/api/owner/experiments/{created.json()['experiment_id']}", headers=headers
        )

    assert created.status_code == 201
    assert created.headers["Cache-Control"] == "no-store"
    assert listed.json()["experiments"] == [created.json()]
    assert read.json() == created.json()
    assert created.json()["lifecycle_state"] == "REVIEWED"
    assert created.json()["automation_state"] == "OFF"
    assert created.json()["execution_eligible"] is False
    assert created.json()["curation"]["classifications"]["structure"] == ("BULL_CALL_DEBIT_SPREAD")


def test_owner_compiles_and_reads_one_exact_never_armed_version() -> None:
    with _client() as client:
        headers = _headers(client)
        source = client.post("/api/owner/experiments", headers=headers, json=_payload())
        experiment_id = source.json()["experiment_id"]
        payload = compile_request(source.json()["definition_hash"]).model_dump(mode="json")
        compiled = client.post(
            f"/api/owner/experiments/{experiment_id}/compile",
            headers=headers,
            json=payload,
        )
        read = client.get(
            f"/api/owner/experiments/{experiment_id}/compiled",
            headers=headers,
        )

    assert compiled.status_code == 201
    assert compiled.headers["Cache-Control"] == "no-store"
    assert read.json() == compiled.json()
    assert compiled.json()["lifecycle_state"] == "COMPILED"
    assert compiled.json()["arm_state"] == "NOT_ARMED"
    assert compiled.json()["automation_state"] == "OFF"
    assert compiled.json()["execution_eligible"] is False
    assert compiled.json()["protocol_hash"] == compiled.json()["compiled_protocol"]["protocol_hash"]


def test_owner_can_read_no_trade_performance_while_public_route_stays_hidden() -> None:
    with _client() as client:
        headers = _headers(client)
        source, _ = _create_compiled(client, headers)
        experiment_id = source["experiment_id"]
        owner = client.get(
            f"/api/owner/experiments/{experiment_id}/performance",
            headers=headers,
        )
        anonymous = client.get(f"/api/experiments/{experiment_id}/performance")

    assert owner.status_code == 200
    assert owner.headers["Cache-Control"] == "no-store"
    assert owner.json()["terminal_state"] == "NO_POSITION"
    assert owner.json()["opened_trade_count"] == 0
    assert owner.json()["entry_cash_flow"]["value"] is None
    assert owner.json()["entry_cash_flow"]["unavailable_reason"] == "NO_OPENED_TRADES"
    assert anonymous.status_code == 404
    assert anonymous.json() == {"detail": "EXPERIMENT_PERFORMANCE_NOT_PUBLISHED"}


def test_registry_routes_require_owner_auth_and_redact_invalid_thesis() -> None:
    with _client() as client:
        missing = client.post("/api/owner/experiments", json=_payload())
        headers = _headers(client)
        invalid = client.post(
            "/api/owner/experiments",
            headers=headers,
            json={"original_thesis": {"source": {"kind": "PASTED_TEXT", "content": "secret"}}},
        )
        old_payload = reviewed_request().model_dump(mode="json")
        old_payload.pop("curation")
        incomplete = client.post(
            "/api/owner/experiments",
            headers=headers,
            json=old_payload,
        )

    assert missing.status_code in {401, 403, 422}
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "EXPERIMENT_DEFINITION_INPUT_REJECTED"}
    assert "secret" not in invalid.text
    assert incomplete.status_code == 422
    assert incomplete.json() == {"detail": "EXPERIMENT_DEFINITION_INPUT_REJECTED"}


def test_compile_rejects_wrong_source_hash_and_redacts_invalid_rules() -> None:
    with _client() as client:
        headers = _headers(client)
        source = client.post("/api/owner/experiments", headers=headers, json=_payload())
        experiment_id = source.json()["experiment_id"]
        valid = compile_request(source.json()["definition_hash"]).model_dump(mode="json")
        wrong_hash = client.post(
            f"/api/owner/experiments/{experiment_id}/compile",
            headers=headers,
            json={**valid, "source_definition_hash": "f" * 64},
        )
        invalid = client.post(
            f"/api/owner/experiments/{experiment_id}/compile",
            headers=headers,
            json={"source_definition_hash": source.json()["definition_hash"], "secret": "x"},
        )

    assert wrong_hash.status_code == 409
    assert wrong_hash.json() == {"detail": "EXPERIMENT_SOURCE_HASH_MISMATCH"}
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "EXPERIMENT_COMPILE_INPUT_REJECTED"}
    assert "secret" not in invalid.text


def test_owner_arms_reads_and_disarms_exact_protocol_without_runtime_readiness() -> None:
    with _client() as client:
        headers = _headers(client)
        source, compiled = _create_compiled(client, headers)
        path = f"/api/owner/experiments/{source['experiment_id']}"
        initial = client.get(f"{path}/authorization", headers=headers)
        arm_payload = authorization_request(
            source["definition_hash"],
            compiled["protocol_hash"],
            0,
        ).model_dump(mode="json")
        armed = client.post(f"{path}/arm", headers=headers, json=arm_payload)
        repeated = client.post(f"{path}/arm", headers=headers, json=arm_payload)
        disarmed = client.post(
            f"{path}/disarm",
            headers=headers,
            json=authorization_request(
                source["definition_hash"],
                compiled["protocol_hash"],
                1,
            ).model_dump(mode="json"),
        )

    assert initial.status_code == 200
    assert initial.json()["authorization_state"] == "NOT_ARMED"
    assert armed.status_code == 200
    assert armed.headers["Cache-Control"] == "no-store"
    assert repeated.json() == armed.json()
    assert armed.json()["authorization_state"] == "ARMED"
    assert armed.json()["entry_authorized"] is True
    assert armed.json()["runtime_state"] == "NOT_CONNECTED"
    assert armed.json()["execution_eligible"] is False
    assert armed.json()["paper_trading_only"] is True
    assert disarmed.json()["authorization_state"] == "DISARMED"
    assert disarmed.json()["entry_authorized"] is False
    assert disarmed.json()["existing_position_risk_management_preserved"] is True


def test_arm_requires_owner_csrf_and_redacts_invalid_or_mismatched_input() -> None:
    with _client() as client:
        headers = _headers(client)
        source, compiled = _create_compiled(client, headers)
        path = f"/api/owner/experiments/{source['experiment_id']}/arm"
        payload = authorization_request(
            source["definition_hash"],
            compiled["protocol_hash"],
            0,
        ).model_dump(mode="json")
        missing_csrf = client.post(path, json=payload)
        mismatch = client.post(
            path,
            headers=headers,
            json={**payload, "protocol_hash": "f" * 64},
        )
        invalid = client.post(
            path,
            headers=headers,
            json={"secret": "dummy-private-text"},
        )

    assert missing_csrf.status_code in {401, 403, 422}
    assert mismatch.status_code == 409
    assert mismatch.headers["Cache-Control"] == "no-store"
    assert mismatch.json() == {"detail": "EXPERIMENT_AUTHORIZATION_HASH_MISMATCH"}
    assert invalid.status_code == 422
    assert invalid.headers["Cache-Control"] == "no-store"
    assert invalid.json() == {"detail": "EXPERIMENT_AUTHORIZATION_INPUT_REJECTED"}
    assert "private thesis text" not in invalid.text


def test_registry_openapi_separates_compile_and_experiment_authorization() -> None:
    paths = app.openapi()["paths"]

    assert set(paths["/api/owner/experiments"]) == {"get", "post"}
    assert set(paths["/api/owner/experiments/{experiment_id}"]) == {"get"}
    assert set(paths["/api/owner/experiments/{experiment_id}/compile"]) == {"post"}
    assert set(paths["/api/owner/experiments/{experiment_id}/compiled"]) == {"get"}
    assert set(paths["/api/owner/experiments/{experiment_id}/arm"]) == {"post"}
    assert set(paths["/api/owner/experiments/{experiment_id}/disarm"]) == {"post"}
    assert set(paths["/api/owner/experiments/{experiment_id}/authorization"]) == {"get"}
    assert set(paths["/api/owner/experiments/{experiment_id}/performance"]) == {"get"}
    assert set(paths["/api/experiments/{experiment_id}/performance"]) == {"get"}
    assert paths["/api/owner/experiments"]["post"]["operationId"] == "owner_experiment_create"
    schema = app.openapi()["components"]["schemas"]["ReviewedExperimentDefinition"]
    properties = schema["properties"]
    assert properties["lifecycle_state"]["const"] == "REVIEWED"
    assert properties["automation_state"]["const"] == "OFF"
    assert properties["execution_eligible"]["const"] is False
    compiled = app.openapi()["components"]["schemas"]["CompiledExperimentVersion"]
    compiled_properties = compiled["properties"]
    assert compiled_properties["lifecycle_state"]["const"] == "COMPILED"
    assert compiled_properties["arm_state"]["const"] == "NOT_ARMED"
    assert compiled_properties["automation_state"]["const"] == "OFF"
    assert compiled_properties["execution_eligible"]["const"] is False
    authorization = app.openapi()["components"]["schemas"]["ExperimentAuthorizationStatus"][
        "properties"
    ]
    assert authorization["runtime_state"]["const"] == "NOT_CONNECTED"
    assert authorization["execution_eligible"]["const"] is False
    assert authorization["paper_trading_only"]["const"] is True
    arm_description = paths["/api/owner/experiments/{experiment_id}/arm"]["post"]["description"]
    disarm_description = paths["/api/owner/experiments/{experiment_id}/disarm"]["post"][
        "description"
    ]
    assert "authorization only" in arm_description
    assert "does not schedule work" in arm_description
    assert "risk-reducing management" in disarm_description
