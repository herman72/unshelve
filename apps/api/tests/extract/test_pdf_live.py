"""The PDF extractor against real files on the web. It runs only when it is asked for:

    uv run pytest -m live -o addopts="" tests/extract/test_pdf_live.py

It reads two public papers from two producers: the Bitcoin paper (OpenOffice, 2009) and the
BERT paper (LaTeX, in two columns, from the ACL Anthology). No key, no cost. The fixtures in
``fixtures/pdf`` come from Chrome; these files show that the reader also works for others.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from unshelve.config import Settings
from unshelve.contracts.dto import SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.pdf import PdfExtractor
from unshelve.net.fetcher import LiveFetcher
from unshelve.testing.fakes import FakeLLM, InMemoryBlobStore

pytestmark = pytest.mark.live


@pytest.fixture
async def context() -> AsyncIterator[ExtractContext]:
    fetcher = LiveFetcher.from_settings(Settings(env="test"))
    yield ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())
    await fetcher.aclose()


async def test_a_real_paper_without_a_title_in_its_facts(context: ExtractContext) -> None:
    found = await PdfExtractor().extract(SourceRef(url="https://bitcoin.org/bitcoin.pdf"), context)

    assert found.kind is ItemKind.PDF
    assert found.title is None  # the file states no title; the model writes one
    assert found.published_at == datetime(2009, 3, 24, tzinfo=UTC)
    assert found.text.startswith("A PDF document, 9 pages, made on 24 March 2009.")
    assert "A purely peer-to-peer version of electronic cash" in found.text


async def test_a_real_paper_in_two_columns_keeps_the_order_of_its_sections(
    context: ExtractContext,
) -> None:
    found = await PdfExtractor().extract(
        SourceRef(url="https://aclanthology.org/N19-1423.pdf"), context
    )

    assert found.title == (
        "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
    )
    assert found.author is not None
    assert found.author.startswith("Jacob Devlin")
    assert found.text.splitlines()[1] == "A PDF document by " + found.author + (
        ", 16 pages, made on 29 April 2019."
    )
    sections = ["Abstract", "1 Introduction", "2 Related Work", "3 BERT", "References"]
    places = [found.text.index(section) for section in sections]
    assert places == sorted(places)


async def test_a_real_file_that_does_not_exist_is_a_404(context: ExtractContext) -> None:
    with pytest.raises(FetchError) as caught:
        await PdfExtractor().extract(
            SourceRef(url="https://bitcoin.org/no-such-made-up-paper.pdf"), context
        )

    assert (caught.value.reason, caught.value.status) == ("http_error", 404)
