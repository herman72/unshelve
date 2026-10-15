"""The Telegram extractor: a forwarded message, and a link to a public post. A pretend fetcher.
Each test name is one rule."""

# This file reads Persian text on purpose.
# ruff: noqa: RUF001

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unshelve.contracts.dto import Extracted, FetchResult, ForwardOrigin, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.article import GENERIC_SCORE
from unshelve.extract.saved_text import LAST_RESORT
from unshelve.extract.telegram import TelegramExtractor
from unshelve.testing.conformance import ExtractorCase, ExtractorConformance
from unshelve.testing.fakes import FakeFetcher, FakeLLM, InMemoryBlobStore

FIXTURES = Path(__file__).parent / "fixtures" / "telegram"
POST = "https://t.me/made_up_channel/42"
EMBED = f"{POST}?embed=1&mode=tme"
NOON = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class Fetcher(FakeFetcher):
    """A pretend fetcher that serves an embed page, and writes down what it was asked."""

    def __init__(self, page: str | None = "post.html", *, post: str = POST) -> None:
        super().__init__()
        self.asked: list[str] = []
        if page is not None:
            self.add(
                f"{post}?embed=1&mode=tme",
                (FIXTURES / page).read_bytes(),
                "text/html; charset=utf-8",
            )

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        self.asked.append(url)
        return await super().get(url, max_bytes=max_bytes, accept=accept)


def _context(fetcher: FakeFetcher) -> ExtractContext:
    return ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())


async def _extract(fetcher: Fetcher, ref: SourceRef | None = None) -> Extracted:
    ref = ref or SourceRef(url=POST, text=POST)
    return await TelegramExtractor().extract(ref, _context(fetcher))


def _forwarded(kind: str = "channel", name: str | None = "Other Channel") -> SourceRef:
    origin = ForwardOrigin.model_validate(
        {"type": kind, "date": NOON, "chat_id": -1001234567890, "message_id": 77, "name": name}
    )
    return SourceRef(text="Leases make a job queue safe.", forward_origin=origin)


class TestTelegramExtractor(ExtractorConformance):
    @pytest.fixture
    def case(self) -> ExtractorCase:
        return ExtractorCase(
            TelegramExtractor(),
            _context(Fetcher()),
            matching=SourceRef(url=POST),
            unrelated=SourceRef(url="https://blog.example.com/queues"),
        )


# --- Which sources are Telegram posts ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        POST,
        "https://t.me/s/made_up_channel/42",
        "https://telegram.me/Made_Up_Channel/42",
        "http://t.me/made_up_channel/42?single",
        "https://t.me/made_up_channel/42?comment=7",
    ],
)
def test_each_address_of_a_public_post_is_claimed_before_the_article_extractor(url: str) -> None:
    assert TelegramExtractor().match(SourceRef(url=url)) > GENERIC_SCORE


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/made_up_channel",  # the channel, not a post
        "https://t.me/c/1234567890/42",  # a post of a private channel
        "https://t.me/+AbCdEfGhIjK",  # an invitation
        "https://t.me/joinchat/AbCdEfGhIjK",
        "https://example.com/made_up_channel/42",
    ],
)
def test_other_links_are_left_to_other_extractors(url: str) -> None:
    assert TelegramExtractor().match(SourceRef(url=url, text=url)) == 0


def test_a_forwarded_message_without_a_link_is_claimed_before_the_saved_text() -> None:
    assert TelegramExtractor().match(_forwarded()) > LAST_RESORT


def test_a_forwarded_message_with_a_link_is_left_to_the_extractor_of_the_link() -> None:
    ref = _forwarded().model_copy(update={"url": "https://blog.example.com/queues"})

    assert TelegramExtractor().match(ref) == 0


def test_a_note_and_an_empty_forward_are_not_claimed() -> None:
    assert TelegramExtractor().match(SourceRef(text="Buy milk")) == 0
    assert TelegramExtractor().match(_forwarded().model_copy(update={"text": "  "})) == 0


# --- A forwarded message -----------------------------------------------------------------


async def test_a_forwarded_post_gives_its_channel_and_its_date_without_a_request() -> None:
    fetcher = Fetcher(None)

    found = await _extract(fetcher, _forwarded())

    assert fetcher.asked == []
    assert found.kind is ItemKind.POST
    assert found.author == "Other Channel"
    assert found.site == "t.me"
    assert found.published_at == NOON  # the date of the first post, not of the forward
    assert found.text == (
        "A post by Other Channel on Telegram, from 1 October 2026.\n\nLeases make a job queue safe."
    )


@pytest.mark.parametrize(
    ("kind", "name", "first_line"),
    [
        ("user", "Ada Example", "A message by Ada Example on Telegram, from 1 October 2026."),
        (
            "hidden_user",
            "Ada Example",
            "A message by Ada Example on Telegram, from 1 October 2026.",
        ),
        ("chat", "A Group", "A post by A Group on Telegram, from 1 October 2026."),
        ("unknown", "Other Channel", "A post by Other Channel on Telegram, from 1 October 2026."),
        ("channel", None, "A forwarded message on Telegram, from 1 October 2026."),
    ],
)
async def test_each_kind_of_forward_names_its_source(
    kind: str, name: str | None, first_line: str
) -> None:
    found = await _extract(Fetcher(None), _forwarded(kind, name))

    assert found.text.splitlines()[0] == first_line
    assert found.author == name


# --- A link to a public post -------------------------------------------------------------


async def test_a_post_gives_its_channel_date_text_and_link_preview() -> None:
    fetcher = Fetcher()

    found = await _extract(fetcher)

    assert fetcher.asked == [EMBED]
    assert found.kind is ItemKind.POST
    assert found.title is None  # the model writes the title
    assert found.author == "Made-up Channel (@made_up_channel)"
    assert found.site == "t.me"
    assert found.final_url == POST
    assert found.published_at == datetime(2026, 9, 30, 8, 15, tzinfo=UTC)
    assert found.text == (
        "A post by Made-up Channel (@made_up_channel) on Telegram, from 30 September 2026.\n\n"
        "A job queue in Postgres & a lease for each job: SKIP LOCKED makes it safe.\n"
        "https://example.com/queues\n\n"
        "Read the notes too. Parts 1 \u2022 2 #queues @made_up_channel\n\n"
        'It links to "Queues in Postgres" (Example Blog): How SKIP LOCKED lets many workers '
        "share one table."
    )
    # A hashtag ("?q=%23queues") and a link of the app ("tg://…") are no web addresses.
    assert found.extra_links == ["https://example.com/queues", "https://example.com/notes"]


async def test_the_link_of_a_preview_is_kept_when_the_text_does_not_have_it() -> None:
    page = (FIXTURES / "post.html").read_text(encoding="utf-8")
    page = page.replace(
        '<a class="tgme_widget_message_link_preview" href="https://example.com/queues">',
        '<a class="tgme_widget_message_link_preview" href="https://example.com/other">',
    )
    fetcher = Fetcher(None)
    fetcher.add(EMBED, page.encode(), "text/html; charset=utf-8")

    found = await _extract(fetcher)

    assert found.extra_links[-1] == "https://example.com/other"


async def test_the_number_of_views_is_not_in_the_text() -> None:
    found = await _extract(Fetcher())

    assert "12.3K" not in found.text


async def test_photos_are_counted_and_the_caption_is_kept() -> None:
    post = "https://t.me/made_up_channel/43"

    found = await _extract(Fetcher("post_photos.html", post=post), SourceRef(url=post))

    assert found.text.endswith("Two pictures of the table.\n\nIt has 2 photos.")


async def test_a_video_without_a_text_is_said() -> None:
    post = "https://t.me/made_up_channel/44"

    found = await _extract(Fetcher("post_video.html", post=post), SourceRef(url=post))

    assert found.text == (
        "A post by Made-up Channel (@made_up_channel) on Telegram, from 2 October 2026.\n\n"
        "It has 1 video."
    )


async def test_a_persian_post_keeps_its_letters_and_its_zero_width_non_joiner() -> None:
    post = "https://t.me/sample_channel/7"

    found = await _extract(Fetcher("post_fa.html", post=post), SourceRef(url=post))

    assert found.author == "کانال نمونه (@sample_channel)"
    assert "می\u200cسازیم" in found.text


# --- When the post is not there ----------------------------------------------------------


async def test_a_post_that_does_not_exist_is_an_extract_error() -> None:
    with pytest.raises(ExtractError, match="Post not found"):
        await _extract(Fetcher("not_found.html"))


async def test_a_page_without_a_post_is_an_extract_error() -> None:
    fetcher = Fetcher(None)
    fetcher.add(EMBED, b"<html><body>Please wait</body></html>", "text/html")

    with pytest.raises(ExtractError, match="no post"):
        await _extract(fetcher)


@pytest.mark.parametrize(
    "failure",
    [
        FetchError("http_error", EMBED, status=429, retry_after=60),
        FetchError("http_error", EMBED, status=503),
        FetchError("timeout", EMBED),
        FetchError("http_error", EMBED, status=404),
    ],
    ids=["429", "503", "timeout", "404"],
)
async def test_a_failure_of_the_page_goes_up_unchanged(failure: FetchError) -> None:
    fetcher = Fetcher(None)
    fetcher.pages[EMBED] = failure

    with pytest.raises(FetchError) as caught:
        await _extract(fetcher)

    # The fetch step decides: try again later, or keep the saved text.
    assert caught.value is failure
