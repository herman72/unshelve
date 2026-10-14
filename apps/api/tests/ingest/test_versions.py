"""The versions of a message, and what an item reads. Each test name is one rule. No database."""

from datetime import UTC, datetime
from typing import Any

from unshelve.contracts.enums import CaptureSource
from unshelve.db.models import Capture
from unshelve.ingest.versions import latest_versions, source

NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _capture(message_id: int, edit_seq: int = 0, **fields: Any) -> Capture:
    values: dict[str, Any] = {
        "id": message_id * 1000 + edit_seq,
        "owner_id": 1,
        "source": CaptureSource.BOT,
        "chat_id": -1001234567890,
        "message_id": message_id,
        "edit_seq": edit_seq,
        "posted_at": NOON,
        "text": "",
        "urls": [],
        "media": [],
        "forward_origin": None,
    }
    return Capture(**(values | fields))


def test_each_message_comes_one_time_in_its_latest_version() -> None:
    first, edit, later_edit, other = (
        _capture(7, 0, text="v0"),
        _capture(7, 5, text="v1"),
        _capture(7, 9, text="v2"),
        _capture(3, 0, text="other"),
    )

    latest = latest_versions([edit, other, later_edit, first])

    assert [(c.message_id, c.text) for c in latest] == [(3, "other"), (7, "v2")]


def test_the_item_reads_each_different_text_one_time_in_the_order_of_the_messages() -> None:
    read = source(
        [
            _capture(8, text="Second thought"),
            _capture(7, text="Read this"),
            _capture(9, text="read   THIS "),  # the same text: other capitals and spaces
            _capture(10, text="  "),
        ]
    )

    assert read.texts == ("Read this", "Second thought")
    assert read.text == "Read this\n\nSecond thought"


def test_the_links_and_the_files_come_one_time_each() -> None:
    photo = {"kind": "photo", "file_unique_id": "unique-a"}
    read = source(
        [
            _capture(7, urls=["https://example.com/a"], media=[photo]),
            _capture(8, urls=["https://example.com/a", "https://example.com/b"], media=[photo]),
        ]
    )

    assert read.urls == ("https://example.com/a", "https://example.com/b")
    assert read.media == (photo,)


def test_the_first_source_of_a_forward_is_the_source_of_the_item() -> None:
    read = source(
        [
            _capture(7, text="a", forward_origin={"type": "channel", "chat_id": 1}),
            _capture(8, text="b", forward_origin={"type": "channel", "chat_id": 2}),
        ]
    )

    assert read.forward_origin == {"type": "channel", "chat_id": 1}


def test_the_code_changes_with_a_new_text_and_not_with_the_same_text_again() -> None:
    one = source([_capture(7, text="Read this")])
    again = source([_capture(7, text="Read this"), _capture(8, text="read this")])
    edited = source([_capture(7, text="Read this"), _capture(7, 5, text="Read this today")])
    with_a_file = source(
        [_capture(7, text="Read this", media=[{"kind": "photo", "local_path": "photos/1.jpg"}])]
    )

    assert again.code() == one.code()
    assert edited.code() != one.code()
    assert with_a_file.code() != one.code()


def test_an_edit_that_keeps_the_text_keeps_the_code() -> None:
    # A button under a post makes an edit with the same text.
    assert (
        source([_capture(7, text="a"), _capture(7, 5, text="a")]).code()
        == source([_capture(7, text="a")]).code()
    )
