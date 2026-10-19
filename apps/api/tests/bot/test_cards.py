"""The text of the cards, of the digest and of the stale prompt. Each test name is one rule. No
database here."""

from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from unshelve.bot import cards
from unshelve.contracts.enums import GoalLabel, ItemStatus
from unshelve.db.models import Goal, Item
from unshelve.resurface.plan import PlannedDigest
from unshelve.resurface.selection import Pick, Reason

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


def _planned(
    *picks: tuple[Item, Pick],
    goal: Goal | None = None,
    titles: dict[int, str] | None = None,
    scores: dict[tuple[int, int], tuple[int, GoalLabel | None]] | None = None,
) -> PlannedDigest:
    return PlannedDigest(
        goal=goal, picks=list(picks), goal_titles=titles or {}, scores=scores or {}
    )


FOCUS = Goal(id=1, owner_id=1, slug="database-internals", title="Database internals")


def test_a_digest_lists_its_picks_in_order_each_with_its_reason() -> None:
    planned = _planned(
        (_item(1), Pick(1, Reason.FOCUS_GOAL, 6, goal_id=1)),
        (_item(2, title="Finnish cases"), Pick(2, Reason.OTHER_GOAL, 6, goal_id=2)),
        (_item(3, title="Sourdough", capture_count=3), Pick(3, Reason.SAVED_AGAIN, 6)),
        (_item(4, title="New paper"), Pick(4, Reason.RECENT, 6)),
        (
            _item(5, title="Bike route", last_captured_at=NOW - timedelta(days=95)),
            Pick(5, Reason.RANDOM, 6),
        ),
        goal=FOCUS,
        titles={1: "Database internals", 2: "Learn Finnish"},
        scores={(1, 1): (3, None), (2, 2): (2, None)},
    )

    text, markup = cards.digest_message(planned, now=NOW)

    parts = text.split("\n\n")
    assert parts[0] == "<b>Your digest</b>\n5 things, about 30 minutes."
    assert parts[1] == "Your focus goal:\n<b>Database internals</b>"
    assert parts[2].startswith("<b>Vector search in Postgres</b>\n#1 ·")
    assert parts[2].endswith("\nFor your focus goal. Score: 3 of 3.")
    assert parts[3].endswith("\nFor your goal:\n<b>Learn Finnish</b>\nScore: 2 of 3.")
    assert parts[4].endswith("\nYou saved it 3 times.")
    assert parts[5].endswith("\nNew in your inbox: saved 1 day ago.")
    assert parts[6].endswith("\nA random pick from your inbox: saved 3 months ago.")
    assert parts[7] == "<i>Right or Wrong: is the score of the goal right?</i>"
    assert _buttons(markup) == [
        [("Open #1", "https://example.com/how-pgvector-works", None), ("Done #1", None, "done:1")],
        [("Right #1", None, "dlabel:1:1:3:right"), ("Wrong #1", None, "dlabel:1:1:3:wrong")],
        [("Open #2", "https://example.com/how-pgvector-works", None), ("Done #2", None, "done:2")],
        [("Right #2", None, "dlabel:2:2:2:right"), ("Wrong #2", None, "dlabel:2:2:2:wrong")],
        [("Open #3", "https://example.com/how-pgvector-works", None), ("Done #3", None, "done:3")],
        [("Open #4", "https://example.com/how-pgvector-works", None), ("Done #4", None, "done:4")],
        [("Open #5", "https://example.com/how-pgvector-works", None), ("Done #5", None, "done:5")],
    ]


def test_a_score_with_a_verdict_says_it_and_gets_no_buttons_of_a_verdict() -> None:
    planned = _planned(
        (_item(1), Pick(1, Reason.FOCUS_GOAL, 6, goal_id=1)),
        (_item(2), Pick(2, Reason.FOCUS_GOAL, 6, goal_id=1)),
        goal=FOCUS,
        titles={1: "Database internals"},
        scores={(1, 1): (3, GoalLabel.CONFIRMED), (2, 1): (0, GoalLabel.REJECTED)},
    )

    text, markup = cards.digest_message(planned, now=NOW)

    assert "For your focus goal. Score: 3 of 3. You said: right." in text
    assert "For your focus goal. Score: 0 of 3. You said: wrong." in text
    assert "Right or Wrong" not in text
    assert [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data] == [  # type: ignore[union-attr]
        "done:1",
        "done:2",
    ]


def test_the_random_pick_shows_its_score_for_the_focus_goal_and_asks_about_it() -> None:
    # A low score is what the other rules never show: a verdict on it finds a useful item that
    # the model missed.
    random_pick = (_item(5, title="Bike route"), Pick(5, Reason.RANDOM, 6))
    planned = _planned(random_pick, goal=FOCUS, scores={(5, 1): (0, None)})

    text, markup = cards.digest_message(planned, now=NOW)

    assert text.split("\n\n")[2].endswith(
        "\nA random pick from your inbox: saved 1 day ago.\nFor your focus goal. Score: 0 of 3."
    )
    assert _buttons(markup)[1] == [
        ("Right #1", None, "dlabel:5:1:0:right"),
        ("Wrong #1", None, "dlabel:5:1:0:wrong"),
    ]


def test_a_random_pick_without_a_score_for_the_focus_goal_asks_nothing() -> None:
    random_pick = (_item(5, title="Bike route"), Pick(5, Reason.RANDOM, 6))
    without_focus = _planned(random_pick, scores={(5, 1): (0, None)})
    without_score = _planned(random_pick, goal=FOCUS)

    for planned in (without_focus, without_score):
        text, markup = cards.digest_message(planned, now=NOW)

        assert "Score" not in text
        assert _buttons(markup) == [
            [
                ("Open #1", "https://example.com/how-pgvector-works", None),
                ("Done #1", None, "done:5"),
            ]
        ]


def test_a_goal_title_stands_alone_on_its_line_and_is_escaped() -> None:
    persian = "یادگیری سیستم <i>"
    planned = _planned(
        (_item(1), Pick(1, Reason.OTHER_GOAL, 6, goal_id=7)),
        goal=Goal(id=8, owner_id=1, slug="x", title="Rust & <i>systems</i>"),
        titles={7: persian},
        scores={(1, 7): (2, None)},
    )

    text, _ = cards.digest_message(planned, now=NOW)

    lines = text.splitlines()
    assert "<b>Rust &amp; &lt;i&gt;systems&lt;/i&gt;</b>" in lines
    assert "<b>" + "یادگیری سیستم &lt;i&gt;" + "</b>" in lines


def test_a_digest_without_a_focus_goal_has_no_line_for_it_and_one_pick_is_one_thing() -> None:
    text, _ = cards.digest_message(_planned((_item(1), Pick(1, Reason.RECENT, 6))), now=NOW)

    assert text.startswith("<b>Your digest</b>\n1 thing, about 6 minutes.\n\n<b>Vector search")
    assert "focus goal" not in text


def test_a_digest_that_is_too_long_loses_the_summaries_first() -> None:
    many = [
        (_item(n, title="t" * 190, summary="s" * 500), Pick(n, Reason.RECENT, 6))
        for n in range(1, 30)
    ]

    text, _ = cards.digest_message(_planned(*many), now=NOW)

    assert "sss" not in text


# --- The stale prompt --------------------------------------------------------------------


def test_the_stale_prompt_asks_about_each_item_with_archive_and_keep() -> None:
    old = [
        _item(4, last_captured_at=NOW - timedelta(days=40)),
        _item(9, title="A note", canonical_url=None, last_captured_at=NOW - timedelta(days=200)),
    ]

    text, markup = cards.stale_message(old, now=NOW)

    parts = text.split("\n\n")
    assert parts[0] == (
        "<b>Still in your inbox</b>\nThese things wait since 30 days or more. Archive, or keep?"
    )
    assert parts[1].startswith("<b>Vector search in Postgres</b>\n#1 ·")
    assert parts[1].endswith("\nsaved 5 weeks ago")
    assert "How pgvector stores vectors." not in text  # no summaries: a short question
    assert parts[2].startswith("<b>A note</b>\n#2")
    assert _buttons(markup) == [
        [
            ("Open #1", "https://example.com/how-pgvector-works", None),
            ("Archive #1", None, "stale:4:archive"),
            ("Keep #1", None, "stale:4:keep"),
        ],
        [("Archive #2", None, "stale:9:archive"), ("Keep #2", None, "stale:9:keep")],
    ]


def test_one_stale_thing_is_one_thing() -> None:
    text, _ = cards.stale_message([_item(4, last_captured_at=NOW - timedelta(days=40))], now=NOW)

    assert "This thing waits since 30 days or more." in text


def test_only_the_buttons_of_this_version_are_read() -> None:
    assert cards.parse_stale("stale:4:archive") == (4, "archive")
    assert cards.parse_stale("stale:4:keep") == (4, "keep")
    for data in ("stale:4:delete", "stale:x:keep", "stale:4", "done:4", "stale:4:keep:1"):
        assert cards.parse_stale(data) is None
    assert cards.parse_label("dlabel:1:2:3:right", prefix=cards.DIGEST_LABEL) == (1, 2, 3, "right")
    assert cards.parse_label("dlabel:1:2:3:right") is None  # not a button of /label
    assert cards.parse_label("label:1:2:3:right", prefix=cards.DIGEST_LABEL) is None


def test_a_pressed_item_loses_its_buttons_and_says_its_number() -> None:
    _, markup = cards.stale_message(
        [_item(4, last_captured_at=NOW - timedelta(days=40)), _item(9)], now=NOW
    )

    assert cards.number_of(markup, "stale:9:keep") == "#2"
    assert cards.number_of(markup, "stale:5:keep") == "This thing"
    assert _buttons(cards.without_item(markup, cards.STALE, 9)) == [
        [
            ("Open #1", "https://example.com/how-pgvector-works", None),
            ("Archive #1", None, "stale:4:archive"),
            ("Keep #1", None, "stale:4:keep"),
        ],
        [("Open #2", "https://example.com/how-pgvector-works", None)],
    ]


def test_the_age_of_an_older_thing_is_said_in_days_weeks_or_months() -> None:
    assert cards.ago(NOW - timedelta(hours=23), NOW) == "saved today"
    assert cards.ago(NOW - timedelta(days=1), NOW) == "saved 1 day ago"
    assert cards.ago(NOW - timedelta(days=9), NOW) == "saved 9 days ago"
    assert cards.ago(NOW - timedelta(days=30), NOW) == "saved 4 weeks ago"
    assert cards.ago(NOW - timedelta(days=400), NOW) == "saved 13 months ago"


def test_a_button_can_have_no_more_than_64_bytes_of_data() -> None:
    largest = 9_223_372_036_854_775_807  # the largest id of a BIGINT
    planned = _planned(
        (_item(largest), Pick(largest, Reason.OTHER_GOAL, 6, goal_id=largest)),
        titles={largest: "A goal"},
        scores={(largest, largest): (3, None)},
    )
    markups = [
        cards.buttons([_item(largest)]),
        cards.digest_buttons(planned),
        cards.stale_message([_item(largest)], now=NOW)[1],
    ]

    data = [
        button.callback_data
        for markup in markups
        for row in markup.inline_keyboard  # type: ignore[union-attr]
        for button in row
        if button.callback_data
    ]
    assert len(data) == 6
    assert all(len(item.encode()) <= 64 for item in data)
