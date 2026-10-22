"""The client for one fixed web service. Each test name is one rule.

The service is a pretend one in the same process (``httpx.MockTransport``): the test sees
each request that the client sends.
"""

import asyncio
import gzip
import json

import httpx
import pytest

from tests.net.support import LocalSite
from unshelve.net.service import ServiceClient, ServiceUnreachableError

BASE = "https://api.example.com"


def _client(
    handler: httpx.MockTransport, *, timeout: float = 5.0, max_bytes: int = 10_000
) -> ServiceClient:
    return ServiceClient(
        BASE,
        headers={"authorization": "Bearer test-key"},
        timeout=timeout,
        max_bytes=max_bytes,
        transport=handler,
    )


def _recording(
    status: int = 200, body: bytes = b'{"ok": true}', headers: dict[str, str] | None = None
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # A stream, as from a real connection: the client reads the bytes as they arrive.
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))

    return httpx.MockTransport(answer), seen


async def test_a_request_goes_to_a_path_of_the_service() -> None:
    transport, seen = _recording()
    client = _client(transport)

    answer = await client.post_json("/v1/embeddings", {"input": ["a text"]})

    assert (answer.status, answer.json()) == (200, {"ok": True})
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.example.com/v1/embeddings"
    assert json.loads(request.content) == {"input": ["a text"]}
    assert request.headers["authorization"] == "Bearer test-key"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept-encoding"] == "gzip, deflate"


@pytest.mark.parametrize(
    "path",
    ["https://other.example/v1/x", "//other.example/v1/x", "v1/embeddings", ""],
)
async def test_a_request_cannot_leave_the_host_of_the_service(path: str) -> None:
    transport, seen = _recording()

    with pytest.raises(ValueError, match="not a path"):
        await _client(transport).post_json(path, {})

    assert seen == []


async def test_a_get_goes_to_a_path_of_the_service_without_a_body() -> None:
    transport, seen = _recording(body=b"%PDF-1.7 made-up bytes")

    answer = await _client(transport).get("/file/report.pdf")

    assert (answer.status, answer.body) == (200, b"%PDF-1.7 made-up bytes")
    [request] = seen
    assert (request.method, str(request.url)) == ("GET", "https://api.example.com/file/report.pdf")
    assert request.content == b""


async def test_a_request_sends_its_own_headers_and_bytes() -> None:
    transport, seen = _recording(body=b"")

    answer = await _client(transport).send(
        "PUT",
        "/bucket/feed.json",
        headers={"content-type": "application/json", "x-amz-date": "20261022T120000Z"},
        content=b'{"items": []}',
    )

    assert answer.status == 200
    [request] = seen
    assert (request.method, str(request.url)) == (
        "PUT",
        "https://api.example.com/bucket/feed.json",
    )
    assert request.content == b'{"items": []}'
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-amz-date"] == "20261022T120000Z"
    # The headers of the client stay.
    assert request.headers["authorization"] == "Bearer test-key"


async def test_the_headers_of_a_request_are_for_that_request_only() -> None:
    transport, seen = _recording()
    client = _client(transport)

    await client.send("GET", "/bucket/feed.json", headers={"x-amz-date": "20261022T120000Z"})
    await client.get("/bucket/feed.json")

    assert [request.headers.get("x-amz-date") for request in seen] == ["20261022T120000Z", None]


async def test_a_request_with_its_own_headers_stays_on_the_host_of_the_service() -> None:
    transport, seen = _recording()

    with pytest.raises(ValueError, match="not a path"):
        await _client(transport).send("PUT", "//other.example/x", headers={}, content=b"x")

    assert seen == []


@pytest.mark.parametrize(
    ("base", "host"),
    [
        ("https://api.example.com", "api.example.com"),
        ("https://api.example.com:443", "api.example.com"),
        ("https://api.example.com:8443", "api.example.com:8443"),
        ("https://bücher.example", "xn--bcher-kva.example"),
    ],
)
async def test_the_host_is_the_host_header_that_the_service_gets(base: str, host: str) -> None:
    transport, seen = _recording()
    client = ServiceClient(base, headers={}, timeout=5.0, max_bytes=100, transport=transport)

    await client.get("/x")

    assert client.host == host
    assert seen[0].headers["host"] == host


async def test_a_get_counts_its_answer_against_the_limit_too() -> None:
    transport, _ = _recording(body=b"x" * 10_001)

    with pytest.raises(ServiceUnreachableError) as caught:
        await _client(transport, max_bytes=10_000).get("/file/large.pdf")

    assert caught.value.reason == "too_large"


async def test_the_error_of_a_wrong_path_does_not_repeat_the_path() -> None:
    # The path of a service can hold a secret: the bot token of Telegram is in the path.
    transport, seen = _recording()

    with pytest.raises(ValueError, match="not a path") as caught:
        await _client(transport).get("bot123:made-up-secret/getFile")

    assert "made-up-secret" not in str(caught.value)
    assert seen == []


@pytest.mark.parametrize("base", ["http://api.example.com", "api.example.com", "https://"])
def test_a_service_needs_an_https_address(base: str) -> None:
    with pytest.raises(ValueError, match="https"):
        ServiceClient(base, headers={}, timeout=1.0, max_bytes=1)


async def test_a_redirect_is_not_followed() -> None:
    transport, seen = _recording(302, b"", {"location": "https://other.example/x"})

    answer = await _client(transport).post_json("/v1/embeddings", {})

    assert answer.status == 302
    assert [str(request.url) for request in seen] == ["https://api.example.com/v1/embeddings"]


async def test_an_answer_longer_than_the_limit_is_refused() -> None:
    transport, _ = _recording(body=b"x" * 10_001)

    with pytest.raises(ServiceUnreachableError) as caught:
        await _client(transport, max_bytes=10_000).post_json("/v1/x", {})

    assert caught.value.reason == "too_large"


async def test_a_compressed_answer_is_counted_after_decompression() -> None:
    small = gzip.compress(b"0" * 100_000)  # about 100 bytes on the wire
    transport, _ = _recording(body=small, headers={"content-encoding": "gzip"})

    with pytest.raises(ServiceUnreachableError) as caught:
        await _client(transport, max_bytes=10_000).post_json("/v1/x", {})

    assert caught.value.reason == "too_large"


async def test_a_compressed_answer_within_the_limit_is_decompressed() -> None:
    transport, _ = _recording(
        body=gzip.compress(b'{"ok": true}'), headers={"content-encoding": "gzip"}
    )

    answer = await _client(transport).post_json("/v1/x", {})

    assert answer.json() == {"ok": True}


async def test_a_service_that_does_not_answer_in_time_is_left() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    with pytest.raises(ServiceUnreachableError) as caught:
        await _client(httpx.MockTransport(slow), timeout=0.05).post_json("/v1/x", {})

    assert caught.value.reason == "timeout"


async def test_no_connection_is_a_network_failure() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ServiceUnreachableError) as caught:
        await _client(httpx.MockTransport(refuse)).post_json("/v1/x", {})

    assert caught.value.reason == "network"


async def test_the_answer_keeps_the_wait_that_the_service_asks_for() -> None:
    transport, _ = _recording(429, b'{"detail": "slow down"}', {"retry-after": "30"})

    answer = await _client(transport).post_json("/v1/x", {})

    assert (answer.status, answer.retry_after) == (429, 30.0)


async def test_the_client_offers_only_the_encodings_that_it_can_read() -> None:
    transport, seen = _recording()
    client = ServiceClient(
        BASE,
        headers={"accept-encoding": "br, zstd"},
        timeout=5.0,
        max_bytes=10_000,
        transport=transport,
    )

    await client.post_json("/v1/x", {})

    assert seen[0].headers["accept-encoding"] == "gzip, deflate"


async def test_a_proxy_from_the_environment_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    # A proxy in the environment would see each request: the key of the service, and the texts.
    proxy = LocalSite()
    await proxy.start()
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    # No pretend transport here: the client is built as in production. No DNS knows a name
    # under .invalid, so the direct way fails at once, and nothing leaves the computer.
    client = ServiceClient(
        "https://unshelve-test.invalid", headers={}, timeout=5.0, max_bytes=1_000
    )
    try:
        with pytest.raises(ServiceUnreachableError):
            await client.post_json("/v1/x", {})
    finally:
        await client.aclose()
        await proxy.stop()

    assert proxy.requests == []
