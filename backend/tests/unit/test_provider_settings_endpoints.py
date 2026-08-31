import pytest

from backend.app.provider_settings import (
    ProviderSettingsValidationError,
    normalize_openai_compatible_endpoint,
)

ALLOWED = frozenset({"https://api.example.com", "https://second.example.com:443/"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.example.com", "https://api.example.com/v1"),
        ("HTTPS://API.EXAMPLE.COM/", "https://api.example.com/v1"),
        ("https://api.example.com:443/v1", "https://api.example.com/v1"),
        ("https://second.example.com/v1/", "https://second.example.com/v1"),
    ],
)
def test_compatible_endpoint_accepts_only_the_normalized_allowed_base(
    raw: str, expected: str
) -> None:
    assert normalize_openai_compatible_endpoint(raw, allowed_origins=ALLOWED) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://api.example.com",
        "https://user@api.example.com",
        "https://user:password@api.example.com",
        "https://api.example.com/v1?mode=unsafe",
        "https://api.example.com/v1?",
        "https://api.example.com/v1#unsafe",
        "https://api.example.com/v1#",
        "https://api.example.com:444/v1",
        "https://api.example.com/v1/chat/completions",
        "https://api.example.com/v1%2fchat",
        "https://unapproved.example.com/v1",
        "https://api.example.com./v1",
        "https://api.example.com\\@unapproved.example/v1",
        "https://bad_host.example/v1",
        "https://-api.example.com/v1",
        "https://api..example.com/v1",
        " https://api.example.com/v1",
        "https://faß.example/v1",
        "https://xn--fa-hia.example/v1",
    ],
)
def test_compatible_endpoint_rejects_ambiguous_or_unapproved_urls(raw: str) -> None:
    with pytest.raises(ProviderSettingsValidationError) as error:
        normalize_openai_compatible_endpoint(raw, allowed_origins=ALLOWED)

    assert raw not in str(error.value)


@pytest.mark.parametrize(
    "target",
    [
        "https://localhost",
        "https://service.localhost",
        "https://service.internal",
        "https://service.local",
        "https://single-label",
        "https://127.0.0.1",
        "https://10.0.0.1",
        "https://169.254.169.254",
        "https://224.0.0.1",
        "https://8.8.8.8",
        "https://[::1]",
        "https://[fe80::1]",
        "https://[ff02::1]",
        "https://127.1",
        "https://0177.0.0.1",
        "https://0x7f.0.0.1",
        "https://service.home.arpa",
        "https://metadata.google.internal",
        "https://service.metadata.google.internal",
        "https://instance-data.ec2.internal",
        "https://metadata.azure.internal",
    ],
)
def test_compatible_endpoint_rejects_local_ip_and_metadata_targets(target: str) -> None:
    with pytest.raises(ProviderSettingsValidationError):
        normalize_openai_compatible_endpoint(
            target,
            allowed_origins=frozenset({target}),
        )


@pytest.mark.parametrize(
    "allowed",
    [
        frozenset(),
        frozenset({"http://api.example.com"}),
        frozenset({"https://api.example.com/v1"}),
        frozenset({"https://api.example.com?"}),
        frozenset({"https://user@api.example.com"}),
        frozenset({"https://127.0.0.1"}),
    ],
)
def test_compatible_endpoint_rejects_empty_or_unsafe_allowlists(
    allowed: frozenset[str],
) -> None:
    with pytest.raises(ProviderSettingsValidationError):
        normalize_openai_compatible_endpoint(
            "https://api.example.com",
            allowed_origins=allowed,
        )


def test_compatible_endpoint_does_not_fold_unicode_into_an_allowed_ascii_host() -> None:
    with pytest.raises(ProviderSettingsValidationError):
        normalize_openai_compatible_endpoint(
            "https://faß.example",
            allowed_origins=frozenset({"https://fass.example"}),
        )


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example.com:secret",
        "https://\ud800.example.com",
    ],
)
def test_endpoint_parse_failures_do_not_chain_input_bearing_exceptions(value: str) -> None:
    with pytest.raises(ProviderSettingsValidationError) as error:
        normalize_openai_compatible_endpoint(
            value,
            allowed_origins=ALLOWED,
        )

    assert error.value.__cause__ is None


def test_compatible_endpoint_can_pin_a_different_exact_base_path() -> None:
    assert (
        normalize_openai_compatible_endpoint(
            "https://api.example.com/openai/",
            allowed_origins=ALLOWED,
            base_path="/openai",
        )
        == "https://api.example.com/openai"
    )

    with pytest.raises(ProviderSettingsValidationError):
        normalize_openai_compatible_endpoint(
            "https://api.example.com/v1",
            allowed_origins=ALLOWED,
            base_path="/openai",
        )


@pytest.mark.parametrize("base_path", ["/../v1", "/v1%2fadmin", "/v1//admin"])
def test_compatible_endpoint_rejects_noncanonical_base_paths(base_path: str) -> None:
    with pytest.raises(ProviderSettingsValidationError):
        normalize_openai_compatible_endpoint(
            "https://api.example.com",
            allowed_origins=ALLOWED,
            base_path=base_path,
        )
