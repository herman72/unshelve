"""The YouTube extractor, on made-up pages in the form of the real ones. A pretend fetcher.
Each test name is one rule."""

# This file reads Persian text on purpose.
# ruff: noqa: RUF001

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unshelve.contracts.dto import Extracted, FetchResult, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.article import GENERIC_SCORE, ArticleExtractor
from unshelve.extract.youtube import MAX_PAGE_BYTES, YouTubeExtractor
from unshelve.testing.conformance import ExtractorCase, ExtractorConformance
from unshelve.testing.fakes import FakeFetcher, FakeLLM, InMemoryBlobStore

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"
VIDEO = "dEmOvIdEo01"
WATCH = f"https://www.youtube.com/watch?v={VIDEO}&hl=en"
OEMBED = (
    "https://www.youtube.com/oembed?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D"
    f"{VIDEO}&format=json"
)


class Fetcher(FakeFetcher):
    """A pretend fetcher that serves the fixtures, and writes down how it was asked."""

    def __init__(self, page: str | None = "watch_en.html", *, oembed: bool = True) -> None:
        super().__init__()
        self.asked: list[tuple[str, int, list[str]]] = []
        if page is not None:
            self.add(WATCH, (FIXTURES / page).read_bytes(), "text/html; charset=utf-8")
        if oembed:
            self.add(OEMBED, (FIXTURES / "oembed.json").read_bytes(), "application/json")

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        self.asked.append((url, max_bytes, list(accept)))
        return await super().get(url, max_bytes=max_bytes, accept=accept)


def _context(fetcher: FakeFetcher) -> ExtractContext:
    return ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())


async def _extract(fetcher: Fetcher, url: str = f"https://youtu.be/{VIDEO}") -> Extracted:
    return await YouTubeExtractor().extract(SourceRef(url=url, text=url), _context(fetcher))


class TestYouTubeExtractor(ExtractorConformance):
    @pytest.fixture
    def case(self) -> ExtractorCase:
        return ExtractorCase(
            YouTubeExtractor(),
            _context(Fetcher()),
            matching=SourceRef(url=f"https://youtu.be/{VIDEO}?si=made-up"),
            unrelated=SourceRef(url="https://blog.example.com/queues"),
        )


# --- Which links are videos --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://youtu.be/{VIDEO}",
        f"https://youtu.be/{VIDEO}?si=made-up&t=42",
        f"https://www.youtube.com/watch?v={VIDEO}",
        f"https://www.youtube.com/watch?v={VIDEO}&list=PLmadeup&index=3",
        f"http://m.youtube.com/watch?v={VIDEO}&feature=share",
        f"https://youtube.com/shorts/{VIDEO}",
        f"https://www.youtube.com/live/{VIDEO}?si=made-up",
        f"https://www.youtube.com/embed/{VIDEO}",
        f"https://www.youtube-nocookie.com/embed/{VIDEO}",
    ],
)
def test_each_address_of_a_video_is_claimed_before_the_article_extractor(url: str) -> None:
    ref = SourceRef(url=url)

    assert YouTubeExtractor().match(ref) > GENERIC_SCORE
    assert ArticleExtractor().match(ref) == GENERIC_SCORE


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@made-up-channel",
        "https://www.youtube.com/playlist?list=PLmadeup",
        "https://www.youtube.com/feed/subscriptions",
        "https://www.youtube.com/watch?v=short",
        "https://example.com/watch?v=dEmOvIdEo01",
        None,
    ],
)
def test_other_pages_and_notes_are_left_to_other_extractors(url: str | None) -> None:
    assert YouTubeExtractor().match(SourceRef(url=url, text="a note")) == 0


# --- What a video gives ------------------------------------------------------------------


async def test_a_video_gives_its_title_channel_date_length_and_description() -> None:
    found = await _extract(Fetcher())

    assert found.kind is ItemKind.VIDEO
    assert found.title == "Job queues in Postgres, from scratch"
    assert found.author == "Made-up Channel"
    assert found.site == "youtube.com"
    assert found.final_url == f"https://www.youtube.com/watch?v={VIDEO}"
    assert found.published_at == datetime(2026, 3, 2, 16, 0, tzinfo=UTC)
    assert found.minutes == 31  # 1,830 seconds, rounded up
    assert found.lang is None  # the model finds the language in the text
    assert found.text.splitlines()[:2] == [
        "Job queues in Postgres, from scratch",
        "A video by Made-up Channel on YouTube, 31 minutes, from 2 March 2026.",
    ]
    assert "Keywords: postgres, job queue, skip locked" in found.text
    assert found.text.endswith("12:30 Leases and retries")
    assert "We build a job queue on one Postgres table" in found.text


async def test_the_links_of_the_description_become_extra_links() -> None:
    found = await _extract(Fetcher())

    assert found.extra_links == [
        "https://github.com/example/queue-demo",
        "https://example.com/queue-slides.pdf",
        "https://example.com/notes",  # without the full stop after it, and each link one time
    ]


async def test_the_number_of_views_is_not_in_the_text() -> None:
    # The number changes each day. A new number would make a new text, and the card and the
    # vectors of the item would be made again for nothing.
    found = await _extract(Fetcher())

    assert "12345" not in found.text


async def test_the_page_is_asked_for_in_english_once() -> None:
    fetcher = Fetcher()

    await _extract(fetcher, f"https://m.youtube.com/watch?v={VIDEO}&t=42")

    # In English: else YouTube writes its messages in the language of the country of the IP.
    assert fetcher.asked == [(WATCH, MAX_PAGE_BYTES, ["text/html"])]


async def test_a_persian_video_keeps_its_letters_and_its_zero_width_non_joiner() -> None:
    found = await _extract(Fetcher("watch_fa.html"))

    assert found.title == "صف کارها در پستگرس"
    assert found.author == "کانال نمونه"
    assert "می\u200cسازیم" in found.text
    assert found.minutes == 2  # 61 seconds
    assert found.published_at == datetime(2026, 5, 9, 20, 30, tzinfo=UTC)


async def test_a_live_stream_without_a_length_has_no_minutes() -> None:
    found = await _extract(Fetcher("watch_live.html"))

    assert found.minutes is None
    assert found.text.splitlines()[1] == (
        "A live stream by Made-up Channel on YouTube, from 1 April 2026."
    )


async def test_a_video_with_an_age_limit_still_gives_its_facts() -> None:
    found = await _extract(Fetcher("age_limit.html"))

    assert found.title == "A talk with an age limit"
    assert found.minutes == 10
    assert "also when the video asks for a sign-in" in found.text


def _page(details: dict[str, object]) -> bytes:
    """A small watch page with these facts of the video, in the form of the real page."""
    player = {"playabilityStatus": {"status": "OK"}, "videoDetails": details}
    return (
        f'<html><script nonce="made-up">var ytInitialPlayerResponse = {json.dumps(player)};'
        "</script></html>"
    ).encode()


async def test_a_video_of_less_than_a_minute_takes_1_minute() -> None:
    fetcher = Fetcher(None)
    fetcher.add(
        WATCH, _page({"title": "A short", "author": "Made-up Channel", "lengthSeconds": "45"})
    )

    found = await _extract(fetcher)

    assert found.minutes == 1
    assert found.text.splitlines()[1] == "A video by Made-up Channel on YouTube, 1 minute."


# --- When the page has no facts ----------------------------------------------------------


async def test_facts_without_a_title_fall_back_to_oembed() -> None:
    fetcher = Fetcher(None)
    fetcher.add(WATCH, _page({"title": " ", "author": "Made-up Channel", "lengthSeconds": "45"}))

    found = await _extract(fetcher)

    assert found.title == "Job queues in Postgres, from scratch"
    assert [url for url, *_ in fetcher.asked] == [WATCH, OEMBED]


@pytest.mark.parametrize("page", ["robot_check.html", "consent.html"])
async def test_a_page_without_the_facts_of_the_video_falls_back_to_oembed(page: str) -> None:
    fetcher = Fetcher(page)

    found = await _extract(fetcher)

    assert [url for url, *_ in fetcher.asked] == [WATCH, OEMBED]
    assert found.kind is ItemKind.VIDEO
    assert (found.title, found.author, found.minutes) == (
        "Job queues in Postgres, from scratch",
        "Made-up Channel",
        None,
    )
    assert found.text == (
        "Job queues in Postgres, from scratch\nA video by Made-up Channel on YouTube."
    )


async def test_a_page_with_a_broken_player_object_falls_back_to_oembed() -> None:
    fetcher = Fetcher(None)
    broken = b'<html><script>var ytInitialPlayerResponse = {"videoDetails": {;</script></html>'
    fetcher.add(WATCH, broken, "text/html")

    found = await _extract(fetcher)

    assert found.title == "Job queues in Postgres, from scratch"


async def test_a_video_that_does_not_exist_is_an_extract_error() -> None:
    fetcher = Fetcher("unavailable.html")

    with pytest.raises(ExtractError, match="This video isn't available anymore"):
        await _extract(fetcher)

    assert [url for url, *_ in fetcher.asked] == [WATCH]  # oEmbed would not know it either


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_a_video_that_oembed_does_not_know_either_is_an_extract_error(status: int) -> None:
    fetcher = Fetcher("robot_check.html", oembed=False)
    fetcher.pages[OEMBED] = FetchError("http_error", OEMBED, status=status)

    with pytest.raises(ExtractError, match="no public facts"):
        await _extract(fetcher)


# --- Failures that can pass --------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        FetchError("http_error", WATCH, status=429, retry_after=120),
        FetchError("http_error", WATCH, status=503),
        FetchError("timeout", WATCH),
        FetchError("network", WATCH),
    ],
    ids=["429", "503", "timeout", "network"],
)
async def test_a_failure_of_the_page_that_can_pass_goes_up_unchanged(error: FetchError) -> None:
    fetcher = Fetcher(None)
    fetcher.pages[WATCH] = error

    with pytest.raises(FetchError) as caught:
        await _extract(fetcher)

    # The fetch step tries again later, with the wait of the server.
    assert caught.value is error


async def test_a_failure_of_oembed_that_can_pass_goes_up_unchanged() -> None:
    fetcher = Fetcher("robot_check.html", oembed=False)
    busy = FetchError("http_error", OEMBED, status=503)
    fetcher.pages[OEMBED] = busy

    with pytest.raises(FetchError) as caught:
        await _extract(fetcher)

    assert caught.value is busy
