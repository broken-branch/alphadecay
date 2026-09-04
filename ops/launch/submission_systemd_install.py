from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

from ops.launch.submission_market_window import MarketWindowSchedule, load_schedule

_ET = ZoneInfo("America/New_York")
_ENTRY_SERVICE_NAME = "alphadecay-submission-market-window.service"
_ENTRY_TIMER_NAME = "alphadecay-submission-market-window.timer"
_LIFECYCLE_SERVICE_NAME = "alphadecay-submission-lifecycle-window.service"
_LIFECYCLE_TIMER_NAME = "alphadecay-submission-lifecycle-window.timer"
_DEFAULT_SIGNAL_SESSION = date(2026, 9, 2)
_CONTINGENCY_SIGNAL_SESSION = date(2026, 9, 3)
_SAFE_PATH = re.compile(r"\A/[A-Za-z0-9._/@+\-]+\Z")


class SystemdInstallError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class UnitInstallation:
    directory: Path
    directory_identity: tuple[int, int]
    files: Mapping[str, bytes]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or stage the fixed SUBMISSION entry and lifecycle user units"
    )
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--schedule", required=True, type=Path)
    parser.add_argument("--lifecycle-schedule", required=True, type=Path)
    parser.add_argument(
        "--signal-session",
        type=_parse_signal_session,
        default=_DEFAULT_SIGNAL_SESSION,
        metavar="YYYY-MM-DD",
    )
    parser.add_argument("--unit-directory", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically stage the four fixed user-unit files",
    )
    return parser


def _parse_signal_session(value: str) -> date:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise argparse.ArgumentTypeError("signal session must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("signal session must use YYYY-MM-DD") from None


def build_installation(
    *,
    repo: Path,
    python: Path,
    schedule_path: Path,
    lifecycle_schedule_path: Path,
    unit_directory: Path,
    expected_repo: Path,
    signal_session: date = _DEFAULT_SIGNAL_SESSION,
) -> UnitInstallation:
    if signal_session not in {_DEFAULT_SIGNAL_SESSION, _CONTINGENCY_SIGNAL_SESSION}:
        raise SystemdInstallError("SYSTEMD_SIGNAL_SESSION_INVALID")
    uid = os.getuid()
    repo = _validate_directory(repo, uid=uid, mode=None, code="SYSTEMD_REPO_INVALID")
    expected_repo = _normalized_absolute(expected_repo, "SYSTEMD_REPO_INVALID")
    if repo != expected_repo:
        raise SystemdInstallError("SYSTEMD_REPO_INVALID")

    python = _validate_python(python, repo=repo, uid=uid)
    schedule_path = _validate_private_file(
        schedule_path,
        uid=uid,
        code="SYSTEMD_SCHEDULE_INVALID",
    )
    schedule_bytes = _read_private_file(
        schedule_path,
        uid=uid,
        code="SYSTEMD_SCHEDULE_INVALID",
    )
    schedule_sha256 = hashlib.sha256(schedule_bytes).hexdigest()
    schedule = load_schedule(
        schedule_path,
        expected_sha256=schedule_sha256,
    )
    lifecycle_schedule_path = _validate_private_file(
        lifecycle_schedule_path,
        uid=uid,
        code="SYSTEMD_LIFECYCLE_SCHEDULE_INVALID",
    )
    lifecycle_schedule_bytes = _read_private_file(
        lifecycle_schedule_path,
        uid=uid,
        code="SYSTEMD_LIFECYCLE_SCHEDULE_INVALID",
    )
    lifecycle_schedule_sha256 = hashlib.sha256(lifecycle_schedule_bytes).hexdigest()
    lifecycle_schedule = load_schedule(
        lifecycle_schedule_path,
        expected_sha256=lifecycle_schedule_sha256,
    )
    _validate_frozen_schedules(schedule, lifecycle_schedule, signal_session=signal_session)
    if schedule.runtime_config != lifecycle_schedule.runtime_config:
        raise SystemdInstallError("SYSTEMD_RUNTIME_CONFIG_MISMATCH")
    _validate_private_file(
        schedule.runtime_config,
        uid=uid,
        code="SYSTEMD_RUNTIME_CONFIG_INVALID",
    )
    unit_directory = _validate_directory(
        unit_directory,
        uid=uid,
        mode=0o700,
        code="SYSTEMD_UNIT_DIRECTORY_INVALID",
    )

    directory_details = unit_directory.stat()
    entry_service = _service_unit(
        description="AlphaDecay bounded SUBMISSION entry window",
        repo=repo,
        python=python,
        schedule=schedule_path,
        schedule_sha256=schedule_sha256,
    )
    lifecycle_service = _service_unit(
        description="AlphaDecay bounded SUBMISSION lifecycle window",
        repo=repo,
        python=python,
        schedule=lifecycle_schedule_path,
        schedule_sha256=lifecycle_schedule_sha256,
    )
    lifecycle_session = _next_weekday(signal_session)
    entry_timer = _timer_unit(
        description=f"Start AlphaDecay for the {signal_session:%B %-d} entry window",
        on_calendar=_on_calendar(signal_session),
        service=_ENTRY_SERVICE_NAME,
    )
    lifecycle_timer = _timer_unit(
        description=f"Start AlphaDecay for the {lifecycle_session:%B %-d} lifecycle window",
        on_calendar=_on_calendar(lifecycle_session),
        service=_LIFECYCLE_SERVICE_NAME,
    )
    return UnitInstallation(
        directory=unit_directory,
        directory_identity=(directory_details.st_dev, directory_details.st_ino),
        files=MappingProxyType(
            {
                _ENTRY_SERVICE_NAME: entry_service.encode("utf-8"),
                _ENTRY_TIMER_NAME: entry_timer.encode("utf-8"),
                _LIFECYCLE_SERVICE_NAME: lifecycle_service.encode("utf-8"),
                _LIFECYCLE_TIMER_NAME: lifecycle_timer.encode("utf-8"),
            }
        ),
    )


def install_units(installation: UnitInstallation) -> None:
    if set(installation.files) != {
        _ENTRY_SERVICE_NAME,
        _ENTRY_TIMER_NAME,
        _LIFECYCLE_SERVICE_NAME,
        _LIFECYCLE_TIMER_NAME,
    }:
        raise SystemdInstallError("SYSTEMD_UNIT_SET_INVALID")
    directory_fd = os.open(
        installation.directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    staged: dict[str, str] = {}
    try:
        details = os.fstat(directory_fd)
        if (
            details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
            or (details.st_dev, details.st_ino) != installation.directory_identity
        ):
            raise SystemdInstallError("SYSTEMD_UNIT_DIRECTORY_INVALID")
        for name in installation.files:
            _validate_unit_name(name)
            _validate_existing_unit(directory_fd, name)
        for name, content in installation.files.items():
            temporary = f".{name}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.fchmod(descriptor, 0o600)
                _write_all(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            staged[name] = temporary
        for name, temporary in staged.items():
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        os.fsync(directory_fd)
    except Exception:
        for temporary in staged.values():
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)
        raise
    finally:
        os.close(directory_fd)


def main(
    argv: Sequence[str] | None = None,
    *,
    writer: Callable[[UnitInstallation], None] = install_units,
    expected_repo: Path | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        installation = build_installation(
            repo=args.repo,
            python=args.python,
            schedule_path=args.schedule,
            lifecycle_schedule_path=args.lifecycle_schedule,
            unit_directory=args.unit_directory,
            expected_repo=expected_repo or _repository_root(),
            signal_session=args.signal_session,
        )
        if not args.apply:
            _emit("PREVIEW_ONLY")
            return 0
        writer(installation)
        _emit("UNITS_STAGED")
        return 0
    except Exception as error:
        if isinstance(error, SystemExit):
            raise
        code = error.code if isinstance(error, SystemdInstallError) else "SYSTEMD_INSTALL_FAILED"
        parser.error(code)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _validate_frozen_schedules(
    entry: MarketWindowSchedule,
    lifecycle: MarketWindowSchedule,
    *,
    signal_session: date,
) -> None:
    lifecycle_session = _next_weekday(signal_session)
    expected_entry_start = datetime.combine(signal_session, time(9, 45), _ET)
    expected_lifecycle_start = datetime.combine(lifecycle_session, time(9, 45), _ET)
    if (
        entry.window_start != expected_entry_start
        or entry.hard_cutoff != expected_entry_start + timedelta(minutes=40)
        or entry.cadence.total_seconds() != 300
        or lifecycle.window_start != expected_lifecycle_start
        or lifecycle.hard_cutoff != expected_lifecycle_start + timedelta(minutes=20)
        or lifecycle.cadence.total_seconds() != 300
    ):
        raise SystemdInstallError("SYSTEMD_FROZEN_SCHEDULE_MISMATCH")


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _on_calendar(value: date) -> str:
    return f"{value.isoformat()} 09:40:00 America/New_York"


def _service_unit(
    *,
    description: str,
    repo: Path,
    python: Path,
    schedule: Path,
    schedule_sha256: str,
) -> str:
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={repo}\n"
        f"ExecStart={python} -m ops.launch.submission_market_window "
        f"--schedule {schedule} --schedule-sha256 {schedule_sha256} --execute\n"
        "Restart=no\n"
        "TimeoutStartSec=50min\n"
        "TimeoutStopSec=20s\n"
        "KillMode=control-group\n"
        "NoNewPrivileges=true\n"
    )


def _timer_unit(*, description: str, on_calendar: str, service: str) -> str:
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={on_calendar}\n"
        "AccuracySec=1s\n"
        "RandomizedDelaySec=0\n"
        "Persistent=true\n"
        f"Unit={service}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _normalized_absolute(path: Path, code: str) -> Path:
    value = str(path)
    if not path.is_absolute() or not _SAFE_PATH.fullmatch(value):
        raise SystemdInstallError(code)
    normalized = Path(os.path.normpath(value))
    if normalized != path:
        raise SystemdInstallError(code)
    return path


def _validate_directory(path: Path, *, uid: int, mode: int | None, code: str) -> Path:
    path = _normalized_absolute(path, code)
    try:
        details = path.lstat()
        if path.resolve(strict=True) != path:
            raise SystemdInstallError(code)
    except OSError:
        raise SystemdInstallError(code) from None
    permissions = stat.S_IMODE(details.st_mode)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != uid
        or permissions & 0o022
        or (mode is not None and permissions != mode)
    ):
        raise SystemdInstallError(code)
    return path


def _validate_private_file(path: Path, *, uid: int, code: str) -> Path:
    path = _normalized_absolute(path, code)
    try:
        details = path.lstat()
        if path.resolve(strict=True) != path:
            raise SystemdInstallError(code)
    except OSError:
        raise SystemdInstallError(code) from None
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != uid
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_nlink != 1
    ):
        raise SystemdInstallError(code)
    return path


def _validate_python(path: Path, *, repo: Path, uid: int) -> Path:
    path = _normalized_absolute(path, "SYSTEMD_PYTHON_INVALID")
    if path != repo / ".venv/bin/python":
        raise SystemdInstallError("SYSTEMD_PYTHON_INVALID")
    try:
        link_details = path.lstat()
        resolved = path.resolve(strict=True)
        target_details = resolved.stat()
        stable_link_details = path.lstat()
        stable_resolved = path.resolve(strict=True)
        stable_target_details = stable_resolved.stat()
    except OSError:
        raise SystemdInstallError("SYSTEMD_PYTHON_INVALID") from None
    if (
        link_details.st_uid != uid
        or (link_details.st_dev, link_details.st_ino)
        != (stable_link_details.st_dev, stable_link_details.st_ino)
        or resolved != stable_resolved
        or not (stat.S_ISREG(link_details.st_mode) or stat.S_ISLNK(link_details.st_mode))
        or link_details.st_nlink != 1
        or not stat.S_ISREG(target_details.st_mode)
        or target_details.st_uid != uid
        or stat.S_IMODE(target_details.st_mode) & 0o022
        or target_details.st_nlink != 1
        or (target_details.st_dev, target_details.st_ino)
        != (stable_target_details.st_dev, stable_target_details.st_ino)
        or not os.access(path, os.X_OK)
    ):
        raise SystemdInstallError("SYSTEMD_PYTHON_INVALID")
    return path


def _read_private_file(path: Path, *, uid: int, code: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise SystemdInstallError(code) from None
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != uid
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise SystemdInstallError(code)
        result = bytearray()
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > 16 * 1024:
                raise SystemdInstallError(code)
        stable_details = path.lstat()
        if (stable_details.st_dev, stable_details.st_ino) != (
            details.st_dev,
            details.st_ino,
        ):
            raise SystemdInstallError(code)
        return bytes(result)
    except OSError:
        raise SystemdInstallError(code) from None
    finally:
        os.close(descriptor)


def _validate_existing_unit(directory_fd: int, name: str) -> None:
    try:
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_nlink != 1
    ):
        raise SystemdInstallError("SYSTEMD_EXISTING_UNIT_INVALID")


def _validate_unit_name(name: str) -> None:
    if name not in {
        _ENTRY_SERVICE_NAME,
        _ENTRY_TIMER_NAME,
        _LIFECYCLE_SERVICE_NAME,
        _LIFECYCLE_TIMER_NAME,
    }:
        raise SystemdInstallError("SYSTEMD_UNIT_NAME_INVALID")


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise SystemdInstallError("SYSTEMD_UNIT_WRITE_FAILED")
        offset += written


def _emit(event: str) -> None:
    print(
        json.dumps(
            {
                "event": event,
                "entry_service": _ENTRY_SERVICE_NAME,
                "entry_timer": _ENTRY_TIMER_NAME,
                "lifecycle_service": _LIFECYCLE_SERVICE_NAME,
                "lifecycle_timer": _LIFECYCLE_TIMER_NAME,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
