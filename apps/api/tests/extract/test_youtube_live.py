"""The YouTube extractor against the real YouTube. It runs only when it is asked for:

    uv run pytest -m live -o addopts="" tests/extract/test_youtube_live.py

It reads public pages: no key, no cost. It checks that the real pages still have the form that
the fixtures in ``fixtures/youtube`` copy.
"""

from collections.abc import AsyncIterator

import pytest

from unshelve.config import Settings
from unshelve.contracts.dto import SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.youtube import YouTubeExtractor
from unshelve.net.fetcher import LiveFetcher
from unshelve.testing.fakes import FakeLLM, InMemoryBlobStore

pytestmark = pytest.mark.live

# "Let's build GPT: from scratch, in code, spelled out", a public talk by Andrej Karpathy.
PUBLIC_VIDEO = "https://youtu.be/kCc8FmEb1nY"


@pytest.fixture
async def context() -> AsyncIterator[ExtractContext]:
    fetcher = LiveFetcher.from_settings(Settings(env="test"))
    yield ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())
    await fetcher.aclose()


async def test_a_real_watch_page_still_has_the_facts_of_the_player(
    context: ExtractContext,
) -> None:
    found = await YouTubeExtractor().extract(SourceRef(url=PUBLIC_VIDEO), context)

    assert found.kind is ItemKind.VIDEO
    assert found.title is not None
    assert "GPT" in found.title
    assert found.author == "Andrej Karpathy"
    assert found.minutes is not None
    assert found.minutes > 60
    assert found.published_at is not None
    assert found.published_at.year == 2023
    assert len(found.text) > 1_000  # the description is there
    assert found.extra_links


async def test_a_real_video_that_does_not_exist_is_an_extract_error(
    context: ExtractContext,
) -> None:
    with pytest.raises(ExtractError, match="not available"):
        await YouTubeExtractor().extract(SourceRef(url="https://youtu.be/aaaaaaaaaaa"), context)
