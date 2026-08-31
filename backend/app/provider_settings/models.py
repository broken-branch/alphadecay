from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import SplitResult, urlsplit

PROVIDER_SETTINGS_SCHEMA_VERSION = 1
OWNER_SETTINGS_SINGLETON_ID = "owner-ai-provider"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"

_PRIVATE_HOST_SUFFIXES = (
    ".corp",
    ".home",
    ".home.arpa",
    ".internal",
    ".lan",
    ".local",
    ".localdomain",
)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_LEGACY_IPV4_LABEL = re.compile(r"(?:[0-9]+|0x[0-9a-f]+)")
_BASE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")


class ProviderSettingsValidationError(ValueError):
    pass


class ProviderKind(StrEnum):
    OWNER_GEMINI = "OWNER_GEMINI"
    OWNER_OPENAI_COMPATIBLE = "OWNER_OPENAI_COMPATIBLE"


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    provider: ProviderKind
    endpoint: str
    model: str
    generation: int
    schema_version: int = PROVIDER_SETTINGS_SCHEMA_VERSION
    singleton_id: str = OWNER_SETTINGS_SINGLETON_ID

    def __post_init__(self) -> None:
        if not isinstance(self.provider, ProviderKind):
            raise ProviderSettingsValidationError("PROVIDER_SETTINGS_PROVIDER_INVALID")
        if (
            type(self.schema_version) is not int
            or self.schema_version != PROVIDER_SETTINGS_SCHEMA_VERSION
        ):
            raise ProviderSettingsValidationError("PROVIDER_SETTINGS_SCHEMA_INVALID")
        if self.singleton_id != OWNER_SETTINGS_SINGLETON_ID:
            raise ProviderSettingsValidationError("PROVIDER_SETTINGS_SINGLETON_INVALID")
        _validate_bounded_text(self.endpoint, field="ENDPOINT", maximum=2048)
        _validate_bounded_text(self.model, field="MODEL", maximum=256)
        if self.provider is ProviderKind.OWNER_GEMINI:
            if self.endpoint != GEMINI_ENDPOINT:
                raise ProviderSettingsValidationError("PROVIDER_SETTINGS_ENDPOINT_INVALID")
        else:
            _validate_canonical_compatible_endpoint(self.endpoint)
        if not isinstance(self.generation, int) or isinstance(self.generation, bool):
            raise ProviderSettingsValidationError("PROVIDER_SETTINGS_GENERATION_INVALID")
        if not 1 <= self.generation < 2**63:
            raise ProviderSettingsValidationError("PROVIDER_SETTINGS_GENERATION_INVALID")

    @classmethod
    def for_gemini(cls, *, model: str, generation: int) -> ProviderMetadata:
        return cls(
            provider=ProviderKind.OWNER_GEMINI,
            endpoint=GEMINI_ENDPOINT,
            model=model,
            generation=generation,
        )

    @classmethod
    def for_openai_compatible(
        cls,
        *,
        endpoint: str,
        model: str,
        generation: int,
        allowed_origins: Iterable[str],
        base_path: str = "/v1",
    ) -> ProviderMetadata:
        return cls(
            provider=ProviderKind.OWNER_OPENAI_COMPATIBLE,
            endpoint=normalize_openai_compatible_endpoint(
                endpoint,
                allowed_origins=allowed_origins,
                base_path=base_path,
            ),
            model=model,
            generation=generation,
        )


def normalize_openai_compatible_endpoint(
    endpoint: str,
    *,
    allowed_origins: Iterable[str],
    base_path: str = "/v1",
) -> str:
    normalized_base = _normalize_base_path(base_path)
    allowed = frozenset(_normalize_origin(origin) for origin in allowed_origins)
    if not allowed:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_ALLOWLIST_EMPTY")

    parts = _split_https_url(endpoint)
    origin = _origin_from_parts(parts)
    if origin not in allowed:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_ORIGIN_NOT_ALLOWED")
    if parts.path not in {"", "/", normalized_base, f"{normalized_base}/"}:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_PATH_INVALID")
    return f"{origin}{normalized_base}"


def _normalize_origin(origin: str) -> str:
    parts = _split_https_url(origin)
    if parts.path not in {"", "/"}:
        raise ProviderSettingsValidationError("PROVIDER_ALLOWED_ORIGIN_INVALID")
    return _origin_from_parts(parts)


def _split_https_url(value: str) -> SplitResult:
    _validate_bounded_text(value, field="ENDPOINT", maximum=2048)
    if "\\" in value or "?" in value or "#" in value:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_INVALID")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_INVALID") from None
    if parts.scheme.lower() != "https":
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_HTTPS_REQUIRED")
    if parts.username is not None or parts.password is not None:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_CREDENTIALS_FORBIDDEN")
    if parts.query or parts.fragment:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_SUFFIX_FORBIDDEN")
    if port not in {None, 443}:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_PORT_INVALID")
    return parts


def _origin_from_parts(parts: SplitResult) -> str:
    hostname = parts.hostname
    if hostname is None or hostname.endswith(".") or "%" in hostname:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_HOST_INVALID")
    if not hostname.isascii():
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_HOST_INVALID")
    normalized_host = hostname.lower()
    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        pass
    else:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_IP_FORBIDDEN")
    labels = normalized_host.split(".")
    if (
        len(labels) < 2
        or len(normalized_host) > 253
        or any(_DNS_LABEL.fullmatch(label) is None for label in labels)
    ):
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_HOST_INVALID")
    if len(labels) <= 4 and all(_LEGACY_IPV4_LABEL.fullmatch(label) for label in labels):
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_IP_FORBIDDEN")
    if normalized_host.endswith((".localhost", *_PRIVATE_HOST_SUFFIXES)):
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_HOST_FORBIDDEN")
    return f"https://{normalized_host}"


def _validate_canonical_compatible_endpoint(endpoint: str) -> None:
    parts = _split_https_url(endpoint)
    origin = _origin_from_parts(parts)
    path = _normalize_base_path(parts.path)
    if endpoint != f"{origin}{path}":
        raise ProviderSettingsValidationError("PROVIDER_SETTINGS_ENDPOINT_INVALID")


def _normalize_base_path(base_path: str) -> str:
    _validate_bounded_text(base_path, field="BASE_PATH", maximum=128)
    if not base_path.startswith("/") or base_path == "/" or base_path.endswith("/"):
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_BASE_PATH_INVALID")
    if "//" in base_path or "?" in base_path or "#" in base_path or "\\" in base_path:
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_BASE_PATH_INVALID")
    segments = base_path[1:].split("/")
    if any(
        segment in {".", ".."} or _BASE_PATH_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise ProviderSettingsValidationError("PROVIDER_ENDPOINT_BASE_PATH_INVALID")
    return base_path


def _validate_bounded_text(value: object, *, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProviderSettingsValidationError(f"PROVIDER_SETTINGS_{field}_INVALID")
    if len(value) > maximum or not value.isprintable():
        raise ProviderSettingsValidationError(f"PROVIDER_SETTINGS_{field}_INVALID")
