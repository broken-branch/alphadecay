from __future__ import annotations

import hashlib
import json
import os
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.alpaca.execution_evidence import baseline_account_fingerprint
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    Base,
    CompetitionEntryBudgetRow,
    SubmissionBaselineRow,
)
from ops.launch.submission_baseline_import import (
    MANIFEST_SCHEMA_VERSION,
    SealedSubmissionBaseline,
    SubmissionBaselineImportError,
    _parse_hash_inventory,
    import_submission_baseline,
    load_submission_baseline_directory,
    main,
    parse_submission_baseline_manifest,
)

ACCOUNT_ID = UUID("11111111-1111-4111-8111-111111111111")
FINGERPRINT = baseline_account_fingerprint(ACCOUNT_ID)
CAPTURED_AT = datetime(2026, 8, 28, 15, 13, tzinfo=UTC)
SOURCE_NAMES = ("account.json", "positions.json", "orders.json", "activities.json")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _source_payloads() -> dict[str, bytes]:
    return {
        "account.json": _json_bytes(
            {
                "id": str(ACCOUNT_ID),
                "status": "ACTIVE",
                "equity": "100000",
                "cash": "100000",
                "trading_blocked": False,
                "account_blocked": False,
                "trade_suspended_by_user": False,
            }
        ),
        "positions.json": _json_bytes([]),
        "orders.json": _json_bytes([]),
        "activities.json": _json_bytes(
            [
                {
                    "id": "initial-funding-fixture",
                    "activity_type": "JNLC",
                    "date": "2026-08-28",
                    "net_amount": "100000",
                }
            ]
        ),
    }


def _manifest_payload(
    sources: dict[str, bytes] | None = None, **changes: object
) -> dict[str, object]:
    sources = _source_payloads() if sources is None else sources
    payload: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "captured_at": "2026-08-28T15:13:00Z",
        "account_role": "SUBMISSION",
        "paper_endpoint_verified": True,
        "account_fingerprint": FINGERPRINT,
        "account_hash": hashlib.sha256(sources["account.json"]).hexdigest(),
        "positions_hash": hashlib.sha256(sources["positions.json"]).hexdigest(),
        "orders_hash": hashlib.sha256(sources["orders.json"]).hexdigest(),
        "activities_hash": hashlib.sha256(sources["activities.json"]).hexdigest(),
        "equity": "100000",
        "cash": "100000",
        "positions_count": 0,
        "orders_count": 0,
        "activities_count": 1,
        "only_activity_type": "JNLC_INITIAL_FUNDING",
        "clean": True,
    }
    payload.update(changes)
    return payload


def _hash_inventory(sources: dict[str, bytes]) -> bytes:
    return "".join(
        f"{hashlib.sha256(sources[name]).hexdigest()}  {name}\n" for name in SOURCE_NAMES
    ).encode("ascii")


def test_hash_inventory_accepts_only_exact_source_directory_prefix() -> None:
    sources = _source_payloads()
    directory = Path("fixtures/sealed-baseline")
    inventory = "".join(
        f"{hashlib.sha256(sources[name]).hexdigest()}  {directory.as_posix()}/{name}\n"
        for name in SOURCE_NAMES
    ).encode("ascii")

    assert set(_parse_hash_inventory(inventory, directory)) == set(SOURCE_NAMES)

    with pytest.raises(
        SubmissionBaselineImportError,
        match="SUBMISSION_BASELINE_HASH_INVENTORY_INVALID",
    ):
        _parse_hash_inventory(
            inventory.replace(
                b"fixtures/sealed-baseline/account.json",
                b"fixtures/other/account.json",
            ),
            directory,
        )

    absolute_directory = Path("/private/submission-baseline-20260828T1513Z")
    absolute_inventory = "".join(
        f"{hashlib.sha256(sources[name]).hexdigest()}  {(absolute_directory / name).as_posix()}\n"
        for name in SOURCE_NAMES
    ).encode("ascii")
    assert set(_parse_hash_inventory(absolute_inventory, absolute_directory)) == set(SOURCE_NAMES)

    for invalid_path in (
        "private/submission-baseline-20260828T1513Z/account.json",
        "/private/submission-baseline-20260828T1513Z-copy/account.json",
        "/private/submission-baseline-20260828T1513Z/sealed/../account.json",
    ):
        with pytest.raises(
            SubmissionBaselineImportError,
            match="SUBMISSION_BASELINE_HASH_INVENTORY_INVALID",
        ):
            _parse_hash_inventory(
                absolute_inventory.replace(
                    b"/private/submission-baseline-20260828T1513Z/account.json",
                    invalid_path.encode("ascii"),
                ),
                absolute_directory,
            )


def _write_private(path: Path, payload: bytes, mode: int = 0o400) -> None:
    path.write_bytes(payload)
    path.chmod(mode)


def _baseline_directory(
    root: Path,
    *,
    sources: dict[str, bytes] | None = None,
    manifest_changes: dict[str, object] | None = None,
    inventory: bytes | None = None,
    mode: int = 0o400,
) -> Path:
    directory = root / "baseline"
    directory.mkdir(parents=True)
    sources = _source_payloads() if sources is None else sources
    manifest = _manifest_payload(sources, **(manifest_changes or {}))
    payloads = {
        **sources,
        "manifest.json": _json_bytes(manifest),
        "hashes.sha256": _hash_inventory(sources) if inventory is None else inventory,
        "doctor.txt": b"synthetic fixture only\n",
    }
    for name, payload in payloads.items():
        _write_private(directory / name, payload, mode)
    return directory


def _replace_private(path: Path, payload: bytes, mode: int = 0o400) -> None:
    path.chmod(0o600)
    path.write_bytes(payload)
    path.chmod(mode)


def _sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_loader_accepts_immutable_0400_capture_and_maps_raw_hashes(tmp_path: Path) -> None:
    directory = _baseline_directory(tmp_path, mode=0o400)

    baseline = load_submission_baseline_directory(directory)

    sources = _source_payloads()
    assert baseline.account_fingerprint == FINGERPRINT
    assert baseline.account_source_hash == hashlib.sha256(sources["account.json"]).hexdigest()
    assert baseline.captured_at == CAPTURED_AT
    assert baseline.equity == Decimal("100000")
    assert baseline.positions_hash == hashlib.sha256(sources["positions.json"]).hexdigest()
    assert baseline.orders_hash == hashlib.sha256(sources["orders.json"]).hexdigest()
    assert baseline.activities_hash == hashlib.sha256(sources["activities.json"]).hexdigest()
    assert FINGERPRINT not in repr(baseline)


def test_loader_accepts_owner_writable_0600_capture(tmp_path: Path) -> None:
    load_submission_baseline_directory(_baseline_directory(tmp_path, mode=0o600))


def test_loader_accepts_exact_absolute_directory_hash_prefix(tmp_path: Path) -> None:
    sources = _source_payloads()
    directory = _baseline_directory(tmp_path, sources=sources)
    inventory = "".join(
        f"{hashlib.sha256(sources[name]).hexdigest()}  {(directory / name).as_posix()}\n"
        for name in SOURCE_NAMES
    ).encode("ascii")
    _replace_private(directory / "hashes.sha256", inventory)

    load_submission_baseline_directory(directory)


def test_loader_accepts_exact_legacy_v1_manifest(tmp_path: Path) -> None:
    directory = _baseline_directory(
        tmp_path,
        manifest_changes={"schema_version": "v1"},
    )

    baseline = load_submission_baseline_directory(directory)

    assert baseline.account_fingerprint == FINGERPRINT
    assert baseline.captured_at == CAPTURED_AT


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"schema_version": True}, "SUBMISSION_BASELINE_SCHEMA_INVALID"),
        ({"schema_version": 1.0}, "SUBMISSION_BASELINE_SCHEMA_INVALID"),
        ({"schema_version": 2}, "SUBMISSION_BASELINE_SCHEMA_INVALID"),
        ({"schema_version": "1"}, "SUBMISSION_BASELINE_SCHEMA_INVALID"),
        ({"schema_version": "V1"}, "SUBMISSION_BASELINE_SCHEMA_INVALID"),
        ({"schema_version": "v01"}, "SUBMISSION_BASELINE_SCHEMA_INVALID"),
        ({"schema_version": "v2"}, "SUBMISSION_BASELINE_SCHEMA_INVALID"),
        ({"account_role": "DEVELOPMENT"}, "SUBMISSION_BASELINE_ROLE_INVALID"),
        ({"paper_endpoint_verified": 1}, "SUBMISSION_BASELINE_PAPER_EVIDENCE_INVALID"),
        ({"paper_endpoint_verified": False}, "SUBMISSION_BASELINE_PAPER_EVIDENCE_INVALID"),
        ({"clean": 1}, "SUBMISSION_BASELINE_NOT_CLEAN"),
        ({"equity": "100000.0"}, "SUBMISSION_BASELINE_BALANCE_INVALID"),
        ({"cash": "99999"}, "SUBMISSION_BASELINE_BALANCE_INVALID"),
        ({"positions_count": False}, "SUBMISSION_BASELINE_COUNT_INVALID"),
        ({"orders_count": 1}, "SUBMISSION_BASELINE_COUNT_INVALID"),
        ({"activities_count": True}, "SUBMISSION_BASELINE_COUNT_INVALID"),
        ({"only_activity_type": "FILL"}, "SUBMISSION_BASELINE_ACTIVITY_INVALID"),
        ({"account_fingerprint": "A" * 64}, "SUBMISSION_BASELINE_FINGERPRINT_INVALID"),
        ({"account_hash": "A" * 64}, "SUBMISSION_BASELINE_HASH_INVALID"),
        ({"captured_at": "2026-08-28T15:13:00+00:00"}, "SUBMISSION_BASELINE_TIME_INVALID"),
    ],
)
def test_parser_rejects_manifest_authority_drift(changes: dict[str, object], code: str) -> None:
    with pytest.raises(SubmissionBaselineImportError, match=code):
        parse_submission_baseline_manifest(_manifest_payload(**changes))


def test_parser_rejects_unknown_and_missing_manifest_fields() -> None:
    unknown = _manifest_payload(unexpected="value")
    missing = _manifest_payload()
    del missing["account_hash"]

    for payload in (unknown, missing):
        with pytest.raises(
            SubmissionBaselineImportError, match="SUBMISSION_BASELINE_MANIFEST_INVALID"
        ):
            parse_submission_baseline_manifest(payload)


@pytest.mark.parametrize("target", ["manifest.json", "account.json", "doctor.txt"])
def test_loader_rejects_symlink_files(tmp_path: Path, target: str) -> None:
    directory = _baseline_directory(tmp_path)
    outside = tmp_path / "outside"
    _write_private(outside, b"{}\n")
    (directory / target).unlink()
    (directory / target).symlink_to(outside)

    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_PRIVATE_FILE_INVALID"
    ):
        load_submission_baseline_directory(directory)


def test_loader_rejects_symlink_directory(tmp_path: Path) -> None:
    directory = _baseline_directory(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)

    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_DIRECTORY_INVALID"
    ):
        load_submission_baseline_directory(alias)


def test_loader_rejects_symlinked_parent_and_nonprivate_directory_mode(tmp_path: Path) -> None:
    actual_parent = tmp_path / "actual"
    directory = _baseline_directory(actual_parent)
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(actual_parent, target_is_directory=True)

    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_DIRECTORY_INVALID"
    ):
        load_submission_baseline_directory(alias_parent / directory.name)

    directory.chmod(0o777)
    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_DIRECTORY_INVALID"
    ):
        load_submission_baseline_directory(directory)


def test_loader_rejects_hardlinked_file(tmp_path: Path) -> None:
    directory = _baseline_directory(tmp_path)
    os.link(directory / "account.json", tmp_path / "linked-account.json")

    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_PRIVATE_FILE_INVALID"
    ):
        load_submission_baseline_directory(directory)


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_loader_rejects_nonregular_authoritative_files(tmp_path: Path, kind: str) -> None:
    directory = _baseline_directory(tmp_path)
    target = directory / "account.json"
    target.unlink()
    if kind == "fifo":
        os.mkfifo(target, 0o400)
    else:
        target.mkdir(mode=0o700)

    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_PRIVATE_FILE_INVALID"
    ):
        load_submission_baseline_directory(directory)


def test_loader_rejects_file_metadata_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _baseline_directory(tmp_path)
    target = directory / "account.json"
    original_read = os.read
    changed = False

    def changing_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        result = original_read(descriptor, size)
        if result and not changed:
            changed = True
            target.chmod(0o600)
            target.chmod(0o400)
        return result

    monkeypatch.setattr("ops.launch.submission_baseline_import.os.read", changing_read)
    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_PRIVATE_FILE_INVALID"
    ):
        load_submission_baseline_directory(directory)


def test_loader_rejects_directory_metadata_change_after_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _baseline_directory(tmp_path)
    original_listdir = os.listdir

    def changing_listdir(descriptor: int) -> list[str]:
        result = original_listdir(descriptor)
        marker = directory / "transient"
        _write_private(marker, b"transient\n")
        marker.unlink()
        return result

    monkeypatch.setattr("ops.launch.submission_baseline_import.os.listdir", changing_listdir)
    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_DIRECTORY_INVALID"
    ):
        load_submission_baseline_directory(directory)


@pytest.mark.parametrize("mode", [0o000, 0o200, 0o500, 0o640, 0o644])
def test_loader_rejects_non_owner_read_only_modes(tmp_path: Path, mode: int) -> None:
    directory = _baseline_directory(tmp_path)
    (directory / "orders.json").chmod(mode)

    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_PRIVATE_FILE_INVALID"
    ):
        load_submission_baseline_directory(directory)


@pytest.mark.parametrize("name", ["manifest.json", "hashes.sha256", *SOURCE_NAMES])
def test_loader_rejects_missing_authoritative_file(tmp_path: Path, name: str) -> None:
    directory = _baseline_directory(tmp_path)
    (directory / name).unlink()

    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_DIRECTORY_INVALID"
    ):
        load_submission_baseline_directory(directory)


def test_loader_rejects_extra_file_and_oversized_file(tmp_path: Path) -> None:
    extra_directory = _baseline_directory(tmp_path / "extra")
    _write_private(extra_directory / "extra.json", b"{}\n")
    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_DIRECTORY_INVALID"
    ):
        load_submission_baseline_directory(extra_directory)

    large_directory = _baseline_directory(tmp_path / "large")
    _replace_private(large_directory / "doctor.txt", b"x" * (1024 * 1024 + 1))
    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_PRIVATE_FILE_INVALID"
    ):
        load_submission_baseline_directory(large_directory)


@pytest.mark.parametrize(
    "inventory_transform",
    [
        lambda lines: [*lines, lines[0]],
        lambda lines: lines[:-1],
        lambda lines: [*lines, lines[0].replace("account.json", "extra.json")],
        lambda lines: [
            lines[0].replace("account.json", "sealed/capture/account.json"),
            *lines[1:],
        ],
        lambda lines: [lines[0].replace("account.json", "./account.json"), *lines[1:]],
        lambda lines: [lines[0].replace("account.json", "/account.json"), *lines[1:]],
        lambda lines: [lines[0].replace("account.json", "dir\\account.json"), *lines[1:]],
        lambda lines: [lines[0].replace("account.json", "unsafe\taccount.json"), *lines[1:]],
    ],
)
def test_hash_inventory_rejects_duplicate_missing_extra_and_traversal(
    tmp_path: Path, inventory_transform: Callable[[list[str]], list[str]]
) -> None:
    sources = _source_payloads()
    lines = _hash_inventory(sources).decode("ascii").splitlines(keepends=True)
    transformed = inventory_transform(lines)
    directory = _baseline_directory(
        tmp_path, sources=sources, inventory="".join(transformed).encode()
    )

    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_HASH_INVENTORY_INVALID"
    ):
        load_submission_baseline_directory(directory)


def test_loader_rejects_inventory_and_manifest_hash_drift(tmp_path: Path) -> None:
    sources = _source_payloads()
    inventory = _hash_inventory(sources).replace(
        hashlib.sha256(sources["account.json"]).hexdigest().encode(), b"f" * 64, 1
    )
    inventory_directory = _baseline_directory(
        tmp_path / "inventory", sources=sources, inventory=inventory
    )
    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_SOURCE_HASH_INVALID"
    ):
        load_submission_baseline_directory(inventory_directory)

    manifest_directory = _baseline_directory(
        tmp_path / "manifest",
        sources=sources,
        manifest_changes={"account_hash": "f" * 64},
    )
    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_SOURCE_HASH_INVALID"
    ):
        load_submission_baseline_directory(manifest_directory)


@pytest.mark.parametrize(
    ("name", "payload", "code"),
    [
        ("account.json", [], "SUBMISSION_BASELINE_ACCOUNT_SOURCE_INVALID"),
        (
            "account.json",
            {
                "id": str(ACCOUNT_ID),
                "status": "ACTIVE",
                "equity": True,
                "cash": "100000",
                "trading_blocked": False,
                "account_blocked": False,
                "trade_suspended_by_user": False,
            },
            "SUBMISSION_BASELINE_ACCOUNT_SOURCE_INVALID",
        ),
        ("positions.json", [{}], "SUBMISSION_BASELINE_POSITION_SOURCE_INVALID"),
        ("orders.json", [{}], "SUBMISSION_BASELINE_ORDER_SOURCE_INVALID"),
        ("activities.json", [], "SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID"),
        (
            "activities.json",
            [
                {
                    "id": "initial-funding-fixture",
                    "activity_type": "FILL",
                    "date": "2026-08-28",
                    "net_amount": "100000",
                }
            ],
            "SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID",
        ),
        (
            "activities.json",
            [
                {
                    "id": "initial-funding-fixture",
                    "activity_type": "JNLC",
                    "date": "2026-08-28",
                    "net_amount": True,
                }
            ],
            "SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID",
        ),
    ],
)
def test_loader_rejects_source_shape_count_type_and_bool_drift(
    tmp_path: Path, name: str, payload: object, code: str
) -> None:
    sources = _source_payloads()
    sources[name] = _json_bytes(payload)
    directory = _baseline_directory(tmp_path, sources=sources)

    with pytest.raises(SubmissionBaselineImportError, match=code):
        load_submission_baseline_directory(directory)


def test_loader_accepts_legacy_account_without_newer_block_fields(tmp_path: Path) -> None:
    sources = _source_payloads()
    account = json.loads(sources["account.json"])
    for field in ("trading_blocked", "account_blocked", "trade_suspended_by_user"):
        del account[field]
    sources["account.json"] = _json_bytes(account)

    load_submission_baseline_directory(_baseline_directory(tmp_path, sources=sources))

    account["trading_blocked"] = None
    sources["account.json"] = _json_bytes(account)
    with pytest.raises(
        SubmissionBaselineImportError,
        match="SUBMISSION_BASELINE_ACCOUNT_SOURCE_INVALID",
    ):
        load_submission_baseline_directory(_baseline_directory(tmp_path / "null", sources=sources))


def test_loader_rejects_account_fingerprint_and_activity_chronology_drift(
    tmp_path: Path,
) -> None:
    fingerprint_directory = _baseline_directory(
        tmp_path / "fingerprint", manifest_changes={"account_fingerprint": "f" * 64}
    )
    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_ACCOUNT_SOURCE_INVALID"
    ):
        load_submission_baseline_directory(fingerprint_directory)

    sources = _source_payloads()
    sources["activities.json"] = _json_bytes(
        [
            {
                "id": "initial-funding-fixture",
                "activity_type": "JNLC",
                "date": "2026-08-29",
                "net_amount": "100000",
            }
        ]
    )
    chronology_directory = _baseline_directory(tmp_path / "chronology", sources=sources)
    with pytest.raises(
        SubmissionBaselineImportError, match="SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID"
    ):
        load_submission_baseline_directory(chronology_directory)


def test_loader_rejects_noncanonical_source_identity_and_activity_time(
    tmp_path: Path,
) -> None:
    account_sources = _source_payloads()
    account = json.loads(account_sources["account.json"])
    account["id"] = "{" + str(ACCOUNT_ID) + "}"
    account_sources["account.json"] = _json_bytes(account)

    activity_sources = _source_payloads()
    activity = json.loads(activity_sources["activities.json"])
    activity[0]["transaction_time"] = []
    activity_sources["activities.json"] = _json_bytes(activity)

    compact_date_sources = _source_payloads()
    compact_date_activity = json.loads(compact_date_sources["activities.json"])
    compact_date_activity[0]["date"] = "20260828"
    compact_date_sources["activities.json"] = _json_bytes(compact_date_activity)

    for root, sources, code in (
        ("account", account_sources, "SUBMISSION_BASELINE_ACCOUNT_SOURCE_INVALID"),
        ("activity", activity_sources, "SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID"),
        (
            "compact-date",
            compact_date_sources,
            "SUBMISSION_BASELINE_ACTIVITY_SOURCE_INVALID",
        ),
    ):
        with pytest.raises(SubmissionBaselineImportError, match=code):
            load_submission_baseline_directory(
                _baseline_directory(tmp_path / root, sources=sources)
            )


def test_loader_rejects_overflowing_json_number(tmp_path: Path) -> None:
    sources = _source_payloads()
    sources["account.json"] = sources["account.json"].replace(
        b"}\n", b',"unknown_ratio":1e999999}\n'
    )

    with pytest.raises(SubmissionBaselineImportError, match="SUBMISSION_BASELINE_JSON_INVALID"):
        load_submission_baseline_directory(_baseline_directory(tmp_path, sources=sources))


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        (
            "manifest.json",
            b'{"schema_version":1,"schema_version":1}\n',
        ),
        ("account.json", b'{"id":"fixture","id":"fixture","equity":"100000"}\n'),
        ("activities.json", b'[{"activity_type":"JNLC","net_amount":NaN}]\n'),
        ("positions.json", b"[] trailing\n"),
        ("orders.json", (b"[" * 1100) + (b"]" * 1100)),
    ],
)
def test_loader_rejects_duplicate_keys_constants_trailing_data_and_excessive_depth(
    tmp_path: Path, name: str, payload: bytes
) -> None:
    if name == "manifest.json":
        directory = _baseline_directory(tmp_path)
        _replace_private(directory / name, payload)
    else:
        sources = _source_payloads()
        sources[name] = payload
        directory = _baseline_directory(tmp_path, sources=sources)

    with pytest.raises(SubmissionBaselineImportError):
        load_submission_baseline_directory(directory)


def test_import_persists_through_existing_authority_and_replays_exactly(tmp_path: Path) -> None:
    sessions = _sessions()
    baseline = load_submission_baseline_directory(_baseline_directory(tmp_path))

    import_submission_baseline(baseline, sessions)
    import_submission_baseline(baseline, sessions)

    with sessions() as session:
        account = session.get(AccountRoleRow, "SUBMISSION")
        rows = session.scalars(select(SubmissionBaselineRow)).all()
    assert account is not None
    assert account.account_fingerprint == FINGERPRINT
    assert account.equity == Decimal("100000")
    assert account.autonomous_enabled is False
    assert len(rows) == 1
    assert rows[0].baseline_id == uuid5(
        NAMESPACE_URL, f"alphadecay:baseline:SUBMISSION:{FINGERPRINT}"
    )
    assert rows[0].positions_hash == baseline.positions_hash
    assert rows[0].orders_hash == baseline.orders_hash
    assert rows[0].activities_hash == baseline.activities_hash
    assert rows[0].contaminated is False


def test_import_rejects_replay_mismatch_contamination_and_second_baseline(
    tmp_path: Path,
) -> None:
    baseline = load_submission_baseline_directory(_baseline_directory(tmp_path))
    sessions = _sessions()
    import_submission_baseline(baseline, sessions)
    mismatch = SealedSubmissionBaseline(
        account_fingerprint=baseline.account_fingerprint,
        account_source_hash=baseline.account_source_hash,
        equity=baseline.equity,
        captured_at=baseline.captured_at,
        positions_hash="f" * 64,
        orders_hash=baseline.orders_hash,
        activities_hash=baseline.activities_hash,
    )
    with pytest.raises(SubmissionBaselineImportError, match="SUBMISSION_BASELINE_DRIFT"):
        import_submission_baseline(mismatch, sessions)

    with sessions.begin() as session:
        stored = session.scalar(select(SubmissionBaselineRow))
        assert stored is not None
        stored.contaminated = True
    with pytest.raises(SubmissionBaselineImportError, match="SUBMISSION_BASELINE_CONTAMINATED"):
        import_submission_baseline(baseline, sessions)

    second_sessions = _sessions()
    import_submission_baseline(baseline, second_sessions)
    with second_sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role="DEVELOPMENT",
                account_fingerprint="b" * 64,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
        session.add(
            SubmissionBaselineRow(
                baseline_id=uuid5(NAMESPACE_URL, "second-baseline"),
                account_role="DEVELOPMENT",
                account_fingerprint="b" * 64,
                equity=Decimal("100000"),
                captured_at=CAPTURED_AT,
                positions_hash="1" * 64,
                orders_hash="2" * 64,
                activities_hash="3" * 64,
                contaminated=False,
            )
        )
    with pytest.raises(SubmissionBaselineImportError, match="SUBMISSION_BASELINE_SECOND_BASELINE"):
        import_submission_baseline(baseline, second_sessions)


def test_import_rejects_cross_role_partial_and_budget_drift(tmp_path: Path) -> None:
    baseline = load_submission_baseline_directory(_baseline_directory(tmp_path))

    cross_role_sessions = _sessions()
    with cross_role_sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role="DEVELOPMENT",
                account_fingerprint=FINGERPRINT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
        session.add(CompetitionEntryBudgetRow(account_role="DEVELOPMENT"))
    with pytest.raises(SubmissionBaselineImportError, match="SUBMISSION_BASELINE_DRIFT"):
        import_submission_baseline(baseline, cross_role_sessions)

    partial_sessions = _sessions()
    with partial_sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role="SUBMISSION",
                account_fingerprint=FINGERPRINT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    with pytest.raises(SubmissionBaselineImportError, match="SUBMISSION_BASELINE_PARTIAL_STATE"):
        import_submission_baseline(baseline, partial_sessions)

    missing_budget_sessions = _sessions()
    import_submission_baseline(baseline, missing_budget_sessions)
    with missing_budget_sessions.begin() as session:
        budget = session.get(CompetitionEntryBudgetRow, "SUBMISSION")
        assert budget is not None
        session.delete(budget)
    with pytest.raises(SubmissionBaselineImportError, match="SUBMISSION_BASELINE_PARTIAL_STATE"):
        import_submission_baseline(baseline, missing_budget_sessions)

    drift_sessions = _sessions()
    import_submission_baseline(baseline, drift_sessions)
    with drift_sessions.begin() as session:
        budget = session.get(CompetitionEntryBudgetRow, "SUBMISSION")
        assert budget is not None
        budget.entries_used = 1
        budget.gross_approved_risk = Decimal("1")
    with pytest.raises(SubmissionBaselineImportError, match="SUBMISSION_BASELINE_BUDGET_DRIFT"):
        import_submission_baseline(baseline, drift_sessions)


def test_import_rejects_enabled_or_fenced_account_state(tmp_path: Path) -> None:
    baseline = load_submission_baseline_directory(_baseline_directory(tmp_path))
    sessions = _sessions()
    import_submission_baseline(baseline, sessions)
    with sessions.begin() as session:
        account = session.get(AccountRoleRow, "SUBMISSION")
        assert account is not None
        account.autonomous_enabled = True

    with pytest.raises(SubmissionBaselineImportError, match="SUBMISSION_BASELINE_DRIFT"):
        import_submission_baseline(baseline, sessions)


def test_preview_verifies_directory_without_database_or_database_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _baseline_directory(tmp_path)
    monkeypatch.setattr(
        "ops.launch.submission_baseline_import.create_engine",
        lambda *_args, **_kwargs: pytest.fail("preview opened database"),
    )
    monkeypatch.setattr(
        "ops.launch.submission_baseline_import._database_url",
        lambda *_args, **_kwargs: pytest.fail("preview read database input"),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("preview opened network"),
    )

    class NoEnvironment(dict[str, str]):
        def __getitem__(self, _key: str) -> str:
            pytest.fail("preview read environment")

        def get(self, _key: str, _default: object = None) -> str | None:
            pytest.fail("preview read environment")

    monkeypatch.setattr(os, "environ", NoEnvironment())

    assert main(["--baseline-directory", str(directory)]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "account_role": "SUBMISSION",
        "database_write": False,
        "manifest_valid": True,
        "mode": "PREVIEW",
        "source_files_verified": 4,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        ["--persist"],
        ["--database-url-file", "unused"],
        ["--manifest-file", "legacy.json"],
        ["--baseline-directory", "unused", "--manifest-file", "legacy.json"],
    ],
)
def test_launcher_rejects_incomplete_and_legacy_ambiguous_inputs(
    tmp_path: Path, arguments: list[str]
) -> None:
    directory = _baseline_directory(tmp_path)
    normalized = [
        str(directory) if value == "unused" and index == 1 else value
        for index, value in enumerate(arguments)
    ]
    if "--baseline-directory" not in normalized:
        normalized = ["--baseline-directory", str(directory), *normalized]

    with pytest.raises(SystemExit):
        main(normalized)


@pytest.mark.parametrize("kind", ["fifo", "hardlink", "symlinked_parent", "device"])
def test_persistence_rejects_unsafe_database_url_file(
    tmp_path: Path,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _baseline_directory(tmp_path / "artifact")
    private_parent = tmp_path / "database"
    private_parent.mkdir()
    database_url_file = private_parent / "url"
    _write_private(database_url_file, b"sqlite+pysqlite://\n".rstrip())
    selected = database_url_file
    if kind == "fifo":
        database_url_file.unlink()
        os.mkfifo(database_url_file, 0o400)
    elif kind == "hardlink":
        selected = tmp_path / "database-url-hardlink"
        os.link(database_url_file, selected)
    elif kind == "symlinked_parent":
        alias_parent = tmp_path / "database-alias"
        alias_parent.symlink_to(private_parent, target_is_directory=True)
        selected = alias_parent / database_url_file.name
    else:
        selected = Path("/dev/null")
    monkeypatch.setattr(
        "ops.launch.submission_baseline_import.create_engine",
        lambda *_args, **_kwargs: pytest.fail("unsafe input opened database"),
    )

    with pytest.raises(SystemExit):
        main(
            [
                "--baseline-directory",
                str(directory),
                "--database-url-file",
                str(selected),
                "--persist",
            ]
        )


def test_launcher_argument_errors_do_not_echo_untrusted_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _baseline_directory(tmp_path)
    secret_argument = f"--unknown-{ACCOUNT_ID}"

    with pytest.raises(SystemExit):
        main(["--baseline-directory", str(directory), secret_argument])

    output = capsys.readouterr()
    assert str(ACCOUNT_ID) not in output.err
    assert "SUBMISSION_BASELINE_ARGUMENT_INVALID" in output.err
    assert output.out == ""


def test_launcher_errors_are_redacted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _baseline_directory(tmp_path)
    raw_account = _source_payloads()["account.json"]
    _replace_private(directory / "account.json", raw_account + b"corrupt")

    with pytest.raises(SystemExit):
        main(["--baseline-directory", str(directory)])

    output = capsys.readouterr()
    assert str(ACCOUNT_ID) not in output.err
    assert FINGERPRINT not in output.err
    assert "initial-funding-fixture" not in output.err
    assert output.out == ""


def test_launcher_redacts_unexpected_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _baseline_directory(tmp_path)

    def fail_load(_directory: Path) -> SealedSubmissionBaseline:
        raise RuntimeError(f"unexpected private failure for {ACCOUNT_ID} {FINGERPRINT}")

    monkeypatch.setattr(
        "ops.launch.submission_baseline_import.load_submission_baseline_directory",
        fail_load,
    )
    with pytest.raises(SystemExit):
        main(["--baseline-directory", str(directory)])

    output = capsys.readouterr()
    assert "SUBMISSION_BASELINE_INPUT_INVALID" in output.err
    assert str(ACCOUNT_ID) not in output.err
    assert FINGERPRINT not in output.err
    assert output.out == ""
