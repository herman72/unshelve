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
LABEL = "label"  # the start of the data of the buttons of a verdict on a score
MAX_REASON = 300


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


# --- Your verdicts on the scores ---------------------------------------------------------

# The scale of the model that scores an item against a goal (unshelve.relevance.prompt).
SCORE_WORDS = ("No link", "Touches the goal", "Helps the goal", "Directly useful")
VERDICTS = {
    "right": "You said: the score is right.",
    "wrong": "You said: the score is wrong.",
    "skip": "Skipped.",
}
NOTHING_TO_LABEL = (
    "No score waits for your verdict now. Each new item gets scores against your goals: ask me "
    "again later with /label."
)


def label_question(item: Item, goal_title: str, score: int, reason: str) -> str:
    """The question about one score. The title, the goal and the reason each stand alone on a
    line: each can be Persian, and each is escaped."""
    lines = [
        "<b>Is this score right?</b>",
        "",
        f"<b>{html.escape(title_of(item), quote=False)}</b>",
    ]
    if item.site:
        lines.append(html.escape(item.site, quote=False))
    if item.summary:
        lines.append(html.escape(_short(item.summary, MAX_SUMMARY), quote=False))
    lines += [
        "",
        "Goal:",
        f"<b>{html.escape(_short(goal_title, MAX_TITLE), quote=False)}</b>",
        f"Score: {SCORE_WORDS[score]} ({score} of 3)",
        html.escape(_short(reason, MAX_REASON), quote=False),
    ]
    return "\n".join(lines)


def label_buttons(item: Item, goal_id: int, score: int) -> InlineKeyboardMarkup:
    """ "Right", "Wrong" and "Skip", each with the score that the question shows; and "Open"."""
    data = f"{LABEL}:{item.id}:{goal_id}:{score}"
    rows = [
        [
            InlineKeyboardButton(text="Right", callback_data=f"{data}:right"),
            InlineKeyboardButton(text="Wrong", callback_data=f"{data}:wrong"),
            InlineKeyboardButton(text="Skip", callback_data=f"{data}:skip"),
        ]
    ]
    if address := _web_address(item.canonical_url):
        rows.append([InlineKeyboardButton(text="Open", url=address)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_label(data: str) -> tuple[int, int, int, str] | None:
    """The item, the goal, the shown score and the verdict of a button, or None for data that a
    button of this version never has."""
    parts = data.split(":")
    if len(parts) != 5 or parts[0] != LABEL:
        return None
    _, item, goal, score, verdict = parts
    if not (item.isdigit() and goal.isdigit()) or score not in {"0", "1", "2", "3"}:
        return None
    if verdict not in VERDICTS:
        return None
    return int(item), int(goal), int(score), verdict


def links_only(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """The buttons without the buttons of a verdict: an answered question keeps "Open"."""
    if markup is None:
        return None
    rows = [
        [button for button in row if not (button.callback_data or "").startswith(f"{LABEL}:")]
        for row in markup.inline_keyboard
    ]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
