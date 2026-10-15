"""The Telegram extractor: a forwarded message, and a link to a public post.

Two sources:

1. A message that you forwarded from a channel, a group or a person. Its text is in the saved
   message already; the origin of the forward gives the name of the source and the date of the
   first post. No request is needed.
2. A link to a post of a public channel (``t.me/<channel>/<post>``). The embed page of Telegram
   (``?embed=1&mode=tme``) shows the post as HTML: the channel, the date, the text, a preview of
   a link, the photos and the video.

A forwarded message with a link is left to the extractor of that link: the page is the thing
that you saved. The text holds no number of views: it changes each day.
"""

import re
from datetime import datetime
from typing import ClassVar
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from unshelve.contracts.dto import Extracted, ForwardOrigin, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import ExtractError
from unshelve.contracts.protocols import ExtractContext
from unshelve.ingest.canonical import canonicalize

# A link to a post wins over the article extractor (10). A forwarded message wins over the
# saved text (1), which would call it a note.
SITE_SCORE = 50
FORWARD_SCORE = 20
SITE = "t.me"
MAX_PAGE_BYTES = 2_000_000
MAX_LINKS = 20
# Each address of a public post has this canonical form (``ingest.canonical``).
_POST = re.compile(r"/([a-z][a-z0-9_]{3,31})/(\d{1,12})")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_MANY_EMPTY_LINES = re.compile(r"\n{3,}")
_PEOPLE = frozenset({"user", "hidden_user"})
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def post_of(url: str | None) -> tuple[str, str] | None:
    """The channel and the number of the public post at ``url``, or None for another page."""
    if url is None:
        return None
    parts = urlsplit(canonicalize(url))
    found = _POST.fullmatch(parts.path) if parts.hostname == SITE else None
    return (found.group(1), found.group(2)) if found else None


class TelegramExtractor:
    name: ClassVar[str] = "telegram"
    version: ClassVar[int] = 1

    def match(self, ref: SourceRef) -> int:
        if post_of(ref.url):
            return SITE_SCORE
        if ref.url is None and ref.forward_origin is not None and ref.text.strip():
            return FORWARD_SCORE
        return 0

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        found = post_of(ref.url)
        if found is not None:
            return await _from_embed(*found, ctx)
        if ref.forward_origin is not None and ref.text.strip():
            return from_forward(ref.forward_origin, ref.text)
        raise ExtractError("neither a forwarded message nor a link to a public post")


def from_forward(origin: ForwardOrigin, text: str) -> Extracted:
    """A forwarded message: its source and the date of the first post, then its text. The
    image extractor uses it too, for a forwarded post with a photo."""
    name = (origin.name or "").strip()
    if not name:
        who = "A forwarded message"
    elif origin.type in _PEOPLE:
        who = f"A message by {name}"
    else:
        who = f"A post by {name}"
    return Extracted(
        kind=ItemKind.POST,
        text=f"{who} on Telegram{_day(origin.date)}.\n\n{text.strip()}",
        author=name or None,
        site=SITE,
        published_at=origin.date,
    )


async def _from_embed(channel: str, number: str, ctx: ExtractContext) -> Extracted:
    post = f"https://t.me/{channel}/{number}"
    # A FetchError goes up unchanged: the fetch step decides between "try again" and "keep
    # the saved text".
    page = await ctx.fetcher.get(
        f"{post}?embed=1&mode=tme", max_bytes=MAX_PAGE_BYTES, accept=["text/html"]
    )
    tree = LexborHTMLParser(_BR.sub("\n", page.body.decode("utf-8", errors="replace")))
    error = tree.css_first(".tgme_widget_message_error")
    if error is not None:
        raise ExtractError(f"the post is not available: {error.text().strip()}")
    if tree.css_first(".tgme_widget_message") is None:
        raise ExtractError("the page has no post")
    owner = _text(tree.css_first(".tgme_widget_message_owner_name"))
    author = f"{owner} (@{channel})" if owner else f"@{channel}"
    time = tree.css_first(".tgme_widget_message_date time")
    when = _moment(time.attributes.get("datetime") if time is not None else None)
    body = tree.css_first(".tgme_widget_message_text")
    parts = [f"A post by {author} on Telegram{_day(when)}.", _text(body)]
    links = [
        href
        for anchor in (body.css("a") if body is not None else [])
        if (href := anchor.attributes.get("href") or "").startswith(("http://", "https://"))
    ]
    preview = tree.css_first(".tgme_widget_message_link_preview")
    if preview is not None:
        parts.append(_preview(preview))
        href = preview.attributes.get("href") or ""
        if href.startswith(("http://", "https://")):
            links.append(href)
    parts.append(_media(tree))
    return Extracted(
        kind=ItemKind.POST,
        text="\n\n".join(part for part in parts if part),
        author=author,
        site=SITE,
        final_url=post,
        published_at=when,
        extra_links=list(dict.fromkeys(links))[:MAX_LINKS],
    )


def _preview(preview: LexborNode) -> str:
    """The preview of a link, in one sentence: its title, its site and its description."""
    title = _text(preview.css_first(".link_preview_title"))
    site = _text(preview.css_first(".link_preview_site_name"))
    description = _text(preview.css_first(".link_preview_description"))
    named = f'"{title}"' if title else "a page"
    if site:
        named += f" ({site})"
    return f"It links to {named}: {description}" if description else f"It links to {named}."


def _media(tree: LexborHTMLParser) -> str:
    counts = {
        "photo": len(tree.css(".tgme_widget_message_photo_wrap")),
        "video": len(tree.css(".tgme_widget_message_video_player")),
    }
    named = [f"{count} {word}{'s' if count > 1 else ''}" for word, count in counts.items() if count]
    return f"It has {' and '.join(named)}." if named else ""


def _text(node: LexborNode | None) -> str:
    """The text of a node: its lines without spaces around them, and at most one empty line."""
    if node is None:
        return ""
    lines = [line.strip() for line in node.text().replace("\xa0", " ").splitlines()]
    return _MANY_EMPTY_LINES.sub("\n\n", "\n".join(lines)).strip()


def _day(when: datetime | None) -> str:
    if when is None:
        return ""
    return f", from {when.day} {_MONTHS[when.month - 1]} {when.year}"


def _moment(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None
