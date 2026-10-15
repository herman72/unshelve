"""The article extractor, on pages that the tests make. A pretend fetcher: no network."""

# This file reads Persian text on purpose. Its letters are no Latin look-alikes.
# ruff: noqa: RUF001

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unshelve.contracts.dto import Extracted, FetchResult, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract import article as article_module
from unshelve.extract.article import GENERIC_SCORE, HTML_TYPES, MAX_HTML_BYTES, ArticleExtractor
from unshelve.testing.conformance import ExtractorCase, ExtractorConformance
from unshelve.testing.fakes import FakeFetcher, FakeLLM, InMemoryBlobStore

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://www.blog.example.com/2026/queues"


class Fetcher(FakeFetcher):
    """A pretend fetcher that also writes down how it was asked."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[tuple[str, int, list[str]]] = []

    def serve(
        self,
        name: str,
        *,
        encoding: str = "utf-8",
        content_type: str | None = "text/html; charset=utf-8",
        final_url: str = URL,
    ) -> None:
        body = (FIXTURES / name).read_text(encoding="utf-8").encode(encoding)
        self.pages[URL] = FetchResult(
            url=URL, final_url=final_url, status=200, content_type=content_type, body=body
        )

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        self.asked.append((url, max_bytes, list(accept)))
        return await super().get(url, max_bytes=max_bytes, accept=accept)


def _context(fetcher: FakeFetcher) -> ExtractContext:
    return ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())


async def _extract(fetcher: Fetcher, url: str = URL) -> Extracted:
    return await ArticleExtractor().extract(SourceRef(url=url), _context(fetcher))


class TestArticleExtractor(ExtractorConformance):
    @pytest.fixture
    def case(self) -> ExtractorCase:
        fetcher = Fetcher()
        fetcher.serve("article_en.html")
        return ExtractorCase(
            ArticleExtractor(),
            _context(fetcher),
            matching=SourceRef(url=URL),
            unrelated=SourceRef(text="a saved note without a link"),
        )


# --- The text ------------------------------------------------------------------------------


async def test_an_article_keeps_its_text_and_leaves_the_rest_of_the_page() -> None:
    fetcher = Fetcher()
    fetcher.serve("article_en.html")

    found = await _extract(fetcher)

    assert found.kind is ItemKind.ARTICLE
    assert "the second worker takes the next free row" in found.text
    assert "## Why SKIP LOCKED" in found.text  # a heading stays a heading
    assert "- One row for each job." in found.text  # and a list stays a list
    assert "“é” must survive" in found.text
    for other in ("Sign in", "Popular posts", "Subscribe", "Great post", "LISTEN and NOTIFY"):
        assert other not in found.text


async def test_the_facts_of_the_page_come_with_the_text() -> None:
    fetcher = Fetcher()
    fetcher.serve("article_en.html")

    found = await _extract(fetcher)

    assert (found.title, found.author, found.lang) == ("Queues in Postgres", "Ada Example", "en")
    assert found.published_at == datetime(2026, 9, 30, tzinfo=UTC)
    assert (found.site, found.final_url) == ("blog.example.com", URL)


async def test_the_site_is_the_host_after_the_redirects() -> None:
    fetcher = Fetcher()
    fetcher.serve("article_en.html", final_url="https://www.other.example.org/queues")

    found = await _extract(fetcher)

    assert (found.site, found.final_url) == (
        "other.example.org",
        "https://www.other.example.org/queues",
    )


async def test_a_persian_page_in_an_old_encoding_is_read_correctly() -> None:
    fetcher = Fetcher()
    # The server names no charset. The page names windows-1256 in a meta tag.
    fetcher.serve("article_fa.html", encoding="cp1256", content_type="text/html")

    found = await _extract(fetcher)

    assert found.title == "جستجوي برداري در پستگرس"
    assert "نزديک‌ترين همسايه‌ها" in found.text
    assert found.lang == "fa"
    for other in ("درباره ما", "پربازديدترين", "حقوق محفوظ"):
        assert other not in found.text


PAGE_WITH_JOINERS = """<html><head><meta charset="utf-8"></head><body><article>
<h1>نزديک\u200cترين همسايه\u200cها</h1>
<p>A family emoji 👨\u200d👩\u200d👧 has two joiners inside. Without them it becomes three
separate faces. This paragraph is long enough to be read as the text of an article, with a
few more words after this sentence, and then a few more, so that it has enough letters.</p>
<p>در اين نوشته، هر دو راه را با هم مقايسه مي\u200cکنيم
و نشان مي\u200cدهيم که کدام راه بهتر است.</p>
</article></body></html>"""


async def test_the_joiners_of_persian_and_of_emoji_survive() -> None:
    fetcher = Fetcher()
    fetcher.add(URL, PAGE_WITH_JOINERS, content_type="text/html; charset=utf-8")

    found = await _extract(fetcher)

    assert found.title == "نزديک\u200cترين همسايه\u200cها"
    assert "👨\u200d👩\u200d👧" in found.text
    assert "مقايسه مي\u200cکنيم" in found.text


async def test_a_page_with_the_symbol_of_the_swap_keeps_the_symbol() -> None:
    # The swap uses two rare symbols. A page that has one of them keeps it as it is.
    page = PAGE_WITH_JOINERS.replace("A family emoji", "The symbol \U0001d306 and a family emoji")
    fetcher = Fetcher()
    fetcher.add(URL, page, content_type="text/html; charset=utf-8")

    found = await _extract(fetcher)

    assert "The symbol \U0001d306 and" in found.text


async def test_a_page_without_the_text_of_an_article_is_an_extract_error() -> None:
    fetcher = Fetcher()
    fetcher.serve("app_shell.html")

    # The page is only a frame for JavaScript. Its one sentence is no article.
    with pytest.raises(ExtractError):
        await _extract(fetcher)


async def test_the_page_is_asked_for_as_html_with_a_size_limit() -> None:
    fetcher = Fetcher()
    fetcher.serve("article_en.html")

    await _extract(fetcher)

    assert fetcher.asked == [(URL, MAX_HTML_BYTES, list(HTML_TYPES))]


async def test_a_long_reading_does_not_stop_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker sends the heartbeat of its job from the same event loop. A reading that held
    # the loop for a long time would stop the heartbeat, and the job could be given away.
    read = article_module._read  # pyright: ignore[reportPrivateUsage]

    def slow_read(html: str, url: str) -> object:
        time.sleep(0.3)  # the work of trafilatura on a very long page
        return read(html, url)

    monkeypatch.setattr(article_module, "_read", slow_read)
    fetcher = Fetcher()
    fetcher.serve("article_en.html")
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    beating = asyncio.create_task(heartbeat())
    try:
        await _extract(fetcher)
    finally:
        beating.cancel()

    assert ticks >= 5  # about 15 in 0.3 s; 0 if the reading held the loop


# --- Errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        FetchError("http_error", URL, status=404),
        FetchError("http_error", URL, status=429, retry_after=30.0),
        FetchError("timeout", URL),
        FetchError("blocked_address", URL),
    ],
    ids=["404", "429", "timeout", "blocked"],
)
async def test_an_error_of_the_fetcher_is_passed_on_unchanged(error: FetchError) -> None:
    fetcher = Fetcher()
    fetcher.pages[URL] = error

    with pytest.raises(FetchError) as caught:
        await _extract(fetcher)

    # The same error: its reason, its code and its wait reach the step that decides.
    assert caught.value is error


async def test_a_source_without_a_link_is_an_extract_error() -> None:
    with pytest.raises(ExtractError):
        await ArticleExtractor().extract(SourceRef(text="a note"), _context(Fetcher()))


# --- Which links it claims ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "score"),
    [
        ("https://blog.example.com/queues", GENERIC_SCORE),
        ("http://example.com/a?page=2", GENERIC_SCORE),
        ("https://example.com/notes.html", GENERIC_SCORE),
        (None, 0),
        ("ftp://example.com/file.txt", 0),
        ("https://example.com/paper.pdf", 0),
        ("https://example.com/report.PDF?download=1", 0),
        ("https://example.com/photo.jpg", 0),
        ("https://example.com/video.mp4#t=10", 0),
        ("https://example.com/archive.tar.gz", 0),
    ],
)
def test_it_claims_web_pages_and_leaves_files_to_others(url: str | None, score: int) -> None:
    assert ArticleExtractor().match(SourceRef(url=url)) == score
