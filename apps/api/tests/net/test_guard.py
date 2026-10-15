"""The guard: each connection of the fetcher opens there. A pretend network: no socket opens."""

import socket
from typing import Any

import httpcore
import pytest

from tests.net.support import NoNetwork, PretendDNS
from unshelve.net import guard as guard_module
from unshelve.net.guard import (
    BlockedAddressError,
    GuardedBackend,
    NoSuchHostError,
    resolve_with_system,
)

PUBLIC = "93.184.215.14"
PUBLIC_V6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"


async def test_a_name_with_a_public_address_gets_a_connection_to_that_address() -> None:
    network = NoNetwork()
    guard = GuardedBackend(PretendDNS({"example.com": [PUBLIC]}), inner=network)

    await guard.connect_tcp("example.com", 443)

    # The address, and not the name: the network below asks DNS no second question.
    assert network.connections == [(PUBLIC, 443)]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (["10.0.0.7"], "private"),
        (["169.254.169.254"], "link-local"),
        (["::1"], "loopback"),
        ([PUBLIC, "127.0.0.1"], "loopback"),  # one bad address is enough
    ],
)
async def test_a_name_with_an_address_that_is_not_public_gets_no_connection(
    answer: list[str], reason: str
) -> None:
    network = NoNetwork()
    guard = GuardedBackend(PretendDNS({"evil.example": answer}), inner=network)

    with pytest.raises(BlockedAddressError) as caught:
        await guard.connect_tcp("evil.example", 80)

    assert caught.value.reason == reason
    assert network.connections == []


@pytest.mark.parametrize(
    ("host", "reason"),
    [
        ("127.0.0.1", "loopback"),
        ("0x7f.1", "loopback"),
        ("2130706433", "loopback"),
        ("::ffff:127.0.0.1", "loopback"),
        ("fe80::1%25en0", "scoped"),
        ("example.1", "not an address"),
    ],
)
async def test_an_address_in_the_link_is_checked_without_a_dns_question(
    host: str, reason: str
) -> None:
    dns = PretendDNS({})
    network = NoNetwork()

    with pytest.raises(BlockedAddressError) as caught:
        await GuardedBackend(dns, inner=network).connect_tcp(host, 80)

    assert caught.value.reason == reason
    assert dns.questions == []
    assert network.connections == []


async def test_a_public_address_in_the_link_gets_a_connection_without_a_dns_question() -> None:
    dns = PretendDNS({})
    network = NoNetwork()

    await GuardedBackend(dns, inner=network).connect_tcp(PUBLIC_V6, 443)

    assert dns.questions == []
    assert network.connections == [(PUBLIC_V6, 443)]


async def test_a_second_dns_answer_cannot_change_the_address_of_the_connection() -> None:
    # DNS rebinding: the first answer is public, each answer after it is private. A guard that
    # checks the first answer and gives the name to the network would connect to the second.
    dns = PretendDNS({"rebind.example": [PUBLIC]}, later={"rebind.example": ["127.0.0.1"]})
    network = NoNetwork()

    await GuardedBackend(dns, inner=network).connect_tcp("rebind.example", 80)

    assert dns.questions == ["rebind.example"]
    assert network.connections == [(PUBLIC, 80)]


async def test_when_the_first_address_refuses_the_next_one_is_tried() -> None:
    network = NoNetwork(failing=[PUBLIC_V6])
    guard = GuardedBackend(PretendDNS({"two.example": [PUBLIC_V6, PUBLIC]}), inner=network)

    await guard.connect_tcp("two.example", 443)

    assert network.connections == [(PUBLIC_V6, 443), (PUBLIC, 443)]


async def test_when_each_address_refuses_the_error_of_the_network_comes_back() -> None:
    network = NoNetwork(failing=[PUBLIC])
    guard = GuardedBackend(PretendDNS({"down.example": [PUBLIC]}), inner=network)

    with pytest.raises(httpcore.ConnectError):
        await guard.connect_tcp("down.example", 443)


async def test_an_unknown_name_is_no_such_host() -> None:
    guard = GuardedBackend(PretendDNS({}), inner=NoNetwork())

    with pytest.raises(NoSuchHostError):
        await guard.connect_tcp("nothing.example", 80)


async def test_an_empty_answer_of_dns_is_no_such_host() -> None:
    guard = GuardedBackend(PretendDNS({"empty.example": []}), inner=NoNetwork())

    with pytest.raises(NoSuchHostError):
        await guard.connect_tcp("empty.example", 80)


async def test_a_unix_socket_is_never_opened() -> None:
    network = NoNetwork()

    with pytest.raises(BlockedAddressError):
        await GuardedBackend(PretendDNS({}), inner=network).connect_unix_socket(
            "/var/run/docker.sock"
        )


# --- The resolver of the system --------------------------------------------------------------


async def test_the_system_resolver_finds_localhost_without_the_network() -> None:
    addresses = {str(address) for address in await resolve_with_system("localhost", 80)}

    assert addresses & {"127.0.0.1", "::1"}


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (socket.EAI_NONAME, NoSuchHostError),  # the name does not exist: a dead link
        (socket.EAI_AGAIN, httpcore.ConnectError),  # DNS did not answer now: try again later
    ],
)
async def test_a_dns_failure_is_a_dead_name_or_a_network_problem(
    monkeypatch: pytest.MonkeyPatch, code: int, expected: type[Exception]
) -> None:
    async def failing(*args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror(code, "pretend failure")

    monkeypatch.setattr(guard_module.anyio, "getaddrinfo", failing)

    with pytest.raises(expected):
        await resolve_with_system("example.com", 80)
