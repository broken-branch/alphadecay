#!/usr/bin/env python3

"""Generate the lock-bound dependency inventory used by release verification."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from ops.release.license_provenance import (
    TARGET_ENVIRONMENT,
    _load_node_lock,
    _load_python_lock,
    _python_closure,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "compliance/locked-dependencies.json"
LICENSE_EVIDENCE_STATUS = (
    "No SPDX conclusion is asserted here. The release verifier must derive each "
    "conclusion from the exact locked archive and retained license material."
)


def build_inventory(root: Path = ROOT) -> dict[str, object]:
    uv_data = (root / "uv.lock").read_bytes()
    node_data = (root / "package-lock.json").read_bytes()
    python, application, python_dev = _load_python_lock(uv_data)
    python_runtime = _python_closure(
        python,
        python[application].dependencies,
        "Python runtime",
    )
    python_build = _python_closure(python, python_dev, "Python build-only") - python_runtime
    mcp_runtime = _python_closure(
        python,
        tuple(
            dependency
            for dependency in python[application].dependencies
            if dependency.name == "alpaca-mcp-server"
        ),
        "MCP runtime",
    )
    node, frontend_runtime, node_build = _load_node_lock(node_data)

    roles: dict[str, set[str]] = defaultdict(set)
    for role, coordinates in (
        ("python-runtime", python_runtime),
        ("mcp-runtime", mcp_runtime),
        ("frontend-runtime", frontend_runtime),
        ("build-only", python_build | node_build),
    ):
        for coordinate in coordinates:
            roles[coordinate].add(role)

    packages: list[dict[str, object]] = []
    for package in python.values():
        if package.coordinate in roles:
            packages.append(
                {
                    "coordinate": package.coordinate,
                    "roles": sorted(roles[package.coordinate]),
                    "artifact_candidates": [
                        {"locator": locator, "integrity": integrity}
                        for locator, integrity in sorted(package.artifacts)
                    ],
                }
            )
    for package in node.values():
        if package.identity in roles:
            packages.append(
                {
                    "coordinate": package.coordinate,
                    "lock_path": package.lock_path,
                    "roles": sorted(roles[package.identity]),
                    "artifact_candidates": [
                        {
                            "locator": package.artifact[0],
                            "integrity": package.artifact[1],
                        }
                    ],
                }
            )

    return {
        "schema_version": 1,
        "status": "LOCK_GRAPH_ONLY",
        "target_environment": TARGET_ENVIRONMENT,
        "lockfiles": {
            "package-lock.json": hashlib.sha256(node_data).hexdigest(),
            "uv.lock": hashlib.sha256(uv_data).hexdigest(),
        },
        "inventories": {
            role: sorted(identity for identity, assigned in roles.items() if role in assigned)
            for role in ("python-runtime", "mcp-runtime", "frontend-runtime", "build-only")
        },
        "packages": sorted(
            packages,
            key=lambda item: (str(item["coordinate"]), str(item.get("lock_path", ""))),
        ),
        "license_evidence_status": LICENSE_EVIDENCE_STATUS,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.write_text(json.dumps(build_inventory(), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
