#!/usr/bin/env python3

"""Generate third-party notices from the installed, lock-matched runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.utils import canonicalize_name

from ops.release.generate_locked_dependency_inventory import build_inventory
from ops.release.license_provenance import (
    APPROVED_LICENSES,
    ComplianceError,
    _python_license,
    _supplemental_licenses,
)

ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = "compliance/locked-dependencies.json"
NOTICE_PATH = "THIRD_PARTY_NOTICES.md"
RUNTIME_ROLES = frozenset({"python-runtime", "mcp-runtime", "frontend-runtime"})
MAX_NOTICE_BODY_BYTES = 512 * 1024
MATERIAL_PREFIXES = ("license", "copying", "copyright", "notice")


@dataclass(frozen=True)
class NoticeBody:
    digest: str
    origin: str
    text: str


@dataclass(frozen=True)
class NoticePackage:
    identity: str
    license_expression: str
    roles: tuple[str, ...]
    bodies: tuple[NoticeBody, ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ComplianceError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ComplianceError(f"{label} must be an object")
    return value


def _material_filename(name: str) -> bool:
    lowered = name.lower()
    return any(
        lowered == prefix
        or lowered.startswith(prefix + ".")
        or lowered.startswith(prefix + "-")
        or lowered.startswith(prefix + "_")
        for prefix in MATERIAL_PREFIXES
    )


def _bounded_body(path: Path, label: str) -> tuple[bytes, str]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ComplianceError(f"{label} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ComplianceError(f"{label} is not a regular file")
    if metadata.st_size > MAX_NOTICE_BODY_BYTES:
        raise ComplianceError(f"{label} exceeds the notice body size limit")
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ComplianceError(f"{label} is not readable UTF-8") from error
    if not text.strip():
        raise ComplianceError(f"{label} is empty")
    return data, text


def _body(path: Path, origin: str) -> NoticeBody:
    data, text = _bounded_body(path, origin)
    return NoticeBody(_sha256(data), origin, text)


def _inventory(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    raw = _load_json(root / INVENTORY_PATH, "locked dependency inventory")
    if raw != build_inventory(root):
        raise ComplianceError("locked dependency inventory differs from the lockfiles")
    inventories = raw["inventories"]
    roles: dict[str, set[str]] = {}
    for role, identities in inventories.items():
        for identity in identities:
            roles.setdefault(identity, set()).add(role)
    by_identity: dict[str, dict[str, Any]] = {}
    for package in raw["packages"]:
        coordinate = package["coordinate"]
        lock_path = package.get("lock_path")
        identity = f"{coordinate}#{lock_path}" if coordinate.startswith("node:") else coordinate
        by_identity[identity] = package
    return by_identity, roles


def _python_packages(
    root: Path,
    identities: set[str],
    roles: dict[str, set[str]],
    distributions: list[importlib.metadata.Distribution] | None,
) -> list[NoticePackage]:
    available = list(importlib.metadata.distributions()) if distributions is None else distributions
    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in available
        if distribution.metadata.get("Name")
    }
    supplemental = _supplemental_licenses(root)
    result: list[NoticePackage] = []
    for identity in sorted(identities):
        coordinate = identity.removeprefix("python:")
        name, expected_version = coordinate.rsplit("==", 1)
        distribution = installed.get(canonicalize_name(name))
        if distribution is None or distribution.version != expected_version:
            raise ComplianceError(f"installed distribution differs from lock: {identity}")
        expression = _python_license(distribution.metadata, identity)
        if expression not in APPROVED_LICENSES:
            raise ComplianceError(f"{identity} has an incompatible license: {expression}")
        bodies: list[NoticeBody] = []
        for member in sorted(distribution.files or (), key=str):
            relative = PurePosixPath(str(member))
            if not _material_filename(relative.name):
                continue
            path = Path(distribution.locate_file(member))
            bodies.append(_body(path, f"installed:{identity}:{relative.as_posix()}"))
        if not bodies:
            supplement = supplemental.get(identity)
            if supplement is None or supplement["license_expression"] != expression:
                raise ComplianceError(f"{identity} has no installed or retained notice material")
            for source in supplement["sources"]:
                bodies.append(_body(root / source["path"], source["url"]))
        result.append(
            NoticePackage(identity, expression, tuple(sorted(roles[identity])), tuple(bodies))
        )
    return result


def _node_packages(
    root: Path,
    identities: set[str],
    packages: dict[str, dict[str, Any]],
    roles: dict[str, set[str]],
) -> list[NoticePackage]:
    result: list[NoticePackage] = []
    for identity in sorted(identities):
        package = packages[identity]
        coordinate = str(package["coordinate"])
        name, expected_version = coordinate.removeprefix("node:").rsplit("@", 1)
        lock_path = package.get("lock_path")
        if not isinstance(lock_path, str):
            raise ComplianceError(f"{identity} has no lock path")
        parsed_lock_path = PurePosixPath(lock_path)
        if (
            parsed_lock_path.is_absolute()
            or ".." in parsed_lock_path.parts
            or parsed_lock_path.as_posix() != lock_path
        ):
            raise ComplianceError(f"{identity} has an unsafe lock path")
        directory = root / lock_path
        metadata = _load_json(directory / "package.json", f"{identity} package metadata")
        if metadata.get("name") != name or metadata.get("version") != expected_version:
            raise ComplianceError(f"installed package differs from lock: {identity}")
        expression = metadata.get("license")
        if not isinstance(expression, str) or expression not in APPROVED_LICENSES:
            raise ComplianceError(f"{identity} has an incompatible license: {expression!r}")
        bodies = tuple(
            _body(path, f"installed:{identity}:{path.name}")
            for path in sorted(directory.iterdir(), key=lambda item: item.name)
            if _material_filename(path.name)
        )
        if not bodies:
            raise ComplianceError(f"{identity} has no installed notice material")
        result.append(NoticePackage(identity, expression, tuple(sorted(roles[identity])), bodies))
    return result


def _fence(text: str) -> str:
    length = 4
    while "`" * length in text:
        length += 1
    marker = "`" * length
    return f"{marker}text\n{text.rstrip()}\n{marker}"


def render_notice(packages: list[NoticePackage]) -> str:
    bodies: dict[str, NoticeBody] = {}
    references: dict[str, list[str]] = {}
    rows: list[str] = []
    for package in packages:
        labels: list[str] = []
        for body in package.bodies:
            existing = bodies.get(body.digest)
            if existing is not None and existing.text != body.text:
                raise ComplianceError("notice body hash collision")
            bodies.setdefault(body.digest, body)
            references.setdefault(body.digest, []).append(body.origin)
            labels.append(f"N-{body.digest[:12]}")
        identity = package.identity.replace("|", "\\|")
        links = ", ".join(f"[{label}](#{label.lower()})" for label in sorted(set(labels)))
        rows.append(
            f"| `{identity}` | {', '.join(package.roles)} | "
            f"`{package.license_expression}` | {links} |"
        )
    lines = [
        "# Third-party notices",
        "",
        (
            "alphadecay redistributes the Python runtime packages and bundled browser packages "
            "listed below. License conclusions come from the metadata installed by the exact "
            "lockfiles; the corresponding license and notice text is retained in this file."
        ),
        "",
        (
            "Build and test dependencies are locked for reproducibility but are not "
            "redistributed with the application. The pinned Python/Debian base image retains "
            "its operating-system notices under `/usr/share/doc` in the production image."
        ),
        "",
        "| Package | Runtime role | SPDX expression | Notice text |",
        "|---|---|---|---|",
        *rows,
        "",
        "# Retained notice text",
        "",
    ]
    for digest, body in sorted(bodies.items()):
        label = f"N-{digest[:12]}"
        lines.extend(
            [
                f"## {label}",
                "",
                f"SHA-256: `{digest}`",
                "",
                "Sources:",
                "",
                *(f"- `{origin}`" for origin in sorted(set(references[digest]))),
                "",
                _fence(body.text),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def build_notice(
    root: Path = ROOT,
    distributions: list[importlib.metadata.Distribution] | None = None,
) -> str:
    packages, roles = _inventory(root)
    runtime = {identity for identity, assigned in roles.items() if assigned & RUNTIME_ROLES}
    python = {identity for identity in runtime if identity.startswith("python:")}
    node = {identity for identity in runtime if identity.startswith("node:")}
    if python | node != runtime:
        raise ComplianceError("runtime inventory contains an unsupported package ecosystem")
    notices = _python_packages(root, python, roles, distributions)
    notices.extend(_node_packages(root, node, packages, roles))
    return render_notice(sorted(notices, key=lambda item: item.identity))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = args.output or args.root / NOTICE_PATH
    try:
        expected = build_notice(args.root)
        if args.check:
            if output.read_text(encoding="utf-8") != expected:
                raise ComplianceError(f"{output} is stale")
        else:
            output.write_text(expected, encoding="utf-8")
    except (ComplianceError, OSError, UnicodeDecodeError) as error:
        print(f"FAIL  {error}")
        return 1
    action = "verified" if args.check else "generated"
    print(f"PASS  {action} {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
