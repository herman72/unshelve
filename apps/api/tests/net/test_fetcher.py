"""The fetcher with a real HTTP connection, to a small web server on 127.0.0.1.

In these tests 127.0.0.1 counts as public, so that the test server can answer. Each other
address follows the real rules. The last part uses the real rules for each address.
"""

import asyncio
import gzip
import ipaddress
import time
import zlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from tests.net.support import (
    LocalSite,
    NoNetwork,
    PretendDNS,
    RecordingBackend,
    a_closed_port,
    answer,
    head_then_wait,
    redirect,
    wait_long,
)
from unshelve.contracts.errors import FetchError
from unshelve.net.addresses import IPAddress, blocked_because
from unshelve.net.fetcher import LiveFetcher, host_header
from unshelve.testing.conformance import FetcherCase, FetcherConformance

LOCAL = ipaddress.IPv4Address("127.0.0.1")
MEGABYTE = 1024 * 1024

MakeFetcher = Callable[..., LiveFetcher]


def _rule_of_the_tests(address: IPAddress) -> str | None:
    """127.0.0.1 counts as public, so that the test server can answer."""
    return None if address == LOCAL else blocked_because(address)


@pytest.fixture
async def site() -> AsyncIterator[LocalSite]:
    site = LocalSite()
    await site.start()
    yield site
    await site.stop()


@pytest.fixture
def network() -> RecordingBackend:
    return RecordingBackend()


@pytest.fixture
async def make_fetcher(site: LocalSite, network: RecordingBackend) -> AsyncIterator[MakeFetcher]:
    made: list[LiveFetcher] = []

    def make(**options: Any) -> LiveFetcher:
        settings: dict[str, Any] = {
            "user_agent": "UnshelveTest/1",
            "timeout": 2.0,
            "host_interval": 0.0,
            "allowed_ports": frozenset({80, 443, site.port}),
            "resolve": PretendDNS(
                {
                    "site.test": ["127.0.0.1"],
                    "private.test": ["10.0.0.7"],
                    "mixed.test": ["127.0.0.1", "10.0.0.7"],
                }
            ),
            "blocked": _rule_of_the_tests,
            "inner_backend": network,
        }
        fetcher = LiveFetcher(**(settings | options))
        made.append(fetcher)
        return fetcher

    yield make
    for fetcher in made:
        await fetcher.aclose()


async def _failure(fetcher: LiveFetcher, url: str, **options: Any) -> FetchError:
    options.setdefault("max_bytes", 10_000)
    options.setdefault("accept", [])
    with pytest.raises(FetchError) as caught:
        await fetcher.get(url, **options)
    return caught.value


class TestLiveFetcher(FetcherConformance):
    @pytest.fixture
    async def case(self, site: LocalSite, make_fetcher: MakeFetcher) -> FetcherCase:  # type: ignore[override]
        site.routes["/hello"] = answer(200, b"hello")
        site.routes["/large"] = answer(200, b"x" * 2000)
        return FetcherCase(
            fetcher=make_fetcher(),
            ok_url=site.url("/hello"),
            missing_url=site.url("/missing"),
            large_url=site.url("/large"),
        )


# --- Redirects -----------------------------------------------------------------------------


async def test_a_redirect_is_followed_to_the_final_address(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    site.routes["/old"] = redirect("/new")
    site.routes["/new"] = answer(200, b"moved here")

    result = await make_fetcher().get(site.url("/old"), max_bytes=1000, accept=[])

    assert (result.url, result.final_url) == (site.url("/old"), site.url("/new"))
    assert result.body == b"moved here"


async def test_a_relative_redirect_is_read_from_the_address_of_its_hop(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    site.routes["/a/b"] = redirect("../c?x=1")
    site.routes["/c"] = answer(200, b"here")

    result = await make_fetcher().get(site.url("/a/b"), max_bytes=1000, accept=[])

    assert result.final_url == site.url("/c?x=1")


async def test_a_redirect_to_a_private_address_gets_no_connection(
    site: LocalSite, make_fetcher: MakeFetcher, network: RecordingBackend
) -> None:
    site.routes["/to-private"] = redirect(site.url("/", host="private.test"))

    error = await _failure(make_fetcher(), site.url("/to-private"))

    assert error.reason == "blocked_address"
    assert network.connections == [("127.0.0.1", site.port)]  # the first hop only


async def test_a_redirect_to_the_metadata_service_gets_no_connection(
    site: LocalSite, make_fetcher: MakeFetcher, network: RecordingBackend
) -> None:
    site.routes["/to-metadata"] = redirect("http://169.254.169.254/latest/meta-data/")

    error = await _failure(make_fetcher(), site.url("/to-metadata"))

    assert error.reason == "blocked_address"
    assert network.connections == [("127.0.0.1", site.port)]


@pytest.mark.parametrize(
    "location", ["file:///etc/passwd", "ftp://site.test/file", "gopher://site.test:70/_x"]
)
async def test_a_redirect_out_of_the_web_is_unsupported(
    site: LocalSite, make_fetcher: MakeFetcher, network: RecordingBackend, location: str
) -> None:
    site.routes["/away"] = redirect(location)

    error = await _failure(make_fetcher(), site.url("/away"))

    assert error.reason == "unsupported"
    assert network.connections == [("127.0.0.1", site.port)]


async def test_too_many_redirects_is_an_http_error(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    site.routes["/loop"] = redirect("/loop")

    error = await _failure(make_fetcher(), site.url("/loop"))

    assert (error.reason, error.status) == ("http_error", 301)
    assert len(site.requests) == 6  # the first request, and 5 redirects


async def test_a_redirect_without_a_location_is_an_http_error(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    site.routes["/nowhere"] = answer(302, content_type=None)

    error = await _failure(make_fetcher(), site.url("/nowhere"))

    assert (error.reason, error.status) == ("http_error", 302)


async def test_each_hop_waits_for_the_turn_of_its_host(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    class Pacer:
        def __init__(self) -> None:
            self.hosts: list[str] = []

        async def wait(self, host: str) -> float:
            self.hosts.append(host)
            return 0.0

    pacer = Pacer()
    site.routes["/old"] = redirect("/new")
    site.routes["/new"] = answer(200, b"here")

    await make_fetcher(pacer=pacer).get(site.url("/old"), max_bytes=1000, accept=[])

    assert pacer.hosts == ["site.test", "site.test"]


# --- Where a connection may go -----------------------------------------------------------------


@pytest.mark.parametrize("host", ["private.test", "mixed.test"])
async def test_a_name_with_an_address_that_is_not_public_gets_no_connection(
    site: LocalSite, make_fetcher: MakeFetcher, network: RecordingBackend, host: str
) -> None:
    error = await _failure(make_fetcher(), site.url("/", host=host))

    assert (error.reason, error.retryable) == ("blocked_address", False)
    assert network.connections == []


async def test_each_new_connection_is_checked_again(
    site: LocalSite, make_fetcher: MakeFetcher, network: RecordingBackend
) -> None:
    # The name is public for the first hop and private for the second (DNS rebinding).
    dns = PretendDNS({"rebind.test": ["127.0.0.1"]}, later={"rebind.test": ["10.0.0.7"]})
    site.routes["/old"] = redirect("/new")

    error = await _failure(make_fetcher(resolve=dns), site.url("/old", host="rebind.test"))

    assert error.reason == "blocked_address"
    assert network.connections == [("127.0.0.1", site.port)]


async def test_an_unknown_name_is_a_dead_link(site: LocalSite, make_fetcher: MakeFetcher) -> None:
    error = await _failure(make_fetcher(), site.url("/", host="nothing.test"))

    assert (error.reason, error.retryable) == ("no_such_host", False)


async def test_a_port_that_is_not_for_the_web_gets_no_connection(
    site: LocalSite, make_fetcher: MakeFetcher, network: RecordingBackend
) -> None:
    fetcher = make_fetcher(allowed_ports=frozenset({80, 443}))

    for url in (site.url("/hello"), "http://site.test:22/", "https://site.test:6379/"):
        assert (await _failure(fetcher, url)).reason == "blocked_address"
    assert network.connections == []


@pytest.mark.parametrize(
    "url",
    [
        "ftp://site.test/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "not a link",
        "http://",
        "https:///path",
    ],
)
async def test_an_address_that_is_not_for_the_web_is_unsupported(
    make_fetcher: MakeFetcher, network: RecordingBackend, url: str
) -> None:
    assert (await _failure(make_fetcher(), url)).reason == "unsupported"
    assert network.connections == []


# --- Size and time -----------------------------------------------------------------------------


async def test_a_declared_length_over_the_limit_is_refused_before_the_body_comes(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    head = ["Content-Type: text/plain", "Content-Length: 10000000"]
    site.routes["/huge"] = head_then_wait(head, then=wait_long)
    started = time.perf_counter()

    error = await _failure(make_fetcher(), site.url("/huge"))

    assert error.reason == "too_large"
    assert time.perf_counter() - started < 1.0  # it did not wait for the body


async def test_a_body_without_a_length_is_cut_at_the_limit(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    async def endless(writer: asyncio.StreamWriter) -> None:
        for _ in range(10_000):  # 10 MB, if nobody stops it
            writer.write(b"3e8\r\n" + b"x" * 1000 + b"\r\n")
            await writer.drain()

    head = ["Content-Type: text/plain", "Transfer-Encoding: chunked"]
    site.routes["/endless"] = head_then_wait(head, then=endless)

    assert (await _failure(make_fetcher(), site.url("/endless"), max_bytes=5000)).reason == (
        "too_large"
    )


async def test_a_gzip_page_is_decompressed(site: LocalSite, make_fetcher: MakeFetcher) -> None:
    text = "<p>یک متن فارسی, and English.</p>" * 50
    packed = gzip.compress(text.encode())
    site.routes["/page"] = answer(
        200, packed, [("Content-Encoding", "gzip")], content_type="text/html; charset=utf-8"
    )

    result = await make_fetcher().get(site.url("/page"), max_bytes=100_000, accept=["text/html"])

    assert result.body.decode() == text
    assert result.content_type == "text/html; charset=utf-8"
    assert site.requests[0].headers["accept-encoding"] == "gzip, deflate"


async def test_a_gzip_bomb_stops_at_the_limit(site: LocalSite, make_fetcher: MakeFetcher) -> None:
    packer = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    bomb = b"".join([packer.compress(bytes(MEGABYTE)) for _ in range(10)] + [packer.flush()])
    site.routes["/bomb"] = answer(200, bomb, [("Content-Encoding", "gzip")])

    error = await _failure(make_fetcher(), site.url("/bomb"), max_bytes=100_000)

    assert error.reason == "too_large"


async def test_a_broken_gzip_body_can_be_tried_again(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    site.routes["/broken"] = answer(200, b"this is not gzip", [("Content-Encoding", "gzip")])

    error = await _failure(make_fetcher(), site.url("/broken"))

    assert (error.reason, error.retryable) == ("network", True)


async def test_an_encoding_that_was_not_offered_is_unsupported(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    site.routes["/brotli"] = answer(200, b"\x0b\x02\x80hello\x03", [("Content-Encoding", "br")])

    assert (await _failure(make_fetcher(), site.url("/brotli"))).reason == "unsupported"


async def test_two_encodings_in_two_headers_are_unsupported(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    twice = gzip.compress(gzip.compress(b"hello"))
    site.routes["/twice"] = answer(
        200, twice, [("Content-Encoding", "gzip"), ("Content-Encoding", "gzip")]
    )

    assert (await _failure(make_fetcher(), site.url("/twice"))).reason == "unsupported"


async def test_an_unaccepted_type_is_refused_before_the_body_comes(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    head = ["Content-Type: application/zip", "Content-Length: 5"]
    site.routes["/file.zip"] = head_then_wait(head, then=wait_long)
    started = time.perf_counter()

    error = await _failure(make_fetcher(), site.url("/file.zip"), accept=["text/html"])

    assert error.reason == "unsupported"
    assert time.perf_counter() - started < 1.0


async def test_a_slow_server_meets_the_time_limit_of_the_whole_fetch(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    async def one_byte_at_a_time(writer: asyncio.StreamWriter) -> None:
        for _ in range(1000):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.05)

    head = ["Content-Type: text/plain", "Content-Length: 1000"]
    site.routes["/slow"] = head_then_wait(head, then=one_byte_at_a_time)
    started = time.perf_counter()

    # Each byte comes in time, so no single read is late. The limit of the whole fetch stops it.
    error = await _failure(make_fetcher(timeout=0.5), site.url("/slow"))

    assert (error.reason, error.retryable) == ("timeout", True)
    assert 0.5 <= time.perf_counter() - started < 1.5


async def test_a_server_that_never_answers_is_a_timeout(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    async def silent(request: Any, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(30)

    site.routes["/silent"] = silent

    error = await _failure(make_fetcher(timeout=0.5), site.url("/silent"))

    assert (error.reason, error.retryable) == ("timeout", True)


async def test_a_closed_port_is_a_network_error_that_can_be_tried_again(
    make_fetcher: MakeFetcher,
) -> None:
    port = a_closed_port()
    fetcher = make_fetcher(allowed_ports=frozenset({port}))

    error = await _failure(fetcher, f"http://site.test:{port}/")

    assert (error.reason, error.retryable) == ("network", True)


# --- What goes out, and what comes back ------------------------------------------------------


async def test_an_error_status_keeps_its_code_and_the_wait_that_the_server_asks_for(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    in_90_seconds = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    site.routes["/busy"] = answer(503, headers=[("Retry-After", "120")])
    site.routes["/limit"] = answer(429, headers=[("Retry-After", in_90_seconds)])
    site.routes["/gone"] = answer(410)
    site.routes["/later"] = answer(503, headers=[("Retry-After", "99999999")])
    fetcher = make_fetcher()

    busy = await _failure(fetcher, site.url("/busy"))
    limit = await _failure(fetcher, site.url("/limit"))
    gone = await _failure(fetcher, site.url("/gone"))
    later = await _failure(fetcher, site.url("/later"))

    assert (busy.reason, busy.status, busy.retryable, busy.retry_after) == (
        "http_error",
        503,
        True,
        120.0,
    )
    assert limit.retry_after is not None
    assert 85 <= limit.retry_after <= 90
    assert (gone.status, gone.retryable, gone.retry_after) == (410, False, None)
    assert later.retry_after == 24 * 60 * 60  # more than one day is read as one day


async def test_the_request_carries_no_credentials_and_names_the_fetcher(
    site: LocalSite, make_fetcher: MakeFetcher
) -> None:
    site.routes["/hello"] = answer(200, b"hello")
    url = f"http://user:secret@site.test:{site.port}/hello"

    result = await make_fetcher().get(url, max_bytes=1000, accept=["text/html", "text/*"])

    headers = site.requests[0].headers
    assert "authorization" not in headers
    assert headers["user-agent"] == "UnshelveTest/1"
    assert headers["host"] == f"site.test:{site.port}"
    assert headers["accept"] == "text/html, text/*"
    assert "secret" not in result.final_url


@pytest.mark.parametrize(
    ("url", "header"),
    [
        ("https://example.com/", "example.com"),
        ("http://example.com:80/", "example.com"),
        ("http://example.com:443/", "example.com:443"),
        ("http://[2606:4700:4700::1111]:8080/", "[2606:4700:4700::1111]:8080"),
        ("https://bücher.example/", "xn--bcher-kva.example"),
    ],
)
def test_the_host_header_names_the_host_as_the_link_does(url: str, header: str) -> None:
    assert host_header(httpx.URL(url)) == header


# --- The real rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "https://127.0.0.1/",
        "http://localhost/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://017700000001/",
        "http://127.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[::127.0.0.1]/",
        "http://[2002:7f00:1::]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://[fe80::1]/",
        "http://[fe80::1%25en0]/",
        "http://[fc00::1]/",
    ],
)
async def test_the_real_rules_refuse_each_form_of_a_local_address(url: str) -> None:
    network = NoNetwork()
    fetcher = LiveFetcher(inner_backend=network)
    try:
        error = await _failure(fetcher, url)
    finally:
        await fetcher.aclose()

    assert error.reason == "blocked_address"
    assert network.connections == []
