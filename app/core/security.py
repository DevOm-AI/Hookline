import hashlib
import hmac
import secrets


def hash_api_key(key: str) -> str:
    # Plain SHA-256 is enough here: keys are long random tokens, not guessable passwords.
    return hashlib.sha256(key.encode()).hexdigest()


def api_key_matches(key: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(key), expected_hash)


def generate_api_key() -> str:
    return f"hk_{secrets.token_urlsafe(32)}"


def generate_endpoint_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


if __name__ == "__main__":
    # uv run python -m app.core.security
    key = generate_api_key()
    print(f"API key (give this to API clients; it is not stored anywhere): {key}")
    print(f"API_KEY_HASH={hash_api_key(key)}")
