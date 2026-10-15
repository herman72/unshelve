"""The PDF extractor: the text, the title and the length of a PDF file, at a link or sent as
a file in the message.

The fetcher brings the bytes of a link; the files of the message (``ctx.files``) bring the
bytes of a file. A child process reads them (``extract.pdf_reader``): a file that
keeps the reader busy, or that fills the memory, stops only that process, and the worker goes
on. The time limit is ``READ_SECONDS``.

The text then loses the marks of the PDF format. A page ends with a line break; letters can
come as glyph forms: a ligature such as "fi" as one sign (U+FB01), or an Arabic or Persian
letter in the form for its place in the word. The Persian letter heh (U+0647) can come as heh
doachashmee (U+06BE), because a font can draw both with one glyph.

A scanned file has pictures of its pages and no text. Then the item keeps its saved text.
"""

import asyncio
import math
import re
import sys
import unicodedata
from datetime import UTC, datetime
from typing import ClassVar, Literal
from urllib.parse import urlsplit

import structlog
from pydantic import BaseModel, Field, ValidationError

from unshelve.contracts.dto import Extracted, MediaRef, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError
from unshelve.contracts.protocols import ExtractContext
from unshelve.reading import reading_minutes

log = structlog.get_logger()

# An extractor for one site (50) knows more than an extractor for a type of file: a link to
# the PDF of a paper on arXiv goes to the arXiv extractor.
FILE_SCORE = 30
PDF_TYPES = ("application/pdf", "application/x-pdf")
# Some servers send a PDF as a file without a type. Its first bytes say what it is.
ACCEPT = (*PDF_TYPES, "application/octet-stream", "binary/octet-stream")
MAX_PDF_BYTES = 30_000_000
# The text of a long file stops after these pages or characters. The model reads the first
# 24,000 characters, and the vectors cover about 160,000.
MAX_PAGES = 300
MAX_TEXT_CHARS = 300_000
# In the worker image, the made-up paper of the tests takes 0.4 seconds, with the start of
# the process.
READ_SECONDS = 30
# Less text than this is no text layer: a scan, with a page number at most.
MIN_TEXT_CHARS = 30
MAX_LINKS = 20
MAX_AUTHOR_CHARS = 200
# The child process: this Python, isolated from the environment and from the current folder.
CHILD = (sys.executable, "-I", "-m", "unshelve.extract.pdf_reader")
# The PDF format is from 1993. An earlier or a later year is the clock of a computer.
FIRST_YEAR = 1993

# Ligatures (U+FB00 to U+FB06) and Arabic presentation forms (U+FB50 to U+FDFF, U+FE70 to
# U+FEFC). NFKC turns each of them into its letters.
_GLYPH_FORMS = ((0xFB00, 0xFB06), (0xFB50, 0xFDFF), (0xFE70, 0xFEFC))
_FORMS = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _GLYPH_FORMS) + "]+")
_HEH = chr(0x0647)
_HEH_DOACHASHMEE = chr(0x06BE)
# Letters of Urdu (tteh, ddal, rreh, noon ghunna, yeh barree) and of Kurdish (reh with small
# v below, lam with small v, oe, yeh with small v). These languages write U+06BE as a letter
# of its own, so a text with one of them keeps it.
_URDU_OR_KURDISH = frozenset(
    map(chr, (0x0679, 0x0688, 0x0691, 0x06BA, 0x06D2, 0x0695, 0x06B5, 0x06C6, 0x06CE))
)
# Control signs that a page can hold (NUL, form feed), and the byte order mark.
_CONTROLS = dict.fromkeys(
    [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), *range(0x7F, 0xA0), 0xFEFF]
)
_SPACES = re.compile(f"[ \t{chr(0xA0)}]+")
_MANY_EMPTY_LINES = re.compile(r"\n{3,}")
_LINK = re.compile(r"https?://[^\s<>\"'()\[\]]+")
# Titles that a program writes for a document without one.
_NO_TITLES = frozenset(
    {"untitled", "untitled document", "document", "presentation", "powerpoint presentation"}
)
# "Microsoft Word - report.docx": the program, then the name of the file.
_PROGRAM = re.compile(r"Microsoft (?:Word|PowerPoint|Excel) - ", re.IGNORECASE)
_FILE_NAME = re.compile(
    r".+\.(?:pdf|docx?|pptx?|xlsx?|od[tps]|rtf|txt|tex|dvi|html?|indd|pages|key)", re.IGNORECASE
)
# Names that a computer gives to its user. They are not the name of a person.
_NO_AUTHORS = frozenset({"administrator", "admin", "user", "owner", "author", "unknown"})
_PDF_DATE = re.compile(r"(?:D:)?(\d{4})(\d{2})(\d{2})")
# A file of a repository: GitHub shows it in a page of its own ("/blob/"), and "/raw/" gives
# the bytes.
_GITHUB_FILE = re.compile(r"(https?://(?:www\.)?github\.com/[^/?#]+/[^/?#]+)/blob/", re.IGNORECASE)
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


class _Reading(BaseModel):
    """The answer of the child process."""

    error: Literal["password", "broken"] | None = None
    detail: str = ""
    pages: int = 0
    read: int = 0
    texts: list[str] = Field(default_factory=list[str])
    title: str | None = None
    author: str | None = None
    created: str | None = None
    links: list[str] = Field(default_factory=list[str])


def is_pdf_link(url: str | None) -> bool:
    """Whether the link is a web address of a file whose name ends with ".pdf"."""
    if url is None:
        return False
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return False
    return parts.path.rsplit("/", 1)[-1].lower().endswith(".pdf")


def pdf_file(ref: SourceRef) -> MediaRef | None:
    """The first PDF file of the message: a document with the type or the name of a PDF."""
    for media in ref.media:
        named = (media.file_name or "").lower().endswith(".pdf")
        if media.kind == "document" and ((media.mime or "").lower() in PDF_TYPES or named):
            return media
    return None


class PdfExtractor:
    name: ClassVar[str] = "pdf"
    version: ClassVar[int] = 1

    def match(self, ref: SourceRef) -> int:
        return FILE_SCORE if is_pdf_link(ref.url) or pdf_file(ref) is not None else 0

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        # A file that you sent comes first: it is the thing itself, and a link in its caption
        # is often only the page about it.
        file = pdf_file(ref)
        if file is not None:
            if ctx.files is None:
                raise ExtractError("no file of a message can be read here")
            # A FetchError goes up unchanged, as for a link.
            return await _from_bytes(await ctx.files.read(file, max_bytes=MAX_PDF_BYTES), None)
        if ref.url is None:
            raise ExtractError("a PDF needs a link or a file")
        return await read_pdf(ref.url, ctx)


async def read_pdf(url: str, ctx: ExtractContext) -> Extracted:
    """The text and the facts of the PDF at ``url``.

    The article extractor calls this too, for a link to a page that answers with a PDF.
    """
    # A FetchError goes up unchanged: the fetch step decides between "try again" and "keep
    # the saved text".
    address = _GITHUB_FILE.sub(r"\1/raw/", url, count=1)
    file = await ctx.fetcher.get(address, max_bytes=MAX_PDF_BYTES, accept=list(ACCEPT))
    return await _from_bytes(file.body, file.final_url)


async def _from_bytes(data: bytes, final_url: str | None) -> Extracted:
    """The text and the facts of the PDF in ``data``. ``final_url``: None for a sent file."""
    # Each PDF file has the mark "%PDF-" in its first 1,024 bytes.
    if b"%PDF-" not in data[:1024]:
        raise ExtractError("the file is not a PDF")
    return _extracted(await _read_in_child(data), final_url)


async def _read_in_child(data: bytes) -> _Reading:
    """The answer of the reader, which reads ``data`` in a process of its own."""
    child = await asyncio.create_subprocess_exec(
        *CHILD,
        str(MAX_PAGES),
        str(MAX_TEXT_CHARS),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(READ_SECONDS):
            out, err = await child.communicate(data)
    except TimeoutError:
        raise ExtractError(f"the PDF was not read in {READ_SECONDS} seconds") from None
    finally:
        # After a timeout, and when the job stops: the process must not stay behind.
        if child.returncode is None:
            child.kill()
            await child.wait()
    if child.returncode != 0:
        # A negative code is a signal: -9 when the system stopped the process for its memory.
        log.warning(
            "pdf.reader_stopped",
            code=child.returncode,
            stderr=err.decode(errors="replace")[-300:],
        )
        raise ExtractError(f"the PDF reader stopped with code {child.returncode}")
    try:
        reading = _Reading.model_validate_json(out)
    except ValidationError as error:
        raise ExtractError("the answer of the PDF reader does not fit") from error
    if reading.error == "password":
        raise ExtractError("the PDF needs a password")
    if reading.error == "broken":
        raise ExtractError(f"the PDF cannot be read ({reading.detail})")
    return reading


def _extracted(reading: _Reading, final_url: str | None) -> Extracted:
    text = clean_text("\n\n".join(reading.texts))
    if len(text) < MIN_TEXT_CHARS:
        raise ExtractError(f"the PDF has {len(text)} characters of text: it can be a scan")
    title = title_of(reading.title)
    author = author_of(reading.author)
    made = made_on(reading.created)
    facts = [f"A PDF document by {author}" if author else "A PDF document"]
    facts.append("1 page" if reading.pages == 1 else f"{reading.pages} pages")
    if made is not None:
        facts.append(f"made on {made.day} {_MONTHS[made.month - 1]} {made.year}")
    about = ", ".join(facts) + "."
    minutes = None
    if 0 < reading.read < reading.pages:
        first = "page" if reading.read == 1 else f"{reading.read} pages"
        about += f" The text is from the first {first}."
        # The pipeline counts the minutes of the text, and this text is only a part.
        minutes = math.ceil(reading_minutes(text) * reading.pages / reading.read)
    head = f"{title}\n{about}" if title else about
    return Extracted(
        kind=ItemKind.PDF,
        text=f"{head}\n\n{text}",
        title=title,
        author=author,
        site=_site(final_url) if final_url else None,
        final_url=final_url,
        published_at=made,
        extra_links=_links(reading.links, text),
        minutes=minutes,
    )


def clean_text(text: str) -> str:
    """The text of the pages without the marks of the PDF format: letters in place of glyph
    forms, one kind of line break, one space between words, and one empty line at most."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _FORMS.sub(lambda found: unicodedata.normalize("NFKC", found.group()), text)
    if _HEH_DOACHASHMEE in text and not _URDU_OR_KURDISH.intersection(text):
        text = text.replace(_HEH_DOACHASHMEE, _HEH)
    text = _SPACES.sub(" ", text.translate(_CONTROLS))
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _MANY_EMPTY_LINES.sub("\n\n", text).strip()


def title_of(raw: str | None) -> str | None:
    """The title in the facts of the file. None for the name of a file, or for a word that a
    program writes when the document has no title."""
    if raw is None:
        return None
    title = " ".join(raw.split())
    found = _PROGRAM.match(title)
    if found is not None:
        title = title[found.end() :]
    if len(title) < 3 or title.casefold() in _NO_TITLES or _FILE_NAME.fullmatch(title):
        return None
    return title


def author_of(raw: str | None) -> str | None:
    """The author in the facts of the file. None for the name that a computer gives its user."""
    if raw is None:
        return None
    author = " ".join(raw.split())
    if not author or author.casefold() in _NO_AUTHORS:
        return None
    if len(author) > MAX_AUTHOR_CHARS:
        author = author[:MAX_AUTHOR_CHARS].rsplit(" ", 1)[0] + " …"
    return author


def made_on(raw: str | None) -> datetime | None:
    """The day on which the file was made ("D:20190429173603Z"). None for a day that cannot be
    true: a PDF made before 1993, or more than one year from now."""
    found = _PDF_DATE.match(raw.strip()) if raw else None
    if found is None:
        return None
    try:
        day = datetime(int(found[1]), int(found[2]), int(found[3]), tzinfo=UTC)
    except ValueError:
        return None
    return day if FIRST_YEAR <= day.year <= datetime.now(UTC).year + 1 else None


def _links(annotations: list[str], text: str) -> list[str]:
    """The web links of the file: those of its annotations, then those in its text, each link
    one time. Only http and https: a "javascript:" link would run in the web app."""
    found: dict[str, None] = {}
    for candidate in [*annotations, *_LINK.findall(text)]:
        link = candidate.strip().rstrip(".,;:!?")
        try:
            parts = urlsplit(link)
        except ValueError:
            continue
        if parts.scheme.lower() in ("http", "https") and parts.hostname:
            found.setdefault(link, None)
    return list(found)[:MAX_LINKS]


def _site(url: str) -> str | None:
    host = urlsplit(url).hostname
    return host.removeprefix("www.") if host else None
