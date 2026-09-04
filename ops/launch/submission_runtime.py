from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.config import RuntimeRole, Settings
from backend.app.contracts.v1 import AccountRole
from backend.app.persistence.opportunity_evidence import SQLAlchemyOpportunityEvidenceRepository
from backend.app.persistence.opportunity_runtime import SQLAlchemyOpportunityPlanAdapter
from backend.app.persistence.runtime import normalize_database_url, verify_schema
from backend.app.runtime.production import _validate_plan_runtime_authority

_FILE_LIMIT = 64 * 1024
_PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
_FIXED_HOST = "127.0.0.1"
_FIXED_PORT = "8000"

_SETTING_ENV = {
    "APP_ACCOUNT_ROLE": "app_account_role",
    "APP_POLICY_HASH": "app_policy_hash",
    "APP_CALIBRATION_HASH": "app_calibration_hash",
    "APP_CALIBRATION_DECISION_BOUNDARY": "app_calibration_decision_boundary",
    "APP_CALIBRATION_SEALED_AT": "app_calibration_sealed_at",
    "APP_ENTRY_EQUITY_FLOOR": "app_entry_equity_floor",
    "APP_MAXIMUM_LIFETIME_ENTRIES": "app_maximum_lifetime_entries",
    "APP_MAXIMUM_LIFETIME_RISK": "app_maximum_lifetime_risk",
    "APP_MAXIMUM_POSITION_LOSS": "app_maximum_position_loss",
    "APP_MAXIMUM_ENTRY_QUANTITY": "app_maximum_entry_quantity",
    "APP_OPPORTUNITY_KEY": "app_opportunity_key",
    "APP_OPPORTUNITY_PLAN_VERSION": "app_opportunity_plan_version",
    "APP_HALT_MAXIMUM_TRADE_AGE_SECONDS": "app_halt_maximum_trade_age_seconds",
    "ALPACA_API_ENDPOINT": "alpaca_api_endpoint",
    "ALPACA_API_KEY": "alpaca_api_key",
    "ALPACA_SECRET_KEY": "alpaca_secret_key",
    "ALPACA_PAPER_TRADE": "alpaca_paper_trade",
    "DATABASE_URL": "database_url",
    "GEMINI_API_KEY": "gemini_api_key",
    "APP_OWNER_ACCESS_CODE": "app_owner_access_code",
    "APP_SESSION_SECRET": "app_session_secret",
    "APP_PROVIDER_SETTINGS_SECRET": "app_provider_settings_secret",
    "APP_OPENAI_COMPATIBLE_ORIGINS": "app_openai_compatible_origins",
    "APP_ALLOWED_ORIGIN": "app_allowed_origin",
    "SCHEDULER_TOKEN": "scheduler_token",
}


class SubmissionRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SubmissionRuntimeConfig:
    environment: dict[str, str]
    settings: Settings
    opportunity_key: str
    opportunity_version: int


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preflight or start the exact persisted SUBMISSION opportunity runtime"
    )
    parser.add_argument("--config", required=True, type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--serve", action="store_true", help="start the connected runtime disarmed")
    action.add_argument(
        "--tick",
        action="store_true",
        help="call one selector-free scheduler tick on the already-running local product",
    )
    parser.add_argument(
        "--autonomous",
        action="store_true",
        help="start with the server autonomy gate enabled; the owner gate remains separate",
    )
    return parser


def load_config(path: Path, *, autonomous: bool = False) -> SubmissionRuntimeConfig:
    if path.name.startswith(".env"):
        raise SubmissionRuntimeError("SUBMISSION_RUNTIME_ENV_FILE_FORBIDDEN")
    try:
        payload = json.loads(
            _read_private_file(path).decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError):
        raise SubmissionRuntimeError("SUBMISSION_RUNTIME_CONFIG_INVALID") from None
    if type(payload) is not dict or set(payload) != set(_SETTING_ENV):
        raise SubmissionRuntimeError("SUBMISSION_RUNTIME_CONFIG_INVALID")
    if any(
        type(value) is not str or (not value and key != "APP_OPENAI_COMPATIBLE_ORIGINS")
        for key, value in payload.items()
    ):
        raise SubmissionRuntimeError("SUBMISSION_RUNTIME_CONFIG_INVALID")
    values = {_SETTING_ENV[key]: value for key, value in payload.items()}
    values.update(
        app_autonomous_enabled=autonomous,
        app_submission_opportunity_enabled=True,
    )
    try:
        settings = Settings.model_validate(values)
        opportunity = settings.opportunity_authority()
    except ValueError:
        raise SubmissionRuntimeError("SUBMISSION_RUNTIME_SETTINGS_INVALID") from None
    if (
        settings.app_account_role is not RuntimeRole.SUBMISSION
        or settings.alpaca_api_endpoint != _PAPER_ENDPOINT
        or settings.alpaca_paper_trade is not True
        or opportunity is None
    ):
        raise SubmissionRuntimeError("SUBMISSION_RUNTIME_AUTHORITY_INVALID")
    opportunity_key, opportunity_version, _maximum_trade_age = opportunity
    environment = dict(payload)
    environment.update(
        APP_RUNTIME_CONFIG_REQUIRED="true",
        APP_ACCOUNT_ROLE=RuntimeRole.SUBMISSION.value,
        APP_SUBMISSION_OPPORTUNITY_ENABLED="true",
        APP_AUTONOMOUS_ENABLED="true" if autonomous else "false",
        ALPACA_API_ENDPOINT=_PAPER_ENDPOINT,
        ALPACA_PAPER_TRADE="true",
    )
    return SubmissionRuntimeConfig(
        environment=environment,
        settings=settings,
        opportunity_key=opportunity_key,
        opportunity_version=opportunity_version,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def verify_persisted_authority(config: SubmissionRuntimeConfig) -> None:
    engine = create_engine(
        normalize_database_url(config.settings.database_url.get_secret_value()),
        pool_pre_ping=True,
    )
    try:
        verify_schema(engine)
        repository = SQLAlchemyOpportunityEvidenceRepository(
            sessionmaker(engine, expire_on_commit=False)
        )
        plan = SQLAlchemyOpportunityPlanAdapter(
            repository,
            opportunity_key=config.opportunity_key,
            version=config.opportunity_version,
            account_role=AccountRole.SUBMISSION,
        ).load(trusted_at=config.settings.app_calibration_sealed_at)
        _validate_plan_runtime_authority(plan, config.settings)
    finally:
        engine.dispose()


def _serve(config: SubmissionRuntimeConfig, runner: Callable[..., object]) -> int:
    environment = os.environ.copy()
    environment.update(config.environment)
    command = (
        sys.executable,
        "-m",
        "uvicorn",
        "backend.app.main:app",
        "--host",
        _FIXED_HOST,
        "--port",
        _FIXED_PORT,
    )
    runner(sys.executable, command, environment)
    return 0


def _tick(config: SubmissionRuntimeConfig, sender: Callable[[Mapping[str, str]], str]) -> int:
    code = sender(
        {
            "ALPHADECAY_SCHEDULER_URL": (
                f"http://{_FIXED_HOST}:{_FIXED_PORT}/api/internal/scheduler/tick"
            ),
            "ALPHADECAY_SCHEDULER_TOKEN": config.environment["SCHEDULER_TOKEN"],
        }
    )
    print(json.dumps({"mode": "SELECTOR_FREE_TICK", "code": code}, sort_keys=True))
    return 0


def send_local_tick(environment: Mapping[str, str]) -> str:
    expected_url = f"http://{_FIXED_HOST}:{_FIXED_PORT}/api/internal/scheduler/tick"
    if (
        set(environment)
        != {
            "ALPHADECAY_SCHEDULER_URL",
            "ALPHADECAY_SCHEDULER_TOKEN",
        }
        or environment["ALPHADECAY_SCHEDULER_URL"] != expected_url
    ):
        raise SubmissionRuntimeError("SUBMISSION_RUNTIME_TICK_AUTHORITY_INVALID")
    token = environment["ALPHADECAY_SCHEDULER_TOKEN"]
    try:
        response = httpx.post(
            expected_url,
            content=b"",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/octet-stream",
            },
            timeout=90.0,
            follow_redirects=False,
            trust_env=False,
        )
        payload = response.json()
        UUID(str(payload["tick_id"]))
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        raise SubmissionRuntimeError("SUBMISSION_RUNTIME_TICK_FAILED") from None
    if (
        response.status_code != 200
        or type(payload) is not dict
        or set(payload) != {"schema_version", "tick_id", "accepted", "code"}
        or payload["schema_version"] != "v1"
        or payload["accepted"] is not True
        or type(payload["code"]) is not str
        or not payload["code"]
    ):
        raise SubmissionRuntimeError("SUBMISSION_RUNTIME_TICK_FAILED")
    return payload["code"]


def main(
    argv: Sequence[str] | None = None,
    *,
    verifier: Callable[[SubmissionRuntimeConfig], None] = verify_persisted_authority,
    runner: Callable[..., object] = os.execvpe,
    tick_sender: Callable[[Mapping[str, str]], str] | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.tick and args.autonomous:
        parser.error("SUBMISSION_RUNTIME_MODE_INVALID")
    try:
        config = load_config(args.config, autonomous=args.autonomous)
        verifier(config)
        if args.tick:
            if tick_sender is None:
                tick_sender = send_local_tick
            return _tick(config, tick_sender)
        if args.serve or args.autonomous:
            return _serve(config, runner)
    except Exception as error:
        if isinstance(error, SystemExit):
            raise
        code = (
            error.code
            if isinstance(error, SubmissionRuntimeError)
            else "SUBMISSION_RUNTIME_PREFLIGHT_FAILED"
        )
        parser.error(code)
    print(
        json.dumps(
            {
                "mode": "NO_WRITE_PREFLIGHT",
                "account_role": AccountRole.SUBMISSION.value,
                "autonomous_enabled": False,
                "opportunity_key": config.opportunity_key,
                "opportunity_version": config.opportunity_version,
                "paper_endpoint_verified": True,
                "entry_budget_verified": True,
                "persisted_authority_verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _read_private_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise SubmissionRuntimeError("SUBMISSION_RUNTIME_PRIVATE_FILE_INVALID")
        result = bytearray()
        while True:
            chunk = os.read(descriptor, min(16 * 1024, _FILE_LIMIT + 1 - len(result)))
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            if len(result) > _FILE_LIMIT:
                raise SubmissionRuntimeError("SUBMISSION_RUNTIME_PRIVATE_FILE_INVALID")
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


if __name__ == "__main__":
    raise SystemExit(main())
