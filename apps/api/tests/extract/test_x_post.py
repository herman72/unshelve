"""The X extractor, on made-up answers in the form of the real ones. A pretend fetcher.
Each test name is one rule."""

# This file reads Persian text on purpose.
# ruff: noqa: RUF001

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest

from unshelve.contracts.dto import Extracted, FetchResult, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract import x_post
from unshelve.extract.article import GENERIC_SCORE, ArticleExtractor
from unshelve.extract.x_post import XPostExtractor, syndication_url
from unshelve.testing.conformance import ExtractorCase, ExtractorConformance
from unshelve.testing.fakes import FakeFetcher, FakeLLM, InMemoryBlobStore

FIXTURES = Path(__file__).parent / "fixtures" / "x"
POST = "1234567890123456789"
SAVED = f"https://twitter.com/MadeUp_Person/status/{POST}?s=20"
CANONICAL = f"https://x.com/madeup_person/status/{POST}"
EMBED = syndication_url(POST)
OEMBED = f"https://publish.x.com/oembed?url={quote(CANONICAL, safe='')}&omit_script=true&dnt=true"


class Fetcher(FakeFetcher):
    """A pretend fetcher that serves the fixtures, and writes down what it was asked."""

    def __init__(self, embed: str | None = "post.json", *, oembed: bool = True) -> None:
        super().__init__()
        self.asked: list[str] = []
        if embed is not None:
            self.add(EMBED, (FIXTURES / embed).read_bytes(), "application/json; charset=utf-8")
        if oembed:
            self.add(
                OEMBED, (FIXTURES / "oembed.json").read_bytes(), "application/json; charset=utf-8"
            )

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        self.asked.append(url)
        return await super().get(url, max_bytes=max_bytes, accept=accept)


def _context(fetcher: FakeFetcher) -> ExtractContext:
    return ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())


async def _extract(fetcher: Fetcher, url: str = SAVED) -> Extracted:
    return await XPostExtractor().extract(SourceRef(url=url, text=url), _context(fetcher))


class TestXPostExtractor(ExtractorConformance):
    @pytest.fixture
    def case(self) -> ExtractorCase:
        return ExtractorCase(
            XPostExtractor(),
            _context(Fetcher()),
            matching=SourceRef(url=SAVED),
            unrelated=SourceRef(url="https://blog.example.com/queues"),
        )


# --- Which links are posts ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        SAVED,
        f"https://x.com/madeup_person/status/{POST}",
        f"https://mobile.twitter.com/madeup_person/status/{POST}/photo/1",
        f"https://fxtwitter.com/madeup_person/status/{POST}",
        f"https://vxtwitter.com/madeup_person/status/{POST}",
        f"https://x.com/i/web/status/{POST}",
    ],
)
def test_each_address_of_a_post_is_claimed_before_the_article_extractor(url: str) -> None:
    ref = SourceRef(url=url)

    assert XPostExtractor().match(ref) > GENERIC_SCORE
    assert ArticleExtractor().match(ref) == GENERIC_SCORE


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/madeup_person",
        "https://x.com/home",
        "https://x.com/search?q=postgres",
        f"https://example.com/madeup_person/status/{POST}",
        None,
    ],
)
def test_other_pages_and_notes_are_left_to_other_extractors(url: str | None) -> None:
    assert XPostExtractor().match(SourceRef(url=url, text="a note")) == 0


# --- What a post gives -------------------------------------------------------------------


async def test_a_post_gives_its_author_date_language_and_text() -> None:
    found = await _extract(Fetcher())

    assert found.kind is ItemKind.POST
    assert found.title is None  # the model writes a title; "A post on X" says nothing
    assert found.author == "Made-up Person (@madeup_person)"
    assert found.site == "x.com"
    assert found.lang == "en"
    assert found.published_at == datetime(2026, 9, 30, 8, 15, tzinfo=UTC)
    assert found.final_url == CANONICAL
    assert found.text == (
        "A post by Made-up Person (@madeup_person) on X, from 30 September 2026.\n\n"
        "Postgres can be a job queue & a log: SKIP LOCKED lets workers share one table.\n\n"
        "https://example.com/queues\n\n"
        "It has 1 photo."
    )


async def test_a_short_link_becomes_the_address_that_it_stands_for() -> None:
    found = await _extract(Fetcher())

    assert "t.co" not in found.text  # the link of the photo is gone, the other one is real
    assert found.extra_links == ["https://example.com/queues"]


async def test_the_number_of_likes_is_not_in_the_text() -> None:
    found = await _extract(Fetcher())

    assert "12345" not in found.text


async def test_only_the_embed_endpoint_is_asked_when_it_answers() -> None:
    fetcher = Fetcher()

    await _extract(fetcher)

    assert fetcher.asked == [EMBED]
    assert EMBED.startswith(
        f"https://cdn.syndication.twimg.com/tweet-result?id={POST}&lang=en&token="
    )


async def test_a_quoted_post_comes_with_its_author_and_text() -> None:
    found = await _extract(Fetcher("quote.json"))

    assert found.text.endswith(
        "It quotes a post by Other Person (@other_person):\n"
        "A job queue in Postgres needs one table and SKIP LOCKED."
    )


async def test_a_long_post_says_that_its_text_is_cut() -> None:
    found = await _extract(Fetcher("long.json"))

    assert found.text.endswith("(X gives only the start of this long post.)")


async def test_a_persian_post_keeps_its_letters_and_its_language() -> None:
    found = await _extract(Fetcher("persian.json"))

    assert found.lang == "fa"
    assert found.author == "کاربر نمونه (@sample_user)"
    assert "می\u200cسازیم" in found.text


@pytest.mark.parametrize("code", ["und", "qme", "zxx", "in-ID", ""])
async def test_a_code_that_is_no_language_gives_no_language(code: str) -> None:
    # X writes "und" when it does not know the language, "qme" for a post of media only, and
    # "zxx" for a post without words. The model finds the language in the text instead.
    answer = json.loads((FIXTURES / "post.json").read_text(encoding="utf-8"))
    answer["lang"] = code
    fetcher = Fetcher(None)
    fetcher.add(EMBED, json.dumps(answer).encode(), "application/json")

    found = await _extract(fetcher)

    assert found.lang is None


# --- The second way: oEmbed --------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        FetchError("http_error", EMBED, status=400),
        FetchError("http_error", EMBED, status=403),
        FetchError("http_error", EMBED, status=429, retry_after=60),
        FetchError("timeout", EMBED),
    ],
    ids=["400", "403", "429", "timeout"],
)
async def test_a_failure_of_the_embed_endpoint_falls_back_to_oembed(answer: FetchError) -> None:
    fetcher = Fetcher(None)
    fetcher.pages[EMBED] = answer

    found = await _extract(fetcher)

    assert fetcher.asked == [EMBED, OEMBED]
    assert found.author == "Made-up Person (@madeup_person)"
    assert found.lang == "en"
    assert found.published_at == datetime(2026, 9, 30, tzinfo=UTC)
    assert found.text == (
        "A post by Made-up Person (@madeup_person) on X, from 30 September 2026.\n\n"
        "Postgres can be a job queue & a log: SKIP LOCKED lets workers share one table.\n\n"
        "https://t.co/AbCdEf1234 pic.x.com/MeDiA98765"
    )


@pytest.mark.parametrize("answer", ["empty.json"], ids=["empty answer"])
async def test_an_answer_without_a_post_falls_back_to_oembed(answer: str) -> None:
    fetcher = Fetcher(answer)

    found = await _extract(fetcher)

    assert fetcher.asked == [EMBED, OEMBED]
    assert found.author == "Made-up Person (@madeup_person)"


async def test_a_broken_answer_falls_back_to_oembed() -> None:
    fetcher = Fetcher(None)
    fetcher.add(EMBED, b"{not json", "application/json")

    found = await _extract(fetcher)

    assert fetcher.asked == [EMBED, OEMBED]
    assert found.author == "Made-up Person (@madeup_person)"


# --- When the post is not there ----------------------------------------------------------


async def test_a_post_that_is_gone_is_an_extract_error() -> None:
    fetcher = Fetcher("tombstone.json")

    with pytest.raises(ExtractError, match="no longer exists"):
        await _extract(fetcher)

    assert fetcher.asked == [EMBED]  # oEmbed would not know it either


@pytest.mark.parametrize("status", [403, 404])
async def test_a_post_that_oembed_does_not_know_either_is_an_extract_error(status: int) -> None:
    fetcher = Fetcher(None, oembed=False)
    fetcher.pages[EMBED] = FetchError("http_error", EMBED, status=400)
    fetcher.pages[OEMBED] = FetchError("http_error", OEMBED, status=status)

    with pytest.raises(ExtractError, match="no public facts"):
        await _extract(fetcher)


async def test_a_failure_of_oembed_that_can_pass_goes_up_unchanged() -> None:
    fetcher = Fetcher(None, oembed=False)
    fetcher.pages[EMBED] = FetchError("http_error", EMBED, status=503)
    busy = FetchError("http_error", OEMBED, status=429, retry_after=60)
    fetcher.pages[OEMBED] = busy

    with pytest.raises(FetchError) as caught:
        await _extract(fetcher)

    assert caught.value is busy  # the fetch step tries again later


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("September 30, 2026", datetime(2026, 9, 30, tzinfo=UTC)),
        ("Sept 30, 2026", None),
        ("February 30, 2026", None),
        ("30 September 2026", None),
    ],
)
def test_the_day_of_oembed_is_read_with_the_names_of_the_months_in_the_code(
    day: str, expected: datetime | None
) -> None:
    assert x_post._oembed_day(day) == expected  # pyright: ignore[reportPrivateUsage]
