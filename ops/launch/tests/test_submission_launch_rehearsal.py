from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest

from backend.app.alpaca.execution_evidence import baseline_account_fingerprint
from backend.app.alpaca.opportunity import OpportunitySnapshotRequest
from backend.app.contracts.v1 import AccountRole
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineSeal,
    OpportunityPlanSpec,
    opportunity_plan_identity,
)
from backend.app.policy.opportunity import OpportunityPolicy, opportunity_policy_hash
from backend.app.services.opportunity_bootstrap import (
    OpportunityBootstrapInput,
    opportunity_bootstrap_payload,
)
from ops.launch.submission_launch_rehearsal import (
    SubmissionLaunchRehearsalError,
    rehearse,
)
from ops.launch.submission_owner_arm import main as owner_arm_main


@pytest.fixture(autouse=True)
def _ready_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ops.launch.submission_launch_rehearsal._reconciliation_ready", lambda _url: True
    )


ACCOUNT = baseline_account_fingerprint(UUID("11111111-1111-1111-1111-111111111111"))
SUBMISSION_BASELINE_ID = uuid5(
    NAMESPACE_URL,
    f"alphadecay:baseline:{AccountRole.SUBMISSION.value}:{ACCOUNT}",
)
FROZEN = datetime(2026, 9, 1, 18, tzinfo=UTC)
ENTRY_START = datetime(2026, 9, 2, 13, 50, tzinfo=UTC)
ENTRY_CUTOFF = datetime(2026, 9, 2, 14, 15, tzinfo=UTC)


def _private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)


def _json(path: Path, payload: object) -> None:
    _private(path, json.dumps(payload, sort_keys=True).encode())


def _plan() -> OpportunityPlanSpec:
    policy = OpportunityPolicy(
        version="fixture-v1",
        opportunity_key="FIXTURE_STRATEGY_V1",
        underlying="ACME",
        selected_decision_boundary=ENTRY_START,
        last_entry_boundary=ENTRY_CUTOFF,
        maximum_decision_delay=timedelta(minutes=5),
        maximum_underlying_age=timedelta(minutes=2),
        maximum_catalyst_age=timedelta(days=1),
        maximum_option_quote_age=timedelta(seconds=20),
        maximum_leg_quote_skew=timedelta(seconds=3),
        minimum_vwap_distance=Decimal("0.01"),
        maximum_vwap_distance=Decimal("0.05"),
        minimum_relative_return=Decimal("0.01"),
        minimum_beta=Decimal("0.1"),
        maximum_beta=Decimal("3"),
        required_trend_hits=3,
        maximum_first_reaction=Decimal("0.2"),
        minimum_catalyst_score=50,
        minimum_candidate_score=50,
        minimum_dte=30,
        maximum_dte=45,
        maximum_relative_spread=Decimal("0.05"),
        minimum_debit_width_fraction=Decimal("0.1"),
        maximum_debit_width_fraction=Decimal("0.8"),
        minimum_credit_width_fraction=Decimal("0.1"),
        maximum_position_loss=Decimal("1125"),
        maximum_equity_risk_fraction=Decimal("0.01125"),
        maximum_lifetime_entries=10,
        maximum_lifetime_risk=Decimal("11250"),
        equity_floor=Decimal("99000"),
        maximum_quantity=5,
    )
    return OpportunityPlanSpec(
        opportunity_key=policy.opportunity_key,
        version=1,
        underlying="ACME",
        event_session=date(2026, 9, 1),
        pre_event_session=date(2026, 8, 31),
        reaction_session=date(2026, 9, 2),
        signal_session=date(2026, 9, 2),
        daily_start_session=date(2026, 7, 1),
        allowed_event_codes=("FIXTURE",),
        evidence_window_start=FROZEN,
        evidence_window_end=ENTRY_START,
        policy=policy,
        request_contract=OpportunitySnapshotRequest(
            account_role=AccountRole.SUBMISSION,
            expected_account_fingerprint=ACCOUNT,
            underlying="ACME",
            benchmark="QQQ",
            decision_boundary=ENTRY_START,
            minimum_expiry=date(2026, 10, 2),
            maximum_expiry=date(2026, 10, 16),
            minimum_strike=Decimal("50"),
            maximum_strike=Decimal("150"),
        ),
        thesis_code="FIXTURE_THESIS",
        thesis_target_contract={"target_kind": "session_close"},
        exposure_limit_contract={"shape": "defined_risk_vertical"},
        invalidation_codes=("FIXTURE_INVALID",),
        frozen_at=FROZEN,
        account_role=AccountRole.SUBMISSION,
    )


def _package(tmp_path: Path, *, include_generated: bool = True) -> Path:
    baseline = tmp_path / "sealed-baseline"
    payloads = {
        "account.json": json.dumps(
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "status": "ACTIVE",
                "equity": "100000",
                "cash": "100000",
                "trading_blocked": False,
                "account_blocked": False,
                "trade_suspended_by_user": False,
            },
            sort_keys=True,
        ).encode()
        + b"\n",
        "positions.json": b"[]\n",
        "orders.json": b"[]\n",
        "activities.json": json.dumps(
            [
                {
                    "id": "initial-funding-fixture",
                    "activity_type": "JNLC",
                    "date": "2026-08-28",
                    "net_amount": "100000",
                }
            ],
            sort_keys=True,
        ).encode()
        + b"\n",
    }
    hashes = {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()}
    for name, payload in payloads.items():
        _private(baseline / name, payload)
    _private(
        baseline / "hashes.sha256",
        "".join(f"{digest}  {name}\n" for name, digest in hashes.items()).encode(),
    )
    manifest = {
        "schema_version": "v1",
        "captured_at": "2026-08-28T15:13:00Z",
        "account_role": "SUBMISSION",
        "paper_endpoint_verified": True,
        "account_fingerprint": ACCOUNT,
        "account_hash": hashes["account.json"],
        "positions_hash": hashes["positions.json"],
        "orders_hash": hashes["orders.json"],
        "activities_hash": hashes["activities.json"],
        "equity": "100000",
        "cash": "100000",
        "positions_count": 0,
        "orders_count": 0,
        "activities_count": 1,
        "only_activity_type": "JNLC_INITIAL_FUNDING",
        "clean": True,
    }
    _json(baseline / "manifest.json", manifest)
    for path in baseline.iterdir():
        path.chmod(0o400)

    plan = _plan()
    plan_id, _ = opportunity_plan_identity(plan)
    seal = OpportunityBaselineSeal(
        plan_id=plan_id,
        account_fingerprint=ACCOUNT,
        account_source_hash="1" * 64,
        positions_manifest=(),
        positions_source_hash="2" * 64,
        orders_manifest=(),
        orders_source_hash="3" * 64,
        activity_manifest=(),
        activity_source_hash="4" * 64,
        book_hash="5" * 64,
        history_hash="6" * 64,
        captured_at=FROZEN + timedelta(minutes=1),
        account_role=AccountRole.SUBMISSION,
        submission_baseline_id=SUBMISSION_BASELINE_ID,
    )
    bootstrap = opportunity_bootstrap_payload(OpportunityBootstrapInput(plan, seal))
    plan_path = tmp_path / "plan.json"
    capture_path = tmp_path / "capture.json"
    _json(plan_path, bootstrap["plan"])
    if include_generated:
        _json(capture_path, bootstrap)
        _json(
            tmp_path / "import-receipt.json",
            {
                "account_role": "SUBMISSION",
                "database_write": True,
                "manifest_valid": True,
                "mode": "PERSIST",
                "source_files_verified": 4,
            },
        )

    database_url = "postgresql://fixture:fixture@127.0.0.1/alphadecay"
    _private(tmp_path / "database-url", database_url.encode())
    runtime = {
        "APP_ACCOUNT_ROLE": "SUBMISSION",
        "APP_POLICY_HASH": opportunity_policy_hash(plan.policy),
        "APP_CALIBRATION_HASH": opportunity_policy_hash(plan.policy),
        "APP_CALIBRATION_DECISION_BOUNDARY": "2026-09-01T18:00:00Z",
        "APP_CALIBRATION_SEALED_AT": "2026-09-01T18:00:00Z",
        "APP_ENTRY_EQUITY_FLOOR": "99000",
        "APP_MAXIMUM_LIFETIME_ENTRIES": "10",
        "APP_MAXIMUM_LIFETIME_RISK": "11250",
        "APP_MAXIMUM_POSITION_LOSS": "1125",
        "APP_MAXIMUM_ENTRY_QUANTITY": "5",
        "APP_OPPORTUNITY_KEY": plan.opportunity_key,
        "APP_OPPORTUNITY_PLAN_VERSION": "1",
        "APP_HALT_MAXIMUM_TRADE_AGE_SECONDS": "30",
        "ALPACA_API_ENDPOINT": "https://paper-api.alpaca.markets",
        "ALPACA_API_KEY": "fixture-paper-key",
        "ALPACA_SECRET_KEY": "fixture-paper-secret",
        "ALPACA_PAPER_TRADE": "true",
        "DATABASE_URL": database_url,
        "GEMINI_API_KEY": "fixture-model-key",
        "APP_OWNER_ACCESS_CODE": "o" * 16,
        "APP_SESSION_SECRET": "s" * 32,
        "APP_PROVIDER_SETTINGS_SECRET": "p" * 32,
        "APP_OPENAI_COMPATIBLE_ORIGINS": "",
        "APP_ALLOWED_ORIGIN": "https://fixture.invalid",
        "SCHEDULER_TOKEN": "t" * 32,
    }
    _json(tmp_path / "runtime.json", runtime)
    _json(
        tmp_path / "entry-schedule.json",
        {
            "runtime_config": str(tmp_path / "runtime.json"),
            "window_start": "2026-09-02T09:45:00-04:00",
            "hard_cutoff": "2026-09-02T10:25:00-04:00",
            "cadence_seconds": 300,
        },
    )
    _json(
        tmp_path / "lifecycle-schedule.json",
        {
            "runtime_config": str(tmp_path / "runtime.json"),
            "window_start": "2026-09-03T09:45:00-04:00",
            "hard_cutoff": "2026-09-03T10:05:00-04:00",
            "cadence_seconds": 300,
        },
    )

    def binding(path: Path) -> dict[str, str]:
        return {
            "path": str(path.relative_to(tmp_path)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    package = {
        "schema_version": "v1",
        "account_role": "SUBMISSION",
        "strategy_code": plan.opportunity_key,
        "judged_baseline_manifest": binding(baseline / "manifest.json"),
        "baseline_import_receipt": (
            binding(tmp_path / "import-receipt.json") if include_generated else None
        ),
        "opportunity_plan": binding(plan_path),
        "opportunity_capture": binding(capture_path) if include_generated else None,
        "database_url": binding(tmp_path / "database-url"),
        "runtime_config": binding(tmp_path / "runtime.json"),
        "entry_schedule": binding(tmp_path / "entry-schedule.json"),
        "lifecycle_schedule": binding(tmp_path / "lifecycle-schedule.json"),
        "entry_window": {
            "starts_at": "2026-09-02T13:50:00Z",
            "cutoff_at": "2026-09-02T14:15:00Z",
            "reconcile_by": "2026-09-02T14:25:00Z",
        },
        "lifecycle_window": {
            "close_at": "2026-09-03T13:45:00Z",
            "reconcile_by": "2026-09-03T14:00:00Z",
        },
    }
    package_path = tmp_path / "launch-package.json"
    _json(package_path, package)
    return package_path


def test_complete_package_rehearses_every_stage_without_external_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path)
    monkeypatch.setattr(
        "ops.launch.submission_launch_rehearsal.importlib.util.find_spec",
        lambda name: (
            object()
            if name in {"ops.launch.submission_baseline_import", "ops.launch.submission_owner_arm"}
            else None
        ),
    )

    result = rehearse(package)

    assert result["ready"] is True
    assert result["external_actions"] is False
    assert {stage["state"] for stage in result["stages"]} == {"READY"}
    serialized = json.dumps(result)
    assert "ACME" not in serialized
    assert "fixture-paper" not in serialized
    assert "postgresql" not in serialized


def test_rehearsal_uses_only_read_file_descriptors_and_no_network_or_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path)
    real_open = os.open
    seen_flags: list[int] = []

    def read_only_open(path, flags, *args, **kwargs):
        seen_flags.append(flags)
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return real_open(path, flags, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("rehearsal cannot use a network or database")

    monkeypatch.setattr("ops.launch.submission_launch_rehearsal.os.open", read_only_open)
    monkeypatch.setattr("ops.launch.submission_runtime.create_engine", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(
        "ops.launch.submission_launch_rehearsal.importlib.util.find_spec",
        lambda _name: object(),
    )

    assert rehearse(package)["ready"] is True
    assert seen_flags


def test_preparation_package_reports_generated_dependencies_missing(tmp_path: Path) -> None:
    result = rehearse(_package(tmp_path, include_generated=False))

    assert result["ready"] is False
    states = {stage["code"]: stage["state"] for stage in result["stages"]}
    assert states["JUDGED_BASELINE_IMPORT"] == "MISSING"
    assert states["READ_ONLY_BASELINE_CAPTURE"] == "MISSING"
    assert states["OPPORTUNITY_PERSISTENCE"] == "MISSING"
    assert states["RUNTIME_PREFLIGHT"] == "MISSING"
    assert states["EXACT_SUBMISSION_PLAN"] == "READY"


def test_rehearsal_reports_missing_reconciliation_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ops.launch.submission_launch_rehearsal._reconciliation_ready", lambda _url: False
    )

    result = rehearse(_package(tmp_path))

    assert result["ready"] is False
    states = {stage["code"]: stage["state"] for stage in result["stages"]}
    assert states["RECONCILIATION"] == "MISSING"


def test_import_receipt_requires_the_exact_stable_values(tmp_path: Path) -> None:
    package = _package(tmp_path)
    receipt = tmp_path / "import-receipt.json"
    value = json.loads(receipt.read_text())
    value["source_files_verified"] = 3
    _json(receipt, value)
    package_value = json.loads(package.read_text())
    package_value["baseline_import_receipt"]["sha256"] = hashlib.sha256(
        receipt.read_bytes()
    ).hexdigest()
    _json(package, package_value)

    with pytest.raises(
        SubmissionLaunchRehearsalError,
        match="SUBMISSION_BASELINE_IMPORT_RECEIPT_INVALID",
    ):
        rehearse(package)


@pytest.mark.parametrize(
    "mutation",
    [
        "manifest_key",
        "payload_hash",
        "hash_parent",
        "hash_prefix",
        "public_mode",
        "boolean_count",
    ],
)
def test_existing_baseline_contract_is_exact_and_owner_only(tmp_path: Path, mutation: str) -> None:
    package = _package(tmp_path)
    baseline = tmp_path / "sealed-baseline"
    if mutation == "manifest_key":
        value = json.loads((baseline / "manifest.json").read_text())
        value["invented_envelope"] = True
        (baseline / "manifest.json").chmod(0o600)
        _json(baseline / "manifest.json", value)
    elif mutation == "payload_hash":
        (baseline / "positions.json").chmod(0o600)
        _private(baseline / "positions.json", b"[{}]\n")
    elif mutation == "hash_parent":
        path = baseline / "hashes.sha256"
        path.chmod(0o600)
        path.write_text(path.read_text().replace("account.json", "other/account.json"))
        path.chmod(0o600)
    elif mutation == "hash_prefix":
        path = baseline / "hashes.sha256"
        path.chmod(0o600)
        path.write_text(path.read_text().replace("positions.json", "nested/positions.json"))
        path.chmod(0o600)
    elif mutation == "boolean_count":
        value = json.loads((baseline / "manifest.json").read_text())
        value["positions_count"] = False
        (baseline / "manifest.json").chmod(0o600)
        _json(baseline / "manifest.json", value)
    else:
        (baseline / "orders.json").chmod(0o644)

    with pytest.raises(SubmissionLaunchRehearsalError):
        rehearse(package)


def test_rejects_links_hardlinks_env_names_and_package_selectors(tmp_path: Path) -> None:
    package = _package(tmp_path)
    value = json.loads(package.read_text())
    value["symbol"] = "ACME"
    _json(package, value)
    with pytest.raises(SubmissionLaunchRehearsalError):
        rehearse(package)

    package = _package(tmp_path / "links")
    runtime = package.parent / "runtime.json"
    link = package.parent / "runtime-link.json"
    link.symlink_to(runtime.name)
    value = json.loads(package.read_text())
    value["runtime_config"] = {
        "path": link.name,
        "sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
    }
    _json(package, value)
    with pytest.raises(SubmissionLaunchRehearsalError):
        rehearse(package)

    hardlink = package.parent / "runtime-hardlink.json"
    os.link(runtime, hardlink)
    value["runtime_config"]["path"] = hardlink.name
    _json(package, value)
    with pytest.raises(SubmissionLaunchRehearsalError):
        rehearse(package)

    value["runtime_config"]["path"] = ".env.runtime"
    _json(package, value)
    with pytest.raises(SubmissionLaunchRehearsalError):
        rehearse(package)


def test_rejects_cross_file_authority_and_non_next_day_close(tmp_path: Path) -> None:
    package = _package(tmp_path)
    value = json.loads(package.read_text())
    value["strategy_code"] = "OTHER_STRATEGY"
    _json(package, value)
    with pytest.raises(SubmissionLaunchRehearsalError, match="AUTHORITY"):
        rehearse(package)

    package = _package(tmp_path / "schedule")
    value = json.loads(package.read_text())
    value["lifecycle_window"] = {
        "close_at": "2026-09-02T15:00:00Z",
        "reconcile_by": "2026-09-02T15:10:00Z",
    }
    _json(package, value)
    with pytest.raises(SubmissionLaunchRehearsalError, match="SCHEDULE"):
        rehearse(package)

    package = _package(tmp_path / "unbound-schedule")
    value = json.loads(package.read_text())
    value["entry_window"]["starts_at"] = "2026-09-02T13:51:00Z"
    _json(package, value)
    with pytest.raises(SubmissionLaunchRehearsalError, match="SCHEDULE"):
        rehearse(package)

    package = _package(tmp_path / "noncanonical-schedule")
    value = json.loads(package.read_text())
    value["entry_window"]["starts_at"] = "2026-09-02T13:50:00.000000Z"
    _json(package, value)
    with pytest.raises(SubmissionLaunchRehearsalError, match="SCHEDULE"):
        rehearse(package)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("APP_CALIBRATION_HASH", "c" * 64),
        ("APP_CALIBRATION_DECISION_BOUNDARY", "2026-09-01T17:59:00Z"),
        ("APP_CALIBRATION_SEALED_AT", "2026-09-01T18:01:00Z"),
    ],
)
def test_rejects_calibration_authority_not_frozen_with_plan(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    package = _package(tmp_path)
    runtime = tmp_path / "runtime.json"
    runtime_value = json.loads(runtime.read_text())
    runtime_value[key] = value
    _json(runtime, runtime_value)
    package_value = json.loads(package.read_text())
    package_value["runtime_config"]["sha256"] = hashlib.sha256(runtime.read_bytes()).hexdigest()
    _json(package, package_value)

    with pytest.raises(SubmissionLaunchRehearsalError, match="AUTHORITY"):
        rehearse(package)


def test_rejects_unbound_controller_schedule_or_runtime(tmp_path: Path) -> None:
    package = _package(tmp_path)
    package_value = json.loads(package.read_text())
    entry_schedule = tmp_path / "entry-schedule.json"
    entry_value = json.loads(entry_schedule.read_text())
    entry_value["window_start"] = "2026-09-02T09:50:00-04:00"
    _json(entry_schedule, entry_value)
    package_value["entry_schedule"]["sha256"] = hashlib.sha256(
        entry_schedule.read_bytes()
    ).hexdigest()
    _json(package, package_value)

    with pytest.raises(SubmissionLaunchRehearsalError, match="SCHEDULE"):
        rehearse(package)

    package = _package(tmp_path / "runtime-mismatch")
    package_value = json.loads(package.read_text())
    lifecycle_schedule = package.parent / "lifecycle-schedule.json"
    lifecycle_value = json.loads(lifecycle_schedule.read_text())
    lifecycle_value["runtime_config"] = str(package.parent / "other-runtime.json")
    _json(lifecycle_schedule, lifecycle_value)
    package_value["lifecycle_schedule"]["sha256"] = hashlib.sha256(
        lifecycle_schedule.read_bytes()
    ).hexdigest()
    _json(package, package_value)

    with pytest.raises(SubmissionLaunchRehearsalError, match="SCHEDULE"):
        rehearse(package)


def test_rejects_lifecycle_window_without_post_cancel_reconciliation_tick(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    package_value = json.loads(package.read_text())
    lifecycle_schedule = tmp_path / "lifecycle-schedule.json"
    lifecycle_value = json.loads(lifecycle_schedule.read_text())
    lifecycle_value["hard_cutoff"] = "2026-09-03T10:00:00-04:00"
    _json(lifecycle_schedule, lifecycle_value)
    package_value["lifecycle_schedule"]["sha256"] = hashlib.sha256(
        lifecycle_schedule.read_bytes()
    ).hexdigest()
    package_value["lifecycle_window"]["reconcile_by"] = "2026-09-03T13:55:00Z"
    _json(package, package_value)

    with pytest.raises(SubmissionLaunchRehearsalError, match="SCHEDULE"):
        rehearse(package)


def test_capture_must_reference_the_deterministic_judged_baseline_id(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    capture = tmp_path / "capture.json"
    value = json.loads(capture.read_text())
    value["submission_baseline_id"] = "22222222-2222-2222-2222-222222222222"
    _json(capture, value)
    package_value = json.loads(package.read_text())
    package_value["opportunity_capture"]["sha256"] = hashlib.sha256(
        capture.read_bytes()
    ).hexdigest()
    _json(package, package_value)

    with pytest.raises(SubmissionLaunchRehearsalError, match="AUTHORITY"):
        rehearse(package)


@pytest.mark.parametrize(
    ("target", "needle"),
    [
        ("package", '"schema_version": "v1"'),
        ("plan", '"account_role": "SUBMISSION"'),
        ("capture", '"account_role": "SUBMISSION"'),
        ("receipt", '"mode": "PERSIST"'),
        ("runtime", '"APP_ACCOUNT_ROLE": "SUBMISSION"'),
    ],
)
def test_rejects_duplicate_keys_in_every_bound_json(
    tmp_path: Path, target: str, needle: str
) -> None:
    package = _package(tmp_path)
    paths = {
        "package": package,
        "plan": tmp_path / "plan.json",
        "capture": tmp_path / "capture.json",
        "receipt": tmp_path / "import-receipt.json",
        "runtime": tmp_path / "runtime.json",
    }
    path = paths[target]
    text = path.read_text()
    duplicate = needle + "," + needle
    assert needle in text
    _private(path, text.replace(needle, duplicate, 1).encode())
    if target != "package":
        value = json.loads(package.read_text())
        binding_key = {
            "plan": "opportunity_plan",
            "capture": "opportunity_capture",
            "receipt": "baseline_import_receipt",
            "runtime": "runtime_config",
        }[target]
        value[binding_key]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        _json(package, value)
    with pytest.raises(SubmissionLaunchRehearsalError):
        rehearse(package)


def test_owner_arm_defaults_to_no_network_preview(tmp_path: Path, capsys) -> None:
    package = _package(tmp_path)
    calls = 0

    def forbidden(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("preview must not call loopback")

    assert (
        owner_arm_main(
            ["--config", str(package.parent / "runtime.json")],
            transport=httpx.MockTransport(forbidden),
        )
        == 0
    )
    assert calls == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "account_role": "SUBMISSION",
        "effective": False,
        "fixed_loopback": True,
        "mode": "NO_NETWORK_PREVIEW",
        "selector_free": True,
    }


def test_owner_arm_uses_short_secure_session_and_verifies_effective_state(
    tmp_path: Path, capsys
) -> None:
    package = _package(tmp_path)
    seen: list[tuple[str, str]] = []
    expected_status = {
        "schema_version": "v1",
        "role": "SUBMISSION",
        "server_enabled": True,
        "account_enabled": True,
        "effective": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 8000
        assert request.url.query == b""
        assert request.headers["origin"] == "https://fixture.invalid"
        if request.url.path == "/api/session" and request.method == "POST":
            assert json.loads(request.content) == {"access_code": "o" * 16}
            return httpx.Response(
                200,
                json={
                    "schema_version": "v1",
                    "authenticated": True,
                    "expires_at": "2026-09-01T18:15:00Z",
                },
                headers=[
                    (
                        "set-cookie",
                        "__Host-alphadecay_session=session-token; Path=/; Secure; "
                        "HttpOnly; SameSite=strict; Max-Age=900",
                    ),
                    (
                        "set-cookie",
                        "__Host-alphadecay_csrf=csrf-token; Path=/; Secure; "
                        "SameSite=strict; Max-Age=900",
                    ),
                ],
            )
        assert request.headers["x-csrf-token"] == "csrf-token"
        assert request.headers["cookie"] == (
            "__Host-alphadecay_session=session-token; __Host-alphadecay_csrf=csrf-token"
        )
        if request.url.path == "/api/session" and request.method == "DELETE":
            return httpx.Response(
                200,
                json={"schema_version": "v1", "authenticated": False, "expires_at": None},
            )
        assert request.content == b""
        return httpx.Response(200, json=expected_status)

    assert (
        owner_arm_main(
            ["--config", str(package.parent / "runtime.json"), "--apply"],
            transport=httpx.MockTransport(handler),
        )
        == 0
    )
    assert seen == [
        ("POST", "/api/session"),
        ("POST", "/api/owner/autonomy/enable"),
        ("GET", "/api/owner/autonomy"),
        ("DELETE", "/api/session"),
    ]
    output = capsys.readouterr().out
    assert json.loads(output)["effective"] is True
    assert "session-token" not in output
    assert "csrf-token" not in output
    assert "fixture-paper" not in output


def test_owner_arm_rejects_ineffective_or_selector_shaped_calls(tmp_path: Path, capsys) -> None:
    package = _package(tmp_path)
    seen: list[tuple[str, str]] = []

    def ineffective(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/session" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "schema_version": "v1",
                    "authenticated": True,
                    "expires_at": "2026-09-01T18:15:00Z",
                },
                headers=[
                    (
                        "set-cookie",
                        "__Host-alphadecay_session=session-token; Path=/; Secure; "
                        "HttpOnly; SameSite=strict; Max-Age=900",
                    ),
                    (
                        "set-cookie",
                        "__Host-alphadecay_csrf=csrf-token; Path=/; Secure; "
                        "SameSite=strict; Max-Age=900",
                    ),
                ],
            )
        if request.url.path == "/api/session" and request.method == "DELETE":
            return httpx.Response(
                200,
                json={"schema_version": "v1", "authenticated": False, "expires_at": None},
            )
        return httpx.Response(
            200,
            json={
                "schema_version": "v1",
                "role": "SUBMISSION",
                "server_enabled": True,
                "account_enabled": False,
                "effective": False,
            },
        )

    with pytest.raises(SystemExit):
        owner_arm_main(
            ["--config", str(package.parent / "runtime.json"), "--apply"],
            transport=httpx.MockTransport(ineffective),
        )
    assert "SUBMISSION_OWNER_ARM_NOT_EFFECTIVE" in capsys.readouterr().err
    assert seen == [
        ("POST", "/api/session"),
        ("POST", "/api/owner/autonomy/enable"),
        ("DELETE", "/api/session"),
    ]

    with pytest.raises(SystemExit):
        owner_arm_main(
            [
                "--config",
                str(package.parent / "runtime.json"),
                "--symbol",
                "ACME",
            ]
        )


def test_owner_arm_logs_out_after_partial_observation_failure(tmp_path: Path, capsys) -> None:
    package = _package(tmp_path)
    seen: list[tuple[str, str]] = []
    status = {
        "schema_version": "v1",
        "role": "SUBMISSION",
        "server_enabled": True,
        "account_enabled": True,
        "effective": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/session" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "schema_version": "v1",
                    "authenticated": True,
                    "expires_at": "2026-09-01T18:15:00Z",
                },
                headers=[
                    (
                        "set-cookie",
                        "__Host-alphadecay_session=session-token; Path=/; Secure; "
                        "HttpOnly; SameSite=strict; Max-Age=900",
                    ),
                    (
                        "set-cookie",
                        "__Host-alphadecay_csrf=csrf-token; Path=/; Secure; "
                        "SameSite=strict; Max-Age=900",
                    ),
                ],
            )
        if request.url.path == "/api/owner/autonomy/enable":
            return httpx.Response(200, json=status)
        if request.url.path == "/api/owner/autonomy":
            return httpx.Response(200, content=b"{" + b"x" * (64 * 1024) + b"}")
        return httpx.Response(
            200,
            json={"schema_version": "v1", "authenticated": False, "expires_at": None},
        )

    with pytest.raises(SystemExit):
        owner_arm_main(
            ["--config", str(package.parent / "runtime.json"), "--apply"],
            transport=httpx.MockTransport(handler),
        )
    assert "SUBMISSION_OWNER_RESPONSE_INVALID" in capsys.readouterr().err
    assert seen[-1] == ("DELETE", "/api/session")


@pytest.mark.parametrize(
    "cookie_mutation",
    ["missing_secure", "missing_httponly", "csrf_httponly", "long_lived"],
)
def test_owner_arm_rejects_wrong_cookie_security_attributes(
    tmp_path: Path, capsys, cookie_mutation: str
) -> None:
    package = _package(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        session = (
            "__Host-alphadecay_session=session-token; Path=/; Secure; HttpOnly; "
            "SameSite=strict; Max-Age=900"
        )
        csrf = "__Host-alphadecay_csrf=csrf-token; Path=/; Secure; SameSite=strict; Max-Age=900"
        if cookie_mutation == "missing_secure":
            session = session.replace("; Secure", "")
        elif cookie_mutation == "missing_httponly":
            session = session.replace("; HttpOnly", "")
        elif cookie_mutation == "csrf_httponly":
            csrf += "; HttpOnly"
        else:
            session = session.replace("Max-Age=900", "Max-Age=3600")
        return httpx.Response(
            200,
            json={
                "schema_version": "v1",
                "authenticated": True,
                "expires_at": "2026-09-01T18:15:00Z",
            },
            headers=[("set-cookie", session), ("set-cookie", csrf)],
        )

    with pytest.raises(SystemExit):
        owner_arm_main(
            ["--config", str(package.parent / "runtime.json"), "--apply"],
            transport=httpx.MockTransport(handler),
        )
    assert "SUBMISSION_OWNER_SESSION_FAILED" in capsys.readouterr().err
