"""The YouTube extractor: the title, the channel, the length, the date and the description of
a video, for its card and its vectors.

It asks for the watch page in English (``hl=en``; else YouTube writes its messages in the
language of the country of the IP). The page holds the facts of the player as JSON, after
``ytInitialPlayerResponse =``. When the page has no such facts (a page that asks for consent,
a check for robots, a new form of the page), the oEmbed answer of YouTube gives the title and
the channel.

The transcript is not read: since 2025 YouTube asks for a token of origin for each transcript,
and a plain request gets an empty answer.

The text holds no number that changes each day, such as the views. A new number would make a
new text, and the card and the vectors of the item would be made again for nothing.
"""

import json
import math
import re
from datetime import datetime
from typing import ClassVar
from urllib.parse import quote

from pydantic import BaseModel, Field, ValidationError

from unshelve.contracts.dto import Extracted, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.html import decode_html
from unshelve.ingest.canonical import canonicalize

# An extractor for one site wins over the article extractor (10).
SITE_SCORE = 50
# A watch page has about 2 MB; the facts of the player are about 0.5 MB of it.
MAX_PAGE_BYTES = 6_000_000
MAX_OEMBED_BYTES = 100_000
MAX_LINKS = 20
MAX_KEYWORDS = 20
SITE = "youtube.com"
# Each address of a video has this canonical form (``ingest.canonical``).
_VIDEO = re.compile(r"https://youtube\.com/watch\?v=([A-Za-z0-9_-]{11})")
_PLAYER = re.compile(r"ytInitialPlayerResponse\s*=\s*")
_LINK = re.compile(r"https?://[^\s<>\"'()\[\]]+")
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


class _Details(BaseModel):
    title: str = ""
    author: str = ""
    length_seconds: int = Field(default=0, alias="lengthSeconds")
    description: str = Field(default="", alias="shortDescription")
    keywords: list[str] = Field(default_factory=list[str])
    live: bool = Field(default=False, alias="isLiveContent")


class _Microformat(BaseModel):
    published: datetime | None = Field(default=None, alias="publishDate")


class _Formats(BaseModel):
    player: _Microformat | None = Field(default=None, alias="playerMicroformatRenderer")


class _Playability(BaseModel):
    status: str = ""
    reason: str = ""


class _Player(BaseModel):
    playability: _Playability = Field(default_factory=_Playability, alias="playabilityStatus")
    details: _Details | None = Field(default=None, alias="videoDetails")
    formats: _Formats | None = Field(default=None, alias="microformat")


class _OEmbed(BaseModel):
    title: str
    author_name: str = ""


def video_id(url: str | None) -> str | None:
    """The id of the video at ``url``, from each of its addresses. None for another page."""
    if url is None:
        return None
    found = _VIDEO.fullmatch(canonicalize(url))
    return found.group(1) if found else None


class YouTubeExtractor:
    name: ClassVar[str] = "youtube"
    version: ClassVar[int] = 1

    def match(self, ref: SourceRef) -> int:
        return SITE_SCORE if video_id(ref.url) else 0

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        video = video_id(ref.url)
        if video is None:
            raise ExtractError("not a YouTube video")
        watch = f"https://www.youtube.com/watch?v={video}"
        # A FetchError goes up unchanged: the fetch step decides between "try again" and
        # "keep the saved text".
        page = await ctx.fetcher.get(
            f"{watch}&hl=en", max_bytes=MAX_PAGE_BYTES, accept=["text/html"]
        )
        player = _player(decode_html(page.body, page.content_type))
        if player is not None and player.details is not None and player.details.title.strip():
            return _from_player(player.details, player, watch)
        if player is not None and player.playability.status == "ERROR":
            reason = player.playability.reason or "no reason"
            raise ExtractError(f"the video is not available: {reason}")
        return await _from_oembed(watch, ctx)


def _player(html: str) -> _Player | None:
    """The facts of the player in the page, or None when the page has none that can be read."""
    found = _PLAYER.search(html)
    if found is None:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, found.end())
        return _Player.model_validate(data)
    except (ValueError, ValidationError):
        return None


def _from_player(details: _Details, player: _Player, watch: str) -> Extracted:
    title = details.title.strip()
    author = details.author.strip()
    published = (
        player.formats.player.published if player.formats and player.formats.player else None
    )
    minutes = math.ceil(details.length_seconds / 60) if details.length_seconds > 0 else None
    what = "A live stream" if details.live and minutes is None else "A video"
    facts = [f"{what} by {author} on YouTube" if author else f"{what} on YouTube"]
    if minutes is not None:
        facts.append("1 minute" if minutes == 1 else f"{minutes} minutes")
    if published is not None:
        # The day of the channel, in its own time zone, as YouTube shows it.
        facts.append(f"from {published.day} {_MONTHS[published.month - 1]} {published.year}")
    lines = [title, ", ".join(facts) + "."]
    keywords = [word.strip() for word in details.keywords if word.strip()][:MAX_KEYWORDS]
    if keywords:
        lines.append("Keywords: " + ", ".join(keywords))
    description = details.description.strip()
    text = "\n".join(lines) + (f"\n\n{description}" if description else "")
    return Extracted(
        kind=ItemKind.VIDEO,
        text=text,
        title=title,
        author=author or None,
        site=SITE,
        final_url=watch,
        published_at=published,
        extra_links=_links(description),
        minutes=minutes,
    )


async def _from_oembed(watch: str, ctx: ExtractContext) -> Extracted:
    """The title and the channel, from oEmbed: the facts that YouTube gives without its page."""
    address = f"https://www.youtube.com/oembed?url={quote(watch, safe='')}&format=json"
    try:
        answer = await ctx.fetcher.get(
            address, max_bytes=MAX_OEMBED_BYTES, accept=["application/json"]
        )
    except FetchError as error:
        if error.retryable:
            raise
        # 400 for a video that does not exist, 401 or 403 for a private one.
        detail = error.status if error.status is not None else error.reason
        raise ExtractError(f"YouTube gives no public facts about this video ({detail})") from error
    try:
        found = _OEmbed.model_validate_json(answer.body)
    except ValidationError as error:
        raise ExtractError("the oEmbed answer of YouTube does not fit") from error
    title = found.title.strip()
    author = found.author_name.strip()
    about = f"A video by {author} on YouTube." if author else "A video on YouTube."
    return Extracted(
        kind=ItemKind.VIDEO,
        text=f"{title}\n{about}" if title else about,
        title=title or None,
        author=author or None,
        site=SITE,
        final_url=watch,
    )


def _links(description: str) -> list[str]:
    """The links in a description, each one time, in order. A full stop after a link is no
    part of it."""
    found: dict[str, None] = {}
    for link in _LINK.findall(description):
        found.setdefault(link.rstrip(".,;:!?"), None)
    return list(found)[:MAX_LINKS]
