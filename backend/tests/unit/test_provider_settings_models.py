from dataclasses import FrozenInstanceError

import pytest

from backend.app.provider_settings import (
    GEMINI_ENDPOINT,
    OWNER_SETTINGS_SINGLETON_ID,
    ProviderKind,
    ProviderMetadata,
    ProviderSettingsValidationError,
)


def test_provider_metadata_builders_return_immutable_normalized_values() -> None:
    gemini = ProviderMetadata.for_gemini(model="gemini-2.5-flash", generation=1)
    compatible = ProviderMetadata.for_openai_compatible(
        endpoint="HTTPS://API.EXAMPLE.COM:443/v1/",
        model="bounded-classifier-v1",
        generation=2,
        allowed_origins=frozenset({"https://api.example.com"}),
    )

    assert gemini.provider is ProviderKind.OWNER_GEMINI
    assert gemini.singleton_id == OWNER_SETTINGS_SINGLETON_ID
    assert gemini.endpoint == GEMINI_ENDPOINT
    assert compatible.provider is ProviderKind.OWNER_OPENAI_COMPATIBLE
    assert compatible.endpoint == "https://api.example.com/v1"

    with pytest.raises(FrozenInstanceError):
        compatible.model = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("model", "generation"),
    [
        ("", 1),
        (" model", 1),
        ("model\nname", 1),
        ("model", 0),
        ("model", 2**63),
    ],
)
def test_provider_metadata_rejects_noncanonical_values(model: str, generation: int) -> None:
    with pytest.raises(ProviderSettingsValidationError) as error:
        ProviderMetadata.for_gemini(model=model, generation=generation)

    assert "gemini-2.5-flash" not in str(error.value)


def test_provider_metadata_rejects_an_untyped_provider_value() -> None:
    with pytest.raises(ProviderSettingsValidationError):
        ProviderMetadata(
            provider="OWNER_GEMINI",  # type: ignore[arg-type]
            endpoint=GEMINI_ENDPOINT,
            model="model",
            generation=1,
        )


def test_gemini_metadata_rejects_endpoint_confusion() -> None:
    with pytest.raises(ProviderSettingsValidationError):
        ProviderMetadata(
            provider=ProviderKind.OWNER_GEMINI,
            endpoint="https://api.example.com/v1",
            model="model",
            generation=1,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "HTTPS://API.EXAMPLE.COM/v1",
        "https://api.example.com:443/v1",
        "https://api.example.com/v1/",
        "https://127.1/v1",
        "https://faß.example/v1",
    ],
)
def test_compatible_metadata_rejects_noncanonical_stored_endpoints(endpoint: str) -> None:
    with pytest.raises(ProviderSettingsValidationError):
        ProviderMetadata(
            provider=ProviderKind.OWNER_OPENAI_COMPATIBLE,
            endpoint=endpoint,
            model="model",
            generation=1,
        )
