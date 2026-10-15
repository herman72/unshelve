"""A client for one fixed web service, such as the API of the embedding provider (ADR 0012).

It is not the fetcher: it never opens a saved link. The address of the service is written in
the code, and a request can only go to a path at that address. Around that rule, it sets the
same kind of limits as the fetcher:

- Only https.
- A path is never a whole address, so a request cannot leave the host of the service.
- No redirect is followed. A service that sends one gives an answer with that status.
- At most ``max_bytes`` of an answer, counted before and after decompression.
- At most ``timeout`` seconds for one request, from the connection to the last byte.
- No proxy from the environment.
"""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from unshelve.net.bodies import (
    ACCEPT_ENCODING,
    BodyTooLargeError,
    BrokenBodyError,
    UnknownEncodingError,
    read_body,
)
from unshelve.net.fetcher import retry_after_seconds

# What carries a request. The tests give a pretend service; the code outside this package
# names the type without importing an HTTP client.
type Transport = httpx.AsyncBaseTransport


class ServiceUnreachableError(Exception):
    """The service gave no usable answer: ``timeout``, ``too_large`` or ``network``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ServiceAnswer:
    status: int
    body: bytes
    # The seconds that the service asks us to wait ("Retry-After"), if it says so.
    retry_after: float | None = None

    def json(self) -> object:
        return json.loads(self.body)


class ServiceClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
        transport: Transport | None = None,
    ) -> None:
        """``transport`` is for the tests: a pretend service in the same process."""
        base = httpx.URL(base_url)
        if base.scheme != "https" or not base.host:
            raise ValueError(f"a service needs an https address, not {base_url!r}")
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._client = httpx.AsyncClient(
            base_url=base,
            headers={**headers, "accept-encoding": ACCEPT_ENCODING},
            # Each step (connect, write, read) may take the whole time. The request as a whole
            # may too: ``post_json`` stops it after ``timeout`` seconds.
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post_json(self, path: str, body: object) -> ServiceAnswer:
        """Send ``body`` as JSON to ``path`` of the service, and read the whole answer."""
        return await self._send("POST", path, body)

    async def get(self, path: str) -> ServiceAnswer:
        """Ask for ``path`` of the service, and read the whole answer."""
        return await self._send("GET", path, None)

    async def _send(self, method: str, path: str, body: object) -> ServiceAnswer:
        if not path.startswith("/") or path.startswith("//"):
            # Without the path: the path of a service can hold a secret (the bot token).
            raise ValueError("not a path of the service: a path starts with one '/'")
        try:
            async with (
                asyncio.timeout(self._timeout),
                self._client.stream(method, path, json=body) as response,
            ):
                data = await read_body(
                    response.aiter_raw(),
                    encoding=response.headers.get("content-encoding"),
                    max_bytes=self._max_bytes,
                )
        except (TimeoutError, httpx.TimeoutException):
            raise ServiceUnreachableError("timeout") from None
        except BodyTooLargeError as error:
            raise ServiceUnreachableError("too_large") from error
        except (httpx.TransportError, BrokenBodyError, UnknownEncodingError) as error:
            raise ServiceUnreachableError("network") from error
        return ServiceAnswer(
            status=response.status_code,
            body=data,
            retry_after=retry_after_seconds(response.headers.get("retry-after")),
        )
