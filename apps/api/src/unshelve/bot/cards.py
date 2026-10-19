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

from unshelve.contracts.enums import GoalLabel
from unshelve.db.models import Item
from unshelve.resurface.plan import PlannedDigest
from unshelve.resurface.selection import Pick, Reason

# Telegram refuses a message with more than 4096 characters.
MAX_MESSAGE = 4096
MAX_TITLE = 200
MAX_SUMMARY = 220
# The start of the data of a button. Telegram gives each button at most 64 bytes of data.
DONE = "done"  # "Done": the item is read
LABEL = "label"  # a verdict on a score, asked by /label: the next question follows
DIGEST_LABEL = "dlabel"  # a verdict on a score of a digest: no question follows
STALE = "stale"  # the stale prompt: archive or keep
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
    if days == 0:
        return "saved today"
    if days < 14:
        return f"saved {days} days ago" if days != 1 else "saved 1 day ago"
    if days < 60:
        return f"saved {days // 7} weeks ago"
    return f"saved {days // 30} months ago"


VERDICT_SAID = {GoalLabel.CONFIRMED: " You said: right.", GoalLabel.REJECTED: " You said: wrong."}


def _reason(item: Item, pick: Pick, planned: PlannedDigest, now: datetime) -> list[str]:
    """The lines that say why the digest brings the item back. A goal title stands alone on
    its line: you can write it in Persian."""
    shown = planned.shown_score(pick)
    line = ""
    if shown is not None:
        _, score, label = shown
        line = f"Score: {score} of 3.{VERDICT_SAID[label] if label else ''}"
    if pick.reason is Reason.FOCUS_GOAL:
        return [f"For your focus goal. {line}".rstrip()]
    if pick.reason is Reason.OTHER_GOAL and pick.goal_id is not None:
        title = _short(planned.goal_titles[pick.goal_id], MAX_TITLE)
        lines = ["For your goal:", f"<b>{html.escape(title, quote=False)}</b>"]
        return [*lines, line] if line else lines
    if pick.reason is Reason.SAVED_AGAIN:
        return [f"You saved it {item.capture_count} times."]
    if pick.reason is Reason.RECENT:
        return [f"New in your inbox: {ago(item.last_captured_at, now)}."]
    lines = [f"A random pick from your inbox: {ago(item.last_captured_at, now)}."]
    if line:
        lines.append(f"For your focus goal. {line}")
    return lines


def _asks_for_a_verdict(pick: Pick, planned: PlannedDigest) -> bool:
    """A digest asks about a score that it shows and that has no verdict yet."""
    shown = planned.shown_score(pick)
    return shown is not None and shown[2] is None


def digest_buttons(planned: PlannedDigest) -> InlineKeyboardMarkup | None:
    """For each item: "Open" and "Done"; under each score that the digest shows and that has
    no verdict yet, "Right" and "Wrong" too. Each button names the number of its item."""
    rows: list[list[InlineKeyboardButton]] = []
    for number, (item, pick) in enumerate(planned.picks, start=1):
        row: list[InlineKeyboardButton] = []
        if address := _web_address(item.canonical_url):
            row.append(InlineKeyboardButton(text=f"Open #{number}", url=address))
        row.append(InlineKeyboardButton(text=f"Done #{number}", callback_data=f"{DONE}:{item.id}"))
        rows.append(row)
        shown = planned.shown_score(pick)
        if shown is not None and shown[2] is None:
            goal_id, score, _ = shown
            data = f"{DIGEST_LABEL}:{item.id}:{goal_id}:{score}"
            rows.append(
                [
                    InlineKeyboardButton(text=f"Right #{number}", callback_data=f"{data}:right"),
                    InlineKeyboardButton(text=f"Wrong #{number}", callback_data=f"{data}:wrong"),
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def digest_message(
    planned: PlannedDigest, *, now: datetime
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The text and the buttons of a digest, in the order of the digest: the focus goal first,
    the random pick last. Each item has the lines of its reason under its card."""
    count = len(planned.picks)
    things = "thing" if count == 1 else "things"
    head = [f"<b>Your digest</b>\n{count} {things}, about {planned.total_minutes} minutes."]
    if planned.goal is not None:
        title = _short(planned.goal.title, MAX_TITLE)
        head.append(f"Your focus goal:\n<b>{html.escape(title, quote=False)}</b>")
    asks = any(_asks_for_a_verdict(pick, planned) for _, pick in planned.picks)

    def text(with_summary: bool) -> str:
        parts = [*head]
        for number, (item, pick) in enumerate(planned.picks, start=1):
            lines = [card(item, number, with_summary=with_summary)]
            lines += _reason(item, pick, planned, now)
            parts.append("\n".join(lines))
        if asks:
            parts.append("<i>Right or Wrong: is the score of the goal right?</i>")
        return "\n\n".join(parts)

    message = text(with_summary=True)
    if len(message) > MAX_MESSAGE:
        message = text(with_summary=False)
    return message, digest_buttons(planned)


NOTHING_YET = (
    "Nothing to bring back now. A thing comes back when it helps a goal, when you saved it "
    "again, or when it is new. A digest does not show a thing again within three weeks."
)


# --- The stale prompt --------------------------------------------------------------------


def stale_message(items: Sequence[Item], *, now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    """The question about old things of the inbox: archive them, or keep them? "Open",
    "Archive" and "Keep" for each one. "Archive" moves a thing out of the inbox; nothing is
    deleted, and the web app can bring it back."""
    head = (
        "<b>Still in your inbox</b>\n"
        f"{'This thing waits' if len(items) == 1 else 'These things wait'} since 30 days or "
        "more. Archive, or keep?"
    )
    parts = [head]
    rows: list[list[InlineKeyboardButton]] = []
    for number, item in enumerate(items, start=1):
        parts.append(f"{card(item, number, with_summary=False)}\n{ago(item.last_captured_at, now)}")
        row: list[InlineKeyboardButton] = []
        if address := _web_address(item.canonical_url):
            row.append(InlineKeyboardButton(text=f"Open #{number}", url=address))
        row += [
            InlineKeyboardButton(
                text=f"Archive #{number}", callback_data=f"{STALE}:{item.id}:archive"
            ),
            InlineKeyboardButton(text=f"Keep #{number}", callback_data=f"{STALE}:{item.id}:keep"),
        ]
        rows.append(row)
    return "\n\n".join(parts), InlineKeyboardMarkup(inline_keyboard=rows)


def parse_stale(data: str) -> tuple[int, str] | None:
    """The item and the action ("archive" or "keep") of a button of the stale prompt."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != STALE or not parts[1].isdigit():
        return None
    if parts[2] not in {"archive", "keep"}:
        return None
    return int(parts[1]), parts[2]


def number_of(markup: InlineKeyboardMarkup | None, data: str) -> str:
    """The number of the item of a button ("#2"), from the text of the button."""
    for row in markup.inline_keyboard if markup is not None else []:
        for button in row:
            if button.callback_data == data:
                return "#" + button.text.rpartition("#")[2]
    return "This thing"


def without_item(
    markup: InlineKeyboardMarkup | None, prefix: str, item_id: int
) -> InlineKeyboardMarkup | None:
    """The same buttons, without the buttons of ``prefix`` for one item. An empty row goes."""
    if markup is None:
        return None
    start = f"{prefix}:{item_id}:"
    rows = [
        [button for button in row if not (button.callback_data or "").startswith(start)]
        for row in markup.inline_keyboard
    ]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


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


def parse_label(data: str, prefix: str = LABEL) -> tuple[int, int, int, str] | None:
    """The item, the goal, the shown score and the verdict of a button, or None for data that a
    button of this version never has."""
    parts = data.split(":")
    if len(parts) != 5 or parts[0] != prefix:
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
