"""The article extractor: the main text of a web page, without its menus and its footer.

It asks the fetcher for the page, decodes the bytes itself (``extract.html``), and lets
trafilatura find the main text. trafilatura never opens a link here: it gets text.

A link to a page can answer with a PDF file ("/download?id=7"). The fetcher refuses that body
after its headers, and the PDF extractor reads the link (``extract.pdf``).
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urlsplit

from trafilatura import bare_extraction
from trafilatura.settings import Document

from unshelve.contracts.dto import Extracted, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext, accepts
from unshelve.extract.html import decode_html, language_of
from unshelve.extract.pdf import PDF_TYPES, read_pdf

# Any web page can be an article. An extractor for one site gives a higher score.
GENERIC_SCORE = 10
HTML_TYPES = ("text/html", "application/xhtml+xml")
MAX_HTML_BYTES = 5_000_000
# Less text than this is no article: a frame for JavaScript, a sign-in wall, an error page.
# Then the item keeps the text of the saved message, and that is better than such a sentence.
MIN_TEXT_LENGTH = 200
# A link to a file is no web page. Other extractors read PDF files and pictures.
_FILES = frozenset(
    {
        "pdf",
        "png", "jpg", "jpeg", "gif", "webp", "svg", "heic",
        "mp3", "m4a", "wav", "ogg",
        "mp4", "mov", "webm", "mkv",
        "zip", "gz", "tgz", "bz2", "xz", "7z", "rar", "tar",
        "exe", "dmg", "apk", "msi",
        "csv", "xlsx", "docx", "pptx", "epub",
    }
)  # fmt: skip
# trafilatura keeps only the characters that Python calls printable. The zero-width
# non-joiner (U+200C) is part of the spelling of Persian ("همسایه‌ها"), and the zero-width  # noqa: RUF003 - Persian text
# joiner (U+200D) holds emoji such as 👨‍👩‍👧 together, but neither is "printable". So they go
# through trafilatura as two rare printable symbols, and they come back after it.
_JOINERS = {"\u200c": "\U0001d306", "\u200d": "\U0001d307"}


class ArticleExtractor:
    name: ClassVar[str] = "article"
    version: ClassVar[int] = 1

    def match(self, ref: SourceRef) -> int:
        if ref.url is None:
            return 0
        parts = urlsplit(ref.url)
        if parts.scheme.lower() not in ("http", "https"):
            return 0
        last = parts.path.rsplit("/", 1)[-1].lower()
        if "." in last and last.rsplit(".", 1)[-1] in _FILES:
            return 0
        return GENERIC_SCORE

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        if ref.url is None:
            raise ExtractError("an article needs a link")
        try:
            page = await ctx.fetcher.get(ref.url, max_bytes=MAX_HTML_BYTES, accept=list(HTML_TYPES))
        except FetchError as error:
            # The link answered with a PDF. The fetcher stopped after the headers, and the PDF
            # extractor asks for the file again, with the limits of a file.
            if error.reason == "unsupported" and accepts(error.content_type, PDF_TYPES):
                return await read_pdf(ref.url, ctx)
            # Any other FetchError goes up unchanged: its reason, code and wait are for the
            # step that decides between "try again" and "keep the saved text".
            raise
        html = decode_html(page.body, page.content_type)
        # trafilatura works for a moment on a long page. In a thread, the event loop of the
        # worker stays free, and the lease of the job keeps its heartbeat.
        article = await asyncio.to_thread(_read, html, page.final_url)
        text = article.text.strip() if article is not None else ""
        if article is None or len(text) < MIN_TEXT_LENGTH:
            raise ExtractError(f"the page has only {len(text)} characters of text")
        return Extracted(
            kind=ItemKind.ARTICLE,
            text=text,
            title=article.title,
            author=article.author,
            site=_site(page.final_url),
            lang=language_of(html),
            final_url=page.final_url,
            published_at=_day(article.date),
        )


@dataclass(frozen=True, slots=True)
class _Article:
    text: str
    title: str | None
    author: str | None
    date: str | None


def _read(html: str, url: str) -> _Article | None:
    """The main text, the title and the author of a page, with their joiners."""
    # A page that has one of the symbols already keeps it, and loses its joiners instead.
    swap = not any(symbol in html for symbol in _JOINERS.values())
    if swap:
        for joiner, symbol in _JOINERS.items():
            html = html.replace(joiner, symbol)
    found = bare_extraction(
        html,
        url=url,
        with_metadata=True,
        # trafilatura puts comments into a field of their own, which we do not read. So this
        # changes nothing in the text, and it saves the work.
        include_comments=False,
        include_tables=True,
        # Headings and lists stay ("## A heading", "- a point"). A model and a reader need them.
        include_formatting=True,
        include_links=False,
    )
    if not isinstance(found, Document):
        return None

    def back(value: str | None) -> str | None:
        if value and swap:
            for joiner, symbol in _JOINERS.items():
                value = value.replace(symbol, joiner)
        return value or None

    return _Article(
        text=back(found.text) or "",
        title=back(found.title),
        author=back(found.author),
        date=found.date,
    )


def _site(url: str) -> str | None:
    host = urlsplit(url).hostname
    return host.removeprefix("www.") if host else None


def _day(value: str | None) -> datetime | None:
    """The date of the page ("2026-09-30") as the start of that day in UTC."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
