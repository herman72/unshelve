"""Which addresses the fetcher may connect to. Each row of the tables is one address."""

import ipaddress

import pytest
from hypothesis import given
from hypothesis import strategies as st

from unshelve.net.addresses import IPAddress, address_of_literal, blocked_because

NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
GLOBAL_V6 = ipaddress.IPv6Network("2000::/3")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        # Public addresses: the fetcher may connect.
        ("8.8.8.8", None),
        ("1.1.1.1", None),
        ("172.32.0.1", None),  # one step after the private 172.16.0.0/12
        ("100.128.0.1", None),  # one step after the shared 100.64.0.0/10
        ("2606:4700:4700::1111", None),
        ("2001:4860:4860::8888", None),
        ("::ffff:8.8.8.8", None),  # a public IPv4 address in an IPv6 form
        ("64:ff9b::808:808", None),  # 8.8.8.8 through a NAT64 gateway
        # IPv4 addresses that are not on the public internet.
        ("127.0.0.1", "loopback"),
        ("127.255.255.254", "loopback"),
        ("0.0.0.0", "this network"),  # noqa: S104 (a test value, nothing listens on it)
        ("10.0.0.1", "private"),
        ("172.16.0.1", "private"),
        ("172.31.255.255", "private"),
        ("192.168.1.1", "private"),
        ("169.254.169.254", "link-local"),  # the metadata service of a cloud machine
        ("100.64.0.1", "shared address space"),
        ("100.127.255.255", "shared address space"),
        ("192.0.0.8", "protocol assignments"),
        ("192.0.2.1", "documentation"),
        ("198.18.0.1", "benchmarking"),
        ("224.0.0.1", "multicast"),
        ("239.255.255.250", "multicast"),
        ("240.0.0.1", "reserved"),
        ("255.255.255.255", "reserved"),
        # IPv6 addresses that are not on the public internet.
        ("::1", "loopback"),
        ("::", "unspecified"),
        ("::ffff:127.0.0.1", "loopback"),  # IPv4 inside IPv6: the IPv4 address decides
        ("::ffff:169.254.169.254", "link-local"),
        ("64:ff9b::7f00:1", "loopback"),  # 127.0.0.1 through a NAT64 gateway
        ("::127.0.0.1", "IPv4-compatible"),
        ("64:ff9b:1::1", "local NAT64"),
        ("100::1", "discard"),
        ("fe80::1", "link-local"),
        ("fe80::1%en0", "scoped"),
        ("fc00::1", "unique local"),
        ("fd12:3456::1", "unique local"),
        ("fec0::1", "site-local"),
        ("ff02::1", "multicast"),
        ("2001::1", "protocol assignments"),  # Teredo
        ("2001:db8::1", "documentation"),
        ("2002:7f00:1::1", "6to4"),
        ("3fff::1", "documentation"),
        ("4000::1", "not global"),
        ("5f00::1", "not global"),
    ],
)
def test_an_address_is_public_or_has_a_reason(text: str, reason: str | None) -> None:
    assert blocked_because(ipaddress.ip_address(text)) == reason


@pytest.mark.parametrize(
    ("host", "address"),
    [
        ("127.0.0.1", "127.0.0.1"),
        # Old forms of an IPv4 address. Browsers and the system resolver read them, too.
        ("127.1", "127.0.0.1"),
        ("0x7f.1", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("2130706433", "127.0.0.1"),
        ("017700000001", "127.0.0.1"),  # octal
        ("0177.0.0.1", "127.0.0.1"),
        ("0", "0.0.0.0"),  # noqa: S104 (a test value, nothing listens on it)
        ("1.2.3.4.", "1.2.3.4"),  # a dot at the end changes nothing
        ("::1", "::1"),
        ("::ffff:127.0.0.1", "::ffff:127.0.0.1"),
        ("fe80::1%25en0", "fe80::1%en0"),  # httpx writes the zone with "%25"
        # Names: DNS gives their addresses.
        ("example.com", None),
        ("1.example", None),
        ("xn--bcher-kva.example", None),
    ],
)
def test_a_host_is_read_as_an_address_or_as_a_name(host: str, address: str | None) -> None:
    expected = ipaddress.ip_address(address) if address is not None else None

    assert address_of_literal(host) == expected


@pytest.mark.parametrize("host", ["example.1", "256.1.1.1", "1.2.3.4.5", "0x1g.1"])
def test_a_host_that_ends_in_a_number_must_be_an_address(host: str) -> None:
    # No DNS name ends in a number. Such a host is an address in a form that we cannot read.
    with pytest.raises(ValueError, match="not an address"):
        address_of_literal(host)


# --- Rules for each address --------------------------------------------------------------


@given(st.ip_addresses(v=4))
def test_each_ipv4_address_that_python_calls_not_global_is_blocked(
    address: ipaddress.IPv4Address,
) -> None:
    if not address.is_global:
        assert blocked_because(address) is not None


@given(st.ip_addresses(v=6))
def test_each_ipv6_address_that_python_calls_not_global_is_blocked(
    address: ipaddress.IPv6Address,
) -> None:
    # The two forms with an IPv4 address inside are decided by that IPv4 address.
    if address.ipv4_mapped is None and address not in NAT64 and not address.is_global:
        assert blocked_because(address) is not None


@given(st.ip_addresses(v=6))
def test_an_ipv6_address_outside_the_global_space_is_blocked(
    address: ipaddress.IPv6Address,
) -> None:
    if address.ipv4_mapped is None and address not in NAT64 and address not in GLOBAL_V6:
        assert blocked_because(address) is not None


@given(st.ip_addresses(v=4))
def test_an_ipv4_address_in_an_ipv6_form_gets_the_same_answer(
    address: ipaddress.IPv4Address,
) -> None:
    mapped: IPAddress = ipaddress.IPv6Address(f"::ffff:{address}")
    through_nat64: IPAddress = ipaddress.IPv6Address(int(NAT64.network_address) + int(address))

    assert blocked_because(mapped) == blocked_because(address)
    assert blocked_because(through_nat64) == blocked_because(address)
