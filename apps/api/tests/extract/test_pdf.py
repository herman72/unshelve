"""The PDF extractor, on made-up files that Chrome printed. A pretend fetcher serves them, and
the reader runs in a child process, as in the worker. Each test name is one rule."""

# This file reads Persian text on purpose.
# ruff: noqa: RUF001

import json
import math
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from unshelve.contracts.dto import Extracted, FetchResult, MediaRef, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract import pdf
from unshelve.extract.article import GENERIC_SCORE, ArticleExtractor
from unshelve.extract.pdf import (
    ACCEPT,
    MAX_PDF_BYTES,
    PdfExtractor,
    author_of,
    clean_text,
    made_on,
    title_of,
)
from unshelve.reading import reading_minutes
from unshelve.testing.conformance import ExtractorCase, ExtractorConformance
from unshelve.testing.fakes import FakeFetcher, FakeFiles, FakeLLM, InMemoryBlobStore

FIXTURES = Path(__file__).parent / "fixtures" / "pdf"
LINK = "https://example.com/papers/leases.pdf"
SENT = MediaRef(
    kind="document",
    file_id="BQACAgMadeUpFileId",
    file_unique_id="AgADmadeup",
    mime="application/pdf",
    size_bytes=96_235,
    file_name="leases.pdf",
)


class Fetcher(FakeFetcher):
    """A pretend fetcher that serves one fixture at LINK, and writes down how it was asked."""

    def __init__(
        self, name: str | None = "paper.pdf", content_type: str = "application/pdf"
    ) -> None:
        super().__init__()
        self.asked: list[tuple[str, int, list[str]]] = []
        if name is not None:
            self.add(LINK, (FIXTURES / name).read_bytes(), content_type)

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        self.asked.append((url, max_bytes, list(accept)))
        return await super().get(url, max_bytes=max_bytes, accept=accept)


def _context(fetcher: FakeFetcher) -> ExtractContext:
    return ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())


async def _extract(fetcher: FakeFetcher, url: str = LINK) -> Extracted:
    return await PdfExtractor().extract(SourceRef(url=url, text=url), _context(fetcher))


def _body(found: Extracted) -> str:
    """The text of the pages, after the lines about the file."""
    return found.text.split("\n\n", 1)[1]


class TestPdfExtractor(ExtractorConformance):
    @pytest.fixture
    def case(self) -> ExtractorCase:
        return ExtractorCase(
            PdfExtractor(),
            _context(Fetcher()),
            matching=SourceRef(url=LINK),
            unrelated=SourceRef(url="https://blog.example.com/queues"),
        )


# --- Which links are PDF files -----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        LINK,
        "https://example.com/papers/LEASES.PDF",
        "http://example.com/leases.pdf",
        "https://example.com/leases.pdf?download=1",
        "https://example.com/leases.pdf#page=3",
    ],
)
def test_a_link_to_a_pdf_file_is_claimed_and_the_article_extractor_leaves_it(url: str) -> None:
    ref = SourceRef(url=url)

    assert PdfExtractor().match(ref) > GENERIC_SCORE
    assert ArticleExtractor().match(ref) == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/pdf",
        "https://example.com/leases.pdf.html",
        "https://example.com/leases.pdfx",
        "https://example.com/?file=leases.pdf",
        "ftp://example.com/leases.pdf",
        None,
    ],
)
def test_other_links_and_notes_are_left_to_other_extractors(url: str | None) -> None:
    assert PdfExtractor().match(SourceRef(url=url, text="a note")) == 0


# --- What a PDF gives --------------------------------------------------------------------


async def test_a_paper_gives_its_title_its_pages_its_day_and_its_text() -> None:
    found = await _extract(Fetcher())

    assert found.kind is ItemKind.PDF
    assert (found.title, found.author) == ("Leases for Job Queues in Postgres", None)
    assert (found.site, found.final_url) == ("example.com", LINK)
    assert found.published_at == datetime(2026, 10, 15, tzinfo=UTC)
    assert found.minutes is None  # the pipeline counts the minutes of the whole text
    assert found.lang is None  # the model finds the language in the text
    assert found.text.splitlines()[:5] == [
        "Leases for Job Queues in Postgres",
        "A PDF document, 3 pages, made on 15 October 2026.",
        "",
        "Leases for Job Queues in Postgres",
        "A. Made-up and B. Example",
    ]
    assert "two workers never take the same row" in found.text
    assert found.text.endswith("[1] C. Made-up. Queues in practice. 2025.")


async def test_the_links_of_the_annotations_and_of_the_text_become_extra_links() -> None:
    found = await _extract(Fetcher())

    assert found.extra_links == [
        "https://example.org/queue-data",  # the words "our data page" open it
        "https://example.com/queue-notes",  # written in the text, without the full stop
    ]


async def test_only_web_links_become_extra_links_each_one_time_and_20_at_most(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A made-up answer of the reader: links of other schemes, a link two times, and 30 links
    # in the text, each with a full stop after it.
    annotations = [
        "javascript:alert(1)",
        "ftp://example.org/file",
        "https://example.org/a",
        "https://example.org/a",
    ]
    text = "Made-up links: " + " ".join(f"https://example.org/{n}." for n in range(30))
    answer = json.dumps({"pages": 1, "read": 1, "texts": [text], "links": annotations})
    monkeypatch.setattr(pdf, "CHILD", (sys.executable, "-c", f"print({answer!r})"))

    found = await _extract(Fetcher())

    expected = ["https://example.org/a", *(f"https://example.org/{n}" for n in range(19))]
    assert found.extra_links == expected


async def test_the_file_is_asked_for_once_with_the_types_of_a_pdf() -> None:
    fetcher = Fetcher()

    await _extract(fetcher)

    assert fetcher.asked == [(LINK, MAX_PDF_BYTES, list(ACCEPT))]


async def test_slides_keep_their_author_and_lose_the_title_that_a_program_wrote() -> None:
    found = await _extract(Fetcher("slides.pdf"))

    assert (found.title, found.author) == (None, "Made-up Speaker")
    assert found.text.splitlines()[:3] == [
        "A PDF document by Made-up Speaker, 3 pages, made on 15 October 2026.",
        "",
        "Queues in practice",
    ]


async def test_a_persian_pdf_gives_letters_and_not_the_forms_of_its_glyphs() -> None:
    found = await _extract(Fetcher("persian.pdf"))

    assert found.title == "صف کارها در پستگرس"
    assert found.text.splitlines()[1] == "A PDF document, 1 page, made on 15 October 2026."
    body = _body(found)
    assert body.splitlines()[0] == "صف کارها در پستگرس"
    assert "در این نوشته یک صف کار را روی یک جدول" in body
    assert "هرگز" in body
    glyph_forms = [sign for sign in body if 0xFB50 <= ord(sign) <= 0xFEFC]
    assert glyph_forms == []
    assert chr(0x06BE) not in body


async def test_a_file_without_a_type_is_read_by_its_first_bytes() -> None:
    found = await _extract(Fetcher("paper.pdf", "application/octet-stream"))

    assert found.title == "Leases for Job Queues in Postgres"


async def test_a_file_in_a_repository_on_github_is_asked_for_as_a_raw_file() -> None:
    # The address with "/blob/" opens a page that shows the file; "/raw/" gives the file.
    page = "https://github.com/made-up-org/queue-kit/blob/main/docs/leases.pdf"
    raw = "https://github.com/made-up-org/queue-kit/raw/main/docs/leases.pdf"
    fetcher = Fetcher(None)
    fetcher.add(raw, (FIXTURES / "paper.pdf").read_bytes(), "application/octet-stream")

    found = await _extract(fetcher, page)

    assert found.title == "Leases for Job Queues in Postgres"
    assert [url for url, *_ in fetcher.asked] == [raw]


async def test_a_file_with_bytes_before_its_mark_is_read() -> None:
    # The mark "%PDF-" can stand anywhere in the first 1,024 bytes, and pypdf reads the file.
    fetcher = Fetcher(None)
    paper = (FIXTURES / "paper.pdf").read_bytes()
    fetcher.add(LINK, b"made-up bytes before the mark\n" + paper, "application/pdf")

    found = await _extract(fetcher)

    assert found.title == "Leases for Job Queues in Postgres"


async def test_a_file_that_limits_only_printing_is_read() -> None:
    found = await _extract(Fetcher("owner_password.pdf"))

    assert _body(found).startswith("Queues in practice")


@pytest.mark.parametrize(
    ("pages", "line"),
    [(1, "The text is from the first page."), (2, "The text is from the first 2 pages.")],
)
async def test_a_long_file_is_read_in_part_and_gets_the_minutes_of_all_its_pages(
    monkeypatch: pytest.MonkeyPatch, pages: int, line: str
) -> None:
    monkeypatch.setattr(pdf, "MAX_PAGES", pages)

    found = await _extract(Fetcher())

    assert found.text.splitlines()[1] == f"A PDF document, 3 pages, made on 15 October 2026. {line}"
    # 3 pages, of which `pages` were read: the minutes of the text, times 3 / pages.
    assert found.minutes == math.ceil(reading_minutes(_body(found)) * 3 / pages)


# --- A PDF that you send as a file -------------------------------------------------------


class RecordingFiles(FakeFiles):
    """A pretend source of files that also writes down the limit that it was asked with."""

    def __init__(self, data: bytes | FetchError) -> None:
        super().__init__({str(SENT.file_id): data})
        self.limits: list[int] = []

    async def read(self, media: MediaRef, *, max_bytes: int) -> bytes:
        self.limits.append(max_bytes)
        return await super().read(media, max_bytes=max_bytes)


async def _extract_sent(files: FakeFiles | None, ref: SourceRef | None = None) -> Extracted:
    context = ExtractContext(
        fetcher=Fetcher(None), blobs=InMemoryBlobStore(), llm=FakeLLM(), files=files
    )
    return await PdfExtractor().extract(ref or SourceRef(text="A paper", media=[SENT]), context)


async def test_a_pdf_sent_as_a_file_gives_its_title_its_pages_and_its_text() -> None:
    files = RecordingFiles((FIXTURES / "paper.pdf").read_bytes())

    found = await _extract_sent(files)

    assert (found.kind, found.title) == (ItemKind.PDF, "Leases for Job Queues in Postgres")
    assert (found.site, found.final_url) == (None, None)  # it has no address
    assert found.text.splitlines()[1] == "A PDF document, 3 pages, made on 15 October 2026."
    assert files.limits == [MAX_PDF_BYTES]


@pytest.mark.parametrize(
    ("media", "claimed"),
    [
        (SENT, True),
        (SENT.model_copy(update={"mime": "application/octet-stream", "file_name": "A.PDF"}), True),
        (SENT.model_copy(update={"mime": "application/zip", "file_name": "data.zip"}), False),
        (SENT.model_copy(update={"mime": None, "file_name": None}), False),
        (MediaRef(kind="photo", file_id="AgACAgMadeUp", file_unique_id="AQADmadeup"), False),
        (SENT.model_copy(update={"kind": "video", "mime": "video/mp4", "file_name": "a.mp4"}),
         False),
    ],
    ids=["a PDF", "a PDF by its name", "a zip file", "no type, no name", "a photo", "a video"],
)  # fmt: skip
def test_a_document_is_a_pdf_by_its_type_or_by_its_name(media: MediaRef, claimed: bool) -> None:
    score = PdfExtractor().match(SourceRef(text="a caption", media=[media]))

    assert (score > GENERIC_SCORE) is claimed
    assert (score == 0) is not claimed


async def test_a_file_comes_before_a_link_to_a_page_in_its_caption() -> None:
    files = RecordingFiles((FIXTURES / "paper.pdf").read_bytes())
    ref = SourceRef(url="https://blog.example.com/queues", text="See also", media=[SENT])

    found = await _extract_sent(files, ref)

    assert found.title == "Leases for Job Queues in Postgres"


async def test_a_sent_file_without_a_source_of_files_is_an_extract_error() -> None:
    # The CLI reads links only: its context has no files.
    with pytest.raises(ExtractError, match="no file of a message"):
        await _extract_sent(None)


async def test_a_failure_of_a_sent_file_goes_up_unchanged() -> None:
    busy = FetchError("http_error", "telegram file AgADmadeup", status=429, retry_after=7)

    with pytest.raises(FetchError) as caught:
        await _extract_sent(RecordingFiles(busy))

    assert caught.value is busy


async def test_a_sent_file_that_is_no_pdf_is_refused_before_a_reader_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf, "CHILD", ("/no/such/reader",))  # it would fail if it started

    with pytest.raises(ExtractError, match="not a PDF"):
        await _extract_sent(RecordingFiles(b"<html><body>Not a PDF.</body></html>"))


# --- Files that give no text -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("scanned.pdf", "0 characters of text: it can be a scan"),
        ("locked.pdf", "needs a password"),
        ("cut.pdf", "cannot be read"),
    ],
)
async def test_a_file_without_text_is_an_extract_error(name: str, reason: str) -> None:
    # The item keeps its saved text.
    with pytest.raises(ExtractError, match=reason):
        await _extract(Fetcher(name))


async def test_bytes_that_are_no_pdf_are_refused_before_a_reader_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf, "CHILD", ("/no/such/reader",))  # it would fail if it started
    fetcher = Fetcher(None)
    fetcher.add(LINK, b"<html><body>A page of a made-up shop.</body></html>", "application/pdf")

    with pytest.raises(ExtractError, match="not a PDF"):
        await _extract(fetcher)


# --- The child process -------------------------------------------------------------------


async def test_a_reader_that_does_not_finish_in_time_is_stopped_and_waited_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid_file = tmp_path / "pid"
    slow = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    monkeypatch.setattr(pdf, "CHILD", (sys.executable, "-c", slow))
    monkeypatch.setattr(pdf, "READ_SECONDS", 0.5)

    with pytest.raises(ExtractError, match=r"not read in 0\.5 seconds"):
        await _extract(Fetcher())

    # The process is gone: not running, and not left as a zombie.
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_a_reader_that_the_system_stops_is_an_extract_error_and_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    monkeypatch.setattr(pdf, "CHILD", (sys.executable, "-c", killed))
    # A new logger: one that an earlier test configured and cached would not be captured.
    monkeypatch.setattr(pdf, "log", structlog.get_logger())

    with capture_logs() as logs, pytest.raises(ExtractError, match="stopped with code -9"):
        await _extract(Fetcher())

    [event] = [entry for entry in logs if entry["event"] == "pdf.reader_stopped"]
    assert event["code"] == -9


async def test_an_answer_that_does_not_fit_is_an_extract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf, "CHILD", (sys.executable, "-c", "print('no JSON')"))

    with pytest.raises(ExtractError, match="does not fit"):
        await _extract(Fetcher())


# --- Failures of the fetch ---------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        FetchError("http_error", LINK, status=404),
        FetchError("http_error", LINK, status=429, retry_after=120),
        FetchError("http_error", LINK, status=503),
        FetchError("timeout", LINK),
        FetchError("too_large", LINK),
    ],
    ids=["404", "429", "503", "timeout", "too_large"],
)
async def test_a_failure_of_the_fetch_goes_up_unchanged(error: FetchError) -> None:
    fetcher = Fetcher(None)
    fetcher.pages[LINK] = error

    with pytest.raises(FetchError) as caught:
        await _extract(fetcher)

    # The fetch step tries a passing failure again, and keeps the saved text for the others.
    assert caught.value is error


async def test_a_page_at_a_link_to_a_pdf_is_unsupported() -> None:
    fetcher = Fetcher(None)
    fetcher.add(LINK, "<html><body>Sign in to read this paper.</body></html>", "text/html")

    with pytest.raises(FetchError) as caught:
        await _extract(fetcher)

    assert (caught.value.reason, caught.value.content_type) == ("unsupported", "text/html")


# --- The facts and the text --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "title"),
    [
        ("Leases for Job Queues in Postgres", "Leases for Job Queues in Postgres"),
        ("  Leases  for\nJob Queues ", "Leases for Job Queues"),
        ("Microsoft Word - Final report", "Final report"),
        ("Microsoft PowerPoint - queues-talk.pptx", None),
        ("scanned.html", None),
        ("draft 3.docx", None),
        ("Untitled", None),
        ("PowerPoint Presentation", None),
        ("Notes on Node.js", "Notes on Node.js"),
        ("ab", None),
        (None, None),
    ],
)
def test_a_title_that_a_program_wrote_is_no_title(raw: str | None, title: str | None) -> None:
    assert title_of(raw) == title


@pytest.mark.parametrize(
    ("raw", "author"),
    [
        ("Made-up Speaker", "Made-up Speaker"),
        ("  A.  Made-up ", "A. Made-up"),
        ("Administrator", None),
        ("user", None),
        ("", None),
        (None, None),
    ],
)
def test_the_name_that_a_computer_gives_its_user_is_no_author(
    raw: str | None, author: str | None
) -> None:
    assert author_of(raw) == author


def test_a_long_list_of_authors_is_cut_after_a_whole_name() -> None:
    author = author_of("Made-up Name, " * 30)

    assert author is not None
    assert len(author) <= 202
    assert author.endswith("Name, …")


@pytest.mark.parametrize(
    ("raw", "day"),
    [
        ("D:20190429173603Z", datetime(2019, 4, 29, tzinfo=UTC)),
        ("D:20261015191205+00'00'", datetime(2026, 10, 15, tzinfo=UTC)),
        ("20190429", datetime(2019, 4, 29, tzinfo=UTC)),
        ("D:19700101000000Z", None),  # the clock of a computer, before PDF existed
        ("D:21000101", None),
        ("D:20191340", None),  # no 13th month
        ("D:2019", None),
        ("yesterday", None),
        (None, None),
    ],
)
def test_the_day_on_which_the_file_was_made(raw: str | None, day: datetime | None) -> None:
    assert made_on(raw) == day


def test_glyph_forms_become_letters() -> None:
    ligature_fi = chr(0xFB01)
    sad_initial, feh_final = chr(0xFEBB), chr(0xFED2)

    assert clean_text(f"ef{ligature_fi}cient") == "efficient"
    assert clean_text(sad_initial + feh_final) == "صف"


def test_heh_doachashmee_becomes_heh_in_persian_and_stays_in_urdu() -> None:
    doachashmee = chr(0x06BE)

    assert clean_text(f"کار{doachashmee}ا") == "کارها"
    # Urdu writes it as a letter of its own ("بھائی"); tteh (ٹ) shows that the text is Urdu.
    urdu = f"ب{doachashmee}ائی ٹیم"
    assert clean_text(urdu) == urdu


def test_line_breaks_spaces_and_empty_lines_become_one_kind_each() -> None:
    raw = "Title\r\nline  two\t\twith" + chr(0xA0) + "space \r\n\r\n\r\n\rnext" + chr(0x0C)
    raw += chr(0) + chr(0xFEFF)

    assert clean_text(raw) == "Title\nline two with space\n\nnext"
    assert clean_text("one\rtwo") == "one\ntwo"
