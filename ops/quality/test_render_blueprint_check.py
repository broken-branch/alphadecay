from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ops.quality.render_blueprint_check import BlueprintError, check, validate_blueprint

ROOT = Path(__file__).resolve().parents[2]


def _blueprint() -> dict[str, object]:
    value = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_repository_blueprint_matches_the_deployment_contract() -> None:
    check(ROOT / "render.yaml")


def test_public_deployment_is_replay_only_on_starter_compute() -> None:
    value = _blueprint()
    service = value["services"][0]
    environment = {entry["key"]: entry for entry in service["envVars"]}

    assert service["plan"] == "starter"
    assert environment["APP_RUNTIME_CONFIG_REQUIRED"]["value"] == "false"


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    source = (ROOT / "render.yaml").read_text(encoding="utf-8")
    path = tmp_path / "render.yaml"
    path.write_text(source + "\nservices: []\n", encoding="utf-8")

    with pytest.raises(BlueprintError, match="duplicate Blueprint key: services"):
        check(path)


def test_preview_configuration_cannot_bypass_the_disabled_default() -> None:
    value = _blueprint()
    value["previews"] = {"generation": "off"}

    with pytest.raises(BlueprintError, match="only services and databases"):
        validate_blueprint(value)


def test_repository_source_cannot_drift_from_the_public_release() -> None:
    value = _blueprint()
    services = value["services"]
    assert isinstance(services, list)
    service = services[0]
    assert isinstance(service, dict)
    service["repo"] = "https://github.com/example/other-repository"

    with pytest.raises(BlueprintError, match="identity, runtime, or deploy guard differs"):
        validate_blueprint(value)


def test_live_endpoint_and_autonomy_cannot_be_enabled_in_blueprint() -> None:
    value = _blueprint()
    services = value["services"]
    assert isinstance(services, list)
    service = services[0]
    assert isinstance(service, dict)
    environment = service["envVars"]
    assert isinstance(environment, list)
    for entry in environment:
        assert isinstance(entry, dict)
        if entry["key"] == "ALPACA_API_ENDPOINT":
            entry["value"] = "https://api.alpaca.markets"
        if entry["key"] == "APP_AUTONOMOUS_ENABLED":
            entry["value"] = "true"

    with pytest.raises(BlueprintError, match="fixed environment value differs"):
        validate_blueprint(value)
