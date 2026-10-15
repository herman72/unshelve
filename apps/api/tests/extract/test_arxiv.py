"""The arXiv extractor, on made-up answers in the form of the real ones. A pretend fetcher.
Each test name is one rule."""

import pyexpat
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unshelve.contracts.dto import Extracted, FetchResult, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract import arxiv as arxiv_module
from unshelve.extract.article import GENERIC_SCORE, ArticleExtractor
from unshelve.extract.arxiv import MIN_EXPAT, ArxivExtractor
from unshelve.testing.conformance import ExtractorCase, ExtractorConformance
from unshelve.testing.fakes import FakeFetcher, FakeLLM, InMemoryBlobStore

FIXTURES = Path(__file__).parent / "fixtures"
PAPER = "2610.01234"
API = f"https://export.arxiv.org/api/query?id_list={PAPER}"
PAGE = f"https://arxiv.org/abs/{PAPER}"
OLD = "cs/0601001"
ATOM = "application/atom+xml; charset=utf-8"


class Fetcher(FakeFetcher):
    """A pretend fetcher that serves the fixtures, and writes down what it was asked."""

    def __init__(self, answer: str | None = "paper.xml", *, paper: str = PAPER) -> None:
        super().__init__()
        self.asked: list[str] = []
        api = f"https://export.arxiv.org/api/query?id_list={paper}"
        if answer is not None:
            self.add(api, (FIXTURES / "arxiv" / answer).read_bytes(), ATOM)
        # The page of the abstract, for the article extractor.
        page = f"https://arxiv.org/abs/{paper}"
        self.add(page, (FIXTURES / "article_en.html").read_bytes(), "text/html; charset=utf-8")

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        self.asked.append(url)
        return await super().get(url, max_bytes=max_bytes, accept=accept)


def _context(fetcher: FakeFetcher) -> ExtractContext:
    return ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())


async def _extract(fetcher: Fetcher, url: str = f"https://arxiv.org/pdf/{PAPER}v1") -> Extracted:
    return await ArxivExtractor().extract(SourceRef(url=url, text=url), _context(fetcher))


class TestArxivExtractor(ExtractorConformance):
    @pytest.fixture
    def case(self) -> ExtractorCase:
        return ExtractorCase(
            ArxivExtractor(),
            _context(Fetcher()),
            matching=SourceRef(url=PAGE),
            unrelated=SourceRef(url="https://blog.example.com/queues"),
        )


# --- Which links are papers --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        PAGE,
        f"https://arxiv.org/abs/{PAPER}v2",
        f"https://arxiv.org/pdf/{PAPER}",
        f"https://arxiv.org/pdf/{PAPER}v1.pdf",
        f"https://arxiv.org/html/{PAPER}v2",
        f"http://export.arxiv.org/abs/{PAPER}",
        f"https://arxiv.org/abs/{OLD}",
    ],
)
def test_each_address_of_a_paper_is_claimed_before_the_article_extractor(url: str) -> None:
    ref = SourceRef(url=url)

    assert ArxivExtractor().match(ref) > GENERIC_SCORE


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/",
        "https://arxiv.org/list/cs.DB/recent",
        "https://arxiv.org/a/example_a_1",
        f"https://example.com/abs/{PAPER}",
        None,
    ],
)
def test_other_pages_and_notes_are_left_to_other_extractors(url: str | None) -> None:
    assert ArxivExtractor().match(SourceRef(url=url, text="a note")) == 0


def test_the_article_extractor_keeps_its_own_score_for_a_paper() -> None:
    assert ArticleExtractor().match(SourceRef(url=PAGE)) == GENERIC_SCORE


# --- What a paper gives ------------------------------------------------------------------


async def test_a_paper_gives_its_title_authors_date_categories_and_abstract() -> None:
    fetcher = Fetcher()

    found = await _extract(fetcher)

    assert fetcher.asked == [API]
    assert found.kind is ItemKind.PAPER
    assert found.title == "A Job Queue on One Postgres Table: Leases, Retries and SKIP LOCKED"
    assert found.author == "Ada Example, Bo Example, Cy Example and 1 more"
    assert found.site == "arxiv.org"
    assert found.lang is None  # the model finds the language in the text
    assert found.final_url == PAGE
    assert found.published_at == datetime(2026, 10, 1, 9, 30, tzinfo=UTC)  # the first version
    assert found.text == (
        "A Job Queue on One Postgres Table: Leases, Retries and SKIP LOCKED\n"
        "A paper on arXiv by Ada Example, Bo Example, Cy Example, Di Example, from 1 October "
        "2026. Categories: cs.DB, cs.DC.\n"
        "Comments: 12 pages, 3 figures. Slides at https://example.com/queue-slides.pdf\n\n"
        "We describe a job queue that keeps each job in one row of a Postgres table. Workers "
        "take jobs with FOR UPDATE SKIP LOCKED, so that two workers never take the same job. "
        "A lease with a time limit frees the job of a worker that dies. Code: "
        "https://github.com/example/queue-paper."
    )


async def test_the_pdf_and_the_links_of_the_paper_become_extra_links() -> None:
    found = await _extract(Fetcher())

    assert found.extra_links == [
        f"https://arxiv.org/pdf/{PAPER}",
        "https://example.com/queue-slides.pdf",
        "https://github.com/example/queue-paper",
    ]


async def test_a_paper_takes_three_minutes_for_each_page() -> None:
    # About 660 words a page, at the 220 words a minute of the reading time of a text.
    found = await _extract(Fetcher())

    assert found.minutes == 36  # 12 pages


async def test_a_paper_that_names_no_pages_counts_the_time_to_read_its_abstract() -> None:
    found = await _extract(Fetcher("paper_old_id.xml", paper=OLD), f"https://arxiv.org/abs/{OLD}")

    assert found.minutes is None


async def test_an_old_identifier_gives_the_journal_and_the_doi() -> None:
    fetcher = Fetcher("paper_old_id.xml", paper=OLD)

    found = await _extract(fetcher, f"https://arxiv.org/abs/{OLD}v3")

    assert fetcher.asked == [f"https://export.arxiv.org/api/query?id_list={OLD}"]
    assert found.final_url == f"https://arxiv.org/abs/{OLD}"
    assert found.author == "Ed Example"
    assert "Journal: Journal of Made-up Results 12:34-56, 2006" in found.text
    assert found.extra_links == [
        f"https://arxiv.org/pdf/{OLD}",
        "https://doi.org/10.1234/made-up.2006.12",
    ]


async def test_a_large_team_is_named_up_to_30_authors() -> None:
    found = await _extract(Fetcher("paper_many_authors.xml"))

    assert found.author == "Author 01 Example, Author 02 Example, Author 03 Example and 42 more"
    byline = found.text.splitlines()[1]
    assert "Author 30 Example, and 15 more" in byline
    assert "Author 31 Example" not in byline


async def test_the_date_of_a_new_version_is_not_in_the_text() -> None:
    # A new version gives the paper a new "updated" date. The text keeps the first date, so a
    # new version alone makes no new card.
    found = await _extract(Fetcher())

    assert "2026-10-08" not in found.text
    assert "8 October" not in found.text


# --- When the API does not answer --------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        FetchError("http_error", API, status=429, retry_after=60),
        FetchError("http_error", API, status=503),
        FetchError("timeout", API),
        FetchError("network", API),
    ],
    ids=["429", "503", "timeout", "network"],
)
async def test_an_api_that_does_not_answer_gives_way_to_the_page(failure: FetchError) -> None:
    fetcher = Fetcher(None)
    fetcher.pages[API] = failure

    found = await _extract(fetcher)

    assert fetcher.asked == [API, PAGE]
    assert found.kind is ItemKind.PAPER  # the article extractor read the page of the abstract
    assert found.title == "Queues in Postgres"


@pytest.mark.parametrize(
    "answer",
    [b"<feed><not-closed>", b"plain text, no XML"],
    ids=["broken XML", "no XML"],
)
async def test_an_answer_that_is_not_atom_gives_way_to_the_page(answer: bytes) -> None:
    fetcher = Fetcher(None)
    fetcher.add(API, answer, ATOM)

    found = await _extract(fetcher)

    assert fetcher.asked == [API, PAGE]
    assert found.title == "Queues in Postgres"


async def test_a_paper_that_the_api_does_not_know_ends_with_the_404_of_its_page() -> None:
    fetcher = Fetcher("empty.xml")
    fetcher.pages[PAGE] = FetchError("http_error", PAGE, status=404)

    with pytest.raises(FetchError) as caught:
        await _extract(fetcher)

    assert fetcher.asked == [API, PAGE]
    assert (caught.value.status, caught.value.retryable) == (404, False)


def test_the_xml_parser_is_new_enough_to_refuse_entity_expansion() -> None:
    # The Python documentation: "Expat versions lower than 2.7.2 may be vulnerable to the
    # 'billion laughs', 'quadratic blowup' and 'large tokens' vulnerabilities".
    assert MIN_EXPAT == (2, 7, 2)
    assert pyexpat.version_info >= MIN_EXPAT


async def test_with_an_older_parser_the_xml_is_not_read_and_the_page_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(arxiv_module, "_SAFE_PARSER", False)
    fetcher = Fetcher()

    found = await _extract(fetcher)

    assert fetcher.asked == [PAGE]
    assert found.kind is ItemKind.PAPER


async def test_an_error_entry_of_the_api_gives_way_to_the_page() -> None:
    fetcher = Fetcher("error.xml")

    found = await _extract(fetcher)

    assert fetcher.asked == [API, PAGE]
    assert found.title == "Queues in Postgres"  # not a paper with the title "Error"


async def test_a_billion_laughs_is_refused_and_the_page_is_read() -> None:
    laughs = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        + "".join(
            f'<!ENTITY lol{n} "{("&lol" + (str(n - 1) if n > 1 else "") + ";") * 10}">'
            for n in range(1, 10)
        )
        + ']><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>&lol9;</title></entry></feed>'
    ).encode()
    fetcher = Fetcher(None)
    fetcher.add(API, laughs, ATOM)

    found = await _extract(fetcher)

    assert fetcher.asked == [API, PAGE]  # the parser refused it, and the page was read
    assert found.title == "Queues in Postgres"
