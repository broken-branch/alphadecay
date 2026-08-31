import json
from pathlib import Path

from backend.app.main import app


def test_checked_in_openapi_matches_application() -> None:
    checked_in = json.loads(Path("contracts/openapi-v1.json").read_text())
    assert app.openapi() == checked_in


def test_anonymous_public_routes_are_named_and_replay_scenarios_are_closed() -> None:
    document = app.openapi()
    expected_operations = {
        ("/api/health", "get"): "anonymous_health",
        ("/api/replays/{scenario}", "post"): "anonymous_replay",
        ("/api/proof", "get"): "anonymous_competition_proof",
        ("/api/competition-record", "get"): "anonymous_competition_record",
    }

    for (path, method), operation_id in expected_operations.items():
        operation = document["paths"][path][method]
        assert operation["operationId"] == operation_id
        assert operation["tags"] == ["Anonymous"]
        assert any(
            phrase in operation["description"]
            for phrase in ("place an order", "contact Alpaca", "calls Alpaca")
        )
        assert "Anonymous-safe" not in operation["description"]

    replay_parameter = document["paths"]["/api/replays/{scenario}"]["post"]["parameters"][0]
    scenario_schema = replay_parameter["schema"]
    assert scenario_schema["$ref"] == "#/components/schemas/ReplayScenario"
    assert document["components"]["schemas"]["ReplayScenario"]["enum"] == [
        "THESIS_INTACT",
        "THETA_TAKEOVER",
        "CATALYST_BROKEN",
        "STALE_QUOTE",
    ]
    assert document["paths"]["/api/replays/{scenario}"]["post"]["responses"]["404"] == {
        "description": "UNKNOWN_REPLAY_SCENARIO"
    }

    assert "Anonymous" not in document["paths"]["/api/owner/runs"]["post"].get("tags", [])


def test_every_operation_has_stable_public_copy_and_grouping() -> None:
    document = app.openapi()
    assert document["info"]["title"] == "alphadecay"
    assert [tag["name"] for tag in document["tags"]] == ["Anonymous", "Owner", "Internal"]

    expected_operations = {
        ("/api/session", "post"): ("owner_session_create", "Owner"),
        ("/api/session", "delete"): ("owner_session_delete", "Owner"),
        ("/api/owner/proof/publications", "post"): (
            "owner_publish_competition_proof",
            "Owner",
        ),
        ("/api/owner/runs", "post"): ("owner_agent_tick", "Owner"),
        ("/api/owner/autonomy", "get"): ("owner_autonomy_status", "Owner"),
        ("/api/owner/autonomy/enable", "post"): ("owner_autonomy_enable", "Owner"),
        ("/api/owner/autonomy/disable", "post"): ("owner_autonomy_disable", "Owner"),
        ("/api/owner/provider-settings", "get"): (
            "owner_provider_settings_status",
            "Owner",
        ),
        ("/api/owner/provider-settings", "put"): (
            "owner_provider_settings_replace",
            "Owner",
        ),
        ("/api/owner/provider-settings", "delete"): (
            "owner_provider_settings_clear",
            "Owner",
        ),
        ("/api/internal/scheduler/tick", "post"): ("internal_scheduler_tick", "Internal"),
    }

    for (path, method), (operation_id, tag) in expected_operations.items():
        operation = document["paths"][path][method]
        assert operation["operationId"] == operation_id
        assert operation["tags"] == [tag]
        assert operation["summary"]
        assert operation["description"]

    owner_run_parameters = document["paths"]["/api/owner/runs"]["post"]["parameters"]
    cookie_titles = {
        parameter["name"]: parameter["schema"]["title"]
        for parameter in owner_run_parameters
        if parameter["in"] == "cookie"
    }
    assert cookie_titles == {
        "__Host-alphadecay_csrf": "CSRF cookie",
        "__Host-alphadecay_session": "Owner session",
    }


def test_competition_record_openapi_is_read_only_and_sanitized() -> None:
    document = app.openapi()
    route = document["paths"]["/api/competition-record"]
    assert set(route) == {"get"}
    response_ref = route["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_ref == {"$ref": "#/components/schemas/CompetitionRecordResponse"}

    item_properties = document["components"]["schemas"]["CompetitionRecordItem"]["properties"]
    assert set(item_properties) == {
        "schema_version",
        "kind",
        "public_record_id",
        "occurred_at",
        "published_at",
        "payload",
        "projection_hash",
        "publication_hash",
        "predecessor_hash",
    }
    assert not {"account_id", "account_fingerprint", "source_id"} & set(item_properties)
