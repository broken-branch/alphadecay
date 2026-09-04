from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from backend.app.api.auth import OwnerSessionManager
from backend.app.main import app
from backend.app.strategy_briefs import (
    StrategyCurationResponse,
    StrategyCurationUnavailable,
)

ORIGIN = "https://alphadecay.example"
ACCESS_CODE = "owner-access-code-fixture"


@contextmanager
def owner_client() -> Iterator[TestClient]:
    original = getattr(app.state, "owner_session_manager", None)
    app.state.owner_session_manager = OwnerSessionManager(
        access_code=ACCESS_CODE,
        signing_secret="session-signing-secret-fixture-value",
        allowed_origin=ORIGIN,
    )
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            yield client
    finally:
        if original is None:
            delattr(app.state, "owner_session_manager")
        else:
            app.state.owner_session_manager = original


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


def complete_brief() -> dict[str, object]:
    return {
        "source": {
            "kind": "PASTED_TEXT",
            "content": (
                "I think SPY can rise over the next month because earnings breadth is improving."
            ),
        },
        "market_scope": "SPY",
        "direction": "BULLISH",
        "horizon": "Two to six weeks",
        "evidence": ["Earnings revisions have broadened for three weeks."],
        "invalidation": ["SPY closes below its 50-day moving average."],
        "risk_budget": {"max_loss_dollars": "225"},
        "notes": "Prefer a simple position that can be explained in one screen.",
    }


def complete_curation_request() -> dict[str, object]:
    return {
        "brief": complete_brief(),
        "protocol_fields": {
            "entry_rule": "Enter after the daily close confirms the move.",
            "no_trade_rule": "Do not enter when price history is incomplete.",
            "profit_exit_rule": "Close at the planned profit threshold.",
            "loss_exit_rule": "Close before the approved maximum loss is exceeded.",
            "time_exit_rule": "Close by the final review date.",
            "invalidation_rules": ["SPY closes below its 50-day moving average."],
        },
    }


def curated_response() -> StrategyCurationResponse:
    payload = complete_curation_request()
    return StrategyCurationResponse.model_validate(
        {
            "intake": payload["brief"],
            "protocol_fields": payload["protocol_fields"],
            "classifications": {
                "direction": "BULLISH",
                "structure": "BULL_CALL_DEBIT_SPREAD",
                "clarity": "READY",
                "evidence": "READY",
                "risk": "READY",
                "exit": "READY",
                "confidence": "HIGH",
            },
            "blocking_questions": [],
            "supporting_evidence": [
                {
                    "evidence_id": "evidence-1",
                    "excerpt": "Earnings revisions have broadened for three weeks.",
                }
            ],
        }
    )


class StubCurationService:
    def __init__(
        self,
        outcome: StrategyCurationResponse | StrategyCurationUnavailable,
    ) -> None:
        self.outcome = outcome
        self.calls = []

    def curate(self, payload):
        self.calls.append(payload)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@contextmanager
def curation_service(service: StubCurationService):
    original = getattr(app.state, "strategy_curation_service", None)
    app.state.strategy_curation_service = service
    try:
        yield service
    finally:
        if original is None:
            delattr(app.state, "strategy_curation_service")
        else:
            app.state.strategy_curation_service = original


def test_owner_turns_a_plain_thesis_into_a_non_executable_review_draft() -> None:
    with owner_client() as client:
        response = client.post(
            "/api/owner/strategy-drafts",
            headers=owner_headers(client),
            json=complete_brief(),
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "schema_version": "v1",
        "status": "DRAFT_REVIEW_REQUIRED",
        "curation_status": "NOT_CURATED",
        "automation_state": "OFF",
        "execution_eligible": False,
        "intake": complete_brief(),
        "assumptions": [
            "USER_BRIEF_UNVERIFIED",
            "OPTIONS_ONLY",
            "PAPER_ONLY",
            "DEFINED_RISK_ONLY",
        ],
        "questions": [],
        "required_before_promotion": [
            "MODEL_CURATION_REQUIRED",
            "EVIDENCE_REVIEW_REQUIRED",
            "RISK_REVIEW_REQUIRED",
            "OWNER_REVIEW_REQUIRED",
        ],
        "structure_constraints": {
            "options_required": True,
            "defined_risk_required": True,
            "naked_short_options_allowed": False,
            "direction": "BULLISH",
            "candidate_families": ["BULL_CALL_DEBIT_SPREAD"],
        },
        "evidence_plan": {
            "submitted_evidence": ["Earnings revisions have broadened for three weeks."],
            "required_checks": [
                "VERIFY_THESIS_CLAIMS",
                "CHECK_MARKET_DATA_RECENCY",
                "CHECK_OPTION_LIQUIDITY",
                "CHECK_INVALIDATION_STATE",
            ],
        },
        "risk_rules": {
            "budget": {"max_loss_dollars": "225"},
            "loss_must_be_bounded": True,
            "size_must_fit_budget": True,
        },
        "exit_rules": {
            "invalidation": ["SPY closes below its 50-day moving average."],
            "required_before_promotion": [
                "PROFIT_EXIT_REQUIRED",
                "LOSS_EXIT_REQUIRED",
                "TIME_EXIT_REQUIRED",
            ],
        },
    }


def test_incomplete_markdown_brief_becomes_questions_instead_of_a_trade_plan() -> None:
    payload = {
        "source": {
            "kind": "MARKDOWN_FILE",
            "filename": "idea.md",
            "content": "# Semiconductors\n\nI expect a large move after the next industry report.",
        },
        "direction": "UNSURE",
    }
    with owner_client() as client:
        first = client.post(
            "/api/owner/strategy-drafts",
            headers=owner_headers(client),
            json=payload,
        )
        second = client.post(
            "/api/owner/strategy-drafts",
            headers=owner_headers(client),
            json=payload,
        )

    assert first.status_code == 200
    assert first.json() == second.json()
    draft = first.json()
    assert draft["status"] == "DRAFT_REVIEW_REQUIRED"
    assert draft["automation_state"] == "OFF"
    assert draft["execution_eligible"] is False
    assert draft["questions"] == [
        "MARKET_SCOPE_REQUIRED",
        "HORIZON_REQUIRED",
        "EVIDENCE_REQUIRED",
        "INVALIDATION_REQUIRED",
        "RISK_BUDGET_REQUIRED",
        "DIRECTION_REVIEW_REQUIRED",
    ]
    assert draft["structure_constraints"]["candidate_families"] == []
    assert draft["risk_rules"].get("budget") is None


def test_strategy_draft_requires_owner_csrf_and_rejects_unsupported_input() -> None:
    payload = complete_brief()
    with owner_client() as client:
        headers = owner_headers(client)
        unauthenticated = client.post("/api/owner/strategy-drafts", json=payload)
        extra_query = client.post(
            "/api/owner/strategy-drafts?execute=true",
            headers=headers,
            json=payload,
        )
        extra_body_field = client.post(
            "/api/owner/strategy-drafts",
            headers=headers,
            json={**payload, "arm": True},
        )
        wrong_file = client.post(
            "/api/owner/strategy-drafts",
            headers=headers,
            json={
                "source": {
                    "kind": "MARKDOWN_FILE",
                    "filename": "idea.pdf",
                    "content": "This thesis is long enough to pass basic content validation.",
                }
            },
        )

    assert unauthenticated.status_code == 422
    assert extra_query.status_code == 422
    assert extra_body_field.status_code == 422
    assert wrong_file.status_code == 422
    for response in (unauthenticated, extra_query, extra_body_field, wrong_file):
        assert response.headers["Cache-Control"] == "no-store"


def test_strategy_draft_openapi_is_owner_only_and_explicitly_non_executable() -> None:
    document = app.openapi()
    operation = document["paths"]["/api/owner/strategy-drafts"]["post"]

    assert operation["operationId"] == "owner_strategy_draft_create"
    assert operation["tags"] == ["Owner"]
    assert "requestBody" in operation
    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_ref == {"$ref": "#/components/schemas/StrategyProtocolDraftResponse"}
    response_schema = document["components"]["schemas"]["StrategyProtocolDraftResponse"]
    execution_schema = response_schema["properties"]["execution_eligible"]
    assert execution_schema["const"] is False


def test_strategy_draft_rejects_blank_human_text_fields() -> None:
    with owner_client() as client:
        headers = owner_headers(client)
        blank_brief = client.post(
            "/api/owner/strategy-drafts",
            headers=headers,
            json={"source": {"kind": "PASTED_TEXT", "content": " " * 20}},
        )
        blank_market = client.post(
            "/api/owner/strategy-drafts",
            headers=headers,
            json={
                "source": {
                    "kind": "PASTED_TEXT",
                    "content": "This thesis has enough meaningful text to be reviewed.",
                },
                "market_scope": "   ",
            },
        )

    assert blank_brief.status_code == 422
    assert blank_market.status_code == 422


@pytest.mark.parametrize(
    ("direction", "family"),
    (
        ("BULLISH", "BULL_CALL_DEBIT_SPREAD"),
        ("BEARISH", "BEAR_PUT_DEBIT_SPREAD"),
        ("NEUTRAL", "IRON_CONDOR"),
    ),
)
def test_direction_only_limits_which_defined_risk_family_can_be_curated(
    direction: str,
    family: str,
) -> None:
    payload = complete_brief()
    payload["direction"] = direction
    with owner_client() as client:
        response = client.post(
            "/api/owner/strategy-drafts",
            headers=owner_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    assert response.json()["structure_constraints"]["candidate_families"] == [family]
    assert response.json()["curation_status"] == "NOT_CURATED"


def test_owner_curates_an_exact_review_only_protocol() -> None:
    service = StubCurationService(curated_response())
    with curation_service(service), owner_client() as client:
        response = client.post(
            "/api/owner/strategy-curations",
            headers=owner_headers(client),
            json=complete_curation_request(),
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == curated_response().model_dump(mode="json", exclude_none=True)
    assert response.json()["execution_eligible"] is False
    assert response.json()["automation_state"] == "OFF"
    assert response.json()["protocol_fields"] == complete_curation_request()["protocol_fields"]
    assert len(service.calls) == 1


def test_strategy_curation_requires_owner_csrf_rejects_queries_and_sanitizes_failures() -> None:
    unavailable = StubCurationService(StrategyCurationUnavailable("CURATION_PROVIDER_ERROR"))
    with curation_service(unavailable), owner_client() as client:
        headers = owner_headers(client)
        unauthenticated = client.post(
            "/api/owner/strategy-curations",
            json=complete_curation_request(),
        )
        query = client.post(
            "/api/owner/strategy-curations?execute=true",
            headers=headers,
            json=complete_curation_request(),
        )
        provider_failure = client.post(
            "/api/owner/strategy-curations",
            headers=headers,
            json=complete_curation_request(),
        )

    assert unauthenticated.status_code == 422
    assert query.status_code == 422
    assert provider_failure.status_code == 503
    assert provider_failure.json() == {"detail": "STRATEGY_CURATION_UNAVAILABLE"}
    assert "CURATION_PROVIDER_ERROR" not in provider_failure.text
    for result in (unauthenticated, query, provider_failure):
        assert result.headers["Cache-Control"] == "no-store"


def test_strategy_curation_redacts_invalid_private_input() -> None:
    sentinel = "private thesis"
    payload = complete_curation_request()
    payload["brief"] = {
        **complete_brief(),
        "source": {"kind": "PASTED_TEXT", "content": sentinel},
    }
    service = StubCurationService(curated_response())
    with curation_service(service), owner_client() as client:
        response = client.post(
            "/api/owner/strategy-curations",
            headers=owner_headers(client),
            json=payload,
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "STRATEGY_CURATION_INPUT_REJECTED"}
    assert sentinel not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert service.calls == []


def test_strategy_curation_openapi_is_owner_only_and_non_executable() -> None:
    operation = app.openapi()["paths"]["/api/owner/strategy-curations"]["post"]
    assert operation["operationId"] == "owner_strategy_curation_create"
    assert operation["tags"] == ["Owner"]
    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_ref == {"$ref": "#/components/schemas/StrategyCurationResponse"}
    response_schema = app.openapi()["components"]["schemas"]["StrategyCurationResponse"]
    assert response_schema["properties"]["execution_eligible"]["const"] is False
