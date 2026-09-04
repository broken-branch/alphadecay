from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_WORKFLOW = _ROOT / ".github" / "workflows" / "scheduler.yml"
_CHECKOUT_ACTION = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
_JOB_GATE = (
    "${{ (github.event_name == 'schedule' && "
    "vars.ALPHADECAY_SCHEDULER_ENABLED == 'true') || "
    "(github.event_name == 'workflow_dispatch' && "
    "inputs.approved_rehearsal == true) }}"
)


class SchedulerWorkflowError(RuntimeError):
    pass


class _UniqueBaseLoader(yaml.BaseLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueBaseLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise SchedulerWorkflowError("SCHEDULER_WORKFLOW_NON_STRING_KEY")
        if key in result:
            raise SchedulerWorkflowError("SCHEDULER_WORKFLOW_DUPLICATE_KEY")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueBaseLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _expected_workflow() -> dict[str, Any]:
    return {
        "name": "Scheduler wake",
        "on": {
            "schedule": [{"cron": "*/5 * * * *"}],
            "workflow_dispatch": {
                "inputs": {
                    "approved_rehearsal": {
                        "description": "Run one approved paper scheduler rehearsal",
                        "required": "true",
                        "default": "false",
                        "type": "boolean",
                    }
                }
            },
        },
        "permissions": {"contents": "read"},
        "concurrency": {
            "group": "alphadecay-scheduler",
            "cancel-in-progress": "false",
        },
        "jobs": {
            "tick": {
                "if": _JOB_GATE,
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": "3",
                "steps": [
                    {
                        "name": "Check out source",
                        "uses": _CHECKOUT_ACTION,
                        "with": {"persist-credentials": "false"},
                    },
                    {
                        "name": "Wake one selector-free agent tick",
                        "run": "python3 ops/deploy/scheduler_tick.py",
                        "env": {
                            "ALPHADECAY_SCHEDULER_URL": ("${{ secrets.ALPHADECAY_SCHEDULER_URL }}"),
                            "ALPHADECAY_SCHEDULER_TOKEN": (
                                "${{ secrets.ALPHADECAY_SCHEDULER_TOKEN }}"
                            ),
                        },
                    },
                ],
            }
        },
    }


def check_scheduler_workflow(path: Path) -> None:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueBaseLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise SchedulerWorkflowError("SCHEDULER_WORKFLOW_UNREADABLE") from error
    if not isinstance(document, Mapping) or document != _expected_workflow():
        raise SchedulerWorkflowError("SCHEDULER_WORKFLOW_UNSAFE_DRIFT")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow", nargs="?", type=Path, default=_DEFAULT_WORKFLOW)
    arguments = parser.parse_args()
    try:
        check_scheduler_workflow(arguments.workflow)
    except SchedulerWorkflowError as error:
        print(str(error))
        return 1
    print("scheduler workflow: safe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
