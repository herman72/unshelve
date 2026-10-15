"""The X extractor against the real X. It runs only when it is asked for:

    uv run pytest -m live -o addopts="" tests/extract/test_x_post_live.py

It reads public posts: no key, no cost. It checks that the real answers still have the form
that the fixtures in ``fixtures/x`` copy, for both ways in.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest

from unshelve.config import Settings
from unshelve.contracts.dto import FetchResult, SourceRef
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.x_post import XPostExtractor
from unshelve.net.fetcher import LiveFetcher
from unshelve.testing.fakes import FakeLLM, InMemoryBlobStore

pytestmark = pytest.mark.live

# "today we launched ChatGPT", by Sam Altman: a public post with a link.
PUBLIC_POST = "https://twitter.com/sama/status/1598038815599661056"


class WithoutTheEmbedEndpoint(LiveFetcher):
    """The real fetcher, for which the endpoint of the embed widgets is down."""

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        if url.startswith("https://cdn.syndication.twimg.com/"):
            raise FetchError("http_error", url, status=503)
        return await super().get(url, max_bytes=max_bytes, accept=accept)


@pytest.fixture
async def fetcher() -> AsyncIterator[LiveFetcher]:
    live = LiveFetcher.from_settings(Settings(env="test"))
    yield live
    await live.aclose()


def _context(fetcher: LiveFetcher) -> ExtractContext:
    return ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())


async def test_the_embed_endpoint_still_gives_the_post(fetcher: LiveFetcher) -> None:
    found = await XPostExtractor().extract(SourceRef(url=PUBLIC_POST), _context(fetcher))

    assert found.author == "Sam Altman (@sama)"
    assert "today we launched ChatGPT" in found.text
    assert found.extra_links == ["http://chat.openai.com"]  # the real address, not t.co
    assert found.lang == "en"
    assert found.published_at is not None
    assert found.published_at.date() == datetime(2022, 11, 30, tzinfo=UTC).date()


async def test_oembed_still_gives_the_post() -> None:
    fetcher = WithoutTheEmbedEndpoint.from_settings(Settings(env="test"))
    try:
        found = await XPostExtractor().extract(SourceRef(url=PUBLIC_POST), _context(fetcher))
    finally:
        await fetcher.aclose()

    assert found.author == "Sam Altman (@sama)"
    assert "today we launched ChatGPT" in found.text
    assert found.published_at == datetime(2022, 11, 30, tzinfo=UTC)


async def test_a_post_that_does_not_exist_is_an_extract_error(fetcher: LiveFetcher) -> None:
    with pytest.raises(ExtractError):
        await XPostExtractor().extract(
            SourceRef(url="https://x.com/jack/status/99999999999999999999"), _context(fetcher)
        )
