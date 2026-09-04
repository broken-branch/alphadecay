from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.alpaca.execution_evidence import baseline_account_fingerprint
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import ExecutionBlocked
from backend.app.persistence.runtime import (
    DatabaseConfigurationError,
    apply_migrations,
    discover_migrations,
    normalize_database_url,
    verify_schema,
)
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    CompetitionEntryBudgetRow,
    SubmissionBaselineRow,
)
from backend.app.persistence.sqlalchemy_repository import SQLAlchemyExecutionRepository

MANIFEST_SCHEMA_VERSION = 1
_LEGACY_MANIFEST_SCHEMA_VERSION = "v1"
INITIAL_FUNDING_MANIFEST_TYPE = "JNLC_INITIAL_FUNDING"
INITIAL_FUNDING_SOURCE_TYPE = "JNLC"
_ROOT = Path(__file__).parents[2]
_FILE_LIMIT = 1024 * 1024
_AGGREGATE_LIMIT = 5 * _FILE_LIMIT
_DATABASE_URL_LIMIT = 4096
_MAX_DIRECTORY_ENTRIES = 7
_PRIVATE_FILE_MODES = frozenset({0o400, 0o600})
_HASH = re.compile(r"^[0-9a-f]{64}$")
_HASH_LINE = re.compile(r"^([0-9a-f]{64}) ([ *])([^\r\n]+)$")
_SOURCE_FILES = ("account.json", "positions.json", "orders.json", "activities.json")
_REQUIRED_FILES = frozenset({"manifest.json", "hashes.sha256", *_SOURCE_FILES})
_ALLOWED_FILES = frozenset({*_REQUIRED_FILES, "doctor.txt"})
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "captured_at",
        "account_role",
        "paper_endpoint_verified",
        "account_fingerprint",
        "account_hash",
        "positions_hash",
        "orders_hash",
        "activities_hash",
        "equity",
        "cash",
        "positions_count",
        "orders_count",
        "activities_count",
        "only_activity_type",
        "clean",
    }
)


class SubmissionBaselineImportError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, repr=False)
class SealedSubmissionBaseline:
    account_fingerprint: str
    account_source_hash: str
    equity: Decimal
    captured_at: datetime
    positions_hash: str
    orders_hash: str
    activities_hash: str


@dataclass(frozen=True)
class _Arguments:
    baseline_directory: Path
    database_url_file: Path | None
    persist: bool


class _CommandParser:
    _USAGE = (
        "usage: submission_baseline_import.py --baseline-directory PATH "
        "[--database-url-file PATH --persist]"
    )

    def parse_args(self, argv: Sequence[str] | None) -> _Arguments:
        values = list(sys.argv[1:] if argv is None else argv)
        baseline_directory: Path | None = None
        database_url_file: Path | None = None
        persist = False
        index = 0
        while index < len(values):
            option = values[index]
            if option == "--persist":
                if persist:
                    self.error("SUBMISSION_BASELINE_ARGUMENT_INVALID")
                persist = True
                index += 1
                continue
            if option in {"--baseline-directory", "--database-url-file"}:
                if index + 1 >= len(values) or values[index + 1].startswith("--"):
                    self.error("SUBMISSION_BASELINE_ARGUMENT_INVALID")
                path = Path(values[index + 1])
                if option == "--baseline-directory":
                    if baseline_directory is not None:
                        self.error("SUBMISSION_BASELINE_ARGUMENT_INVALID")
                    baseline_directory = path
                else:
                    if database_url_file is not None:
                        self.error("SUBMISSION_BASELINE_ARGUMENT_INVALID")
                    database_url_file = path
                index += 2
                continue
            if option in {"-h", "--help"}:
                print(self._USAGE)
                raise SystemExit(0)
            self.error("SUBMISSION_BASELINE_ARGUMENT_INVALID")
        if baseline_directory is None:
            self.error("SUBMISSION_BASELINE_ARGUMENT_INVALID")
        if persist != (database_url_file is not None):
            self.error("SUBMISSION_BASELINE_PERSISTENCE_ARGUMENT_INVALID")
        return _Arguments(baseline_directory, database_url_file, persist)

    def error(self, code: str) -> None:
        print(f"{self._USAGE}\nsubmission_baseline_import.py: error: {code}", file=sys.stderr)
        raise SystemExit(2)


def load_submission_baseline_directory(directory: Path) -> SealedSubmissionBaseline:
    files = _read_baseline_directory(directory)
    manifest_payload = _load_json(files["manifest.json"])
    manifest = parse_submission_baseline_manifest(manifest_payload)
    recorded_hashes = _parse_hash_inventory(files["hashes.sha256"], directory)
    expected_hashes = {
        "account.json": manifest.account_source_hash,
        "positions.json": manifest.positions_hash,
        "orders.json": manifest.orders_hash,
        "activities.json": manifest.activities_hash,
    }
    for name, expected in expected_hashes.items():
        observed = hashlib.sha256(files[name]).hexdigest()
        if observed != recorded_hashes[name] or observed != expected:
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_SOURCE_HASH_INVALID")

    sources = {name: _load_json(files[name]) for name in _SOURCE_FILES}
    _validate_source_payloads(manifest, sources)
    return manifest


def parse_submission_baseline_manifest(value: object) -> SealedSubmissionBaseline:
    if type(value) is not dict or set(value) != _MANIFEST_FIELDS:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_MANIFEST_INVALID")
    schema_version = value["schema_version"]
    if (type(schema_version) is not int or schema_version != MANIFEST_SCHEMA_VERSION) and (
        type(schema_version) is not str or schema_version != _LEGACY_MANIFEST_SCHEMA_VERSION
    ):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_SCHEMA_INVALID")
    if value["account_role"] != AccountRole.SUBMISSION.value:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ROLE_INVALID")
    if value["paper_endpoint_verified"] is not True:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PAPER_EVIDENCE_INVALID")
    if value["clean"] is not True:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_NOT_CLEAN")
    if value["equity"] != "100000" or value["cash"] != "100000":
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_BALANCE_INVALID")
    if (
        not _exact_count(value["positions_count"], 0)
        or not _exact_count(value["orders_count"], 0)
        or not _exact_count(value["activities_count"], 1)
    ):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_COUNT_INVALID")
    if value["only_activity_type"] != INITIAL_FUNDING_MANIFEST_TYPE:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ACTIVITY_INVALID")

    account_fingerprint = value["account_fingerprint"]
    if not _is_hash(account_fingerprint):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_FINGERPRINT_INVALID")
    source_hashes = (
        value["account_hash"],
        value["positions_hash"],
        value["orders_hash"],
        value["activities_hash"],
    )
    if any(not _is_hash(item) for item in source_hashes):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_HASH_INVALID")

    return SealedSubmissionBaseline(
        account_fingerprint=account_fingerprint,
        account_source_hash=source_hashes[0],
        equity=Decimal("100000"),
        captured_at=_canonical_timestamp(value["captured_at"]),
        positions_hash=source_hashes[1],
        orders_hash=source_hashes[2],
        activities_hash=source_hashes[3],
    )


def import_submission_baseline(
    manifest: SealedSubmissionBaseline,
    sessions: sessionmaker[Session],
) -> None:
    if type(manifest) is not SealedSubmissionBaseline:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_MANIFEST_INVALID")
    if _existing_baseline_state(manifest, sessions):
        return

    repository = SQLAlchemyExecutionRepository(sessions)
    try:
        repository.register_account(
            role=AccountRole.SUBMISSION,
            fingerprint=manifest.account_fingerprint,
            equity=manifest.equity,
            autonomous_enabled=False,
        )
        repository.capture_baseline(
            role=AccountRole.SUBMISSION,
            fingerprint=manifest.account_fingerprint,
            equity=manifest.equity,
            captured_at=manifest.captured_at,
            positions_hash=manifest.positions_hash,
            orders_hash=manifest.orders_hash,
            activities_hash=manifest.activities_hash,
        )
    except ExecutionBlocked as error:
        if str(error) == "BASELINE_ALREADY_CAPTURED" and _existing_baseline_state(
            manifest, sessions
        ):
            return
        raise SubmissionBaselineImportError(_repository_error_code(error)) from None
    except IntegrityError:
        if _existing_baseline_state(manifest, sessions):
            return
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PERSISTENCE_INVALID") from None
    if not _existing_baseline_state(manifest, sessions):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PERSISTENCE_INVALID")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _CommandParser()
    args = parser.parse_args(argv)

    engine = None
    try:
        manifest = load_submission_baseline_directory(args.baseline_directory)
        if args.persist:
            database_url = _database_url(
                _read_private_path(args.database_url_file, _DATABASE_URL_LIMIT)
            )
            engine = create_engine(database_url, pool_pre_ping=True)
            apply_migrations(engine, discover_migrations(_ROOT / "migrations"))
            verify_schema(engine)
            import_submission_baseline(
                manifest,
                sessionmaker(engine, expire_on_commit=False),
            )
    except (
        json.JSONDecodeError,
        RecursionError,
        UnicodeDecodeError,
        OSError,
        SQLAlchemyError,
        DatabaseConfigurationError,
        SubmissionBaselineImportError,
    ) as error:
        parser.error(_error_code(error))
    except Exception:
        parser.error("SUBMISSION_BASELINE_INPUT_INVALID")
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                parser.error("SUBMISSION_BASELINE_DATABASE_INVALID")

    print(
        json.dumps(
            {
                "account_role": AccountRole.SUBMISSION.value,
                "database_write": args.persist,
                "manifest_valid": True,
                "mode": "PERSIST" if args.persist else "PREVIEW",
                "source_files_verified": len(_SOURCE_FILES),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _read_baseline_directory(directory: Path) -> dict[str, bytes]:
    descriptor = _open_path_without_symlinks(
        directory,
        directory=True,
        code="SUBMISSION_BASELINE_DIRECTORY_INVALID",
    )
    try:
        initial_metadata = os.fstat(descriptor)
        getuid = getattr(os, "getuid", None)
        directory_mode = stat.S_IMODE(initial_metadata.st_mode)
        if (
            getuid is None
            or not stat.S_ISDIR(initial_metadata.st_mode)
            or initial_metadata.st_uid != getuid()
            or directory_mode & 0o500 != 0o500
            or directory_mode & 0o022 != 0
        ):
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DIRECTORY_INVALID")
        names = os.listdir(descriptor)
        if (
            len(names) > _MAX_DIRECTORY_ENTRIES
            or len(names) != len(set(names))
            or not _REQUIRED_FILES.issubset(names)
            or not set(names).issubset(_ALLOWED_FILES)
        ):
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DIRECTORY_INVALID")
        result: dict[str, bytes] = {}
        aggregate_size = 0
        for name in sorted(names):
            content = _read_private_descriptor(descriptor, name, _FILE_LIMIT)
            aggregate_size += len(content)
            if aggregate_size > _AGGREGATE_LIMIT:
                raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID")
            if name in _REQUIRED_FILES:
                result[name] = content
        final_metadata = os.fstat(descriptor)
        if not _stable_metadata(initial_metadata, final_metadata):
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DIRECTORY_INVALID")
        return result
    except OSError:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DIRECTORY_INVALID") from None
    finally:
        os.close(descriptor)


def _read_private_descriptor(directory: int, name: str, limit: int) -> bytes:
    if name not in _ALLOWED_FILES:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    getuid = getattr(os, "getuid", None)
    if nofollow is None or nonblock is None or getuid is None:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID")
    flags = os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except OSError:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID") from None
    try:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != getuid()
            or metadata.st_nlink != 1
            or mode not in _PRIVATE_FILE_MODES
            or metadata.st_size > limit
        ):
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID")
        result = bytearray()
        while len(result) <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
        if len(result) > limit:
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID")
        final_metadata = os.fstat(descriptor)
        if (
            not _stable_metadata(metadata, final_metadata)
            or final_metadata.st_uid != getuid()
            or final_metadata.st_nlink != 1
            or stat.S_IMODE(final_metadata.st_mode) not in _PRIVATE_FILE_MODES
            or final_metadata.st_size != len(result)
        ):
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID")
        return bytes(result)
    except OSError:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID") from None
    finally:
        os.close(descriptor)


def _read_private_path(path: Path | None, limit: int) -> bytes:
    if path is None:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DATABASE_URL_INVALID")
    descriptor = _open_path_without_symlinks(
        path,
        directory=False,
        code="SUBMISSION_BASELINE_PRIVATE_FILE_INVALID",
    )
    try:
        metadata = os.fstat(descriptor)
        getuid = getattr(os, "getuid", None)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            getuid is None
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != getuid()
            or metadata.st_nlink != 1
            or mode not in _PRIVATE_FILE_MODES
            or metadata.st_size > limit
        ):
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID")
        result = bytearray()
        while len(result) <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
        if len(result) > limit:
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID")
        final_metadata = os.fstat(descriptor)
        if (
            not _stable_metadata(metadata, final_metadata)
            or final_metadata.st_uid != getuid()
            or final_metadata.st_nlink != 1
            or stat.S_IMODE(final_metadata.st_mode) not in _PRIVATE_FILE_MODES
            or final_metadata.st_size != len(result)
        ):
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID")
        return bytes(result)
    except OSError:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PRIVATE_FILE_INVALID") from None
    finally:
        os.close(descriptor)


def _open_path_without_symlinks(path: Path, *, directory: bool, code: str) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if nofollow is None or directory_flag is None or nonblock is None:
        raise SubmissionBaselineImportError(code)
    parts = path.parts
    components = parts[1:] if path.is_absolute() else parts
    if not components or any(part in {"", ".", ".."} for part in components):
        raise SubmissionBaselineImportError(code)
    base_flags = os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0)
    try:
        parent = os.open(path.anchor or ".", base_flags | directory_flag)
    except OSError:
        raise SubmissionBaselineImportError(code) from None
    try:
        for index, component in enumerate(components):
            is_last = index == len(components) - 1
            flags = base_flags
            if not is_last or directory:
                flags |= directory_flag
            child = os.open(component, flags, dir_fd=parent)
            if is_last:
                return child
            os.close(parent)
            parent = child
    except OSError:
        raise SubmissionBaselineImportError(code) from None
    finally:
        os.close(parent)
    raise SubmissionBaselineImportError(code)


def _stable_metadata(initial: os.stat_result, final: os.stat_result) -> bool:
    return (
        initial.st_dev,
        initial.st_ino,
        initial.st_mode,
        initial.st_uid,
        initial.st_gid,
        initial.st_nlink,
        initial.st_size,
        initial.st_mtime_ns,
        initial.st_ctime_ns,
    ) == (
        final.st_dev,
        final.st_ino,
        final.st_mode,
        final.st_uid,
        final.st_gid,
        final.st_nlink,
        final.st_size,
        final.st_mtime_ns,
        final.st_ctime_ns,
    )


def _parse_hash_inventory(payload: bytes, directory: Path) -> dict[str, str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_HASH_INVENTORY_INVALID") from None
    if not text.endswith("\n"):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_HASH_INVENTORY_INVALID")
    result: dict[str, str] = {}
    allowed_paths = {name: name for name in _SOURCE_FILES}
    allowed_paths.update({(directory / name).as_posix(): name for name in _SOURCE_FILES})
    for line in text.splitlines():
        match = _HASH_LINE.fullmatch(line)
        if match is None:
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_HASH_INVENTORY_INVALID")
        digest, _marker, raw_path = match.groups()
        source_name = allowed_paths.get(raw_path)
        if source_name is None or source_name in result:
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_HASH_INVENTORY_INVALID")
        result[source_name] = digest
    if set(result) != set(_SOURCE_FILES):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_HASH_INVENTORY_INVALID")
    return result


def _load_json(payload: bytes) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
    except (RecursionError, TypeError, UnicodeDecodeError, ValueError):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_JSON_INVALID") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_JSON_INVALID")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise SubmissionBaselineImportError("SUBMISSION_BASELINE_JSON_INVALID")


def _parse_json_float(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_JSON_INVALID") from None
    if not parsed.is_finite() or not math.isfinite(float(parsed)):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_JSON_INVALID")
    return parsed


def _validate_source_payloads(
    manifest: SealedSubmissionBaseline,
    sources: dict[str, object],
) -> None:
    account = sources["account.json"]
    positions = sources["positions.json"]
    orders = sources["orders.json"]
    activities = sources["activities.json"]
    if type(account) is not dict or len(account) > 256:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ACCOUNT_SOURCE_INVALID")
    try:
        raw_account_id = _required_string(account.get("id"))
        account_id = UUID(raw_account_id)
    except (TypeError, ValueError):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ACCOUNT_SOURCE_INVALID") from None
    if (
        str(account_id) != raw_account_id
        or baseline_account_fingerprint(account_id) != manifest.account_fingerprint
    ):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ACCOUNT_SOURCE_INVALID")
    if (
        account.get("status") != "ACTIVE"
        or any(
            field in account and account[field] is not False
            for field in (
                "trading_blocked",
                "account_blocked",
                "trade_suspended_by_user",
            )
        )
        or _decimal(account.get("equity")) != Decimal("100000")
        or _decimal(account.get("cash")) != Decimal("100000")
    ):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ACCOUNT_SOURCE_INVALID")
    if type(positions) is not list or positions:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_POSITION_SOURCE_INVALID")
    if type(orders) is not list or orders:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ORDER_SOURCE_INVALID")
    if type(activities) is not list or len(activities) != 1:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID")
    activity = activities[0]
    if (
        type(activity) is not dict
        or len(activity) > 256
        or not _valid_string(activity.get("id"))
        or activity.get("activity_type") != INITIAL_FUNDING_SOURCE_TYPE
        or _decimal(activity.get("net_amount")) != Decimal("100000")
        or not _activity_precedes_capture(activity, manifest.captured_at)
    ):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID")


def _activity_precedes_capture(activity: dict[str, object], captured_at: datetime) -> bool:
    transaction_time = activity.get("transaction_time")
    if transaction_time is not None and transaction_time != "":
        try:
            return _provider_timestamp(transaction_time) <= captured_at
        except SubmissionBaselineImportError:
            return False
    activity_date = activity.get("date")
    if type(activity_date) is not str:
        return False
    try:
        parsed = date.fromisoformat(activity_date)
        return activity_date == parsed.isoformat() and parsed <= captured_at.date()
    except ValueError:
        return False


def _existing_baseline_state(
    manifest: SealedSubmissionBaseline,
    sessions: sessionmaker[Session],
) -> bool:
    with sessions() as session:
        baselines = session.scalars(select(SubmissionBaselineRow)).all()
        account = session.get(AccountRoleRow, AccountRole.SUBMISSION.value)
        budget = session.get(CompetitionEntryBudgetRow, AccountRole.SUBMISSION.value)
        assigned_role = session.scalar(
            select(AccountRoleRow.role).where(
                AccountRoleRow.account_fingerprint == manifest.account_fingerprint
            )
        )
    if assigned_role not in {None, AccountRole.SUBMISSION.value}:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DRIFT")
    if (account is None) != (budget is None):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_PARTIAL_STATE")
    if budget is not None and (
        budget.entries_used != 0
        or budget.gross_approved_risk != Decimal("0")
        or budget.reserved_intent_id is not None
        or budget.reserved_risk != Decimal("0")
    ):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_BUDGET_DRIFT")
    if len(baselines) > 1 or baselines and baselines[0].account_role != "SUBMISSION":
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_SECOND_BASELINE")
    if not baselines:
        if account is not None and (
            account.account_fingerprint != manifest.account_fingerprint
            or account.equity != manifest.equity
            or not _account_state_is_clean(account)
        ):
            raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DRIFT")
        return False

    baseline = baselines[0]
    if baseline.contaminated:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_CONTAMINATED")
    expected_baseline_id = uuid5(
        NAMESPACE_URL,
        f"alphadecay:baseline:SUBMISSION:{manifest.account_fingerprint}",
    )
    if account is None or (
        baseline.baseline_id != expected_baseline_id
        or baseline.account_fingerprint != manifest.account_fingerprint
        or baseline.equity != manifest.equity
        or _utc(baseline.captured_at) != manifest.captured_at
        or baseline.positions_hash != manifest.positions_hash
        or baseline.orders_hash != manifest.orders_hash
        or baseline.activities_hash != manifest.activities_hash
        or account.account_fingerprint != manifest.account_fingerprint
        or account.equity != manifest.equity
        or not _account_state_is_clean(account)
    ):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DRIFT")
    return True


def _account_state_is_clean(account: AccountRoleRow) -> bool:
    return (
        account.autonomous_enabled is False
        and account.execution_locked is False
        and account.execution_lock_reason is None
        and account.execution_locked_at is None
        and account.execution_lock_id is None
        and account.execution_lock_generation == 0
        and account.recovery_pending is False
        and account.execution_epoch == 0
        and account.claim_generation == 0
    )


def _repository_error_code(error: ExecutionBlocked) -> str:
    if str(error) in {
        "ACCOUNT_FINGERPRINT_MISMATCH",
        "ACCOUNT_FINGERPRINT_ROLE_MISMATCH",
        "BASELINE_ACCOUNT_MISMATCH",
        "BASELINE_EQUITY_INVALID",
    }:
        return "SUBMISSION_BASELINE_DRIFT"
    return "SUBMISSION_BASELINE_PERSISTENCE_INVALID"


def _database_url(payload: bytes) -> str:
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DATABASE_URL_INVALID") from None
    if not value or value != value.strip() or "\n" in value or "\r" in value:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DATABASE_URL_INVALID")
    try:
        return normalize_database_url(value)
    except DatabaseConfigurationError:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_DATABASE_URL_INVALID") from None


def _canonical_timestamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_TIME_INVALID") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_TIME_INVALID")
    parsed = parsed.astimezone(UTC)
    if value != parsed.isoformat().replace("+00:00", "Z"):
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_TIME_INVALID")
    return parsed


def _provider_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SubmissionBaselineImportError("SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID")
    return parsed.astimezone(UTC)


def _decimal(value: object) -> Decimal | None:
    if type(value) is not str:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _required_string(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > 4096:
        raise ValueError
    return value


def _valid_string(value: object) -> bool:
    return type(value) is str and bool(value) and value == value.strip() and len(value) <= 4096


def _exact_count(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH.fullmatch(value) is not None


def _error_code(error: BaseException) -> str:
    if isinstance(error, SubmissionBaselineImportError):
        return error.code
    if isinstance(error, DatabaseConfigurationError | SQLAlchemyError):
        return "SUBMISSION_BASELINE_DATABASE_INVALID"
    return "SUBMISSION_BASELINE_INPUT_INVALID"


if __name__ == "__main__":
    raise SystemExit(main())
