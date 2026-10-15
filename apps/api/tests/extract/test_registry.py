"""The extractor that reads a source, and the text that you saved. No network."""

from typing import ClassVar

import pytest

from unshelve.contracts.dto import Extracted, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.protocols import ExtractContext, Extractor
from unshelve.extract.article import ArticleExtractor
from unshelve.extract.registry import EXTRACTORS, FALLBACK, choose
from unshelve.extract.saved_text import SavedTextExtractor
from unshelve.testing.conformance import ExtractorCase, ExtractorConformance
from unshelve.testing.fakes import FakeFetcher, FakeLLM, InMemoryBlobStore


def _context() -> ExtractContext:
    return ExtractContext(fetcher=FakeFetcher(), blobs=InMemoryBlobStore(), llm=FakeLLM())


class OneSite:
    """An extractor for one site, as the next packages bring them."""

    name: ClassVar[str] = "one_site"
    version: ClassVar[int] = 1

    def match(self, ref: SourceRef) -> int:
        return 100 if ref.url and "video.example" in ref.url else 0

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        return Extracted(kind=ItemKind.VIDEO, text="a transcript")


class TestSavedTextExtractor(ExtractorConformance):
    @pytest.fixture
    def case(self) -> ExtractorCase:
        return ExtractorCase(
            SavedTextExtractor(), _context(), SourceRef(text="a saved note"), SourceRef()
        )


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (SourceRef(url="https://blog.example.com/queues", text="read later"), "article"),
        (SourceRef(url="https://youtu.be/dEmOvIdEo01?si=made-up"), "youtube"),
        (SourceRef(url="https://www.youtube.com/@made-up-channel"), "article"),
        (SourceRef(url="https://twitter.com/MadeUp_Person/status/1234567890123456789"), "x"),
        (SourceRef(url="https://x.com/madeup_person"), "article"),
        (SourceRef(url="https://github.com/made-up-org/queue-kit"), "github"),
        (SourceRef(url="https://github.com/made-up-org/queue-kit/issues/7"), "article"),
        (SourceRef(text="a note without a link"), "saved_text"),
        (SourceRef(url="https://example.com/paper.pdf"), "pdf"),
        (SourceRef(url="https://example.com/paper.pdf?download=1"), "pdf"),
        (SourceRef(url="https://github.com/made-up-org/queue-kit/blob/main/leases.pdf"), "pdf"),
        (SourceRef(url="https://arxiv.org/pdf/2610.01234v1"), "arxiv"),  # a PDF of arXiv
        (SourceRef(url="https://arxiv.org/pdf/2610.01234v1.pdf"), "arxiv"),  # arXiv wins: 50
        (SourceRef(url="https://arxiv.org/list/cs.DB/recent"), "article"),
        (SourceRef(url="https://t.me/s/made_up_channel/42"), "telegram"),
        (SourceRef(url="https://t.me/made_up_channel"), "article"),
        (SourceRef(), "saved_text"),  # a photo without a caption: nobody claims it
    ],
)
def test_the_extractor_with_the_highest_score_reads_the_source(
    ref: SourceRef, expected: str
) -> None:
    assert choose(ref).name == expected


def test_an_extractor_for_one_site_wins_over_the_article_extractor() -> None:
    extractors: list[Extractor] = [ArticleExtractor(), OneSite(), FALLBACK]

    assert choose(SourceRef(url="https://video.example/watch/1"), extractors).name == "one_site"
    assert choose(SourceRef(url="https://blog.example.com/a"), extractors).name == "article"


def test_on_a_tie_the_first_extractor_in_the_list_wins() -> None:
    first, second = OneSite(), OneSite()

    assert choose(SourceRef(url="https://video.example/1"), [first, second, FALLBACK]) is first


def test_the_text_that_you_saved_is_the_last_extractor() -> None:
    assert EXTRACTORS[-1] is FALLBACK


@pytest.mark.parametrize(
    ("ref", "kind"),
    [
        (SourceRef(text="Buy milk"), ItemKind.NOTE),
        (SourceRef(url="https://example.com/a", text="https://example.com/a"), ItemKind.UNKNOWN),
        # A link that came without a text: the text stays empty, and the model is not asked.
        (SourceRef(url="https://example.com/a"), ItemKind.UNKNOWN),
    ],
)
async def test_the_saved_text_is_a_note_or_a_link_that_was_not_read(
    ref: SourceRef, kind: ItemKind
) -> None:
    found = await SavedTextExtractor().extract(ref, _context())

    assert (found.kind, found.text, found.final_url) == (kind, ref.text, None)
