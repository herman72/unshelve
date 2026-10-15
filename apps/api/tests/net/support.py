"""Helpers for the tests of the fetcher: a pretend DNS, a record of each connection, and a
small web server on 127.0.0.1 that can send what a real server should never send."""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import httpcore

from unshelve.net.addresses import IPAddress
from unshelve.net.guard import NoSuchHostError, default_backend

# --- DNS and connections -------------------------------------------------------------------


class PretendDNS:
    """Answers from a table. ``later`` gives a different answer from the second question on,
    as a DNS server does in DNS rebinding."""

    def __init__(
        self, table: dict[str, list[str]], *, later: dict[str, list[str]] | None = None
    ) -> None:
        self._table = table
        self._later = later or {}
        self.questions: list[str] = []

    async def __call__(self, host: str, port: int) -> list[IPAddress]:
        asked_before = host in self.questions
        self.questions.append(host)
        texts = self._later.get(host) if asked_before else None
        texts = texts if texts is not None else self._table.get(host)
        if texts is None:
            raise NoSuchHostError(host)
        return [ipaddress.ip_address(text) for text in texts]


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Opens real connections, and writes down each one. The guard calls it only after its
    check, so a test can prove that a refused address got no connection."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner = inner or default_backend()
        self.connections: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (the signature of httpcore)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connections.append((host, port))
        return await self._inner.connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class NoNetwork(httpcore.AsyncNetworkBackend):
    """Writes down each connection and opens none. Addresses in ``failing`` refuse."""

    def __init__(self, failing: Iterable[str] = ()) -> None:
        self.connections: list[tuple[str, int]] = []
        self._failing = set(failing)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (the signature of httpcore)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connections.append((host, port))
        if host in self._failing:
            raise httpcore.ConnectError(f"refused: {host}")
        return httpcore.AsyncNetworkStream()

    async def sleep(self, seconds: float) -> None:
        return None


# --- A web server for the tests --------------------------------------------------------------


@dataclass
class Request:
    method: str
    target: str
    headers: dict[str, str]


Handler = Callable[[Request, asyncio.StreamWriter], Awaitable[None]]


def answer(
    status: int = 200,
    body: bytes = b"",
    headers: Sequence[tuple[str, str]] = (),
    *,
    content_type: str | None = "text/plain",
) -> Handler:
    """A handler that sends one whole answer, with its length."""

    async def handle(request: Request, writer: asyncio.StreamWriter) -> None:
        lines = [f"HTTP/1.1 {status} {HTTPStatus(status).phrase}"]
        if content_type is not None:
            lines.append(f"Content-Type: {content_type}")
        lines += [f"{name}: {value}" for name, value in headers]
        lines += [f"Content-Length: {len(body)}", "Connection: close", "", ""]
        writer.write("\r\n".join(lines).encode() + body)
        await writer.drain()

    return handle


def redirect(location: str, status: int = 301) -> Handler:
    return answer(status, headers=[("Location", location)], content_type=None)


def head_then_wait(
    head: Sequence[str], *, then: Callable[[asyncio.StreamWriter], Awaitable[None]]
) -> Handler:
    """A handler that sends the status line and the headers, and then does ``then``."""

    async def handle(request: Request, writer: asyncio.StreamWriter) -> None:
        writer.write(("\r\n".join(["HTTP/1.1 200 OK", *head, "", ""])).encode())
        await writer.drain()
        await then(writer)

    return handle


async def wait_long(writer: asyncio.StreamWriter) -> None:
    await asyncio.sleep(30)


@dataclass
class LocalSite:
    """A web server on 127.0.0.1 with one handler for each path."""

    routes: dict[str, Handler] = field(default_factory=dict[str, Handler])
    requests: list[Request] = field(default_factory=list[Request])
    port: int = 0
    _server: asyncio.Server | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def url(self, path: str, host: str = "site.test") -> str:
        return f"http://{host}:{self.port}{path}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            request_line = (await reader.readline()).decode("latin-1").strip()
            if not request_line:
                return
            method, target, _ = request_line.split(" ", 2)
            headers: dict[str, str] = {}
            while (line := (await reader.readline()).decode("latin-1").strip()) != "":
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
            request = Request(method, target, headers)
            self.requests.append(request)
            path = target.split("?", 1)[0]
            handler = self.routes.get(path, answer(404, b"no such page"))
            await handler(request, writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._tasks.discard(task)
            writer.close()


def a_closed_port() -> int:
    """A port on 127.0.0.1 where nothing listens."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
