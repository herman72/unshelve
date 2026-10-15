"""The arXiv extractor against the real arXiv. It runs only when it is asked for:

    uv run pytest -m live -o addopts="" tests/extract/test_arxiv_live.py

It reads public papers: no key, no cost. The fetcher waits three seconds between two requests
to the API, as its terms ask, so this file takes some seconds. It checks that the real answers
still have the form that the fixtures in ``fixtures/arxiv`` copy, and that the page of an
abstract still has enough text for the article extractor.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest

from unshelve.config import Settings
from unshelve.contracts.dto import FetchResult, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.arxiv import ArxivExtractor
from unshelve.net.fetcher import LiveFetcher
from unshelve.testing.fakes import FakeLLM, InMemoryBlobStore

pytestmark = pytest.mark.live

ATTENTION = "https://arxiv.org/abs/1706.03762"  # "Attention Is All You Need"


class WithoutTheApi(LiveFetcher):
    """The real fetcher, for which the API of arXiv is down."""

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        if url.startswith("https://export.arxiv.org/"):
            raise FetchError("http_error", url, status=503)
        return await super().get(url, max_bytes=max_bytes, accept=accept)


@pytest.fixture
async def fetcher() -> AsyncIterator[LiveFetcher]:
    live = LiveFetcher.from_settings(Settings(env="test"))
    yield live
    await live.aclose()


def _context(fetcher: LiveFetcher) -> ExtractContext:
    return ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())


async def test_the_api_still_gives_the_paper(fetcher: LiveFetcher) -> None:
    found = await ArxivExtractor().extract(SourceRef(url=ATTENTION), _context(fetcher))

    assert found.kind is ItemKind.PAPER
    assert found.title == "Attention Is All You Need"
    assert found.author == "Ashish Vaswani, Noam Shazeer, Niki Parmar and 5 more"
    assert found.published_at == datetime(2017, 6, 12, 17, 57, 34, tzinfo=UTC)
    assert found.minutes == 45  # "15 pages, 5 figures"
    assert "Categories: cs.CL, cs.LG." in found.text
    assert "The dominant sequence transduction models" in found.text


async def test_the_page_of_the_abstract_still_has_the_abstract() -> None:
    fetcher = WithoutTheApi.from_settings(Settings(env="test"))
    try:
        found = await ArxivExtractor().extract(SourceRef(url=ATTENTION), _context(fetcher))
    finally:
        await fetcher.aclose()

    assert found.kind is ItemKind.PAPER
    assert "The dominant sequence transduction models" in found.text


async def test_a_paper_that_does_not_exist_ends_with_a_404(fetcher: LiveFetcher) -> None:
    with pytest.raises(FetchError) as caught:
        await ArxivExtractor().extract(
            SourceRef(url="https://arxiv.org/abs/2607.99999"), _context(fetcher)
        )

    assert caught.value.status == 404
