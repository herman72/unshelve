"""The fetcher: it opens a saved link on the internet, for the extractors.

It is the only code that opens an address that came from outside. Each connection passes the
guard (``guard.GuardedBackend``): only a public address. Around the guard, the fetcher sets
the other limits:

- Only http and https, and only the ports of the web (80 and 443).
- At most 5 redirects. Each new connection passes the guard again.
- At most ``max_bytes``, counted before and after decompression (``bodies.read_body``).
- At most ``timeout`` seconds for the whole fetch, with its redirects. So a server that sends
  one byte each second cannot hold the worker for ever.
- A pause between two requests to the same host (``pacing.HostPacer``).
- No cookies, no credentials from the link, and no proxy from the environment.
"""

import asyncio
import email.utils
import itertools
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

import httpcore
import httpx
import structlog

from unshelve.config import Settings
from unshelve.contracts.dto import FetchResult
from unshelve.contracts.errors import FetchError
from unshelve.contracts.protocols import accepts
from unshelve.net.addresses import IPAddress, blocked_because
from unshelve.net.bodies import (
    ACCEPT_ENCODING,
    BodyTooLargeError,
    BrokenBodyError,
    UnknownEncodingError,
    read_body,
)
from unshelve.net.guard import (
    BlockedAddressError,
    GuardedBackend,
    NoSuchHostError,
    Resolver,
    resolve_with_system,
)
from unshelve.net.pacing import HostPacer

log = structlog.get_logger()

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; Unshelve/0.1)"
WEB_PORTS = frozenset({80, 443})
MAX_REDIRECTS = 5
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_DEFAULT_PORTS = {"http": 80, "https": 443}
# A server can ask us to wait ("Retry-After"). More than one day is read as one day.
_LONGEST_WAIT = 24 * 60 * 60.0


class Pacer(Protocol):
    async def wait(self, host: str) -> float: ...


class LiveFetcher:
    """The real fetcher. It implements ``contracts.protocols.Fetcher``."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        host_interval: float = 1.0,
        max_redirects: int = MAX_REDIRECTS,
        allowed_ports: frozenset[int] = WEB_PORTS,
        resolve: Resolver = resolve_with_system,
        blocked: Callable[[IPAddress], str | None] = blocked_because,
        inner_backend: httpcore.AsyncNetworkBackend | None = None,
        pacer: Pacer | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout
        self._max_redirects = max_redirects
        self._allowed_ports = allowed_ports
        self._pacer: Pacer = pacer or HostPacer(host_interval)
        # httpcore, and not httpx: httpcore lets us give the network backend, so the guard
        # opens each connection. It reads no proxy from the environment and keeps no cookies.
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=GuardedBackend(resolve, blocked=blocked, inner=inner_backend),
            max_connections=10,
            retries=0,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "LiveFetcher":
        return cls(
            user_agent=settings.fetch_user_agent,
            timeout=settings.fetch_timeout,
            host_interval=settings.fetch_host_interval,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                result, redirects = await self._follow(url, max_bytes=max_bytes, accept=accept)
        except TimeoutError:
            error = FetchError("timeout", url)
            self._log_failure(error, started)
            raise error from None
        except FetchError as error:
            self._log_failure(error, started)
            raise
        log.info(
            "fetch.done",
            host=_host_of(url),
            status=result.status,
            bytes=len(result.body),
            redirects=redirects,
            seconds=round(time.perf_counter() - started, 3),
        )
        return result

    async def _follow(
        self, url: str, *, max_bytes: int, accept: Sequence[str]
    ) -> tuple[FetchResult, int]:
        address = self._checked(url, url)
        for redirects in itertools.count():
            await self._pacer.wait(address.host)
            try:
                async with self._pool.stream(
                    "GET",
                    httpcore.URL(
                        scheme=address.raw_scheme,
                        host=address.raw_host,
                        port=address.port,
                        target=address.raw_path,
                    ),
                    headers=self._headers(address, accept),
                    extensions={
                        "timeout": dict.fromkeys(
                            ("connect", "read", "write", "pool"), self._timeout
                        )
                    },
                ) as response:
                    status = response.status
                    location = _header(response.headers, "location")
                    if status in _REDIRECTS and location:
                        if redirects == self._max_redirects:
                            raise FetchError("http_error", url, status=status)
                        address = self._checked(str(address.join(location)), url)
                        continue
                    if not 200 <= status < 300:
                        wait = _seconds(_header(response.headers, "retry-after"))
                        raise FetchError("http_error", url, status=status, retry_after=wait)
                    content_type = _header(response.headers, "content-type")
                    if not accepts(content_type, accept):
                        raise FetchError("unsupported", url)
                    length = _header(response.headers, "content-length")
                    if length is not None and length.isdigit() and int(length) > max_bytes:
                        raise FetchError("too_large", url)
                    body = await read_body(
                        response.aiter_stream(),
                        encoding=_header(response.headers, "content-encoding"),
                        max_bytes=max_bytes,
                    )
            except BlockedAddressError as error:
                log.warning(
                    "fetch.blocked",
                    host=error.host,
                    address=str(error.address) if error.address is not None else None,
                    reason=error.reason,
                )
                raise FetchError("blocked_address", url) from error
            except NoSuchHostError as error:
                raise FetchError("no_such_host", url) from error
            except BodyTooLargeError as error:
                raise FetchError("too_large", url) from error
            except (UnknownEncodingError, httpcore.UnsupportedProtocol) as error:
                raise FetchError("unsupported", url) from error
            except httpcore.TimeoutException as error:
                raise FetchError("timeout", url) from error
            except (httpcore.NetworkError, httpcore.ProtocolError, BrokenBodyError) as error:
                # A retry can get a whole body, or a connection that works.
                raise FetchError("network", url) from error
            result = FetchResult(
                url=url,
                final_url=str(address.copy_with(username=None, password=None)),
                status=status,
                content_type=content_type,
                body=body,
            )
            return result, redirects
        raise AssertionError("unreachable")  # pragma: no cover

    def _checked(self, text: str, url: str) -> httpx.URL:
        """``text`` as an address that the fetcher may ask for, or a ``FetchError``."""
        try:
            address = httpx.URL(text)
        except (httpx.InvalidURL, ValueError, TypeError) as error:
            raise FetchError("unsupported", url) from error
        if address.scheme not in _DEFAULT_PORTS or not address.host:
            raise FetchError("unsupported", url)
        port = address.port or _DEFAULT_PORTS[address.scheme]
        if port not in self._allowed_ports:
            # A port such as 22 or 6379 is a service, not a web page.
            refused = BlockedAddressError(address.host, f"port {port}")
            log.warning("fetch.blocked", host=address.host, address=None, reason=refused.reason)
            raise FetchError("blocked_address", url) from refused
        return address

    def _headers(self, address: httpx.URL, accept: Sequence[str]) -> list[tuple[bytes, bytes]]:
        return [
            (b"Host", host_header(address).encode("ascii")),
            (b"User-Agent", self._user_agent.encode()),
            (b"Accept", (", ".join(accept) or "*/*").encode()),
            (b"Accept-Encoding", ACCEPT_ENCODING.encode()),
        ]

    def _log_failure(self, error: FetchError, started: float) -> None:
        log.info(
            "fetch.failed",
            host=_host_of(error.url),
            reason=error.reason,
            status=error.status,
            seconds=round(time.perf_counter() - started, 3),
        )


def host_header(address: httpx.URL) -> str:
    """The value of the header "Host": the host, with the port when it is not the default."""
    host = address.raw_host.decode("ascii")
    if ":" in host:
        host = f"[{host}]"  # an IPv6 address
    if address.port is not None and address.port != _DEFAULT_PORTS.get(address.scheme):
        host = f"{host}:{address.port}"
    return host


def _header(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """The value of a response header. A header that comes more than one time is joined."""
    found = [
        value.decode("latin-1") for key, value in headers if key.decode("latin-1").lower() == name
    ]
    return ", ".join(found) if found else None


def _seconds(retry_after: str | None) -> float | None:
    """The seconds of "Retry-After": a number, or a date."""
    if retry_after is None:
        return None
    text = retry_after.strip()
    if text.isdigit():
        seconds = float(text)
    else:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), _LONGEST_WAIT)


def _host_of(url: str) -> str:
    """The host of a link, for the log. The path and the query can hold private words."""
    try:
        return httpx.URL(url).host or "?"
    except (httpx.InvalidURL, ValueError, TypeError):
        return "?"
