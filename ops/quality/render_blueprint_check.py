#!/usr/bin/env python3

"""Validate the deployment-critical AlphaDecay Render Blueprint contract."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


class BlueprintError(ValueError):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise BlueprintError(f"duplicate Blueprint key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _mapping,
)

FIXED_ENV = {
    "APP_RUNTIME_CONFIG_REQUIRED": {"value": "false"},
    "APP_ACCOUNT_ROLE": {"value": "SUBMISSION"},
    "APP_AUTONOMOUS_ENABLED": {"value": "false"},
    "ALPACA_API_ENDPOINT": {"value": "https://paper-api.alpaca.markets"},
    "ALPACA_PAPER_TRADE": {"value": "true"},
}
UNSYNCED_ENV = frozenset(
    {
        "APP_POLICY_HASH",
        "APP_CALIBRATION_HASH",
        "APP_CALIBRATION_DECISION_BOUNDARY",
        "APP_CALIBRATION_SEALED_AT",
        "APP_ENTRY_EQUITY_FLOOR",
        "APP_MAXIMUM_LIFETIME_ENTRIES",
        "APP_MAXIMUM_LIFETIME_RISK",
        "APP_MAXIMUM_POSITION_LOSS",
        "APP_MAXIMUM_ENTRY_QUANTITY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "GEMINI_API_KEY",
        "APP_OWNER_ACCESS_CODE",
        "APP_ALLOWED_ORIGIN",
        "APP_OPENAI_COMPATIBLE_ORIGINS",
    }
)
GENERATED_ENV = frozenset({"APP_SESSION_SECRET", "APP_PROVIDER_SETTINGS_SECRET", "SCHEDULER_TOKEN"})


def _environment(entries: object) -> dict[str, dict[str, object]]:
    if not isinstance(entries, list):
        raise BlueprintError("service envVars must be a list")
    result: dict[str, dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            raise BlueprintError("service envVar entry is invalid")
        key = entry["key"]
        if key in result:
            raise BlueprintError(f"service repeats envVar {key}")
        result[key] = {name: value for name, value in entry.items() if name != "key"}
    return result


def validate_blueprint(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"services", "databases"}:
        raise BlueprintError("Blueprint root must contain only services and databases")
    services = value["services"]
    if not isinstance(services, list) or len(services) != 1 or not isinstance(services[0], dict):
        raise BlueprintError("Blueprint must define exactly one web service")
    service = services[0]
    expected_service = {
        "type": "web",
        "name": "alphadecay",
        "runtime": "docker",
        "plan": "starter",
        "region": "ohio",
        "branch": "main",
        "repo": "https://github.com/broken-branch/alphadecay",
        "autoDeployTrigger": "off",
        "dockerfilePath": "./Dockerfile",
        "dockerContext": ".",
        "healthCheckPath": "/api/health",
    }
    if {key: service.get(key) for key in expected_service} != expected_service:
        raise BlueprintError("web service identity, runtime, or deploy guard differs")
    if set(service) != set(expected_service) | {"envVars"}:
        raise BlueprintError("web service contains an unsupported field")

    environment = _environment(service["envVars"])
    expected_keys = set(FIXED_ENV) | set(UNSYNCED_ENV) | set(GENERATED_ENV) | {"DATABASE_URL"}
    if set(environment) != expected_keys:
        raise BlueprintError("web service environment variable set differs")
    for key, expected in FIXED_ENV.items():
        if environment[key] != expected:
            raise BlueprintError(f"fixed environment value differs: {key}")
    for key in UNSYNCED_ENV:
        if environment[key] != {"sync": False}:
            raise BlueprintError(f"secret environment input is not unsynced: {key}")
    for key in GENERATED_ENV:
        if environment[key] != {"generateValue": True}:
            raise BlueprintError(f"generated environment secret differs: {key}")
    if environment["DATABASE_URL"] != {
        "fromDatabase": {"name": "alphadecay-db", "property": "connectionString"}
    }:
        raise BlueprintError("database connection reference differs")

    databases = value["databases"]
    expected_database = {
        "name": "alphadecay-db",
        "databaseName": "alphadecay",
        "user": "alphadecay",
        "plan": "free",
        "region": "ohio",
        "postgresMajorVersion": "17",
        "ipAllowList": [],
    }
    if databases != [expected_database]:
        raise BlueprintError("PostgreSQL Blueprint contract differs")


def check(path: Path) -> None:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise BlueprintError(f"cannot read Blueprint: {error}") from error
    validate_blueprint(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, nargs="?", default=Path("render.yaml"))
    args = parser.parse_args()
    try:
        check(args.path)
    except BlueprintError as error:
        print(f"FAIL  {error}")
        return 1
    print("PASS  Render Blueprint contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
