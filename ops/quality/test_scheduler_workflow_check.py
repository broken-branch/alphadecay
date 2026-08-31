from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ops.quality.scheduler_workflow_check import (
    SchedulerWorkflowError,
    check_scheduler_workflow,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "scheduler.yml"
PUBLIC_CI = ROOT / ".github" / "workflows" / "public-ci.yml"
PRIVATE_CI = ROOT / ".github" / "workflows" / "ci.yml"


def _document() -> dict[str, Any]:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "scheduler.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_repository_scheduler_workflow_is_exactly_default_off() -> None:
    check_scheduler_workflow(WORKFLOW)


def test_public_ci_runs_scheduler_workflow_check() -> None:
    assert "python ops/quality/scheduler_workflow_check.py" in PUBLIC_CI.read_text(encoding="utf-8")


@pytest.mark.skipif(not PRIVATE_CI.exists(), reason="private CI is not part of the public export")
def test_private_ci_runs_scheduler_workflow_check() -> None:
    assert "python ops/quality/scheduler_workflow_check.py" in PRIVATE_CI.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("path", "unsafe_value"),
    [
        (("on", "schedule", 0, "cron"), "*/5 * * * *"),
        (("jobs", "tick", "if"), "${{ always() }}"),
        (("permissions", "contents"), "write"),
        (("concurrency", "cancel-in-progress"), "true"),
        (("jobs", "tick", "timeout-minutes"), "30"),
        (("jobs", "tick", "steps", 0, "uses"), "actions/checkout@main"),
        (("jobs", "tick", "steps", 0, "with", "persist-credentials"), "true"),
        (("jobs", "tick", "steps", 1, "run"), "curl https://example.invalid"),
        (
            ("jobs", "tick", "steps", 1, "env", "ALPHADECAY_SCHEDULER_TOKEN"),
            "${{ secrets.GITHUB_TOKEN }}",
        ),
        (("on", "workflow_dispatch", "inputs", "approved_rehearsal", "default"), "true"),
        (("on", "workflow_dispatch", "inputs", "approved_rehearsal", "type"), "string"),
    ],
)
def test_checker_rejects_unsafe_scheduler_drift(
    tmp_path: Path, path: tuple[str | int, ...], unsafe_value: str
) -> None:
    document = _document()
    target: Any = document
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = unsafe_value

    with pytest.raises(SchedulerWorkflowError, match="SCHEDULER_WORKFLOW_UNSAFE_DRIFT"):
        check_scheduler_workflow(_write(tmp_path, document))


def test_checker_rejects_duplicate_workflow_keys(tmp_path: Path) -> None:
    duplicate = WORKFLOW.read_text(encoding="utf-8") + "\npermissions: {}\n"
    path = tmp_path / "scheduler.yml"
    path.write_text(duplicate, encoding="utf-8")

    with pytest.raises(SchedulerWorkflowError, match="SCHEDULER_WORKFLOW_DUPLICATE_KEY"):
        check_scheduler_workflow(path)


def test_checker_rejects_duplicate_nested_keys(tmp_path: Path) -> None:
    duplicate = WORKFLOW.read_text(encoding="utf-8").replace(
        "    timeout-minutes: 3\n",
        "    timeout-minutes: 3\n    timeout-minutes: 2\n",
    )
    path = tmp_path / "scheduler.yml"
    path.write_text(duplicate, encoding="utf-8")

    with pytest.raises(SchedulerWorkflowError, match="SCHEDULER_WORKFLOW_DUPLICATE_KEY"):
        check_scheduler_workflow(path)


def test_checker_rejects_job_wide_secret_exposure(tmp_path: Path) -> None:
    document = _document()
    document["jobs"]["tick"]["env"] = document["jobs"]["tick"]["steps"][1].pop("env")

    with pytest.raises(SchedulerWorkflowError, match="SCHEDULER_WORKFLOW_UNSAFE_DRIFT"):
        check_scheduler_workflow(_write(tmp_path, document))
