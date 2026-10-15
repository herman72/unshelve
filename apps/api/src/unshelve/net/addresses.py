"""Which addresses the fetcher may connect to. Pure: no network.

A saved link is text from the internet. Without this check, a link such as
``http://169.254.169.254/`` or ``http://192.168.1.1/`` makes our server open a page inside
the network where it runs: the metadata service of a cloud machine, or the page of a router.
This attack is called SSRF, server-side request forgery.

The rule: the fetcher connects only to a public address. The lists here decide what is
public. They do not depend only on ``ipaddress.is_global``: the standard library calls a
multicast address global, and its lists change between Python versions. A property test
checks that each address that the standard library calls not global is blocked here, too.
"""

import ipaddress
import re
import socket

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_V4 = ipaddress.IPv4Network
_V6 = ipaddress.IPv6Network

# IPv4 networks that are not on the public internet, with a short name for the logs.
BLOCKED_V4: tuple[tuple[ipaddress.IPv4Network, str], ...] = (
    (_V4("0.0.0.0/8"), "this network"),
    (_V4("10.0.0.0/8"), "private"),
    (_V4("100.64.0.0/10"), "shared address space"),  # the network of a provider (CGNAT)
    (_V4("127.0.0.0/8"), "loopback"),
    (_V4("169.254.0.0/16"), "link-local"),  # also the metadata service of a cloud machine
    (_V4("172.16.0.0/12"), "private"),
    (_V4("192.0.0.0/24"), "protocol assignments"),
    (_V4("192.0.2.0/24"), "documentation"),
    (_V4("192.88.99.0/24"), "6to4 relay"),
    (_V4("192.168.0.0/16"), "private"),
    (_V4("198.18.0.0/15"), "benchmarking"),
    (_V4("198.51.100.0/24"), "documentation"),
    (_V4("203.0.113.0/24"), "documentation"),
    (_V4("224.0.0.0/4"), "multicast"),
    (_V4("240.0.0.0/4"), "reserved"),  # also 255.255.255.255
)

# IPv6 networks with a name. The order matters: the first match gives the name.
NAMED_V6: tuple[tuple[ipaddress.IPv6Network, str], ...] = (
    (_V6("::/128"), "unspecified"),
    (_V6("::1/128"), "loopback"),
    (_V6("::/96"), "IPv4-compatible"),  # an old form: "::127.0.0.1"
    (_V6("64:ff9b:1::/48"), "local NAT64"),
    (_V6("100::/64"), "discard"),
    (_V6("fc00::/7"), "unique local"),
    (_V6("fe80::/10"), "link-local"),
    (_V6("fec0::/10"), "site-local"),
    (_V6("ff00::/8"), "multicast"),
)
# Only this part of the IPv6 space is public, and the networks below are parts of it that
# are not.
GLOBAL_V6 = _V6("2000::/3")
BLOCKED_V6: tuple[tuple[ipaddress.IPv6Network, str], ...] = (
    (_V6("2001::/23"), "protocol assignments"),  # also Teredo, 2001::/32
    (_V6("2001:db8::/32"), "documentation"),
    (_V6("2002::/16"), "6to4"),
    (_V6("3fff::/20"), "documentation"),
)
# A NAT64 gateway connects "64:ff9b::a.b.c.d" to the IPv4 address a.b.c.d.
NAT64 = _V6("64:ff9b::/96")


def blocked_because(address: IPAddress) -> str | None:
    """Why the fetcher must not connect to ``address``, or None for a public address."""
    if isinstance(address, ipaddress.IPv4Address):
        return _first(address, BLOCKED_V4)
    if address.scope_id is not None:
        # "fe80::1%en0": an address that has a meaning only on one network card of this machine.
        return "scoped"
    inside = address.ipv4_mapped or _from_nat64(address)
    if inside is not None:
        # "::ffff:127.0.0.1" reaches 127.0.0.1. The IPv4 address inside decides.
        return blocked_because(inside)
    named = _first(address, NAMED_V6)
    if named is not None:
        return named
    if address not in GLOBAL_V6:
        return "not global"
    return _first(address, BLOCKED_V6)


def _first(
    address: IPAddress,
    networks: tuple[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str], ...],
) -> str | None:
    return next((name for network, name in networks if address in network), None)


def _from_nat64(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address not in NAT64:
        return None
    return ipaddress.IPv4Address(int(address) & 0xFFFF_FFFF)


# A host "ends in a number" when its last label is a decimal or a hexadecimal number. No DNS
# name does, so such a host is an IPv4 address, maybe in an old form ("127.1", "0x7f.1",
# "2130706433"). The rule is the one of the URL standard of the browsers (WHATWG).
_NUMBER = re.compile(r"(0x[0-9a-f]*|[0-9]+)", re.IGNORECASE)


def address_of_literal(host: str) -> IPAddress | None:
    """The address that ``host`` is, or None when ``host`` is a name for DNS.

    The fetcher reads each form of an address itself. So no DNS question is ever asked for an
    address, and the old forms cannot slip past the check. Raises ``ValueError`` for a host
    that ends in a number but is no address.
    """
    if ":" in host:
        # An IPv6 address. httpx writes the zone of "fe80::1%en0" as "%25en0".
        return ipaddress.IPv6Address(host.replace("%25", "%", 1))
    trimmed = host.removesuffix(".")
    if not _NUMBER.fullmatch(trimmed.rsplit(".", 1)[-1]):
        return None
    try:
        # inet_aton reads the old forms, as the system resolver and the browsers do.
        return ipaddress.IPv4Address(socket.inet_aton(trimmed))
    except OSError as error:
        msg = f"not an address: {host}"
        raise ValueError(msg) from error
