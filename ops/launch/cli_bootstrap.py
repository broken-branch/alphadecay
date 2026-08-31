from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import selectors
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from backend.app.order_limits import MAX_STRUCTURAL_OPTION_QUANTITY

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
APPROVAL_SCHEMA = "alphadecay.cli-bootstrap-approval.v1"
REQUEST_ARTIFACT_SCHEMA = "alphadecay.cli-bootstrap-request.v1"
PROVENANCE_ARTIFACT_SCHEMA = "alphadecay.cli-bootstrap-provenance.v1"
RESULT_ARTIFACT_SCHEMA = "alphadecay.cli-bootstrap-result.v1"
MAX_FILE_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_CLI_OUTPUT = 64 * 1024
MAX_PROCESS_SECONDS = 30
CREDENTIAL_FINGERPRINT_DOMAIN = b"alphadecay.cli-bootstrap.credential.v1\0"
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
OCC_SYMBOL = re.compile(r"^([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})$")
CLIENT_ORDER_ID = re.compile(r"^ad-(\d{8})-e-([0-9a-f]{24})-a0$")
DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1,4})?$")
AUTHORITY_ENVIRONMENT_KEYS = frozenset(
    {
        "APCA_API_BASE_URL",
        "APCA_API_PROFILE",
        "ALPACA_API_ENDPOINT",
        "ALPACA_API_URL",
        "ALPACA_PROFILE",
    }
)
ORDER_STATUSES = frozenset(
    {
        "accepted",
        "accepted_for_bidding",
        "calculated",
        "canceled",
        "done_for_day",
        "expired",
        "filled",
        "new",
        "partially_filled",
        "pending_cancel",
        "pending_new",
        "pending_replace",
        "rejected",
        "replaced",
        "stopped",
        "suspended",
    }
)


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class Credentials:
    api_key: str
    secret_key: str

    def __post_init__(self) -> None:
        if not self.api_key or not self.secret_key:
            raise BootstrapError("CREDENTIALS_REQUIRED")


@dataclass(frozen=True)
class CliPin:
    version: str
    archive_sha256: str
    binary_sha256: str
    archive_member: str


@dataclass(frozen=True)
class BootstrapResult:
    outcome: str
    request_sha256: str
    response_sha256: str | None


class ProcessPort(Protocol):
    def run_fd(
        self,
        descriptor: int,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> str: ...


class BoundedSubprocess:
    def run_fd(
        self,
        descriptor: int,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> str:
        executable = f"/proc/self/fd/{descriptor}"
        if not argv or argv[0] != executable:
            raise BootstrapError("CLI_EXECUTABLE_MISMATCH")
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                list(argv),
                executable=executable,
                env=dict(environment),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                pass_fds=(descriptor,),
                start_new_session=True,
            )
            assert process.stdout is not None
            deadline = time.monotonic() + MAX_PROCESS_SECONDS
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise BootstrapError("CLI_PROCESS_FAILED")
                    chunk = os.read(process.stdout.fileno(), 16 * 1024)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_CLI_OUTPUT:
                        raise BootstrapError("CLI_OUTPUT_TOO_LARGE")
            if process.wait(timeout=max(deadline - time.monotonic(), 0.1)) != 0:
                raise BootstrapError("CLI_PROCESS_FAILED")
            return bytes(output).decode("utf-8", errors="strict")
        except BootstrapError:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            raise
        except (OSError, UnicodeError, subprocess.SubprocessError):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            raise BootstrapError("CLI_PROCESS_FAILED") from None


class PinnedCli:
    def __init__(self, descriptor: int, pin: CliPin, process: ProcessPort) -> None:
        self._descriptor = descriptor
        self._pin = pin
        self._process = process
        self._closed = False

    @property
    def pin(self) -> CliPin:
        return self._pin

    @classmethod
    def from_archive(cls, archive: Path, pin: CliPin, process: ProcessPort) -> PinnedCli:
        if not HEX_64.fullmatch(pin.archive_sha256) or not HEX_64.fullmatch(pin.binary_sha256):
            raise BootstrapError("CLI_PIN_INVALID")
        descriptor = _open_regular_private_or_public(archive, "CLI_ARCHIVE_INVALID")
        try:
            archive_bytes = _read_fd(descriptor, MAX_ARCHIVE_BYTES, "CLI_RELEASE_FILE_TOO_LARGE")
        finally:
            os.close(descriptor)
        if hashlib.sha256(archive_bytes).hexdigest() != pin.archive_sha256:
            raise BootstrapError("CLI_ARCHIVE_DIGEST_MISMATCH")
        try:
            with tarfile.open(fileobj=_BytesReader(archive_bytes), mode="r:*") as bundle:
                members = bundle.getmembers()
                matches = [member for member in members if member.name == pin.archive_member]
                if len(matches) != 1 or not matches[0].isfile():
                    raise BootstrapError("CLI_ARCHIVE_MEMBER_INVALID")
                source = bundle.extractfile(matches[0])
                if source is None:
                    raise BootstrapError("CLI_ARCHIVE_MEMBER_INVALID")
                binary_bytes = source.read(MAX_ARCHIVE_BYTES + 1)
        except BootstrapError:
            raise
        except (OSError, tarfile.TarError):
            raise BootstrapError("CLI_ARCHIVE_INVALID") from None
        if (
            len(binary_bytes) > MAX_ARCHIVE_BYTES
            or hashlib.sha256(binary_bytes).hexdigest() != pin.binary_sha256
        ):
            raise BootstrapError("CLI_BINARY_DIGEST_MISMATCH")
        with tempfile.TemporaryFile() as binary:
            binary.write(binary_bytes)
            binary.flush()
            os.fchmod(binary.fileno(), 0o500)
            descriptor = os.open(f"/proc/self/fd/{binary.fileno()}", os.O_RDONLY)
        return cls(descriptor, pin, process)

    def verify_identity_and_paper(self, credentials: Credentials) -> None:
        version = self._run(("version",), credentials).strip()
        if version != self._pin.version:
            raise BootstrapError("CLI_VERSION_MISMATCH")
        if not _doctor_verified(self._run(("doctor", "--quiet"), credentials), self._pin.version):
            raise BootstrapError("CLI_PAPER_HOST_NOT_VERIFIED")

    def dry_run(self, order: Mapping[str, object], credentials: Credentials) -> str:
        return self._run(_order_args(order, dry_run=True), credentials)

    def submit(self, order: Mapping[str, object], credentials: Credentials) -> str:
        return self._run(_order_args(order, dry_run=False), credentials)

    def _run(self, arguments: tuple[str, ...], credentials: Credentials) -> str:
        if self._closed:
            raise BootstrapError("CLI_ALREADY_CLOSED")
        if (
            hashlib.sha256(
                _read_fd(self._descriptor, MAX_ARCHIVE_BYTES, "CLI_BINARY_CHANGED")
            ).hexdigest()
            != self._pin.binary_sha256
        ):
            raise BootstrapError("CLI_BINARY_CHANGED")
        environment = _child_environment(credentials)
        executable = f"/proc/self/fd/{self._descriptor}"
        output = self._process.run_fd(
            self._descriptor,
            (executable, *arguments),
            environment,
        )
        if type(output) is not str:
            raise BootstrapError("CLI_OUTPUT_INVALID")
        try:
            size = len(output.encode("utf-8", errors="strict"))
        except UnicodeError:
            raise BootstrapError("CLI_OUTPUT_INVALID") from None
        if size > MAX_CLI_OUTPUT:
            raise BootstrapError("CLI_OUTPUT_TOO_LARGE")
        return output

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._closed = True


class _ArtifactDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            os.mkdir(path, mode=0o700)
        except FileExistsError:
            raise BootstrapError("ARTIFACT_DIRECTORY_EXISTS") from None
        except OSError:
            raise BootstrapError("ARTIFACT_DIRECTORY_INVALID") from None
        self._written: list[Path] = []

    def write(self, name: str, value: Mapping[str, object]) -> None:
        destination = self.path / name
        payload = _canonical_json(value) + b"\n"
        if len(payload) > MAX_FILE_BYTES:
            raise BootstrapError("ARTIFACT_TOO_LARGE")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise BootstrapError("ARTIFACT_WRITE_FAILED") from None
        self._written.append(destination)

    def abandon_if_empty(self) -> None:
        if not self._written:
            with contextlib.suppress(OSError):
                os.rmdir(self.path)


def bootstrap(
    *,
    approval_path: Path,
    artifact_directory: Path,
    cli: PinnedCli,
    credentials: Credentials,
    execute_approved_entry: bool = False,
    ambient_environment: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BootstrapResult:
    artifacts: _ArtifactDirectory | None = None
    try:
        environment = os.environ if ambient_environment is None else ambient_environment
        _reject_ambient_authority(environment)
        approval = _load_approval(approval_path)
        instant = _utc(now())
        validated = _validate_approval(approval, instant)
        _validate_credential_authority(validated, credentials)
        if execute_approved_entry:
            _validate_submit_preconditions(validated, instant)
        artifacts = _ArtifactDirectory(artifact_directory)
        cli.verify_identity_and_paper(credentials)
        dry_run = _parse_json_object(
            cli.dry_run(validated["order"], credentials),
            "CLI_DRY_RUN_INVALID",
        )
        normalized_dry_run = _normalize_dry_run(dry_run)
        if _canonical_json(normalized_dry_run) != _canonical_json(validated["order"]):
            raise BootstrapError("CLI_DRY_RUN_MISMATCH")
        request_sha256 = hashlib.sha256(_canonical_json(validated["order"])).hexdigest()
        artifacts.write(
            "request.json",
            {
                "schema_version": REQUEST_ARTIFACT_SCHEMA,
                "approval_hash": validated["approval_hash"],
                "intent_digest": validated["intent_digest"],
                "request": validated["order"],
                "request_sha256": request_sha256,
            },
        )
        artifacts.write(
            "provenance.json",
            {
                "schema_version": PROVENANCE_ARTIFACT_SCHEMA,
                "account_role": validated["account_role"],
                "approval_hash": validated["approval_hash"],
                "intent_digest": validated["intent_digest"],
                "policy_hash": validated["policy_hash"],
                "book_fingerprint": validated["book_fingerprint"],
                "paper_endpoint": PAPER_ENDPOINT,
                "cli_version": cli.pin.version,
                "cli_archive_sha256": cli.pin.archive_sha256,
                "cli_binary_sha256": cli.pin.binary_sha256,
                "dry_run_verified": True,
                "doctor_checks_completed": 1,
                "doctor_checks_required": 2 if execute_approved_entry else 1,
                "execution_requested": execute_approved_entry,
                "recorded_at": _render_time(instant),
                "request_sha256": request_sha256,
            },
        )
        if not execute_approved_entry:
            return BootstrapResult("DRY_RUN_VERIFIED", request_sha256, None)
        cli.verify_identity_and_paper(credentials)
        dispatch_time = _utc(now())
        _validate_submit_preconditions(validated, dispatch_time)
        _claim_dispatch_once(approval_path, validated["approval_hash"])
        raw_result = cli.submit(validated["order"], credentials)
        try:
            result = _parse_json_object(raw_result, "CLI_SUBMIT_RESULT_AMBIGUOUS")
            client_order_id = result.get("client_order_id")
            status_value = result.get("status")
            if client_order_id != validated["order"]["client_order_id"]:
                raise BootstrapError("CLI_SUBMIT_RESULT_AMBIGUOUS")
            if status_value not in ORDER_STATUSES:
                raise BootstrapError("CLI_SUBMIT_RESULT_AMBIGUOUS")
            response_sha256 = hashlib.sha256(_canonical_json(result)).hexdigest()
        except BootstrapError:
            raise
        artifacts.write(
            "result.json",
            {
                "schema_version": RESULT_ARTIFACT_SCHEMA,
                "client_order_id": client_order_id,
                "outcome": "DISPATCH_RESPONSE_RECEIVED",
                "response_sha256": response_sha256,
                "doctor_checks_completed": 2,
                "recorded_at": _render_time(_utc(now())),
            },
        )
        return BootstrapResult("SUBMIT_DISPATCHED", request_sha256, response_sha256)
    finally:
        cli.close()
        if artifacts is not None:
            artifacts.abandon_if_empty()


def _load_approval(path: Path) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise BootstrapError("APPROVAL_FILE_INVALID") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BootstrapError("APPROVAL_FILE_INVALID")
        if info.st_mode & 0o077:
            raise BootstrapError("APPROVAL_FILE_NOT_PRIVATE")
        raw = _read_fd(descriptor, MAX_FILE_BYTES, "APPROVAL_FILE_TOO_LARGE")
    finally:
        os.close(descriptor)
    try:
        return _parse_json_object(raw.decode("utf-8", errors="strict"), "APPROVAL_INVALID")
    except UnicodeError:
        raise BootstrapError("APPROVAL_INVALID") from None


def _claim_dispatch_once(approval_path: Path, approval_hash: object) -> None:
    assert isinstance(approval_hash, str)
    marker = approval_path.with_name(f"{approval_path.name}.dispatch-attempted")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(approval_hash.encode("ascii") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise BootstrapError("DISPATCH_ALREADY_ATTEMPTED") from None
    except OSError:
        raise BootstrapError("DISPATCH_GUARD_FAILED") from None


def _validate_approval(value: dict[str, object], instant: datetime) -> dict[str, object]:
    expected = {
        "schema_version",
        "account_role",
        "approval_hash",
        "intent_digest",
        "order_sha256",
        "policy_hash",
        "book_fingerprint",
        "credential_fingerprint",
        "trade_date",
        "valid_until",
        "paper_endpoint",
        "paper_trade",
        "order",
        "submit_preconditions",
    }
    if set(value) != expected:
        raise BootstrapError("APPROVAL_INVALID")
    if value["schema_version"] != APPROVAL_SCHEMA:
        raise BootstrapError("APPROVAL_INVALID")
    if value["account_role"] not in {"DEVELOPMENT", "SUBMISSION"}:
        raise BootstrapError("APPROVAL_INVALID")
    for field in (
        "approval_hash",
        "intent_digest",
        "order_sha256",
        "policy_hash",
        "book_fingerprint",
        "credential_fingerprint",
    ):
        if type(value[field]) is not str or not HEX_64.fullmatch(value[field]):
            raise BootstrapError("APPROVAL_INVALID")
    approval_payload = {key: item for key, item in value.items() if key != "approval_hash"}
    if hashlib.sha256(_canonical_json(approval_payload)).hexdigest() != value["approval_hash"]:
        raise BootstrapError("APPROVAL_INVALID")
    if value["paper_endpoint"] != PAPER_ENDPOINT or value["paper_trade"] is not True:
        raise BootstrapError("APPROVAL_INVALID")
    trade_date = _parse_date(value["trade_date"])
    valid_until = _parse_time(value["valid_until"], "APPROVAL_INVALID")
    if valid_until <= instant:
        raise BootstrapError("APPROVAL_INVALID")
    order = _validate_order(value["order"], value["intent_digest"], trade_date)
    if hashlib.sha256(_canonical_json(order)).hexdigest() != value["order_sha256"]:
        raise BootstrapError("APPROVAL_INVALID")
    preconditions = _validate_precondition_shape(value["submit_preconditions"])
    return {**value, "order": order, "submit_preconditions": preconditions}


def _validate_credential_authority(
    approval: Mapping[str, object], credentials: Credentials
) -> None:
    expected = hashlib.sha256(
        CREDENTIAL_FINGERPRINT_DOMAIN + credentials.api_key.encode("utf-8")
    ).hexdigest()
    fingerprint = approval["credential_fingerprint"]
    assert isinstance(fingerprint, str)
    if not hmac.compare_digest(fingerprint, expected):
        raise BootstrapError("CREDENTIAL_AUTHORITY_MISMATCH")


def _validate_order(value: object, intent_digest: object, trade_date: date) -> dict[str, object]:
    if type(value) is not dict:
        raise BootstrapError("APPROVAL_INVALID")
    expected = {
        "client_order_id",
        "legs",
        "limit_price",
        "order_class",
        "qty",
        "time_in_force",
        "type",
    }
    if set(value) != expected:
        raise BootstrapError("APPROVAL_INVALID")
    if (
        value["order_class"] != "mleg"
        or value["time_in_force"] != "day"
        or value["type"] != "limit"
    ):
        raise BootstrapError("APPROVAL_INVALID")
    quantity = _strict_integer(value["qty"], 1, MAX_STRUCTURAL_OPTION_QUANTITY)
    price = _strict_decimal(value["limit_price"])
    if price == 0:
        raise BootstrapError("APPROVAL_INVALID")
    if type(value["client_order_id"]) is not str:
        raise BootstrapError("APPROVAL_INVALID")
    match = CLIENT_ORDER_ID.fullmatch(value["client_order_id"])
    if (
        match is None
        or match.group(1) != trade_date.strftime("%Y%m%d")
        or match.group(2) != str(intent_digest)[:24]
    ):
        raise BootstrapError("APPROVAL_INVALID")
    legs = value["legs"]
    if type(legs) is not list or len(legs) != 2:
        raise BootstrapError("APPROVAL_INVALID")
    normalized_legs = [_validate_leg(leg) for leg in legs]
    if {leg["position_intent"] for leg in normalized_legs} != {
        "buy_to_open",
        "sell_to_open",
    }:
        raise BootstrapError("APPROVAL_INVALID")
    symbols = [OCC_SYMBOL.fullmatch(leg["symbol"]) for leg in normalized_legs]
    if any(symbol is None for symbol in symbols):
        raise BootstrapError("APPROVAL_INVALID")
    assert symbols[0] is not None and symbols[1] is not None
    if symbols[0].groups()[:3] != symbols[1].groups()[:3]:
        raise BootstrapError("APPROVAL_INVALID")
    strikes = [Decimal(symbol.group(4)) / 1000 for symbol in symbols]
    width = abs(strikes[0] - strikes[1])
    if width == 0 or abs(price) >= width:
        raise BootstrapError("APPROVAL_INVALID")
    return {
        "client_order_id": value["client_order_id"],
        "legs": normalized_legs,
        "limit_price": _render_decimal(price),
        "order_class": "mleg",
        "qty": str(quantity),
        "time_in_force": "day",
        "type": "limit",
    }


def _validate_leg(value: object) -> dict[str, str]:
    if type(value) is not dict or set(value) != {"symbol", "ratio_qty", "position_intent"}:
        raise BootstrapError("APPROVAL_INVALID")
    if type(value["symbol"]) is not str or OCC_SYMBOL.fullmatch(value["symbol"]) is None:
        raise BootstrapError("APPROVAL_INVALID")
    if value["ratio_qty"] != "1" or value["position_intent"] not in {
        "buy_to_open",
        "sell_to_open",
    }:
        raise BootstrapError("APPROVAL_INVALID")
    return {
        "symbol": value["symbol"],
        "ratio_qty": "1",
        "position_intent": value["position_intent"],
    }


def _validate_precondition_shape(value: object) -> dict[str, object]:
    expected = {
        "checked_at",
        "expires_at",
        "book_fingerprint",
        "account_role",
        "market_open",
        "buying_power_verified",
        "positions_orders_reconciled",
        "quotes_fresh",
        "risk_rechecked",
        "idempotency_clear",
        "submit_authorized",
    }
    if type(value) is not dict or set(value) != expected:
        raise BootstrapError("APPROVAL_INVALID")
    for field in expected - {"checked_at", "expires_at", "book_fingerprint", "account_role"}:
        if type(value[field]) is not bool:
            raise BootstrapError("APPROVAL_INVALID")
    _parse_time(value["checked_at"], "APPROVAL_INVALID")
    _parse_time(value["expires_at"], "APPROVAL_INVALID")
    return dict(value)


def _validate_submit_preconditions(approval: Mapping[str, object], instant: datetime) -> None:
    preconditions = approval["submit_preconditions"]
    assert isinstance(preconditions, dict)
    checked_at = _parse_time(preconditions["checked_at"], "SUBMIT_PRECONDITION_FAILED")
    expires_at = _parse_time(preconditions["expires_at"], "SUBMIT_PRECONDITION_FAILED")
    valid_until = _parse_time(approval["valid_until"], "SUBMIT_PRECONDITION_FAILED")
    booleans = (
        "market_open",
        "buying_power_verified",
        "positions_orders_reconciled",
        "quotes_fresh",
        "risk_rechecked",
        "idempotency_clear",
        "submit_authorized",
    )
    if (
        preconditions["book_fingerprint"] != approval["book_fingerprint"]
        or preconditions["account_role"] != approval["account_role"]
        or not all(preconditions[field] is True for field in booleans)
        or checked_at > instant
        or instant > expires_at
        or expires_at > valid_until
        or expires_at - checked_at > timedelta(seconds=60)
    ):
        raise BootstrapError("SUBMIT_PRECONDITION_FAILED")


def _normalize_dry_run(value: dict[str, object]) -> dict[str, object]:
    allowed = {
        "client_order_id",
        "legs",
        "limit_price",
        "order_class",
        "qty",
        "time_in_force",
        "type",
        "advanced_instructions",
    }
    if set(value) - allowed or value.get("advanced_instructions") not in (None, {}, [], ""):
        raise BootstrapError("CLI_DRY_RUN_INVALID")
    cleaned = {key: item for key, item in value.items() if key != "advanced_instructions"}
    if set(cleaned) != allowed - {"advanced_instructions"}:
        raise BootstrapError("CLI_DRY_RUN_INVALID")
    return cleaned


def _order_args(order: Mapping[str, object], *, dry_run: bool) -> tuple[str, ...]:
    arguments = (
        "order",
        "submit",
        "--quiet",
        "--order-class",
        "mleg",
        "--qty",
        str(order["qty"]),
        "--type",
        "limit",
        "--time-in-force",
        "day",
        "--limit-price",
        str(order["limit_price"]),
        "--client-order-id",
        str(order["client_order_id"]),
        "--legs",
        json.dumps(order["legs"], separators=(",", ":"), ensure_ascii=True),
    )
    return (*arguments, "--dry-run") if dry_run else arguments


def _reject_ambient_authority(environment: Mapping[str, str]) -> None:
    if any(key in environment for key in AUTHORITY_ENVIRONMENT_KEYS):
        raise BootstrapError("CLI_AMBIENT_AUTHORITY_REJECTED")
    live = environment.get("ALPACA_LIVE_TRADE")
    if live is not None and live.casefold() != "false":
        raise BootstrapError("CLI_AMBIENT_AUTHORITY_REJECTED")


def _child_environment(credentials: Credentials) -> Mapping[str, str]:
    return MappingProxyType(
        {
            "ALPACA_API_KEY": credentials.api_key,
            "ALPACA_SECRET_KEY": credentials.secret_key,
            "ALPACA_LIVE_TRADE": "false",
            "LC_ALL": "C.UTF-8",
        }
    )


def _doctor_verified(value: str, version: str) -> bool:
    if len(value.encode("utf-8")) > MAX_CLI_OUTPUT:
        raise BootstrapError("CLI_OUTPUT_TOO_LARGE")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        return False
    lines = tuple(line.strip() for line in value.splitlines() if line.strip())
    required = (
        f"Alpaca CLI {version}",
        "✓ no saved profiles configured (using env var credentials)",
        "✓ active profile: paper",
        "✓ API key credentials from env (ALPACA_API_KEY + ALPACA_SECRET_KEY)",
        "Connectivity:",
        f"Trading:  {PAPER_ENDPOINT}",
        "✓ trading API: connected",
        "Data:     https://data.alpaca.markets",
        "✓ data API: connected",
        "All checks passed.",
    )
    positions: list[int] = []
    for expected in required:
        matches = [index for index, line in enumerate(lines) if line == expected]
        if len(matches) != 1:
            return False
        positions.append(matches[0])
    allowed = set(required)
    authority_tokens = ("profile", "credential", "trading:", "trading api:", "data:", "data api:")
    for line in lines:
        folded = line.casefold()
        if any(token in folded for token in authority_tokens) and line not in allowed:
            return False
        if re.search(r"\blive\b|\boauth\b", line, re.IGNORECASE):
            return False
        urls = re.findall(r"https?://[^\s]+", line, re.IGNORECASE)
        if any(url not in {PAPER_ENDPOINT, "https://data.alpaca.markets"} for url in urls):
            return False
        if re.search(
            r"\b(?:denied|disabled|disconnected|error|failed|failure|missing|skipped|"
            r"unavailable|unknown|warn|warning)\b|\bnot\s+ok\b",
            line,
            re.IGNORECASE,
        ):
            return False
    return positions == sorted(positions) and bool(lines) and lines[-1] == required[-1]


def _parse_json_object(value: str, code: str) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise BootstrapError(code)
            result[key] = item
        return result

    try:
        result = json.loads(
            value,
            object_pairs_hook=unique,
            parse_constant=lambda _value: _reject_json_constant(code),
        )
    except BootstrapError:
        raise
    except (TypeError, ValueError):
        raise BootstrapError(code) from None
    if type(result) is not dict:
        raise BootstrapError(code)
    return result


def _reject_json_constant(code: str) -> None:
    raise BootstrapError(code)


def _parse_date(value: object) -> date:
    if type(value) is not str:
        raise BootstrapError("APPROVAL_INVALID")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise BootstrapError("APPROVAL_INVALID") from None
    if parsed.isoformat() != value:
        raise BootstrapError("APPROVAL_INVALID")
    return parsed


def _parse_time(value: object, code: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise BootstrapError(code)
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise BootstrapError(code) from None
    if result.tzinfo != UTC or _render_time(result) != value:
        raise BootstrapError(code)
    return result


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise BootstrapError("CLOCK_INVALID")
    return value.astimezone(UTC)


def _render_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _strict_integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not str or not value.isascii() or not value.isdigit():
        raise BootstrapError("APPROVAL_INVALID")
    if len(value) > 1 and value.startswith("0"):
        raise BootstrapError("APPROVAL_INVALID")
    result = int(value)
    if not minimum <= result <= maximum:
        raise BootstrapError("APPROVAL_INVALID")
    return result


def _strict_decimal(value: object) -> Decimal:
    if type(value) is not str or DECIMAL.fullmatch(value) is None:
        raise BootstrapError("APPROVAL_INVALID")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise BootstrapError("APPROVAL_INVALID") from None
    if not result.is_finite() or _render_decimal(result) != value:
        raise BootstrapError("APPROVAL_INVALID")
    return result


def _render_decimal(value: Decimal) -> str:
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result == "-0" else result


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _open_regular_private_or_public(path: Path, code: str) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise BootstrapError(code) from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise BootstrapError(code)
    return descriptor


def _read_fd(descriptor: int, limit: int, code: str) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    result = bytearray()
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(result)))
        if not chunk:
            break
        result.extend(chunk)
        if len(result) > limit:
            raise BootstrapError(code)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return bytes(result)


class _BytesReader:
    def __init__(self, value: bytes) -> None:
        import io

        self._value = io.BytesIO(value)

    def __getattr__(self, name: str) -> object:
        return getattr(self._value, name)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview one frozen Alpaca paper entry")
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--cli-archive", required=True, type=Path)
    parser.add_argument(
        "--execute-approved-entry",
        action="store_true",
        help="submit only the exact dry-run-verified paper entry",
    )
    args = parser.parse_args(argv)
    credentials = Credentials(
        os.environ.get("ALPACA_API_KEY", ""),
        os.environ.get("ALPACA_SECRET_KEY", ""),
    )
    pin = CliPin(
        version="0.0.13",
        archive_sha256="50cd254d81b6bbc541259eeeb4bb1a8f7c319557fa49fc3b2765cddd72a66a82",
        binary_sha256="502bb6a8c87f0b6791669861853168caf41f228767bd89e88f6eabe5f1e8cc1c",
        archive_member="alpaca",
    )
    cli = PinnedCli.from_archive(args.cli_archive, pin, BoundedSubprocess())
    result = bootstrap(
        approval_path=args.approval,
        artifact_directory=args.artifacts,
        cli=cli,
        credentials=credentials,
        execute_approved_entry=args.execute_approved_entry,
    )
    print(result.outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
