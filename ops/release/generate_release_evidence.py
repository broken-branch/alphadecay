#!/usr/local/bin/python3.12

"""Generate deterministic OCI, uv-install, SBOM, and invocation evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import types
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from packaging.utils import canonicalize_name

    from ops.release.license_provenance import (
        OciSnapshot,
        _archive_conclusion,
        _archive_members,
        _base_image,
        _bounded_bytes,
        _docker_authority,
        _installed_debian_components,
        _json_bytes,
        _materialize_ofl_asset_evidence,
        _oci_blob,
        _oci_entry_records,
        _oci_snapshot,
        _packaging_authority,
        _regular_file,
        _relative,
        _uv_component,
        _walk_files,
        verify_and_generate,
    )

_LICENSE_PROVENANCE_SHA256 = "3952f5078f17795ae43e0b498c106d01204be1d8d3a94093c0b35e0808d62f11"
_AUTHORITY_NAMES = (
    "OciSnapshot",
    "_archive_conclusion",
    "_archive_members",
    "_base_image",
    "_bounded_bytes",
    "_docker_authority",
    "_installed_debian_components",
    "_json_bytes",
    "_materialize_ofl_asset_evidence",
    "_oci_blob",
    "_oci_entry_records",
    "_oci_snapshot",
    "_packaging_authority",
    "_regular_file",
    "_relative",
    "_uv_component",
    "_walk_files",
    "verify_and_generate",
)


def _trusted_import_paths(policy: dict[str, Any]) -> list[str]:
    interpreter_root = Path(sys.base_prefix).resolve()
    raw_interpreter = policy.get("interpreter")
    site_value = raw_interpreter.get("site_packages") if isinstance(raw_interpreter, dict) else None
    if not isinstance(site_value, str):
        raise ValueError("release evidence site-packages policy is absent")
    policy_site = Path(site_value)
    if not policy_site.is_absolute() or policy_site.resolve(strict=True) != policy_site:
        raise ValueError("release evidence site-packages policy is not exact and absolute")
    expected_suffix = Path("lib/python3.12/site-packages").parts
    if policy_site.parts[-len(expected_suffix) :] != expected_suffix:
        raise ValueError("release evidence site-packages policy has an unexpected layout")
    environment_prefix = policy_site.parents[2]
    sys.prefix = str(environment_prefix)
    sys.exec_prefix = str(environment_prefix)
    trusted_paths: list[str] = []
    for entry in sys.path:
        if entry:
            resolved = Path(entry).resolve()
            if resolved.is_relative_to(interpreter_root):
                trusted_paths.append(str(resolved))
    if str(policy_site) not in trusted_paths:
        trusted_paths.append(str(policy_site))
    return trusted_paths


def _tracked_module_bytes(source: Path) -> tuple[Path, bytes]:
    module_path = (source / "ops/release/license_provenance.py").resolve(strict=True)
    expected_path = (source.resolve(strict=True) / "ops/release/license_provenance.py").resolve(
        strict=True
    )
    if module_path != expected_path:
        raise ValueError("release provenance module origin differs from tracked source")
    descriptor = os.open(module_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4 * 1024 * 1024:
            raise ValueError("release provenance module is not a bounded regular file")
        data = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    if len(data) != metadata.st_size or _sha256(data) != _LICENSE_PROVENANCE_SHA256:
        raise ValueError("release provenance module differs from tracked verified bytes")
    return module_path, data


def _activate_tracked_authority(module_path: Path, data: bytes) -> None:
    module_name = "ops.release.license_provenance"
    module = types.ModuleType(module_name)
    module.__file__ = str(module_path)
    module.__package__ = "ops.release"
    sys.modules[module_name] = module
    exec(compile(data, str(module_path), "exec"), module.__dict__)
    if Path(module.__file__).resolve(strict=True) != module_path:
        raise ValueError("active release provenance module origin differs")
    authority = {name: getattr(module, name, None) for name in _AUTHORITY_NAMES}
    if any(value is None for value in authority.values()):
        raise ValueError("release provenance module authority is incomplete")
    for name, value in authority.items():
        if callable(value) and getattr(value, "__module__", None) != module_name:
            raise ValueError(f"release provenance callable {name} has foreign authority")
    globals().update(authority)
    from packaging.utils import canonicalize_name as active_canonicalize_name

    globals()["canonicalize_name"] = active_canonicalize_name


def _load_tracked_authority(source: Path, policy: dict[str, Any]) -> None:
    sys.path[:] = _trusted_import_paths(policy)
    module_path, data = _tracked_module_bytes(source)
    _activate_tracked_authority(module_path, data)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(root: Path, relative: str, label: str) -> dict[str, Any]:
    return _json_bytes(
        _bounded_bytes(_regular_file(root, relative, label, control=True), label), label
    )


def _bootstrap_policy(source: Path) -> dict[str, Any]:
    path = source / "ops/release/sbom-tool.json"
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
            raise ValueError("release evidence bootstrap policy is not a bounded regular file")
        data = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    if len(data) != metadata.st_size:
        raise ValueError("release evidence bootstrap policy changed while reading")

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("release evidence bootstrap policy repeats a key")
            result[key] = value
        return result

    value = json.loads(data, object_pairs_hook=object_pairs)
    if not isinstance(value, dict):
        raise ValueError("release evidence bootstrap policy must be an object")
    return value


def _write_json(path: Path, value: object) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def _interpreter_authority(policy: dict[str, Any], base: OciSnapshot) -> dict[str, str]:
    raw = policy.get("interpreter")
    if not isinstance(raw, dict):
        raise ValueError("release evidence interpreter policy is absent")
    path_value = raw.get("path")
    expected_policy = {
        "implementation": "CPython",
        "version": "3.12.13",
        "path": path_value,
        "site_packages": raw.get("site_packages"),
        "sha256_source": "base-image",
    }
    if raw != expected_policy or not isinstance(path_value, str):
        raise ValueError("release evidence interpreter policy is invalid")
    interpreter = Path(path_value)
    if not interpreter.is_absolute() or sys.executable != path_value:
        raise ValueError("release evidence interpreter path differs from policy")
    metadata = interpreter.lstat()
    resolved = interpreter.resolve(strict=True)
    if not stat.S_ISREG(metadata.st_mode) or resolved != interpreter:
        raise ValueError("release evidence interpreter must be an exact regular file")
    if Path("/proc/self/exe").resolve(strict=True) != interpreter:
        raise ValueError("release evidence process executable differs from policy")
    descriptor = os.open(interpreter, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size > 64 * 1024 * 1024
        ):
            raise ValueError("release evidence interpreter identity changed")
        executable = os.read(descriptor, opened.st_size + 1)
    finally:
        os.close(descriptor)
    if len(executable) != opened.st_size or not opened.st_mode & 0o111:
        raise ValueError("release evidence interpreter is not a bounded executable")
    if (
        base.entries.get(path_value) is None
        or base.entries[path_value].kind != "file"
        or not base.entries[path_value].mode & 0o111
    ):
        raise ValueError("release evidence interpreter is absent from the retained base image")
    if base.files.get(path_value) != executable:
        raise ValueError("release evidence interpreter differs from retained base-image bytes")
    selected = shutil.which("python3")
    if selected is not None and Path(selected).resolve(strict=True) != interpreter:
        raise ValueError("PATH exposes a different release evidence interpreter")
    return {
        "implementation": "CPython",
        "version": "3.12.13",
        "path": path_value,
        "resolved_path": str(resolved),
        "site_packages": str(raw["site_packages"]),
        "sha256": _sha256(executable),
    }


def _context_digest(export: Path) -> tuple[str, list[dict[str, str]]]:
    records: list[dict[str, str]] = []
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
        data = _bounded_bytes(_regular_file(export, path, f"context {path}", control=True), path)
        records.append({"path": path, "sha256": _sha256(data)})
    for root in ("backend", "fixtures", "frontend", "migrations", "third_party"):
        for path, data in sorted(
            _walk_files(export, root, symlink_label="context symlink").items()
        ):
            records.append({"path": path, "sha256": _sha256(data)})
    encoded = json.dumps(records, separators=(",", ":"), sort_keys=True).encode()
    return _sha256(encoded), records


def _image_files(layout: Path, manifest_digest: str) -> tuple[list[str], OciSnapshot, str]:
    manifest = _json_bytes(_oci_blob(layout, manifest_digest, "OCI manifest"), "OCI manifest")
    descriptors = manifest.get("layers")
    if not isinstance(descriptors, list):
        raise ValueError("OCI manifest layers must be a list")
    layers = [str(item["digest"]) for item in descriptors]
    config = manifest.get("config")
    if not isinstance(config, dict):
        raise ValueError("OCI manifest config must be an object")
    image_config = _json_bytes(_oci_blob(layout, config.get("digest"), "OCI config"), "OCI config")
    created = image_config.get("created")
    if not isinstance(created, str) or not created:
        raise ValueError("OCI config must provide a deterministic creation timestamp")
    return (
        layers,
        _oci_snapshot(_oci_blob(layout, digest, "OCI layer") for digest in layers),
        created,
    )


def _spdx_package(coordinate: str, expression: str) -> dict[str, object]:
    return {
        "SPDXID": f"SPDXRef-Package-{_sha256(coordinate.encode())}",
        "name": coordinate,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": expression,
        "licenseDeclared": expression,
        "copyrightText": "NOASSERTION",
    }


def _python_installations(
    filesystem: dict[str, bytes], registry: dict[str, Any]
) -> tuple[list[str], list[dict[str, object]]]:
    package_evidence = {
        item["coordinate"]: item for item in registry.get("packages", []) if isinstance(item, dict)
    }
    installations: list[dict[str, object]] = []
    coordinates: list[str] = []
    for path, data in sorted(filesystem.items()):
        if not path.endswith(".dist-info/METADATA") or not path.startswith(
            "/app/.venv/lib/python3.12/site-packages/"
        ):
            continue
        metadata = BytesParser().parsebytes(data)
        name, version = metadata.get("Name"), metadata.get("Version")
        if not name or not version:
            raise ValueError(f"installed metadata identity is incomplete: {path}")
        coordinate = f"python:{canonicalize_name(name)}=={version}"
        evidence = package_evidence.get(coordinate)
        if evidence is None:
            raise ValueError(f"installed package has no selected artifact: {coordinate}")
        dist_info = str(PurePosixPath(path).parent)
        record_path = f"{dist_info}/RECORD"
        license_paths = sorted(
            candidate for candidate in filesystem if candidate.startswith(f"{dist_info}/licenses/")
        )
        if record_path not in filesystem or not license_paths:
            raise ValueError(f"installed package evidence is incomplete: {coordinate}")
        installations.append(
            {
                "coordinate": coordinate,
                "artifact_integrity": evidence["artifact"]["integrity"],
                "metadata_path": path,
                "record_path": record_path,
                "license_paths": license_paths,
            }
        )
        coordinates.append(coordinate)
    return sorted(coordinates), sorted(installations, key=lambda item: str(item["coordinate"]))


def _components(snapshot: OciSnapshot) -> list[dict[str, object]]:
    components, _, _ = _installed_debian_components(snapshot)
    return components


def _package_licenses(registry: dict[str, Any], artifacts: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in registry.get("packages", []):
        if not isinstance(item, dict) or not str(item.get("coordinate", "")).startswith("python:"):
            continue
        coordinate = str(item["coordinate"])
        artifact = item.get("artifact")
        if not isinstance(artifact, dict):
            raise ValueError(f"package artifact is absent: {coordinate}")
        relative = _relative(artifact.get("path"), f"{coordinate} artifact path")
        data = _bounded_bytes(
            _regular_file(artifacts, relative, f"{coordinate} package artifact"),
            f"{coordinate} package artifact",
        )
        members = _archive_members(data, coordinate, relative)
        expression, _, _, _ = _archive_conclusion(coordinate, members, require_material=False)
        result[coordinate] = expression
    return result


def _validate_startup() -> None:
    if sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 12, 13):
        raise ValueError("release evidence requires CPython 3.12.13")
    if not sys.flags.isolated or not sys.flags.no_site or not sys.flags.safe_path:
        raise ValueError("release evidence requires isolated no-site interpreter startup")
    if any(name.startswith("PYTHON") for name in os.environ):
        raise ValueError("release evidence rejects Python startup environment variables")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("scan",))
    parser.add_argument("subject")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--layout", default="image")
    parser.add_argument("--output", required=True)
    parser.add_argument("--packaging-authority-sha256", required=True)
    return parser.parse_args()


def _validate_canonical_args(args: argparse.Namespace) -> None:
    canonical_arguments = {
        "source": Path("."),
        "export": Path("public-export"),
        "artifacts": Path("artifacts"),
        "layout": "image",
        "output": "spdx-json=reports/final-image.sbom.json",
    }
    for name, expected in canonical_arguments.items():
        if getattr(args, name) != expected:
            raise ValueError(f"{name} must be {expected}")


def _generate_evidence(args: argparse.Namespace) -> None:
    bootstrap_policy = _bootstrap_policy(args.source)
    _load_tracked_authority(args.source, bootstrap_policy)
    packaging_authority = _packaging_authority()
    packaging_binding = _sha256(
        json.dumps(packaging_authority, separators=(",", ":"), sort_keys=True).encode()
    )
    if args.packaging_authority_sha256 != packaging_binding:
        raise ValueError("packaging authority binding differs from the active installation")
    if sys.argv[0] != "artifacts/tools/release-evidence":
        raise ValueError("release evidence must run through the canonical materialized path")
    materialized = _regular_file(
        Path("."), sys.argv[0], "materialized release evidence generator", control=True
    )
    if stat.S_IMODE(materialized.stat().st_mode) != 0o755:
        raise ValueError("materialized release evidence generator must be mode 0755")
    tracked = _regular_file(
        args.source,
        "ops/release/generate_release_evidence.py",
        "tracked release evidence generator",
        control=True,
    )
    if materialized.read_bytes() != tracked.read_bytes():
        raise ValueError("materialized release evidence generator differs from tracked source")
    source_policy = _read_json(args.source, "ops/release/sbom-tool.json", "source tool policy")
    export_policy = _read_json(args.export, "ops/release/sbom-tool.json", "export tool policy")
    if source_policy != export_policy:
        raise ValueError("staged tool policy differs from tracked source")
    if source_policy != bootstrap_policy:
        raise ValueError("active tool policy differs from bootstrap authority")
    expected_tool = args.artifacts / _relative(
        source_policy.get("path"), "materialized tool policy path"
    )
    if materialized.resolve() != expected_tool.resolve() or source_policy.get("sha256") != _sha256(
        materialized.read_bytes()
    ):
        raise ValueError("materialized release evidence generator differs from tool policy")
    prefix = "oci-manifest:"
    if not args.subject.startswith(prefix):
        raise ValueError("subject must be an OCI manifest digest")
    manifest_digest = args.subject.removeprefix(prefix)
    layout = args.artifacts / args.layout
    layers, snapshot, created = _image_files(layout, manifest_digest)
    filesystem = snapshot.files
    registry = _read_json(args.export, "compliance/dependency-evidence.json", "dependency evidence")
    registry = _materialize_ofl_asset_evidence(args.export, registry)
    _write_json(args.export / "compliance/dependency-evidence.json", registry)
    docker_data = _bounded_bytes(
        _regular_file(args.export, "Dockerfile", "Dockerfile", control=True), "Dockerfile"
    )
    docker = _docker_authority(docker_data)
    base = _base_image(args.artifacts / "base-image", docker.application_base)
    uv_image = _base_image(args.artifacts / "uv-image", docker.uv_base)
    interpreter = _interpreter_authority(source_policy, base.snapshot)
    if tuple(layers[: len(base.layers)]) != base.layers:
        raise ValueError("final OCI layer prefix differs from retained base image")
    packages, installations = _python_installations(filesystem, registry)
    components = [*_components(base.snapshot), _uv_component(uv_image)[0]]
    reports = args.artifacts / "reports"
    sbom_path = args.artifacts / args.output.removeprefix("spdx-json=")
    uv_path = reports / "uv-install.json"
    package_licenses = _package_licenses(registry, args.artifacts)
    spdx_packages = [
        _spdx_package(coordinate, package_licenses[coordinate]) for coordinate in packages
    ] + [
        _spdx_package(str(component["coordinate"]), str(component["license_expression"]))
        for component in components
    ]
    _write_json(
        sbom_path,
        {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "alphadecay-final-image",
            "documentNamespace": (
                "https://alphadecay.dev/spdx/final-image/" + manifest_digest.removeprefix("sha256:")
            ),
            "creationInfo": {
                "created": created,
                "creators": ["Tool: alphadecay-release-evidence-1.0.0"],
            },
            "packages": spdx_packages,
            "documentDescribes": [item["SPDXID"] for item in spdx_packages],
            "alphadecay": {
                "schema_version": 3,
                "subject_manifest_digest": manifest_digest,
                "packages": packages,
                "components": components,
            },
        },
    )
    lock_data = _bounded_bytes(
        _regular_file(args.export, "uv.lock", "uv lock", control=True), "uv lock"
    )
    _write_json(
        uv_path,
        {
            "schema_version": 1,
            "lock_sha256": _sha256(lock_data),
            "venv_root": "/app/.venv",
            "packages": installations,
        },
    )
    inventory = {
        "manifest_digest": manifest_digest,
        "layers": layers,
        "files": [
            {"path": path, "sha256": _sha256(data)} for path, data in sorted(filesystem.items())
        ],
        "entries": _oci_entry_records(snapshot),
    }
    _write_json(reports / "final-image-files.json", inventory)
    tool_data = _bounded_bytes(
        _regular_file(
            args.source,
            "ops/release/generate_release_evidence.py",
            "release evidence generator",
            control=True,
        ),
        "release evidence generator",
    )
    invocation = [
        interpreter["path"],
        "-I",
        "-S",
        "artifacts/tools/release-evidence",
        "scan",
        args.subject,
        "--source",
        str(args.source),
        "--export",
        str(args.export),
        "--artifacts",
        str(args.artifacts),
        "--layout",
        args.layout,
        "--output",
        args.output,
    ]
    invocation.extend(("--packaging-authority-sha256", packaging_binding))
    module_paths = [
        "ops/release/generate_release_evidence.py",
        "ops/release/license_provenance.py",
    ]
    modules = []
    for path in module_paths:
        data = _bounded_bytes(
            _regular_file(args.source, path, f"tracked release module {path}", control=True),
            f"tracked release module {path}",
        )
        modules.append({"path": path, "sha256": _sha256(data)})
    context_sha256, context_files = _context_digest(args.export)
    input_records = [
        {"path": path, "sha256": _sha256(_bounded_bytes(_regular_file(root, path, path), path))}
        for root, path in (
            (args.export, "Dockerfile"),
            (args.export, "uv.lock"),
            (args.export, "package-lock.json"),
            (args.export, "compliance/dependency-evidence.json"),
            (args.export, "compliance/frontend-bundle.json"),
            (args.artifacts, f"{args.layout}/blobs/sha256/{manifest_digest[7:]}"),
            (args.artifacts, "base-image/index.json"),
            (args.artifacts, "uv-image/index.json"),
        )
    ]
    _write_json(
        reports / "sbom-attestation.json",
        {
            "schema_version": 3,
            "tool": {
                "name": "alphadecay-release-evidence",
                "version": "1.0.0",
                "path": "tools/release-evidence",
                "sha256": _sha256(tool_data),
            },
            "invocation": invocation,
            "python": interpreter,
            "packaging": packaging_authority,
            "modules": modules,
            "format": "spdx-json",
            "input_manifest_digest": manifest_digest,
            "dockerfile_sha256": _sha256(docker_data),
            "context": {"sha256": context_sha256, "files": context_files},
            "base_image": {
                "reference": base.reference,
                "manifest_digest": base.manifest_digest,
                "config_digest": base.config_digest,
                "layers": list(base.layers),
                "diff_ids": list(base.diff_ids),
            },
            "uv_image": {
                "reference": uv_image.reference,
                "manifest_digest": uv_image.manifest_digest,
                "config_digest": uv_image.config_digest,
                "layers": list(uv_image.layers),
                "diff_ids": list(uv_image.diff_ids),
            },
            "inputs": sorted(input_records, key=lambda item: item["path"]),
            "outputs": [
                {
                    "path": str(path.relative_to(args.artifacts)),
                    "sha256": _sha256(path.read_bytes()),
                }
                for path in (sbom_path, uv_path, reports / "final-image-files.json")
            ],
        },
    )
    attestation_path = reports / "sbom-attestation.json"
    _write_json(
        args.export / "compliance/final-image-spdx.json",
        {
            "schema_version": 4,
            "oci_layout": args.layout,
            "base_oci_layout": "base-image",
            "uv_oci_layout": "uv-image",
            "manifest_digest": manifest_digest,
            "base_image": {
                "reference": base.reference,
                "manifest_digest": base.manifest_digest,
                "config_digest": base.config_digest,
                "layers": list(base.layers),
                "diff_ids": list(base.diff_ids),
            },
            "uv_image": {
                "reference": uv_image.reference,
                "manifest_digest": uv_image.manifest_digest,
                "config_digest": uv_image.config_digest,
                "layers": list(uv_image.layers),
                "diff_ids": list(uv_image.diff_ids),
            },
            "layers": layers,
            "interpreter": interpreter,
            "files": inventory["files"],
            "entries": _oci_entry_records(snapshot),
            "packages": packages + sorted(str(component["coordinate"]) for component in components),
            "application_roots": [
                {"source": "backend", "image": "/app/backend"},
                {"source": "fixtures", "image": "/app/fixtures"},
                {"source": "migrations", "image": "/app/migrations"},
                {"source": "dist", "image": "/app/dist"},
            ],
            "application_files": [
                {"source": "pyproject.toml", "image": "/app/pyproject.toml"},
                {"source": "uv.lock", "image": "/app/uv.lock"},
            ],
            "external_sbom": {
                "path": str(sbom_path.relative_to(args.artifacts)),
                "sha256": _sha256(sbom_path.read_bytes()),
            },
            "sbom_attestation": {
                "path": str(attestation_path.relative_to(args.artifacts)),
                "sha256": _sha256(attestation_path.read_bytes()),
            },
            "uv_install_report": {
                "path": str(uv_path.relative_to(args.artifacts)),
                "sha256": _sha256(uv_path.read_bytes()),
            },
        },
    )
    verify_and_generate(args.source, args.export, args.artifacts)


def main() -> int:
    _validate_startup()
    args = _parse_args()
    _validate_canonical_args(args)
    _generate_evidence(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
