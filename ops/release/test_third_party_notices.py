from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path

import pytest

from ops.release.generate_third_party_notices import build_notice
from ops.release.license_provenance import ComplianceError


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture(tmp_path: Path, *, python_license: bool = True) -> importlib.metadata.Distribution:
    uv_lock = b"fixture uv lock\n"
    node_lock = b"fixture node lock\n"
    (tmp_path / "uv.lock").write_bytes(uv_lock)
    (tmp_path / "package-lock.json").write_bytes(node_lock)
    python_id = "python:demo==1.0.0"
    node_id = "node:browser-demo@2.0.0#node_modules/browser-demo"
    inventory = {
        "schema_version": 1,
        "status": "LOCK_GRAPH_ONLY",
        "target_environment": {},
        "lockfiles": {
            "uv.lock": hashlib.sha256(uv_lock).hexdigest(),
            "package-lock.json": hashlib.sha256(node_lock).hexdigest(),
        },
        "inventories": {
            "build-only": [],
            "frontend-runtime": [node_id],
            "mcp-runtime": [python_id],
            "python-runtime": [python_id],
        },
        "packages": [
            {
                "coordinate": "node:browser-demo@2.0.0",
                "lock_path": "node_modules/browser-demo",
                "roles": ["frontend-runtime"],
                "artifact_candidates": [],
            },
            {
                "coordinate": python_id,
                "roles": ["mcp-runtime", "python-runtime"],
                "artifact_candidates": [],
            },
        ],
        "license_evidence_status": "fixture",
    }
    _write(
        tmp_path / "compliance/locked-dependencies.json",
        json.dumps(inventory, sort_keys=True),
    )
    _write(
        tmp_path / "third_party/notices/supplemental-licenses.json",
        json.dumps({"schema_version": 1, "packages": []}),
    )
    node = tmp_path / "node_modules/browser-demo"
    _write(
        node / "package.json",
        json.dumps({"name": "browser-demo", "version": "2.0.0", "license": "MIT"}),
    )
    _write(node / "LICENSE", "Shared fixture license.\n")
    dist_info = tmp_path / "site/demo-1.0.0.dist-info"
    _write(
        dist_info / "METADATA",
        "Metadata-Version: 2.4\nName: demo\nVersion: 1.0.0\nLicense-Expression: MIT\n",
    )
    records = ["demo-1.0.0.dist-info/METADATA,,"]
    if python_license:
        _write(dist_info / "licenses/LICENSE", "Shared fixture license.\n")
        records.append("demo-1.0.0.dist-info/licenses/LICENSE,,")
    records.append("demo-1.0.0.dist-info/RECORD,,")
    _write(dist_info / "RECORD", "\n".join(records) + "\n")
    return importlib.metadata.PathDistribution(dist_info)


def _accept_fixture_inventory(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    inventory = json.loads(
        (root / "compliance/locked-dependencies.json").read_text(encoding="utf-8")
    )
    monkeypatch.setattr(
        "ops.release.generate_third_party_notices.build_inventory",
        lambda _root: inventory,
    )


def test_notice_is_lock_bound_and_deduplicates_identical_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _fixture(tmp_path)
    _accept_fixture_inventory(monkeypatch, tmp_path)

    notice = build_notice(tmp_path, [distribution])

    assert "`python:demo==1.0.0`" in notice
    assert "`node:browser-demo@2.0.0#node_modules/browser-demo`" in notice
    assert "mcp-runtime, python-runtime" in notice
    assert notice.count("Shared fixture license.") == 1


def test_notice_rejects_stale_lock_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _fixture(tmp_path)
    expected = json.loads(
        (tmp_path / "compliance/locked-dependencies.json").read_text(encoding="utf-8")
    )
    expected["lockfiles"]["uv.lock"] = "changed"
    monkeypatch.setattr(
        "ops.release.generate_third_party_notices.build_inventory",
        lambda _root: expected,
    )

    with pytest.raises(ComplianceError, match="differs from the lockfiles"):
        build_notice(tmp_path, [distribution])


def test_notice_rejects_runtime_without_retained_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _fixture(tmp_path, python_license=False)
    _accept_fixture_inventory(monkeypatch, tmp_path)

    with pytest.raises(ComplianceError, match="no installed or retained notice material"):
        build_notice(tmp_path, [distribution])
