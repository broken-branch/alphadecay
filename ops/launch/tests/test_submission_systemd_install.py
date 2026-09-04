from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from ops.launch.submission_market_window import (
    MarketWindowError,
    RuntimeDependencies,
    _next_tick,
    load_schedule,
    run_window,
)
from ops.launch.submission_runtime import SubmissionRuntimeConfig
from ops.launch.submission_systemd_install import (
    SystemdInstallError,
    build_installation,
    install_units,
    main,
)

_ET = ZoneInfo("America/New_York")
_ENTRY_SERVICE = "alphadecay-submission-market-window.service"
_ENTRY_TIMER = "alphadecay-submission-market-window.timer"
_LIFECYCLE_SERVICE = "alphadecay-submission-lifecycle-window.service"
_LIFECYCLE_TIMER = "alphadecay-submission-lifecycle-window.timer"
_UNITS = {_ENTRY_SERVICE, _ENTRY_TIMER, _LIFECYCLE_SERVICE, _LIFECYCLE_TIMER}


def _private_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _installation_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    python.chmod(0o700)

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    runtime = private / "runtime.json"
    runtime.write_text("{}", encoding="utf-8")
    runtime.chmod(0o600)
    schedule = private / "schedule.json"
    _private_json(
        schedule,
        {
            "runtime_config": str(runtime),
            "window_start": "2026-09-02T09:45:00-04:00",
            "hard_cutoff": "2026-09-02T10:25:00-04:00",
            "cadence_seconds": 300,
        },
    )
    lifecycle_schedule = private / "lifecycle-schedule.json"
    _private_json(
        lifecycle_schedule,
        {
            "runtime_config": str(runtime),
            "window_start": "2026-09-03T09:45:00-04:00",
            "hard_cutoff": "2026-09-03T10:05:00-04:00",
            "cadence_seconds": 300,
        },
    )
    units = tmp_path / "units"
    units.mkdir(mode=0o700)
    return repo, python, schedule, lifecycle_schedule, units


def _arguments(
    repo: Path,
    python: Path,
    schedule: Path,
    lifecycle_schedule: Path,
    units: Path,
) -> list[str]:
    return [
        "--repo",
        str(repo),
        "--python",
        str(python),
        "--schedule",
        str(schedule),
        "--lifecycle-schedule",
        str(lifecycle_schedule),
        "--unit-directory",
        str(units),
    ]


def test_preview_performs_no_write_or_writer_call_and_redacts_paths(tmp_path: Path, capsys) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    calls = []

    assert (
        main(
            _arguments(repo, python, schedule, lifecycle_schedule, units),
            writer=lambda _installation: calls.append("write"),
            expected_repo=repo,
        )
        == 0
    )

    assert calls == []
    assert list(units.iterdir()) == []
    output = capsys.readouterr().out
    assert json.loads(output)["event"] == "PREVIEW_ONLY"
    assert str(repo) not in output
    assert str(schedule) not in output
    assert "runtime.json" not in output


def test_units_have_two_exact_persistent_timers_and_fixed_no_shell_commands(
    tmp_path: Path,
) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    installation = build_installation(
        repo=repo,
        python=python,
        schedule_path=schedule,
        lifecycle_schedule_path=lifecycle_schedule,
        unit_directory=units,
        expected_repo=repo,
    )

    for service_name, timer_name, schedule_path, on_calendar in (
        (_ENTRY_SERVICE, _ENTRY_TIMER, schedule, "2026-09-02 09:40:00 America/New_York"),
        (
            _LIFECYCLE_SERVICE,
            _LIFECYCLE_TIMER,
            lifecycle_schedule,
            "2026-09-03 09:40:00 America/New_York",
        ),
    ):
        service = installation.files[service_name].decode("utf-8")
        timer = installation.files[timer_name].decode("utf-8")
        assert service.count("ExecStart=") == 1
        assert service.count("WorkingDirectory=") == 1
        assert f"WorkingDirectory={repo}" in service
        assert (
            f"ExecStart={python} -m ops.launch.submission_market_window "
            f"--schedule {schedule_path} --schedule-sha256 "
        ) in service
        assert service.rstrip().endswith("NoNewPrivileges=true")
        assert " --execute\n" in service
        assert "/bin/sh" not in service
        assert " uv " not in service
        assert "owner/autonomy" not in service
        assert "daemon-reload" not in service
        assert "systemctl" not in service
        assert "Wants=network-online.target" in service
        assert "After=network-online.target" in service
        assert "Restart=no" in service
        assert "TimeoutStartSec=50min" in service
        assert "TimeoutStopSec=20s" in service
        assert timer.count("OnCalendar=") == 1
        assert f"OnCalendar={on_calendar}" in timer
        assert "RandomizedDelaySec=0" in timer
        assert "Persistent=true" in timer
        assert f"Unit={service_name}" in timer


def test_installer_accepts_contingency_schedules_without_service_template_change(
    tmp_path: Path,
) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    entry_payload = json.loads(schedule.read_text(encoding="utf-8"))
    entry_payload.update(
        window_start="2026-09-03T09:45:00-04:00",
        hard_cutoff="2026-09-03T10:25:00-04:00",
    )
    _private_json(schedule, entry_payload)
    lifecycle_payload = json.loads(lifecycle_schedule.read_text(encoding="utf-8"))
    lifecycle_payload.update(
        window_start="2026-09-04T09:45:00-04:00",
        hard_cutoff="2026-09-04T10:05:00-04:00",
    )
    _private_json(lifecycle_schedule, lifecycle_payload)

    installation = build_installation(
        repo=repo,
        python=python,
        schedule_path=schedule,
        lifecycle_schedule_path=lifecycle_schedule,
        unit_directory=units,
        expected_repo=repo,
        signal_session=date(2026, 9, 3),
    )

    assert (
        "OnCalendar=2026-09-03 09:40:00 America/New_York"
        in installation.files[_ENTRY_TIMER].decode()
    )
    assert (
        "OnCalendar=2026-09-04 09:40:00 America/New_York"
        in installation.files[_LIFECYCLE_TIMER].decode()
    )
    for service_name in (_ENTRY_SERVICE, _LIFECYCLE_SERVICE):
        service = installation.files[service_name].decode()
        assert service.count("ExecStart=") == 1
        assert "-m ops.launch.submission_market_window" in service
        assert "--execute" in service


def test_apply_atomically_leaves_only_fixed_private_unit_files(tmp_path: Path) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    installation = build_installation(
        repo=repo,
        python=python,
        schedule_path=schedule,
        lifecycle_schedule_path=lifecycle_schedule,
        unit_directory=units,
        expected_repo=repo,
    )

    install_units(installation)
    first = {path.name: path.read_bytes() for path in units.iterdir()}
    install_units(installation)

    assert set(first) == _UNITS
    assert {path.name for path in units.iterdir()} == _UNITS
    for path in units.iterdir():
        details = path.stat()
        assert stat.S_IMODE(details.st_mode) == 0o600
        assert details.st_uid == os.getuid()
        assert details.st_nlink == 1
        assert path.read_bytes() == first[path.name]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("window_start", "SYSTEMD_FROZEN_SCHEDULE_MISMATCH"),
        ("hard_cutoff", "SYSTEMD_FROZEN_SCHEDULE_MISMATCH"),
        ("cadence_seconds", "MARKET_WINDOW_CADENCE_INVALID"),
    ],
)
def test_frozen_schedule_cannot_be_changed(tmp_path: Path, field: str, expected: str) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    payload = json.loads(schedule.read_text(encoding="utf-8"))
    payload[field] = {
        "window_start": "2026-09-02T09:50:00-04:00",
        "hard_cutoff": "2026-09-02T10:30:00-04:00",
        "cadence_seconds": 60,
    }[field]
    _private_json(schedule, payload)

    with pytest.raises((SystemdInstallError, MarketWindowError), match=expected):
        build_installation(
            repo=repo,
            python=python,
            schedule_path=schedule,
            lifecycle_schedule_path=lifecycle_schedule,
            unit_directory=units,
            expected_repo=repo,
        )


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("window_start", "2026-09-03T09:50:00-04:00", "SYSTEMD_FROZEN_SCHEDULE_MISMATCH"),
        ("hard_cutoff", "2026-09-03T10:00:00-04:00", "SYSTEMD_FROZEN_SCHEDULE_MISMATCH"),
        ("cadence_seconds", 60, "MARKET_WINDOW_CADENCE_INVALID"),
    ],
)
def test_lifecycle_schedule_is_exact(
    tmp_path: Path, field: str, value: object, expected: str
) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    payload = json.loads(lifecycle_schedule.read_text(encoding="utf-8"))
    payload[field] = value
    _private_json(lifecycle_schedule, payload)

    with pytest.raises((SystemdInstallError, MarketWindowError), match=expected):
        build_installation(
            repo=repo,
            python=python,
            schedule_path=schedule,
            lifecycle_schedule_path=lifecycle_schedule,
            unit_directory=units,
            expected_repo=repo,
        )


def test_lifecycle_uses_same_runtime_and_ticks_close_through_reconciliation(
    tmp_path: Path,
) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    lifecycle = load_schedule(lifecycle_schedule)

    assert lifecycle.window_start == datetime(2026, 9, 3, 9, 45, tzinfo=_ET)
    assert lifecycle.hard_cutoff == datetime(2026, 9, 3, 10, 5, tzinfo=_ET)
    tick = lifecycle.window_start
    ticks = []
    while tick < lifecycle.hard_cutoff:
        ticks.append(tick)
        tick += lifecycle.cadence
    assert ticks == [
        datetime(2026, 9, 3, 9, 45, tzinfo=_ET),
        datetime(2026, 9, 3, 9, 50, tzinfo=_ET),
        datetime(2026, 9, 3, 9, 55, tzinfo=_ET),
        datetime(2026, 9, 3, 10, 0, tzinfo=_ET),
    ]

    other_runtime = lifecycle_schedule.parent / "other-runtime.json"
    _private_json(other_runtime, {})
    payload = json.loads(lifecycle_schedule.read_text(encoding="utf-8"))
    payload["runtime_config"] = str(other_runtime)
    _private_json(lifecycle_schedule, payload)
    with pytest.raises(SystemdInstallError, match="SYSTEMD_RUNTIME_CONFIG_MISMATCH"):
        build_installation(
            repo=repo,
            python=python,
            schedule_path=schedule,
            lifecycle_schedule_path=lifecycle_schedule,
            unit_directory=units,
            expected_repo=repo,
        )


def test_paths_require_exact_repo_venv_location_owner_and_private_modes(
    tmp_path: Path,
) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    outside = tmp_path / "python"
    outside.write_bytes(b"python")
    outside.chmod(0o700)

    with pytest.raises(SystemdInstallError, match="SYSTEMD_PYTHON_INVALID"):
        build_installation(
            repo=repo,
            python=outside,
            schedule_path=schedule,
            lifecycle_schedule_path=lifecycle_schedule,
            unit_directory=units,
            expected_repo=repo,
        )

    schedule.chmod(0o640)
    with pytest.raises(SystemdInstallError, match="SYSTEMD_SCHEDULE_INVALID"):
        build_installation(
            repo=repo,
            python=python,
            schedule_path=schedule,
            lifecycle_schedule_path=lifecycle_schedule,
            unit_directory=units,
            expected_repo=repo,
        )

    schedule.chmod(0o600)
    units.chmod(0o755)
    with pytest.raises(SystemdInstallError, match="SYSTEMD_UNIT_DIRECTORY_INVALID"):
        build_installation(
            repo=repo,
            python=python,
            schedule_path=schedule,
            lifecycle_schedule_path=lifecycle_schedule,
            unit_directory=units,
            expected_repo=repo,
        )


def test_persistent_catch_up_uses_next_boundary_and_cutoff_still_fails_closed(
    tmp_path: Path,
) -> None:
    repo, python, schedule_path, lifecycle_schedule, units = _installation_paths(tmp_path)
    installation = build_installation(
        repo=repo,
        python=python,
        schedule_path=schedule_path,
        lifecycle_schedule_path=lifecycle_schedule,
        unit_directory=units,
        expected_repo=repo,
    )
    timer = installation.files[_ENTRY_TIMER].decode("utf-8")
    schedule = load_schedule(schedule_path)
    restarted_at = datetime(2026, 9, 2, 9, 52, tzinfo=_ET)

    assert "Persistent=true" in timer
    assert _next_tick(schedule.window_start, restarted_at, schedule.cadence) == datetime(
        2026, 9, 2, 9, 55, tzinfo=_ET
    )

    calls = []
    dependencies = RuntimeDependencies(
        now=lambda: datetime(2026, 9, 2, 10, 25, tzinfo=_ET),
        sleep=lambda _seconds: calls.append("sleep"),
        port_is_clear=lambda: calls.append("port") or True,
        spawn=lambda _command: calls.append("spawn"),
        readiness_probe=lambda: calls.append("ready") or True,
        tick_sender=lambda _environment: calls.append("tick") or "ACCEPTED",
        emit=lambda _event, _at: calls.append("emit"),
    )
    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_CLOSED"):
        run_window(schedule, cast(SubmissionRuntimeConfig, object()), dependencies)
    assert calls == []


def test_existing_symlink_unit_is_rejected_without_touching_target(tmp_path: Path) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    installation = build_installation(
        repo=repo,
        python=python,
        schedule_path=schedule,
        lifecycle_schedule_path=lifecycle_schedule,
        unit_directory=units,
        expected_repo=repo,
    )
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    (units / _ENTRY_SERVICE).symlink_to(target)

    with pytest.raises(SystemdInstallError, match="SYSTEMD_EXISTING_UNIT_INVALID"):
        install_units(installation)

    assert target.read_text(encoding="utf-8") == "unchanged"
    assert {path.name for path in units.iterdir()} == {_ENTRY_SERVICE}


def test_staged_service_rejects_later_schedule_content_drift(tmp_path: Path) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    installation = build_installation(
        repo=repo,
        python=python,
        schedule_path=schedule,
        lifecycle_schedule_path=lifecycle_schedule,
        unit_directory=units,
        expected_repo=repo,
    )
    service = installation.files[_ENTRY_SERVICE].decode("utf-8")
    digest = service.split("--schedule-sha256 ", 1)[1].split(" ", 1)[0]
    payload = json.loads(schedule.read_text(encoding="utf-8"))
    schedule.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_SCHEDULE_DIGEST_MISMATCH"):
        load_schedule(schedule, expected_sha256=digest)


def test_apply_rejects_replaced_unit_directory(tmp_path: Path) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    installation = build_installation(
        repo=repo,
        python=python,
        schedule_path=schedule,
        lifecycle_schedule_path=lifecycle_schedule,
        unit_directory=units,
        expected_repo=repo,
    )
    original = tmp_path / "original-units"
    units.rename(original)
    units.mkdir(mode=0o700)

    with pytest.raises(SystemdInstallError, match="SYSTEMD_UNIT_DIRECTORY_INVALID"):
        install_units(installation)

    assert list(units.iterdir()) == []
    assert list(original.iterdir()) == []


def test_apply_requires_the_exact_four_unit_files(tmp_path: Path) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)
    installation = build_installation(
        repo=repo,
        python=python,
        schedule_path=schedule,
        lifecycle_schedule_path=lifecycle_schedule,
        unit_directory=units,
        expected_repo=repo,
    )

    with pytest.raises(SystemdInstallError, match="SYSTEMD_UNIT_SET_INVALID"):
        install_units(
            replace(
                installation,
                files={_ENTRY_SERVICE: installation.files[_ENTRY_SERVICE]},
            )
        )

    assert list(units.iterdir()) == []


def test_apply_failure_redacts_paths_and_exception_text(tmp_path: Path, capsys) -> None:
    repo, python, schedule, lifecycle_schedule, units = _installation_paths(tmp_path)

    def fail(_installation) -> None:
        raise RuntimeError(f"sensitive detail at {schedule}")

    with pytest.raises(SystemExit):
        main(
            [*_arguments(repo, python, schedule, lifecycle_schedule, units), "--apply"],
            writer=fail,
            expected_repo=repo,
        )

    output = capsys.readouterr()
    assert output.out == ""
    assert "SYSTEMD_INSTALL_FAILED" in output.err
    assert str(repo) not in output.err
    assert str(schedule) not in output.err
    assert "sensitive detail" not in output.err
