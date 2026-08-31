from __future__ import annotations

import json
from pathlib import Path

from ops.release.generate_locked_dependency_inventory import build_inventory

ROOT = Path(__file__).resolve().parents[2]


def test_locked_dependency_inventory_matches_resolved_graph() -> None:
    inventory = json.loads((ROOT / "compliance/locked-dependencies.json").read_text())
    assert inventory == build_inventory()
