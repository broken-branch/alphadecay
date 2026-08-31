from dataclasses import FrozenInstanceError, replace

import pytest

from backend.app.provider_settings import (
    GEMINI_ENDPOINT,
    CredentialCodec,
    CredentialCodecError,
    CredentialSecret,
    EncryptedCredential,
    ProviderKind,
    ProviderMetadata,
    ProviderSettingsValidationError,
)

MASTER_SECRET = b"m" * 32
OTHER_MASTER_SECRET = b"n" * 32
PLAINTEXT = "synthetic-api-key-never-log"


def _metadata() -> ProviderMetadata:
    return ProviderMetadata.for_gemini(model="gemini-2.5-flash", generation=7)


def test_credential_codec_roundtrips_through_the_metadata_bound_envelope() -> None:
    codec = CredentialCodec(MASTER_SECRET)
    secret = CredentialSecret.from_text(PLAINTEXT)

    encrypted = codec.encrypt(secret, _metadata())
    decrypted = codec.decrypt(encrypted, _metadata())

    assert decrypted.reveal_text() == PLAINTEXT
    assert encrypted.schema_version == 1
    assert len(encrypted.nonce) == 12
    assert len(encrypted.ciphertext) > len(PLAINTEXT)


def test_each_encryption_uses_a_fresh_nonce() -> None:
    codec = CredentialCodec(MASTER_SECRET)
    secret = CredentialSecret.from_text(PLAINTEXT)

    first = codec.encrypt(secret, _metadata())
    second = codec.encrypt(secret, _metadata())

    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


@pytest.mark.parametrize(
    "drifted_metadata",
    [
        ProviderMetadata(
            provider=ProviderKind.OWNER_OPENAI_COMPATIBLE,
            endpoint=GEMINI_ENDPOINT,
            model="gemini-2.5-flash",
            generation=7,
        ),
        ProviderMetadata.for_gemini(model="gemini-2.5-pro", generation=7),
        ProviderMetadata.for_gemini(model="gemini-2.5-flash", generation=8),
    ],
)
def test_decryption_fails_closed_on_each_aad_metadata_drift(
    drifted_metadata: ProviderMetadata,
) -> None:
    encrypted = CredentialCodec(MASTER_SECRET).encrypt(
        CredentialSecret.from_text(PLAINTEXT),
        _metadata(),
    )

    with pytest.raises(CredentialCodecError) as error:
        CredentialCodec(MASTER_SECRET).decrypt(encrypted, drifted_metadata)

    assert str(error.value) == "PROVIDER_CREDENTIAL_DECRYPT_FAILED"
    assert PLAINTEXT not in repr(error.value)


def test_decryption_fails_closed_on_compatible_endpoint_drift() -> None:
    original = ProviderMetadata.for_openai_compatible(
        endpoint="https://api.example.com",
        model="classifier-v1",
        generation=7,
        allowed_origins={"https://api.example.com", "https://second.example.com"},
    )
    drifted = ProviderMetadata.for_openai_compatible(
        endpoint="https://second.example.com",
        model="classifier-v1",
        generation=7,
        allowed_origins={"https://api.example.com", "https://second.example.com"},
    )
    codec = CredentialCodec(MASTER_SECRET)
    encrypted = codec.encrypt(CredentialSecret.from_text(PLAINTEXT), original)

    with pytest.raises(CredentialCodecError, match="^PROVIDER_CREDENTIAL_DECRYPT_FAILED$"):
        codec.decrypt(encrypted, drifted)


@pytest.mark.parametrize("field", ["nonce", "ciphertext"])
def test_decryption_fails_closed_on_ciphertext_envelope_tamper(field: str) -> None:
    encrypted = CredentialCodec(MASTER_SECRET).encrypt(
        CredentialSecret.from_text(PLAINTEXT),
        _metadata(),
    )
    value = getattr(encrypted, field)
    tampered = replace(encrypted, **{field: bytes([value[0] ^ 1]) + value[1:]})

    with pytest.raises(CredentialCodecError, match="^PROVIDER_CREDENTIAL_DECRYPT_FAILED$"):
        CredentialCodec(MASTER_SECRET).decrypt(tampered, _metadata())


def test_decryption_fails_closed_on_key_drift() -> None:
    encrypted = CredentialCodec(MASTER_SECRET).encrypt(
        CredentialSecret.from_text(PLAINTEXT),
        _metadata(),
    )

    with pytest.raises(CredentialCodecError, match="^PROVIDER_CREDENTIAL_DECRYPT_FAILED$"):
        CredentialCodec(OTHER_MASTER_SECRET).decrypt(encrypted, _metadata())


def test_decryption_fails_closed_on_envelope_schema_drift() -> None:
    encrypted = CredentialCodec(MASTER_SECRET).encrypt(
        CredentialSecret.from_text(PLAINTEXT),
        _metadata(),
    )
    drifted = replace(encrypted, schema_version=2)

    with pytest.raises(CredentialCodecError, match="^PROVIDER_CREDENTIAL_DECRYPT_FAILED$"):
        CredentialCodec(MASTER_SECRET).decrypt(drifted, _metadata())

    bool_schema = replace(encrypted, schema_version=True)
    with pytest.raises(CredentialCodecError, match="^PROVIDER_CREDENTIAL_DECRYPT_FAILED$"):
        CredentialCodec(MASTER_SECRET).decrypt(bool_schema, _metadata())


def test_metadata_constructor_fails_closed_on_schema_or_singleton_drift() -> None:
    with pytest.raises(ProviderSettingsValidationError):
        replace(_metadata(), schema_version=2)
    with pytest.raises(ProviderSettingsValidationError):
        replace(_metadata(), singleton_id="another-owner")
    with pytest.raises(ProviderSettingsValidationError):
        replace(_metadata(), schema_version=True)


def test_secret_key_and_ciphertext_representations_are_redacted() -> None:
    secret = CredentialSecret.from_text(PLAINTEXT)
    codec = CredentialCodec(MASTER_SECRET)
    encrypted = codec.encrypt(secret, _metadata())

    for rendered in (
        str(secret),
        repr(secret),
        str(codec),
        repr(codec),
        str(encrypted),
        repr(encrypted),
    ):
        assert PLAINTEXT not in rendered
        assert MASTER_SECRET.hex() not in rendered
        assert encrypted.nonce.hex() not in rendered
        assert encrypted.ciphertext.hex() not in rendered
        assert "redacted" in rendered.lower()

    with pytest.raises(FrozenInstanceError):
        secret._value = b"changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        encrypted.ciphertext = b"changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    [
        b"short",
        b"",
    ],
)
def test_codec_rejects_invalid_master_secrets_without_echoing_them(value: bytes) -> None:
    with pytest.raises(CredentialCodecError) as error:
        CredentialCodec(value)

    if value:
        assert value.hex() not in str(error.value)


def test_secret_rejects_empty_and_oversized_values_without_echoing_them() -> None:
    for value in ("", "x" * 16_385):
        with pytest.raises(CredentialCodecError) as error:
            CredentialSecret.from_text(value)
        if value:
            assert value not in str(error.value)


def test_invalid_envelope_shape_is_rejected_without_ciphertext_in_the_error() -> None:
    malformed = EncryptedCredential(schema_version=1, nonce=b"short", ciphertext=b"content")

    with pytest.raises(CredentialCodecError) as error:
        CredentialCodec(MASTER_SECRET).decrypt(malformed, _metadata())

    assert malformed.ciphertext.hex() not in str(error.value)


def test_oversized_ciphertext_is_rejected_before_aead_decryption() -> None:
    codec = CredentialCodec(MASTER_SECRET)
    malformed = EncryptedCredential(
        schema_version=1,
        nonce=b"n" * 12,
        ciphertext=b"c" * 16_401,
    )

    class FailingAead:
        def decrypt(self, *_args, **_kwargs):
            raise AssertionError("oversized ciphertext reached AES-GCM")

    codec._aead = FailingAead()

    with pytest.raises(CredentialCodecError, match="^PROVIDER_CREDENTIAL_DECRYPT_FAILED$"):
        codec.decrypt(malformed, _metadata())


def test_invalid_utf8_exception_does_not_chain_or_render_plaintext_bytes() -> None:
    secret = CredentialSecret.from_bytes(b"private-\xff-value")

    with pytest.raises(CredentialCodecError) as error:
        secret.reveal_text()

    assert error.value.__cause__ is None
    assert "private" not in repr(error.value)
