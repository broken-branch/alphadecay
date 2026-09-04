#!/usr/bin/env python3

"""Verify archive- and image-bound license provenance for a public release."""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import gzip
import hashlib
import hmac
import importlib.metadata
import io
import json
import re
import stat
import struct
import sys
import tarfile
import tomllib
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from email.header import Header
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

import packaging
from packaging.markers import InvalidMarker, Marker
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

APPROVED_LICENSES = frozenset(
    {
        "Apache-2.0",
        "Apache-2.0 OR BSD-2-Clause",
        "Apache-2.0 OR BSD-3-Clause",
        "Apache-2.0 OR MIT",
        "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "BlueOak-1.0.0",
        "CC0-1.0",
        "ISC",
        "MIT",
        "MIT AND PSF-2.0",
        "MIT OR Apache-2.0",
        "MIT-0",
        "MPL-2.0",
        "PSF-2.0",
        "Unlicense",
        "0BSD",
        "Zlib",
    }
)
REGISTRY_PATH = "compliance/dependency-evidence.json"
BUNDLE_PATH = "compliance/frontend-bundle.json"
IMAGE_PATH = "compliance/final-image-spdx.json"
PROVENANCE_PATH = "compliance/release-provenance.json"
NOTICE_PATH = "THIRD_PARTY_NOTICES.md"
SUPPLEMENTAL_LICENSE_PATH = "third_party/notices/supplemental-licenses.json"
GENERATED_PATHS = frozenset({PROVENANCE_PATH, NOTICE_PATH})
ASSET_PREFIXES = ("public/", "fixtures/replay/")
ASSET_SUFFIXES = frozenset(
    {".avif", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp", ".woff", ".woff2"}
)
FONT_SUFFIXES = frozenset({".woff", ".woff2"})
OFL_LICENSE = "OFL-1.1"
OFL_PROVENANCE_SUFFIX = ".font-provenance.json"
PACKAGING_VERSION = "26.3"
MATERIAL_NAMES = ("license", "copying", "copyright", "notice")
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 64 * 1024 * 1024
MAX_COMPRESSED_BYTES = 256 * 1024 * 1024
MAX_OCI_LAYERS = 128
MAX_OCI_COMPRESSED_BYTES = 512 * 1024 * 1024
MAX_OCI_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_OCI_MEMBERS = 200_000
TARGET_ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.13",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.12.13",
    "python_version": "3.12",
    "sys_platform": "linux",
}


class ComplianceError(ValueError):
    """The staged release lacks exact, consistent compliance evidence."""


@dataclass(frozen=True)
class Dependency:
    name: str
    extras: tuple[str, ...] = ()


@dataclass(frozen=True)
class PythonPackage:
    coordinate: str
    dependencies: tuple[Dependency, ...]
    optional_dependencies: dict[str, tuple[Dependency, ...]]
    artifacts: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class NodePackage:
    coordinate: str
    lock_path: str
    dependencies: tuple[str, ...]
    optional_dependencies: tuple[str, ...]
    peer_dependencies: tuple[str, ...]
    artifact: tuple[str, str]

    @property
    def identity(self) -> str:
        return f"{self.coordinate}#{self.lock_path}"


@dataclass(frozen=True)
class Result:
    python_runtime: int
    mcp_runtime: int
    frontend_runtime: int
    build_only: int
    assets: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _packaging_authority() -> dict[str, str]:
    try:
        distribution = importlib.metadata.distribution("packaging")
        version = distribution.version
        files = tuple(distribution.files or ())
        module_member = next(path for path in files if str(path) == "packaging/__init__.py")
        metadata_member = next(path for path in files if str(path).endswith(".dist-info/METADATA"))
        module_path = Path(distribution.locate_file(module_member)).resolve(strict=True)
        metadata_path = Path(distribution.locate_file(metadata_member)).resolve(strict=True)
        import_path = Path(packaging.__file__ or "").resolve(strict=True)
    except (ImportError, OSError, StopIteration, importlib.metadata.PackageNotFoundError) as error:
        raise ComplianceError("active packaging distribution is unavailable") from error
    if canonicalize_name(distribution.metadata.get("Name", "")) != "packaging":
        raise ComplianceError("active packaging distribution identity is unexpected")
    if version != PACKAGING_VERSION:
        raise ComplianceError("active packaging distribution version is unexpected")
    environment_root = Path(sys.prefix).resolve(strict=True)
    if not module_path.is_relative_to(environment_root) or not metadata_path.is_relative_to(
        environment_root
    ):
        raise ComplianceError("active packaging distribution path is unexpected")
    if import_path != module_path:
        raise ComplianceError("active packaging import origin differs from its distribution")
    return {
        "coordinate": f"python:packaging=={version}",
        "distribution_path": metadata_path.relative_to(environment_root).as_posix(),
        "distribution_sha256": _sha256(metadata_path.read_bytes()),
        "import_origin": import_path.relative_to(environment_root).as_posix(),
        "import_sha256": _sha256(import_path.read_bytes()),
    }


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ComplianceError(f"{label} must be a nonempty string")
    return value


def _relative(value: object, label: str) -> str:
    text = _string(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != text:
        raise ComplianceError(f"{label} is not a safe relative path: {text!r}")
    return text


def _regular_file(root: Path, relative: str, label: str, *, control: bool = False) -> Path:
    relative = _relative(relative, label)
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise ComplianceError(f"{label} is missing: {relative}") from error
        if stat.S_ISLNK(mode):
            error_code = "SYMLINKED_CONTROL" if control else "symlinked path"
            raise ComplianceError(f"{error_code}: {relative}")
    if not stat.S_ISREG(current.lstat().st_mode):
        raise ComplianceError(f"{label} is not a regular file: {relative}")
    return current


def _bounded_bytes(path: Path, label: str, limit: int = MAX_COMPRESSED_BYTES) -> bytes:
    size = path.stat().st_size
    if size > limit:
        raise ComplianceError(f"{label} exceeds the bounded input size")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ComplianceError(f"{label} exceeds the bounded input size")
    return data


def _read_control(root: Path, relative: str, label: str) -> bytes:
    return _bounded_bytes(_regular_file(root, relative, label, control=True), label)


def _json_bytes(data: bytes, label: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ComplianceError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    except ComplianceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ComplianceError(f"cannot parse {label}: {error}") from error
    if not isinstance(value, dict):
        raise ComplianceError(f"{label} must be a JSON object")
    return value


def _read_control_json(root: Path, relative: str, label: str) -> dict[str, Any]:
    return _json_bytes(_read_control(root, relative, label), label)


def _checked_root(root: Path, label: str) -> Path:
    try:
        mode = root.lstat().st_mode
    except OSError as error:
        raise ComplianceError(f"{label} is missing") from error
    if stat.S_ISLNK(mode):
        raise ComplianceError(f"SYMLINKED_CONTROL: {label}")
    if not stat.S_ISDIR(mode):
        raise ComplianceError(f"{label} is not a directory")
    return root.resolve(strict=True)


def _walk_files(root: Path, relative: str, *, symlink_label: str) -> dict[str, bytes]:
    base = root / relative
    try:
        base_mode = base.lstat().st_mode
    except OSError as error:
        raise ComplianceError(f"missing directory: {relative}") from error
    if stat.S_ISLNK(base_mode):
        raise ComplianceError(f"{symlink_label}: {relative}")
    if not stat.S_ISDIR(base_mode):
        raise ComplianceError(f"not a directory: {relative}")
    result: dict[str, bytes] = {}
    pending = [base]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            mode = path.lstat().st_mode
            release_path = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                raise ComplianceError(f"{symlink_label}: {release_path}")
            if stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISREG(mode):
                result[release_path] = path.read_bytes()
            else:
                raise ComplianceError(f"unsupported filesystem entry: {release_path}")
    return result


def _dependencies(value: object, label: str) -> tuple[Dependency, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ComplianceError(f"{label} must be a list")
    result: list[Dependency] = []
    for item in value:
        if not isinstance(item, dict):
            raise ComplianceError(f"{label} contains a non-object dependency")
        marker = item.get("marker")
        if marker is not None:
            try:
                if not Marker(_string(marker, f"{label} marker")).evaluate(TARGET_ENVIRONMENT):
                    continue
            except InvalidMarker as error:
                raise ComplianceError(f"{label} contains an invalid marker") from error
        extras = item.get("extra", item.get("extras", []))
        if not isinstance(extras, list) or any(not isinstance(extra, str) for extra in extras):
            raise ComplianceError(f"{label} extras must be a string list")
        result.append(Dependency(_string(item.get("name"), f"{label} name"), tuple(extras)))
    return tuple(result)


def _target_wheel(locator: str) -> bool:
    filename = locator.rsplit("/", 1)[-1].lower()
    if not filename.endswith(".whl"):
        return False
    try:
        _, _, _, wheel_tags = parse_wheel_filename(filename)
    except ValueError as error:
        raise ComplianceError(f"invalid wheel filename: {filename}") from error
    platforms = ["linux_x86_64", "manylinux2014_x86_64", "manylinux2010_x86_64"]
    platforms.extend(f"manylinux_2_{minor}_x86_64" for minor in range(5, 37))
    target_tags = set(cpython_tags((3, 12), platforms=platforms))
    target_tags.update(compatible_tags((3, 12), interpreter="cp312", platforms=platforms))
    return not wheel_tags.isdisjoint(target_tags)


def _python_artifacts(raw: dict[str, Any], label: str) -> frozenset[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for field in ("wheels", "sdist"):
        value = raw.get(field)
        if value is None:
            continue
        entries = value if isinstance(value, list) else [value]
        for item in entries:
            if not isinstance(item, dict):
                raise ComplianceError(f"{label} has invalid artifact data")
            locator = _string(item.get("url"), f"{label} URL")
            if field == "wheels" and not _target_wheel(locator):
                continue
            result.add(
                (
                    locator,
                    _string(item.get("hash"), f"{label} hash"),
                )
            )
    return frozenset(result)


def _load_python_lock(data: bytes) -> tuple[dict[str, PythonPackage], str, tuple[Dependency, ...]]:
    try:
        lock = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ComplianceError(f"cannot parse uv.lock: {error}") from error
    raw_packages = lock.get("package")
    if not isinstance(raw_packages, list):
        raise ComplianceError("uv.lock must contain a package list")
    packages: dict[str, PythonPackage] = {}
    root = ""
    dev: tuple[Dependency, ...] = ()
    for raw in raw_packages:
        if not isinstance(raw, dict):
            raise ComplianceError("uv.lock contains an invalid package")
        name = _string(raw.get("name"), "uv.lock package name")
        version = _string(raw.get("version"), f"uv.lock {name} version")
        optional_raw = raw.get("optional-dependencies", {})
        if not isinstance(optional_raw, dict):
            raise ComplianceError(f"uv.lock {name} optional dependencies must be an object")
        package = PythonPackage(
            f"python:{name}=={version}",
            _dependencies(raw.get("dependencies"), f"uv.lock {name} dependencies"),
            {
                key: _dependencies(value, f"uv.lock {name} extra {key}")
                for key, value in optional_raw.items()
            },
            _python_artifacts(raw, f"uv.lock {name}"),
        )
        if name in packages:
            raise ComplianceError(f"uv.lock dependency name {name!r} is ambiguous")
        packages[name] = package
        source = raw.get("source")
        if isinstance(source, dict) and source.get("editable") == ".":
            if root:
                raise ComplianceError("uv.lock contains multiple editable roots")
            root = name
            groups = raw.get("dev-dependencies", {})
            if not isinstance(groups, dict):
                raise ComplianceError("uv.lock root dev dependencies must be an object")
            dev = tuple(
                dep
                for value in groups.values()
                for dep in _dependencies(value, "uv.lock dev dependency")
            )
    if not root:
        raise ComplianceError("uv.lock has no editable root")
    return packages, root, dev


def _python_closure(
    packages: dict[str, PythonPackage], roots: tuple[Dependency, ...], label: str
) -> set[str]:
    result: set[str] = set()
    pending = list(roots)
    visited: set[Dependency] = set()
    while pending:
        dependency = pending.pop()
        if dependency in visited:
            continue
        visited.add(dependency)
        package = packages.get(dependency.name)
        if package is None:
            raise ComplianceError(f"{label} references absent package {dependency.name}")
        result.add(package.coordinate)
        pending.extend(package.dependencies)
        for extra in dependency.extras:
            if extra not in package.optional_dependencies:
                raise ComplianceError(f"{label} requests absent extra {extra}")
            pending.extend(package.optional_dependencies[extra])
    return result


def _node_target(raw: dict[str, Any], label: str) -> bool:
    def applies(value: object, target: str) -> bool:
        if value is None:
            return True
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ComplianceError(f"{label} target constraint must be a string list")
        if f"!{target}" in value:
            return False
        positive = [item for item in value if not item.startswith("!")]
        return not positive or target in positive

    return (
        applies(raw.get("os"), "linux")
        and applies(raw.get("cpu"), "x64")
        and applies(raw.get("libc"), "glibc")
    )


def _node_name(lock_path: str) -> str:
    marker = "node_modules/"
    if marker not in lock_path:
        raise ComplianceError(f"unsupported package-lock path: {lock_path}")
    return lock_path.rsplit(marker, 1)[1]


def _resolve_node(packages: dict[str, NodePackage], parent: str, name: str) -> str:
    base = parent
    while True:
        candidate = f"{base}/node_modules/{name}" if base else f"node_modules/{name}"
        if candidate in packages:
            return candidate
        marker = "/node_modules/"
        if marker not in base:
            break
        base = base.rsplit(marker, 1)[0]
    candidate = f"node_modules/{name}"
    if candidate in packages:
        return candidate
    raise ComplianceError(f"package-lock dependency {name!r} is unresolved from {parent!r}")


def _resolve_optional_node(packages: dict[str, NodePackage], parent: str, name: str) -> str | None:
    try:
        return _resolve_node(packages, parent, name)
    except ComplianceError:
        return None


def _node_closure(packages: dict[str, NodePackage], roots: dict[str, object]) -> set[str]:
    pending = [_resolve_node(packages, "", name) for name in roots]
    visited: set[str] = set()
    result: set[str] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        package = packages[path]
        result.add(package.identity)
        pending.extend(_resolve_node(packages, path, name) for name in package.dependencies)
        pending.extend(_resolve_node(packages, path, name) for name in package.peer_dependencies)
        pending.extend(
            resolved
            for name in package.optional_dependencies
            if (resolved := _resolve_optional_node(packages, path, name)) is not None
        )
    return result


def _load_node_lock(data: bytes) -> tuple[dict[str, NodePackage], set[str], set[str]]:
    lock = _json_bytes(data, "package-lock.json")
    raw_packages = lock.get("packages")
    if not isinstance(raw_packages, dict) or not isinstance(raw_packages.get(""), dict):
        raise ComplianceError("package-lock.json has no project root")
    packages: dict[str, NodePackage] = {}
    for lock_path, raw in raw_packages.items():
        if (
            lock_path == ""
            or not isinstance(lock_path, str)
            or not isinstance(raw, dict)
            or raw.get("link") is True
        ):
            continue
        if not _node_target(raw, f"package-lock {lock_path}"):
            continue
        name = _node_name(lock_path)
        version = _string(raw.get("version"), f"package-lock {name} version")
        dependencies = raw.get("dependencies", {})
        optional = raw.get("optionalDependencies", {})
        peers = raw.get("peerDependencies", {})
        peer_meta = raw.get("peerDependenciesMeta", {})
        if not all(isinstance(value, dict) for value in (dependencies, optional, peers, peer_meta)):
            raise ComplianceError(f"package-lock {name} dependency fields must be objects")
        required_peers = tuple(
            dependency
            for dependency in peers
            if not (
                isinstance(peer_meta.get(dependency), dict)
                and peer_meta[dependency].get("optional") is True
            )
        )
        optional_peers = tuple(
            dependency
            for dependency in peers
            if isinstance(peer_meta.get(dependency), dict)
            and peer_meta[dependency].get("optional") is True
        )
        packages[lock_path] = NodePackage(
            f"node:{name}@{version}",
            lock_path,
            tuple(dependencies),
            tuple(optional) + optional_peers,
            required_peers,
            (
                _string(raw.get("resolved"), f"package-lock {name} resolved"),
                _string(raw.get("integrity"), f"package-lock {name} integrity"),
            ),
        )
    root = raw_packages[""]
    runtime_roots = root.get("dependencies", {})
    optional_roots = root.get("optionalDependencies", {})
    peer_roots = root.get("peerDependencies", {})
    peer_meta = root.get("peerDependenciesMeta", {})
    build_roots = root.get("devDependencies", {})
    if not all(
        isinstance(value, dict)
        for value in (runtime_roots, optional_roots, peer_roots, peer_meta, build_roots)
    ):
        raise ComplianceError("package-lock root dependencies must be objects")
    runtime = _node_closure(packages, runtime_roots)
    installed_optional_roots = {
        name: value
        for name, value in optional_roots.items()
        if _resolve_optional_node(packages, "", name) is not None
    }
    runtime.update(_node_closure(packages, installed_optional_roots))
    required_peer_roots = {
        name: value
        for name, value in peer_roots.items()
        if not (isinstance(peer_meta.get(name), dict) and peer_meta[name].get("optional") is True)
    }
    runtime.update(_node_closure(packages, required_peer_roots))
    installed_optional_peer_roots = {
        name: value
        for name, value in peer_roots.items()
        if isinstance(peer_meta.get(name), dict)
        and peer_meta[name].get("optional") is True
        and _resolve_optional_node(packages, "", name) is not None
    }
    runtime.update(_node_closure(packages, installed_optional_peer_roots))
    build = _node_closure(packages, build_roots) - runtime
    return packages, runtime, build


def _integrity(data: bytes, expected: str, label: str) -> None:
    if expected.startswith("sha256:"):
        actual = "sha256:" + _sha256(data)
    elif expected.startswith("sha512-"):
        actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    else:
        raise ComplianceError(f"{label} uses an unsupported integrity algorithm")
    if not hmac.compare_digest(expected, actual):
        raise ComplianceError(f"{label} artifact hash drift")


def _archive_member_name(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value or value.endswith("/"):
        raise ComplianceError(f"{label} contains an unsafe archive member {value!r}")
    return value


def _link_target(
    name: str, target: str, *, hardlink: bool, label: str, allow_absolute: bool
) -> str:
    if not target:
        raise ComplianceError(f"{label} contains an empty link")
    candidate = PurePosixPath(target) if hardlink else PurePosixPath(name).parent / target
    if candidate.is_absolute():
        if not allow_absolute:
            raise ComplianceError(f"{label} link escapes its archive")
        candidate = PurePosixPath(*candidate.parts[1:])
    parts: list[str] = []
    for part in candidate.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ComplianceError(f"{label} link escapes its archive")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _resolve_links(
    files: dict[str, bytes],
    links: dict[str, tuple[str, bool]],
    label: str,
    *,
    allow_absolute: bool = False,
) -> dict[str, bytes]:
    resolved = dict(files)

    def resolve(name: str, visiting: set[str]) -> bytes:
        if name in resolved:
            return resolved[name]
        if name in visiting or name not in links:
            raise ComplianceError(f"{label} contains a cyclic or dangling link")
        visiting.add(name)
        target, hardlink = links[name]
        target_name = _link_target(
            name,
            target,
            hardlink=hardlink,
            label=label,
            allow_absolute=allow_absolute,
        )
        value = resolve(target_name, visiting)
        visiting.remove(name)
        resolved[name] = value
        return value

    for name in links:
        resolve(name, set())
    return resolved


def _archive_members(data: bytes, coordinate: str, artifact_path: str) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    links: dict[str, tuple[str, bool]] = {}
    python_archive = coordinate.startswith("python:")
    wheel_archive = python_archive and artifact_path.endswith(".whl")
    zip_archive = wheel_archive or (python_archive and artifact_path.endswith(".zip"))
    if python_archive and not artifact_path.endswith((".whl", ".zip", ".tar.gz", ".tgz")):
        raise ComplianceError(f"NON_ARCHIVE_PACKAGE_BLOB: {coordinate}")
    if coordinate.startswith("node:") and not artifact_path.endswith((".tgz", ".tar.gz")):
        raise ComplianceError(f"NON_ARCHIVE_PACKAGE_BLOB: {coordinate}")
    if zip_archive:
        if not zipfile.is_zipfile(io.BytesIO(data)):
            error_code = "NON_ARCHIVE_WHEEL_BLOB" if wheel_archive else "NON_ARCHIVE_PACKAGE_BLOB"
            raise ComplianceError(f"{error_code}: {coordinate}")
        try:
            eocd = data.rfind(b"PK\x05\x06", max(0, len(data) - 65_557))
            if eocd < 0 or eocd + 22 > len(data):
                raise ComplianceError(f"{coordinate} ZIP central directory is absent")
            member_count = struct.unpack_from("<H", data, eocd + 10)[0]
            central_size = struct.unpack_from("<I", data, eocd + 12)[0]
            if member_count == 0xFFFF:
                raise ComplianceError(f"{coordinate} ZIP64 member count is unsupported")
            if member_count > MAX_ARCHIVE_MEMBERS:
                raise ComplianceError(f"{coordinate} archive has too many members")
            if central_size > MAX_COMPRESSED_BYTES:
                raise ComplianceError(f"{coordinate} ZIP metadata exceeds its bound")
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos) != member_count:
                    raise ComplianceError(f"{coordinate} ZIP member count is inconsistent")
                total = 0
                for info in infos:
                    if info.is_dir():
                        continue
                    mode = info.external_attr >> 16
                    name = _archive_member_name(info.filename, coordinate)
                    if name in members or name in links:
                        raise ComplianceError(f"{coordinate} archive repeats {name}")
                    if info.file_size > MAX_SINGLE_FILE_BYTES:
                        raise ComplianceError(f"{coordinate} archive member is too large")
                    total += info.file_size
                    if total > MAX_ARCHIVE_BYTES:
                        raise ComplianceError(f"{coordinate} archive is too large")
                    if stat.S_ISLNK(mode):
                        links[name] = (archive.read(info).decode("utf-8"), False)
                    elif stat.S_IFMT(mode) and not stat.S_ISREG(mode):
                        raise ComplianceError(f"{coordinate} ZIP contains a special file")
                    else:
                        members[name] = archive.read(info)
        except zipfile.BadZipFile as error:
            error_code = "NON_ARCHIVE_WHEEL_BLOB" if wheel_archive else "NON_ARCHIVE_PACKAGE_BLOB"
            raise ComplianceError(f"{error_code}: {coordinate}") from error
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r|*") as archive:
                total = 0
                count = 0
                for info in archive:
                    count += 1
                    if count > MAX_ARCHIVE_MEMBERS:
                        raise ComplianceError(f"{coordinate} archive has too many members")
                    if info.isdir():
                        continue
                    if not (info.isfile() or info.issym() or info.islnk()):
                        raise ComplianceError(
                            f"{coordinate} archive contains a link or special file"
                        )
                    name = _archive_member_name(info.name, coordinate)
                    if name in members or name in links:
                        raise ComplianceError(f"{coordinate} archive repeats {name}")
                    if info.size > MAX_SINGLE_FILE_BYTES:
                        raise ComplianceError(f"{coordinate} archive member is too large")
                    total += info.size
                    if total > MAX_ARCHIVE_BYTES:
                        raise ComplianceError(f"{coordinate} archive is too large")
                    if info.issym() or info.islnk():
                        links[name] = (info.linkname, info.islnk())
                        continue
                    stream = archive.extractfile(info)
                    if stream is None:
                        raise ComplianceError(f"{coordinate} archive member is unreadable")
                    members[name] = stream.read()
        except (tarfile.TarError, OSError) as error:
            raise ComplianceError(f"NON_ARCHIVE_PACKAGE_BLOB: {coordinate}") from error
    if not members:
        raise ComplianceError(f"{coordinate} archive is empty")
    return _resolve_links(members, links, coordinate)


LEGACY_LICENSE_CLASSIFIERS = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: MIT No Attribution License (MIT-0)": "MIT-0",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
}
GENERIC_LICENSE_CLASSIFIERS = frozenset({"License :: OSI Approved :: BSD License"})
LEGACY_LICENSE_ALIASES = {
    "Apache 2.0": "Apache-2.0",
    "Apache License, Version 2.0": "Apache-2.0",
    "Apache Software License": "Apache-2.0",
    "Apache Software License v2": "Apache-2.0",
    "BSD 3-Clause License": "BSD-3-Clause",
    "ISC License (ISCL)": "ISC",
    "MIT License": "MIT",
    "MIT No Attribution": "MIT-0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
}
PACKAGE_LICENSE_ALIASES = {
    ("python:pyasn1-modules==0.4.2", "BSD"): "BSD-2-Clause",
    ("python:pyperclip==1.11.0", "BSD"): "BSD-3-Clause",
    ("python:python-dateutil==2.9.0.post0", "Dual License"): ("Apache-2.0 OR BSD-3-Clause"),
    ("python:uvloop==0.22.1", "MIT License"): "Apache-2.0 OR MIT",
}


def _legacy_license(value: object, coordinate: str) -> str:
    if not isinstance(value, str | Header):
        raise ComplianceError(f"{coordinate} legacy License metadata is invalid")
    normalized = " ".join(str(value).split())
    if not normalized:
        raise ComplianceError(f"{coordinate} legacy License metadata is empty")
    package_alias = PACKAGE_LICENSE_ALIASES.get((coordinate, normalized))
    if package_alias is not None:
        return package_alias
    if normalized in APPROVED_LICENSES:
        return normalized
    alias = LEGACY_LICENSE_ALIASES.get(normalized)
    if alias is not None:
        return alias
    if coordinate == "python:beartype==0.22.9" and normalized.startswith("MIT License "):
        return "MIT"
    if coordinate == "python:uncalled-for==0.4.0" and normalized.startswith(
        "# Released under MIT License "
    ):
        return "MIT"
    if coordinate == "python:pandas==3.0.5" and normalized.startswith("BSD 3-Clause License "):
        return "BSD-3-Clause"
    raise ComplianceError(f"{coordinate} has contradictory or unknown legacy License metadata")


def _python_license(metadata: Any, coordinate: str) -> str:
    expressions = metadata.get_all("License-Expression", [])
    if len(expressions) > 1:
        raise ComplianceError(f"{coordinate} archive metadata repeats License-Expression")
    expression = (
        _string(expressions[0], f"{coordinate} archive license expression") if expressions else None
    )
    legacy_values = metadata.get_all("License", [])
    legacy = [value for value in legacy_values if str(value).strip()]
    if expression is not None and legacy:
        raise ComplianceError(f"{coordinate} has contradictory Python license fields")
    if len(legacy) > 1:
        raise ComplianceError(f"{coordinate} has contradictory or unknown legacy License metadata")
    license_classifiers = [
        value for value in metadata.get_all("Classifier", []) if value.startswith("License ::")
    ]
    unknown_classifiers = [
        value
        for value in license_classifiers
        if value not in LEGACY_LICENSE_CLASSIFIERS and value not in GENERIC_LICENSE_CLASSIFIERS
    ]
    if unknown_classifiers:
        raise ComplianceError(f"{coordinate} has an unrecognized legacy license classifier")
    if expression is not None:
        return expression
    if legacy:
        return _legacy_license(legacy[0], coordinate)
    conclusions = {LEGACY_LICENSE_CLASSIFIERS[value] for value in license_classifiers}
    if len(conclusions) != 1:
        raise ComplianceError(
            f"{coordinate} legacy metadata lacks one evidence-backed license conclusion"
        )
    return conclusions.pop()


def _wheel_console_scripts(
    members: dict[str, bytes], coordinate: str, metadata_path: str
) -> dict[str, str]:
    candidates = [path for path in members if path.endswith(".dist-info/entry_points.txt")]
    if len(candidates) > 1:
        raise ComplianceError(f"{coordinate} wheel has ambiguous entry-point metadata")
    if not candidates:
        return {}
    expected_path = str(PurePosixPath(metadata_path).with_name("entry_points.txt"))
    if candidates != [expected_path]:
        raise ComplianceError(f"{coordinate} wheel entry points do not match its metadata")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(members[candidates[0]].decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as error:
        raise ComplianceError(f"{coordinate} wheel entry points are invalid") from error
    result: dict[str, str] = {}
    scripts = parser.items("console_scripts") if parser.has_section("console_scripts") else ()
    for name, target in scripts:
        match = re.fullmatch(
            r"(?P<target>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*)"
            r"(?:\s+\[(?:[A-Za-z0-9._-]+(?:\s*,\s*[A-Za-z0-9._-]+)*)\])?",
            target,
        )
        if (
            not name
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in name
            )
            or name in {".", ".."}
            or match is None
        ):
            raise ComplianceError(f"{coordinate} wheel console script is invalid")
        result[name] = match.group("target")
    return result


def _archive_conclusion(
    coordinate: str, members: dict[str, bytes], *, require_material: bool = True
) -> tuple[str, str, set[str], dict[str, str]]:
    if coordinate.startswith("python:"):
        candidates = [
            path
            for path in members
            if path.endswith(".dist-info/METADATA") or path.endswith("/PKG-INFO")
        ]
        if len(candidates) != 1:
            raise ComplianceError(f"{coordinate} archive has ambiguous package metadata")
        metadata_path = candidates[0]
        try:
            metadata = BytesParser().parsebytes(members[metadata_path])
            for header in ("Name", "Version"):
                if len(metadata.get_all(header, [])) != 1:
                    raise ComplianceError(
                        f"{coordinate} archive metadata repeats or omits {header}"
                    )
            expression = _python_license(metadata, coordinate)
        except ComplianceError:
            raise
        except Exception as error:
            raise ComplianceError(f"{coordinate} package metadata is invalid") from error
        name, version = coordinate.removeprefix("python:").rsplit("==", 1)
        if (
            canonicalize_name(metadata.get("Name", "")) != canonicalize_name(name)
            or metadata.get("Version") != version
        ):
            raise ComplianceError(f"{coordinate} archive metadata identity does not match lock")
    else:
        candidates = [
            path
            for path in members
            if PurePosixPath(path).name == "package.json" and len(PurePosixPath(path).parts) <= 2
        ]
        if len(candidates) != 1:
            raise ComplianceError(f"{coordinate} archive has ambiguous package metadata")
        metadata_path = candidates[0]
        metadata_parts = PurePosixPath(metadata_path).parts
        if len(metadata_parts) == 2 and any(
            PurePosixPath(path).parts[0] != metadata_parts[0] for path in members
        ):
            raise ComplianceError(f"{coordinate} archive has ambiguous package metadata")
        metadata = _json_bytes(members[metadata_path], f"{coordinate} package.json")
        expression = metadata.get("license")
        name, version = coordinate.removeprefix("node:").rsplit("@", 1)
        if metadata.get("name") != name or metadata.get("version") != version:
            raise ComplianceError(f"{coordinate} archive metadata identity does not match lock")
    expression = _string(expression, f"{coordinate} archive license expression")
    if expression not in APPROVED_LICENSES:
        raise ComplianceError(
            f"{coordinate} has ambiguous, unknown, or incompatible license {expression!r}"
        )
    material = {
        path for path in members if PurePosixPath(path).name.lower().startswith(MATERIAL_NAMES)
    }
    if require_material and not material:
        raise ComplianceError(f"{coordinate} archive has no license material")
    console_scripts = (
        _wheel_console_scripts(members, coordinate, metadata_path)
        if coordinate.startswith("python:")
        else {}
    )
    return expression, metadata_path, material, console_scripts


def _supplemental_licenses(export: Path) -> dict[str, dict[str, Any]]:
    raw = _read_control_json(
        export,
        SUPPLEMENTAL_LICENSE_PATH,
        "supplemental license registry",
    )
    if set(raw) != {"schema_version", "packages"} or raw.get("schema_version") != 1:
        raise ComplianceError("supplemental license registry schema is invalid")
    packages = raw.get("packages")
    if not isinstance(packages, list):
        raise ComplianceError("supplemental license registry packages must be a list")
    reviewed: dict[str, dict[str, Any]] = {}
    retained_paths: set[str] = set()
    for item in packages:
        if not isinstance(item, dict):
            raise ComplianceError("supplemental license registry package is invalid")
        coordinate = _string(item.get("coordinate"), "supplemental license coordinate")
        lock_path = item.get("lock_path")
        expected_keys = {
            "artifacts",
            "coordinate",
            "disposition",
            "license_expression",
            "sources",
        }
        if coordinate.startswith("node:"):
            expected_keys.add("lock_path")
            lock_path = _relative(lock_path, f"{coordinate} supplemental lock path")
            identity = f"{coordinate}#{lock_path}"
        else:
            identity = coordinate
        if set(item) != expected_keys or identity in reviewed:
            raise ComplianceError(f"{coordinate} supplemental license entry is invalid")
        expression = _string(
            item.get("license_expression"), f"{coordinate} supplemental license expression"
        )
        if expression not in APPROVED_LICENSES:
            raise ComplianceError(f"{coordinate} supplemental license is incompatible")
        disposition = _string(item.get("disposition"), f"{coordinate} supplemental disposition")
        if disposition not in {
            "upstream-license-at-release",
            "artifact-expression-plus-spdx-terms",
        }:
            raise ComplianceError(f"{coordinate} supplemental disposition is unsupported")
        artifacts = item.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ComplianceError(f"{coordinate} supplemental artifacts are invalid")
        artifact_pairs: set[tuple[str, str]] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != {"integrity", "locator"}:
                raise ComplianceError(f"{coordinate} supplemental artifact is invalid")
            pair = (
                _string(artifact.get("locator"), f"{coordinate} supplemental locator"),
                _string(artifact.get("integrity"), f"{coordinate} supplemental integrity"),
            )
            if pair in artifact_pairs:
                raise ComplianceError(f"{coordinate} repeats a supplemental artifact")
            artifact_pairs.add(pair)
        sources = item.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ComplianceError(f"{coordinate} supplemental sources are invalid")
        reviewed_sources: list[dict[str, str]] = []
        for source in sources:
            if not isinstance(source, dict) or set(source) != {
                "path",
                "revision",
                "sha256",
                "upstream_path",
                "url",
            }:
                raise ComplianceError(f"{coordinate} supplemental source is invalid")
            revision = _string(source.get("revision"), f"{coordinate} source revision")
            if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                raise ComplianceError(f"{coordinate} source revision is not immutable")
            upstream_path = _relative(
                source.get("upstream_path"), f"{coordinate} supplemental upstream path"
            )
            url = _string(source.get("url"), f"{coordinate} supplemental source URL")
            if (
                re.fullmatch(
                    rf"https://raw\.githubusercontent\.com/[^/]+/[^/]+/{revision}/"
                    + re.escape(upstream_path),
                    url,
                )
                is None
            ):
                raise ComplianceError(f"{coordinate} supplemental source URL is not immutable")
            digest = _string(source.get("sha256"), f"{coordinate} supplemental source hash")
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ComplianceError(f"{coordinate} supplemental source hash is invalid")
            path = _relative(source.get("path"), f"{coordinate} supplemental retained path")
            if path != f"third_party/notices/{digest}.txt":
                raise ComplianceError(f"{coordinate} supplemental retained path is not addressed")
            data = _read_control(export, path, f"{coordinate} supplemental retained text")
            if _sha256(data) != digest:
                raise ComplianceError(f"{coordinate} supplemental retained text drift")
            try:
                if not data.decode("utf-8").strip():
                    raise ComplianceError(f"{coordinate} supplemental retained text is empty")
            except UnicodeDecodeError as error:
                raise ComplianceError(
                    f"{coordinate} supplemental retained text is not UTF-8"
                ) from error
            retained_paths.add(path)
            reviewed_sources.append(
                {
                    "path": path,
                    "revision": revision,
                    "sha256": digest,
                    "upstream_path": upstream_path,
                    "url": url,
                }
            )
        if disposition == "artifact-expression-plus-spdx-terms" and (
            expression != "MIT"
            or len(reviewed_sources) != 1
            or reviewed_sources[0]["upstream_path"] != "text/MIT.txt"
            or reviewed_sources[0]["url"]
            != (
                "https://raw.githubusercontent.com/spdx/license-list-data/"
                f"{reviewed_sources[0]['revision']}/text/MIT.txt"
            )
        ):
            raise ComplianceError(f"{coordinate} canonical SPDX terms binding is invalid")
        reviewed[identity] = {
            "artifacts": frozenset(artifact_pairs),
            "disposition": disposition,
            "license_expression": expression,
            "sources": reviewed_sources,
        }
    actual_notice_paths = {
        path
        for path in _walk_files(
            export,
            "third_party/notices",
            symlink_label="SYMLINKED_CONTROL",
        )
        if path != SUPPLEMENTAL_LICENSE_PATH
    }
    if actual_notice_paths != retained_paths:
        raise ComplianceError("supplemental retained license file set differs from registry")
    return reviewed


def _wheel_record(members: dict[str, bytes], coordinate: str) -> list[dict[str, Any]]:
    candidates = [path for path in members if path.endswith(".dist-info/RECORD")]
    if len(candidates) != 1:
        raise ComplianceError(f"{coordinate} wheel has ambiguous RECORD evidence")
    record_path = candidates[0]
    try:
        rows = csv.reader(io.StringIO(members[record_path].decode("utf-8")))
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if len(row) != 3:
                raise ComplianceError(f"{coordinate} wheel RECORD row is invalid")
            path = _archive_member_name(row[0], f"{coordinate} wheel RECORD")
            if path in seen or path not in members:
                raise ComplianceError(f"{coordinate} wheel RECORD path is duplicate or absent")
            seen.add(path)
            if path == record_path:
                if row[1] or row[2]:
                    raise ComplianceError(f"{coordinate} wheel RECORD self-row is invalid")
                continue
            if not row[1].startswith("sha256=") or not row[2].isdecimal():
                raise ComplianceError(f"{coordinate} wheel RECORD lacks exact hash or size")
            encoded = row[1].removeprefix("sha256=")
            expected = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if not hmac.compare_digest(expected, hashlib.sha256(members[path]).digest()) or int(
                row[2]
            ) != len(members[path]):
                raise ComplianceError(f"{coordinate} wheel RECORD hash or size drift")
            result.append(
                {"path": path, "sha256": _sha256(members[path]), "size": len(members[path])}
            )
    except (UnicodeDecodeError, csv.Error, ValueError) as error:
        if isinstance(error, ComplianceError):
            raise
        raise ComplianceError(f"{coordinate} wheel RECORD is invalid") from error
    if seen != set(members):
        raise ComplianceError(f"{coordinate} wheel RECORD omits archive members")
    return result


def _hash_record(export: Path, raw: object, label: str) -> tuple[str, str, bytes]:
    if not isinstance(raw, dict):
        raise ComplianceError(f"{label} must be an object")
    relative = _relative(raw.get("path"), f"{label} path")
    data = _regular_file(export, relative, label).read_bytes()
    digest = _string(raw.get("sha256"), f"{label} hash")
    if digest != _sha256(data):
        raise ComplianceError(f"{label} hash drift")
    return relative, digest, data


def _package_evidence(
    export: Path,
    artifact_root: Path,
    raw_packages: object,
    locked: dict[str, frozenset[tuple[str, str]]],
    roles: dict[str, set[str]],
) -> list[dict[str, Any]]:
    if not isinstance(raw_packages, list):
        raise ComplianceError("packages evidence must be a list")
    reviewed: dict[str, dict[str, Any]] = {}
    supplemental = _supplemental_licenses(export)
    used_supplemental: set[str] = set()
    for index, raw in enumerate(raw_packages):
        if not isinstance(raw, dict):
            raise ComplianceError(f"package evidence #{index + 1} must be an object")
        if "license_expression" in raw or "roles" in raw:
            raise ComplianceError(
                "SELF_ASSERTED_LICENSE: package conclusions must come from archives"
            )
        coordinate = _string(raw.get("coordinate"), "package coordinate")
        if coordinate.startswith("node:"):
            lock_path = _relative(raw.get("lock_path"), f"{coordinate} lock path")
            identity = f"{coordinate}#{lock_path}"
        else:
            if "lock_path" in raw:
                raise ComplianceError(f"{coordinate} has an unexpected lock path")
            lock_path = None
            identity = coordinate
        if identity in reviewed:
            raise ComplianceError(f"package evidence repeats {identity}")
        if identity not in roles:
            error_code = (
                "ORPHAN_NODE_CLASSIFICATION"
                if coordinate.startswith("node:")
                else "unused package evidence"
            )
            raise ComplianceError(f"{error_code}: {identity}")
        artifact = raw.get("artifact")
        if not isinstance(artifact, dict):
            raise ComplianceError(f"{coordinate} artifact must be an object")
        pair = (
            _string(artifact.get("locator"), f"{coordinate} locator"),
            _string(artifact.get("integrity"), f"{coordinate} integrity"),
        )
        if pair not in locked.get(identity, frozenset()):
            raise ComplianceError(f"{identity} artifact does not match its exact lock entry")
        artifact_path = _relative(artifact.get("path"), f"{coordinate} artifact path")
        data = _bounded_bytes(
            _regular_file(artifact_root, artifact_path, f"{coordinate} artifact"),
            f"{coordinate} artifact",
        )
        _integrity(data, pair[1], coordinate)
        members = _archive_members(data, coordinate, artifact_path)
        expression, metadata_member, material_members, console_scripts = _archive_conclusion(
            coordinate,
            members,
            require_material=False,
        )
        supplemental_members: list[dict[str, str]] = []
        if material_members:
            if identity in supplemental:
                raise ComplianceError(f"{coordinate} has unnecessary supplemental license evidence")
        else:
            supplement = supplemental.get(identity)
            if (
                supplement is None
                or pair not in supplement["artifacts"]
                or expression != supplement["license_expression"]
            ):
                raise ComplianceError(f"{coordinate} archive has no bound license material")
            used_supplemental.add(identity)
            supplemental_members = [
                {
                    **source,
                    "disposition": supplement["disposition"],
                }
                for source in supplement["sources"]
            ]
        wheel_record = (
            _wheel_record(members, coordinate)
            if coordinate.startswith("python:") and artifact_path.endswith(".whl")
            else []
        )
        retained_raw = raw.get("retained_files")
        if not isinstance(retained_raw, list):
            raise ComplianceError(f"{coordinate} retained_files must be a list")
        retained: dict[str, dict[str, str]] = {}
        for item in retained_raw:
            if not isinstance(item, dict):
                raise ComplianceError(f"{coordinate} has invalid retained file")
            archive_member = _relative(item.get("archive_path"), f"{coordinate} archive path")
            path, digest, retained_data = _hash_record(export, item, f"{coordinate} retained file")
            if archive_member not in members or retained_data != members[archive_member]:
                raise ComplianceError(
                    f"{coordinate} mismatched upstream material: {archive_member}"
                )
            retained[archive_member] = {
                "archive_path": archive_member,
                "path": path,
                "sha256": digest,
            }
        expected = material_members | {metadata_member}
        if set(retained) != expected:
            raise ComplianceError(
                f"{coordinate} retained evidence does not exactly match archive material"
            )
        sources = raw.get("source_members")
        if not isinstance(sources, list):
            raise ComplianceError(f"{coordinate} source_members must name archive members")
        retained_sources: list[dict[str, str]] = []
        if expression == "MPL-2.0":
            expected_sources = {
                path
                for path in members
                if ".dist-info/" not in path and not path.endswith(".dist-info")
            }
            for item in sources:
                if not isinstance(item, dict):
                    raise ComplianceError(
                        f"{coordinate} MPL-2.0 evidence lacks retained source form"
                    )
                archive_member = _relative(
                    item.get("archive_path"), f"{coordinate} MPL source archive path"
                )
                path, digest, retained_data = _hash_record(
                    export, item, f"{coordinate} retained source form"
                )
                if archive_member not in members or retained_data != members[archive_member]:
                    raise ComplianceError(f"{coordinate} mismatched retained source form")
                disposition = _string(
                    item.get("obligation_disposition"),
                    f"{coordinate} MPL source obligation disposition",
                )
                if disposition not in {"unmodified-source-retained", "modified-source-retained"}:
                    raise ComplianceError(f"{coordinate} MPL source obligation is unresolved")
                retained_sources.append(
                    {
                        "archive_path": archive_member,
                        "path": path,
                        "sha256": digest,
                        "obligation_disposition": disposition,
                    }
                )
            if {item["archive_path"] for item in retained_sources} != expected_sources:
                raise ComplianceError(f"{coordinate} MPL-2.0 evidence lacks retained source form")
        elif any(not isinstance(item, str) or item not in members for item in sources):
            raise ComplianceError(f"{coordinate} source_members must name archive members")
        if raw.get("review_disposition") != "approved":
            raise ComplianceError(f"{coordinate} lacks approved review disposition")
        reviewed[identity] = {
            "coordinate": coordinate,
            **({"lock_path": lock_path} if lock_path is not None else {}),
            "roles": sorted(roles[identity]),
            "artifact": {"locator": pair[0], "integrity": pair[1], "sha256": _sha256(data)},
            "license_expression": expression,
            "archive_members": [retained[key] for key in sorted(retained)],
            "supplemental_license_members": supplemental_members,
            "source_members": (
                sorted(retained_sources, key=lambda item: item["archive_path"])
                if expression == "MPL-2.0"
                else sorted(sources)
            ),
            "wheel_record": wheel_record,
            "console_scripts": console_scripts,
            "review_disposition": "approved",
        }
    missing = set(roles) - set(reviewed)
    if missing:
        raise ComplianceError(
            "locked packages lack archive evidence: " + ", ".join(sorted(missing))
        )
    if used_supplemental != set(supplemental):
        raise ComplianceError("supplemental license registry contains unused package evidence")
    return [reviewed[key] for key in sorted(reviewed)]


def _ofl_source(
    value: object,
    *,
    repository_url: str,
    revision: str,
    label: str,
) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "release_path",
        "sha256",
        "upstream_path",
        "url",
    }:
        raise ComplianceError(f"{label} source fields are invalid")
    release_path = _relative(value.get("release_path"), f"{label} release path")
    upstream_path = _relative(value.get("upstream_path"), f"{label} upstream path")
    digest = _string(value.get("sha256"), f"{label} hash")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ComplianceError(f"{label} hash is invalid")
    url = _string(value.get("url"), f"{label} URL")
    repository = re.fullmatch(
        r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)",
        repository_url,
    )
    if repository is None:
        raise ComplianceError(f"{label} repository URL is not a fixed HTTPS GitHub repository")
    expected_url = (
        "https://raw.githubusercontent.com/"
        f"{repository['owner']}/{repository['repo']}/{revision}/{upstream_path}"
    )
    if url != expected_url:
        raise ComplianceError(f"{label} source URL is not immutable")
    return {
        "release_path": release_path,
        "sha256": digest,
        "upstream_path": upstream_path,
        "url": url,
    }


def _ofl_manifest(export: Path, path: str, digest: str) -> dict[str, Any]:
    if not path.endswith(OFL_PROVENANCE_SUFFIX):
        raise ComplianceError("OFL font provenance must use the fixed manifest suffix")
    data = _bounded_bytes(
        _regular_file(export, path, "OFL font provenance", control=True),
        "OFL font provenance",
        limit=1024 * 1024,
    )
    if _sha256(data) != digest:
        raise ComplianceError("OFL font provenance hash drift")
    value = _json_bytes(data, "OFL font provenance")
    if (
        set(value)
        != {
            "fonts",
            "license",
            "repository_url",
            "schema_version",
            "source_revision",
        }
        or value.get("schema_version") != 1
    ):
        raise ComplianceError("OFL font provenance fields are invalid")
    revision = _string(value.get("source_revision"), "OFL font source revision")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ComplianceError("OFL font source revision is not immutable")
    repository_url = _string(value.get("repository_url"), "OFL font repository URL")
    license_source = _ofl_source(
        value.get("license"),
        repository_url=repository_url,
        revision=revision,
        label="OFL license",
    )
    raw_fonts = value.get("fonts")
    if not isinstance(raw_fonts, list) or not raw_fonts:
        raise ComplianceError("OFL font provenance must list at least one font")
    fonts: dict[str, dict[str, str]] = {}
    for raw_font in raw_fonts:
        font = _ofl_source(
            raw_font,
            repository_url=repository_url,
            revision=revision,
            label="OFL font",
        )
        release_path = font["release_path"]
        if PurePosixPath(release_path).suffix.lower() not in FONT_SUFFIXES:
            raise ComplianceError("OFL provenance may contain only WOFF or WOFF2 fonts")
        if release_path in fonts:
            raise ComplianceError("OFL font provenance repeats a font")
        fonts[release_path] = font
    return {
        "repository_url": repository_url,
        "source_revision": revision,
        "license": license_source,
        "fonts": fonts,
    }


def _materialize_ofl_asset_evidence(export: Path, registry: dict[str, Any]) -> dict[str, Any]:
    raw_assets = registry.get("assets")
    if not isinstance(raw_assets, list):
        raise ComplianceError("assets evidence must be a list")
    existing: dict[str, dict[str, Any]] = {}
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise ComplianceError("asset evidence must contain objects")
        path = _relative(raw.get("path"), "asset path")
        if path in existing:
            raise ComplianceError(f"asset evidence repeats {path}")
        existing[path] = raw

    release_files = _walk_files(export, ".", symlink_label="UNREGISTERED_PUBLIC_SYMLINK")
    generated: dict[str, dict[str, Any]] = {}
    for provenance_path in sorted(
        path for path in release_files if path.endswith(OFL_PROVENANCE_SUFFIX)
    ):
        manifest_digest = _sha256(release_files[provenance_path])
        manifest = _ofl_manifest(export, provenance_path, manifest_digest)
        parent = PurePosixPath(provenance_path).parent
        license_source = manifest["license"]
        license_path = license_source["release_path"]
        if (
            PurePosixPath(license_path).parent != parent
            or PurePosixPath(license_path).name != "OFL.txt"
        ):
            raise ComplianceError("OFL font manifest license must be adjacent")
        for path, font in sorted(manifest["fonts"].items()):
            if PurePosixPath(path).parent != parent:
                raise ComplianceError("OFL font manifest entries must be adjacent")
            if path in generated:
                raise ComplianceError(f"OFL font manifests repeat {path}")
            generated[path] = {
                "path": path,
                "sha256": font["sha256"],
                "origin": (
                    f"{manifest['repository_url']} at {manifest['source_revision']}:"
                    f"{font['upstream_path']}"
                ),
                "creation_method": (
                    "unmodified upstream " + PurePosixPath(path).suffix.removeprefix(".").upper()
                ),
                "license_expression": OFL_LICENSE,
                "ofl_evidence": {
                    "license_path": license_path,
                    "license_sha256": license_source["sha256"],
                    "provenance_path": provenance_path,
                    "provenance_sha256": manifest_digest,
                },
            }

    retained: list[dict[str, Any]] = []
    existing_ofl: dict[str, dict[str, Any]] = {}
    for path, raw in existing.items():
        if raw.get("license_expression") == OFL_LICENSE:
            existing_ofl[path] = raw
        else:
            if path in generated:
                raise ComplianceError(f"generated OFL asset conflicts with {path}")
            retained.append(raw)
    if existing_ofl and existing_ofl != generated:
        raise ComplianceError("dependency evidence OFL assets differ from adjacent provenance")

    materialized = {
        **registry,
        "assets": sorted([*retained, *generated.values()], key=lambda item: item["path"]),
    }
    _assets(export, materialized["assets"])
    return materialized


def _assets(export: Path, raw_assets: object) -> list[dict[str, Any]]:
    if not isinstance(raw_assets, list):
        raise ComplianceError("assets evidence must be a list")
    actual: dict[str, bytes] = {}
    for prefix in ASSET_PREFIXES:
        actual.update(
            _walk_files(export, prefix.rstrip("/"), symlink_label="UNREGISTERED_PUBLIC_SYMLINK")
        )
    release_files = _walk_files(export, ".", symlink_label="UNREGISTERED_PUBLIC_SYMLINK")
    for path, data in release_files.items():
        if PurePosixPath(path).suffix.lower() in ASSET_SUFFIXES:
            actual[path] = data
    reviewed: dict[str, dict[str, Any]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    manifest_assets: dict[str, set[str]] = defaultdict(set)
    retained_evidence_paths: set[str] = set()
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise ComplianceError("asset evidence must contain objects")
        path = _relative(raw.get("path"), "asset path")
        if path not in actual:
            raise ComplianceError(f"public-export path mismatch: absent {path}")
        digest = _string(raw.get("sha256"), f"{path} hash")
        if digest != _sha256(actual[path]):
            raise ComplianceError(f"asset hash drift for {path}")
        expression = _string(raw.get("license_expression"), f"{path} license")
        suffix = PurePosixPath(path).suffix.lower()
        ofl_evidence = raw.get("ofl_evidence")
        if expression == OFL_LICENSE:
            if suffix not in FONT_SUFFIXES:
                raise ComplianceError("OFL-1.1 is restricted to WOFF and WOFF2 font assets")
            if not isinstance(ofl_evidence, dict) or set(ofl_evidence) != {
                "license_path",
                "license_sha256",
                "provenance_path",
                "provenance_sha256",
            }:
                raise ComplianceError(f"{path} lacks exact OFL font evidence")
        elif ofl_evidence is not None:
            raise ComplianceError(f"{path} contains unused OFL font evidence")
        elif expression not in APPROVED_LICENSES:
            raise ComplianceError(f"{path} has incompatible license")
        asset: dict[str, Any] = {
            key: _string(raw.get(key), f"{path} {key}")
            for key in ("path", "sha256", "origin", "creation_method", "license_expression")
        }
        if expression == OFL_LICENSE:
            evidence = {
                key: _string(ofl_evidence.get(key), f"{path} {key}")
                for key in (
                    "license_path",
                    "license_sha256",
                    "provenance_path",
                    "provenance_sha256",
                )
            }
            parent = PurePosixPath(path).parent
            license_path = _relative(evidence["license_path"], f"{path} OFL license path")
            provenance_path = _relative(evidence["provenance_path"], f"{path} OFL provenance path")
            if (
                PurePosixPath(license_path).parent != parent
                or PurePosixPath(provenance_path).parent != parent
                or PurePosixPath(license_path).name != "OFL.txt"
                or license_path == provenance_path
            ):
                raise ComplianceError(f"{path} OFL evidence must be adjacent to the font")
            license_data = _bounded_bytes(
                _regular_file(export, license_path, f"{path} OFL license", control=True),
                f"{path} OFL license",
                limit=1024 * 1024,
            )
            if _sha256(license_data) != evidence["license_sha256"]:
                raise ComplianceError(f"{path} OFL license hash drift")
            try:
                license_text = license_data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ComplianceError(f"{path} OFL license is not UTF-8") from error
            if "SIL OPEN FONT LICENSE Version 1.1" not in license_text:
                raise ComplianceError(f"{path} retained license is not OFL-1.1")
            manifest = manifests.get(provenance_path)
            if manifest is None:
                manifest = _ofl_manifest(export, provenance_path, evidence["provenance_sha256"])
                manifests[provenance_path] = manifest
            elif manifest["provenance_sha256"] != evidence["provenance_sha256"]:
                raise ComplianceError("OFL font provenance path has conflicting hashes")
            manifest["provenance_sha256"] = evidence["provenance_sha256"]
            if (
                manifest["license"]["release_path"] != license_path
                or manifest["license"]["sha256"] != evidence["license_sha256"]
            ):
                raise ComplianceError(f"{path} OFL manifest does not bind its retained license")
            font = manifest["fonts"].get(path)
            if font is None or font["sha256"] != digest:
                raise ComplianceError(f"{path} OFL manifest does not bind the font bytes")
            manifest_assets[provenance_path].add(path)
            retained_evidence_paths.update((license_path, provenance_path))
            asset["ofl_evidence"] = {
                **evidence,
                "repository_url": manifest["repository_url"],
                "source_revision": manifest["source_revision"],
                "upstream_path": font["upstream_path"],
                "source_url": font["url"],
                "license_upstream_path": manifest["license"]["upstream_path"],
                "license_source_url": manifest["license"]["url"],
            }
        reviewed[path] = asset
    unregistered = set(actual) - set(reviewed) - retained_evidence_paths
    absent = set(reviewed) - set(actual)
    if unregistered or absent:
        raise ComplianceError(
            "public-export path mismatch: " + ", ".join(sorted(unregistered | absent))
        )
    provenance_files = {path for path in release_files if path.endswith(OFL_PROVENANCE_SUFFIX)}
    if provenance_files != set(manifests):
        raise ComplianceError(
            "unused OFL font provenance files: "
            + ", ".join(sorted(provenance_files ^ set(manifests)))
        )
    for provenance_path, manifest in manifests.items():
        if set(manifest["fonts"]) != manifest_assets[provenance_path]:
            raise ComplianceError(f"unused OFL font provenance entries in {provenance_path}")
    return [reviewed[key] for key in sorted(reviewed)]


def _inventory_files(raw: object, label: str) -> dict[str, str]:
    if not isinstance(raw, list):
        raise ComplianceError(f"{label} files must be a list")
    result: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ComplianceError(f"{label} file must be an object")
        path = _string(item.get("path"), f"{label} file path")
        if path in result:
            raise ComplianceError(f"{label} repeats {path}")
        result[path] = _string(item.get("sha256"), f"{label} file hash")
    return result


def _bundle(export: Path, raw: dict[str, Any], expected_packages: set[str]) -> None:
    if raw.get("schema_version") != 2:
        raise ComplianceError("frontend bundle schema_version must be 2")
    root = _relative(raw.get("root"), "frontend bundle root")
    actual = {
        path: _sha256(data)
        for path, data in _walk_files(export, root, symlink_label="UNBOUND_BUNDLE_FILE").items()
    }
    recorded = _inventory_files(raw.get("files"), "frontend bundle")
    if actual != recorded:
        raise ComplianceError("UNBOUND_BUNDLE_FILE: frontend dist byte inventory differs")
    packages = raw.get("packages")
    if (
        not isinstance(packages, list)
        or any(not isinstance(item, str) for item in packages)
        or set(packages) != expected_packages
    ):
        raise ComplianceError("frontend bundle package inventory drifts from reachable lock graph")


def _oci_blob(root: Path, digest: object, label: str) -> bytes:
    value = _string(digest, f"{label} digest")
    if not value.startswith("sha256:") or len(value) != 71:
        raise ComplianceError(f"{label} digest is not SHA-256")
    data = _bounded_bytes(_regular_file(root, f"blobs/sha256/{value[7:]}", label), label)
    if "sha256:" + _sha256(data) != value:
        raise ComplianceError(f"{label} digest drift")
    return data


def _external_file(root: Path, raw: object, label: str) -> bytes:
    if not isinstance(raw, dict):
        raise ComplianceError(f"{label} must be an object")
    path = _relative(raw.get("path"), f"{label} path")
    data = _bounded_bytes(_regular_file(root, path, label, control=True), label)
    if _string(raw.get("sha256"), f"{label} hash") != _sha256(data):
        raise ComplianceError(f"{label} hash drift")
    return data


@dataclass(frozen=True)
class DockerAuthority:
    application_base: str
    uv_base: str
    application_copies: tuple[str, ...]
    application_runs: tuple[str, ...]


def _docker_authority(data: bytes) -> DockerAuthority:
    try:
        physical = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ComplianceError("Dockerfile is not UTF-8") from error
    escape = "\\"
    directive_window = True
    for raw in physical:
        stripped = raw.strip()
        if not stripped:
            directive_window = False
            continue
        if not stripped.startswith("#"):
            break
        directive = stripped[1:].strip()
        if directive.lower().startswith("escape="):
            if not directive_window:
                raise ComplianceError("Dockerfile escape directive is outside directive window")
            value = directive.split("=", 1)[1].strip()
            if value not in {"\\", "`"}:
                raise ComplianceError("Dockerfile escape directive is invalid")
            escape = value
        elif "=" not in directive:
            directive_window = False
    logical: list[str] = []
    pending = ""
    for raw in physical:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continued = stripped.endswith(escape)
        fragment = stripped[:-1].rstrip() if continued else stripped
        pending = f"{pending} {fragment}".strip()
        if not continued:
            logical.append(pending)
            pending = ""
    if pending:
        raise ComplianceError("Dockerfile ends in an incomplete instruction")
    stage: str | None = None
    application_base = ""
    uv_base = ""
    copies: list[str] = []
    runs: list[str] = []
    for line in logical:
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ComplianceError("Dockerfile contains a malformed instruction")
        opcode, arguments = parts[0].upper(), " ".join(parts[1].split())
        if opcode == "FROM":
            instruction_parts = arguments.split()
            stage = (
                instruction_parts[-1]
                if len(instruction_parts) >= 3 and instruction_parts[-2].upper() == "AS"
                else None
            )
            if stage == "application":
                application_base = instruction_parts[0] if len(instruction_parts) == 3 else ""
            elif stage == "uv":
                uv_base = instruction_parts[0] if len(instruction_parts) == 3 else ""
            continue
        if stage == "application" and opcode == "COPY":
            copies.append(arguments)
        elif stage == "application" and opcode == "RUN":
            runs.append(arguments)
    return DockerAuthority(application_base, uv_base, tuple(copies), tuple(runs))


def _installed_metadata(data: bytes, coordinate: str) -> str:
    metadata = BytesParser().parsebytes(data)
    for header in ("Name", "Version"):
        if len(metadata.get_all(header, [])) != 1:
            raise ComplianceError(f"{coordinate} installed metadata repeats or omits {header}")
    name, version = coordinate.removeprefix("python:").rsplit("==", 1)
    if (
        canonicalize_name(metadata["Name"]) != canonicalize_name(name)
        or metadata["Version"] != version
    ):
        raise ComplianceError(f"{coordinate} installed metadata identity mismatch")
    return _python_license(metadata, coordinate)


def _console_script_bytes(venv_root: str, target: str) -> bytes:
    module, function = target.split(":", 1)
    return (
        f"#!{venv_root}/bin/python\n"
        "# -*- coding: utf-8 -*-\n"
        "import sys\n"
        f"from {module} import {function}\n"
        'if __name__ == "__main__":\n'
        '    if sys.argv[0].endswith("-script.pyw"):\n'
        "        sys.argv[0] = sys.argv[0][:-11]\n"
        '    elif sys.argv[0].endswith(".exe"):\n'
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        f"    sys.exit({function}())\n"
    ).encode()


def _normalize_venv_path(base: str, value: str, venv_root: str) -> str:
    if PurePosixPath(value).is_absolute():
        raise ComplianceError("installed RECORD contains an absolute path")
    parts = list(PurePosixPath(base).parts)
    for part in PurePosixPath(value).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if len(parts) <= 1:
                raise ComplianceError("installed RECORD path escapes the venv")
            parts.pop()
        else:
            parts.append(part)
    normalized = str(PurePosixPath(*parts))
    root = venv_root.rstrip("/")
    if normalized != root and not normalized.startswith(root + "/"):
        raise ComplianceError("installed RECORD path escapes the exact venv root")
    return normalized


def _record_owned_paths(
    record_path: str, data: bytes, filesystem: dict[str, bytes], venv_root: str
) -> set[str]:
    base = str(PurePosixPath(record_path).parent.parent)
    try:
        rows = csv.reader(io.StringIO(data.decode("utf-8")))
        result: set[str] = set()
        for row in rows:
            if not row:
                continue
            if len(row) != 3:
                raise ComplianceError("installed RECORD row must have three fields")
            path = _normalize_venv_path(base, row[0], venv_root)
            if path in result:
                raise ComplianceError("installed RECORD repeats a normalized path")
            if path not in filesystem:
                raise ComplianceError("installed RECORD contains an absent path")
            hash_value, size_value = row[1], row[2]
            if path == record_path:
                if hash_value or size_value:
                    raise ComplianceError("installed RECORD self-row must omit hash and size")
            else:
                if not hash_value.startswith("sha256=") or not size_value.isdecimal():
                    raise ComplianceError("installed RECORD lacks an exact hash or size")
                encoded = hash_value.removeprefix("sha256=")
                try:
                    expected = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                except ValueError as error:
                    raise ComplianceError("installed RECORD hash is invalid") from error
                if not hmac.compare_digest(expected, hashlib.sha256(filesystem[path]).digest()):
                    raise ComplianceError(f"installed RECORD hash drift for {path}")
                if int(size_value) != len(filesystem[path]):
                    raise ComplianceError(f"installed RECORD size drift for {path}")
            result.add(path)
        return result
    except (UnicodeDecodeError, csv.Error) as error:
        raise ComplianceError("installed RECORD is invalid") from error


def _control_records(data: bytes, label: str) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ComplianceError(f"{label} is not UTF-8") from error
    records: list[dict[str, str]] = []
    for paragraph in text.split("\n\n"):
        fields: dict[str, str] = {}
        current: str | None = None
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")) and current is not None:
                fields[current] += "\n" + line[1:]
                continue
            if ":" not in line:
                raise ComplianceError(f"{label} contains a malformed field")
            key, value = line.split(":", 1)
            value = value[1:] if value.startswith(" ") else value
            if key in fields:
                raise ComplianceError(f"{label} repeats field {key}")
            fields[key] = value
            current = key
        if fields:
            records.append(fields)
    return records


DEBIAN_LICENSE_ALIASES = {
    "Expat": "MIT",
    "BSD-2-clause": "BSD-2-Clause",
    "BSD-3-clause": "BSD-3-Clause",
}


def _debian_pattern_matches(path: str, pattern: str, coordinate: str) -> bool:
    translated = ["^"]
    escaped = False
    for character in pattern:
        if escaped:
            translated.append(re.escape(character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "*":
            translated.append(".*")
        elif character == "?":
            translated.append(".")
        else:
            translated.append(re.escape(character))
    if escaped:
        raise ComplianceError(f"{coordinate} has an invalid Debian Files pattern")
    translated.append("$")
    return re.match("".join(translated), path) is not None


def _debian_license(data: bytes, coordinate: str, paths: Iterable[str] = ("*",)) -> str:
    records = _control_records(data, f"{coordinate} Debian copyright")
    if not records or not records[0].get("Format", "").startswith("https://www.debian.org/"):
        raise ComplianceError(f"{coordinate} lacks machine-readable Debian copyright evidence")
    file_records = [record for record in records if "Files" in record]
    if not file_records or any(
        "Copyright" not in record or "License" not in record for record in file_records
    ):
        raise ComplianceError(f"{coordinate} has incomplete Debian file stanzas")
    definition_records = [
        record for record in records if "Files" not in record and "License" in record
    ]
    definition_items = [
        (
            record["License"].splitlines()[0].strip(),
            "\n".join(record["License"].splitlines()[1:]).strip(),
        )
        for record in definition_records
    ]
    definitions = {expression: body for expression, body in definition_items}
    if len(definitions) != len(definition_items):
        raise ComplianceError(f"{coordinate} repeats a Debian license definition")
    selected: set[str] = set()
    for path in paths:
        normalized = path.lstrip("/")
        expression = None
        for record in file_records:
            patterns = record["Files"].split()
            if any(
                _debian_pattern_matches(normalized, pattern, coordinate) for pattern in patterns
            ):
                expression = record["License"].splitlines()[0].strip()
        if expression is None:
            raise ComplianceError(f"{coordinate} copyright patterns omit {normalized}")
        selected.add(expression)
    normalized_expressions: set[str] = set()
    for expression in selected:
        if not definitions.get(expression):
            raise ComplianceError(f"{coordinate} has incomplete Debian license evidence")
        normalized = DEBIAN_LICENSE_ALIASES.get(expression, expression)
        if normalized not in APPROVED_LICENSES:
            raise ComplianceError(f"{coordinate} has unknown or incompatible Debian license")
        normalized_expressions.add(normalized)
    if not normalized_expressions:
        raise ComplianceError(f"{coordinate} has no resolved Debian file license")
    return " AND ".join(
        f"({expression})" if " OR " in expression else expression
        for expression in sorted(normalized_expressions)
    )


def _installed_debian_components(
    snapshot: OciSnapshot,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], set[str]]:
    filesystem = snapshot.files
    status_path = "/var/lib/dpkg/status"
    if status_path not in filesystem:
        raise ComplianceError("pinned base image lacks the dpkg status database")
    installed = [
        record
        for record in _control_records(filesystem[status_path], "dpkg status database")
        if record.get("Status") == "install ok installed"
    ]
    if not installed:
        raise ComplianceError("pinned base image has no installed dpkg packages")
    result: list[dict[str, Any]] = []
    bodies: list[tuple[str, str]] = []
    owned: set[str] = {status_path}
    seen: set[str] = set()
    for record in installed:
        name = _string(record.get("Package"), "installed Debian package name")
        version = _string(record.get("Version"), f"{name} installed version")
        architecture = _string(record.get("Architecture"), f"{name} installed architecture")
        if architecture not in {"amd64", "all"}:
            raise ComplianceError(f"{name} installed architecture is unsupported")
        coordinate = f"deb:{name}:{architecture}=={version}"
        if coordinate in seen:
            raise ComplianceError(f"dpkg status repeats {coordinate}")
        seen.add(coordinate)
        control_prefixes = [name, f"{name}:{architecture}"]
        list_candidates = [
            f"/var/lib/dpkg/info/{prefix}.list"
            for prefix in control_prefixes
            if f"/var/lib/dpkg/info/{prefix}.list" in filesystem
        ]
        if len(list_candidates) != 1:
            raise ComplianceError(f"{coordinate} lacks dpkg ownership evidence")
        list_path = list_candidates[0]
        try:
            listed = [line for line in filesystem[list_path].decode("utf-8").splitlines() if line]
        except UnicodeDecodeError as error:
            raise ComplianceError(f"{coordinate} dpkg file list is invalid") from error
        if len(listed) != len(set(listed)):
            raise ComplianceError(f"{coordinate} dpkg file list repeats a path")
        paths: set[str] = set()
        for path in listed:
            if not path.startswith("/") or PurePosixPath(path).as_posix() != path:
                raise ComplianceError(f"{coordinate} dpkg ownership path is invalid")
            if path not in snapshot.entries:
                raise ComplianceError(f"{coordinate} dpkg ownership contains absent paths")
            paths.add(path)
        license_paths = sorted(
            path for path in paths if PurePosixPath(path).name.lower().startswith(MATERIAL_NAMES)
        )
        if not license_paths:
            raise ComplianceError(f"{coordinate} installed license paths are absent")
        conclusions = {
            _debian_license(
                filesystem[path],
                coordinate,
                (owned_path.lstrip("/") for owned_path in sorted(paths)),
            )
            for path in license_paths
        }
        if len(conclusions) != 1:
            raise ComplianceError(f"{coordinate} Debian license identity mismatch")
        expression = conclusions.pop()
        metadata_paths = [status_path, list_path]
        control_name = PurePosixPath(list_path).name.removesuffix(".list")
        md5_path = f"/var/lib/dpkg/info/{control_name}.md5sums"
        if md5_path in filesystem:
            metadata_paths.append(md5_path)
            seen_md5: set[str] = set()
            try:
                lines = filesystem[md5_path].decode("utf-8").splitlines()
            except UnicodeDecodeError as error:
                raise ComplianceError(f"{coordinate} dpkg md5sums is invalid") from error
            for line in lines:
                try:
                    digest, relative = line.split("  ", 1)
                except ValueError as error:
                    raise ComplianceError(f"{coordinate} dpkg md5sums is malformed") from error
                path = "/" + relative.lstrip("/")
                if path in seen_md5 or path not in paths or path not in filesystem:
                    raise ComplianceError(f"{coordinate} dpkg md5sums path is invalid")
                actual = hashlib.md5(filesystem[path], usedforsecurity=False).hexdigest()
                if len(digest) != 32 or not hmac.compare_digest(digest, actual):
                    raise ComplianceError(f"{coordinate} dpkg md5sums drift for {path}")
                seen_md5.add(path)
        for path in license_paths:
            try:
                bodies.append((path, filesystem[path].decode("utf-8")))
            except UnicodeDecodeError as error:
                raise ComplianceError(f"{coordinate} license evidence is not UTF-8") from error
        result.append(
            {
                "coordinate": coordinate,
                "roles": ["base-image", "final-image"],
                "license_expression": expression,
                "metadata_paths": [
                    {"path": path, "sha256": _sha256(filesystem[path])} for path in metadata_paths
                ],
                "license_paths": [
                    {"path": path, "sha256": _sha256(filesystem[path])} for path in license_paths
                ],
            }
        )
        owned.update(paths)
        owned.update(metadata_paths)
    return sorted(result, key=lambda item: item["coordinate"]), bodies, owned


@dataclass(frozen=True)
class OciEntry:
    kind: str
    mode: int
    uid: int
    gid: int
    data: bytes | None = None
    target: str | None = None

    def record(self, path: str) -> dict[str, object]:
        result: dict[str, object] = {
            "path": "/" + path.lstrip("/"),
            "kind": self.kind,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
        }
        if self.data is not None:
            result["sha256"] = _sha256(self.data)
            result["size"] = len(self.data)
        if self.target is not None:
            result["target"] = self.target
        return result


@dataclass(frozen=True)
class OciSnapshot:
    files: dict[str, bytes]
    entries: dict[str, OciEntry]


def _oci_snapshot(layer_blobs: Iterable[bytes]) -> OciSnapshot:
    filesystem: dict[str, bytes] = {}
    links: dict[str, tuple[str, bool]] = {}
    directories: set[str] = set()
    entries_by_path: dict[str, OciEntry] = {}
    aggregate_compressed = 0
    aggregate_expanded = 0
    aggregate_members = 0
    for data in layer_blobs:
        aggregate_compressed += len(data)
        if aggregate_compressed > MAX_OCI_COMPRESSED_BYTES:
            raise ComplianceError("final image layers exceed aggregate compressed bound")
        entries: dict[str, bytes] = {}
        layer_links: dict[str, tuple[str, bool]] = {}
        layer_directories: set[str] = set()
        layer_metadata: dict[str, OciEntry] = {}
        whiteouts: set[str] = set()
        opaque: set[str] = set()
        seen: set[str] = set()
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r|*") as archive:
                count = 0
                total = 0
                for info in archive:
                    count += 1
                    aggregate_members += 1
                    if count > MAX_ARCHIVE_MEMBERS:
                        raise ComplianceError("final image layer has too many members")
                    if aggregate_members > MAX_OCI_MEMBERS:
                        raise ComplianceError("final image layers exceed aggregate member bound")
                    raw_name = info.name.rstrip("/")
                    if not raw_name:
                        continue
                    name = _archive_member_name(raw_name, "final image layer")
                    if name in seen:
                        raise ComplianceError(f"final image layer repeats member path {name}")
                    seen.add(name)
                    entry_mode = stat.S_IMODE(info.mode)
                    if info.isdir():
                        layer_directories.add(name)
                        layer_metadata[name] = OciEntry("directory", entry_mode, info.uid, info.gid)
                        continue
                    if not (info.isfile() or info.issym() or info.islnk()):
                        raise ComplianceError("final image layer contains a link or special file")
                    basename = PurePosixPath(name).name
                    if basename == ".wh..wh..opq":
                        opaque.add(str(PurePosixPath(name).parent))
                        continue
                    if basename.startswith(".wh."):
                        whiteouts.add(str(PurePosixPath(name).parent / basename[4:]))
                        continue
                    if info.size > MAX_SINGLE_FILE_BYTES:
                        raise ComplianceError("final image layer member is too large")
                    total += info.size
                    aggregate_expanded += info.size
                    if total > MAX_ARCHIVE_BYTES:
                        raise ComplianceError("final image layer expands beyond its bound")
                    if aggregate_expanded > MAX_OCI_EXPANDED_BYTES:
                        raise ComplianceError("final image layers exceed aggregate expanded bound")
                    if info.issym() or info.islnk():
                        layer_links[name] = (info.linkname, info.islnk())
                        layer_metadata[name] = OciEntry(
                            "hardlink" if info.islnk() else "symlink",
                            entry_mode,
                            info.uid,
                            info.gid,
                            target=info.linkname,
                        )
                        continue
                    stream = archive.extractfile(info)
                    if stream is None:
                        raise ComplianceError("final image layer contains unreadable data")
                    value = stream.read()
                    entries[name] = value
                    layer_metadata[name] = OciEntry(
                        "file", entry_mode, info.uid, info.gid, data=value
                    )
        except (tarfile.TarError, OSError) as error:
            raise ComplianceError("final image layer is not a valid tar archive") from error

        def remove_tree(target: str) -> None:
            prefix = target.rstrip("/") + "/"
            for collection in (filesystem, links):
                matching = [
                    item for item in collection if item == target or item.startswith(prefix)
                ]
                for path in matching:
                    collection.pop(path, None)
            for path in [
                item for item in entries_by_path if item == target or item.startswith(prefix)
            ]:
                entries_by_path.pop(path, None)
            directories.difference_update(
                item for item in directories if item == target or item.startswith(prefix)
            )

        for directory in opaque:
            prefix = directory.rstrip("/") + "/" if directory != "." else ""
            for path in [item for item in filesystem if item.startswith(prefix)]:
                filesystem.pop(path)
            for path in [item for item in links if item.startswith(prefix)]:
                links.pop(path)
            directories.difference_update(item for item in directories if item.startswith(prefix))
            for path in [item for item in entries_by_path if item.startswith(prefix)]:
                entries_by_path.pop(path, None)
        for target in whiteouts:
            remove_tree(target)
        incoming = set(entries) | set(layer_links) | layer_directories
        all_links = set(links) | set(layer_links)
        for name in incoming:
            parents = list(PurePosixPath(name).parents)[:-1]
            if any(str(parent) in all_links for parent in parents):
                raise ComplianceError("final image layer traverses a symlinked parent")
        for name in set(entries) | set(layer_links):
            remove_tree(name)
        for name in layer_directories:
            if name in filesystem or name in links:
                remove_tree(name)
            directories.add(name)
        hardlinks = {name for name, (_, hardlink) in layer_links.items() if hardlink}

        def hardlink_bytes(
            name: str,
            visiting: set[str],
            current_links: dict[str, tuple[str, bool]] = layer_links,
            current_entries: dict[str, bytes] = entries,
            current_hardlinks: set[str] = hardlinks,
        ) -> bytes:
            if name in visiting:
                raise ComplianceError("final image contains a cyclic hardlink")
            visiting.add(name)
            target = _link_target(
                name,
                current_links[name][0],
                hardlink=True,
                label="final image",
                allow_absolute=True,
            )
            if target in current_entries:
                value = current_entries[target]
            elif target in filesystem:
                value = filesystem[target]
            elif target in current_hardlinks:
                value = hardlink_bytes(target, visiting)
            else:
                raise ComplianceError("final image contains a dangling hardlink")
            visiting.remove(name)
            return value

        for name in hardlinks:
            entries[name] = hardlink_bytes(name, set())
            metadata = layer_metadata[name]
            layer_metadata[name] = OciEntry(
                metadata.kind,
                metadata.mode,
                metadata.uid,
                metadata.gid,
                data=entries[name],
                target=metadata.target,
            )
            layer_links.pop(name)
        filesystem.update(entries)
        links.update(layer_links)
        entries_by_path.update(layer_metadata)
    for name in set(filesystem) | set(links):
        directories.update(
            str(parent) for parent in PurePosixPath(name).parents if str(parent) not in {".", "/"}
        )

    resolved_links: dict[str, bytes] = {}

    def resolve_link(name: str, visiting: set[str]) -> bytes | None:
        if name in resolved_links:
            return resolved_links[name]
        if name in visiting or name not in links:
            raise ComplianceError("final image contains a cyclic or dangling link")
        visiting.add(name)
        target, hardlink = links[name]
        target_name = _link_target(
            name,
            target,
            hardlink=hardlink,
            label="final image",
            allow_absolute=True,
        )
        if target_name in filesystem:
            value: bytes | None = filesystem[target_name]
        elif target_name in links:
            value = resolve_link(target_name, visiting)
        elif target_name in directories:
            value = None
        else:
            raise ComplianceError("final image contains a cyclic or dangling link")
        visiting.remove(name)
        if value is not None:
            resolved_links[name] = value
        return value

    for name in links:
        resolve_link(name, set())
    for directory in directories:
        entries_by_path.setdefault(directory, OciEntry("directory", 0o755, 0, 0))
    files = {
        "/" + path.lstrip("/"): data for path, data in {**filesystem, **resolved_links}.items()
    }
    return OciSnapshot(
        files, {"/" + path.lstrip("/"): entry for path, entry in entries_by_path.items()}
    )


def _oci_files(layer_blobs: Iterable[bytes]) -> dict[str, bytes]:
    return _oci_snapshot(layer_blobs).files


def _oci_entry_records(snapshot: OciSnapshot) -> list[dict[str, object]]:
    return [snapshot.entries[path].record(path) for path in sorted(snapshot.entries)]


@dataclass(frozen=True)
class BaseImage:
    reference: str
    manifest_digest: str
    config_digest: str
    layers: tuple[str, ...]
    diff_ids: tuple[str, ...]
    snapshot: OciSnapshot
    config: dict[str, Any]


def _rootfs_diff_ids(config: dict[str, Any], label: str) -> tuple[str, ...]:
    rootfs = config.get("rootfs")
    values = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.startswith("sha256:") for value in values
    ):
        raise ComplianceError(f"{label} config lacks exact rootfs diff IDs")
    return tuple(values)


def _layer_diff_id(data: bytes, media_type: object, label: str) -> str:
    media = _string(media_type, f"{label} media type")
    compressed = data.startswith(b"\x1f\x8b")
    declared_gzip = media.endswith("+gzip")
    if compressed != declared_gzip:
        raise ComplianceError(f"{label} compression differs from declared media type")
    if declared_gzip:
        try:
            expanded = gzip.decompress(data)
        except (OSError, EOFError) as error:
            raise ComplianceError(f"{label} gzip payload is invalid") from error
    elif media.endswith(".tar") or media.endswith(".tar+uncompressed"):
        expanded = data
    else:
        raise ComplianceError(f"{label} compression is unsupported")
    if len(expanded) > MAX_ARCHIVE_BYTES:
        raise ComplianceError(f"{label} expands beyond its bound")
    return "sha256:" + _sha256(expanded)


def _base_image(layout: Path, reference: str) -> BaseImage:
    marker = "@sha256:"
    if marker not in reference:
        raise ComplianceError("application base is not digest pinned")
    index_digest = "sha256:" + reference.rsplit(marker, 1)[1]
    index_data = _read_control(layout, "index.json", "base OCI index")
    if "sha256:" + _sha256(index_data) != index_digest:
        raise ComplianceError("retained base OCI index does not match the Dockerfile digest")
    index = _json_bytes(index_data, "base OCI index")
    descriptors = index.get("manifests")
    matches = (
        [
            descriptor
            for descriptor in descriptors
            if isinstance(descriptor, dict)
            and descriptor.get("platform") == {"architecture": "amd64", "os": "linux"}
        ]
        if isinstance(descriptors, list)
        else []
    )
    if len(matches) != 1:
        raise ComplianceError("base OCI index does not select one linux/amd64 manifest")
    manifest_digest = _string(matches[0].get("digest"), "base OCI manifest digest")
    manifest_data = _oci_blob(layout, manifest_digest, "base OCI manifest")
    if matches[0].get("size") != len(manifest_data):
        raise ComplianceError("base OCI manifest descriptor size drift")
    manifest = _json_bytes(manifest_data, "base OCI manifest")
    config_descriptor = manifest.get("config")
    layer_descriptors = manifest.get("layers")
    if not isinstance(config_descriptor, dict) or not isinstance(layer_descriptors, list):
        raise ComplianceError("base OCI manifest shape is invalid")
    config_digest = _string(config_descriptor.get("digest"), "base OCI config digest")
    config_data = _oci_blob(layout, config_digest, "base OCI config")
    if config_descriptor.get("size") != len(config_data):
        raise ComplianceError("base OCI config descriptor size drift")
    config = _json_bytes(config_data, "base OCI config")
    if config.get("architecture") != "amd64" or config.get("os") != "linux":
        raise ComplianceError("base OCI config platform differs from linux/amd64")
    layers: list[str] = []
    blobs: list[bytes] = []
    media_types: list[str] = []
    for descriptor in layer_descriptors:
        if not isinstance(descriptor, dict):
            raise ComplianceError("base OCI layer descriptor is invalid")
        digest = _string(descriptor.get("digest"), "base OCI layer digest")
        data = _oci_blob(layout, digest, "base OCI layer")
        if descriptor.get("size") != len(data):
            raise ComplianceError("base OCI layer descriptor size drift")
        layers.append(digest)
        blobs.append(data)
        media_types.append(_string(descriptor.get("mediaType"), "base OCI layer media type"))
    diff_ids = _rootfs_diff_ids(config, "base OCI")
    actual_diff_ids = tuple(
        _layer_diff_id(data, media, "base OCI layer")
        for data, media in zip(blobs, media_types, strict=True)
    )
    if diff_ids != actual_diff_ids:
        raise ComplianceError("base OCI config diff IDs do not match retained layers")
    return BaseImage(
        reference,
        manifest_digest,
        config_digest,
        tuple(layers),
        diff_ids,
        _oci_snapshot(blobs),
        config,
    )


def _uv_component(image: BaseImage) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    labels = image.config.get("config", {}).get("Labels")
    expected_labels = {
        "org.opencontainers.image.source": "https://github.com/astral-sh/uv",
        "org.opencontainers.image.version": "0.12.3",
        "org.opencontainers.image.licenses": "MIT OR Apache-2.0",
    }
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in expected_labels.items()
    ):
        raise ComplianceError("uv source image identity or license labels differ")
    license_paths = sorted(
        path
        for path in image.snapshot.files
        if PurePosixPath(path).name.lower().startswith(MATERIAL_NAMES)
    )
    if not license_paths:
        raise ComplianceError("uv source image lacks retained license or notice material")
    bodies: list[tuple[str, str]] = []
    for path in license_paths:
        try:
            bodies.append((path, image.snapshot.files[path].decode("utf-8")))
        except UnicodeDecodeError as error:
            raise ComplianceError("uv source image license evidence is not UTF-8") from error
    return (
        {
            "coordinate": "oci:ghcr.io/astral-sh/uv==0.12.3",
            "roles": ["build-tool", "final-image"],
            "license_expression": expected_labels["org.opencontainers.image.licenses"],
            "metadata_paths": [],
            "license_paths": [
                {"path": path, "sha256": _sha256(image.snapshot.files[path])}
                for path in license_paths
            ],
        },
        bodies,
    )


def _base_interpreter_authority(policy: dict[str, Any], base: BaseImage) -> dict[str, str]:
    raw = policy.get("interpreter")
    if not isinstance(raw, dict):
        raise ComplianceError("pinned interpreter policy is absent")
    path = raw.get("path")
    expected = {
        "implementation": "CPython",
        "version": "3.12.13",
        "path": path,
        "site_packages": raw.get("site_packages"),
        "sha256_source": "base-image",
    }
    if raw != expected or not isinstance(path, str):
        raise ComplianceError("pinned interpreter policy is invalid")
    parsed = PurePosixPath(path)
    if not parsed.is_absolute() or ".." in parsed.parts or str(parsed) != path:
        raise ComplianceError("pinned interpreter path is not exact and absolute")
    entry = base.snapshot.entries.get(path)
    data = base.snapshot.files.get(path)
    if entry is None or entry.kind != "file" or data is None or not entry.mode & 0o111:
        raise ComplianceError("pinned interpreter is not a base-owned regular executable")
    return {
        "implementation": "CPython",
        "version": "3.12.13",
        "path": path,
        "resolved_path": path,
        "site_packages": str(raw["site_packages"]),
        "sha256": _sha256(data),
    }


def _validate_base_metadata_changes(base: OciSnapshot, snapshot: OciSnapshot) -> set[str]:
    mutable_paths = {"/etc/passwd", "/etc/group", "/etc/shadow", "/etc/gshadow"}
    changed_base = {
        path
        for path, entry in base.entries.items()
        if snapshot.entries.get(path) != entry and path not in mutable_paths
    }
    if changed_base:
        raise ComplianceError(
            "final image changes pinned base-owned system paths: " + ", ".join(sorted(changed_base))
        )

    additions: dict[str, list[str]] = {}
    for path in mutable_paths:
        before = base.files.get(path)
        after = snapshot.files.get(path)
        if before == after:
            continue
        if before is None or after is None:
            raise ComplianceError("expected user/group metadata path is absent")
        before_entry = base.entries[path]
        after_entry = snapshot.entries[path]
        if (
            after_entry.kind != "file"
            or after_entry.mode != before_entry.mode
            or after_entry.uid != before_entry.uid
            or after_entry.gid != before_entry.gid
        ):
            raise ComplianceError("user/group metadata topology changed")
        try:
            old_lines = before.decode("utf-8").splitlines()
            new_lines = after.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ComplianceError("user/group metadata is not UTF-8") from error
        if len(new_lines) != len(old_lines) + 1 or new_lines[:-1] != old_lines:
            raise ComplianceError("final image user/group metadata change is not additive")
        fields = new_lines[-1].split(":")
        additions[path] = fields
        if not fields or fields[0] != "alphadecay":
            raise ComplianceError("final image adds unexpected user/group metadata")
        if path == "/etc/passwd" and (
            len(fields) != 7 or fields[4] != "" or fields[5:] != ["/app", "/usr/sbin/nologin"]
        ):
            raise ComplianceError("final image alphadecay passwd entry is invalid")
        if path == "/etc/group" and (len(fields) != 4 or fields[3] != ""):
            raise ComplianceError("final image alphadecay group entry is invalid")

    passwd = additions.get("/etc/passwd")
    group = additions.get("/etc/group")
    shadow = additions.get("/etc/shadow")
    gshadow = additions.get("/etc/gshadow")
    if additions and (passwd is None or group is None):
        raise ComplianceError("final image user and group metadata changes are incomplete")
    if (shadow is None) != (gshadow is None):
        raise ComplianceError("final image shadow metadata changes are incomplete")
    if (
        passwd is not None
        and group is not None
        and (
            passwd[1] != "x"
            or group[1] != "x"
            or not passwd[2].isdecimal()
            or not passwd[3].isdecimal()
            or not group[2].isdecimal()
            or passwd[3] != group[2]
            or not 1 <= int(passwd[2]) <= 999
            or not 1 <= int(group[2]) <= 999
        )
    ):
        raise ComplianceError("final image alphadecay user/group identity is invalid")
    if shadow is not None and (
        len(shadow) != 9
        or shadow[1] not in {"!", "*"}
        or any(value and not value.isdecimal() for value in shadow[2:])
    ):
        raise ComplianceError("final image alphadecay shadow entry is invalid")
    if gshadow is not None and (
        len(gshadow) != 4 or gshadow[1] not in {"!", "*"} or gshadow[2:] != ["", ""]
    ):
        raise ComplianceError("final image alphadecay gshadow entry is invalid")
    return mutable_paths


def _application_image_paths(
    export: Path, raw: dict[str, Any], filesystem: dict[str, bytes]
) -> set[str]:
    application_roots = raw.get("application_roots")
    expected_roots = [
        {"source": "backend", "image": "/app/backend"},
        {"source": "fixtures", "image": "/app/fixtures"},
        {"source": "migrations", "image": "/app/migrations"},
        {"source": "dist", "image": "/app/dist"},
    ]
    if application_roots != expected_roots:
        raise ComplianceError("final-image application roots do not match Docker build inputs")

    application_files: set[str] = set()
    for mapping in application_roots:
        source_root = mapping["source"]
        image_root = mapping["image"]
        source_files = _walk_files(export, source_root, symlink_label="symlinked build input")
        root_application_files: set[str] = set()
        for source_path, data in source_files.items():
            relative = PurePosixPath(source_path).relative_to(source_root).as_posix()
            image_path = f"{image_root}/{relative}"
            if filesystem.get(image_path) != data:
                raise ComplianceError(f"application image byte drift: {image_path}")
            root_application_files.add(image_path)
        actual_application = {
            path
            for path in filesystem
            if path == image_root or path.startswith(image_root.rstrip("/") + "/")
        }
        if actual_application != root_application_files:
            raise ComplianceError(
                "UNDECLARED_APP_BYTES: "
                + ", ".join(sorted(actual_application - root_application_files))
            )
        application_files.update(root_application_files)

    application_file_records = raw.get("application_files")
    expected_file_records = [
        {"source": "pyproject.toml", "image": "/app/pyproject.toml"},
        {"source": "uv.lock", "image": "/app/uv.lock"},
    ]
    if application_file_records != expected_file_records:
        raise ComplianceError("final-image application files do not match Docker build inputs")
    for mapping in application_file_records:
        source_data = _regular_file(export, mapping["source"], "Docker build input").read_bytes()
        if filesystem.get(mapping["image"]) != source_data:
            raise ComplianceError(f"application image byte drift: {mapping['image']}")
        application_files.add(mapping["image"])
    return application_files


def _validate_uv_runtime(
    snapshot: OciSnapshot, uv_image: BaseImage, filesystem: dict[str, bytes]
) -> set[str]:
    uv_paths = {"/bin/uv", "/bin/uvx"}
    if uv_paths & set(snapshot.entries) != uv_paths:
        raise ComplianceError("cross-stage uv runtime is incomplete")
    for path in uv_paths:
        entry = snapshot.entries[path]
        if entry.kind not in {"file", "hardlink"} or not entry.mode & 0o111:
            raise ComplianceError("cross-stage uv runtime topology is invalid")
        source_path = "/" + PurePosixPath(path).name
        source_entry = uv_image.snapshot.entries.get(source_path)
        expected_target = None
        actual_target = None
        if source_entry is not None and source_entry.target is not None:
            source_target = _link_target(
                source_path,
                source_entry.target,
                hardlink=source_entry.kind == "hardlink",
                label="uv source image",
                allow_absolute=True,
            )
            expected_target = "bin/" + PurePosixPath(source_target).name
        if entry.target is not None:
            actual_target = _link_target(
                path,
                entry.target,
                hardlink=entry.kind == "hardlink",
                label="final uv runtime",
                allow_absolute=True,
            )
        if (
            source_entry is None
            or source_entry.kind != entry.kind
            or source_entry.mode != entry.mode
            or source_entry.uid != entry.uid
            or source_entry.gid != entry.gid
            or expected_target != actual_target
            or uv_image.snapshot.files.get(source_path) != filesystem.get(path)
        ):
            raise ComplianceError("cross-stage uv runtime provenance differs from source image")
    return uv_paths


def _installed_python_paths(
    installations: list[object],
    expected_packages: set[str],
    packages: list[dict[str, Any]],
    filesystem: dict[str, bytes],
    actual_files: dict[str, str],
    standard_licenses: dict[str, str],
    venv_root: str,
) -> set[str]:
    package_by_coordinate = {item["coordinate"]: item for item in packages}
    installed_coordinates: set[str] = set()
    owned_paths: set[str] = set()
    for installation in installations:
        if not isinstance(installation, dict):
            raise ComplianceError("uv install report Python installation must be an object")
        coordinate = _string(installation.get("coordinate"), "installed Python coordinate")
        package = package_by_coordinate.get(coordinate)
        if (
            coordinate not in expected_packages
            or package is None
            or coordinate in installed_coordinates
        ):
            raise ComplianceError("uv install report Python installation set is invalid")
        if installation.get("artifact_integrity") != package["artifact"]["integrity"]:
            raise ComplianceError(f"{coordinate} installed artifact selection is not exact")
        metadata_path = _string(installation.get("metadata_path"), f"{coordinate} metadata path")
        record_path = _string(installation.get("record_path"), f"{coordinate} RECORD path")
        license_paths = installation.get("license_paths")
        if not isinstance(license_paths, list) or not license_paths:
            raise ComplianceError(f"{coordinate} installed license paths are absent")
        if metadata_path not in filesystem or record_path not in filesystem:
            raise ComplianceError(f"{coordinate} installed metadata or RECORD is absent")
        if (
            _installed_metadata(filesystem[metadata_path], coordinate)
            != package["license_expression"]
        ):
            raise ComplianceError(f"{coordinate} installed license conclusion differs")
        if standard_licenses.get(coordinate) != package["license_expression"]:
            raise ComplianceError(f"{coordinate} SPDX package conclusion differs")
        archive_metadata = next(
            item
            for item in package["archive_members"]
            if item["archive_path"].endswith(("METADATA", "PKG-INFO"))
        )
        if _sha256(filesystem[metadata_path]) != archive_metadata["sha256"]:
            raise ComplianceError(f"{coordinate} installed metadata differs from selected artifact")
        package_owned = _record_owned_paths(
            record_path, filesystem[record_path], filesystem, venv_root
        )
        if not package_owned or any(path not in filesystem for path in package_owned):
            raise ComplianceError(f"{coordinate} RECORD contains absent installed files")
        if metadata_path not in package_owned or record_path not in package_owned:
            raise ComplianceError(f"{coordinate} RECORD omits its installed metadata")
        wheel_record = package.get("wheel_record")
        if not isinstance(wheel_record, list) or not wheel_record:
            raise ComplianceError(f"{coordinate} selected install artifact is not a bound wheel")
        site_root = str(PurePosixPath(record_path).parent.parent)
        wheel_installed_paths: set[str] = set()
        for record in wheel_record:
            installed_path = str(PurePosixPath(site_root) / record["path"])
            if (
                installed_path not in package_owned
                or actual_files.get(installed_path) != record["sha256"]
                or len(filesystem[installed_path]) != record["size"]
            ):
                raise ComplianceError(
                    f"{coordinate} installed bytes differ from the selected wheel RECORD"
                )
            wheel_installed_paths.add(installed_path)
        if "generated_paths" in installation:
            raise ComplianceError(f"{coordinate} uses self-asserted generated install paths")
        console_scripts = package.get("console_scripts")
        if not isinstance(console_scripts, dict):
            raise ComplianceError(f"{coordinate} console-script evidence is invalid")
        generated_paths = {f"{venv_root.rstrip('/')}/bin/{name}" for name in console_scripts}
        for name, target in console_scripts.items():
            path = f"{venv_root.rstrip('/')}/bin/{name}"
            if path not in package_owned or filesystem.get(path) != _console_script_bytes(
                venv_root, target
            ):
                raise ComplianceError(
                    f"{coordinate} generated console script differs from wheel entry points"
                )
        if package_owned != wheel_installed_paths | {record_path} | generated_paths:
            raise ComplianceError(f"{coordinate} installed RECORD ownership is ambiguous")
        archive_license_hashes = {
            item["sha256"]
            for item in package["archive_members"]
            if PurePosixPath(item["archive_path"]).name.lower().startswith(MATERIAL_NAMES)
        }
        for path in license_paths:
            if not isinstance(path, str) or path not in package_owned or path not in filesystem:
                raise ComplianceError(f"{coordinate} installed license ownership is invalid")
            if _sha256(filesystem[path]) not in archive_license_hashes:
                raise ComplianceError(
                    f"{coordinate} installed license differs from selected artifact"
                )
        owned_paths.update(package_owned)
        installed_coordinates.add(coordinate)
    if installed_coordinates != expected_packages:
        raise ComplianceError("uv install report omits installed Python runtime packages")
    return owned_paths


def _image(
    artifact_root: Path,
    export: Path,
    uv_lock_hash: str,
    tool_policy: dict[str, Any],
    raw: dict[str, Any],
    expected_packages: set[str],
    packages: list[dict[str, Any]],
    base_reference: str,
    uv_reference: str,
) -> tuple[str, list[str], dict[str, str], list[dict[str, Any]], list[tuple[str, str]]]:
    if raw.get("schema_version") != 4:
        raise ComplianceError("final-image inventory schema_version must be 4")
    layout_relative = _relative(raw.get("oci_layout"), "OCI layout path")
    layout = artifact_root / layout_relative
    _read_control_json(layout, "oci-layout", "OCI layout marker")
    index = _read_control_json(layout, "index.json", "OCI index")
    descriptors = index.get("manifests")
    if not isinstance(descriptors, list):
        raise ComplianceError("OCI index manifests must be a list")
    manifest_digest = _string(raw.get("manifest_digest"), "final image manifest digest")
    matches = [
        item
        for item in descriptors
        if isinstance(item, dict) and item.get("digest") == manifest_digest
    ]
    if len(matches) != 1:
        raise ComplianceError("final image manifest is not uniquely bound by OCI index")
    manifest_data = _oci_blob(layout, manifest_digest, "OCI manifest")
    if matches[0].get("size") != len(manifest_data):
        raise ComplianceError("OCI index manifest size drift")
    manifest = _json_bytes(manifest_data, "OCI manifest")
    config = manifest.get("config")
    if not isinstance(config, dict):
        raise ComplianceError("OCI manifest config must be an object")
    config_data = _oci_blob(layout, config.get("digest"), "OCI config")
    if config.get("size") != len(config_data):
        raise ComplianceError("OCI config descriptor size drift")
    image_config = _json_bytes(config_data, "OCI config")
    if image_config.get("architecture") != "amd64" or image_config.get("os") != "linux":
        raise ComplianceError("OCI platform does not match the pinned slim image")
    layer_descriptors = manifest.get("layers")
    if not isinstance(layer_descriptors, list):
        raise ComplianceError("OCI manifest layers must be a list")
    if len(layer_descriptors) > MAX_OCI_LAYERS:
        raise ComplianceError("final image exceeds aggregate layer-count bound")
    descriptor_sizes = [
        item.get("size") if isinstance(item, dict) else None for item in layer_descriptors
    ]
    if (
        any(not isinstance(size, int) or size < 0 for size in descriptor_sizes)
        or sum(descriptor_sizes) > MAX_OCI_COMPRESSED_BYTES
    ):
        raise ComplianceError("final image layer descriptors exceed aggregate compressed bound")
    layers = [
        _string(item.get("digest") if isinstance(item, dict) else None, "OCI layer digest")
        for item in layer_descriptors
    ]
    recorded_layers = raw.get("layers")
    if not isinstance(recorded_layers, list) or recorded_layers != layers:
        raise ComplianceError("final-image layer inventory drift")
    layer_blobs: list[bytes] = []
    for descriptor, digest in zip(layer_descriptors, layers, strict=True):
        data = _oci_blob(layout, digest, "OCI layer")
        if descriptor.get("size") != len(data):
            raise ComplianceError("OCI layer descriptor size drift")
        layer_blobs.append(data)
    final_diff_ids = _rootfs_diff_ids(image_config, "final OCI")
    if final_diff_ids != tuple(
        _layer_diff_id(data, descriptor.get("mediaType"), "final OCI layer")
        for data, descriptor in zip(layer_blobs, layer_descriptors, strict=True)
    ):
        raise ComplianceError("final OCI config diff IDs do not match retained layers")
    base_layout_relative = _relative(raw.get("base_oci_layout"), "base OCI layout path")
    base_layout = artifact_root / base_layout_relative
    _read_control_json(base_layout, "oci-layout", "base OCI layout marker")
    base = _base_image(base_layout, base_reference)
    if (
        tuple(layers[: len(base.layers)]) != base.layers
        or final_diff_ids[: len(base.diff_ids)] != base.diff_ids
    ):
        raise ComplianceError(
            "final OCI base layer and diff-ID prefix differs from the pinned base"
        )
    expected_base = {
        "reference": base.reference,
        "manifest_digest": base.manifest_digest,
        "config_digest": base.config_digest,
        "layers": list(base.layers),
        "diff_ids": list(base.diff_ids),
    }
    if raw.get("base_image") != expected_base:
        raise ComplianceError("final-image base authority drift")
    interpreter = _base_interpreter_authority(tool_policy, base)
    if raw.get("interpreter") != interpreter:
        raise ComplianceError("final-image interpreter authority drift")
    uv_layout_relative = _relative(raw.get("uv_oci_layout"), "uv OCI layout path")
    uv_layout = artifact_root / uv_layout_relative
    _read_control_json(uv_layout, "oci-layout", "uv OCI layout marker")
    uv_image = _base_image(uv_layout, uv_reference)
    expected_uv = {
        "reference": uv_image.reference,
        "manifest_digest": uv_image.manifest_digest,
        "config_digest": uv_image.config_digest,
        "layers": list(uv_image.layers),
        "diff_ids": list(uv_image.diff_ids),
    }
    if raw.get("uv_image") != expected_uv:
        raise ComplianceError("final-image uv source authority drift")
    uv_component, uv_bodies = _uv_component(uv_image)
    snapshot = _oci_snapshot(layer_blobs)
    mutable_base_paths = _validate_base_metadata_changes(base.snapshot, snapshot)
    filesystem = snapshot.files
    recorded_files = _inventory_files(raw.get("files"), "final-image")
    actual_files = {path: _sha256(data) for path, data in filesystem.items()}
    if recorded_files != actual_files:
        raise ComplianceError("final-image filesystem inventory drift")
    if raw.get("entries") != _oci_entry_records(snapshot):
        raise ComplianceError("final-image topology inventory drift")
    sbom_ref = raw.get("external_sbom")
    if not isinstance(sbom_ref, dict):
        raise ComplianceError("final-image requires an external tool-provenanced SBOM")
    attestation_ref = raw.get("sbom_attestation")
    attestation = _json_bytes(
        _external_file(artifact_root, attestation_ref, "SBOM attestation"),
        "SBOM attestation",
    )
    if attestation.get("schema_version") != 3:
        raise ComplianceError("SBOM attestation schema is unsupported")
    tool = attestation.get("tool")
    if not isinstance(tool, dict):
        raise ComplianceError("external SBOM tool provenance is absent")
    expected_tool = {key: tool_policy.get(key) for key in ("name", "version", "path", "sha256")}
    if tool != expected_tool:
        raise ComplianceError("external SBOM tool differs from the pinned tool policy")
    tool_path = _relative(tool.get("path"), "external SBOM tool path")
    materialized_tool = _regular_file(artifact_root, tool_path, "external SBOM tool", control=True)
    if stat.S_IMODE(materialized_tool.stat().st_mode) != 0o755:
        raise ComplianceError("external SBOM tool is not mode 0755")
    tool_data = _bounded_bytes(materialized_tool, "external SBOM tool")
    if _string(tool.get("sha256"), "external SBOM tool hash") != _sha256(
        tool_data
    ) or tool_data != _read_control(
        export, "ops/release/generate_release_evidence.py", "tracked SBOM tool"
    ):
        raise ComplianceError("external SBOM tool differs from tracked executable bytes")
    sbom_data = _external_file(artifact_root, sbom_ref, "external SBOM")
    template = tool_policy.get("invocation_template")
    if (
        not isinstance(template, list)
        or any(not isinstance(item, str) for item in template)
        or template[:3] != [interpreter["path"], "-I", "-S"]
    ):
        raise ComplianceError("SBOM tool invocation policy is invalid")
    packaging_authority = _packaging_authority()
    packaging_binding = _sha256(
        json.dumps(packaging_authority, separators=(",", ":"), sort_keys=True).encode()
    )
    expected_invocation = [item.replace("{manifest_digest}", manifest_digest) for item in template]
    expected_invocation.extend(("--packaging-authority-sha256", packaging_binding))
    module_paths = [
        "ops/release/generate_release_evidence.py",
        "ops/release/license_provenance.py",
    ]
    if tool_policy.get("modules") != module_paths:
        raise ComplianceError("SBOM tool policy omits a required tracked module")
    expected_modules = []
    for path in module_paths:
        data = _read_control(export, _relative(path, "tracked module path"), "tracked module")
        expected_modules.append({"path": path, "sha256": _sha256(data)})
    context_records: list[dict[str, str]] = []
    for path in (
        ".dockerignore",
        "Dockerfile",
        "index.html",
        "package-lock.json",
        "package.json",
        "pyproject.toml",
        "tsconfig.json",
        "uv.lock",
        "vitest.config.ts",
    ):
        data = _read_control(export, path, f"context {path}")
        context_records.append({"path": path, "sha256": _sha256(data)})
    for root in ("backend", "fixtures", "frontend", "migrations", "third_party"):
        for path, data in sorted(
            _walk_files(export, root, symlink_label="context symlink").items()
        ):
            context_records.append({"path": path, "sha256": _sha256(data)})
    context_digest = _sha256(
        json.dumps(context_records, separators=(",", ":"), sort_keys=True).encode()
    )
    expected_base_attestation = {
        "reference": base.reference,
        "manifest_digest": base.manifest_digest,
        "config_digest": base.config_digest,
        "layers": list(base.layers),
        "diff_ids": list(base.diff_ids),
    }
    expected_inputs: list[dict[str, str]] = []
    for root, path in (
        (export, "Dockerfile"),
        (export, "uv.lock"),
        (export, "package-lock.json"),
        (export, REGISTRY_PATH),
        (export, BUNDLE_PATH),
        (artifact_root, f"{layout_relative}/blobs/sha256/{manifest_digest[7:]}"),
        (artifact_root, f"{base_layout_relative}/index.json"),
        (artifact_root, f"{uv_layout_relative}/index.json"),
    ):
        data = _bounded_bytes(_regular_file(root, path, f"attested input {path}"), path)
        expected_inputs.append({"path": path, "sha256": _sha256(data)})
    expected_inputs.sort(key=lambda item: item["path"])
    uv_ref = raw.get("uv_install_report")
    if not isinstance(uv_ref, dict):
        raise ComplianceError("final-image uv install report reference is absent")
    expected_outputs = []
    for reference in (
        sbom_ref,
        uv_ref,
        {"path": "reports/final-image-files.json"},
    ):
        path = _relative(reference.get("path"), "attested output path")
        data = _bounded_bytes(_regular_file(artifact_root, path, f"attested output {path}"), path)
        expected_outputs.append({"path": path, "sha256": _sha256(data)})
    if (
        attestation.get("format") != tool_policy.get("format")
        or attestation.get("input_manifest_digest") != manifest_digest
        or attestation.get("invocation") != expected_invocation
        or attestation.get("python") != interpreter
        or attestation.get("packaging") != packaging_authority
        or attestation.get("modules") != expected_modules
        or attestation.get("dockerfile_sha256")
        != _sha256(_read_control(export, "Dockerfile", "Dockerfile"))
        or attestation.get("context") != {"sha256": context_digest, "files": context_records}
        or attestation.get("base_image") != expected_base_attestation
        or attestation.get("uv_image") != expected_uv
        or attestation.get("inputs") != expected_inputs
        or attestation.get("outputs") != expected_outputs
    ):
        raise ComplianceError("SBOM attestation does not bind tool, invocation, input, and output")
    sbom = _json_bytes(sbom_data, "external SBOM")
    if (
        sbom.get("spdxVersion") != "SPDX-2.3"
        or sbom.get("dataLicense") != "CC0-1.0"
        or sbom.get("SPDXID") != "SPDXRef-DOCUMENT"
        or sbom.get("name") != "alphadecay-final-image"
        or sbom.get("documentNamespace")
        != "https://alphadecay.dev/spdx/final-image/" + manifest_digest.removeprefix("sha256:")
    ):
        raise ComplianceError("external SBOM is not the pinned SPDX document shape")
    creation = sbom.get("creationInfo")
    expected_creator = f"Tool: {tool_policy.get('name')}-{tool_policy.get('version')}"
    if not isinstance(creation, dict) or creation.get("creators") != [expected_creator]:
        raise ComplianceError("external SBOM creation authority is invalid")
    extension = sbom.get("alphadecay")
    if (
        not isinstance(extension, dict)
        or extension.get("schema_version") != 3
        or extension.get("subject_manifest_digest") != manifest_digest
    ):
        raise ComplianceError("external SBOM does not bind the exact OCI manifest")
    spdx_packages = sbom.get("packages")
    if not isinstance(spdx_packages, list) or any(
        not isinstance(item, dict) for item in spdx_packages
    ):
        raise ComplianceError("external SBOM packages are invalid")
    standard_licenses: dict[str, str] = {}
    standard_ids: set[str] = set()
    for item in spdx_packages:
        coordinate = _string(item.get("name"), "SPDX package name")
        package_id = _string(item.get("SPDXID"), "SPDX package identifier")
        expression = _string(item.get("licenseConcluded"), "SPDX package license")
        if (
            coordinate in standard_licenses
            or package_id in standard_ids
            or item.get("licenseDeclared") != expression
            or item.get("filesAnalyzed") is not False
        ):
            raise ComplianceError("external SBOM package identity is ambiguous")
        standard_licenses[coordinate] = expression
        standard_ids.add(package_id)
    if set(sbom.get("documentDescribes", [])) != standard_ids:
        raise ComplianceError("external SBOM document scope is invalid")
    sbom_packages = extension.get("packages")
    sbom_components = extension.get("components", [])
    uv_report = _json_bytes(
        _external_file(artifact_root, raw.get("uv_install_report"), "uv install report"),
        "uv install report",
    )
    if (
        uv_report.get("schema_version") != 1
        or uv_report.get("lock_sha256") != uv_lock_hash
        or uv_report.get("venv_root") != "/app/.venv"
    ):
        raise ComplianceError("uv install report does not bind the locked target venv")
    installations = uv_report.get("packages")
    recorded_packages = raw.get("packages")
    if not isinstance(sbom_packages, list) or any(
        not isinstance(item, str) for item in sbom_packages
    ):
        raise ComplianceError("final-image trusted SBOM packages must be strings")
    if not isinstance(sbom_components, list):
        raise ComplianceError("final-image trusted SBOM components must be a list")
    if not isinstance(installations, list):
        raise ComplianceError("uv install report lacks Python installation evidence")
    owned_paths: set[str] = {interpreter["path"]}
    owned_paths.update(
        _installed_python_paths(
            installations,
            expected_packages,
            packages,
            filesystem,
            actual_files,
            standard_licenses,
            uv_report["venv_root"],
        )
    )
    debian_components, component_bodies, debian_owned_paths = _installed_debian_components(
        base.snapshot
    )
    components = [*debian_components, uv_component]
    component_bodies.extend(uv_bodies)
    if sbom_components != components:
        raise ComplianceError("final-image OS components differ from the pinned base filesystem")
    component_coordinates = {item["coordinate"] for item in components}
    if any(
        standard_licenses.get(item["coordinate"]) != item["license_expression"]
        for item in components
    ):
        raise ComplianceError("final-image SPDX component conclusion differs")
    owned_paths.update(debian_owned_paths)
    expected_inventory = expected_packages | component_coordinates
    if (
        recorded_packages != sbom_packages + sorted(component_coordinates)
        or set(recorded_packages) != expected_inventory
        or set(standard_licenses) != expected_inventory
    ):
        raise ComplianceError("final-image package inventory drift")
    retained = {
        f"/app/{member['path']}": member["sha256"]
        for package in packages
        for member in package["archive_members"]
        if PurePosixPath(member["archive_path"]).name.lower().startswith(MATERIAL_NAMES)
    }
    for path, digest in retained.items():
        if actual_files.get(path) != digest:
            raise ComplianceError(f"final-image retained archive evidence drift: {path}")
    owned_paths.update(retained)
    application_files = _application_image_paths(export, raw, filesystem)
    owned_paths.update(application_files)
    undeclared_app = {
        path
        for path in filesystem
        if path.startswith("/app/") and path not in application_files and path not in owned_paths
    }
    if undeclared_app:
        raise ComplianceError("UNDECLARED_APP_BYTES: " + ", ".join(sorted(undeclared_app)))
    uv_paths = _validate_uv_runtime(snapshot, uv_image, filesystem)
    owned_paths.update(uv_paths)
    owned_paths.update(
        path
        for path in ("/etc/passwd", "/etc/group", "/etc/shadow", "/etc/gshadow")
        if path in filesystem
    )
    unowned = set(filesystem) - owned_paths
    if unowned:
        raise ComplianceError("UNDECLARED_RUNTIME_BYTES: " + ", ".join(sorted(unowned)))
    allowed_added_entries = (
        uv_paths
        | {"/bin"}
        | mutable_base_paths
        | {path for path in snapshot.entries if path == "/app" or path.startswith("/app/")}
    )
    unowned_entries = set(snapshot.entries) - set(base.snapshot.entries) - allowed_added_entries
    if unowned_entries:
        raise ComplianceError("UNDECLARED_RUNTIME_TOPOLOGY: " + ", ".join(sorted(unowned_entries)))
    return manifest_digest, layers, actual_files, components, component_bodies


def _release_files(export: Path) -> list[dict[str, str]]:
    files = _walk_files(export, "", symlink_label="symlinked release path")
    return [
        {"path": path, "sha256": _sha256(data)}
        for path, data in sorted(files.items())
        if path not in GENERATED_PATHS
    ]


def _notice(
    export: Path,
    packages: list[dict[str, Any]],
    components: list[dict[str, Any]],
    component_bodies: list[tuple[str, str]],
    assets: list[dict[str, Any]],
) -> str:
    lines = [
        "# Third-party notices",
        "",
        "Generated from retained package, image, and reviewed static-asset evidence.",
        "",
    ]
    for package in packages:
        heading = package["coordinate"]
        if "lock_path" in package:
            heading += f" ({package['lock_path']})"
        lines.extend([f"## {heading}", "", f"SPDX: `{package['license_expression']}`", ""])
        for member in package["archive_members"]:
            if not PurePosixPath(member["archive_path"]).name.lower().startswith(MATERIAL_NAMES):
                continue
            body = _regular_file(export, member["path"], "retained notice body").read_text(
                encoding="utf-8"
            )
            lines.extend(
                [
                    f"Source member: `{member['archive_path']}`",
                    "",
                    "```text",
                    body.rstrip(),
                    "```",
                    "",
                ]
            )
        for member in package["supplemental_license_members"]:
            body = _regular_file(
                export,
                member["path"],
                "supplemental retained notice body",
            ).read_text(encoding="utf-8")
            if member["disposition"] == "artifact-expression-plus-spdx-terms":
                lines.extend(
                    [
                        "The exact package archive declares this SPDX license but does not "
                        "include a license body or package-specific copyright notice. The "
                        "canonical SPDX terms are retained below.",
                        "",
                    ]
                )
            lines.extend(
                [
                    f"Upstream source: `{member['url']}`",
                    "",
                    "```text",
                    body.rstrip(),
                    "```",
                    "",
                ]
            )
    ofl_assets = [asset for asset in assets if asset["license_expression"] == OFL_LICENSE]
    by_manifest: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for asset in ofl_assets:
        by_manifest[asset["ofl_evidence"]["provenance_path"]].append(asset)
    emitted_licenses: set[tuple[str, str]] = set()
    for provenance_path, fonts in sorted(by_manifest.items()):
        fonts.sort(key=lambda item: item["path"])
        evidence = fonts[0]["ofl_evidence"]
        lines.extend(
            [
                f"## OFL font assets ({provenance_path})",
                "",
                f"SPDX: `{OFL_LICENSE}`",
                "",
                f"Upstream repository: `{evidence['repository_url']}`",
                "",
                f"Pinned revision: `{evidence['source_revision']}`",
                "",
                f"Provenance manifest SHA-256: `{evidence['provenance_sha256']}`",
                "",
            ]
        )
        for font in fonts:
            source = font["ofl_evidence"]
            lines.extend(
                [
                    f"- `{font['path']}` (`{font['sha256']}`)",
                    f"  - Upstream path: `{source['upstream_path']}`",
                    f"  - Immutable source: `{source['source_url']}`",
                ]
            )
        lines.append("")
        license_key = (evidence["license_path"], evidence["license_sha256"])
        if license_key in emitted_licenses:
            lines.extend(
                [
                    f"Retained OFL terms: `{evidence['license_path']}` "
                    f"(`{evidence['license_sha256']}`; listed above).",
                    "",
                ]
            )
            continue
        emitted_licenses.add(license_key)
        license_data = _bounded_bytes(
            _regular_file(export, evidence["license_path"], "retained OFL license"),
            "retained OFL license",
            limit=1024 * 1024,
        )
        if _sha256(license_data) != evidence["license_sha256"]:
            raise ComplianceError("retained OFL license changed before notice generation")
        license_body = license_data.decode("utf-8")
        lines.extend(
            [
                f"License source: `{evidence['license_source_url']}`",
                "",
                f"Retained path: `{evidence['license_path']}`",
                "",
                "```text",
                license_body.rstrip(),
                "```",
                "",
            ]
        )
    bodies = dict(component_bodies)
    for component in components:
        lines.extend(
            [
                f"## {component['coordinate']}",
                "",
                f"SPDX: `{component['license_expression']}`",
                "",
            ]
        )
        for evidence in component["license_paths"]:
            lines.extend(
                [
                    f"Image evidence: `{evidence['path']}`",
                    "",
                    "```text",
                    bodies[evidence["path"]].rstrip(),
                    "```",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def verify_and_generate(source: Path, export: Path, artifact_root: Path | None = None) -> Result:
    source = _checked_root(source, "source root")
    export = _checked_root(export, "export root")
    artifact_root = _checked_root(
        artifact_root if artifact_root is not None else export.parent / "artifacts",
        "artifact root",
    )
    registry = _read_control_json(export, REGISTRY_PATH, REGISTRY_PATH)
    bundle_raw = _read_control_json(export, BUNDLE_PATH, BUNDLE_PATH)
    image_raw = _read_control_json(export, IMAGE_PATH, IMAGE_PATH)
    if registry.get("schema_version") != 3:
        raise ComplianceError("dependency evidence schema_version must be 3")
    if registry.get("target_environment") != TARGET_ENVIRONMENT:
        raise ComplianceError("dependency evidence target environment differs")
    lock_hashes = registry.get("lockfiles")
    if not isinstance(lock_hashes, dict):
        raise ComplianceError("dependency evidence lockfiles must be an object")
    controls: dict[str, bytes] = {}
    for relative in (
        "uv.lock",
        "package-lock.json",
        "Dockerfile",
        "ops/release/sbom-tool.json",
    ):
        source_data = _read_control(source, relative, f"source {relative}")
        export_data = _read_control(export, relative, f"export {relative}")
        if source_data != export_data:
            raise ComplianceError(f"staged public export {relative} differs from source")
        controls[relative] = export_data
    source_supplemental = _read_control(
        source,
        SUPPLEMENTAL_LICENSE_PATH,
        "source supplemental license registry",
    )
    export_supplemental = _read_control(
        export,
        SUPPLEMENTAL_LICENSE_PATH,
        "export supplemental license registry",
    )
    if source_supplemental != export_supplemental:
        raise ComplianceError("staged supplemental license registry differs from source")
    if _supplemental_licenses(source) != _supplemental_licenses(export):
        raise ComplianceError("staged supplemental license material differs from source")
    docker = _docker_authority(controls["Dockerfile"])
    tool_policy = _json_bytes(controls["ops/release/sbom-tool.json"], "pinned SBOM tool policy")
    expected_copies = (
        "--from=uv /uv /uvx /bin/",
        "pyproject.toml uv.lock ./",
        "backend ./backend",
        "third_party ./third_party",
        "fixtures ./fixtures",
        "migrations ./migrations",
        "--from=frontend /app/dist ./dist",
    )
    if docker.application_base != tool_policy.get("base_image"):
        raise ComplianceError("Dockerfile application stage does not use the pinned base")
    if docker.uv_base != tool_policy.get("uv_image"):
        raise ComplianceError("Dockerfile uv stage does not use the pinned source image")
    if docker.application_copies != expected_copies:
        raise ComplianceError("Dockerfile application COPY authority differs")
    if "uv sync --frozen --no-dev --no-editable" not in docker.application_runs:
        raise ComplianceError("Dockerfile production dependency target is not locked")
    for relative in ("uv.lock", "package-lock.json"):
        if lock_hashes.get(relative) != _sha256(controls[relative]):
            raise ComplianceError(f"stale lock hash for {relative}")

    python, python_root, python_dev = _load_python_lock(controls["uv.lock"])
    python_runtime = _python_closure(python, python[python_root].dependencies, "Python runtime")
    python_build = _python_closure(python, python_dev, "Python build-only") - python_runtime
    mcp_direct = tuple(
        dep for dep in python[python_root].dependencies if dep.name == "alpaca-mcp-server"
    )
    if len(mcp_direct) != 1:
        raise ComplianceError("locked application must directly select alpaca-mcp-server")
    mcp_runtime = _python_closure(python, mcp_direct, "MCP runtime")
    node, frontend_runtime, node_build = _load_node_lock(controls["package-lock.json"])
    roles: dict[str, set[str]] = defaultdict(set)
    for role, coordinates in (
        ("python-runtime", python_runtime),
        ("mcp-runtime", mcp_runtime),
        ("frontend-runtime", frontend_runtime),
        ("build-only", python_build | node_build),
    ):
        for coordinate in coordinates:
            roles[coordinate].add(role)
    locked = {
        package.coordinate: package.artifacts
        for package in python.values()
        if package.coordinate in roles
    }
    for package in node.values():
        if package.identity in roles:
            locked[package.identity] = frozenset({package.artifact})
    packages = _package_evidence(export, artifact_root, registry.get("packages"), locked, roles)
    components = registry.get("components")
    if components != []:
        raise ComplianceError(
            "final-image components must be derived from the OCI filesystem and trusted SBOM"
        )
    assets = _assets(export, registry.get("assets"))
    _bundle(export, bundle_raw, frontend_runtime)
    manifest_digest, layers, image_files, image_components, component_bodies = _image(
        artifact_root,
        export,
        lock_hashes["uv.lock"],
        tool_policy,
        image_raw,
        python_runtime,
        packages,
        docker.application_base,
        docker.uv_base,
    )
    notice = _notice(export, packages, image_components, component_bodies, assets)
    provenance = {
        "schema_version": 3,
        "lockfiles": {key: lock_hashes[key] for key in sorted(lock_hashes)},
        "inventories": {
            role: sorted(coordinate for coordinate, assigned in roles.items() if role in assigned)
            for role in ("python-runtime", "mcp-runtime", "frontend-runtime", "build-only")
        },
        "packages": packages,
        "components": image_components,
        "assets": assets,
        "release_files": _release_files(export),
        "frontend_bundle_inventory_sha256": _sha256(
            _read_control(export, BUNDLE_PATH, BUNDLE_PATH)
        ),
        "final_image_inventory_sha256": _sha256(_read_control(export, IMAGE_PATH, IMAGE_PATH)),
        "oci_manifest_digest": manifest_digest,
        "oci_layers": layers,
        "oci_files": [
            {"path": path, "sha256": digest} for path, digest in sorted(image_files.items())
        ],
        "third_party_notices_sha256": _sha256(notice.encode()),
    }
    provenance_text = json.dumps(provenance, indent=2, sort_keys=True) + "\n"
    notice_path = export / NOTICE_PATH
    if (
        notice_path.exists()
        and _regular_file(export, NOTICE_PATH, NOTICE_PATH).read_text(encoding="utf-8") != notice
    ):
        raise ComplianceError("THIRD_PARTY_NOTICES.md is stale")
    provenance_path = export / PROVENANCE_PATH
    if (
        provenance_path.exists()
        and _regular_file(export, PROVENANCE_PATH, PROVENANCE_PATH).read_text(encoding="utf-8")
        != provenance_text
    ):
        raise ComplianceError("release-provenance.json is stale")
    notice_path.write_text(notice, encoding="utf-8")
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(provenance_text, encoding="utf-8")
    return Result(
        len(python_runtime),
        len(mcp_runtime),
        len(frontend_runtime),
        len(python_build | node_build),
        len(assets),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        result = verify_and_generate(args.source, args.export, args.artifacts)
    except (ComplianceError, OSError, UnicodeDecodeError) as error:
        print(f"FAIL  {error}", file=sys.stderr)
        return 1
    print(
        "PASS  artifact-bound release compliance "
        f"({result.python_runtime} Python runtime, {result.mcp_runtime} MCP, "
        f"{result.frontend_runtime} frontend runtime, {result.build_only} build-only, "
        f"{result.assets} assets)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
