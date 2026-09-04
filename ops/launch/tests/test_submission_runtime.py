from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from backend.app.config import RuntimeRole
from backend.app.policy.opportunity import (
    STRUCTURAL_BEARISH_OTM_PILOT_ID,
    STRUCTURAL_BULLISH_OTM_PILOT_ID,
    OpportunityDirection,
    structural_pilot_profile,
)
from ops.launch.submission_runtime import SubmissionRuntimeError, load_config, main


def _payload() -> dict[str, str]:
    return {
        "APP_ACCOUNT_ROLE": "SUBMISSION",
        "APP_POLICY_HASH": "a" * 64,
        "APP_CALIBRATION_HASH": "b" * 64,
        "APP_CALIBRATION_DECISION_BOUNDARY": "2026-09-01T14:00:00Z",
        "APP_CALIBRATION_SEALED_AT": "2026-09-01T14:01:00Z",
        "APP_ENTRY_EQUITY_FLOOR": "99000",
        "APP_MAXIMUM_LIFETIME_ENTRIES": "1",
        "APP_MAXIMUM_LIFETIME_RISK": "500",
        "APP_MAXIMUM_POSITION_LOSS": "500",
        "APP_MAXIMUM_ENTRY_QUANTITY": "1",
        "APP_OPPORTUNITY_KEY": "EXACT_EVENT_V1",
        "APP_OPPORTUNITY_PLAN_VERSION": "2",
        "APP_HALT_MAXIMUM_TRADE_AGE_SECONDS": "30",
        "ALPACA_API_ENDPOINT": "https://paper-api.alpaca.markets",
        "ALPACA_API_KEY": "paper-key",
        "ALPACA_SECRET_KEY": "paper-secret",
        "ALPACA_PAPER_TRADE": "true",
        "DATABASE_URL": "postgresql://user:secret@localhost/alphadecay",
        "GEMINI_API_KEY": "model-key",
        "APP_OWNER_ACCESS_CODE": "o" * 16,
        "APP_SESSION_SECRET": "s" * 32,
        "APP_PROVIDER_SETTINGS_SECRET": "p" * 32,
        "APP_OPENAI_COMPATIBLE_ORIGINS": "",
        "APP_ALLOWED_ORIGIN": "https://alphadecay.example",
        "SCHEDULER_TOKEN": "t" * 32,
    }


def _private_json(path: Path, payload: dict[str, str]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_default_is_read_only_exact_authority_preflight(tmp_path: Path, capsys) -> None:
    config_file = tmp_path / "runtime.json"
    _private_json(config_file, _payload())
    verified = []

    assert main(["--config", str(config_file)], verifier=verified.append) == 0

    config = verified[0]
    assert config.settings.app_account_role is RuntimeRole.SUBMISSION
    assert config.settings.app_autonomous_enabled is False
    assert config.opportunity_key == "EXACT_EVENT_V1"
    assert config.opportunity_version == 2
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "NO_WRITE_PREFLIGHT"
    assert "paper-key" not in json.dumps(output)
    assert "secret" not in json.dumps(output)


@pytest.mark.parametrize(
    ("opportunity_key", "direction"),
    (
        (STRUCTURAL_BULLISH_OTM_PILOT_ID, OpportunityDirection.BULLISH),
        (STRUCTURAL_BEARISH_OTM_PILOT_ID, OpportunityDirection.BEARISH),
    ),
)
def test_runtime_opportunity_key_selects_registered_profile(
    tmp_path: Path,
    opportunity_key: str,
    direction: OpportunityDirection,
) -> None:
    payload = _payload()
    payload["APP_OPPORTUNITY_KEY"] = opportunity_key
    payload["APP_OPPORTUNITY_PLAN_VERSION"] = "1"
    config_file = tmp_path / "runtime.json"
    _private_json(config_file, payload)

    config = load_config(config_file)
    profile = structural_pilot_profile(config.opportunity_key)

    assert config.opportunity_key == opportunity_key
    assert config.opportunity_version == 1
    assert profile is not None
    assert profile.direction is direction


def test_disarmed_serve_starts_existing_product_with_fixed_local_bind(tmp_path: Path) -> None:
    config_file = tmp_path / "runtime.json"
    _private_json(config_file, _payload())
    calls = []

    def runner(program, argv, environment):
        calls.append((program, argv, environment))

    assert (
        main(
            ["--config", str(config_file), "--serve"],
            verifier=lambda _config: None,
            runner=runner,
        )
        == 0
    )
    program, argv, environment = calls[0]
    assert program == sys.executable
    assert argv[:3] == (sys.executable, "-m", "uvicorn")
    assert "uv" not in argv
    assert argv[-4:] == ("--host", "127.0.0.1", "--port", "8000")
    assert environment["APP_RUNTIME_CONFIG_REQUIRED"] == "true"
    assert environment["APP_ACCOUNT_ROLE"] == "SUBMISSION"
    assert environment["APP_SUBMISSION_OPPORTUNITY_ENABLED"] == "true"
    assert environment["APP_AUTONOMOUS_ENABLED"] == "false"


def test_autonomous_mode_only_opens_server_gate_and_does_not_arm_account(tmp_path: Path) -> None:
    config_file = tmp_path / "runtime.json"
    _private_json(config_file, _payload())
    calls = []

    main(
        ["--config", str(config_file), "--autonomous"],
        verifier=lambda _config: None,
        runner=lambda program, argv, environment: calls.append(environment),
    )

    assert calls[0]["APP_AUTONOMOUS_ENABLED"] == "true"
    assert "ACCOUNT_AUTONOMY" not in calls[0]


def test_serve_and_autonomous_can_be_combined_for_bounded_controller(tmp_path: Path) -> None:
    config_file = tmp_path / "runtime.json"
    _private_json(config_file, _payload())
    calls = []

    assert (
        main(
            ["--config", str(config_file), "--serve", "--autonomous"],
            verifier=lambda _config: None,
            runner=lambda program, argv, environment: calls.append((program, argv, environment)),
        )
        == 0
    )

    assert calls[0][2]["APP_AUTONOMOUS_ENABLED"] == "true"


def test_tick_cannot_be_combined_with_autonomous(tmp_path: Path) -> None:
    config_file = tmp_path / "runtime.json"
    _private_json(config_file, _payload())

    with pytest.raises(SystemExit):
        main(
            ["--config", str(config_file), "--tick", "--autonomous"],
            verifier=lambda _config: None,
        )


def test_tick_uses_fixed_selector_free_route_and_private_token(tmp_path: Path, capsys) -> None:
    config_file = tmp_path / "runtime.json"
    _private_json(config_file, _payload())
    seen = []

    assert (
        main(
            ["--config", str(config_file), "--tick"],
            verifier=lambda _config: None,
            tick_sender=lambda environment: seen.append(environment) or "NO_TRADE",
        )
        == 0
    )

    assert seen == [
        {
            "ALPHADECAY_SCHEDULER_URL": "http://127.0.0.1:8000/api/internal/scheduler/tick",
            "ALPHADECAY_SCHEDULER_TOKEN": "t" * 32,
        }
    ]
    assert json.loads(capsys.readouterr().out) == {
        "code": "NO_TRADE",
        "mode": "SELECTOR_FREE_TICK",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("APP_ACCOUNT_ROLE", "DEVELOPMENT"),
        ("ALPACA_API_ENDPOINT", "https://api.alpaca.markets"),
        ("ALPACA_PAPER_TRADE", "false"),
        ("APP_MAXIMUM_POSITION_LOSS", "501"),
        ("APP_OPPORTUNITY_PLAN_VERSION", "0"),
    ],
)
def test_rejects_role_live_endpoint_paper_off_and_invalid_authority(
    tmp_path: Path, key: str, value: str
) -> None:
    config_file = tmp_path / "runtime.json"
    payload = _payload()
    payload[key] = value
    _private_json(config_file, payload)

    with pytest.raises(SubmissionRuntimeError):
        load_config(config_file)


@pytest.mark.parametrize("selector", ["SYMBOL", "LEGS", "QUANTITY", "PRICE"])
def test_rejects_every_order_selector(tmp_path: Path, selector: str) -> None:
    config_file = tmp_path / "runtime.json"
    payload = _payload()
    payload[selector] = "forbidden"
    _private_json(config_file, payload)

    with pytest.raises(SubmissionRuntimeError):
        load_config(config_file)


def test_rejects_env_files_links_hardlinks_and_public_files(tmp_path: Path) -> None:
    config_file = tmp_path / "runtime.json"
    _private_json(config_file, _payload())
    env_file = tmp_path / ".env.local"
    _private_json(env_file, _payload())
    link = tmp_path / "link.json"
    link.symlink_to(config_file)
    hardlink = tmp_path / "hardlink.json"
    os.link(config_file, hardlink)
    public = tmp_path / "public.json"
    _private_json(public, _payload())
    public.chmod(0o644)
    readonly = tmp_path / "readonly.json"
    _private_json(readonly, _payload())
    readonly.chmod(0o400)
    executable = tmp_path / "executable.json"
    _private_json(executable, _payload())
    executable.chmod(0o700)

    for path in (env_file, link, hardlink, public, readonly, executable):
        with pytest.raises(SubmissionRuntimeError):
            load_config(path)


def test_rejects_duplicate_runtime_keys(tmp_path: Path) -> None:
    config_file = tmp_path / "runtime.json"
    payload = json.dumps(_payload())
    duplicate = '"APP_ACCOUNT_ROLE": "SUBMISSION", '
    assert duplicate in payload
    config_file.write_text(payload.replace(duplicate, duplicate * 2, 1))
    config_file.chmod(0o600)

    with pytest.raises(SubmissionRuntimeError, match="CONFIG_INVALID"):
        load_config(config_file)


def test_preflight_failure_does_not_print_database_credentials(tmp_path: Path, capsys) -> None:
    config_file = tmp_path / "runtime.json"
    _private_json(config_file, _payload())

    with pytest.raises(SystemExit):
        main(
            ["--config", str(config_file)],
            verifier=lambda _config: (_ for _ in ()).throw(
                RuntimeError("postgresql://user:secret@localhost/alphadecay")
            ),
        )

    error = capsys.readouterr().err
    assert "SUBMISSION_RUNTIME_PREFLIGHT_FAILED" in error
    assert "postgresql" not in error
    assert "secret" not in error
