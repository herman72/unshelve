"""The Telegram extractor against the real Telegram. It runs only when it is asked for:

    uv run pytest -m live -o addopts="" tests/extract/test_telegram_live.py

It reads a public post of the official channel of Telegram: no key, no cost. It checks that the
real embed pages still have the form that the fixtures in ``fixtures/telegram`` copy.
"""

from collections.abc import AsyncIterator
from datetime import date

import pytest

from unshelve.config import Settings
from unshelve.contracts.dto import SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.telegram import TelegramExtractor
from unshelve.net.fetcher import LiveFetcher
from unshelve.testing.fakes import FakeLLM, InMemoryBlobStore

pytestmark = pytest.mark.live


@pytest.fixture
async def context() -> AsyncIterator[ExtractContext]:
    fetcher = LiveFetcher.from_settings(Settings(env="test"))
    yield ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())
    await fetcher.aclose()


async def test_a_real_post_gives_its_channel_date_text_and_preview(
    context: ExtractContext,
) -> None:
    found = await TelegramExtractor().extract(SourceRef(url="https://t.me/s/telegram/460"), context)

    assert found.kind is ItemKind.POST
    assert found.author == "Telegram News (@telegram)"
    assert found.published_at is not None
    assert found.published_at.date() == date(2026, 8, 26)
    assert "13 years of Telegram features" in found.text
    assert "It links to" in found.text
    assert "https://telegram.org/blog/welcome-messages-buttons-TG-13" in found.extra_links


async def test_a_real_post_that_does_not_exist_is_an_extract_error(
    context: ExtractContext,
) -> None:
    with pytest.raises(ExtractError, match="Post not found"):
        await TelegramExtractor().extract(SourceRef(url="https://t.me/durov/999999999"), context)
