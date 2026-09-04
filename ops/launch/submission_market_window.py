from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import FrameType
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from ops.launch.submission_runtime import (
    SubmissionRuntimeConfig,
    send_local_tick,
)
from ops.launch.submission_runtime import (
    load_config as load_runtime_config,
)

_FILE_LIMIT = 16 * 1024
_ET = ZoneInfo("America/New_York")
_CADENCE_SECONDS = 300
_MAXIMUM_START_LEAD = timedelta(minutes=5)
_FIXED_HOST = "127.0.0.1"
_FIXED_PORT = 8000
_READY_URL = f"http://{_FIXED_HOST}:{_FIXED_PORT}/api/health"
_TICK_URL = f"http://{_FIXED_HOST}:{_FIXED_PORT}/api/internal/scheduler/tick"
_SCHEDULE_KEYS = {
    "runtime_config",
    "window_start",
    "hard_cutoff",
    "cadence_seconds",
}
_DEFAULT_SIGNAL_SESSION = date(2026, 9, 2)
_CONTINGENCY_SIGNAL_SESSION = date(2026, 9, 3)
_FINAL_SIGNAL_SESSION = date(2026, 9, 4)


class MarketWindowError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProcessPort(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


@dataclass(frozen=True)
class MarketWindowSchedule:
    runtime_config: Path
    window_start: datetime
    hard_cutoff: datetime
    cadence: timedelta


@dataclass(frozen=True)
class RuntimeDependencies:
    now: Callable[[], datetime]
    sleep: Callable[[float], None]
    port_is_clear: Callable[[], bool]
    spawn: Callable[[Sequence[str]], ProcessPort]
    readiness_probe: Callable[[], bool]
    tick_sender: Callable[[Mapping[str, str]], str]
    emit: Callable[[str, datetime], None]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or run one bounded local SUBMISSION market window"
    )
    parser.add_argument("--schedule", type=Path)
    parser.add_argument("--schedule-sha256")
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--entry-output", type=Path)
    parser.add_argument("--lifecycle-output", type=Path)
    parser.add_argument(
        "--signal-session",
        type=_parse_signal_session,
        default=_DEFAULT_SIGNAL_SESSION,
        metavar="YYYY-MM-DD",
        help="signal session used only when generating entry and lifecycle schedules",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="start the fixed local runtime and run the bounded tick window",
    )
    return parser


def _parse_signal_session(value: str) -> date:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise argparse.ArgumentTypeError("signal session must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("signal session must use YYYY-MM-DD") from None


def schedule_payloads(
    runtime_config: Path,
    *,
    signal_session: date = _DEFAULT_SIGNAL_SESSION,
) -> tuple[dict[str, object], dict[str, object]]:
    if signal_session not in {
        _DEFAULT_SIGNAL_SESSION,
        _CONTINGENCY_SIGNAL_SESSION,
        _FINAL_SIGNAL_SESSION,
    }:
        raise MarketWindowError("MARKET_WINDOW_SIGNAL_SESSION_INVALID")
    if not runtime_config.is_absolute() or _has_env_component(runtime_config):
        raise MarketWindowError("MARKET_WINDOW_RUNTIME_CONFIG_INVALID")
    lifecycle_session = _next_weekday(signal_session)
    entry_start = datetime.combine(signal_session, time(9, 45), _ET)
    lifecycle_start = datetime.combine(lifecycle_session, time(9, 45), _ET)
    common = {
        "runtime_config": str(runtime_config),
        "cadence_seconds": _CADENCE_SECONDS,
    }
    return (
        {
            **common,
            "window_start": entry_start.isoformat(),
            "hard_cutoff": (entry_start + timedelta(minutes=40)).isoformat(),
        },
        {
            **common,
            "window_start": lifecycle_start.isoformat(),
            "hard_cutoff": (lifecycle_start + timedelta(minutes=20)).isoformat(),
        },
    )


def write_schedule_pair(
    *,
    runtime_config: Path,
    entry_output: Path,
    lifecycle_output: Path,
    signal_session: date = _DEFAULT_SIGNAL_SESSION,
) -> None:
    entry, lifecycle = schedule_payloads(runtime_config, signal_session=signal_session)
    outputs = ((entry_output, entry), (lifecycle_output, lifecycle))
    if entry_output == lifecycle_output:
        raise MarketWindowError("MARKET_WINDOW_OUTPUT_INVALID")
    temporary_paths: list[Path] = []
    try:
        for output, payload in outputs:
            _validate_new_output(output)
            temporary = output.parent / f".{output.name}.{uuid4().hex}.tmp"
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                offset = 0
                while offset < len(encoded):
                    written = os.write(descriptor, encoded[offset:])
                    if written <= 0:
                        raise OSError
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            temporary_paths.append(temporary)
        for (output, _), temporary in zip(outputs, temporary_paths, strict=True):
            os.replace(temporary, output)
        temporary_paths.clear()
    finally:
        for temporary in temporary_paths:
            with suppress(FileNotFoundError):
                temporary.unlink()


def _validate_new_output(path: Path) -> None:
    if _has_env_component(path) or not path.is_absolute():
        raise MarketWindowError("MARKET_WINDOW_OUTPUT_INVALID")
    if path.exists() or path.is_symlink() or not path.parent.is_dir() or path.parent.is_symlink():
        raise MarketWindowError("MARKET_WINDOW_OUTPUT_INVALID")


def _has_env_component(path: Path) -> bool:
    return any(part.startswith(".env") for part in path.parts)


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def load_schedule(path: Path, *, expected_sha256: str | None = None) -> MarketWindowSchedule:
    if path.name.startswith(".env"):
        raise MarketWindowError("MARKET_WINDOW_ENV_FILE_FORBIDDEN")
    try:
        raw_schedule = _read_private_file(path)
        if expected_sha256 is not None and (
            not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or hashlib.sha256(raw_schedule).hexdigest() != expected_sha256
        ):
            raise MarketWindowError("MARKET_WINDOW_SCHEDULE_DIGEST_MISMATCH")
        payload = json.loads(
            raw_schedule.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError):
        raise MarketWindowError("MARKET_WINDOW_CONFIG_INVALID") from None
    if type(payload) is not dict or set(payload) != _SCHEDULE_KEYS:
        raise MarketWindowError("MARKET_WINDOW_CONFIG_INVALID")
    runtime_value = payload["runtime_config"]
    if type(runtime_value) is not str or not runtime_value:
        raise MarketWindowError("MARKET_WINDOW_CONFIG_INVALID")
    runtime_config = Path(runtime_value)
    if not runtime_config.is_absolute() or runtime_config.name.startswith(".env"):
        raise MarketWindowError("MARKET_WINDOW_RUNTIME_CONFIG_INVALID")
    if (
        type(payload["cadence_seconds"]) is not int
        or payload["cadence_seconds"] != _CADENCE_SECONDS
    ):
        raise MarketWindowError("MARKET_WINDOW_CADENCE_INVALID")
    window_start = _parse_et_timestamp(payload["window_start"])
    hard_cutoff = _parse_et_timestamp(payload["hard_cutoff"])
    duration = hard_cutoff - window_start
    if (
        window_start.date() != hard_cutoff.date()
        or duration <= timedelta(0)
        or duration > timedelta(hours=4)
        or duration.total_seconds() % _CADENCE_SECONDS
    ):
        raise MarketWindowError("MARKET_WINDOW_RANGE_INVALID")
    return MarketWindowSchedule(
        runtime_config=runtime_config,
        window_start=window_start,
        hard_cutoff=hard_cutoff,
        cadence=timedelta(seconds=_CADENCE_SECONDS),
    )


def _parse_et_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise MarketWindowError("MARKET_WINDOW_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MarketWindowError("MARKET_WINDOW_TIME_INVALID") from None
    if parsed.tzinfo is None or parsed.microsecond or parsed.second or parsed.isoformat() != value:
        raise MarketWindowError("MARKET_WINDOW_TIME_INVALID")
    naive = parsed.replace(tzinfo=None)
    valid: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=_ET, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(_ET)
        if round_trip.replace(tzinfo=None) == naive:
            valid.append(candidate)
    offsets = {candidate.utcoffset() for candidate in valid}
    if len(offsets) != 1 or parsed.utcoffset() not in offsets:
        raise MarketWindowError("MARKET_WINDOW_TIME_NOT_ET")
    return parsed.astimezone(_ET)


def _runtime_command(schedule: MarketWindowSchedule) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "ops.launch.submission_runtime",
        "--config",
        str(schedule.runtime_config),
        "--serve",
        "--autonomous",
    )


def _tick_environment(config: SubmissionRuntimeConfig) -> dict[str, str]:
    return {
        "ALPHADECAY_SCHEDULER_URL": _TICK_URL,
        "ALPHADECAY_SCHEDULER_TOKEN": config.environment["SCHEDULER_TOKEN"],
    }


def run_window(
    schedule: MarketWindowSchedule,
    runtime_config: SubmissionRuntimeConfig,
    dependencies: RuntimeDependencies,
) -> None:
    now = _trusted_now(dependencies.now)
    if now < schedule.window_start - _MAXIMUM_START_LEAD:
        raise MarketWindowError("MARKET_WINDOW_NOT_OPEN")
    if now >= schedule.hard_cutoff:
        raise MarketWindowError("MARKET_WINDOW_CLOSED")
    process: ProcessPort | None = None
    try:
        try:
            port_is_clear = dependencies.port_is_clear()
        except Exception:
            raise MarketWindowError("MARKET_WINDOW_PORT_CHECK_FAILED") from None
        if port_is_clear is not True:
            raise MarketWindowError("MARKET_WINDOW_PORT_OCCUPIED")
        process = dependencies.spawn(_runtime_command(schedule))
        dependencies.emit("RUNTIME_STARTED", now)
        _wait_until_ready(schedule, process, dependencies)
        dependencies.emit("RUNTIME_READY", _trusted_now(dependencies.now))
        next_tick = _next_tick(
            schedule.window_start,
            _trusted_now(dependencies.now),
            schedule.cadence,
        )
        while next_tick < schedule.hard_cutoff:
            _wait_until(next_tick, schedule.hard_cutoff, process, dependencies)
            now = _trusted_now(dependencies.now)
            if now >= schedule.hard_cutoff:
                break
            if process.poll() is not None:
                raise MarketWindowError("MARKET_WINDOW_CHILD_DIED")
            try:
                dependencies.tick_sender(_tick_environment(runtime_config))
            except Exception as error:
                print(f"tick failed: {type(error).__name__}: {error}"[:400], file=sys.stderr)
                raise MarketWindowError("MARKET_WINDOW_TICK_FAILED") from None
            dependencies.emit("TICK_ACCEPTED", now)
            next_tick += schedule.cadence
        _wait_until(schedule.hard_cutoff, schedule.hard_cutoff, process, dependencies)
        dependencies.emit("HARD_CUTOFF_REACHED", _trusted_now(dependencies.now))
    finally:
        if process is not None:
            _stop_process(process)
            dependencies.emit("RUNTIME_STOPPED", _trusted_now(dependencies.now))


def _wait_until_ready(
    schedule: MarketWindowSchedule,
    process: ProcessPort,
    dependencies: RuntimeDependencies,
) -> None:
    deadline = min(
        _trusted_now(dependencies.now) + timedelta(seconds=60),
        schedule.hard_cutoff,
    )
    while _trusted_now(dependencies.now) < deadline:
        if process.poll() is not None:
            raise MarketWindowError("MARKET_WINDOW_CHILD_DIED")
        try:
            if dependencies.readiness_probe():
                return
        except Exception:
            raise MarketWindowError("MARKET_WINDOW_READINESS_FAILED") from None
        dependencies.sleep(1.0)
    raise MarketWindowError("MARKET_WINDOW_READINESS_FAILED")


def _wait_until(
    target: datetime,
    hard_cutoff: datetime,
    process: ProcessPort,
    dependencies: RuntimeDependencies,
) -> None:
    while True:
        now = _trusted_now(dependencies.now)
        if now >= target or now >= hard_cutoff:
            return
        if process.poll() is not None:
            raise MarketWindowError("MARKET_WINDOW_CHILD_DIED")
        remaining = min(target, hard_cutoff) - now
        dependencies.sleep(min(1.0, remaining.total_seconds()))


def _next_tick(start: datetime, now: datetime, cadence: timedelta) -> datetime:
    if now <= start:
        return start
    elapsed = (now - start).total_seconds()
    intervals = int(elapsed // cadence.total_seconds())
    candidate = start + intervals * cadence
    return candidate if candidate >= now else candidate + cadence


def _trusted_now(provider: Callable[[], datetime]) -> datetime:
    value = provider()
    if value.tzinfo is None:
        raise MarketWindowError("MARKET_WINDOW_CLOCK_INVALID")
    return value.astimezone(_ET)


def _stop_process(process: ProcessPort) -> None:
    if process.poll() is not None:
        return
    _terminate_process_group(process)
    process.terminate()
    try:
        process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def _terminate_process_group(process: ProcessPort) -> None:
    """The runtime launcher forks the server, so stop the whole session, not only the parent."""
    pid = getattr(process, "pid", None)
    if type(pid) is not int or pid <= 0:
        return
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, signal.SIGTERM)


def _spawn(command: Sequence[str]) -> ProcessPort:
    if tuple(command)[:3] != (
        sys.executable,
        "-m",
        "ops.launch.submission_runtime",
    ):
        raise MarketWindowError("MARKET_WINDOW_COMMAND_INVALID")
    log_handle = _backend_log_handle()
    return subprocess.Popen(
        tuple(command),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT if log_handle is not subprocess.DEVNULL else subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        start_new_session=True,
    )


_BACKEND_LOG_PATH: Path | None = None


def _backend_log_handle() -> object:
    """Keep the runtime's own output next to the schedule so failures stay diagnosable."""
    if _BACKEND_LOG_PATH is None:
        return subprocess.DEVNULL
    try:
        return open(_BACKEND_LOG_PATH, "ab", buffering=0)
    except OSError:
        return subprocess.DEVNULL


def _readiness_probe() -> bool:
    try:
        response = httpx.get(
            _READY_URL,
            headers={"Accept": "application/json"},
            timeout=2.0,
            follow_redirects=False,
            trust_env=False,
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return (
        response.status_code == 200
        and type(payload) is dict
        and {"status", "build", "runtime_mode"} <= set(payload)
        and payload["status"] == "ok"
        and type(payload["build"]) is str
        and bool(payload["build"])
        and payload["runtime_mode"] == "CONNECTED"
    )


def _local_port_is_clear() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Match the server's own bind semantics so a previous window's TIME_WAIT sockets
        # do not report the port as occupied.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((_FIXED_HOST, _FIXED_PORT))
        except OSError:
            return False
    return True


def _emit(event: str, at: datetime) -> None:
    print(json.dumps({"at": at.isoformat(), "event": event}, sort_keys=True), flush=True)


def _dependencies() -> RuntimeDependencies:
    import time

    return RuntimeDependencies(
        now=lambda: datetime.now(tz=_ET),
        sleep=time.sleep,
        port_is_clear=_local_port_is_clear,
        spawn=_spawn,
        readiness_probe=_readiness_probe,
        tick_sender=send_local_tick,
        emit=_emit,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    dependencies: RuntimeDependencies | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    old_handlers: dict[signal.Signals, signal.Handlers] = {}

    def interrupted(_signum: int, _frame: FrameType | None) -> None:
        raise MarketWindowError("MARKET_WINDOW_INTERRUPTED")

    try:
        generation_values = (args.runtime_config, args.entry_output, args.lifecycle_output)
        if any(value is not None for value in generation_values):
            if (
                not all(value is not None for value in generation_values)
                or args.schedule is not None
                or args.schedule_sha256 is not None
                or args.execute
            ):
                raise MarketWindowError("MARKET_WINDOW_GENERATION_ARGUMENTS_INVALID")
            write_schedule_pair(
                runtime_config=args.runtime_config,
                entry_output=args.entry_output,
                lifecycle_output=args.lifecycle_output,
                signal_session=args.signal_session,
            )
            print(json.dumps({"event": "SCHEDULES_WRITTEN"}, sort_keys=True))
            return 0
        global _BACKEND_LOG_PATH
        if args.schedule is not None:
            _BACKEND_LOG_PATH = Path(args.schedule).resolve().with_name("backend.log")
        if args.schedule is None:
            raise MarketWindowError("MARKET_WINDOW_SCHEDULE_REQUIRED")
        schedule = load_schedule(
            args.schedule,
            expected_sha256=args.schedule_sha256,
        )
        runtime_config = load_runtime_config(schedule.runtime_config, autonomous=True)
        if not args.execute:
            _emit("PREVIEW_ONLY", schedule.window_start)
            return 0
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, interrupted)
        run_window(schedule, runtime_config, dependencies or _dependencies())
        return 0
    except Exception as error:
        if isinstance(error, SystemExit):
            raise
        code = error.code if isinstance(error, MarketWindowError) else "MARKET_WINDOW_FAILED"
        parser.error(code)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def _read_private_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise MarketWindowError("MARKET_WINDOW_PRIVATE_FILE_INVALID")
        result = bytearray()
        while True:
            chunk = os.read(descriptor, min(4096, _FILE_LIMIT + 1 - len(result)))
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            if len(result) > _FILE_LIMIT:
                raise MarketWindowError("MARKET_WINDOW_PRIVATE_FILE_INVALID")
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
