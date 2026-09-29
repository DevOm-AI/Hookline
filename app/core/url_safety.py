"""Reject webhook URLs that point at private or internal addresses (SSRF).

Without this, anyone with an API key could register e.g. http://169.254.169.254/ or
http://postgres:5432/ and make Hookline's workers call our own internal services.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# NAT64 addresses embed an IPv4 address in the last 32 bits, and count as global.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


class UnsafeURLError(ValueError):
    """The URL must not be called: it is malformed, unresolvable or not public."""


def ensure_public_url(url: str, *, allow_loopback: bool = False) -> None:
    """Raise UnsafeURLError unless every address the URL's host resolves to is public.

    allow_loopback (DEBUG only) additionally permits localhost, 127.0.0.0/8 and ::1.

    This checks at registration time only. DNS can change afterwards (rebinding), so the
    delivery worker must check the address it actually connects to as well.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError("URL has an invalid port") from exc

    for address in _resolve(host, port):
        if _is_public(address):
            continue
        if allow_loopback and _unwrap(address).is_loopback:
            continue
        raise UnsafeURLError("URL resolves to a private or internal address")


def _resolve(host: str, port: int) -> list[IPAddress]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeURLError("URL host could not be resolved") from exc
    # sockaddr[0] may carry an IPv6 zone id ("fe80::1%eth0"); it is never public anyway.
    return [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]


def _unwrap(address: IPAddress) -> IPAddress:
    """Return the IPv4 address hidden inside an IPv4-mapped or NAT64 IPv6 address."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in _NAT64:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def _is_public(address: IPAddress) -> bool:
    address = _unwrap(address)
    # is_global excludes private, loopback, link-local, CGNAT, reserved and unspecified
    # ranges, but counts most multicast as global, so reject that separately.
    return address.is_global and not address.is_multicast
