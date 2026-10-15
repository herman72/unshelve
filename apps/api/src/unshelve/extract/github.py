"""The GitHub extractor: the facts and the README of a repository.

The REST API of GitHub answers without a key: one request for the facts (description,
language, license, topics), one for the README. Without a key, it allows 60 requests an hour
from one address. When the API does not answer (that limit, a busy server, an answer in a new
form), the article extractor reads the page of the repository instead: it shows the same
README.

Only the page of a repository: an issue, a pull request or a file stays with the article
extractor.

A README has badges, logos and comments in HTML. They say nothing to a reader of the text, so
they go. The text holds no number that changes each day, such as the stars.
"""

import base64
import re
from datetime import datetime
from typing import ClassVar
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from unshelve.contracts.dto import Extracted, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.article import ArticleExtractor
from unshelve.ingest.canonical import canonicalize

# An extractor for one site wins over the article extractor (10).
SITE_SCORE = 50
SITE = "github.com"
# A README of 1 MB is about 1.4 MB as base64 in JSON.
MAX_ANSWER_BYTES = 3_000_000
_REPOSITORY = re.compile(r"/([a-z0-9][a-z0-9-]{0,38})/([a-z0-9._-]{1,100})/?")
# Pages of GitHub with two parts in their path that are no repository.
_NOT_AN_OWNER = frozenset(
    {
        "about", "apps", "collections", "customer-stories", "enterprise", "events", "explore",
        "features", "issues", "login", "marketplace", "new", "notifications", "organizations",
        "orgs", "pricing", "pulls", "readme", "search", "security", "settings", "site",
        "sponsors", "topics", "trending", "users",
    }
)  # fmt: skip
# Comments, pictures and empty paragraphs of HTML; pictures and empty links of Markdown.
_NOISE = (
    re.compile(r"<!--.*?-->", re.DOTALL),
    re.compile(r"</?(?:img|picture|source)\b[^>]*>", re.IGNORECASE),
    re.compile(r"!\[[^\]]*\]\([^)]*\)"),
    re.compile(r"\[\s*\]\([^)]*\)"),
    re.compile(r"<p\b[^>]*>\s*</p>", re.IGNORECASE),
)
_MANY_EMPTY_LINES = re.compile(r"\n[ \t]*(?:\n[ \t]*)+\n")
_NO_LICENSE = frozenset({"", "NOASSERTION"})


class _License(BaseModel):
    name: str = ""
    spdx_id: str | None = ""


class _Owner(BaseModel):
    login: str = ""


class _Parent(BaseModel):
    full_name: str = ""


class _Repository(BaseModel):
    full_name: str
    html_url: str = ""
    description: str | None = None
    homepage: str | None = None
    language: str | None = None
    license: _License | None = None
    topics: list[str] = Field(default_factory=list[str])
    owner: _Owner = Field(default_factory=_Owner)
    fork: bool = False
    parent: _Parent | None = None
    archived: bool = False
    created_at: datetime | None = None


class _Readme(BaseModel):
    content: str = ""
    encoding: str = ""


def repository(url: str | None) -> tuple[str, str] | None:
    """The owner and the name of the repository at ``url``, or None for another page."""
    if url is None:
        return None
    parts = urlsplit(canonicalize(url))
    found = _REPOSITORY.fullmatch(parts.path) if parts.hostname == SITE else None
    if found is None or found.group(1) in _NOT_AN_OWNER:
        return None
    return found.group(1), found.group(2)


class GitHubExtractor:
    name: ClassVar[str] = "github"
    version: ClassVar[int] = 1

    def match(self, ref: SourceRef) -> int:
        return SITE_SCORE if repository(ref.url) else 0

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        found = repository(ref.url)
        if found is None:
            raise ExtractError("not a repository on GitHub")
        owner, name = found
        api = f"https://api.github.com/repos/{owner}/{name}"
        page = f"https://github.com/{owner}/{name}"
        try:
            answer = await ctx.fetcher.get(
                api, max_bytes=MAX_ANSWER_BYTES, accept=["application/json"]
            )
            facts = _Repository.model_validate_json(answer.body)
            readme = await _readme(api, ctx)
        except (FetchError, ValueError):
            # The limit of the API, a busy server, or an answer in a new form. The page shows
            # the same README; its own failure goes up to the fetch step.
            from_page = await ArticleExtractor().extract(SourceRef(url=page), ctx)
            return from_page.model_copy(update={"kind": ItemKind.REPO})
        return _extracted(facts, readme, page)


async def _readme(api: str, ctx: ExtractContext) -> str | None:
    """The README as text, or None when the repository has none."""
    try:
        answer = await ctx.fetcher.get(
            f"{api}/readme", max_bytes=MAX_ANSWER_BYTES, accept=["application/json"]
        )
    except FetchError as error:
        if error.reason == "http_error" and error.status == 404:
            return None
        raise
    found = _Readme.model_validate_json(answer.body)
    if found.encoding != "base64":
        raise ValueError(f"a README in the encoding {found.encoding!r}")
    # The base64 text has a line break after each 60 characters; the decoder skips them.
    return base64.b64decode(found.content).decode("utf-8", errors="replace")


def _extracted(facts: _Repository, readme: str | None, page: str) -> Extracted:
    description = (facts.description or "").strip()
    title = f"{facts.full_name}: {description}" if description else facts.full_name
    about = ["A repository on GitHub"]
    if facts.language:
        about.append(f"in {facts.language}")
    if facts.license and (facts.license.spdx_id or "") not in _NO_LICENSE:
        about.append(f"under the {facts.license.name}")
    sentences = [", ".join(about) + "."]
    topics = [topic for topic in facts.topics if topic.strip()]
    if topics:
        sentences.append(f"Topics: {', '.join(topics)}.")
    if facts.fork and facts.parent and facts.parent.full_name:
        sentences.append(f"It is a fork of {facts.parent.full_name}.")
    if facts.archived:
        sentences.append("It is archived: its owner no longer changes it.")
    lines = [title, " ".join(sentences)]
    homepage = (facts.homepage or "").strip()
    if not homepage.startswith(("http://", "https://")):
        homepage = ""
    if homepage:
        lines.append(f"Homepage: {homepage}")
    text = "\n".join(lines)
    if readme is not None and (clean := _clean(readme)):
        text += f"\n\n{clean}"
    return Extracted(
        kind=ItemKind.REPO,
        text=text,
        title=title,
        author=facts.owner.login or None,
        site=SITE,
        final_url=facts.html_url or page,
        published_at=facts.created_at,
        extra_links=[homepage] if homepage else [],
    )


def _clean(readme: str) -> str:
    """The README without its comments, pictures and badges, and without many empty lines."""
    for noise in _NOISE:
        readme = noise.sub("", readme)
    lines = [line.rstrip() for line in readme.splitlines()]
    return _MANY_EMPTY_LINES.sub("\n\n", "\n".join(lines)).strip()
