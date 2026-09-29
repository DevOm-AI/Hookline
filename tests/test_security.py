import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import (
    api_key_matches,
    generate_api_key,
    generate_endpoint_secret,
    hash_api_key,
)


def test_hash_api_key_is_sha256_hex():
    assert hash_api_key("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_api_key_matches_only_the_right_key():
    stored = hash_api_key("hk_right")

    assert api_key_matches("hk_right", stored)
    assert not api_key_matches("hk_wrong", stored)


def test_generated_keys_and_secrets_are_prefixed_and_unique():
    assert generate_api_key().startswith("hk_")
    assert generate_endpoint_secret().startswith("whsec_")
    assert generate_api_key() != generate_api_key()
    assert generate_endpoint_secret() != generate_endpoint_secret()


@pytest.mark.parametrize("value", ["hk_plaintext_key", "ABC", "", "g" * 64])
def test_settings_reject_api_key_hash_that_is_not_sha256_hex(value: str):
    # Catches pasting the raw key into API_KEY_HASH.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_key_hash=value)
