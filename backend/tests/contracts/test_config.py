from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.main import app


def values(endpoint: str = "https://paper-api.alpaca.markets") -> dict[str, object]:
    return {
        "app_account_role": "DEVELOPMENT",
        "app_policy_hash": "a" * 64,
        "app_calibration_hash": "b" * 64,
        "app_calibration_decision_boundary": datetime(2026, 8, 28, 16, tzinfo=UTC),
        "app_calibration_sealed_at": datetime(2026, 8, 28, 16, 1, tzinfo=UTC),
        "app_entry_equity_floor": "99000",
        "app_maximum_lifetime_entries": "3",
        "app_maximum_lifetime_risk": "1500",
        "app_maximum_position_loss": "900",
        "app_maximum_entry_quantity": "4",
        "alpaca_api_endpoint": endpoint,
        "alpaca_api_key": "key",
        "alpaca_secret_key": "secret",
        "database_url": "postgresql://user:secret@db.example/alphadecay",
        "gemini_api_key": "model-key",
        "app_owner_access_code": "owner-access-code-fixture",
        "app_session_secret": "s" * 32,
        "app_provider_settings_secret": "p" * 32,
        "app_allowed_origin": "https://alphadecay.example",
        "scheduler_token": "t" * 32,
    }


def test_paper_endpoint_is_accepted() -> None:
    settings = Settings(**values())

    assert settings.alpaca_paper_trade is True
    assert settings.entry_budget_limits().policy_hash == "a" * 64


def test_development_opportunity_authority_is_exact_and_bounded() -> None:
    supplied = values()
    supplied.update(
        {
            "app_opportunity_key": "ACME_EVENT",
            "app_opportunity_plan_version": "3",
            "app_halt_maximum_trade_age_seconds": "15",
        }
    )

    configured = Settings(**supplied)

    authority = configured.opportunity_authority()
    assert authority is not None
    key, version, maximum_trade_age = authority
    assert key == "ACME_EVENT"
    assert version == 3
    assert maximum_trade_age.total_seconds() == 15


@pytest.mark.parametrize(
    "updates",
    (
        {"app_opportunity_key": "ACME_EVENT"},
        {
            "app_opportunity_key": "ACME_EVENT",
            "app_opportunity_plan_version": "0",
            "app_halt_maximum_trade_age_seconds": "15",
        },
        {
            "app_opportunity_key": "ACME_EVENT",
            "app_opportunity_plan_version": "3",
            "app_halt_maximum_trade_age_seconds": "121",
        },
    ),
)
def test_development_opportunity_authority_fails_closed_when_incomplete_or_invalid(
    updates: dict[str, str],
) -> None:
    supplied = values()
    supplied.update(updates)

    with pytest.raises(ValidationError, match="opportunity authority"):
        Settings(**supplied)


def test_submission_rejects_partial_opportunity_authority_even_when_gate_is_off() -> None:
    supplied = values()
    supplied["app_account_role"] = "SUBMISSION"
    supplied["app_opportunity_key"] = "unused-partial-setting"

    with pytest.raises(ValidationError, match="opportunity authority is incomplete"):
        Settings(**supplied)


def test_submission_opportunity_authority_requires_explicit_gate() -> None:
    supplied = values()
    supplied.update(
        {
            "app_account_role": "SUBMISSION",
            "app_opportunity_key": "ACME_EVENT",
            "app_opportunity_plan_version": "3",
            "app_halt_maximum_trade_age_seconds": "15",
        }
    )

    assert Settings(**supplied).opportunity_authority() is None
    supplied["app_submission_opportunity_enabled"] = True
    assert Settings(**supplied).opportunity_authority() is not None


def test_openai_compatible_origins_are_canonical_and_canonical_duplicates_fail() -> None:
    supplied = values()
    supplied["app_openai_compatible_origins"] = (
        "HTTPS://API.EXAMPLE.COM:443/, https://second.example.com"
    )
    configured = Settings(**supplied)
    assert configured.openai_compatible_origins() == (
        "https://api.example.com",
        "https://second.example.com",
    )

    supplied["app_openai_compatible_origins"] = (
        "https://api.example.com,HTTPS://API.EXAMPLE.COM:443/"
    )
    with pytest.raises(ValidationError, match="OpenAI-compatible origins are invalid"):
        Settings(**supplied)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_entry_equity_floor", "not-a-number"),
        ("app_maximum_lifetime_entries", "0"),
        ("app_maximum_lifetime_risk", "899"),
        ("app_maximum_position_loss", "100001"),
        ("app_maximum_entry_quantity", "101"),
    ],
)
def test_private_entry_limits_fail_closed(field: str, value: str) -> None:
    supplied = values()
    supplied[field] = value

    with pytest.raises(ValidationError, match="entry budget limits are invalid"):
        Settings(**supplied)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.alpaca.markets",
        "https://example.com",
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets:444",
        "https://paper-api.alpaca.markets/v2",
        "https://user@paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets?mode=paper",
        "https://paper-api.alpaca.markets#fragment",
    ],
)
def test_nonpaper_endpoint_is_rejected(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="paper endpoint"):
        Settings(**values(endpoint))


def test_deployed_runtime_validates_required_paper_configuration(monkeypatch) -> None:
    monkeypatch.setenv("APP_RUNTIME_CONFIG_REQUIRED", "true")
    monkeypatch.delenv("APP_ACCOUNT_ROLE", raising=False)
    monkeypatch.setenv("ALPACA_API_ENDPOINT", "https://paper-api.alpaca.markets")
    monkeypatch.setenv("ALPACA_API_KEY", "fixture-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fixture-secret")

    with pytest.raises(ValidationError, match="app_account_role"), TestClient(app):
        pass


@pytest.mark.parametrize("field", ["alpaca_api_key", "alpaca_secret_key", "gemini_api_key"])
def test_empty_credentials_are_rejected(field: str) -> None:
    supplied = values()
    supplied[field] = "   "

    with pytest.raises(ValidationError, match="provider credentials are required"):
        Settings(**supplied)


@pytest.mark.parametrize(
    "origin",
    [
        "http://alphadecay.example",
        "https://user@alphadecay.example",
        "https://alphadecay.example/app",
        "https://alphadecay.example?owner=true",
        "https://alphadecay.example:443",
        "https://alphadecay.example:bad",
    ],
)
def test_allowed_origin_must_be_an_exact_https_origin(origin: str) -> None:
    supplied = values()
    supplied["app_allowed_origin"] = origin

    with pytest.raises(ValidationError, match="HTTPS origin"):
        Settings(**supplied)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_owner_access_code", "too-short"),
        ("app_session_secret", "too-short"),
        ("app_provider_settings_secret", "too-short"),
        ("scheduler_token", "too-short"),
    ],
)
def test_runtime_secrets_have_minimum_lengths(field: str, value: str) -> None:
    supplied = values()
    supplied[field] = value

    with pytest.raises(ValidationError, match="at least"):
        Settings(**supplied)


def test_provider_settings_secret_is_redacted_from_configuration_errors() -> None:
    supplied = values()
    secret = "synthetic-sensitive-short"
    supplied["app_provider_settings_secret"] = secret

    with pytest.raises(ValidationError) as error:
        Settings(**supplied)

    assert secret not in str(error.value)


@pytest.mark.parametrize("field", ["app_policy_hash", "app_calibration_hash"])
def test_runtime_authority_hashes_are_exact_lowercase_sha256(field: str) -> None:
    supplied = values()
    supplied[field] = "A" * 64

    with pytest.raises(ValidationError, match="lowercase SHA-256"):
        Settings(**supplied)


@pytest.mark.parametrize(
    ("boundary", "sealed"),
    [
        (
            datetime(2026, 8, 28, 16),
            datetime(2026, 8, 28, 16, 1, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 28, 16, tzinfo=UTC),
            datetime(2026, 8, 28, 15, 59, tzinfo=UTC),
        ),
    ],
)
def test_calibration_timestamps_must_be_ordered_utc(boundary: datetime, sealed: datetime) -> None:
    supplied = values()
    supplied["app_calibration_decision_boundary"] = boundary
    supplied["app_calibration_sealed_at"] = sealed

    with pytest.raises(ValidationError, match="ordered UTC"):
        Settings(**supplied)
