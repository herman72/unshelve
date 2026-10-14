"""The text of the messages that show items in Telegram. Pure: no database, no network.

Two rules make a card safe:

* **Escaped.** A title or a summary is text from the internet. In a message with HTML marks,
  "<b>" in a title would be a mark. Each such text goes through ``html.escape``.
* **Right to left.** Telegram gives each line the direction of its first letter. A Persian
  title next to English words on one line can show its parts in a strange order. So each text
  from the internet stands alone on its own line, and the English words are on other lines.
"""

import html
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from unshelve.db.models import Item

# Telegram refuses a message with more than 4096 characters.
MAX_MESSAGE = 4096
MAX_TITLE = 200
MAX_SUMMARY = 220
DONE = "done"  # the start of the data of the button "Done"


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _web_address(url: str | None) -> str | None:
    """The address for a button "Open", only if it is a web address."""
    if not url:
        return None
    try:
        return url if urlsplit(url).scheme in {"http", "https"} and urlsplit(url).netloc else None
    except ValueError:
        return None


def title_of(item: Item) -> str:
    return _short(item.title or item.canonical_url or "A note without a title", MAX_TITLE)


def _meta(item: Item, number: int) -> str:
    parts = [f"#{number}"]
    if item.site:
        parts.append(item.site)
    if item.est_minutes:
        parts.append(f"{item.est_minutes} min")
    if item.capture_count > 1:
        parts.append(f"saved {item.capture_count} times")
    return " · ".join(html.escape(part, quote=False) for part in parts)


def card(item: Item, number: int, *, with_summary: bool = True) -> str:
    """The lines of one item: the title alone, then the facts, then the summary alone."""
    lines = [f"<b>{html.escape(title_of(item), quote=False)}</b>", _meta(item, number)]
    if with_summary and item.summary:
        lines.append(html.escape(_short(item.summary, MAX_SUMMARY), quote=False))
    return "\n".join(lines)


def buttons(items: Sequence[Item]) -> InlineKeyboardMarkup | None:
    """One row for each item: "Open #1" when it has a web address, and "Done #1"."""
    rows: list[list[InlineKeyboardButton]] = []
    for number, item in enumerate(items, start=1):
        row: list[InlineKeyboardButton] = []
        if address := _web_address(item.canonical_url):
            row.append(InlineKeyboardButton(text=f"Open #{number}", url=address))
        row.append(InlineKeyboardButton(text=f"Done #{number}", callback_data=f"{DONE}:{item.id}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def has_button(markup: InlineKeyboardMarkup | None, data: str) -> bool:
    if markup is None:
        return False
    return any(button.callback_data == data for row in markup.inline_keyboard for button in row)


def without_button(markup: InlineKeyboardMarkup | None, data: str) -> InlineKeyboardMarkup | None:
    """The same buttons, without the one with this data. A row that is empty then goes."""
    if markup is None:
        return None
    rows = [
        [button for button in row if button.callback_data != data] for row in markup.inline_keyboard
    ]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def ago(then: datetime, now: datetime) -> str:
    days = (now - then).days
    if days < 14:
        return f"saved {days} days ago" if days != 1 else "saved 1 day ago"
    if days < 60:
        return f"saved {days // 7} weeks ago"
    return f"saved {days // 30} months ago"


def digest_message(
    recent: Sequence[Item], from_the_shelf: Item | None, *, now: datetime, days: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The text and the buttons of one digest. The items are numbered in the order of the
    buttons: first the recent ones, then the one from the shelf."""
    shown = [*recent, *([from_the_shelf] if from_the_shelf is not None else [])]

    def text(with_summary: bool) -> str:
        if recent:
            things = "thing" if len(recent) == 1 else "things"
            head = f"<b>Your digest</b>\nYou saved {len(recent)} {things} in the last {days} days."
        else:
            head = f"<b>Your digest</b>\nYou saved nothing new in the last {days} days."
        parts = [head]
        parts.extend(
            card(item, number, with_summary=with_summary)
            for number, item in enumerate(recent, start=1)
        )
        if from_the_shelf is not None:
            number = len(recent) + 1
            parts.append(
                "<b>From your shelf</b>\n"
                + card(from_the_shelf, number, with_summary=with_summary)
                + f"\n{ago(from_the_shelf.last_captured_at, now)}"
            )
        return "\n\n".join(parts)

    message = text(with_summary=True)
    if len(message) > MAX_MESSAGE:
        message = text(with_summary=False)
    return message, buttons(shown)


NOTHING_YET = "Nothing to show yet. Post a link in your channel, and ask me again with /digest."
