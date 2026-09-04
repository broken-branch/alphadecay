from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ops.launch.cli_bootstrap import (
    BootstrapError,
    CliPin,
    Credentials,
    PinnedCli,
    bootstrap,
)

NOW = datetime(2026, 8, 31, 15, 0, 10, tzinfo=UTC)
PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
CREDENTIAL_FINGERPRINT_DOMAIN = b"alphadecay.cli-bootstrap.credential.v1\0"


def _doctor() -> str:
    return "\n".join(
        [
            "Alpaca CLI 0.0.13",
            "  ✓ no saved profiles configured (using env var credentials)",
            "  ✓ active profile: paper",
            "  ✓ API key credentials from env (ALPACA_API_KEY + ALPACA_SECRET_KEY)",
            "Connectivity:",
            f"  Trading:  {PAPER_ENDPOINT}",
            "  ✓ trading API: connected",
            "  Data:     https://data.alpaca.markets",
            "  ✓ data API: connected",
            "All checks passed.",
        ]
    )


def _request(intent_digest: str) -> dict[str, object]:
    return {
        "client_order_id": f"ad-20260831-e-{intent_digest[:24]}-a0",
        "legs": [
            {
                "symbol": "NVDA260918C00230000",
                "ratio_qty": "1",
                "position_intent": "buy_to_open",
            },
            {
                "symbol": "NVDA260918C00240000",
                "ratio_qty": "1",
                "position_intent": "sell_to_open",
            },
        ],
        "limit_price": "3.25",
        "order_class": "mleg",
        "qty": "2",
        "time_in_force": "day",
        "type": "limit",
    }


def _approval() -> dict[str, object]:
    intent_digest = "a" * 64
    order = _request(intent_digest)
    approval = {
        "schema_version": "alphadecay.cli-bootstrap-approval.v1",
        "account_role": "DEVELOPMENT",
        "intent_digest": intent_digest,
        "order_sha256": hashlib.sha256(
            json.dumps(order, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
        "policy_hash": "c" * 64,
        "book_fingerprint": "d" * 64,
        "credential_fingerprint": hashlib.sha256(
            CREDENTIAL_FINGERPRINT_DOMAIN + b"key"
        ).hexdigest(),
        "trade_date": "2026-08-31",
        "valid_until": "2026-08-31T15:01:00Z",
        "paper_endpoint": PAPER_ENDPOINT,
        "paper_trade": True,
        "order": order,
        "submit_preconditions": {
            "checked_at": "2026-08-31T15:00:00Z",
            "expires_at": "2026-08-31T15:00:30Z",
            "book_fingerprint": "d" * 64,
            "account_role": "DEVELOPMENT",
            "market_open": True,
            "buying_power_verified": True,
            "positions_orders_reconciled": True,
            "quotes_fresh": True,
            "risk_rechecked": True,
            "idempotency_clear": True,
            "submit_authorized": True,
        },
    }
    return _rehash_approval(approval)


def _rehash_approval(approval: dict[str, object]) -> dict[str, object]:
    payload = {key: value for key, value in approval.items() if key != "approval_hash"}
    approval["approval_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return approval


def _archive(tmp_path: Path) -> tuple[Path, CliPin]:
    binary = b"#!/bin/sh\nexit 0\n"
    archive = tmp_path / "alpaca.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("alpaca")
        member.mode = 0o755
        member.size = len(binary)
        bundle.addfile(member, io.BytesIO(binary))
    return archive, CliPin(
        version="0.0.13",
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        binary_sha256=hashlib.sha256(binary).hexdigest(),
        archive_member="alpaca",
    )


@dataclass
class FakeRunner:
    outputs: list[str]
    calls: list[tuple[int, tuple[str, ...], dict[str, str]]] = field(default_factory=list)

    def run_fd(self, descriptor, argv, environment):
        self.calls.append((descriptor, argv, dict(environment)))
        output = self.outputs.pop(0)
        if output == "__FAIL__":
            raise BootstrapError("CLI_PROCESS_FAILED")
        return output


def _runner(*, submit: bool = False, dry_run: dict[str, object] | None = None) -> FakeRunner:
    outputs = ["0.0.13\n", _doctor(), json.dumps(dry_run or _request("a" * 64))]
    if submit:
        outputs += [
            "0.0.13\n",
            _doctor(),
            json.dumps(
                {
                    "id": "broker-order-id-must-not-be-persisted",
                    "account_id": "account-id-must-not-be-persisted",
                    "client_order_id": _request("a" * 64)["client_order_id"],
                    "status": "accepted",
                }
            ),
        ]
    return FakeRunner(outputs)


def _run(
    tmp_path: Path,
    *,
    execute: bool = False,
    approval: dict[str, object] | None = None,
    runner: FakeRunner | None = None,
    ambient: dict[str, str] | None = None,
):
    archive, pin = _archive(tmp_path)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval or _approval()))
    approval_path.chmod(0o600)
    artifacts = tmp_path / "artifacts"
    process = runner or _runner(submit=execute)
    cli = PinnedCli.from_archive(archive, pin, process)
    result = bootstrap(
        approval_path=approval_path,
        artifact_directory=artifacts,
        cli=cli,
        credentials=Credentials("key", "secret"),
        execute_approved_entry=execute,
        ambient_environment=ambient or {},
        now=lambda: NOW,
    )
    return result, process, artifacts


def test_preview_is_default_and_runs_identity_doctor_then_dry_run(tmp_path: Path) -> None:
    result, process, artifacts = _run(tmp_path)
    assert result.outcome == "DRY_RUN_VERIFIED"
    assert [call[1][1:3] for call in process.calls] == [
        ("version",),
        ("doctor", "--quiet"),
        ("order", "submit"),
    ]
    assert process.calls[-1][1][-1] == "--dry-run"
    assert set(path.name for path in artifacts.iterdir()) == {"request.json", "provenance.json"}


def test_execute_reverifies_identity_and_paper_doctor_immediately_before_submit(
    tmp_path: Path,
) -> None:
    result, process, artifacts = _run(tmp_path, execute=True)
    assert result.outcome == "SUBMIT_DISPATCHED"
    assert [call[1][1:3] for call in process.calls] == [
        ("version",),
        ("doctor", "--quiet"),
        ("order", "submit"),
        ("version",),
        ("doctor", "--quiet"),
        ("order", "submit"),
    ]
    assert "--dry-run" not in process.calls[-1][1]
    persisted = "".join(path.read_text() for path in artifacts.iterdir())
    assert "broker-order-id" not in persisted
    assert "account-id" not in persisted
    assert "credential_fingerprint" not in persisted
    assert '"status"' not in (artifacts / "result.json").read_text()
    assert '"outcome":"DISPATCH_RESPONSE_RECEIVED"' in (artifacts / "result.json").read_text()
    assert "key" not in persisted and "secret" not in persisted
    assert set(path.name for path in artifacts.iterdir()) == {
        "request.json",
        "provenance.json",
        "result.json",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ALPACA_LIVE_TRADE", "true"),
        ("APCA_API_BASE_URL", PAPER_ENDPOINT),
        ("ALPACA_API_ENDPOINT", PAPER_ENDPOINT),
        ("ALPACA_API_URL", PAPER_ENDPOINT),
        ("ALPACA_PROFILE", "paper"),
        ("APCA_API_PROFILE", "paper"),
    ],
)
def test_rejects_every_ambient_endpoint_live_or_profile_selector(
    tmp_path: Path, key: str, value: str
) -> None:
    with pytest.raises(BootstrapError, match="CLI_AMBIENT_AUTHORITY_REJECTED"):
        _run(tmp_path, ambient={key: value})


def test_child_environment_is_frozen_and_contains_no_selector(tmp_path: Path) -> None:
    _, process, _ = _run(tmp_path, execute=True)
    for _, argv, environment in process.calls:
        assert environment == {
            "ALPACA_API_KEY": "key",
            "ALPACA_SECRET_KEY": "secret",
            "ALPACA_LIVE_TRADE": "false",
            "LC_ALL": "C.UTF-8",
        }
        assert not any(
            forbidden in argv
            for forbidden in (
                "-p",
                "--profile",
                "market",
                "cancel-all",
                "close-all",
                "replace",
            )
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(paper_endpoint="https://api.alpaca.markets"),
        lambda value: value.update(paper_trade=False),
        lambda value: value["order"].update(type="market"),
        lambda value: value["order"].update(order_class="simple"),
        lambda value: value["order"].update(qty="7"),
        lambda value: value["order"]["legs"].append(value["order"]["legs"][0]),
        lambda value: value["order"].update(client_order_id="operator-edited"),
        lambda value: value.update(extra="not-allowed"),
    ],
)
def test_rejects_mutated_or_unbounded_approval_before_cli(tmp_path: Path, mutation) -> None:
    approval = _approval()
    mutation(approval)
    process = _runner()
    with pytest.raises(BootstrapError, match="APPROVAL_INVALID"):
        _run(tmp_path, approval=approval, runner=process)
    assert process.calls == []


def test_rejects_forged_approval_hash_before_cli(tmp_path: Path) -> None:
    approval = _approval()
    approval["approval_hash"] = "b" * 64
    process = _runner()
    with pytest.raises(BootstrapError, match="APPROVAL_INVALID"):
        _run(tmp_path, approval=approval, runner=process)
    assert process.calls == []


def test_rejects_quantity_above_public_structural_bound(tmp_path: Path) -> None:
    approval = _approval()
    approval["order"]["qty"] = "101"
    approval["order_sha256"] = hashlib.sha256(
        json.dumps(approval["order"], sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    _rehash_approval(approval)
    process = _runner()
    with pytest.raises(BootstrapError, match="APPROVAL_INVALID"):
        _run(tmp_path, approval=approval, runner=process)
    assert process.calls == []


def test_rejects_credentials_not_bound_to_approval_before_cli(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(_approval()))
    approval_path.chmod(0o600)
    process = _runner()
    cli = PinnedCli.from_archive(archive, pin, process)
    with pytest.raises(BootstrapError, match="CREDENTIAL_AUTHORITY_MISMATCH"):
        bootstrap(
            approval_path=approval_path,
            artifact_directory=tmp_path / "artifacts",
            cli=cli,
            credentials=Credentials("different-key", "secret"),
            ambient_environment={},
            now=lambda: NOW,
        )
    assert process.calls == []
    assert not (tmp_path / "artifacts").exists()


def test_dry_run_must_exactly_match_approved_envelope(tmp_path: Path) -> None:
    dry_run = _request("a" * 64)
    dry_run["limit_price"] = "3.26"
    process = _runner(dry_run=dry_run)
    with pytest.raises(BootstrapError, match="CLI_DRY_RUN_MISMATCH"):
        _run(tmp_path, runner=process)
    assert len(process.calls) == 3


def test_duplicate_dry_run_key_is_rejected(tmp_path: Path) -> None:
    duplicate = json.dumps(_request("a" * 64)).replace('"qty": "2"', '"qty": "99", "qty": "2"')
    process = FakeRunner(["0.0.13\n", _doctor(), duplicate])
    with pytest.raises(BootstrapError, match="CLI_DRY_RUN_INVALID"):
        _run(tmp_path, runner=process)


@pytest.mark.parametrize(
    "field",
    [
        "market_open",
        "buying_power_verified",
        "positions_orders_reconciled",
        "quotes_fresh",
        "risk_rechecked",
        "idempotency_clear",
        "submit_authorized",
    ],
)
def test_execute_requires_every_fresh_exact_submit_precondition(tmp_path: Path, field: str) -> None:
    approval = _approval()
    approval["submit_preconditions"][field] = False
    _rehash_approval(approval)
    process = _runner(submit=True)
    with pytest.raises(BootstrapError, match="SUBMIT_PRECONDITION_FAILED"):
        _run(tmp_path, execute=True, approval=approval, runner=process)
    assert process.calls == []


def test_execute_rejects_expired_preconditions_without_cli(tmp_path: Path) -> None:
    approval = _approval()
    approval["submit_preconditions"]["expires_at"] = "2026-08-31T15:00:09Z"
    _rehash_approval(approval)
    process = _runner(submit=True)
    with pytest.raises(BootstrapError, match="SUBMIT_PRECONDITION_FAILED"):
        _run(tmp_path, execute=True, approval=approval, runner=process)
    assert process.calls == []


def test_preview_does_not_require_submit_authority(tmp_path: Path) -> None:
    approval = _approval()
    approval["submit_preconditions"]["submit_authorized"] = False
    _rehash_approval(approval)
    result, process, _ = _run(tmp_path, approval=approval)
    assert result.outcome == "DRY_RUN_VERIFIED"
    assert len(process.calls) == 3


def test_second_doctor_failure_prevents_submit(tmp_path: Path) -> None:
    process = FakeRunner(
        [
            "0.0.13\n",
            _doctor(),
            json.dumps(_request("a" * 64)),
            "0.0.13\n",
            _doctor().replace(PAPER_ENDPOINT, "https://api.alpaca.markets"),
        ]
    )
    with pytest.raises(BootstrapError, match="CLI_PAPER_HOST_NOT_VERIFIED"):
        _run(tmp_path, execute=True, runner=process)
    assert len(process.calls) == 5
    assert not any("--dry-run" not in call[1] and "order" in call[1] for call in process.calls)


@pytest.mark.parametrize(
    "response",
    [
        {
            "client_order_id": "wrong-client-lineage",
            "status": "accepted",
        },
        {
            "client_order_id": _request("a" * 64)["client_order_id"],
            "status": "invented-status account-id-123",
        },
    ],
)
def test_ambiguous_submit_response_is_not_recorded_as_a_result(
    tmp_path: Path, response: dict[str, object]
) -> None:
    process = FakeRunner(
        [
            "0.0.13\n",
            _doctor(),
            json.dumps(_request("a" * 64)),
            "0.0.13\n",
            _doctor(),
            json.dumps(response),
        ]
    )
    with pytest.raises(BootstrapError, match="CLI_SUBMIT_RESULT_AMBIGUOUS"):
        _run(tmp_path, execute=True, runner=process)
    artifacts = tmp_path / "artifacts"
    assert (artifacts / "request.json").exists()
    assert (artifacts / "provenance.json").exists()
    assert not (artifacts / "result.json").exists()
    assert (tmp_path / "approval.json.dispatch-attempted").exists()


def test_uncertain_dispatch_cannot_be_retried_with_a_new_artifact_directory(
    tmp_path: Path,
) -> None:
    archive, pin = _archive(tmp_path)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(_approval()))
    approval_path.chmod(0o600)
    ambiguous = FakeRunner(
        [
            "0.0.13\n",
            _doctor(),
            json.dumps(_request("a" * 64)),
            "0.0.13\n",
            _doctor(),
            "not-json",
        ]
    )
    with pytest.raises(BootstrapError, match="CLI_SUBMIT_RESULT_AMBIGUOUS"):
        bootstrap(
            approval_path=approval_path,
            artifact_directory=tmp_path / "artifacts-a",
            cli=PinnedCli.from_archive(archive, pin, ambiguous),
            credentials=Credentials("key", "secret"),
            execute_approved_entry=True,
            ambient_environment={},
            now=lambda: NOW,
        )
    retry = _runner(submit=True)
    with pytest.raises(BootstrapError, match="DISPATCH_ALREADY_ATTEMPTED"):
        bootstrap(
            approval_path=approval_path,
            artifact_directory=tmp_path / "artifacts-b",
            cli=PinnedCli.from_archive(archive, pin, retry),
            credentials=Credentials("key", "secret"),
            execute_approved_entry=True,
            ambient_environment={},
            now=lambda: NOW,
        )
    assert len(retry.calls) == 5
    assert not any("order" in call[1] and "--dry-run" not in call[1] for call in retry.calls)


def test_artifacts_are_private_and_canonical(tmp_path: Path) -> None:
    _, _, artifacts = _run(tmp_path, execute=True)
    assert stat.S_IMODE(artifacts.stat().st_mode) == 0o700
    for path in artifacts.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_bytes().endswith(b"\n")
        value = json.loads(path.read_text())
        assert path.read_bytes() == (
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        )


def test_preconditions_are_rechecked_after_second_doctor_and_before_submit(
    tmp_path: Path,
) -> None:
    archive, pin = _archive(tmp_path)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(_approval()))
    approval_path.chmod(0o600)
    process = _runner(submit=True)
    cli = PinnedCli.from_archive(archive, pin, process)
    instants = iter(
        [
            NOW,
            datetime(2026, 8, 31, 15, 0, 31, tzinfo=UTC),
        ]
    )
    with pytest.raises(BootstrapError, match="SUBMIT_PRECONDITION_FAILED"):
        bootstrap(
            approval_path=approval_path,
            artifact_directory=tmp_path / "artifacts",
            cli=cli,
            credentials=Credentials("key", "secret"),
            execute_approved_entry=True,
            ambient_environment={},
            now=lambda: next(instants),
        )
    assert len(process.calls) == 5
    assert (tmp_path / "artifacts" / "provenance.json").exists()
    assert not (tmp_path / "artifacts" / "result.json").exists()


def test_archive_and_version_identity_are_pinned(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)
    with pytest.raises(BootstrapError, match="CLI_ARCHIVE_DIGEST_MISMATCH"):
        PinnedCli.from_archive(
            archive,
            CliPin(pin.version, "0" * 64, pin.binary_sha256, pin.archive_member),
            _runner(),
        )
    cli = PinnedCli.from_archive(archive, pin, FakeRunner(["0.0.14\n"]))
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(_approval()))
    approval_path.chmod(0o600)
    with pytest.raises(BootstrapError, match="CLI_VERSION_MISMATCH"):
        bootstrap(
            approval_path=approval_path,
            artifact_directory=tmp_path / "artifacts",
            cli=cli,
            credentials=Credentials("key", "secret"),
            now=lambda: NOW,
        )


def test_existing_or_symlink_artifact_directory_is_rejected(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    archive, pin = _archive(tmp_path)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(_approval()))
    approval_path.chmod(0o600)
    cli = PinnedCli.from_archive(archive, pin, _runner())
    with pytest.raises(BootstrapError, match="ARTIFACT_DIRECTORY_EXISTS"):
        bootstrap(
            approval_path=approval_path,
            artifact_directory=existing,
            cli=cli,
            credentials=Credentials("key", "secret"),
            now=lambda: NOW,
        )
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(target, link)
    cli = PinnedCli.from_archive(archive, pin, _runner())
    with pytest.raises(BootstrapError, match="ARTIFACT_DIRECTORY_EXISTS"):
        bootstrap(
            approval_path=approval_path,
            artifact_directory=link,
            cli=cli,
            credentials=Credentials("key", "secret"),
            now=lambda: NOW,
        )


def test_approval_file_must_be_private_regular_and_not_symlink(tmp_path: Path) -> None:
    archive, pin = _archive(tmp_path)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(_approval()))
    approval.chmod(0o644)
    cli = PinnedCli.from_archive(archive, pin, _runner())
    with pytest.raises(BootstrapError, match="APPROVAL_FILE_NOT_PRIVATE"):
        bootstrap(
            approval_path=approval,
            artifact_directory=tmp_path / "artifacts-a",
            cli=cli,
            credentials=Credentials("key", "secret"),
            now=lambda: NOW,
        )
    approval.chmod(0o600)
    link = tmp_path / "approval-link.json"
    os.symlink(approval, link)
    cli = PinnedCli.from_archive(archive, pin, _runner())
    with pytest.raises(BootstrapError, match="APPROVAL_FILE_INVALID"):
        bootstrap(
            approval_path=link,
            artifact_directory=tmp_path / "artifacts-b",
            cli=cli,
            credentials=Credentials("key", "secret"),
            now=lambda: NOW,
        )


def test_output_and_timeout_failures_stop_before_later_authority(tmp_path: Path) -> None:
    process = FakeRunner(["0.0.13\n", "x" * (64 * 1024 + 1)])
    with pytest.raises(BootstrapError, match="CLI_OUTPUT_TOO_LARGE"):
        _run(tmp_path, runner=process)
    assert len(process.calls) == 2
