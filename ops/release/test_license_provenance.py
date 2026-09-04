from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import unittest
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest import mock

from ops.release.license_provenance import (
    ComplianceError,
    _archive_conclusion,
    _archive_members,
    _assets,
    _base_image,
    _console_script_bytes,
    _debian_license,
    _docker_authority,
    _installed_debian_components,
    _layer_diff_id,
    _load_node_lock,
    _notice,
    _oci_entry_records,
    _oci_files,
    _oci_snapshot,
    _package_evidence,
    _packaging_authority,
    _supplemental_licenses,
    _uv_component,
    verify_and_generate,
)

SBOM_TOOL_DATA = b"pinned synthetic sbom tool\n"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


def write_json(path: Path, value: object) -> None:
    write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def wheel(
    name: str, version: str, expression: str = "MIT", *, duplicate_license: bool = False
) -> tuple[bytes, dict[str, bytes]]:
    stem = name.replace("-", "_")
    members = {
        f"{stem}-{version}.dist-info/METADATA": (
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
            f"License-Expression: {expression}\n"
            + (f"License-Expression: {expression}\n" if duplicate_license else "")
        ).encode(),
        f"{stem}-{version}.dist-info/licenses/LICENSE": f"MIT license for {name}\n".encode(),
        f"{stem}-{version}.dist-info/entry_points.txt": (
            f"[console_scripts]\n{stem} = {stem}:main\n"
        ).encode(),
        f"{stem}/__init__.py": b"\n",
    }
    record_path = f"{stem}-{version}.dist-info/RECORD"
    record_rows = []
    for path, data in sorted(members.items()):
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        record_rows.append(f"{path},sha256={encoded},{len(data)}\n")
    members[record_path] = ("".join(record_rows) + f"{record_path},,\n").encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, data in sorted(members.items()):
            info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue(), members


def npm_archive(name: str, version: str) -> tuple[bytes, dict[str, bytes]]:
    members = {
        "package/package.json": (
            json.dumps({"name": name, "version": version, "license": "MIT"}, sort_keys=True) + "\n"
        ).encode(),
        "package/LICENSE": f"MIT license for {name}\n".encode(),
        "package/index.js": b"module.exports = {};\n",
    }
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as archive:
        for path, data in sorted(members.items()):
            info = tarfile.TarInfo(path)
            info.mtime = 0
            info.mode = 0o644
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(tar_bytes.getvalue(), mtime=0), members


def layer(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path, data in sorted(files.items()):
            info = tarfile.TarInfo(path.lstrip("/"))
            info.mtime = 0
            info.mode = (
                0o755
                if path in {"/bin/uv", "/bin/uvx", "/uv", "/uvx"} or path.endswith("/python3.12")
                else 0o644
            )
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def layer_members(members: list[tuple[str, bytes | str, str]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path, value, kind in members:
            info = tarfile.TarInfo(path.lstrip("/"))
            info.mtime = 0
            info.mode = 0o755 if path in {"/bin/uv", "/bin/uvx", "/uv", "/uvx"} else 0o644
            if kind == "file":
                data = value if isinstance(value, bytes) else value.encode()
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            elif kind == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                info.linkname = value.decode() if isinstance(value, bytes) else value
                archive.addfile(info)
    return output.getvalue()


class Fixture:
    def __init__(self, root: Path) -> None:
        self.source = root / "source"
        self.export = root / "export"
        self.artifacts = root / "artifacts"
        self.interpreter_path = "/usr/local/bin/python3.12"
        self.interpreter_site_packages = "/app/.venv/lib/python3.12/site-packages"
        self.interpreter_bytes = b"fixture CPython 3.12.13 executable\n"
        self._write_base_image()
        self._write_uv_image()
        self.archives: dict[str, tuple[str, str, str, dict[str, bytes]]] = {}
        python_specs = (
            ("runtime-lib", "2.0.0"),
            ("alpaca-mcp-server", "3.0.0"),
            ("mcp-child", "3.1.0"),
            ("test-tool", "4.0.0"),
        )
        for name, version in python_specs:
            data, members = wheel(name, version)
            path = f"archives/{name.replace('-', '_')}-{version}-py3-none-any.whl"
            write(self.artifacts / path, data)
            self.archives[f"python:{name}=={version}"] = (
                f"https://example.invalid/{Path(path).name}",
                "sha256:" + sha256(data),
                path,
                members,
            )
        node_specs = (
            ("browser-lib", "5.0.0"),
            ("browser-child", "5.1.0"),
            ("optional-lib", "5.2.0"),
            ("peer-lib", "5.3.0"),
            ("root-optional", "5.4.0"),
            ("bundle-tool", "6.0.0"),
            ("orphan-tool", "9.0.0"),
        )
        for name, version in node_specs:
            data, members = npm_archive(name, version)
            path = f"archives/{name}-{version}.tgz"
            write(self.artifacts / path, data)
            integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
            self.archives[f"node:{name}@{version}"] = (
                f"https://example.invalid/{Path(path).name}",
                integrity,
                path,
                members,
            )
        self._write_locks()
        self._write_export()

    def _write_base_image(self) -> None:
        self.base_files = {
            self.interpreter_path: self.interpreter_bytes,
            "/var/lib/dpkg/status": (
                b"Package: runtime-base\nVersion: 1\nArchitecture: amd64\n"
                b"Status: install ok installed\nLicense: MIT\n"
            ),
            "/var/lib/dpkg/info/runtime-base.list": (
                b"/usr/lib/x86_64-linux-gnu/libbase.so.1\n/usr/share/doc/runtime-base/LICENSE\n"
            ),
            "/var/lib/dpkg/info/runtime-base.md5sums": (
                hashlib.md5(b"safe shared library bytes\n", usedforsecurity=False)
                .hexdigest()
                .encode()
                + b"  usr/lib/x86_64-linux-gnu/libbase.so.1\n"
            ),
            "/usr/lib/x86_64-linux-gnu/libbase.so.1": b"safe shared library bytes\n",
            "/usr/share/doc/runtime-base/LICENSE": (
                b"Format: https://www.debian.org/doc/packaging-manuals/"
                b"copyright-format/1.0/\n\nFiles: *\nCopyright: Fixture authors\n"
                b"License: MIT\n\nLicense: MIT\n Permission is granted for this fixture.\n"
            ),
        }
        self.base_layer_data = layer(self.base_files)
        layer_digest = "sha256:" + sha256(self.base_layer_data)
        config_data = (
            json.dumps(
                {
                    "architecture": "amd64",
                    "os": "linux",
                    "rootfs": {"type": "layers", "diff_ids": [layer_digest]},
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        config_digest = "sha256:" + sha256(config_data)
        manifest_data = json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": config_digest, "size": len(config_data)},
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar",
                        "digest": layer_digest,
                        "size": len(self.base_layer_data),
                    }
                ],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        manifest_digest = "sha256:" + sha256(manifest_data)
        index_data = (
            json.dumps(
                {
                    "schemaVersion": 2,
                    "manifests": [
                        {
                            "digest": manifest_digest,
                            "size": len(manifest_data),
                            "platform": {"architecture": "amd64", "os": "linux"},
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        self.base_reference = "python:fixture@sha256:" + sha256(index_data)
        self.base_manifest_digest = manifest_digest
        self.base_config_digest = config_digest
        base = self.artifacts / "base-image"
        write_json(base / "oci-layout", {"imageLayoutVersion": "1.0.0"})
        write(base / "index.json", index_data)
        for digest, data in (
            (layer_digest, self.base_layer_data),
            (config_digest, config_data),
            (manifest_digest, manifest_data),
        ):
            write(base / "blobs/sha256" / digest[7:], data)

    def _write_uv_image(self) -> None:
        self.uv_files = {
            "/uv": b"fixture uv executable\n",
            "/uvx": b"fixture uvx executable\n",
            "/LICENSE-MIT": b"MIT license for fixture uv\n",
        }
        layer_data = layer(self.uv_files)
        layer_digest = "sha256:" + sha256(layer_data)
        config_data = (
            json.dumps(
                {
                    "architecture": "amd64",
                    "os": "linux",
                    "rootfs": {"type": "layers", "diff_ids": [layer_digest]},
                    "config": {
                        "Labels": {
                            "org.opencontainers.image.source": "https://github.com/astral-sh/uv",
                            "org.opencontainers.image.version": "0.12.3",
                            "org.opencontainers.image.licenses": "MIT OR Apache-2.0",
                        }
                    },
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        config_digest = "sha256:" + sha256(config_data)
        manifest_data = json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": config_digest, "size": len(config_data)},
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar",
                        "digest": layer_digest,
                        "size": len(layer_data),
                    }
                ],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        manifest_digest = "sha256:" + sha256(manifest_data)
        index_data = (
            json.dumps(
                {
                    "schemaVersion": 2,
                    "manifests": [
                        {
                            "digest": manifest_digest,
                            "size": len(manifest_data),
                            "platform": {"architecture": "amd64", "os": "linux"},
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        self.uv_reference = "ghcr.io/astral-sh/uv:0.12.3@sha256:" + sha256(index_data)
        self.uv_manifest_digest = manifest_digest
        self.uv_config_digest = config_digest
        self.uv_layer_digest = layer_digest
        uv = self.artifacts / "uv-image"
        write_json(uv / "oci-layout", {"imageLayoutVersion": "1.0.0"})
        write(uv / "index.json", index_data)
        for digest, data in (
            (layer_digest, layer_data),
            (config_digest, config_data),
            (manifest_digest, manifest_data),
        ):
            write(uv / "blobs/sha256" / digest[7:], data)

    def _write_locks(self) -> None:
        py = {key: value for key, value in self.archives.items() if key.startswith("python:")}
        runtime_url, runtime_hash = py["python:runtime-lib==2.0.0"][:2]
        mcp_url, mcp_hash = py["python:alpaca-mcp-server==3.0.0"][:2]
        child_url, child_hash = py["python:mcp-child==3.1.0"][:2]
        test_url, test_hash = py["python:test-tool==4.0.0"][:2]
        uv_lock = f'''version = 1

[[package]]
name = "demo"
version = "1.0.0"
source = {{ editable = "." }}
dependencies = [{{ name = "runtime-lib" }}, {{ name = "alpaca-mcp-server" }}]
[package.dev-dependencies]
dev = [{{ name = "test-tool" }}]

[[package]]
name = "runtime-lib"
version = "2.0.0"
source = {{ registry = "https://example.invalid/simple" }}
wheels = [{{ url = "{runtime_url}", hash = "{runtime_hash}" }}]

[[package]]
name = "alpaca-mcp-server"
version = "3.0.0"
source = {{ registry = "https://example.invalid/simple" }}
dependencies = [{{ name = "mcp-child" }}]
wheels = [{{ url = "{mcp_url}", hash = "{mcp_hash}" }}]

[[package]]
name = "mcp-child"
version = "3.1.0"
source = {{ registry = "https://example.invalid/simple" }}
wheels = [{{ url = "{child_url}", hash = "{child_hash}" }}]

[[package]]
name = "test-tool"
version = "4.0.0"
source = {{ registry = "https://example.invalid/simple" }}
wheels = [{{ url = "{test_url}", hash = "{test_hash}" }}]
'''
        node = {key: value for key, value in self.archives.items() if key.startswith("node:")}
        package_lock = {
            "name": "demo",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {
                    "name": "demo",
                    "version": "1.0.0",
                    "dependencies": {"browser-lib": "5.0.0"},
                    "optionalDependencies": {"root-optional": "5.4.0"},
                    "devDependencies": {"bundle-tool": "6.0.0"},
                },
                "node_modules/browser-lib": {
                    "version": "5.0.0",
                    "resolved": node["node:browser-lib@5.0.0"][0],
                    "integrity": node["node:browser-lib@5.0.0"][1],
                    "dependencies": {"browser-child": "5.1.0"},
                    "optionalDependencies": {"optional-lib": "5.2.0"},
                    "peerDependencies": {"peer-lib": "5.3.0"},
                    "peerDependenciesMeta": {"peer-lib": {"optional": False}},
                },
                "node_modules/browser-child": {
                    "version": "5.1.0",
                    "resolved": node["node:browser-child@5.1.0"][0],
                    "integrity": node["node:browser-child@5.1.0"][1],
                },
                "node_modules/optional-lib": {
                    "version": "5.2.0",
                    "resolved": node["node:optional-lib@5.2.0"][0],
                    "integrity": node["node:optional-lib@5.2.0"][1],
                    "optional": True,
                },
                "node_modules/peer-lib": {
                    "version": "5.3.0",
                    "resolved": node["node:peer-lib@5.3.0"][0],
                    "integrity": node["node:peer-lib@5.3.0"][1],
                },
                "node_modules/root-optional": {
                    "version": "5.4.0",
                    "resolved": node["node:root-optional@5.4.0"][0],
                    "integrity": node["node:root-optional@5.4.0"][1],
                    "optional": True,
                },
                "node_modules/bundle-tool": {
                    "version": "6.0.0",
                    "resolved": node["node:bundle-tool@6.0.0"][0],
                    "integrity": node["node:bundle-tool@6.0.0"][1],
                },
                "node_modules/orphan-tool": {
                    "version": "9.0.0",
                    "resolved": node["node:orphan-tool@9.0.0"][0],
                    "integrity": node["node:orphan-tool@9.0.0"][1],
                },
            },
        }
        for tree in (self.source, self.export):
            write(tree / "uv.lock", uv_lock)
            write_json(tree / "package-lock.json", package_lock)
            write(tree / ".dockerignore", ".git\n")
            write(tree / "index.html", "<main></main>\n")
            write_json(tree / "package.json", {"name": "demo", "version": "1.0.0"})
            write_json(tree / "tsconfig.json", {"compilerOptions": {}})
            write(tree / "vitest.config.ts", "export default {};\n")
            write(tree / "frontend/main.ts", "export {};\n")
            write(
                tree / "Dockerfile",
                f"FROM {self.uv_reference} AS uv\n"
                f"FROM {self.base_reference} AS application\n"
                'ENV PATH="/app/.venv/bin:$PATH"\n'
                "WORKDIR /app\n"
                "COPY --from=uv /uv /uvx /bin/\n"
                "COPY pyproject.toml uv.lock ./\n"
                "COPY backend ./backend\n"
                "COPY third_party ./third_party\n"
                "COPY fixtures ./fixtures\n"
                "COPY migrations ./migrations\n"
                "COPY --from=frontend /app/dist ./dist\n"
                "RUN uv sync --frozen --no-dev --no-editable\n",
            )
            write(tree / "backend/app/main.py", "print('safe')\n")
            write(tree / "migrations/0001.sql", "SELECT 1;\n")
            write(tree / "pyproject.toml", "[project]\nname='demo'\nversion='1.0.0'\n")
            write_json(
                tree / "ops/release/sbom-tool.json",
                {
                    "base_image": self.base_reference,
                    "uv_image": self.uv_reference,
                    "name": "fixture-sbom",
                    "version": "1.0.0",
                    "path": "tools/sbom-tool",
                    "sha256": sha256(SBOM_TOOL_DATA),
                    "interpreter": {
                        "implementation": "CPython",
                        "version": "3.12.13",
                        "path": self.interpreter_path,
                        "site_packages": self.interpreter_site_packages,
                        "sha256_source": "base-image",
                    },
                    "invocation_template": [
                        self.interpreter_path,
                        "-I",
                        "-S",
                        "tools/sbom-tool",
                        "scan",
                        "oci-manifest:{manifest_digest}",
                        "--output",
                        "spdx-json=reports/final-image.sbom.json",
                    ],
                    "format": "spdx-json",
                    "modules": [
                        "ops/release/generate_release_evidence.py",
                        "ops/release/license_provenance.py",
                    ],
                },
            )
            write(tree / "ops/release/generate_release_evidence.py", SBOM_TOOL_DATA)
            write(tree / "ops/release/license_provenance.py", b"fixture verifier\n")
            write_json(
                tree / "third_party/notices/supplemental-licenses.json",
                {"packages": [], "schema_version": 1},
            )

    def _write_export(self) -> None:
        write(self.export / "public/favicon.svg", "<svg/>\n")
        write(self.export / "fixtures/replay/SAFE.json", '{"provenance":"REPLAY / FIXTURE DATA"}\n')
        write(self.export / "dist/app.js", "console.log('safe');\n")
        write(self.export / "dist/app.css", "body {}\n")
        packages = []
        retained: dict[str, bytes] = {}
        reachable = {
            "python:runtime-lib==2.0.0",
            "python:alpaca-mcp-server==3.0.0",
            "python:mcp-child==3.1.0",
            "python:test-tool==4.0.0",
            "node:browser-lib@5.0.0",
            "node:browser-child@5.1.0",
            "node:bundle-tool@6.0.0",
            "node:optional-lib@5.2.0",
            "node:peer-lib@5.3.0",
            "node:root-optional@5.4.0",
        }
        node_lock_paths = {
            f"node:{name}@{version}": f"node_modules/{name}"
            for name, version in (
                ("browser-lib", "5.0.0"),
                ("browser-child", "5.1.0"),
                ("optional-lib", "5.2.0"),
                ("peer-lib", "5.3.0"),
                ("root-optional", "5.4.0"),
                ("bundle-tool", "6.0.0"),
            )
        }
        for coordinate in sorted(reachable):
            locator, integrity, archive_path, members = self.archives[coordinate]
            metadata_member = next(
                path
                for path in members
                if path.endswith("METADATA") or path == "package/package.json"
            )
            license_member = next(path for path in members if Path(path).name == "LICENSE")
            token = (
                coordinate.replace(":", "-").replace("/", "-").replace("@", "-").replace("=", "-")
            )
            metadata_path = f"third_party/evidence/{token}/metadata"
            license_path = f"third_party/evidence/{token}/LICENSE"
            write(self.export / metadata_path, members[metadata_member])
            write(self.export / license_path, members[license_member])
            retained[license_path] = members[license_member]
            packages.append(
                {
                    "coordinate": coordinate,
                    **(
                        {"lock_path": node_lock_paths[coordinate]}
                        if coordinate.startswith("node:")
                        else {}
                    ),
                    "artifact": {
                        "locator": locator,
                        "integrity": integrity,
                        "path": archive_path,
                    },
                    "retained_files": [
                        {
                            "archive_path": metadata_member,
                            "path": metadata_path,
                            "sha256": sha256(members[metadata_member]),
                        },
                        {
                            "archive_path": license_member,
                            "path": license_path,
                            "sha256": sha256(members[license_member]),
                        },
                    ],
                    "source_members": [],
                    "review_disposition": "approved",
                }
            )
        assets = []
        for path in ("public/favicon.svg", "fixtures/replay/SAFE.json"):
            assets.append(
                {
                    "path": path,
                    "sha256": sha256((self.export / path).read_bytes()),
                    "origin": "created for the fixture",
                    "creation_method": "hand-authored",
                    "license_expression": "MIT",
                }
            )
        registry = {
            "schema_version": 3,
            "target_environment": {
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
            },
            "lockfiles": {
                "uv.lock": sha256((self.export / "uv.lock").read_bytes()),
                "package-lock.json": sha256((self.export / "package-lock.json").read_bytes()),
            },
            "packages": packages,
            "components": [],
            "assets": assets,
        }
        write_json(self.export / "compliance/dependency-evidence.json", registry)
        write_json(
            self.export / "compliance/frontend-bundle.json",
            {
                "schema_version": 2,
                "root": "dist",
                "packages": [
                    "node:browser-child@5.1.0#node_modules/browser-child",
                    "node:browser-lib@5.0.0#node_modules/browser-lib",
                    "node:optional-lib@5.2.0#node_modules/optional-lib",
                    "node:peer-lib@5.3.0#node_modules/peer-lib",
                    "node:root-optional@5.4.0#node_modules/root-optional",
                ],
                "files": [
                    {"path": path, "sha256": sha256((self.export / path).read_bytes())}
                    for path in ("dist/app.css", "dist/app.js")
                ],
            },
        )
        image_packages = [
            "python:alpaca-mcp-server==3.0.0",
            "python:mcp-child==3.1.0",
            "python:runtime-lib==2.0.0",
        ]
        component = _installed_debian_components(_oci_snapshot([self.base_layer_data]))[0][0]
        uv_component = _uv_component(_base_image(self.artifacts / "uv-image", self.uv_reference))[0]
        image_files = {
            self.interpreter_path: self.interpreter_bytes,
            "/bin/uv": b"fixture uv executable\n",
            "/bin/uvx": b"fixture uvx executable\n",
            "/app/backend/app/main.py": b"print('safe')\n",
            "/app/fixtures/replay/SAFE.json": b'{"provenance":"REPLAY / FIXTURE DATA"}\n',
            "/app/migrations/0001.sql": b"SELECT 1;\n",
            "/app/dist/app.js": b"console.log('safe');\n",
            "/app/dist/app.css": b"body {}\n",
            "/app/pyproject.toml": b"[project]\nname='demo'\nversion='1.0.0'\n",
            "/app/uv.lock": (self.export / "uv.lock").read_bytes(),
            "/var/lib/dpkg/status": (
                b"Package: runtime-base\nVersion: 1\nArchitecture: amd64\n"
                b"Status: install ok installed\nLicense: MIT\n"
            ),
            "/var/lib/dpkg/info/runtime-base.list": (
                b"/usr/lib/x86_64-linux-gnu/libbase.so.1\n/usr/share/doc/runtime-base/LICENSE\n"
            ),
            "/var/lib/dpkg/info/runtime-base.md5sums": (
                hashlib.md5(b"safe shared library bytes\n", usedforsecurity=False)
                .hexdigest()
                .encode()
                + b"  usr/lib/x86_64-linux-gnu/libbase.so.1\n"
            ),
            "/usr/lib/x86_64-linux-gnu/libbase.so.1": b"safe shared library bytes\n",
            "/usr/share/doc/runtime-base/LICENSE": (
                b"Format: https://www.debian.org/doc/packaging-manuals/"
                b"copyright-format/1.0/\n\nFiles: *\nCopyright: Fixture authors\n"
                b"License: MIT\n\nLicense: MIT\n Permission is granted for this fixture.\n"
            ),
        }
        python_artifacts = []
        for coordinate in image_packages:
            name, version = coordinate.removeprefix("python:").split("==", 1)
            stem = name.replace("-", "_")
            site = "/app/.venv/lib/python3.12/site-packages"
            module_path = f"{site}/{stem}/__init__.py"
            metadata_path = f"{site}/{stem}-{version}.dist-info/METADATA"
            record_path = f"{site}/{stem}-{version}.dist-info/RECORD"
            license_path = f"{site}/{stem}-{version}.dist-info/licenses/LICENSE"
            entry_points_path = f"{site}/{stem}-{version}.dist-info/entry_points.txt"
            console_path = f"/app/.venv/bin/{stem}"
            members = self.archives[coordinate][3]
            archive_metadata = next(path for path in members if path.endswith("METADATA"))
            archive_license = next(path for path in members if Path(path).name == "LICENSE")
            archive_entry_points = next(
                path for path in members if path.endswith("entry_points.txt")
            )
            image_files[module_path] = b"\n"
            image_files[metadata_path] = members[archive_metadata]
            image_files[license_path] = members[archive_license]
            image_files[entry_points_path] = members[archive_entry_points]
            image_files[console_path] = _console_script_bytes("/app/.venv", f"{stem}:main")
            record_rows = []
            for installed_path in (
                module_path,
                metadata_path,
                license_path,
                entry_points_path,
                console_path,
            ):
                installed_data = image_files[installed_path]
                encoded = (
                    base64.urlsafe_b64encode(hashlib.sha256(installed_data).digest())
                    .decode()
                    .rstrip("=")
                )
                record_rows.append(
                    f"{os.path.relpath(installed_path, site)},sha256={encoded},"
                    f"{len(installed_data)}\n"
                )
            image_files[record_path] = (
                "".join(record_rows) + f"{record_path.removeprefix(site + '/')},,\n"
            ).encode()
            python_artifacts.append(
                {
                    "coordinate": coordinate,
                    "artifact_integrity": self.archives[coordinate][1],
                    "metadata_path": metadata_path,
                    "record_path": record_path,
                    "license_paths": [license_path],
                }
            )
        for path, data in retained.items():
            image_files[f"/app/{path}"] = data
        app_layer_files = {
            path: data for path, data in image_files.items() if self.base_files.get(path) != data
        }
        layer_data = layer(app_layer_files)
        layer_digest = "sha256:" + sha256(layer_data)
        base_layer_digest = "sha256:" + sha256(self.base_layer_data)
        config_data = (
            json.dumps(
                {
                    "architecture": "amd64",
                    "os": "linux",
                    "created": "2026-08-29T00:00:00Z",
                    "rootfs": {
                        "type": "layers",
                        "diff_ids": [base_layer_digest, layer_digest],
                    },
                    "config": {
                        "Labels": {
                            "org.alphadecay.base": self.base_reference,
                            "org.alphadecay.glibc": "2.36",
                        }
                    },
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        config_digest = "sha256:" + sha256(config_data)
        manifest_data = json.dumps(
            {
                "schemaVersion": 2,
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": config_digest,
                    "size": len(config_data),
                },
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar",
                        "digest": base_layer_digest,
                        "size": len(self.base_layer_data),
                    },
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar",
                        "digest": layer_digest,
                        "size": len(layer_data),
                    },
                ],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        manifest_digest = "sha256:" + sha256(manifest_data)
        all_components = [component, uv_component]
        all_coordinates = image_packages + [item["coordinate"] for item in all_components]
        licenses = {item["coordinate"]: item["license_expression"] for item in all_components}
        sbom = {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "alphadecay-final-image",
            "documentNamespace": (
                "https://alphadecay.dev/spdx/final-image/" + manifest_digest.removeprefix("sha256:")
            ),
            "creationInfo": {
                "created": "2026-08-29T00:00:00Z",
                "creators": ["Tool: fixture-sbom-1.0.0"],
            },
            "packages": [
                {
                    "SPDXID": f"SPDXRef-Package-{sha256(coordinate.encode())}",
                    "name": coordinate,
                    "downloadLocation": "NOASSERTION",
                    "filesAnalyzed": False,
                    "licenseConcluded": licenses.get(coordinate, "MIT"),
                    "licenseDeclared": licenses.get(coordinate, "MIT"),
                    "copyrightText": "NOASSERTION",
                }
                for coordinate in all_coordinates
            ],
            "documentDescribes": [
                f"SPDXRef-Package-{sha256(coordinate.encode())}" for coordinate in all_coordinates
            ],
            "alphadecay": {
                "schema_version": 3,
                "subject_manifest_digest": manifest_digest,
                "packages": image_packages,
                "components": all_components,
            },
        }
        sbom_data = (json.dumps(sbom, indent=2, sort_keys=True) + "\n").encode()
        uv_report = {
            "schema_version": 1,
            "lock_sha256": sha256((self.export / "uv.lock").read_bytes()),
            "venv_root": "/app/.venv",
            "packages": python_artifacts,
        }
        uv_report_data = (json.dumps(uv_report, indent=2, sort_keys=True) + "\n").encode()
        tool_data = SBOM_TOOL_DATA
        write(self.artifacts / "reports/final-image.sbom.json", sbom_data)
        write(self.artifacts / "reports/uv-install.json", uv_report_data)
        write(self.artifacts / "tools/sbom-tool", tool_data)
        (self.artifacts / "tools/sbom-tool").chmod(0o755)
        write_json(
            self.artifacts / "reports/final-image-files.json",
            {
                "manifest_digest": manifest_digest,
                "layers": [base_layer_digest, layer_digest],
                "files": [
                    {"path": path, "sha256": sha256(data)}
                    for path, data in sorted(image_files.items())
                ],
                "entries": _oci_entry_records(_oci_snapshot([self.base_layer_data, layer_data])),
            },
        )
        context_records = []
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
            context_records.append(
                {"path": path, "sha256": sha256((self.export / path).read_bytes())}
            )
        for root in ("backend", "fixtures", "frontend", "migrations", "third_party"):
            for path in sorted(item for item in self.export.rglob("*") if item.is_file()):
                relative = path.relative_to(self.export).as_posix()
                if relative == root or relative.startswith(root + "/"):
                    context_records.append({"path": relative, "sha256": sha256(path.read_bytes())})
        context_sha = sha256(
            json.dumps(context_records, separators=(",", ":"), sort_keys=True).encode()
        )
        inputs = [
            {"path": path, "sha256": digest}
            for path, digest in (
                ("Dockerfile", sha256((self.export / "Dockerfile").read_bytes())),
                ("uv.lock", sha256((self.export / "uv.lock").read_bytes())),
                ("package-lock.json", sha256((self.export / "package-lock.json").read_bytes())),
                (
                    "compliance/dependency-evidence.json",
                    sha256((self.export / "compliance/dependency-evidence.json").read_bytes()),
                ),
                (
                    "compliance/frontend-bundle.json",
                    sha256((self.export / "compliance/frontend-bundle.json").read_bytes()),
                ),
                (f"image/blobs/sha256/{manifest_digest[7:]}", sha256(manifest_data)),
                (
                    "base-image/index.json",
                    sha256((self.artifacts / "base-image/index.json").read_bytes()),
                ),
                (
                    "uv-image/index.json",
                    sha256((self.artifacts / "uv-image/index.json").read_bytes()),
                ),
            )
        ]
        inputs.sort(key=lambda item: item["path"])
        packaging_authority = _packaging_authority()
        packaging_binding = sha256(
            json.dumps(packaging_authority, separators=(",", ":"), sort_keys=True).encode()
        )
        attestation = {
            "schema_version": 3,
            "tool": {
                "name": "fixture-sbom",
                "version": "1.0.0",
                "path": "tools/sbom-tool",
                "sha256": sha256(tool_data),
            },
            "invocation": [
                self.interpreter_path,
                "-I",
                "-S",
                "tools/sbom-tool",
                "scan",
                f"oci-manifest:{manifest_digest}",
                "--output",
                "spdx-json=reports/final-image.sbom.json",
                "--packaging-authority-sha256",
                packaging_binding,
            ],
            "python": {
                "implementation": "CPython",
                "version": "3.12.13",
                "path": self.interpreter_path,
                "resolved_path": self.interpreter_path,
                "site_packages": self.interpreter_site_packages,
                "sha256": sha256(self.interpreter_bytes),
            },
            "packaging": packaging_authority,
            "modules": [
                {
                    "path": path,
                    "sha256": sha256((self.export / path).read_bytes()),
                }
                for path in (
                    "ops/release/generate_release_evidence.py",
                    "ops/release/license_provenance.py",
                )
            ],
            "format": "spdx-json",
            "input_manifest_digest": manifest_digest,
            "dockerfile_sha256": sha256((self.export / "Dockerfile").read_bytes()),
            "context": {"sha256": context_sha, "files": context_records},
            "base_image": {
                "reference": self.base_reference,
                "manifest_digest": self.base_manifest_digest,
                "config_digest": self.base_config_digest,
                "layers": [base_layer_digest],
                "diff_ids": [base_layer_digest],
            },
            "uv_image": {
                "reference": self.uv_reference,
                "manifest_digest": self.uv_manifest_digest,
                "config_digest": self.uv_config_digest,
                "layers": [self.uv_layer_digest],
                "diff_ids": [self.uv_layer_digest],
            },
            "inputs": inputs,
            "outputs": [
                {"path": path, "sha256": sha256((self.artifacts / path).read_bytes())}
                for path in (
                    "reports/final-image.sbom.json",
                    "reports/uv-install.json",
                    "reports/final-image-files.json",
                )
            ],
        }
        attestation_data = (json.dumps(attestation, indent=2, sort_keys=True) + "\n").encode()
        write(self.artifacts / "reports/sbom-attestation.json", attestation_data)
        image_root = self.artifacts / "image"
        write_json(image_root / "oci-layout", {"imageLayoutVersion": "1.0.0"})
        for digest_value, data in (
            (config_digest, config_data),
            (base_layer_digest, self.base_layer_data),
            (layer_digest, layer_data),
            (manifest_digest, manifest_data),
        ):
            write(image_root / "blobs/sha256" / digest_value.split(":", 1)[1], data)
        write_json(
            image_root / "index.json",
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": manifest_digest,
                        "size": len(manifest_data),
                    }
                ],
            },
        )
        write_json(
            self.export / "compliance/final-image-spdx.json",
            {
                "schema_version": 4,
                "oci_layout": "image",
                "base_oci_layout": "base-image",
                "uv_oci_layout": "uv-image",
                "manifest_digest": manifest_digest,
                "base_image": {
                    "reference": self.base_reference,
                    "manifest_digest": self.base_manifest_digest,
                    "config_digest": self.base_config_digest,
                    "layers": [base_layer_digest],
                    "diff_ids": [base_layer_digest],
                },
                "uv_image": {
                    "reference": self.uv_reference,
                    "manifest_digest": self.uv_manifest_digest,
                    "config_digest": self.uv_config_digest,
                    "layers": [self.uv_layer_digest],
                    "diff_ids": [self.uv_layer_digest],
                },
                "external_sbom": {
                    "path": "reports/final-image.sbom.json",
                    "sha256": sha256(sbom_data),
                },
                "sbom_attestation": {
                    "path": "reports/sbom-attestation.json",
                    "sha256": sha256(attestation_data),
                },
                "uv_install_report": {
                    "path": "reports/uv-install.json",
                    "sha256": sha256(uv_report_data),
                },
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
                "packages": image_packages + [item["coordinate"] for item in all_components],
                "layers": [base_layer_digest, layer_digest],
                "interpreter": {
                    "implementation": "CPython",
                    "version": "3.12.13",
                    "path": self.interpreter_path,
                    "resolved_path": self.interpreter_path,
                    "site_packages": self.interpreter_site_packages,
                    "sha256": sha256(self.interpreter_bytes),
                },
                "files": [
                    {"path": path, "sha256": sha256(data)}
                    for path, data in sorted(image_files.items())
                ],
                "entries": _oci_entry_records(_oci_snapshot([self.base_layer_data, layer_data])),
            },
        )

    def registry(self) -> dict:
        return json.loads((self.export / "compliance/dependency-evidence.json").read_text())

    def save_registry(self, value: dict) -> None:
        write_json(self.export / "compliance/dependency-evidence.json", value)

    def external_sbom(self) -> tuple[Path, dict]:
        inventory = json.loads((self.export / "compliance/final-image-spdx.json").read_text())
        path = self.artifacts / inventory["external_sbom"]["path"]
        return path, json.loads(path.read_text())

    def save_external_sbom(self, value: dict) -> None:
        inventory_path = self.export / "compliance/final-image-spdx.json"
        inventory = json.loads(inventory_path.read_text())
        path = self.artifacts / inventory["external_sbom"]["path"]
        write_json(path, value)
        inventory["external_sbom"]["sha256"] = sha256(path.read_bytes())
        self._refresh_attestation(inventory)
        write_json(inventory_path, inventory)

    def uv_report(self) -> tuple[Path, dict]:
        inventory = json.loads((self.export / "compliance/final-image-spdx.json").read_text())
        path = self.artifacts / inventory["uv_install_report"]["path"]
        return path, json.loads(path.read_text())

    def save_uv_report(self, value: dict) -> None:
        inventory_path = self.export / "compliance/final-image-spdx.json"
        inventory = json.loads(inventory_path.read_text())
        path = self.artifacts / inventory["uv_install_report"]["path"]
        write_json(path, value)
        inventory["uv_install_report"]["sha256"] = sha256(path.read_bytes())
        self._refresh_attestation(inventory)
        write_json(inventory_path, inventory)

    def _refresh_attestation(self, inventory: dict) -> None:
        attestation_path = self.artifacts / inventory["sbom_attestation"]["path"]
        attestation = json.loads(attestation_path.read_text())
        manifest_digest = inventory["manifest_digest"]
        attestation["input_manifest_digest"] = manifest_digest
        attestation["invocation"] = [
            self.interpreter_path,
            "-I",
            "-S",
            "tools/sbom-tool",
            "scan",
            f"oci-manifest:{manifest_digest}",
            "--output",
            "spdx-json=reports/final-image.sbom.json",
            "--packaging-authority-sha256",
            sha256(
                json.dumps(attestation["packaging"], separators=(",", ":"), sort_keys=True).encode()
            ),
        ]
        manifest_input = next(
            item for item in attestation["inputs"] if item["path"].startswith("image/blobs/")
        )
        manifest_input["path"] = f"image/blobs/sha256/{manifest_digest[7:]}"
        manifest_input["sha256"] = manifest_digest[7:]
        context_records = []
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
            context_records.append(
                {"path": path, "sha256": sha256((self.export / path).read_bytes())}
            )
        for root in ("backend", "fixtures", "frontend", "migrations", "third_party"):
            for path in sorted(item for item in self.export.rglob("*") if item.is_file()):
                relative = path.relative_to(self.export).as_posix()
                if relative == root or relative.startswith(root + "/"):
                    context_records.append({"path": relative, "sha256": sha256(path.read_bytes())})
        attestation["context"] = {
            "sha256": sha256(
                json.dumps(context_records, separators=(",", ":"), sort_keys=True).encode()
            ),
            "files": context_records,
        }
        for output in attestation["outputs"]:
            output["sha256"] = sha256((self.artifacts / output["path"]).read_bytes())
        write_json(attestation_path, attestation)
        inventory["sbom_attestation"]["sha256"] = sha256(attestation_path.read_bytes())

    def add_image_file(self, path: str, data: bytes) -> None:
        inventory_path = self.export / "compliance/final-image-spdx.json"
        inventory = json.loads(inventory_path.read_text())
        layout = self.artifacts / inventory["oci_layout"]
        manifest_path = layout / "blobs/sha256" / inventory["manifest_digest"][7:]
        manifest = json.loads(manifest_path.read_text())
        old_layers = [
            (layout / "blobs/sha256" / descriptor["digest"][7:]).read_bytes()
            for descriptor in manifest["layers"]
        ]
        files = _oci_files(old_layers)
        files[path] = data
        changed = {
            item: value for item, value in files.items() if self.base_files.get(item) != value
        }
        self.replace_image_layers([layer(changed)], files)

    def replace_image_layers(
        self, layer_data_items: list[bytes], final_files: dict[str, bytes]
    ) -> None:
        if not layer_data_items or layer_data_items[0] != self.base_layer_data:
            layer_data_items = [self.base_layer_data, *layer_data_items]
        inventory_path = self.export / "compliance/final-image-spdx.json"
        inventory = json.loads(inventory_path.read_text())
        layout = self.artifacts / inventory["oci_layout"]
        old_manifest_path = layout / "blobs/sha256" / inventory["manifest_digest"][7:]
        manifest = json.loads(old_manifest_path.read_text())
        descriptors = []
        for data in layer_data_items:
            digest_value = "sha256:" + sha256(data)
            write(layout / "blobs/sha256" / digest_value[7:], data)
            descriptors.append(
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": digest_value,
                    "size": len(data),
                }
            )
        manifest["layers"] = descriptors
        config_data = (
            json.dumps(
                {
                    "architecture": "amd64",
                    "os": "linux",
                    "created": "2026-08-29T00:00:00Z",
                    "rootfs": {
                        "type": "layers",
                        "diff_ids": ["sha256:" + sha256(data) for data in layer_data_items],
                    },
                    "config": {
                        "Labels": {
                            "org.alphadecay.base": self.base_reference,
                            "org.alphadecay.glibc": "2.36",
                        }
                    },
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        config_digest = "sha256:" + sha256(config_data)
        write(layout / "blobs/sha256" / config_digest[7:], config_data)
        manifest["config"].update(digest=config_digest, size=len(config_data))
        manifest_data = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
        manifest_digest = "sha256:" + sha256(manifest_data)
        write(layout / "blobs/sha256" / manifest_digest[7:], manifest_data)
        index = json.loads((layout / "index.json").read_text())
        index["manifests"][0].update(digest=manifest_digest, size=len(manifest_data))
        write_json(layout / "index.json", index)
        sbom_path, sbom = self.external_sbom()
        sbom["documentNamespace"] = (
            "https://alphadecay.dev/spdx/final-image/" + manifest_digest.removeprefix("sha256:")
        )
        sbom["alphadecay"]["subject_manifest_digest"] = manifest_digest
        write_json(sbom_path, sbom)
        inventory["external_sbom"]["sha256"] = sha256(sbom_path.read_bytes())
        inventory["manifest_digest"] = manifest_digest
        inventory["layers"] = [item["digest"] for item in descriptors]
        inventory["files"] = [
            {"path": path, "sha256": sha256(data)} for path, data in sorted(final_files.items())
        ]
        inventory["entries"] = _oci_entry_records(_oci_snapshot(layer_data_items))
        write_json(
            self.artifacts / "reports/final-image-files.json",
            {
                "manifest_digest": manifest_digest,
                "layers": inventory["layers"],
                "files": inventory["files"],
                "entries": inventory["entries"],
            },
        )
        self._refresh_attestation(inventory)
        write_json(inventory_path, inventory)


class LicenseProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def verify(self):
        return verify_and_generate(self.fixture.source, self.fixture.export, self.fixture.artifacts)

    def assert_rejected(self, token: str) -> None:
        with self.assertRaisesRegex(ComplianceError, token):
            self.verify()

    def add_ofl_fonts(
        self,
        *,
        suffixes: tuple[str, ...] = ("woff", "woff2"),
        unused_entry: bool = False,
    ) -> dict[str, object]:
        directory = "frontend/src/assets/fonts"
        revision = "a" * 40
        repository_url = "https://github.com/IBM/plex"
        license_path = f"{directory}/OFL.txt"
        license_data = b"SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007\nFixture terms.\n"
        write(self.fixture.export / license_path, license_data)
        license_source = {
            "release_path": license_path,
            "sha256": sha256(license_data),
            "upstream_path": "LICENSE.txt",
            "url": (f"https://raw.githubusercontent.com/IBM/plex/{revision}/LICENSE.txt"),
        }
        font_sources = []
        font_data: dict[str, bytes] = {}
        for suffix in suffixes:
            name = f"IBMPlexSans-Regular.{suffix}"
            path = f"{directory}/{name}"
            data = f"fixture-{suffix}".encode()
            write(self.fixture.export / path, data)
            upstream_path = f"packages/plex-sans/fonts/complete/{suffix}/{name}"
            font_data[path] = data
            font_sources.append(
                {
                    "release_path": path,
                    "sha256": sha256(data),
                    "upstream_path": upstream_path,
                    "url": (
                        f"https://raw.githubusercontent.com/IBM/plex/{revision}/{upstream_path}"
                    ),
                }
            )
        if unused_entry:
            unused_path = f"{directory}/IBMPlexSans-Unused.woff2"
            upstream_path = "packages/plex-sans/fonts/complete/woff2/IBMPlexSans-Unused.woff2"
            font_sources.append(
                {
                    "release_path": unused_path,
                    "sha256": "b" * 64,
                    "upstream_path": upstream_path,
                    "url": (
                        f"https://raw.githubusercontent.com/IBM/plex/{revision}/{upstream_path}"
                    ),
                }
            )
        provenance_path = f"{directory}/IBM-Plex.font-provenance.json"
        write_json(
            self.fixture.export / provenance_path,
            {
                "schema_version": 1,
                "repository_url": repository_url,
                "source_revision": revision,
                "license": license_source,
                "fonts": font_sources,
            },
        )
        provenance_sha256 = sha256((self.fixture.export / provenance_path).read_bytes())
        registry = self.fixture.registry()
        for path, data in font_data.items():
            registry["assets"].append(
                {
                    "path": path,
                    "sha256": sha256(data),
                    "origin": f"{repository_url} at {revision}",
                    "creation_method": "unmodified upstream font",
                    "license_expression": "OFL-1.1",
                    "ofl_evidence": {
                        "license_path": license_path,
                        "license_sha256": sha256(license_data),
                        "provenance_path": provenance_path,
                        "provenance_sha256": provenance_sha256,
                    },
                }
            )
        self.fixture.save_registry(registry)
        return {
            "font_paths": tuple(font_data),
            "license_path": license_path,
            "license_sha256": sha256(license_data),
            "provenance_path": provenance_path,
            "provenance_sha256": provenance_sha256,
            "repository_url": repository_url,
            "revision": revision,
        }

    def rewrite_ofl_manifest(
        self,
        evidence: dict[str, object],
        mutate: Callable[[dict[str, object]], None],
    ) -> None:
        path = self.fixture.export / str(evidence["provenance_path"])
        manifest = json.loads(path.read_text())
        mutate(manifest)
        write_json(path, manifest)
        digest = sha256(path.read_bytes())
        registry = self.fixture.registry()
        for asset in registry["assets"]:
            if asset.get("license_expression") == "OFL-1.1":
                asset["ofl_evidence"]["provenance_sha256"] = digest
        self.fixture.save_registry(registry)

    def test_generates_archive_and_oci_bound_provenance(self) -> None:
        result = self.verify()
        self.assertEqual(
            (result.python_runtime, result.mcp_runtime, result.frontend_runtime, result.build_only),
            (3, 2, 5, 2),
        )
        provenance = json.loads(
            (self.fixture.export / "compliance/release-provenance.json").read_text()
        )
        self.assertEqual(provenance["schema_version"], 3)
        self.assertIn("oci_manifest_digest", provenance)
        self.assertTrue(all("archive_members" in item for item in provenance["packages"]))
        self.assertIn(
            "node:bundle-tool@6.0.0#node_modules/bundle-tool",
            provenance["inventories"]["build-only"],
        )
        self.assertNotIn(
            "node:bundle-tool@6.0.0#node_modules/bundle-tool",
            provenance["inventories"]["frontend-runtime"],
        )

    def test_self_asserted_license_field_is_rejected(self) -> None:
        registry = self.fixture.registry()
        registry["packages"][0]["license_expression"] = "MIT"
        self.fixture.save_registry(registry)
        self.assert_rejected("SELF_ASSERTED_LICENSE")

    def test_every_shipped_frontend_asset_requires_license_evidence(self) -> None:
        write(self.fixture.export / "frontend/src/unreviewed.svg", "<svg/>\n")

        with self.assertRaisesRegex(ComplianceError, "public-export path mismatch"):
            _assets(self.fixture.export, self.fixture.registry()["assets"])

    def test_open_font_assets_bind_woff_and_woff2_evidence_and_notice_once(self) -> None:
        evidence = self.add_ofl_fonts()

        reviewed = _assets(self.fixture.export, self.fixture.registry()["assets"])
        fonts = [item for item in reviewed if item["license_expression"] == "OFL-1.1"]
        self.assertEqual({Path(item["path"]).suffix for item in fonts}, {".woff", ".woff2"})
        self.assertTrue(
            all(
                item["ofl_evidence"]["source_revision"] == evidence["revision"]
                and item["ofl_evidence"]["license_sha256"] == evidence["license_sha256"]
                and item["ofl_evidence"]["provenance_sha256"] == evidence["provenance_sha256"]
                for item in fonts
            )
        )
        notice = _notice(self.fixture.export, [], [], [], reviewed)
        self.assertEqual(notice.count("SIL OPEN FONT LICENSE Version 1.1"), 1)
        self.assertIn(str(evidence["repository_url"]), notice)
        self.assertIn(str(evidence["revision"]), notice)
        for path in evidence["font_paths"]:
            self.assertIn(path, notice)

    def test_unregistered_woff_and_woff2_assets_are_rejected(self) -> None:
        paths = (
            "frontend/src/assets/fonts/unregistered.woff",
            "frontend/src/assets/fonts/unregistered.woff2",
        )
        for path in paths:
            write(self.fixture.export / path, b"unregistered")

        with self.assertRaisesRegex(ComplianceError, "public-export path mismatch") as raised:
            _assets(self.fixture.export, self.fixture.registry()["assets"])
        for path in paths:
            self.assertIn(path, str(raised.exception))

    def test_ofl_is_rejected_for_non_font_asset(self) -> None:
        registry = self.fixture.registry()
        registry["assets"][0]["license_expression"] = "OFL-1.1"

        with self.assertRaisesRegex(ComplianceError, "restricted to WOFF and WOFF2"):
            _assets(self.fixture.export, registry["assets"])

    def test_ofl_is_rejected_for_package_archive(self) -> None:
        registry = self.fixture.registry()
        package = next(
            item
            for item in registry["packages"]
            if item["coordinate"] == "python:runtime-lib==2.0.0"
        )
        data, _ = wheel("runtime-lib", "2.0.0", "OFL-1.1")
        write(self.fixture.artifacts / package["artifact"]["path"], data)
        old_integrity = package["artifact"]["integrity"]
        new_integrity = "sha256:" + sha256(data)
        package["artifact"]["integrity"] = new_integrity
        for tree in (self.fixture.source, self.fixture.export):
            lock = tree / "uv.lock"
            write(lock, lock.read_text().replace(old_integrity, new_integrity))
        registry["lockfiles"]["uv.lock"] = sha256((self.fixture.export / "uv.lock").read_bytes())
        self.fixture.save_registry(registry)

        self.assert_rejected("incompatible license")

    def test_ofl_font_rejects_missing_or_drifted_license(self) -> None:
        evidence = self.add_ofl_fonts(suffixes=("woff2",))
        license_path = self.fixture.export / str(evidence["license_path"])
        license_path.unlink()
        with self.assertRaisesRegex(ComplianceError, "OFL license is missing"):
            _assets(self.fixture.export, self.fixture.registry()["assets"])

        write(license_path, b"changed")
        with self.assertRaisesRegex(ComplianceError, "OFL license hash drift"):
            _assets(self.fixture.export, self.fixture.registry()["assets"])

    def test_ofl_font_rejects_missing_or_drifted_provenance(self) -> None:
        evidence = self.add_ofl_fonts(suffixes=("woff2",))
        provenance_path = self.fixture.export / str(evidence["provenance_path"])
        original = provenance_path.read_bytes()
        provenance_path.unlink()
        with self.assertRaisesRegex(ComplianceError, "OFL font provenance is missing"):
            _assets(self.fixture.export, self.fixture.registry()["assets"])

        write(provenance_path, original + b"changed")
        with self.assertRaisesRegex(ComplianceError, "OFL font provenance hash drift"):
            _assets(self.fixture.export, self.fixture.registry()["assets"])

    def test_ofl_font_rejects_unused_provenance_entries(self) -> None:
        self.add_ofl_fonts(suffixes=("woff2",), unused_entry=True)

        with self.assertRaisesRegex(ComplianceError, "unused OFL font provenance entries"):
            _assets(self.fixture.export, self.fixture.registry()["assets"])

    def test_ofl_font_rejects_mutable_source_revision(self) -> None:
        evidence = self.add_ofl_fonts(suffixes=("woff2",))
        self.rewrite_ofl_manifest(
            evidence,
            lambda manifest: manifest.__setitem__("source_revision", "main"),
        )

        with self.assertRaisesRegex(ComplianceError, "source revision is not immutable"):
            _assets(self.fixture.export, self.fixture.registry()["assets"])

    def test_ofl_font_rejects_mutable_source_url(self) -> None:
        evidence = self.add_ofl_fonts(suffixes=("woff2",))

        def mutate(manifest: dict[str, object]) -> None:
            fonts = cast(list[dict[str, object]], manifest["fonts"])
            fonts[0]["url"] = "https://github.com/IBM/plex/raw/main/font.woff2"

        self.rewrite_ofl_manifest(evidence, mutate)

        with self.assertRaisesRegex(ComplianceError, "source URL is not immutable"):
            _assets(self.fixture.export, self.fixture.registry()["assets"])

    def test_ofl_font_rejects_manifest_font_hash_drift(self) -> None:
        evidence = self.add_ofl_fonts(suffixes=("woff2",))

        def mutate(manifest: dict[str, object]) -> None:
            fonts = cast(list[dict[str, object]], manifest["fonts"])
            fonts[0]["sha256"] = "c" * 64

        self.rewrite_ofl_manifest(evidence, mutate)

        with self.assertRaisesRegex(ComplianceError, "manifest does not bind the font bytes"):
            _assets(self.fixture.export, self.fixture.registry()["assets"])

    def test_ofl_font_rejects_unreferenced_provenance_file(self) -> None:
        write_json(
            self.fixture.export / "frontend/src/assets/fonts/orphan.font-provenance.json",
            {"schema_version": 1},
        )

        with self.assertRaisesRegex(ComplianceError, "unused OFL font provenance files"):
            _assets(self.fixture.export, self.fixture.registry()["assets"])

    def test_mpl_source_member_must_be_retained_with_the_release(self) -> None:
        coordinate = "python:mpl-fixture==1.0.0"
        data, members = wheel("mpl-fixture", "1.0.0", "MPL-2.0")
        artifact_path = "archives/mpl_fixture-1.0.0-py3-none-any.whl"
        write(self.fixture.artifacts / artifact_path, data)
        metadata_member = "mpl_fixture-1.0.0.dist-info/METADATA"
        license_member = "mpl_fixture-1.0.0.dist-info/licenses/LICENSE"
        metadata_path = "third_party/evidence/mpl-fixture/metadata"
        license_path = "third_party/evidence/mpl-fixture/LICENSE"
        write(self.fixture.export / metadata_path, members[metadata_member])
        write(self.fixture.export / license_path, members[license_member])
        locator = "https://example.invalid/mpl_fixture-1.0.0-py3-none-any.whl"
        integrity = "sha256:" + sha256(data)
        evidence = {
            "coordinate": coordinate,
            "artifact": {
                "locator": locator,
                "integrity": integrity,
                "path": artifact_path,
            },
            "retained_files": [
                {
                    "archive_path": metadata_member,
                    "path": metadata_path,
                    "sha256": sha256(members[metadata_member]),
                },
                {
                    "archive_path": license_member,
                    "path": license_path,
                    "sha256": sha256(members[license_member]),
                },
            ],
            "source_members": ["mpl_fixture/__init__.py"],
            "review_disposition": "approved",
        }

        with self.assertRaisesRegex(ComplianceError, "retained source form"):
            _package_evidence(
                self.fixture.export,
                self.fixture.artifacts,
                [evidence],
                {coordinate: frozenset({(locator, integrity)})},
                {coordinate: {"python-runtime"}},
            )

    def test_target_optional_and_required_peer_packages_require_evidence(self) -> None:
        for coordinate in (
            "node:optional-lib@5.2.0",
            "node:peer-lib@5.3.0",
            "node:root-optional@5.4.0",
        ):
            with self.subTest(coordinate=coordinate):
                registry = self.fixture.registry()
                registry["packages"] = [
                    item for item in registry["packages"] if item["coordinate"] != coordinate
                ]
                self.fixture.save_registry(registry)
                self.assert_rejected("lack archive evidence")
                self.fixture._write_export()

    def test_root_required_peer_is_part_of_runtime_closure(self) -> None:
        for tree in (self.fixture.source, self.fixture.export):
            path = tree / "package-lock.json"
            lock = json.loads(path.read_text())
            lock["packages"][""]["peerDependencies"] = {"orphan-tool": "9.0.0"}
            write_json(path, lock)
        registry = self.fixture.registry()
        registry["lockfiles"]["package-lock.json"] = sha256(
            (self.fixture.export / "package-lock.json").read_bytes()
        )
        self.fixture.save_registry(registry)
        self.assert_rejected("lack archive evidence")

    def test_node_identity_keeps_lock_path_and_integrity_distinct(self) -> None:
        lock = {
            "packages": {
                "": {
                    "dependencies": {"left": "1"},
                    "devDependencies": {"right": "1"},
                },
                "node_modules/left": {
                    "version": "1",
                    "resolved": "https://example.invalid/left.tgz",
                    "integrity": "sha512-left",
                    "dependencies": {"shared": "1"},
                },
                "node_modules/left/node_modules/shared": {
                    "version": "1",
                    "resolved": "https://example.invalid/shared-a.tgz",
                    "integrity": "sha512-a",
                },
                "node_modules/right": {
                    "version": "1",
                    "resolved": "https://example.invalid/right.tgz",
                    "integrity": "sha512-right",
                    "dependencies": {"shared": "1"},
                },
                "node_modules/right/node_modules/shared": {
                    "version": "1",
                    "resolved": "https://example.invalid/shared-b.tgz",
                    "integrity": "sha512-b",
                },
            }
        }
        _, runtime, build = _load_node_lock(json.dumps(lock).encode())
        self.assertIn("node:shared@1#node_modules/left/node_modules/shared", runtime)
        self.assertIn("node:shared@1#node_modules/right/node_modules/shared", build)
        self.assertTrue(runtime.isdisjoint(build))

    def test_installed_optional_peer_is_in_runtime_closure(self) -> None:
        for tree in (self.fixture.source, self.fixture.export):
            path = tree / "package-lock.json"
            lock = json.loads(path.read_text())
            lock["packages"][""]["peerDependencies"] = {"orphan-tool": "9.0.0"}
            lock["packages"][""]["peerDependenciesMeta"] = {"orphan-tool": {"optional": True}}
            write_json(path, lock)
        registry = self.fixture.registry()
        registry["lockfiles"]["package-lock.json"] = sha256(
            (self.fixture.export / "package-lock.json").read_bytes()
        )
        self.fixture.save_registry(registry)
        self.assert_rejected("lack archive evidence")

    def test_node_lock_filters_packages_for_the_target_libc(self) -> None:
        lock = {
            "lockfileVersion": 3,
            "packages": {
                "": {
                    "dependencies": {"portable": "1.0.0"},
                },
                "node_modules/portable": {
                    "version": "1.0.0",
                    "resolved": "https://example.invalid/portable.tgz",
                    "integrity": "sha512-portable",
                    "optionalDependencies": {
                        "gnu-addon": "1.0.0",
                        "musl-addon": "1.0.0",
                    },
                },
                "node_modules/gnu-addon": {
                    "version": "1.0.0",
                    "resolved": "https://example.invalid/gnu.tgz",
                    "integrity": "sha512-gnu",
                    "libc": ["glibc"],
                },
                "node_modules/musl-addon": {
                    "version": "1.0.0",
                    "resolved": "https://example.invalid/musl.tgz",
                    "integrity": "sha512-musl",
                    "libc": ["musl"],
                },
            },
        }

        packages, runtime, _ = _load_node_lock(json.dumps(lock).encode())

        self.assertIn("node_modules/gnu-addon", packages)
        self.assertNotIn("node_modules/musl-addon", packages)
        self.assertIn("node:gnu-addon@1.0.0#node_modules/gnu-addon", runtime)

    def test_node_archive_discovers_one_safe_package_root(self) -> None:
        members = {
            "release-root/package.json": json.dumps(
                {"name": "demo", "version": "1.0.0", "license": "MIT"}
            ).encode(),
            "release-root/LICENSE": b"MIT license\n",
            "release-root/index.js": b"module.exports = {};\n",
        }

        expression, metadata, material, _ = _archive_conclusion("node:demo@1.0.0", members)

        self.assertEqual(expression, "MIT")
        self.assertEqual(metadata, "release-root/package.json")
        self.assertEqual(material, {"release-root/LICENSE"})

    def test_node_archive_rejects_multiple_package_roots(self) -> None:
        members = {
            "first/package.json": json.dumps(
                {"name": "demo", "version": "1.0.0", "license": "MIT"}
            ).encode(),
            "first/LICENSE": b"MIT license\n",
            "second/package.json": json.dumps(
                {"name": "nested", "version": "1.0.0", "license": "MIT"}
            ).encode(),
        }

        with self.assertRaisesRegex(ComplianceError, "ambiguous package metadata"):
            _archive_conclusion("node:demo@1.0.0", members)

    def test_commented_docker_instructions_have_no_authority(self) -> None:
        for tree in (self.fixture.source, self.fixture.export):
            path = tree / "Dockerfile"
            original = path.read_text()
            write(path, "FROM scratch\n" + "".join(f"# {line}\n" for line in original.splitlines()))
        self.assert_rejected("application stage does not use the pinned base")

    def test_legacy_python_license_is_derived_from_archive_classifier(self) -> None:
        data, _ = wheel("legacy-lib", "1.0.0")
        source = io.BytesIO(data)
        output = io.BytesIO()
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
            for info in original.infolist():
                value = original.read(info)
                if info.filename.endswith("METADATA"):
                    value = value.replace(b"License-Expression: MIT\n", b"") + (
                        b"Classifier: License :: OSI Approved :: MIT License\n"
                    )
                changed.writestr(info, value)
        members = _archive_members(
            output.getvalue(),
            "python:legacy-lib==1.0.0",
            "legacy_lib-1.0.0-py3-none-any.whl",
        )
        expression, _, _, _ = _archive_conclusion("python:legacy-lib==1.0.0", members)
        self.assertEqual(expression, "MIT")

    def test_python_license_expression_supersedes_generic_classifier(self) -> None:
        data, _ = wheel("modern-bsd", "1.0.0", "BSD-3-Clause")
        source = io.BytesIO(data)
        output = io.BytesIO()
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
            for info in original.infolist():
                value = original.read(info)
                if info.filename.endswith("METADATA"):
                    value += b"Classifier: License :: OSI Approved :: BSD License\n"
                changed.writestr(info, value)
        members = _archive_members(
            output.getvalue(),
            "python:modern-bsd==1.0.0",
            "modern_bsd-1.0.0-py3-none-any.whl",
        )

        expression, _, _, _ = _archive_conclusion("python:modern-bsd==1.0.0", members)

        self.assertEqual(expression, "BSD-3-Clause")

    def test_audited_python_license_expressions_are_supported(self) -> None:
        for expression in (
            "0BSD",
            "Zlib",
            "MIT AND PSF-2.0",
            "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
        ):
            data, _ = wheel("permissive-lib", "1.0.0", expression)
            members = _archive_members(
                data,
                "python:permissive-lib==1.0.0",
                "permissive_lib-1.0.0-py3-none-any.whl",
            )
            with self.subTest(expression=expression):
                conclusion, _, _, _ = _archive_conclusion("python:permissive-lib==1.0.0", members)
                self.assertEqual(conclusion, expression)

    def test_audited_legacy_license_alias_is_normalized(self) -> None:
        data, _ = wheel("sseclient-py", "1.9.0")
        source = io.BytesIO(data)
        output = io.BytesIO()
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
            for info in original.infolist():
                value = original.read(info)
                if info.filename.endswith("METADATA"):
                    value = value.replace(
                        b"License-Expression: MIT\n",
                        b"License:  Apache   Software License v2  \n",
                    )
                changed.writestr(info, value)
        members = _archive_members(
            output.getvalue(),
            "python:sseclient-py==1.9.0",
            "sseclient_py-1.9.0-py3-none-any.whl",
        )

        expression, _, _, _ = _archive_conclusion("python:sseclient-py==1.9.0", members)

        self.assertEqual(expression, "Apache-2.0")

    def test_legacy_alias_still_requires_retained_license_material(self) -> None:
        members = {
            "sseclient_py-1.9.0.dist-info/METADATA": (
                b"Metadata-Version: 2.1\n"
                b"Name: sseclient-py\n"
                b"Version: 1.9.0\n"
                b"License: Apache Software License v2\n"
            ),
            "sseclient_py/__init__.py": b"\n",
        }

        with self.assertRaisesRegex(ComplianceError, "no license material"):
            _archive_conclusion("python:sseclient-py==1.9.0", members)

    def test_digest_bound_supplement_closes_an_archive_material_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export = root / "export"
            artifacts = root / "artifacts"
            body = b"MIT License\n\nPermission is hereby granted.\n"
            body_hash = sha256(body)
            retained_path = f"third_party/notices/{body_hash}.txt"
            write(export / retained_path, body)
            revision = "1" * 40
            locator = "https://registry.example.invalid/gap-1.0.0.tgz"
            members = {
                "package/package.json": json.dumps(
                    {"license": "MIT", "name": "gap", "version": "1.0.0"}
                ).encode(),
                "package/index.js": b"module.exports = {};\n",
            }
            tar_bytes = io.BytesIO()
            with tarfile.open(fileobj=tar_bytes, mode="w") as archive:
                for path, data in members.items():
                    info = tarfile.TarInfo(path)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            blob = gzip.compress(tar_bytes.getvalue(), mtime=0)
            integrity = "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()
            artifact_path = "packages/gap.tgz"
            write(artifacts / artifact_path, blob)
            metadata_path = "third_party/evidence/gap/package.json"
            write(export / metadata_path, members["package/package.json"])
            write_json(
                export / "third_party/notices/supplemental-licenses.json",
                {
                    "packages": [
                        {
                            "artifacts": [{"integrity": integrity, "locator": locator}],
                            "coordinate": "node:gap@1.0.0",
                            "disposition": "upstream-license-at-release",
                            "license_expression": "MIT",
                            "lock_path": "node_modules/gap",
                            "sources": [
                                {
                                    "path": retained_path,
                                    "revision": revision,
                                    "sha256": body_hash,
                                    "upstream_path": "LICENSE",
                                    "url": (
                                        "https://raw.githubusercontent.com/example/gap/"
                                        f"{revision}/LICENSE"
                                    ),
                                }
                            ],
                        }
                    ],
                    "schema_version": 1,
                },
            )
            identity = "node:gap@1.0.0#node_modules/gap"
            packages = _package_evidence(
                export,
                artifacts,
                [
                    {
                        "artifact": {
                            "integrity": integrity,
                            "locator": locator,
                            "path": artifact_path,
                        },
                        "coordinate": "node:gap@1.0.0",
                        "lock_path": "node_modules/gap",
                        "retained_files": [
                            {
                                "archive_path": "package/package.json",
                                "path": metadata_path,
                                "sha256": sha256(members["package/package.json"]),
                            }
                        ],
                        "review_disposition": "approved",
                        "source_members": [],
                    }
                ],
                {identity: frozenset({(locator, integrity)})},
                {identity: {"build-only"}},
            )

            self.assertEqual(packages[0]["license_expression"], "MIT")
            self.assertEqual(packages[0]["supplemental_license_members"][0]["sha256"], body_hash)

    def test_supplemental_license_registry_rejects_retained_text_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory)
            body = b"license body\n"
            digest = sha256(body)
            path = f"third_party/notices/{digest}.txt"
            write(export / path, b"changed\n")
            revision = "2" * 40
            write_json(
                export / "third_party/notices/supplemental-licenses.json",
                {
                    "packages": [
                        {
                            "artifacts": [
                                {
                                    "integrity": "sha512-value",
                                    "locator": "https://example.invalid/a",
                                }
                            ],
                            "coordinate": "node:gap@1.0.0",
                            "disposition": "upstream-license-at-release",
                            "license_expression": "MIT",
                            "lock_path": "node_modules/gap",
                            "sources": [
                                {
                                    "path": path,
                                    "revision": revision,
                                    "sha256": digest,
                                    "upstream_path": "LICENSE",
                                    "url": (
                                        "https://raw.githubusercontent.com/example/gap/"
                                        f"{revision}/LICENSE"
                                    ),
                                }
                            ],
                        }
                    ],
                    "schema_version": 1,
                },
            )

            with self.assertRaisesRegex(ComplianceError, "retained text drift"):
                _supplemental_licenses(export)

    def test_tracked_supplemental_registry_covers_exact_archive_gaps(self) -> None:
        repository = Path(__file__).resolve().parents[2]

        self.assertEqual(
            set(_supplemental_licenses(repository)),
            {
                "node:@esbuild/linux-x64@0.28.2#node_modules/@esbuild/linux-x64",
                ("node:@napi-rs/lzma-linux-x64-gnu@1.5.1#node_modules/@napi-rs/lzma-linux-x64-gnu"),
                (
                    "node:@rolldown/binding-linux-x64-gnu@1.2.6#"
                    "node_modules/@rolldown/binding-linux-x64-gnu"
                ),
                (
                    "node:@rollup/rollup-linux-x64-gnu@4.63.1#"
                    "node_modules/@rollup/rollup-linux-x64-gnu"
                ),
                "node:decimal.js@10.6.0#node_modules/decimal.js",
                "node:saxes@6.0.0#node_modules/saxes",
                "node:stackback@0.0.2#node_modules/stackback",
                "node:std-env@3.10.0#node_modules/std-env",
                "node:tinyrainbow@2.0.0#node_modules/tinyrainbow",
                "node:tinyspy@4.0.4#node_modules/tinyspy",
                "python:aiofile==3.12.3",
                "python:fastmcp-slim==3.4.0",
            },
        )

    def test_ambiguous_bsd_alias_is_bound_to_audited_packages(self) -> None:
        for name, expected in (
            ("pyasn1-modules", "BSD-2-Clause"),
            ("pyperclip", "BSD-3-Clause"),
        ):
            data, _ = wheel(name, "0.4.2" if name == "pyasn1-modules" else "1.11.0")
            source = io.BytesIO(data)
            output = io.BytesIO()
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
                for info in original.infolist():
                    value = original.read(info)
                    if info.filename.endswith("METADATA"):
                        value = value.replace(b"License-Expression: MIT\n", b"License: BSD\n")
                    changed.writestr(info, value)
            version = "0.4.2" if name == "pyasn1-modules" else "1.11.0"
            coordinate = f"python:{name}=={version}"
            members = _archive_members(
                output.getvalue(), coordinate, f"{name}-{version}-py3-none-any.whl"
            )
            with self.subTest(name=name):
                expression, _, _, _ = _archive_conclusion(coordinate, members)
                self.assertEqual(expression, expected)

        data, _ = wheel("unreviewed-bsd", "1.0.0")
        source = io.BytesIO(data)
        output = io.BytesIO()
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
            for info in original.infolist():
                value = original.read(info)
                if info.filename.endswith("METADATA"):
                    value = value.replace(b"License-Expression: MIT\n", b"License: BSD\n")
                changed.writestr(info, value)
        members = _archive_members(
            output.getvalue(),
            "python:unreviewed-bsd==1.0.0",
            "unreviewed_bsd-1.0.0-py3-none-any.whl",
        )
        with self.assertRaisesRegex(ComplianceError, "unknown legacy"):
            _archive_conclusion("python:unreviewed-bsd==1.0.0", members)

    def test_console_script_accepts_standard_extras_suffix(self) -> None:
        data, _ = wheel("extra-cli", "1.0.0")
        source = io.BytesIO(data)
        output = io.BytesIO()
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
            for info in original.infolist():
                value = original.read(info)
                if info.filename.endswith("entry_points.txt"):
                    value = b"[console_scripts]\nextra-cli = extra_cli:main [speedups]\n"
                changed.writestr(info, value)
        members = _archive_members(
            output.getvalue(),
            "python:extra-cli==1.0.0",
            "extra_cli-1.0.0-py3-none-any.whl",
        )

        _, _, _, scripts = _archive_conclusion("python:extra-cli==1.0.0", members)

        self.assertEqual(scripts, {"extra-cli": "extra_cli:main"})

    def test_console_script_rejects_nonstandard_extras_suffix(self) -> None:
        data, _ = wheel("extra-cli", "1.0.0")
        source = io.BytesIO(data)
        output = io.BytesIO()
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
            for info in original.infolist():
                value = original.read(info)
                if info.filename.endswith("entry_points.txt"):
                    value = b"[console_scripts]\nextra-cli = extra_cli:main [speedups; os]\n"
                changed.writestr(info, value)
        members = _archive_members(
            output.getvalue(),
            "python:extra-cli==1.0.0",
            "extra_cli-1.0.0-py3-none-any.whl",
        )

        with self.assertRaisesRegex(ComplianceError, "console script is invalid"):
            _archive_conclusion("python:extra-cli==1.0.0", members)

    def test_contradictory_and_unknown_python_license_fields_are_unresolved(self) -> None:
        for extra, token in (
            (b"License: BSD-3-Clause\n", "contradictory Python license fields"),
            (b"Classifier: License :: Other/Proprietary License\n", "unrecognized legacy"),
        ):
            data, _ = wheel("conflict-lib", "1.0.0")
            source = io.BytesIO(data)
            output = io.BytesIO()
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
                for info in original.infolist():
                    value = original.read(info)
                    if info.filename.endswith("METADATA"):
                        value += extra
                    changed.writestr(info, value)
            members = _archive_members(
                output.getvalue(),
                "python:conflict-lib==1.0.0",
                "conflict_lib-1.0.0-py3-none-any.whl",
            )
            with self.subTest(extra=extra), self.assertRaisesRegex(ComplianceError, token):
                _archive_conclusion("python:conflict-lib==1.0.0", members)

    def test_python_license_expression_cannot_coexist_with_legacy_license(self) -> None:
        data, _ = wheel("conflict-lib", "1.0.0")
        source = io.BytesIO(data)
        output = io.BytesIO()
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
            for info in original.infolist():
                value = original.read(info)
                if info.filename.endswith("METADATA"):
                    value += b"License: MIT\n"
                changed.writestr(info, value)
        members = _archive_members(
            output.getvalue(),
            "python:conflict-lib==1.0.0",
            "conflict_lib-1.0.0-py3-none-any.whl",
        )

        with self.assertRaisesRegex(ComplianceError, "contradictory Python license fields"):
            _archive_conclusion("python:conflict-lib==1.0.0", members)

    def test_oci_layer_compression_must_match_its_declared_media_type(self) -> None:
        compressed = gzip.compress(layer({"/safe": b"bytes"}), mtime=0)

        with self.assertRaisesRegex(ComplianceError, "compression"):
            _layer_diff_id(
                compressed,
                "application/vnd.oci.image.layer.v1.tar",
                "fixture OCI layer",
            )

    def test_dockerfile_escape_directive_controls_continuations(self) -> None:
        docker = (
            f"# escape=`\nFROM {self.fixture.base_reference} AS application\n"
            "COPY --from=uv /uv /uvx`\n"
            "# ignored continuation comment\n"
            " /bin/\n"
            "COPY backend ./backend\n"
            "RUN uv sync --frozen --no-dev --no-editable`\n"
            " && true\n"
        ).encode()
        authority = _docker_authority(docker)
        self.assertEqual(authority.application_copies[0], "--from=uv /uv /uvx /bin/")
        self.assertEqual(
            authority.application_runs[0],
            "uv sync --frozen --no-dev --no-editable && true",
        )

    def test_dockerfile_rejects_escape_directive_after_an_ordinary_comment(self) -> None:
        docker = (
            "# ordinary comment ends the parser-directive window\n"
            "# escape=`\n"
            f"FROM {self.fixture.base_reference} AS application\n"
            "RUN echo first`\n"
            " && echo second\n"
        ).encode()

        with self.assertRaisesRegex(ComplianceError, "escape directive"):
            _docker_authority(docker)

    def test_debian_file_patterns_select_specific_applicable_license(self) -> None:
        body = (
            b"Format: https://www.debian.org/doc/packaging-manuals/"
            b"copyright-format/1.0/\n\n"
            b"Files: *\nCopyright: General\nLicense: Expat\n\n"
            b"Files: usr/lib/special/*\nCopyright: Special\nLicense: BSD-3-clause\n\n"
            b"License: Expat\n MIT terms.\n\n"
            b"License: BSD-3-clause\n BSD terms.\n"
        )
        self.assertEqual(
            _debian_license(
                body,
                "deb:fixture:amd64==1",
                ("usr/lib/general.so", "usr/lib/special/tool.so"),
            ),
            "BSD-3-Clause AND MIT",
        )

    def test_debian_file_patterns_use_the_last_matching_paragraph(self) -> None:
        body = (
            b"Format: https://www.debian.org/doc/packaging-manuals/"
            b"copyright-format/1.0/\n\n"
            b"Files: usr/lib/special/*\nCopyright: Special\nLicense: BSD-3-clause\n\n"
            b"Files: *\nCopyright: General override\nLicense: Expat\n\n"
            b"License: Expat\n MIT terms.\n\n"
            b"License: BSD-3-clause\n BSD terms.\n"
        )
        self.assertEqual(
            _debian_license(body, "deb:fixture:amd64==1", ("usr/lib/special/tool.so",)),
            "MIT",
        )

    def test_caller_cannot_bless_an_arbitrary_generated_console_script(self) -> None:
        backdoor = "/app/.venv/bin/backdoor"
        payload = b"#!/bin/sh\necho unbound\n"
        self.fixture.add_image_file(backdoor, payload)
        inventory = json.loads(
            (self.fixture.export / "compliance/final-image-spdx.json").read_text()
        )
        layout = self.fixture.artifacts / inventory["oci_layout"]
        manifest = json.loads(
            (layout / "blobs/sha256" / inventory["manifest_digest"][7:]).read_text()
        )
        filesystem = _oci_files(
            [
                (layout / "blobs/sha256" / item["digest"][7:]).read_bytes()
                for item in manifest["layers"]
            ]
        )
        record_path = next(
            path for path in filesystem if path.endswith("runtime_lib-2.0.0.dist-info/RECORD")
        )
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
        self.fixture.add_image_file(
            record_path,
            filesystem[record_path]
            + f"../../../bin/backdoor,sha256={digest},{len(payload)}\n".encode(),
        )
        _, report = self.fixture.uv_report()
        installation = next(
            item for item in report["packages"] if item["coordinate"] == "python:runtime-lib==2.0.0"
        )
        installation["generated_paths"] = [backdoor]
        self.fixture.save_uv_report(report)
        self.assert_rejected("self-asserted generated install paths")

    def test_upper_whiteout_cannot_leave_a_materialized_dangling_link(self) -> None:
        lower = layer_members([("/app/target", b"old", "file"), ("/app/link", "target", "symlink")])
        upper = layer({"/app/.wh.target": b""})
        with self.assertRaisesRegex(ComplianceError, "dangling link"):
            _oci_files([lower, upper])

    def test_rewritten_dpkg_database_cannot_claim_application_bytes(self) -> None:
        backdoor = "/app/private-tool"
        payload = b"private runtime\n"
        self.fixture.add_image_file(backdoor, payload)
        inventory = json.loads(
            (self.fixture.export / "compliance/final-image-spdx.json").read_text()
        )
        layout = self.fixture.artifacts / inventory["oci_layout"]
        manifest = json.loads(
            (layout / "blobs/sha256" / inventory["manifest_digest"][7:]).read_text()
        )
        filesystem = _oci_files(
            [
                (layout / "blobs/sha256" / item["digest"][7:]).read_bytes()
                for item in manifest["layers"]
            ]
        )
        list_path = "/var/lib/dpkg/info/runtime-base.list"
        md5_path = "/var/lib/dpkg/info/runtime-base.md5sums"
        self.fixture.add_image_file(list_path, filesystem[list_path] + backdoor.encode() + b"\n")
        self.fixture.add_image_file(
            md5_path,
            filesystem[md5_path]
            + hashlib.md5(payload, usedforsecurity=False).hexdigest().encode()
            + b"  app/private-tool\n",
        )
        self.assert_rejected("changes pinned base-owned system paths")

    def test_image_embedded_claimant_cannot_replace_external_sbom(self) -> None:
        path = self.fixture.export / "compliance/final-image-spdx.json"
        inventory = json.loads(path.read_text())
        inventory.pop("external_sbom")
        inventory["sbom_path"] = "/app/claimant.json"
        write_json(path, inventory)
        self.assert_rejected("external tool-provenanced SBOM")

    def test_duplicate_json_keys_are_rejected_at_every_control_boundary(self) -> None:
        path = self.fixture.export / "compliance/dependency-evidence.json"
        text = path.read_text().replace(
            '"schema_version": 3,', '"schema_version": 3, "schema_version": 3,', 1
        )
        write(path, text)
        self.assert_rejected("duplicate JSON key")

    def test_external_sbom_must_bind_manifest_and_exact_installed_wheel(self) -> None:
        _, sbom = self.fixture.external_sbom()
        sbom["alphadecay"]["subject_manifest_digest"] = "sha256:" + "0" * 64
        self.fixture.save_external_sbom(sbom)
        self.assert_rejected("exact OCI manifest")
        self.fixture._write_export()
        _, report = self.fixture.uv_report()
        report["packages"][0]["artifact_integrity"] = "sha256:" + "0" * 64
        self.fixture.save_uv_report(report)
        self.assert_rejected("installed artifact selection is not exact")

    def test_unowned_runtime_library_is_rejected(self) -> None:
        self.fixture.add_image_file("/usr/lib/libproprietary.so", b"private runtime\n")
        self.assert_rejected("UNDECLARED_RUNTIME_BYTES")

    def test_cross_stage_uv_runtime_cannot_be_omitted(self) -> None:
        inventory = json.loads(
            (self.fixture.export / "compliance/final-image-spdx.json").read_text()
        )
        layout = self.fixture.artifacts / inventory["oci_layout"]
        files = _oci_files(
            (layout / "blobs/sha256" / digest[7:]).read_bytes() for digest in inventory["layers"]
        )
        files.pop("/bin/uvx")
        changed = {
            path: data for path, data in files.items() if self.fixture.base_files.get(path) != data
        }
        self.fixture.replace_image_layers([layer(changed)], files)
        self.assert_rejected("cross-stage uv runtime is incomplete")

    def test_cross_stage_uv_runtime_bytes_require_source_provenance(self) -> None:
        self.fixture.add_image_file("/bin/uv", b"unrelated executable bytes\n")
        self.assert_rejected("cross-stage uv runtime provenance")

    def test_unowned_empty_runtime_directory_is_rejected(self) -> None:
        inventory = json.loads(
            (self.fixture.export / "compliance/final-image-spdx.json").read_text()
        )
        layout = self.fixture.artifacts / inventory["oci_layout"]
        files = _oci_files(
            (layout / "blobs/sha256" / digest[7:]).read_bytes() for digest in inventory["layers"]
        )
        changed = {
            path: data for path, data in files.items() if self.fixture.base_files.get(path) != data
        }
        members = [(path, data, "file") for path, data in changed.items()]
        members.append(("/opt/private", b"", "directory"))
        self.fixture.replace_image_layers([layer_members(members)], files)
        self.assert_rejected("UNDECLARED_RUNTIME_TOPOLOGY")

    def test_duplicate_single_use_python_metadata_header_is_rejected(self) -> None:
        registry = self.fixture.registry()
        package = next(
            item
            for item in registry["packages"]
            if item["coordinate"] == "python:runtime-lib==2.0.0"
        )
        data, _ = wheel("runtime-lib", "2.0.0", duplicate_license=True)
        write(self.fixture.artifacts / package["artifact"]["path"], data)
        old = package["artifact"]["integrity"]
        new = "sha256:" + sha256(data)
        package["artifact"]["integrity"] = new
        for tree in (self.fixture.source, self.fixture.export):
            lock = tree / "uv.lock"
            write(lock, lock.read_text().replace(old, new))
        registry["lockfiles"]["uv.lock"] = sha256((self.fixture.export / "uv.lock").read_bytes())
        self.fixture.save_registry(registry)
        _, sbom = self.fixture.uv_report()
        installation = next(
            item for item in sbom["packages"] if item["coordinate"] == package["coordinate"]
        )
        installation["artifact_integrity"] = new
        self.fixture.save_uv_report(sbom)
        self.assert_rejected("repeats License-Expression")

    def test_incompatible_python_implementation_or_version_wheel_is_rejected(self) -> None:
        for tag in (
            "cp313-abi3-manylinux_2_17_x86_64",
            "pp312-pypy312_pp73-linux_x86_64",
            "cp312-cp312-manylinux_2_37_x86_64",
        ):
            with self.subTest(tag=tag):
                registry = self.fixture.registry()
                package = next(
                    item
                    for item in registry["packages"]
                    if item["coordinate"] == "python:runtime-lib==2.0.0"
                )
                old_url = package["artifact"]["locator"]
                new_url = old_url.rsplit("/", 1)[0] + f"/runtime_lib-2.0.0-{tag}.whl"
                package["artifact"]["locator"] = new_url
                for tree in (self.fixture.source, self.fixture.export):
                    lock = tree / "uv.lock"
                    write(lock, lock.read_text().replace(old_url, new_url))
                registry["lockfiles"]["uv.lock"] = sha256(
                    (self.fixture.export / "uv.lock").read_bytes()
                )
                self.fixture.save_registry(registry)
                self.assert_rejected("does not match its exact lock entry")
                self.fixture._write_locks()
                self.fixture._write_export()

    def test_layer_whiteouts_precede_entries_and_safe_internal_links_resolve(self) -> None:
        inventory = json.loads(
            (self.fixture.export / "compliance/final-image-spdx.json").read_text()
        )
        layout = self.fixture.artifacts / inventory["oci_layout"]
        lower = (layout / "blobs/sha256" / inventory["layers"][-1][7:]).read_bytes()
        final_files = _oci_files([self.fixture.base_layer_data, lower])
        main_path = "/app/backend/app/main.py"
        new_main = b"print('upper safe')\n"
        for tree in (self.fixture.source, self.fixture.export):
            write(tree / "backend/app/main.py", new_main)
        upper = layer_members(
            [
                (main_path, new_main, "file"),
                ("/app/backend/app/.wh.main.py", b"", "file"),
            ]
        )
        final_files[main_path] = new_main
        self.fixture.replace_image_layers([lower, upper], final_files)
        self.verify()
        linked = _oci_files(
            [
                layer_members(
                    [
                        ("/safe/target", b"safe", "file"),
                        ("/safe/link", "target", "symlink"),
                        ("/safe/hard", "safe/target", "hardlink"),
                    ]
                )
            ]
        )
        self.assertEqual(linked["/safe/link"], b"safe")
        self.assertEqual(linked["/safe/hard"], b"safe")

    def test_final_inventory_preserves_directory_symlink_and_hardlink_topology(self) -> None:
        snapshot = _oci_snapshot(
            [
                layer_members(
                    [
                        ("/tools/uv", b"binary", "file"),
                        ("/tools/uvx", "tools/uv", "hardlink"),
                        ("/tools/current", "uv", "symlink"),
                    ]
                )
            ]
        )
        records = {item["path"]: item for item in _oci_entry_records(snapshot)}
        self.assertEqual(records["/tools"]["kind"], "directory")
        self.assertEqual(records["/tools/uv"]["kind"], "file")
        self.assertEqual(records["/tools/uvx"]["kind"], "hardlink")
        self.assertEqual(records["/tools/uvx"]["target"], "tools/uv")
        self.assertEqual(records["/tools/uvx"]["sha256"], sha256(b"binary"))
        self.assertEqual(records["/tools/current"]["kind"], "symlink")
        self.assertEqual(snapshot.files["/tools/current"], b"binary")
        replaced = _oci_snapshot(
            [
                layer_members(
                    [
                        ("/tools/uv", b"old", "file"),
                        ("/tools/uvx", "tools/uv", "hardlink"),
                    ]
                ),
                layer({"/tools/uv": b"new"}),
            ]
        )
        self.assertEqual(replaced.files["/tools/uv"], b"new")
        self.assertEqual(replaced.files["/tools/uvx"], b"old")

    def test_duplicate_layer_member_path_is_rejected(self) -> None:
        inventory = json.loads(
            (self.fixture.export / "compliance/final-image-spdx.json").read_text()
        )
        layout = self.fixture.artifacts / inventory["oci_layout"]
        lower = (layout / "blobs/sha256" / inventory["layers"][-1][7:]).read_bytes()
        duplicate = layer_members(
            [
                ("/app/repeated", b"one", "file"),
                ("/app/repeated", b"two", "file"),
            ]
        )
        with self.assertRaisesRegex(ComplianceError, "repeats member path"):
            self.fixture.replace_image_layers([lower, duplicate], {})

    def test_changed_installed_python_bytes_are_rejected_by_record(self) -> None:
        module_path = "/app/.venv/lib/python3.12/site-packages/runtime_lib/__init__.py"
        record_path = "/app/.venv/lib/python3.12/site-packages/runtime_lib-2.0.0.dist-info/RECORD"
        inventory = json.loads(
            (self.fixture.export / "compliance/final-image-spdx.json").read_text()
        )
        layout = self.fixture.artifacts / inventory["oci_layout"]
        layer_path = layout / "blobs/sha256" / inventory["layers"][-1][7:]
        with tarfile.open(layer_path, mode="r:") as archive:
            record_stream = archive.extractfile(record_path.lstrip("/"))
            assert record_stream is not None
            record_data = record_stream.read()
        changed = b"changed\n"
        encoded = base64.urlsafe_b64encode(hashlib.sha256(changed).digest()).decode().rstrip("=")
        rows = record_data.decode().splitlines()
        rows[0] = f"runtime_lib/__init__.py,sha256={encoded},{len(changed)}"
        changed_record = ("\n".join(rows) + "\n").encode()
        self.fixture.add_image_file(module_path, changed)
        self.fixture.add_image_file(record_path, changed_record)
        self.assert_rejected("selected wheel RECORD")

    def test_arbitrary_app_and_dpkg_control_bytes_are_rejected(self) -> None:
        self.fixture.add_image_file("/app/private-tool", b"private\n")
        self.assert_rejected("UNDECLARED_APP_BYTES")
        self.fixture._write_export()
        self.fixture.add_image_file("/var/lib/dpkg/info/private.list", b"/usr/bin/private\n")
        self.assert_rejected("UNDECLARED_RUNTIME_BYTES")

    def test_swapped_sbom_tool_is_rejected_against_source_policy(self) -> None:
        inventory_path = self.fixture.export / "compliance/final-image-spdx.json"
        inventory = json.loads(inventory_path.read_text())
        attestation_path = self.fixture.artifacts / inventory["sbom_attestation"]["path"]
        attestation = json.loads(attestation_path.read_text())
        swapped = b"different sbom tool\n"
        write(self.fixture.artifacts / attestation["tool"]["path"], swapped)
        attestation["tool"]["sha256"] = sha256(swapped)
        write_json(attestation_path, attestation)
        inventory["sbom_attestation"]["sha256"] = sha256(attestation_path.read_bytes())
        write_json(inventory_path, inventory)
        self.assert_rejected("pinned tool policy")

    def test_sbom_attestation_binds_active_packaging_distribution(self) -> None:
        inventory_path = self.fixture.export / "compliance/final-image-spdx.json"
        inventory = json.loads(inventory_path.read_text())
        attestation_path = self.fixture.artifacts / inventory["sbom_attestation"]["path"]
        attestation = json.loads(attestation_path.read_text())
        attestation["packaging"]["coordinate"] = "python:packaging==0"
        write_json(attestation_path, attestation)
        inventory["sbom_attestation"]["sha256"] = sha256(attestation_path.read_bytes())
        write_json(inventory_path, inventory)

        self.assert_rejected("SBOM attestation")

    def test_interpreter_bytes_are_bound_in_inventory_and_attestation(self) -> None:
        inventory_path = self.fixture.export / "compliance/final-image-spdx.json"
        inventory = json.loads(inventory_path.read_text())
        attestation_path = self.fixture.artifacts / inventory["sbom_attestation"]["path"]
        attestation = json.loads(attestation_path.read_text())
        attestation["python"]["sha256"] = "0" * 64
        write_json(attestation_path, attestation)
        inventory["sbom_attestation"]["sha256"] = sha256(attestation_path.read_bytes())
        write_json(inventory_path, inventory)
        self.assert_rejected("SBOM attestation")

        self.fixture._write_export()
        inventory = json.loads(inventory_path.read_text())
        inventory["interpreter"]["resolved_path"] = "/tmp/substituted-python"
        write_json(inventory_path, inventory)
        self.assert_rejected("final-image interpreter authority drift")

    def test_gpl_copyright_body_cannot_inject_a_permissive_line(self) -> None:
        body = (
            b"Format: https://www.debian.org/doc/packaging-manuals/"
            b"copyright-format/1.0/\n\nFiles: *\nCopyright: Someone\n"
            b"License: GPL-3.0-only\n\nLicense: GPL-3.0-only\n GPL terms.\n"
            b" License: MIT\n"
        )
        self.fixture.add_image_file("/usr/share/doc/runtime-base/LICENSE", body)
        self.assert_rejected("changes pinned base-owned system paths")

    def test_duplicate_dpkg_field_and_record_path_are_rejected(self) -> None:
        status = b"Package: runtime-base\nPackage: runtime-base\nVersion: 1\nLicense: MIT\n"
        self.fixture.add_image_file("/var/lib/dpkg/status", status)
        self.assert_rejected("changes pinned base-owned system paths")
        self.fixture._write_export()
        record_path = "/app/.venv/lib/python3.12/site-packages/runtime_lib-2.0.0.dist-info/RECORD"
        inventory = json.loads(
            (self.fixture.export / "compliance/final-image-spdx.json").read_text()
        )
        layout = self.fixture.artifacts / inventory["oci_layout"]
        layer_path = layout / "blobs/sha256" / inventory["layers"][-1][7:]
        with tarfile.open(layer_path, mode="r:") as archive:
            original = archive.extractfile(record_path.lstrip("/"))
            assert original is not None
            data = original.read()
        first_row = data.splitlines(keepends=True)[0]
        self.fixture.add_image_file(record_path, data + first_row)
        self.assert_rejected("repeats a normalized path")

    def test_dpkg_directory_entries_and_md5sums_are_supported(self) -> None:
        list_path = "/var/lib/dpkg/info/runtime-base.list"
        data = self.fixture.base_files[list_path]
        self.fixture.add_image_file(list_path, b"/usr/lib/x86_64-linux-gnu/\n" + data)
        self.assert_rejected("changes pinned base-owned system paths")

    def test_aggregate_oci_layer_count_is_bounded_before_materialization(self) -> None:
        empty_layer = layer({})
        self.fixture.replace_image_layers([empty_layer] * 129, {})
        self.assert_rejected("aggregate layer-count bound")

    def test_archive_limits_reject_oversized_input_before_expansion(self) -> None:
        registry = self.fixture.registry()
        package = next(
            item
            for item in registry["packages"]
            if item["coordinate"] == "python:runtime-lib==2.0.0"
        )
        artifact_path = self.fixture.artifacts / package["artifact"]["path"]
        with artifact_path.open("wb") as stream:
            stream.truncate(256 * 1024 * 1024 + 1)
        self.assert_rejected("bounded input size")

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "runtime_lib-2.0.0.dist-info/METADATA",
                "Metadata-Version: 2.4\nName: runtime-lib\nVersion: 2.0.0\n"
                "License-Expression: MIT\n",
            )
            archive.writestr(
                "runtime_lib-2.0.0.dist-info/licenses/LICENSE",
                "MIT license for runtime-lib\n",
            )
            with archive.open("runtime_lib/payload.bin", "w") as payload:
                block = b"\0" * (1024 * 1024)
                for _ in range(65):
                    payload.write(block)
        data = output.getvalue()
        write(artifact_path, data)
        old_integrity = package["artifact"]["integrity"]
        new_integrity = "sha256:" + sha256(data)
        package["artifact"]["integrity"] = new_integrity
        for tree in (self.fixture.source, self.fixture.export):
            lock = tree / "uv.lock"
            write(lock, lock.read_text().replace(old_integrity, new_integrity))
        registry["lockfiles"]["uv.lock"] = sha256((self.fixture.export / "uv.lock").read_bytes())
        self.fixture.save_registry(registry)
        self.assert_rejected("archive member is too large")

    def test_zip_member_count_is_bounded_before_loading_member_metadata(self) -> None:
        data, _ = wheel("bounded-lib", "1.0.0")
        with (
            mock.patch("ops.release.license_provenance.MAX_ARCHIVE_MEMBERS", 0),
            mock.patch.object(
                zipfile.ZipFile,
                "infolist",
                side_effect=AssertionError("metadata must not be loaded"),
            ),
            self.assertRaisesRegex(ComplianceError, "too many members"),
        ):
            _archive_members(
                data,
                "python:bounded-lib==1.0.0",
                "bounded_lib-1.0.0-py3-none-any.whl",
            )

    def test_non_archive_wheel_blob_is_rejected_even_when_lock_hash_matches(self) -> None:
        registry = self.fixture.registry()
        package = next(
            item
            for item in registry["packages"]
            if item["coordinate"] == "python:runtime-lib==2.0.0"
        )
        blob = b"not a wheel archive\n"
        write(self.fixture.artifacts / package["artifact"]["path"], blob)
        package["artifact"]["integrity"] = "sha256:" + sha256(blob)
        for tree in (self.fixture.source, self.fixture.export):
            path = tree / "uv.lock"
            write(
                path,
                path.read_text().replace(
                    self.fixture.archives["python:runtime-lib==2.0.0"][1],
                    package["artifact"]["integrity"],
                ),
            )
        registry["lockfiles"]["uv.lock"] = sha256((self.fixture.export / "uv.lock").read_bytes())
        self.fixture.save_registry(registry)
        self.assert_rejected("NON_ARCHIVE_WHEEL_BLOB")

    def test_retained_text_must_match_upstream_archive_member(self) -> None:
        registry = self.fixture.registry()
        package = registry["packages"][0]
        retained = package["retained_files"][1]
        changed = b"self-authored replacement\n"
        write(self.fixture.export / retained["path"], changed)
        retained["sha256"] = sha256(changed)
        self.fixture.save_registry(registry)
        self.assert_rejected("mismatched upstream material")

    def test_unbound_bundle_file_is_rejected(self) -> None:
        write(self.fixture.export / "dist/unlisted.js", "console.log('extra');\n")
        self.assert_rejected("UNBOUND_BUNDLE_FILE")

    def test_symlinked_fixed_control_is_rejected(self) -> None:
        control = self.fixture.export / "compliance/frontend-bundle.json"
        target = self.fixture.export / "bundle-copy.json"
        control.rename(target)
        control.symlink_to(target)
        self.assert_rejected("SYMLINKED_CONTROL")

    def test_unregistered_public_symlink_is_rejected(self) -> None:
        (self.fixture.export / "public/alias.svg").symlink_to("favicon.svg")
        self.assert_rejected("UNREGISTERED_PUBLIC_SYMLINK")

    def test_orphan_node_classification_is_rejected(self) -> None:
        registry = self.fixture.registry()
        coordinate = "node:orphan-tool@9.0.0"
        locator, integrity, archive_path, members = self.fixture.archives[coordinate]
        metadata_member = "package/package.json"
        license_member = "package/LICENSE"
        base = "third_party/evidence/orphan"
        write(self.fixture.export / f"{base}/metadata", members[metadata_member])
        write(self.fixture.export / f"{base}/LICENSE", members[license_member])
        registry["packages"].append(
            {
                "coordinate": coordinate,
                "lock_path": "node_modules/orphan-tool",
                "artifact": {"locator": locator, "integrity": integrity, "path": archive_path},
                "retained_files": [
                    {
                        "archive_path": metadata_member,
                        "path": f"{base}/metadata",
                        "sha256": sha256(members[metadata_member]),
                    },
                    {
                        "archive_path": license_member,
                        "path": f"{base}/LICENSE",
                        "sha256": sha256(members[license_member]),
                    },
                ],
                "source_members": [],
                "review_disposition": "approved",
            }
        )
        self.fixture.save_registry(registry)
        self.assert_rejected("ORPHAN_NODE_CLASSIFICATION")

    def test_image_package_layer_and_filesystem_drift_are_rejected(self) -> None:
        path = self.fixture.export / "compliance/final-image-spdx.json"
        for field, value, token in (
            ("packages", "python:extra==1", "final-image package inventory drift"),
            ("layers", "sha256:" + "0" * 64, "final-image layer inventory drift"),
            (
                "files",
                {"path": "/extra", "sha256": "0" * 64},
                "final-image filesystem inventory drift",
            ),
        ):
            original = json.loads(path.read_text())
            original[field].append(value)
            write_json(path, original)
            self.assert_rejected(token)
            self.fixture._write_export()

    def test_archive_incompatible_license_conclusion_is_rejected(self) -> None:
        registry = self.fixture.registry()
        package = next(
            item
            for item in registry["packages"]
            if item["coordinate"] == "python:runtime-lib==2.0.0"
        )
        data, _ = wheel("runtime-lib", "2.0.0", "GPL-3.0-only")
        write(self.fixture.artifacts / package["artifact"]["path"], data)
        old_integrity = package["artifact"]["integrity"]
        new_integrity = "sha256:" + sha256(data)
        package["artifact"]["integrity"] = new_integrity
        for tree in (self.fixture.source, self.fixture.export):
            lock = tree / "uv.lock"
            write(lock, lock.read_text().replace(old_integrity, new_integrity))
        registry["lockfiles"]["uv.lock"] = sha256((self.fixture.export / "uv.lock").read_bytes())
        self.fixture.save_registry(registry)
        self.assert_rejected("incompatible license")

    def test_stale_lock_hash_and_source_export_mismatch_are_rejected(self) -> None:
        export_lock = self.fixture.export / "uv.lock"
        write(export_lock, export_lock.read_text() + "\n")
        self.assert_rejected("differs from source")
        write(self.fixture.source / "uv.lock", export_lock.read_bytes())
        self.assert_rejected("stale lock hash")

    def test_retained_archive_and_generated_notice_drift_are_rejected(self) -> None:
        registry = self.fixture.registry()
        retained = registry["packages"][0]["retained_files"]
        retained.pop()
        self.fixture.save_registry(registry)
        self.assert_rejected("does not exactly match archive material")
        self.fixture._write_export()
        self.verify()
        write(self.fixture.export / "THIRD_PARTY_NOTICES.md", "stale\n")
        self.assert_rejected("THIRD_PARTY_NOTICES.md is stale")

    def test_public_export_asset_path_and_hash_drift_are_rejected(self) -> None:
        write(self.fixture.export / "public/unregistered.svg", "<svg/>\n")
        self.assert_rejected("public-export path mismatch")
        self.fixture._write_export()
        registry = self.fixture.registry()
        registry["assets"][0]["sha256"] = "0" * 64
        self.fixture.save_registry(registry)
        self.assert_rejected("asset hash drift")

    def test_retained_archive_blob_drift_is_rejected(self) -> None:
        registry = self.fixture.registry()
        artifact = registry["packages"][0]["artifact"]
        path = self.fixture.artifacts / artifact["path"]
        write(path, path.read_bytes() + b"changed")
        self.assert_rejected("artifact hash drift")

    def test_valid_lock_hash_cannot_hide_evidence_and_image_drift(self) -> None:
        registry = self.fixture.registry()
        registry["lockfiles"]["uv.lock"] = sha256((self.fixture.export / "uv.lock").read_bytes())
        retained = registry["packages"][0]["retained_files"][1]
        changed = b"reviewed replacement\n"
        write(self.fixture.export / retained["path"], changed)
        retained["sha256"] = sha256(changed)
        self.fixture.save_registry(registry)
        image_path = self.fixture.export / "compliance/final-image-spdx.json"
        image = json.loads(image_path.read_text())
        image["packages"].append("python:extra==1")
        write_json(image_path, image)
        self.assert_rejected("mismatched upstream material")

    def test_tracked_evidence_generator_is_bound_to_the_release_policy(self) -> None:
        release_dir = Path(__file__).parent
        generator = (release_dir / "generate_release_evidence.py").read_bytes()
        policy = json.loads((release_dir / "sbom-tool.json").read_text())
        self.assertEqual(policy["sha256"], sha256(generator))
        self.assertEqual(
            policy["invocation_template"],
            [
                "/usr/local/bin/python3.12",
                "-I",
                "-S",
                "artifacts/tools/release-evidence",
                "scan",
                "oci-manifest:{manifest_digest}",
                "--source",
                ".",
                "--export",
                "public-export",
                "--artifacts",
                "artifacts",
                "--layout",
                "image",
                "--output",
                "spdx-json=reports/final-image.sbom.json",
            ],
        )
        self.assertEqual(
            policy["interpreter"],
            {
                "implementation": "CPython",
                "version": "3.12.13",
                "path": "/usr/local/bin/python3.12",
                "site_packages": "/app/.venv/lib/python3.12/site-packages",
                "sha256_source": "base-image",
            },
        )
        self.assertEqual(
            policy["modules"],
            [
                "ops/release/generate_release_evidence.py",
                "ops/release/license_provenance.py",
            ],
        )

    def test_tracked_generator_produces_verifiable_evidence_with_ofl_fonts(self) -> None:
        release_dir = Path(__file__).parent
        repository_root = release_dir.parents[1]
        interpreter = Path(sys.executable).resolve(strict=True)
        self.fixture.interpreter_path = str(interpreter)
        self.fixture.interpreter_site_packages = str(
            Path(sysconfig.get_paths()["purelib"]).resolve(strict=True)
        )
        self.fixture.interpreter_bytes = interpreter.read_bytes()
        self.fixture._write_base_image()
        self.fixture._write_locks()
        for tree in (self.fixture.source, self.fixture.export):
            shutil.copytree(
                repository_root / "frontend/src/assets/fonts/ibm-plex",
                tree / "frontend/src/assets/fonts/ibm-plex",
                dirs_exist_ok=True,
            )
        self.fixture._write_export()
        source = self.fixture.source
        export = source / "public-export"
        artifacts = source / "artifacts"
        shutil.copytree(self.fixture.export, export)
        shutil.copytree(self.fixture.artifacts, artifacts)
        generator = (release_dir / "generate_release_evidence.py").read_bytes()
        policy = json.loads((release_dir / "sbom-tool.json").read_text())
        policy["base_image"] = self.fixture.base_reference
        policy["uv_image"] = self.fixture.uv_reference
        policy["sha256"] = sha256(generator)
        policy["interpreter"]["path"] = str(interpreter)
        policy["interpreter"]["site_packages"] = self.fixture.interpreter_site_packages
        policy["invocation_template"][0] = str(interpreter)
        write(source / "ops/release/generate_release_evidence.py", generator)
        write(export / "ops/release/generate_release_evidence.py", generator)
        verifier = (release_dir / "license_provenance.py").read_bytes()
        write(source / "ops/release/license_provenance.py", verifier)
        write(export / "ops/release/license_provenance.py", verifier)
        write_json(source / "ops/release/sbom-tool.json", policy)
        write_json(export / "ops/release/sbom-tool.json", policy)
        write(artifacts / "tools/release-evidence", generator)
        (artifacts / "tools/release-evidence").chmod(0o755)
        image = json.loads((export / "compliance/final-image-spdx.json").read_text())
        tool_arguments = [
            "artifacts/tools/release-evidence",
            "scan",
            f"oci-manifest:{image['manifest_digest']}",
            "--source",
            ".",
            "--export",
            "public-export",
            "--artifacts",
            "artifacts",
            "--layout",
            "image",
            "--output",
            "spdx-json=reports/final-image.sbom.json",
            "--packaging-authority-sha256",
            sha256(
                json.dumps(_packaging_authority(), separators=(",", ":"), sort_keys=True).encode()
            ),
        ]
        arguments = [str(interpreter), "-I", "-S", *tool_arguments]
        previous = Path.cwd()
        try:
            os.chdir(source)
            environment = os.environ.copy()
            environment["PATH"] = f"{interpreter.parent}:{environment['PATH']}"
            first_run = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )
            self.assertEqual(first_run.returncode, 0, first_run.stderr)
            registry = json.loads((export / "compliance/dependency-evidence.json").read_text())
            expected_fonts = {
                "frontend/src/assets/fonts/ibm-plex/IBMPlexMono-Regular.woff2": (
                    "ba204497f16b6d334cee9d1e963a831b73e3a56e1d6300a8489d18df7214b350"
                ),
                "frontend/src/assets/fonts/ibm-plex/IBMPlexMono-SemiBold.woff2": (
                    "6a825b4824c01cbb401e829e5a066a1818411bcb3538b5a5792c5ca9b82343c3"
                ),
                "frontend/src/assets/fonts/ibm-plex/IBMPlexSans-Bold.woff2": (
                    "fa7130d854a660b39a7fc9e6e0f2dc23dba5f1346e2adea3e1fe37b6d884133d"
                ),
                "frontend/src/assets/fonts/ibm-plex/IBMPlexSans-Medium.woff2": (
                    "5660f8a658f8bb50dbc005232f885eadffd2bc1c235c4f6fbb63469d1f9cde6d"
                ),
                "frontend/src/assets/fonts/ibm-plex/IBMPlexSans-Regular.woff2": (
                    "ba711a3085ff9f27440b6b9c4550cfc47c97bf36591d5da958b975bb3add8c1a"
                ),
                "frontend/src/assets/fonts/ibm-plex/IBMPlexSans-SemiBold.woff2": (
                    "f78048030eab62e860efa39a0df79e2e5581bf122eb95b9bc42c0b8a4988d205"
                ),
            }
            font_assets = {
                item["path"]: item
                for item in registry["assets"]
                if item["license_expression"] == "OFL-1.1"
            }
            self.assertEqual(
                {path: item["sha256"] for path, item in font_assets.items()},
                expected_fonts,
            )
            self.assertTrue(
                all(
                    item["ofl_evidence"]["license_path"]
                    == "frontend/src/assets/fonts/ibm-plex/OFL.txt"
                    and item["ofl_evidence"]["provenance_path"]
                    == "frontend/src/assets/fonts/ibm-plex/IBM-Plex.font-provenance.json"
                    for item in font_assets.values()
                )
            )
            notice = (export / "THIRD_PARTY_NOTICES.md").read_text()
            self.assertEqual(notice.count("SIL OPEN FONT LICENSE Version 1.1"), 1)
            provenance = json.loads((export / "compliance/release-provenance.json").read_text())
            self.assertEqual(
                {
                    item["path"]: item["ofl_evidence"]["source_revision"]
                    for item in provenance["assets"]
                    if item["license_expression"] == "OFL-1.1"
                },
                {path: "bf260093582f04622aacc1e9f9ca604d7ccd0c42" for path in expected_fonts},
            )
            path_marker = source / "unexpected-path-interpreter"
            path_wrapper = source / "path-wrapper" / "python3"
            write(
                path_wrapper,
                "#!/bin/sh\n"
                f"printf launched > {str(path_marker)!r}\n"
                f'exec {sys.executable!r} "$@"\n',
            )
            path_wrapper.chmod(0o755)
            substituted_path_environment = environment.copy()
            substituted_path_environment["PATH"] = (
                f"{path_wrapper.parent}:{substituted_path_environment['PATH']}"
            )
            substituted_path_run = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                env=substituted_path_environment,
                text=True,
            )
            self.assertFalse(path_marker.exists())
            self.assertNotEqual(substituted_path_run.returncode, 0, substituted_path_run.stderr)
            interpreter_link = source / "substituted-python3.12"
            interpreter_link.symlink_to(interpreter)
            linked_policy = json.loads(json.dumps(policy))
            linked_policy["interpreter"]["path"] = str(interpreter_link)
            linked_policy["invocation_template"][0] = str(interpreter_link)
            write_json(source / "ops/release/sbom-tool.json", linked_policy)
            write_json(export / "ops/release/sbom-tool.json", linked_policy)
            linked_run = subprocess.run(
                [str(interpreter_link), "-I", "-S", *tool_arguments],
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )
            self.assertNotEqual(linked_run.returncode, 0, linked_run.stderr)
            write_json(source / "ops/release/sbom-tool.json", policy)
            write_json(export / "ops/release/sbom-tool.json", policy)
            generated_paths = (
                export / "THIRD_PARTY_NOTICES.md",
                export / "compliance/dependency-evidence.json",
                export / "compliance/release-provenance.json",
                export / "compliance/final-image-spdx.json",
                artifacts / "reports/final-image.sbom.json",
                artifacts / "reports/final-image-files.json",
                artifacts / "reports/sbom-attestation.json",
                artifacts / "reports/uv-install.json",
            )
            first = {path: path.read_bytes() for path in generated_paths}
            second_run = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )
            self.assertEqual(second_run.returncode, 0, second_run.stderr)
            self.assertEqual(first, {path: path.read_bytes() for path in generated_paths})

            marker = source / "unexpected-import-wrapper"
            startup = source / "startup"
            startup.mkdir()
            write(
                startup / "sitecustomize.py",
                "import importlib\n"
                "import pathlib\n"
                "import sys\n"
                "sys.path.insert(0, str(pathlib.Path.cwd()))\n"
                "module = importlib.import_module('ops.release.license_provenance')\n"
                "original = module.verify_and_generate\n"
                "def wrapped(*args, **kwargs):\n"
                f"    pathlib.Path({str(marker)!r}).write_text('wrapped\\n')\n"
                "    return original(*args, **kwargs)\n"
                "module.verify_and_generate = wrapped\n",
            )
            injected_environment = environment.copy()
            injected_environment["PYTHONPATH"] = str(startup)
            injected_run = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                env=injected_environment,
                text=True,
            )
            self.assertNotEqual(injected_run.returncode, 0, injected_run.stderr)
            self.assertFalse(marker.exists())

            user_marker = source / "unexpected-user-wrapper"
            user_site = source / "user-site"
            user_customization = (
                user_site
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
                / "usercustomize.py"
            )
            write(
                user_customization,
                f"from pathlib import Path\nPath({str(user_marker)!r}).write_text('loaded\\n')\n",
            )
            user_environment = environment.copy()
            user_environment["PYTHONUSERBASE"] = str(user_site)
            user_run = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                env=user_environment,
                text=True,
            )
            self.assertNotEqual(user_run.returncode, 0, user_run.stderr)
            self.assertFalse(user_marker.exists())

            direct_run = subprocess.run(
                [str(interpreter), *tool_arguments],
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )
            self.assertNotEqual(direct_run.returncode, 0, direct_run.stderr)

            verifier_path = source / "ops/release/license_provenance.py"
            original_verifier = verifier_path.read_bytes()
            write(verifier_path, original_verifier + b"\n# substituted\n")
            try:
                substituted_run = subprocess.run(
                    arguments,
                    check=False,
                    capture_output=True,
                    env=environment,
                    text=True,
                )
                self.assertNotEqual(substituted_run.returncode, 0, substituted_run.stderr)
            finally:
                write(verifier_path, original_verifier)
        finally:
            os.chdir(previous)
        verify_and_generate(source, export, artifacts)


if __name__ == "__main__":
    unittest.main()
