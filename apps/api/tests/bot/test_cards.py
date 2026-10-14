"""The text of the cards and of the digest. Each test name is one rule. No database here."""

from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from unshelve.bot import cards
from unshelve.contracts.enums import ItemStatus
from unshelve.db.models import Item

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


def _item(item_id: int = 1, **fields: Any) -> Item:
    values: dict[str, Any] = {
        "id": item_id,
        "owner_id": 1,
        "status": ItemStatus.INBOX,
        "title": "Vector search in Postgres",
        "canonical_url": "https://example.com/how-pgvector-works",
        "site": "example.com",
        "summary": "How pgvector stores vectors.",
        "est_minutes": 6,
        "capture_count": 1,
        "first_captured_at": NOW - timedelta(days=1),
        "last_captured_at": NOW - timedelta(days=1),
    }
    return Item(**(values | fields))


def _buttons(markup: InlineKeyboardMarkup | None) -> list[list[tuple[str, str | None, str | None]]]:
    assert markup is not None
    return [[(b.text, b.url, b.callback_data) for b in row] for row in markup.inline_keyboard]


def test_a_card_has_the_title_alone_then_the_facts_then_the_summary() -> None:
    assert cards.card(_item(), 1).split("\n") == [
        "<b>Vector search in Postgres</b>",
        "#1 · example.com · 6 min",
        "How pgvector stores vectors.",
    ]


def test_text_from_the_internet_cannot_add_marks_to_the_message() -> None:
    hostile = _item(
        title="<b>Win</b> & <a href='x'>click</a>",
        summary="</b><script>alert(1)</script>",
        site="a<b>",
    )

    text = cards.card(hostile, 1)

    assert text.split("\n") == [
        "<b>&lt;b&gt;Win&lt;/b&gt; &amp; &lt;a href='x'&gt;click&lt;/a&gt;</b>",
        "#1 · a&lt;b&gt; · 6 min",
        "&lt;/b&gt;&lt;script&gt;alert(1)&lt;/script&gt;",
    ]


def test_a_persian_title_is_alone_on_its_line() -> None:
    persian = _item(title="مقدمه‌ای بر pgvector و جستجوی برداری", site="blog.example.com")

    title_line, facts_line, _ = cards.card(persian, 2).split("\n")

    # The line of the title has no English word of ours. Telegram gives it the direction of
    # its first letter: right to left.
    assert title_line == "<b>مقدمه‌ای بر pgvector و جستجوی برداری</b>"
    assert facts_line == "#2 · blog.example.com · 6 min"


def test_an_item_without_a_title_shows_its_address_or_says_so() -> None:
    assert cards.title_of(_item(title=None)) == "https://example.com/how-pgvector-works"
    assert cards.title_of(_item(title=None, canonical_url=None)) == "A note without a title"


def test_a_long_title_and_a_long_summary_are_cut() -> None:
    long = _item(title="word " * 100, summary="sentence " * 100)

    title_line, _, summary_line = cards.card(long, 1).split("\n")

    assert len(title_line) <= cards.MAX_TITLE + len("<b></b>")
    assert title_line.endswith("…</b>")
    assert len(summary_line) <= cards.MAX_SUMMARY
    assert summary_line.endswith("…")


def test_the_facts_say_how_many_times_a_thing_was_saved() -> None:
    assert cards.card(_item(capture_count=3), 1).split("\n")[1] == (
        "#1 · example.com · 6 min · saved 3 times"
    )


def test_each_item_gets_a_button_to_open_it_and_a_button_for_done() -> None:
    note = _item(2, canonical_url=None)

    rows = _buttons(cards.buttons([_item(1), note]))

    assert rows == [
        [
            ("Open #1", "https://example.com/how-pgvector-works", None),
            ("Done #1", None, "done:1"),
        ],
        [("Done #2", None, "done:2")],
    ]


def test_an_address_that_is_not_a_web_address_gets_no_open_button() -> None:
    for address in (
        "javascript:alert(1)",
        "tg://resolve?domain=x",
        "file:///etc/passwd",
        "https://",
    ):
        rows = _buttons(cards.buttons([_item(canonical_url=address)]))
        assert rows == [[("Done #1", None, "done:1")]]


def test_the_pressed_button_goes_and_an_empty_row_goes_too() -> None:
    markup = cards.buttons([_item(1), _item(2, canonical_url=None)])

    fewer = cards.without_button(markup, "done:2")
    none_left = cards.without_button(cards.buttons([_item(5, canonical_url=None)]), "done:5")

    assert _buttons(fewer) == [
        [("Open #1", "https://example.com/how-pgvector-works", None), ("Done #1", None, "done:1")]
    ]
    assert none_left is None
    assert cards.without_button(markup, "done:9") == markup


def test_a_digest_lists_the_week_and_then_one_thing_from_the_shelf() -> None:
    older = _item(9, title="An old paper", last_captured_at=NOW - timedelta(days=95))

    text, markup = cards.digest_message(
        [_item(1), _item(2, title="Second")], older, now=NOW, days=7
    )

    parts = text.split("\n\n")
    assert parts[0] == "<b>Your digest</b>\nYou saved 2 things in the last 7 days."
    assert parts[1].startswith("<b>Vector search in Postgres</b>\n#1 ·")
    assert parts[2].startswith("<b>Second</b>\n#2 ·")
    assert parts[3].startswith("<b>From your shelf</b>\n<b>An old paper</b>\n#3 ·")
    assert parts[3].endswith("saved 3 months ago")
    assert markup is not None
    assert [row[-1].callback_data for row in markup.inline_keyboard] == [
        "done:1",
        "done:2",
        "done:9",
    ]


def test_a_week_without_saves_still_brings_one_thing_from_the_shelf() -> None:
    text, _ = cards.digest_message(
        [], _item(9, last_captured_at=NOW - timedelta(days=20)), now=NOW, days=7
    )

    assert text.startswith("<b>Your digest</b>\nYou saved nothing new in the last 7 days.")
    assert "<b>From your shelf</b>" in text


def test_one_saved_thing_is_one_thing() -> None:
    text, _ = cards.digest_message([_item()], None, now=NOW, days=7)

    assert "You saved 1 thing in the last 7 days." in text


def test_a_digest_that_is_too_long_loses_the_summaries_first() -> None:
    many = [_item(n, title="t" * 190, summary="s" * 500) for n in range(1, 30)]

    text, _ = cards.digest_message(many, None, now=NOW, days=7)

    assert len(text) <= cards.MAX_MESSAGE or "sss" not in text
    assert "sss" not in text


def test_the_age_of_an_older_thing_is_said_in_days_weeks_or_months() -> None:
    assert cards.ago(NOW - timedelta(days=1), NOW) == "saved 1 day ago"
    assert cards.ago(NOW - timedelta(days=9), NOW) == "saved 9 days ago"
    assert cards.ago(NOW - timedelta(days=30), NOW) == "saved 4 weeks ago"
    assert cards.ago(NOW - timedelta(days=400), NOW) == "saved 13 months ago"


def test_a_button_can_have_no_more_than_64_bytes_of_data() -> None:
    markup = cards.buttons([_item(9_223_372_036_854_775_807)])  # the largest id of a BIGINT

    assert markup is not None
    button: InlineKeyboardButton = markup.inline_keyboard[0][-1]
    assert button.callback_data is not None
    assert len(button.callback_data.encode()) <= 64
