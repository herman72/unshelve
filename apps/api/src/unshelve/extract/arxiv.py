"""The arXiv extractor: the title, the authors, the abstract and the facts of a paper.

The API of arXiv (``export.arxiv.org/api/query``) answers with Atom, a form of XML: one entry
for each paper. Its terms ask for one request every three seconds at most, so the fetcher
paces this host for three seconds (``net.fetcher.SLOW_HOSTS``).

When the API does not answer, or does not know the paper, the article extractor reads the page
of the abstract. A paper that does not exist ends with the 404 of that page.

The XML is read by the standard library. Its parser, expat, refuses the "billion laughs" and
the "quadratic blowup" from version 2.7.2 on (``MIN_EXPAT``), and by default it opens no local
file and no network connection. With an older expat, the extractor does not parse the XML: it
reads the page.

The text holds the date of the first version: a new version alone makes no new card.
"""

import pyexpat
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar
from urllib.parse import quote, urlsplit

from unshelve.contracts.dto import Extracted, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.article import ArticleExtractor
from unshelve.ingest.canonical import canonicalize

# An extractor for one site wins over the article extractor (10).
SITE_SCORE = 50
SITE = "arxiv.org"
MAX_ANSWER_BYTES = 2_000_000
# A page of a paper has about 660 words; at 220 words a minute that is 3 minutes.
MINUTES_A_PAGE = 3
NAMES_IN_THE_TEXT = 30
NAMES_AS_AUTHOR = 3
MAX_LINKS = 20
# The first version of expat that the Python documentation calls safe from entity expansion.
MIN_EXPAT = (2, 7, 2)
_SAFE_PARSER = pyexpat.version_info >= MIN_EXPAT
# Each address of a paper has this canonical form (``ingest.canonical``): the new number
# (2607.16100) or the old form (hep-th/9901001), without a version.
_PAPER = re.compile(r"/abs/(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})")
_PAGES = re.compile(r"(\d{1,4})\s*pages", re.IGNORECASE)
_LINK = re.compile(r"https?://[^\s<>\"'()\[\]]+")
_ATOM = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


@dataclass(frozen=True)
class _Paper:
    title: str
    authors: list[str]
    abstract: str
    published: datetime | None
    categories: list[str]
    comment: str
    journal: str
    doi: str


def paper_id(url: str | None) -> str | None:
    """The identifier of the paper at ``url``, from each of its addresses. None for another page."""
    if url is None:
        return None
    parts = urlsplit(canonicalize(url))
    found = _PAPER.fullmatch(parts.path) if parts.hostname == SITE else None
    return found.group(1) if found else None


class ArxivExtractor:
    name: ClassVar[str] = "arxiv"
    version: ClassVar[int] = 1

    def match(self, ref: SourceRef) -> int:
        return SITE_SCORE if paper_id(ref.url) else 0

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        paper = paper_id(ref.url)
        if paper is None:
            raise ExtractError("not a paper on arXiv")
        page = f"https://arxiv.org/abs/{paper}"
        api = f"https://export.arxiv.org/api/query?id_list={quote(paper, safe='/')}"
        found: _Paper | None = None
        if _SAFE_PARSER:
            try:
                answer = await ctx.fetcher.get(
                    api, max_bytes=MAX_ANSWER_BYTES, accept=["application/atom+xml"]
                )
                found = _read(answer.body)
            except FetchError:
                found = None
        if found is None:
            # The API is busy, gives no Atom, or does not know the paper. The page of the
            # abstract has the same abstract; its own failure goes up to the fetch step.
            from_page = await ArticleExtractor().extract(SourceRef(url=page), ctx)
            return from_page.model_copy(update={"kind": ItemKind.PAPER})
        return _extracted(found, paper, page)


def _read(body: bytes) -> _Paper | None:
    """The paper in an answer of the API, or None when the answer has no paper."""
    try:
        # expat 2.7.2 or newer refuses the "billion laughs"; it opens no file and no network.
        feed = ET.fromstring(body)  # noqa: S314
    except ET.ParseError:
        return None
    entry = feed.find("atom:entry", _ATOM)
    if entry is None or "/api/errors" in entry.findtext("atom:id", "", _ATOM):
        return None
    title = _one_line(entry.findtext("atom:title", "", _ATOM))
    if not title:
        return None
    published = entry.findtext("atom:published", "", _ATOM)
    primary = entry.find("arxiv:primary_category", _ATOM)
    categories = [primary.get("term", "")] if primary is not None else []
    categories += [node.get("term", "") for node in entry.findall("atom:category", _ATOM)]
    return _Paper(
        title=title,
        authors=[
            name
            for node in entry.findall("atom:author", _ATOM)
            if (name := _one_line(node.findtext("atom:name", "", _ATOM)))
        ],
        abstract=_one_line(entry.findtext("atom:summary", "", _ATOM)),
        published=_moment(published),
        categories=list(dict.fromkeys(term for term in categories if term)),
        comment=_one_line(entry.findtext("arxiv:comment", "", _ATOM)),
        journal=_one_line(entry.findtext("arxiv:journal_ref", "", _ATOM)),
        doi=_one_line(entry.findtext("arxiv:doi", "", _ATOM)),
    )


def _extracted(paper: _Paper, identifier: str, page: str) -> Extracted:
    facts = [f"A paper on arXiv by {_names(paper.authors, NAMES_IN_THE_TEXT, ', and')}"]
    if paper.published is not None:
        day = paper.published
        facts.append(f"from {day.day} {_MONTHS[day.month - 1]} {day.year}")
    sentences = [", ".join(facts) + "."]
    if paper.categories:
        sentences.append(f"Categories: {', '.join(paper.categories)}.")
    lines = [paper.title, " ".join(sentences)]
    if paper.comment:
        lines.append(f"Comments: {paper.comment}")
    if paper.journal:
        lines.append(f"Journal: {paper.journal}")
    text = "\n".join(lines) + (f"\n\n{paper.abstract}" if paper.abstract else "")
    links = [f"https://arxiv.org/pdf/{identifier}"]
    if paper.doi:
        links.append(f"https://doi.org/{paper.doi}")
    links += [link.rstrip(".,;:!?") for link in _LINK.findall(f"{paper.comment} {paper.abstract}")]
    pages = _PAGES.search(paper.comment)
    return Extracted(
        kind=ItemKind.PAPER,
        text=text,
        title=paper.title,
        author=_names(paper.authors, NAMES_AS_AUTHOR, " and") if paper.authors else None,
        site=SITE,
        final_url=page,
        published_at=paper.published,
        extra_links=list(dict.fromkeys(links))[:MAX_LINKS],
        minutes=int(pages.group(1)) * MINUTES_A_PAGE if pages else None,
    )


def _names(authors: list[str], most: int, joiner: str) -> str:
    """The first ``most`` names, and how many more there are."""
    if not authors:
        return "unknown authors"
    shown = ", ".join(authors[:most])
    more = len(authors) - most
    return f"{shown}{joiner} {more} more" if more > 0 else shown


def _one_line(text: str | None) -> str:
    """The API breaks titles and abstracts into lines; a text is one line here."""
    return " ".join((text or "").split())


def _moment(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
