"""Read the HTML export of a Telegram chat: the folder that "Export chat history" makes.

Two apps write this export, and their pages are almost the same (docs/adr/0009):

* Telegram Desktop. In the Mac App Store its name is Telegram Lite.
* Telegram for macOS.

This module is pure: it reads the files of an export and gives messages. It knows nothing
about the database.

The format has no documentation. What this reader knows comes from two real exports and from
the code that writes the export in Telegram Desktop (``export_output_html.cpp``). So the
reader is strict about the things that Unshelve depends on: a message without an id, or with
a date that it cannot read, stops the import with a clear error. A post that it cannot read
correctly is counted and left out. That is better than a wrong text: a post that was left
out can be imported later, and a stored capture is never changed.
"""

import re
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser, LexborNode

from unshelve.contracts.dto import MediaKind
from unshelve.contracts.errors import UnshelveError


class ExportFormatError(UnshelveError):
    """The export has a structure that this reader does not know."""


@dataclass(frozen=True, slots=True)
class ExportedFile:
    """A file of a message. ``path`` is inside the export folder, or None when the export has
    only the name of the file (the box for this kind of file was not ticked).

    ``path`` is text from the page. The code that opens the file must first make sure that
    the path stays inside the folder of the export.
    """

    kind: MediaKind
    path: str | None
    name: str | None


@dataclass(frozen=True, slots=True)
class ExportedMessage:
    message_id: int
    # The clock time of the computer that made the export. It has no time zone: the export
    # of one app gives none, and the export of the other gave a wrong one (docs/adr/0009).
    sent_at: datetime
    text: str
    urls: tuple[str, ...]
    files: tuple[ExportedFile, ...]
    forwarded_from: str | None
    forwarded_at: datetime | None


@dataclass(slots=True)
class ExportedChat:
    title: str
    messages: list[ExportedMessage] = field(default_factory=list[ExportedMessage])
    # What was read and not turned into a message, by kind: for the summary of an import.
    left_out: Counter[str] = field(default_factory=Counter[str])


# The class of the element that shows a file, and the kind of file that it means.
_FILE_KINDS: dict[str, MediaKind] = {
    # A file that is in the export: a link to the file, with a small picture.
    "photo_wrap": "photo",
    "video_file_wrap": "video",
    "animated_wrap": "animation",
    "sticker_wrap": "sticker",
    # A box with a title. Telegram Desktop writes it for a document, a voice message and an
    # audio file, and for each file that it did not download.
    "media_photo": "photo",
    "media_video": "video",
    "media_file": "document",
    "media_voice_message": "voice",
    "media_audio_file": "audio",
}
# A sticker and an animation that were not downloaded use the box of a photo and of a video.
# Then the title of the box tells them apart.
_KIND_OF_TITLE: dict[str, MediaKind] = {"Sticker": "sticker", "Animation": "animation"}
# The title of the box of a document is the name of the file, or this word when the file has
# no name. The title of each other box is a label such as "Photo".
_DOCUMENT_WITHOUT_NAME = "File"
# Content that is not a file and not text: it has no place in an item yet.
_OTHER_CONTENT = {
    "media_poll": "poll",
    "media_location": "location",
    "media_live_location": "location",
    "media_venue": "location",
    "media_contact": "contact",
    "media_call": "call",
    "media_game": "game",
    "media_invoice": "invoice",
}
UNKNOWN_CONTENT = "content of a kind that is not known"
RICH_MESSAGE = "rich message"
NO_CONTENT = "message without text and without a file"
SERVICE_MESSAGE = "service message"
_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
# "13.10.2026 22:40:37 UTC+02:00": Telegram Desktop. The zone text is not used.
_NUMERIC_DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2})\b")
# "13 October 2026, 22:40:37": Telegram for macOS.
_WORD_DATE = re.compile(r"^(\d{1,2}) ([A-Za-z]+) (\d{4}), (\d{2}):(\d{2}):(\d{2})$")
_PAGE_NAME = re.compile(r"^messages(\d*)\.html$")
_WEB_ADDRESS = re.compile(r"^https?://", re.IGNORECASE)


def parse_clock_time(text: str) -> datetime:
    """The date and time of a message, as the export writes it. No time zone."""
    text = text.strip()
    with suppress(ValueError):  # 31 February, or the hour 25
        if found := _NUMERIC_DATE.match(text):
            day, month, year, hour, minute, second = (int(part) for part in found.groups())
            return datetime(year, month, day, hour, minute, second)  # noqa: DTZ001 - see sent_at
        if (found := _WORD_DATE.match(text)) and (month := _MONTHS.get(found.group(2).lower())):
            day, year = int(found.group(1)), int(found.group(3))
            hour, minute, second = (int(found.group(n)) for n in (4, 5, 6))
            return datetime(year, month, day, hour, minute, second)  # noqa: DTZ001
    msg = f"a date in a form that the importer does not know: {text!r}"
    raise ExportFormatError(msg)


def _classes(node: LexborNode) -> set[str]:
    return set((node.attributes.get("class") or "").split())


def _plain_text(node: LexborNode) -> str:
    """The text of an element as the person wrote it.

    Telegram writes each line break of a post as ``<br>``, and the marks of bold text, links
    and code as elements around the text. The page has a line break of its own only before
    and after the text, and these two are removed.
    """
    parts: list[str] = []

    def walk(element: LexborNode) -> None:
        for child in element.iter(include_text=True):
            if child.tag == "-text":
                parts.append(child.text_content or "")
            elif child.tag == "br":
                parts.append("\n")
            else:
                walk(child)

    walk(node)
    return "".join(parts).strip()


def _is_mention(address: str, label: str) -> bool:
    # Telegram Desktop writes @name as <a href="https://t.me/name">@name</a>.
    return label.startswith("@") and address.rstrip("/").rsplit("/", 1)[-1] == label[1:]


def _urls(text_node: LexborNode) -> tuple[str, ...]:
    """The web links of a message, in their order, each one time.

    The export writes a mention such as @name as a link. The bot does not count a mention as
    a link, so the importer does not count it too.
    """
    found: list[str] = []
    for link in text_node.css("a"):
        address = link.attributes.get("href") or ""
        if _WEB_ADDRESS.match(address) and not _is_mention(address, link.text(strip=True)):
            found.append(address)
    return tuple(dict.fromkeys(found))


def _file(element: LexborNode, classes: set[str]) -> ExportedFile | None:
    box = next((name for name in classes if name in _FILE_KINDS), None)
    if box is None:
        return None
    title_node = element.css_first("div.title")
    title = title_node.text(strip=True) if title_node else None
    # The box of a file that is in the export is a link to the file.
    path = (element.attributes.get("href") or None) if element.tag == "a" else None
    name = Path(path).name if path else None
    if box == "media_file" and title and title != _DOCUMENT_WITHOUT_NAME:
        name = title
    return ExportedFile(
        kind=_KIND_OF_TITLE.get(title or "", _FILE_KINDS[box]), path=path, name=name
    )


def _content(message: LexborNode) -> tuple[tuple[ExportedFile, ...], list[str]]:
    """The files of a message, and the names of the other content that it holds."""
    files: list[ExportedFile] = []
    others: list[str] = []
    for wrap in message.css("div.media_wrap"):
        known = False
        for element in wrap.iter():
            classes = _classes(element)
            if (file := _file(element, classes)) is not None:
                files.append(file)
                known = True
            elif other := next((_OTHER_CONTENT[c] for c in classes if c in _OTHER_CONTENT), None):
                others.append(other)
                known = True
            elif "webpage_preview" in classes:
                # The preview of a link is not content: the link is in the text.
                known = True
        if not known:
            others.append(UNKNOWN_CONTENT)
    return tuple(files), others


def _forwarded(
    message: LexborNode, source_before: str | None
) -> tuple[str | None, datetime | None]:
    """The name of the source of a forwarded post, and the date of the first post if known.

    ``source_before`` is the source of the post before this one on the page.
    """
    # Telegram Desktop: <div class="forwarded body"><div class="from_name">Name <span ...>
    block = message.css_first("div.forwarded.body")
    if block is not None:
        name_node = block.css_first("div.from_name")
        if name_node is None:
            # Posts of one person that were forwarded together: Telegram Desktop writes the
            # name on the first post only. The posts after it have the same source.
            if source_before is None:
                msg = "a forwarded post without the name of its source"
                raise ExportFormatError(msg)
            return source_before, None
        date_node = name_node.css_first("span.date")
        when = parse_clock_time(date_node.attributes.get("title") or "") if date_node else None
        name = "".join(
            child.text_content or ""
            for child in name_node.iter(include_text=True)
            if child.tag == "-text"
        )
        return name.strip() or None, when
    # Telegram for macOS: <div class="forwarded_from details">Forwarded from Name</div>
    line = message.css_first("div.forwarded_from")
    if line is not None:
        name = line.text(strip=True).removeprefix("Forwarded from").strip()
        return name or None, None
    return None, None


def _message_id(node: LexborNode, page: Path) -> int:
    identifier = node.attributes.get("id") or ""
    if not identifier.startswith("message") or not identifier[7:].lstrip("-").isdigit():
        msg = f"{page.name}: a message without a message id (id={identifier!r})"
        raise ExportFormatError(msg)
    return int(identifier[7:])


def _post(
    node: LexborNode,
    message_id: int,
    sent_at: datetime,
    forwarded: tuple[str | None, datetime | None],
    left_out: Counter[str],
) -> ExportedMessage | None:
    """The content of one post, or None for a post that is counted and left out."""
    text_node = node.css_first("div.text")
    if text_node is not None and "rich_message" in _classes(text_node):
        # A post with headings, lists and tables. Its text has a structure that this reader
        # does not know, and no real export with such a post was seen.
        left_out[RICH_MESSAGE] += 1
        return None
    files, others = _content(node)
    left_out.update(others)
    exported = ExportedMessage(
        message_id=message_id,
        sent_at=sent_at,
        text=_plain_text(text_node) if text_node else "",
        urls=_urls(text_node) if text_node else (),
        files=files,
        forwarded_from=forwarded[0],
        forwarded_at=forwarded[1],
    )
    if exported.text or exported.files:
        return exported
    if not others:
        left_out[NO_CONTENT] += 1
    return None


def _message(
    node: LexborNode, page: Path, source_before: str | None, left_out: Counter[str]
) -> tuple[ExportedMessage | None, str | None]:
    """Read one post. Returns the message, or None for a post that is left out, and the
    source of the post if it was forwarded: the next post can need it."""
    message_id = _message_id(node, page)
    date_node = node.css_first("div.date")
    try:
        if date_node is None or not date_node.attributes.get("title"):
            msg = "no date"
            raise ExportFormatError(msg)
        sent_at = parse_clock_time(date_node.attributes.get("title") or "")
        forwarded = _forwarded(node, source_before)
    except ExportFormatError as error:
        msg = f"{page.name}: message {message_id} has {error}"
        raise ExportFormatError(msg) from error
    return _post(node, message_id, sent_at, forwarded, left_out), forwarded[0]


def export_pages(folder: Path) -> list[Path]:
    """The pages of an export, in their order: messages.html, messages2.html, and so on."""
    pages = [
        (int(found.group(1) or 1), path)
        for path in folder.iterdir()
        if (found := _PAGE_NAME.match(path.name))
    ]
    if not pages:
        msg = f"{folder} has no file messages.html: it is not an HTML export of a chat"
        raise ExportFormatError(msg)
    return [path for _, path in sorted(pages)]


def read_export(folder: Path) -> ExportedChat:
    """Read every page of an export. The messages come back in the order of the export:
    the oldest post first."""
    chat: ExportedChat | None = None
    for page in export_pages(folder):
        tree = LexborHTMLParser(page.read_text(encoding="utf-8"))
        if chat is None:
            # The title is in the header of the first page. The two apps give it two classes.
            title = tree.css_first(".page_header .text.bold, .page_header .name.bold")
            if title is None:
                msg = f"{page.name}: the page has no chat title"
                raise ExportFormatError(msg)
            chat = ExportedChat(title=title.text(strip=True))
        # Telegram Desktop starts each page as if no post came before it.
        source_before: str | None = None
        for node in tree.css("div.message"):
            classes = _classes(node)
            if "service" in classes:
                source_before = None
                # A line with a date is layout only. Telegram Desktop gives it a negative id
                # ("message-1"), and Telegram for macOS gives it no id.
                if (node.attributes.get("id") or "").removeprefix("message").isdigit():
                    # "Channel created", "pinned a message": a message, and not a post.
                    chat.left_out[SERVICE_MESSAGE] += 1
            elif "default" in classes:
                message, source_before = _message(node, page, source_before, chat.left_out)
                if message is not None:
                    chat.messages.append(message)
            else:
                kind_of_message = " ".join(sorted(classes))
                msg = f"{page.name}: a message of a kind that is not known: {kind_of_message}"
                raise ExportFormatError(msg)
    if chat is None:  # export_pages gives one page or more
        msg = f"{folder} has no pages"
        raise ExportFormatError(msg)
    return chat
