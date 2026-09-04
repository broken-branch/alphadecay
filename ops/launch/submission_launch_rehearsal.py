from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import AccountRole
from backend.app.persistence.runtime import normalize_database_url, verify_schema
from backend.app.persistence.sqlalchemy_repository import SQLAlchemyExecutionRepository
from backend.app.policy.opportunity import opportunity_policy_hash
from backend.app.services.opportunity_bootstrap import (
    OpportunityBootstrapError,
    parse_opportunity_bootstrap,
    parse_opportunity_plan,
)
from ops.launch.submission_baseline_import import (
    SealedSubmissionBaseline,
    SubmissionBaselineImportError,
    load_submission_baseline_directory,
)
from ops.launch.submission_market_window import MarketWindowError, load_schedule
from ops.launch.submission_runtime import SubmissionRuntimeError, load_config

_FILE_LIMIT = 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")
_NEW_YORK = ZoneInfo("America/New_York")
_BINDING_KEYS = {"path", "sha256"}
_PACKAGE_KEYS = {
    "schema_version",
    "account_role",
    "strategy_code",
    "judged_baseline_manifest",
    "baseline_import_receipt",
    "opportunity_plan",
    "opportunity_capture",
    "database_url",
    "runtime_config",
    "entry_schedule",
    "lifecycle_schedule",
    "entry_window",
    "lifecycle_window",
}
_ENTRY_KEYS = {"starts_at", "cutoff_at", "reconcile_by"}
_LIFECYCLE_KEYS = {"close_at", "reconcile_by"}
_IMPORT_RECEIPT_KEYS = {
    "account_role",
    "database_write",
    "manifest_valid",
    "mode",
    "source_files_verified",
}


class SubmissionLaunchRehearsalError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _BoundFile:
    path: Path
    payload: bytes


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the private SUBMISSION launch package without external actions"
    )
    parser.add_argument("--package", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = rehearse(args.package)
    except Exception as error:
        code = (
            error.code
            if isinstance(error, SubmissionLaunchRehearsalError)
            else "SUBMISSION_LAUNCH_REHEARSAL_FAILED"
        )
        parser.error(code)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ready"] else 1


def rehearse(package_path: Path) -> dict[str, object]:
    if package_path.name.startswith(".env"):
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_PACKAGE_INVALID")
    package = _json(_read_private(package_path), "SUBMISSION_LAUNCH_PACKAGE_INVALID")
    if set(package) != _PACKAGE_KEYS:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_PACKAGE_INVALID")
    if (
        package["schema_version"] != "v1"
        or package["account_role"] != AccountRole.SUBMISSION.value
        or not _plain_string(package["strategy_code"])
    ):
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_AUTHORITY_INVALID")

    root = package_path.parent
    baseline = _binding(
        root,
        package["judged_baseline_manifest"],
        required=True,
        allowed_modes=frozenset({0o400, 0o600}),
    )
    assert baseline is not None
    baseline_manifest = _validate_sealed_baseline(baseline)

    plan_file = _binding(root, package["opportunity_plan"], required=True)
    assert plan_file is not None
    try:
        plan = parse_opportunity_plan(
            _json(plan_file.payload, "SUBMISSION_LAUNCH_PLAN_INVALID"),
            account_role=AccountRole.SUBMISSION,
        )
    except OpportunityBootstrapError as error:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_PLAN_INVALID") from error
    if (
        plan.opportunity_key != package["strategy_code"]
        or plan.request_contract.expected_account_fingerprint
        != baseline_manifest.account_fingerprint
    ):
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_AUTHORITY_INVALID")

    capture_file = _binding(root, package["opportunity_capture"], required=False)
    capture = None
    if capture_file is not None:
        try:
            capture = parse_opportunity_bootstrap(
                _json(capture_file.payload, "SUBMISSION_LAUNCH_CAPTURE_INVALID"),
                account_role=AccountRole.SUBMISSION,
            )
        except OpportunityBootstrapError as error:
            raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_CAPTURE_INVALID") from error
        expected_baseline_id = uuid5(
            NAMESPACE_URL,
            "alphadecay:baseline:"
            f"{AccountRole.SUBMISSION.value}:{baseline_manifest.account_fingerprint}",
        )
        if (
            capture.plan != plan
            or capture.baseline.account_fingerprint != baseline_manifest.account_fingerprint
            or capture.baseline.submission_baseline_id != expected_baseline_id
        ):
            raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_AUTHORITY_INVALID")

    import_receipt = _binding(root, package["baseline_import_receipt"], required=False)
    imported = False
    if import_receipt is not None:
        receipt = _json(
            import_receipt.payload,
            "SUBMISSION_BASELINE_IMPORT_RECEIPT_INVALID",
        )
        imported = (
            set(receipt) == _IMPORT_RECEIPT_KEYS
            and receipt["account_role"] == AccountRole.SUBMISSION.value
            and receipt["database_write"] is True
            and receipt["manifest_valid"] is True
            and receipt["mode"] == "PERSIST"
            and receipt["source_files_verified"] == 4
        )
        if not imported:
            raise SubmissionLaunchRehearsalError("SUBMISSION_BASELINE_IMPORT_RECEIPT_INVALID")
    database_url = _binding(root, package["database_url"], required=True)
    runtime_file = _binding(root, package["runtime_config"], required=True)
    entry_schedule_file = _binding(root, package["entry_schedule"], required=True)
    lifecycle_schedule_file = _binding(root, package["lifecycle_schedule"], required=True)
    assert (
        database_url is not None
        and runtime_file is not None
        and entry_schedule_file is not None
        and lifecycle_schedule_file is not None
    )
    try:
        runtime = load_config(_binding_path(root, package["runtime_config"]), autonomous=False)
    except SubmissionRuntimeError as error:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_RUNTIME_INVALID") from error
    if _read_private(runtime_file.path) != runtime_file.payload:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_BINDING_MISMATCH")
    database_value = database_url.payload.decode("utf-8")
    if (
        database_value != database_value.strip()
        or runtime.environment["DATABASE_URL"] != database_value
        or runtime.opportunity_key != plan.opportunity_key
        or runtime.opportunity_version != plan.version
        or runtime.settings.app_policy_hash.get_secret_value()
        != opportunity_policy_hash(plan.policy)
        or runtime.settings.app_calibration_hash.get_secret_value()
        != opportunity_policy_hash(plan.policy)
        or runtime.settings.app_calibration_decision_boundary != plan.frozen_at
        or runtime.settings.app_calibration_sealed_at != plan.evidence_window_start
    ):
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_AUTHORITY_INVALID")
    limits = runtime.settings.entry_budget_limits()
    if (
        limits.maximum_lifetime_entries != plan.policy.maximum_lifetime_entries
        or limits.maximum_lifetime_risk != plan.policy.maximum_lifetime_risk
        or limits.maximum_position_loss != plan.policy.maximum_position_loss
        or limits.maximum_entry_quantity != plan.policy.maximum_quantity
        or limits.equity_floor != plan.policy.equity_floor
    ):
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_AUTHORITY_INVALID")

    _validate_schedule(
        package["entry_window"],
        package["lifecycle_window"],
        entry_schedule_file=entry_schedule_file,
        lifecycle_schedule_file=lifecycle_schedule_file,
        runtime_config_path=runtime_file.path,
        selected_decision_boundary=plan.policy.selected_decision_boundary,
        last_entry_boundary=plan.policy.last_entry_boundary,
    )
    importer_present = importlib.util.find_spec("ops.launch.submission_baseline_import") is not None
    owner_arm_present = importlib.util.find_spec("ops.launch.submission_owner_arm") is not None
    routes_present = _required_routes_present()
    reconciliation_ready = _reconciliation_ready(database_value)
    stages = (
        ("JUDGED_BASELINE_IMPORT", importer_present and imported),
        ("EXACT_SUBMISSION_PLAN", True),
        ("READ_ONLY_BASELINE_CAPTURE", capture is not None),
        ("OPPORTUNITY_PERSISTENCE", capture is not None),
        ("RUNTIME_PREFLIGHT", capture is not None and imported),
        ("RECONCILIATION", reconciliation_ready),
        ("DURABLE_OWNER_ARMING", routes_present and owner_arm_present),
        ("AUTONOMOUS_SERVE", routes_present),
        ("SELECTOR_FREE_ENTRY_TICKS", routes_present),
        ("ENTRY_CUTOFF_RECONCILIATION", routes_present),
        ("NEXT_DAY_LIFECYCLE_CLOSE", routes_present),
        ("LIFECYCLE_RECONCILIATION", routes_present),
    )
    return {
        "mode": "NO_WRITE_REHEARSAL",
        "account_role": AccountRole.SUBMISSION.value,
        "paper_only": True,
        "external_actions": False,
        "ready": all(ready for _, ready in stages),
        "stages": [
            {"code": code, "state": "READY" if ready else "MISSING"} for code, ready in stages
        ],
    }


def _reconciliation_ready(database_url: str) -> bool:
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    try:
        verify_schema(engine)
        repository = SQLAlchemyExecutionRepository(sessionmaker(engine, expire_on_commit=False))
        repository.get_reconciliation_state(AccountRole.SUBMISSION)
    except Exception:
        return False
    finally:
        engine.dispose()
    return True


def _validate_sealed_baseline(bound: _BoundFile) -> SealedSubmissionBaseline:
    if bound.path.name != "manifest.json":
        raise SubmissionLaunchRehearsalError("SUBMISSION_BASELINE_MANIFEST_INVALID")
    try:
        return load_submission_baseline_directory(bound.path.parent)
    except SubmissionBaselineImportError as error:
        raise SubmissionLaunchRehearsalError("SUBMISSION_BASELINE_MANIFEST_INVALID") from error


def _binding(
    root: Path,
    value: object,
    *,
    required: bool,
    allowed_modes: frozenset[int] = frozenset({0o600}),
) -> _BoundFile | None:
    if value is None:
        if required:
            raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_ARTIFACT_MISSING")
        return None
    if type(value) is not dict or set(value) != _BINDING_KEYS:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_BINDING_INVALID")
    path = _binding_path(root, value)
    expected = value["sha256"]
    if not _hash_value(expected):
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_BINDING_INVALID")
    try:
        payload = _read_private(path, allowed_modes=allowed_modes)
    except FileNotFoundError:
        if required:
            raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_ARTIFACT_MISSING") from None
        return None
    except OSError:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_PRIVATE_FILE_INVALID") from None
    if hashlib.sha256(payload).hexdigest() != expected:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_BINDING_MISMATCH")
    return _BoundFile(path, payload)


def _binding_path(root: Path, value: object) -> Path:
    if type(value) is not dict or set(value) != _BINDING_KEYS:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_BINDING_INVALID")
    raw = value["path"]
    if not _plain_string(raw) or Path(raw).is_absolute() or Path(raw).name.startswith(".env"):
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_BINDING_INVALID")
    path = root / raw
    if ".." in Path(raw).parts:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_BINDING_INVALID")
    return path


def _read_private(path: Path, *, allowed_modes: frozenset[int] = frozenset({0o600})) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) not in allowed_modes
            or metadata.st_nlink != 1
        ):
            raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_PRIVATE_FILE_INVALID")
        result = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _FILE_LIMIT + 1 - len(result)))
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            if len(result) > _FILE_LIMIT:
                raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_PRIVATE_FILE_INVALID")
    finally:
        os.close(descriptor)


def _validate_schedule(
    entry: object,
    lifecycle: object,
    *,
    entry_schedule_file: _BoundFile,
    lifecycle_schedule_file: _BoundFile,
    runtime_config_path: Path,
    selected_decision_boundary: datetime,
    last_entry_boundary: datetime,
) -> None:
    if type(entry) is not dict or set(entry) != _ENTRY_KEYS:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_SCHEDULE_INVALID")
    if type(lifecycle) is not dict or set(lifecycle) != _LIFECYCLE_KEYS:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_SCHEDULE_INVALID")
    try:
        starts_at = _time(entry["starts_at"])
        cutoff_at = _time(entry["cutoff_at"])
        entry_reconcile = _time(entry["reconcile_by"])
        close_at = _time(lifecycle["close_at"])
        close_reconcile = _time(lifecycle["reconcile_by"])
    except ValueError:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_SCHEDULE_INVALID") from None
    try:
        entry_schedule = load_schedule(
            entry_schedule_file.path,
            expected_sha256=hashlib.sha256(entry_schedule_file.payload).hexdigest(),
        )
        lifecycle_schedule = load_schedule(
            lifecycle_schedule_file.path,
            expected_sha256=hashlib.sha256(lifecycle_schedule_file.payload).hexdigest(),
        )
    except MarketWindowError as error:
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_SCHEDULE_INVALID") from error
    if (
        starts_at != selected_decision_boundary
        or cutoff_at != last_entry_boundary
        or not starts_at < cutoff_at <= entry_reconcile < close_at < close_reconcile
        or entry_schedule.runtime_config != runtime_config_path.absolute()
        or lifecycle_schedule.runtime_config != runtime_config_path.absolute()
        or entry_schedule.window_start != starts_at - timedelta(minutes=5)
        or entry_schedule.hard_cutoff != entry_reconcile
        or lifecycle_schedule.window_start != close_at
        or close_reconcile != close_at + timedelta(minutes=15)
        or lifecycle_schedule.hard_cutoff != close_reconcile + timedelta(minutes=5)
    ):
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_SCHEDULE_INVALID")
    if close_at.astimezone(_NEW_YORK).date() <= starts_at.astimezone(_NEW_YORK).date():
        raise SubmissionLaunchRehearsalError("SUBMISSION_LAUNCH_SCHEDULE_INVALID")


def _required_routes_present() -> bool:
    from backend.app.main import app

    routes = {
        (route.path, tuple(sorted(methods)))
        for route in app.routes
        if (methods := getattr(route, "methods", None)) is not None
    }
    return (
        ("/api/session", ("POST",)) in routes
        and ("/api/session", ("DELETE",)) in routes
        and ("/api/owner/autonomy/enable", ("POST",)) in routes
        and ("/api/owner/autonomy", ("GET",)) in routes
        and ("/api/internal/scheduler/tick", ("POST",)) in routes
    )


def _json(payload: bytes, code: str) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SubmissionLaunchRehearsalError(code) from None
    if type(value) is not dict:
        raise SubmissionLaunchRehearsalError(code)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _time(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    if (
        result.tzinfo is None
        or result.utcoffset() != UTC.utcoffset(result)
        or result.isoformat(timespec="seconds").replace("+00:00", "Z") != value
    ):
        raise ValueError
    return result


def _plain_string(value: object) -> bool:
    return type(value) is str and bool(value) and len(value) <= 256


def _hash_value(value: object) -> bool:
    return type(value) is str and _HASH.fullmatch(value) is not None


if __name__ == "__main__":
    raise SystemExit(main())
