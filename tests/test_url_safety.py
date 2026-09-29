import pytest

from app.core.url_safety import (
    UnresolvableHostError,
    UnsafeURLError,
    ensure_public_url,
    resolve_public_addresses,
)

BLOCKED_HOSTS = [
    "127.0.0.1",  # loopback
    "localhost",
    "10.0.0.5",  # private
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",  # link-local, cloud metadata
    "0.0.0.0",  # unspecified
    "100.64.0.1",  # carrier-grade NAT
    "224.0.0.1",  # multicast
    "[::1]",
    "[fc00::1]",  # unique local
    "[fe80::1]",  # link-local
    "[::ffff:127.0.0.1]",  # IPv4-mapped loopback
    "[::ffff:10.0.0.1]",
    "[64:ff9b::a9fe:a9fe]",  # NAT64 wrapping 169.254.169.254
    "2130706433",  # 127.0.0.1 as a decimal number
    "0x7f000001",  # ... as hex
    "127.1",  # ... shortened
]


@pytest.mark.parametrize("host", BLOCKED_HOSTS)
def test_blocks_private_and_internal_addresses(host: str):
    with pytest.raises(UnsafeURLError, match="private or internal"):
        ensure_public_url(f"https://{host}/hook")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/hook",
        "http://93.184.215.14:8080/hook",
        "https://[2606:2800:21f:cb07:6820:80da:af6b:8b2c]/hook",
    ],
)
def test_allows_public_addresses(url: str):
    ensure_public_url(url)


def test_blocks_hostname_resolving_to_private_address(dns: dict[str, list[str]]):
    dns["internal.example"] = ["10.1.2.3"]

    with pytest.raises(UnsafeURLError, match="private or internal"):
        ensure_public_url("https://internal.example/hook")


def test_blocks_hostname_when_any_address_is_private(dns: dict[str, list[str]]):
    dns["mixed.example"] = ["93.184.215.14", "192.168.0.10"]

    with pytest.raises(UnsafeURLError, match="private or internal"):
        ensure_public_url("https://mixed.example/hook")


def test_blocks_unresolvable_host():
    # UnresolvableHostError: DNS can recover, so the delivery worker retries these.
    with pytest.raises(UnresolvableHostError, match="could not be resolved"):
        ensure_public_url("https://does-not-exist.invalid/hook")


def test_invalid_hostname_is_not_just_unresolvable():
    """No DNS answer will ever fix it, so it must not be treated as retryable."""
    with pytest.raises(UnsafeURLError, match="not a valid hostname") as caught:
        ensure_public_url(f"https://{'a' * 70}.com/hook")

    assert not isinstance(caught.value, UnresolvableHostError)


@pytest.mark.parametrize("url", ["https:///hook", "https://example.com:99999/hook"])
def test_blocks_malformed_urls(url: str):
    with pytest.raises(UnsafeURLError):
        ensure_public_url(url)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.0.2", "[::1]"])
def test_allow_loopback_permits_localhost(host: str):
    ensure_public_url(f"http://{host}:9000/hook", allow_loopback=True)


@pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0"])
def test_allow_loopback_still_blocks_other_internal_addresses(host: str):
    with pytest.raises(UnsafeURLError):
        ensure_public_url(f"http://{host}/hook", allow_loopback=True)


def test_resolve_public_addresses_returns_every_address_once_in_order(
    dns: dict[str, list[str]],
):
    dns["example.com"] = ["2606:2800::1", "93.184.215.14", "2606:2800::1"]

    addresses = resolve_public_addresses("https://example.com/hook")

    assert [str(a) for a in addresses] == ["2606:2800::1", "93.184.215.14"]


def test_resolve_public_addresses_applies_the_same_checks(dns: dict[str, list[str]]):
    dns["example.com"] = ["93.184.215.14", "10.0.0.5"]

    with pytest.raises(UnsafeURLError, match="private or internal"):
        resolve_public_addresses("https://example.com/hook")
