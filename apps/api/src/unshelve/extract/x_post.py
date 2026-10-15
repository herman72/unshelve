"""The X extractor: the text, the author, the date and the language of a post on X.

Two ways in, and then the saved text:

1. The endpoint of the embed widgets of X (``cdn.syndication.twimg.com/tweet-result``) gives
   the post as JSON: the text, the real address behind each short ``t.co`` link, the date, the
   language, a quoted post, and the photos and videos. X documents it nowhere, so it can change.
2. The official oEmbed of X (``publish.x.com/oembed``) gives the text as HTML, with the
   author and the day. Its links stay short.

The embed endpoint asks for a token, and X does not check its value today. The extractor sends
the value of the formula of the embed widget.

The text holds no number that changes each day, such as the likes.
"""

import html
import math
import re
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, Field, ValidationError
from selectolax.lexbor import LexborHTMLParser

from unshelve.contracts.dto import Extracted, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.ingest.canonical import canonicalize

# An extractor for one site wins over the article extractor (10).
SITE_SCORE = 50
SITE = "x.com"
MAX_ANSWER_BYTES = 1_000_000
MAX_LINKS = 20
# Each address of a post has this canonical form (``ingest.canonical``).
_POST = re.compile(r"https://x\.com/(?:[a-z0-9_]{1,15}|i)/status/(\d{1,25})")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LANGUAGE = re.compile(r"[a-z]{2}")
_OEMBED_DAY = re.compile(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})")
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
_MEDIA_WORDS = {"photo": "photo", "video": "video", "animated_gif": "GIF"}


class _User(BaseModel):
    name: str = ""
    screen_name: str = ""


class _Link(BaseModel):
    url: str = ""
    expanded_url: str = ""


class _Entities(BaseModel):
    urls: list[_Link] = Field(default_factory=list[_Link])
    media: list[_Link] = Field(default_factory=list[_Link])


class _Media(BaseModel):
    type: str = ""


class _TombstoneText(BaseModel):
    text: str = ""


class _Tombstone(BaseModel):
    text: _TombstoneText = Field(default_factory=_TombstoneText)


class _Post(BaseModel):
    typename: str = Field(default="", alias="__typename")
    text: str = ""
    lang: str = ""
    created_at: datetime | None = None
    user: _User = Field(default_factory=_User)
    entities: _Entities = Field(default_factory=_Entities)
    media: list[_Media] = Field(default_factory=list[_Media], alias="mediaDetails")
    quoted: "_Post | None" = Field(default=None, alias="quoted_tweet")
    # Only a mark: X cut the text of a long post, and does not send the rest here.
    note: dict[str, object] | None = Field(default=None, alias="note_tweet")
    tombstone: _Tombstone | None = None


_Post.model_rebuild()


class _OEmbed(BaseModel):
    html: str
    author_name: str = ""
    author_url: str = ""


def post_id(url: str | None) -> str | None:
    """The number of the post at ``url``, from each of its addresses. None for another page."""
    if url is None:
        return None
    found = _POST.fullmatch(canonicalize(url))
    return found.group(1) if found else None


def syndication_url(post: str) -> str:
    return f"https://cdn.syndication.twimg.com/tweet-result?id={post}&lang=en&token={_token(post)}"


def _token(post: str) -> str:
    """The formula of the embed widget: (number / 10**15 * pi) in base 36, without the zeros
    and the point."""
    value = int(post) / 1e15 * math.pi
    whole, fraction = int(value), value - int(value)
    digits = ""
    while whole:
        whole, rest = divmod(whole, 36)
        digits = _BASE36[rest] + digits
    for _ in range(12):
        fraction *= 36
        digit = int(fraction)
        digits += _BASE36[digit]
        fraction -= digit
    return digits.replace("0", "") or "a"


class XPostExtractor:
    name: ClassVar[str] = "x"
    version: ClassVar[int] = 1

    def match(self, ref: SourceRef) -> int:
        return SITE_SCORE if post_id(ref.url) else 0

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        post = post_id(ref.url)
        if ref.url is None or post is None:
            raise ExtractError("not a post on X")
        canonical = canonicalize(ref.url)
        found = await _from_embed(post, ctx)
        if found is not None:
            return _extracted(found, canonical)
        return await _from_oembed(canonical, ctx)


async def _from_embed(post: str, ctx: ExtractContext) -> _Post | None:
    """The post from the endpoint of the embed widgets, or None when it does not give one.
    Each failure here goes on to oEmbed: it is a different service of X."""
    try:
        answer = await ctx.fetcher.get(
            syndication_url(post), max_bytes=MAX_ANSWER_BYTES, accept=["application/json"]
        )
        found = _Post.model_validate_json(answer.body)
    except (FetchError, ValidationError):
        return None
    if found.typename == "TweetTombstone":
        reason = found.tombstone.text.text if found.tombstone else ""
        raise ExtractError(f"the post is not available: {reason or 'no reason'}")
    if not found.text.strip() and not found.media:
        return None
    return found


def _extracted(post: _Post, canonical: str) -> Extracted:
    parts = [f"A post by {_author(post.user)} on X{_day(post.created_at)}.", _text_of(post)]
    if post.quoted is not None and _text_of(post.quoted):
        parts.append(f"It quotes a post by {_author(post.quoted.user)}:\n{_text_of(post.quoted)}")
    if media := _media(post):
        parts.append(media)
    if post.note is not None:
        parts.append("(X gives only the start of this long post.)")
    links = [link.expanded_url for link in post.entities.urls if link.expanded_url]
    return Extracted(
        kind=ItemKind.POST,
        text="\n\n".join(part for part in parts if part),
        author=_author(post.user),
        site=SITE,
        lang=post.lang if _LANGUAGE.fullmatch(post.lang) else None,
        final_url=canonical,
        published_at=post.created_at,
        extra_links=list(dict.fromkeys(links))[:MAX_LINKS],
    )


def _text_of(post: _Post) -> str:
    """The text of a post with each short link replaced by its real address. The short link
    of a photo or a video goes: the media are counted in a sentence of their own."""
    text = html.unescape(post.text)
    for link in post.entities.urls:
        if link.url and link.expanded_url:
            text = text.replace(link.url, link.expanded_url)
    for link in post.entities.media:
        if link.url:
            text = text.replace(link.url, "")
    return text.strip()


def _author(user: _User) -> str:
    name, handle = user.name.strip(), user.screen_name.strip()
    if name and handle:
        return f"{name} (@{handle})"
    return name or (f"@{handle}" if handle else "an unknown author")


def _day(when: datetime | None) -> str:
    if when is None:
        return ""
    day = when.astimezone(UTC)
    return f", from {day.day} {_MONTHS[day.month - 1]} {day.year}"


def _media(post: _Post) -> str:
    counts: dict[str, int] = {}
    for item in post.media:
        word = _MEDIA_WORDS.get(item.type)
        if word is not None:
            counts[word] = counts.get(word, 0) + 1
    if not counts:
        return ""
    named = [f"{count} {word}{'s' if count > 1 else ''}" for word, count in counts.items()]
    return f"It has {' and '.join(named)}."


async def _from_oembed(canonical: str, ctx: ExtractContext) -> Extracted:
    """The text, the author and the day from the official oEmbed of X."""
    address = (
        f"https://publish.x.com/oembed?url={quote(canonical, safe='')}&omit_script=true&dnt=true"
    )
    try:
        answer = await ctx.fetcher.get(
            address, max_bytes=MAX_ANSWER_BYTES, accept=["application/json"]
        )
    except FetchError as error:
        if error.retryable:
            raise
        detail = error.status if error.status is not None else error.reason
        raise ExtractError(f"X gives no public facts about this post ({detail})") from error
    try:
        found = _OEmbed.model_validate_json(answer.body)
    except ValidationError as error:
        raise ExtractError("the oEmbed answer of X does not fit") from error
    tree = LexborHTMLParser(_BR.sub("\n", found.html))
    paragraph = tree.css_first("blockquote p")
    text = paragraph.text().strip() if paragraph is not None else ""
    if not text:
        raise ExtractError("the oEmbed answer of X has no text")
    lang = (paragraph.attributes.get("lang") or "") if paragraph is not None else ""
    dates = [node.text().strip() for node in tree.css("blockquote > a")]
    when = _oembed_day(dates[-1]) if dates else None
    handle = urlsplit(found.author_url).path.strip("/")
    user = _User(name=found.author_name, screen_name=handle if "/" not in handle else "")
    return Extracted(
        kind=ItemKind.POST,
        text=f"A post by {_author(user)} on X{_day(when)}.\n\n{text}",
        author=_author(user),
        site=SITE,
        lang=lang if _LANGUAGE.fullmatch(lang) else None,
        final_url=canonical,
        published_at=when,
    )


def _oembed_day(text: str) -> datetime | None:
    """The day of the post in oEmbed, such as "September 30, 2026", at midnight in UTC. The
    names of the months are in the code: ``strptime`` would read them in the language of the
    computer."""
    found = _OEMBED_DAY.fullmatch(text)
    if found is None or found.group(1) not in _MONTHS:
        return None
    month = _MONTHS.index(found.group(1)) + 1
    try:
        return datetime(int(found.group(3)), month, int(found.group(2)), tzinfo=UTC)
    except ValueError:
        return None
