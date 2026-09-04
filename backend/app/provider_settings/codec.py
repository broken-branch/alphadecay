from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .models import PROVIDER_SETTINGS_SCHEMA_VERSION, ProviderMetadata

_HKDF_SALT = b"AlphaDecay owner provider settings salt v1"
_HKDF_CONTEXT = b"AlphaDecay owner provider credential AES-256-GCM v1"
_NONCE_BYTES = 12
_MINIMUM_MASTER_BYTES = 32
_MAXIMUM_INPUT_BYTES = 16_384


class CredentialCodecError(ValueError):
    pass


@dataclass(frozen=True, slots=True, repr=False, init=False)
class CredentialSecret:
    _value: bytes

    def __init__(self, value: bytes) -> None:
        if not isinstance(value, bytes) or not 1 <= len(value) <= _MAXIMUM_INPUT_BYTES:
            raise CredentialCodecError("PROVIDER_CREDENTIAL_INVALID")
        object.__setattr__(self, "_value", value)

    @classmethod
    def from_text(cls, value: str) -> CredentialSecret:
        if not isinstance(value, str):
            raise CredentialCodecError("PROVIDER_CREDENTIAL_INVALID")
        try:
            return cls(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise CredentialCodecError("PROVIDER_CREDENTIAL_INVALID") from None

    @classmethod
    def from_bytes(cls, value: bytes) -> CredentialSecret:
        return cls(bytes(value))

    def reveal_text(self) -> str:
        try:
            return self._value.decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialCodecError("PROVIDER_CREDENTIAL_TEXT_INVALID") from None

    def __repr__(self) -> str:
        return "CredentialSecret(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedCredential:
    schema_version: int
    nonce: bytes
    ciphertext: bytes

    def __repr__(self) -> str:
        return "EncryptedCredential(<redacted>)"

    __str__ = __repr__


class CredentialCodec:
    __slots__ = ("_aead",)

    def __init__(self, master_secret: bytes) -> None:
        if not isinstance(master_secret, bytes) or len(master_secret) < _MINIMUM_MASTER_BYTES:
            raise CredentialCodecError("PROVIDER_MASTER_SECRET_INVALID")
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=_HKDF_CONTEXT,
        ).derive(master_secret)
        self._aead = AESGCM(key)

    def encrypt(
        self,
        credential: CredentialSecret,
        metadata: ProviderMetadata,
    ) -> EncryptedCredential:
        if not isinstance(credential, CredentialSecret):
            raise CredentialCodecError("PROVIDER_CREDENTIAL_INVALID")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, credential._value, _authenticated_metadata(metadata))
        return EncryptedCredential(
            schema_version=PROVIDER_SETTINGS_SCHEMA_VERSION,
            nonce=nonce,
            ciphertext=ciphertext,
        )

    def decrypt(
        self,
        encrypted: EncryptedCredential,
        metadata: ProviderMetadata,
    ) -> CredentialSecret:
        try:
            if (
                not isinstance(encrypted, EncryptedCredential)
                or type(encrypted.schema_version) is not int
                or encrypted.schema_version != PROVIDER_SETTINGS_SCHEMA_VERSION
                or not isinstance(encrypted.nonce, bytes)
                or len(encrypted.nonce) != _NONCE_BYTES
                or not isinstance(encrypted.ciphertext, bytes)
                or not 16 < len(encrypted.ciphertext) <= _MAXIMUM_INPUT_BYTES + 16
            ):
                raise CredentialCodecError("PROVIDER_CREDENTIAL_DECRYPT_FAILED")
            plaintext = self._aead.decrypt(
                encrypted.nonce,
                encrypted.ciphertext,
                _authenticated_metadata(metadata),
            )
            return CredentialSecret.from_bytes(plaintext)
        except (CredentialCodecError, InvalidTag, TypeError, ValueError):
            raise CredentialCodecError("PROVIDER_CREDENTIAL_DECRYPT_FAILED") from None

    def __repr__(self) -> str:
        return "CredentialCodec(<redacted>)"

    __str__ = __repr__


def _authenticated_metadata(metadata: ProviderMetadata) -> bytes:
    if not isinstance(metadata, ProviderMetadata):
        raise CredentialCodecError("PROVIDER_CREDENTIAL_METADATA_INVALID")
    validated = ProviderMetadata(
        schema_version=metadata.schema_version,
        singleton_id=metadata.singleton_id,
        provider=metadata.provider,
        endpoint=metadata.endpoint,
        model=metadata.model,
        generation=metadata.generation,
    )
    return json.dumps(
        [
            validated.schema_version,
            validated.singleton_id,
            validated.provider.value,
            validated.endpoint,
            validated.model,
            validated.generation,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
