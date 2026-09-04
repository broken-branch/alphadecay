from __future__ import annotations

import json
import os
import signal
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops.launch.submission_market_window import (
    MarketWindowError,
    RuntimeDependencies,
    load_schedule,
    main,
    run_window,
    schedule_payloads,
)
from ops.launch.submission_runtime import load_config as load_runtime_config

_ET = ZoneInfo("America/New_York")


def _runtime_payload() -> dict[str, str]:
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
        "ALPACA_API_KEY": "private-paper-key",
        "ALPACA_SECRET_KEY": "private-paper-secret",
        "ALPACA_PAPER_TRADE": "true",
        "DATABASE_URL": "postgresql://user:secret@localhost/alphadecay",
        "GEMINI_API_KEY": "private-model-key",
        "APP_OWNER_ACCESS_CODE": "o" * 16,
        "APP_SESSION_SECRET": "s" * 32,
        "APP_PROVIDER_SETTINGS_SECRET": "p" * 32,
        "APP_OPENAI_COMPATIBLE_ORIGINS": "",
        "APP_ALLOWED_ORIGIN": "https://alphadecay.example",
        "SCHEDULER_TOKEN": "t" * 32,
    }


def _private_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _files(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime.json"
    _private_json(runtime, _runtime_payload())
    schedule = tmp_path / "schedule.json"
    _private_json(
        schedule,
        {
            "runtime_config": str(runtime),
            "window_start": "2026-06-15T10:00:00-04:00",
            "hard_cutoff": "2026-06-15T10:15:00-04:00",
            "cadence_seconds": 300,
        },
    )
    return runtime, schedule


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeProcess:
    def __init__(self, *, dies_after_polls: int | None = None) -> None:
        self.dies_after_polls = dies_after_polls
        self.polls = 0
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        self.polls += 1
        if self.terminated or self.killed:
            return 0
        if self.dies_after_polls is not None and self.polls >= self.dies_after_polls:
            return 9
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


def _dependencies(
    clock: FakeClock,
    process: FakeProcess,
    *,
    readiness_probe=lambda: True,
    tick_sender=lambda _environment: "ACCEPTED",
    port_is_clear=lambda: True,
):
    commands: list[tuple[str, ...]] = []
    events: list[tuple[str, datetime]] = []
    ticks: list[dict[str, str]] = []

    def spawn(command):
        commands.append(tuple(command))
        return process

    def send(environment):
        ticks.append(dict(environment))
        return tick_sender(environment)

    return (
        RuntimeDependencies(
            now=clock.now,
            sleep=clock.sleep,
            port_is_clear=port_is_clear,
            spawn=spawn,
            readiness_probe=readiness_probe,
            tick_sender=send,
            emit=lambda event, at: events.append((event, at)),
        ),
        commands,
        events,
        ticks,
    )


def test_default_is_preview_only_with_no_process_or_network(tmp_path: Path, capsys) -> None:
    _, schedule = _files(tmp_path)
    used = []
    dependencies = RuntimeDependencies(
        now=lambda: used.append("clock") or datetime.now(tz=_ET),
        sleep=lambda _seconds: used.append("sleep"),
        port_is_clear=lambda: used.append("port") or True,
        spawn=lambda _command: used.append("spawn"),
        readiness_probe=lambda: used.append("http") or True,
        tick_sender=lambda _environment: used.append("tick") or "ACCEPTED",
        emit=lambda _event, _at: used.append("emit"),
    )

    assert main(["--schedule", str(schedule)], dependencies=dependencies) == 0

    assert used == []
    output = capsys.readouterr().out
    assert json.loads(output)["event"] == "PREVIEW_ONLY"
    assert "private-paper-key" not in output
    assert "secret" not in output


def test_schedule_generation_defaults_and_thursday_contingency(tmp_path: Path) -> None:
    runtime = (tmp_path / "runtime.json").absolute()

    default_entry, default_lifecycle = schedule_payloads(runtime)
    entry, lifecycle = schedule_payloads(runtime, signal_session=date(2026, 9, 3))

    assert default_entry == {
        "runtime_config": str(runtime),
        "window_start": "2026-09-02T09:45:00-04:00",
        "hard_cutoff": "2026-09-02T10:25:00-04:00",
        "cadence_seconds": 300,
    }
    assert default_lifecycle["window_start"] == "2026-09-03T09:45:00-04:00"
    assert default_lifecycle["hard_cutoff"] == "2026-09-03T10:05:00-04:00"
    assert entry["window_start"] == "2026-09-03T09:45:00-04:00"
    assert entry["hard_cutoff"] == "2026-09-03T10:25:00-04:00"
    assert lifecycle["window_start"] == "2026-09-04T09:45:00-04:00"
    assert lifecycle["hard_cutoff"] == "2026-09-04T10:05:00-04:00"


def test_cli_generates_private_contingency_schedule_pair(tmp_path: Path, capsys) -> None:
    runtime = (tmp_path / "runtime.json").absolute()
    entry = (tmp_path / "entry.json").absolute()
    lifecycle = (tmp_path / "lifecycle.json").absolute()

    assert (
        main(
            [
                "--runtime-config",
                str(runtime),
                "--entry-output",
                str(entry),
                "--lifecycle-output",
                str(lifecycle),
                "--signal-session",
                "2026-09-03",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == {"event": "SCHEDULES_WRITTEN"}
    assert entry.stat().st_mode & 0o777 == 0o600
    assert lifecycle.stat().st_mode & 0o777 == 0o600
    assert load_schedule(entry).window_start == datetime(2026, 9, 3, 9, 45, tzinfo=_ET)
    assert load_schedule(lifecycle).hard_cutoff == datetime(2026, 9, 4, 10, 5, tzinfo=_ET)


def test_schedule_requires_exact_regular_0600_non_env_file(tmp_path: Path) -> None:
    _, schedule = _files(tmp_path)
    env_file = tmp_path / ".env.schedule"
    env_file.write_bytes(schedule.read_bytes())
    env_file.chmod(0o600)
    public = tmp_path / "public.json"
    public.write_bytes(schedule.read_bytes())
    public.chmod(0o644)
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(schedule)
    hardlink = tmp_path / "hardlink.json"
    os.link(schedule, hardlink)

    for path in (env_file, public, symlink, hardlink):
        with pytest.raises(MarketWindowError):
            load_schedule(path)


def test_schedule_rejects_duplicate_keys(tmp_path: Path) -> None:
    _, schedule = _files(tmp_path)
    schedule.write_text(
        schedule.read_text(encoding="utf-8").replace(
            '"cadence_seconds": 300',
            '"cadence_seconds": 300, "cadence_seconds": 300',
        ),
        encoding="utf-8",
    )

    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_CONFIG_INVALID"):
        load_schedule(schedule)


@pytest.mark.parametrize(
    "selector", ["account", "symbol", "contract", "order", "quantity", "price"]
)
def test_schedule_rejects_selectors_and_unknown_keys(tmp_path: Path, selector: str) -> None:
    runtime, schedule = _files(tmp_path)
    payload = json.loads(schedule.read_text(encoding="utf-8"))
    payload[selector] = "forbidden"
    _private_json(schedule, payload)

    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_CONFIG_INVALID"):
        load_schedule(schedule)

    assert runtime.exists()


@pytest.mark.parametrize(
    ("start", "cutoff"),
    [
        ("2026-06-15T10:00:00Z", "2026-06-15T10:15:00Z"),
        ("2026-11-01T01:30:00-04:00", "2026-11-01T01:45:00-04:00"),
        ("2026-03-08T02:30:00-05:00", "2026-03-08T02:45:00-05:00"),
        ("2026-06-15T10:00:30-04:00", "2026-06-15T10:15:00-04:00"),
    ],
)
def test_schedule_rejects_non_et_ambiguous_nonexistent_or_inexact_times(
    tmp_path: Path, start: str, cutoff: str
) -> None:
    _, schedule = _files(tmp_path)
    payload = json.loads(schedule.read_text(encoding="utf-8"))
    payload.update(window_start=start, hard_cutoff=cutoff)
    _private_json(schedule, payload)

    with pytest.raises(MarketWindowError):
        load_schedule(schedule)


@pytest.mark.parametrize("cadence", [299, 301, 300.0, "300", True])
def test_cadence_is_exactly_five_minutes(tmp_path: Path, cadence: object) -> None:
    _, schedule = _files(tmp_path)
    payload = json.loads(schedule.read_text(encoding="utf-8"))
    payload["cadence_seconds"] = cadence
    _private_json(schedule, payload)

    if type(cadence) is int and cadence == 300:
        pytest.fail("invalid test input")
    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_CADENCE_INVALID"):
        load_schedule(schedule)


def test_before_window_waits_then_ticks_on_exact_cadence_and_stops_at_cutoff(
    tmp_path: Path,
) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    schedule = load_schedule(schedule_path)
    runtime = load_runtime_config(runtime_path, autonomous=True)
    clock = FakeClock(datetime(2026, 6, 15, 9, 59, 58, tzinfo=_ET))
    process = FakeProcess()
    dependencies, commands, events, ticks = _dependencies(clock, process)

    run_window(schedule, runtime, dependencies)

    assert commands == [
        (
            sys.executable,
            "-m",
            "ops.launch.submission_runtime",
            "--config",
            str(runtime_path),
            "--serve",
            "--autonomous",
        )
    ]
    assert [at.strftime("%H:%M:%S") for event, at in events if event == "TICK_ACCEPTED"] == [
        "10:00:00",
        "10:05:00",
        "10:10:00",
    ]
    assert all(
        set(tick) == {"ALPHADECAY_SCHEDULER_URL", "ALPHADECAY_SCHEDULER_TOKEN"} for tick in ticks
    )
    assert all(
        tick["ALPHADECAY_SCHEDULER_URL"].endswith("/api/internal/scheduler/tick") for tick in ticks
    )
    assert events[-2][0] == "HARD_CUTOFF_REACHED"
    assert events[-1][0] == "RUNTIME_STOPPED"
    assert process.terminated is True


def test_pending_entry_recovery_does_not_stop_the_next_scheduled_tick(tmp_path: Path) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    schedule = replace(
        load_schedule(schedule_path),
        hard_cutoff=datetime(2026, 6, 15, 10, 10, tzinfo=_ET),
    )
    runtime = load_runtime_config(runtime_path, autonomous=True)
    clock = FakeClock(datetime(2026, 6, 15, 9, 59, 58, tzinfo=_ET))
    process = FakeProcess()
    codes = iter(("ENTRY_EXECUTION_RECOVERY_PENDING", "PROVIDER_FAILURE_NO_TRADE"))
    accepted_codes: list[str] = []

    def send_tick(_environment) -> str:
        code = next(codes)
        accepted_codes.append(code)
        return code

    dependencies, _commands, events, ticks = _dependencies(
        clock,
        process,
        tick_sender=send_tick,
    )

    run_window(schedule, runtime, dependencies)

    assert len(ticks) == 2
    assert accepted_codes == [
        "ENTRY_EXECUTION_RECOVERY_PENDING",
        "PROVIDER_FAILURE_NO_TRADE",
    ]
    assert [event for event, _at in events].count("TICK_ACCEPTED") == 2
    assert events[-2][0] == "HARD_CUTOFF_REACHED"
    assert events[-1][0] == "RUNTIME_STOPPED"


def test_unresolved_entry_recovery_is_recorded_through_cutoff(tmp_path: Path) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    schedule = replace(
        load_schedule(schedule_path),
        hard_cutoff=datetime(2026, 6, 15, 10, 10, tzinfo=_ET),
    )
    runtime = load_runtime_config(runtime_path, autonomous=True)
    clock = FakeClock(datetime(2026, 6, 15, 9, 59, 58, tzinfo=_ET))
    process = FakeProcess()
    accepted_codes: list[str] = []

    def send_tick(_environment) -> str:
        accepted_codes.append("ENTRY_EXECUTION_RECOVERY_PENDING")
        return accepted_codes[-1]

    dependencies, _commands, events, ticks = _dependencies(
        clock,
        process,
        tick_sender=send_tick,
    )

    run_window(schedule, runtime, dependencies)

    assert len(ticks) == 2
    assert accepted_codes == [
        "ENTRY_EXECUTION_RECOVERY_PENDING",
        "ENTRY_EXECUTION_RECOVERY_PENDING",
    ]
    assert [event for event, _at in events].count("TICK_ACCEPTED") == 2
    assert events[-2] == ("HARD_CUTOFF_REACHED", schedule.hard_cutoff)
    assert events[-1] == ("RUNTIME_STOPPED", schedule.hard_cutoff)
    assert process.terminated is True


def test_lifecycle_window_sends_ten_oclock_reconciliation_tick_before_cutoff(
    tmp_path: Path,
) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    payload = json.loads(schedule_path.read_text(encoding="utf-8"))
    payload.update(
        window_start="2026-09-03T09:45:00-04:00",
        hard_cutoff="2026-09-03T10:05:00-04:00",
    )
    _private_json(schedule_path, payload)
    schedule = load_schedule(schedule_path)
    runtime = load_runtime_config(runtime_path, autonomous=True)
    clock = FakeClock(datetime(2026, 9, 3, 9, 44, 58, tzinfo=_ET))
    process = FakeProcess()
    dependencies, _, events, _ = _dependencies(clock, process)

    run_window(schedule, runtime, dependencies)

    assert [at.strftime("%H:%M") for event, at in events if event == "TICK_ACCEPTED"] == [
        "09:45",
        "09:50",
        "09:55",
        "10:00",
    ]
    assert next(at for event, at in events if event == "HARD_CUTOFF_REACHED") == datetime(
        2026, 9, 3, 10, 5, tzinfo=_ET
    )


def test_too_early_fails_before_process_or_network(tmp_path: Path) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    schedule = load_schedule(schedule_path)
    runtime = load_runtime_config(runtime_path, autonomous=True)
    clock = FakeClock(datetime(2026, 6, 15, 9, 54, 59, tzinfo=_ET))
    process = FakeProcess()
    dependencies, commands, events, ticks = _dependencies(clock, process)

    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_NOT_OPEN"):
        run_window(schedule, runtime, dependencies)

    assert commands == []
    assert events == []
    assert ticks == []


def test_occupied_loopback_port_fails_before_spawn_or_tick(tmp_path: Path) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    schedule = load_schedule(schedule_path)
    runtime = load_runtime_config(runtime_path, autonomous=True)
    clock = FakeClock(datetime(2026, 6, 15, 9, 59, 58, tzinfo=_ET))
    process = FakeProcess()
    dependencies, commands, events, ticks = _dependencies(
        clock,
        process,
        port_is_clear=lambda: False,
    )

    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_PORT_OCCUPIED"):
        run_window(schedule, runtime, dependencies)

    assert commands == []
    assert events == []
    assert ticks == []


def test_starting_within_window_uses_next_cadence_boundary(tmp_path: Path) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    schedule = load_schedule(schedule_path)
    runtime = load_runtime_config(runtime_path, autonomous=True)
    clock = FakeClock(datetime(2026, 6, 15, 10, 2, tzinfo=_ET))
    process = FakeProcess()
    dependencies, _, events, _ = _dependencies(clock, process)

    run_window(schedule, runtime, dependencies)

    assert [at.strftime("%H:%M") for event, at in events if event == "TICK_ACCEPTED"] == [
        "10:05",
        "10:10",
    ]


def test_after_window_fails_before_process_or_network(tmp_path: Path) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    schedule = load_schedule(schedule_path)
    runtime = load_runtime_config(runtime_path, autonomous=True)
    clock = FakeClock(datetime(2026, 6, 15, 10, 15, tzinfo=_ET))
    process = FakeProcess()
    dependencies, commands, events, ticks = _dependencies(clock, process)

    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_CLOSED"):
        run_window(schedule, runtime, dependencies)

    assert commands == []
    assert events == []
    assert ticks == []


def test_child_death_fails_closed_and_cleanup_runs(tmp_path: Path) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    schedule = load_schedule(schedule_path)
    runtime = load_runtime_config(runtime_path, autonomous=True)
    clock = FakeClock(datetime(2026, 6, 15, 9, 59, 58, tzinfo=_ET))
    process = FakeProcess(dies_after_polls=3)
    dependencies, _, events, ticks = _dependencies(clock, process)

    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_CHILD_DIED"):
        run_window(schedule, runtime, dependencies)

    assert ticks == []
    assert events[-1][0] == "RUNTIME_STOPPED"


def test_termination_signal_fails_closed_and_cleans_up(tmp_path: Path) -> None:
    _, schedule_path = _files(tmp_path)
    clock = FakeClock(datetime(2026, 6, 15, 10, 0, tzinfo=_ET))
    process = FakeProcess()

    def terminate_self() -> bool:
        os.kill(os.getpid(), signal.SIGTERM)
        return True

    dependencies, _, events, ticks = _dependencies(
        clock,
        process,
        readiness_probe=terminate_self,
    )

    with pytest.raises(SystemExit):
        main(["--schedule", str(schedule_path), "--execute"], dependencies=dependencies)

    assert ticks == []
    assert events[-1][0] == "RUNTIME_STOPPED"
    assert process.terminated is True


def test_readiness_and_tick_auth_failure_fail_closed_and_cleanup(
    tmp_path: Path,
) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    schedule = load_schedule(schedule_path)
    runtime = load_runtime_config(runtime_path, autonomous=True)

    for kwargs, expected in (
        ({"readiness_probe": lambda: (_ for _ in ()).throw(RuntimeError("secret"))}, "READINESS"),
        (
            {"tick_sender": lambda _environment: (_ for _ in ()).throw(RuntimeError("secret"))},
            "TICK",
        ),
    ):
        clock = FakeClock(datetime(2026, 6, 15, 10, 0, tzinfo=_ET))
        process = FakeProcess()
        dependencies, _, events, _ = _dependencies(clock, process, **kwargs)
        with pytest.raises(MarketWindowError, match=f"MARKET_WINDOW_{expected}_FAILED") as error:
            run_window(schedule, runtime, dependencies)
        assert "secret" not in str(error.value)
        assert events[-1][0] == "RUNTIME_STOPPED"
        assert process.terminated is True


def test_readiness_timeout_sends_no_tick_and_cleans_up(tmp_path: Path) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    schedule = load_schedule(schedule_path)
    runtime = load_runtime_config(runtime_path, autonomous=True)
    clock = FakeClock(datetime(2026, 6, 15, 10, 0, tzinfo=_ET))
    process = FakeProcess()
    dependencies, _, events, ticks = _dependencies(
        clock,
        process,
        readiness_probe=lambda: False,
    )

    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_READINESS_FAILED"):
        run_window(schedule, runtime, dependencies)

    assert ticks == []
    assert events[-1][0] == "RUNTIME_STOPPED"
    assert process.terminated is True


def test_execution_has_no_durable_arm_call_or_selector_and_logs_are_redacted(
    tmp_path: Path, capsys
) -> None:
    runtime_path, schedule_path = _files(tmp_path)
    clock = FakeClock(datetime(2026, 6, 15, 10, 14, 59, tzinfo=_ET))
    process = FakeProcess()
    dependencies, commands, _, _ = _dependencies(clock, process)

    def emit(event, at):
        print(json.dumps({"event": event, "at": at.isoformat()}))

    dependencies = replace(dependencies, emit=emit)

    assert (
        main(
            ["--schedule", str(schedule_path), "--execute"],
            dependencies=dependencies,
        )
        == 0
    )

    command_text = " ".join(commands[0])
    assert "/api/owner/autonomy" not in command_text
    assert "enable" not in command_text.lower()
    selectors = ("symbol", "contract", "order", "quantity", "price")
    assert not any(selector in command_text.lower() for selector in selectors)
    output = capsys.readouterr().out
    assert "private-paper-key" not in output
    assert "paper-secret" not in output
    assert "private-model-key" not in output
    assert "t" * 32 not in output
