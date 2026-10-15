"""The guard: each connection of the fetcher opens here, and nowhere else.

httpcore opens a connection with ``connect_tcp(host, port)`` of its network backend, and it
starts TLS only after that, with the name of the host. ``GuardedBackend`` is that network
backend. For each connection it does three things:

1. It finds the addresses of the host one time: the address in the link, or the answer of DNS.
2. It refuses the connection if one of them is not public (``addresses.blocked_because``).
3. It connects to an address that it checked, and not to the name.

So no second DNS question comes between the check and the connection. A DNS server that gives
a public address to the check and a private address to the connection ("DNS rebinding") gets
no second question. TLS still checks the certificate against the name.
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable
from typing import Any, Protocol, cast

import anyio
import httpcore

from unshelve.net.addresses import IPAddress, address_of_literal, blocked_because

# The codes of getaddrinfo for "this name has no address". Other codes, such as EAI_AGAIN,
# mean that DNS did not answer now.
_NO_SUCH_NAME = frozenset({socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)})


class BlockedAddressError(Exception):
    """The guard refused the destination of a connection."""

    def __init__(self, host: str, reason: str, address: IPAddress | None = None) -> None:
        # "evil.example is 10.0.0.7: private", or "169.254.169.254: link-local"
        named = f"{host} is {address}" if address is not None and str(address) != host else host
        super().__init__(f"{named}: {reason}")
        self.host = host
        self.reason = reason
        self.address = address


class NoSuchHostError(Exception):
    """DNS has no address for the name."""


def default_backend() -> httpcore.AsyncNetworkBackend:
    """The network backend of httpcore for asyncio. httpcore defines this class only when anyio
    is installed, so the type checker does not know its type."""
    return cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())


class Resolver(Protocol):
    async def __call__(self, host: str, port: int) -> list[IPAddress]: ...


async def resolve_with_system(host: str, port: int) -> list[IPAddress]:
    """The addresses of ``host``, from the resolver of the system, each one time."""
    try:
        found = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        if error.errno in _NO_SUCH_NAME:
            raise NoSuchHostError(host) from error
        msg = f"DNS failed for {host}: {error}"
        raise httpcore.ConnectError(msg) from error
    addresses: list[IPAddress] = []
    for *_, socket_address in found:
        address = ipaddress.ip_address(socket_address[0])
        if address not in addresses:
            addresses.append(address)
    return addresses


class GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        resolve: Resolver = resolve_with_system,
        *,
        blocked: Callable[[IPAddress], str | None] = blocked_because,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolve = resolve
        self._blocked = blocked
        self._inner = inner or default_backend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (the signature of httpcore)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._addresses(host, port)
        for address in addresses:
            reason = self._blocked(address)
            if reason is not None:
                raise BlockedAddressError(host, reason, address)
        failures: list[httpcore.ConnectError | httpcore.ConnectTimeout] = []
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                failures.append(error)
        # _addresses never gives an empty list, so each address failed.
        raise failures[-1]

    async def _addresses(self, host: str, port: int) -> list[IPAddress]:
        try:
            literal = address_of_literal(host)
        except ValueError:
            raise BlockedAddressError(host, "not an address") from None
        if literal is not None:
            return [literal]
        addresses = await self._resolve(host, port)
        if not addresses:
            raise NoSuchHostError(host)
        return addresses

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 (the signature of httpcore)
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # A unix socket is a door into this machine, for example to Docker. Never.
        raise BlockedAddressError(path, "unix socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)
