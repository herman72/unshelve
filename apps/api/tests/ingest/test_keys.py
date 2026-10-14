"""The keys of a save. Each test name is one rule. No database is used here."""

from datetime import UTC, datetime
from typing import Any

from unshelve.contracts.enums import CaptureSource
from unshelve.db.models import Capture
from unshelve.ingest import keys
from unshelve.ingest.keys import keys_of, primary_url, text_key

NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
CHAT = -1001234567890


def _capture(message_id: int = 7, **fields: Any) -> Capture:
    values: dict[str, Any] = {
        "owner_id": 1,
        "source": CaptureSource.BOT,
        "chat_id": CHAT,
        "message_id": message_id,
        "edit_seq": 0,
        "posted_at": NOON,
        "text": "",
        "urls": [],
        "media": [],
        "media_group_id": None,
        "forward_origin": None,
    }
    return Capture(**(values | fields))


def _photo(name: str) -> dict[str, Any]:
    return {"kind": "photo", "file_id": f"id-{name}", "file_unique_id": f"unique-{name}"}


def test_a_link_gives_a_key_with_its_canonical_address() -> None:
    post = _capture(urls=["http://www.example.com/post/?utm_source=telegram#top"], text="Read it")

    assert keys_of([post]) == ["url:https://example.com/post"]
    assert primary_url([post]) == "https://example.com/post"


def test_only_the_first_link_is_a_key() -> None:
    post = _capture(urls=["https://example.com/first", "https://example.com/second"])

    assert keys_of([post]) == ["url:https://example.com/first"]


def test_a_note_gives_a_key_from_its_text() -> None:
    note = _capture(text="Idea: compare RRF with a weighted sum")

    (key,) = keys_of([note])
    assert key == text_key("Idea: compare RRF with a weighted sum")
    assert key.startswith("text:")
    assert len(key) == len("text:") + 32


def test_capital_letters_and_white_space_do_not_change_the_key_of_a_note() -> None:
    assert text_key("Idea:  compare RRF\nwith a weighted sum ") == text_key(
        "idea: compare rrf with a weighted sum"
    )
    assert text_key("Idea one") != text_key("Idea two")


def test_an_empty_note_has_no_key() -> None:
    assert text_key("  \n ") is None
    assert keys_of([_capture(text="  ")]) == []


def test_the_text_is_not_a_key_when_the_post_has_a_link() -> None:
    post = _capture(text="Read this", urls=["https://example.com/post"])

    assert keys_of([post]) == ["url:https://example.com/post"]


def test_a_file_gives_a_key_and_the_caption_does_not() -> None:
    post = _capture(text="The whiteboard", media=[_photo("a")])

    assert keys_of([post]) == ["tg_file:unique-a"]


def test_a_file_of_an_export_has_no_key() -> None:
    # An export gives the path of a file, and no id that stays the same in each copy.
    post = _capture(media=[{"kind": "photo", "local_path": "photos/photo_1.jpg"}])

    assert keys_of([post]) == []


def test_a_forwarded_post_gives_a_key_with_its_source() -> None:
    post = _capture(
        text="A post of a channel",
        forward_origin={"type": "channel", "chat_id": -1001111111111, "message_id": 590},
    )

    assert set(keys_of([post])) == {
        "tg_fwd:-1001111111111:590",
        text_key("A post of a channel"),
    }


def test_a_forward_without_the_ids_of_its_source_gives_no_key() -> None:
    # From a person, or from an export: the name of the source is known, and no id.
    from_a_person = _capture(urls=["https://example.com/post"], forward_origin={"type": "user"})
    from_an_export = _capture(
        urls=["https://example.com/post"], forward_origin={"type": "unknown", "name": "Tech Notes"}
    )

    # A group as the source: the chat is known, and not the message.
    from_a_group = _capture(
        urls=["https://example.com/post"],
        forward_origin={"type": "chat", "chat_id": -1005555555555, "message_id": None},
    )

    assert keys_of([from_a_person]) == ["url:https://example.com/post"]
    assert keys_of([from_an_export]) == ["url:https://example.com/post"]
    assert keys_of([from_a_group]) == ["url:https://example.com/post"]


def test_an_album_gives_one_key_for_the_album_and_one_for_each_file() -> None:
    album = [
        _capture(7, media_group_id="143", media=[_photo("a")], text="Slides"),
        _capture(8, media_group_id="143", media=[_photo("b")]),
        _capture(9, media_group_id="143", media=[_photo("c")]),
    ]

    assert keys_of(album) == [
        f"tg_album:{CHAT}:143",
        "tg_file:unique-a",
        "tg_file:unique-b",
        "tg_file:unique-c",
    ]


def test_the_link_of_an_album_is_the_first_link_in_its_parts() -> None:
    album = [
        _capture(7, media_group_id="143", media=[_photo("a")]),
        _capture(8, media_group_id="143", media=[_photo("b")], urls=["https://example.com/talk"]),
    ]

    assert primary_url(album) == "https://example.com/talk"
    assert "url:https://example.com/talk" in keys_of(album)


def test_the_keys_come_in_a_fixed_order_and_one_time_each() -> None:
    post = _capture(
        urls=["https://example.com/post"],
        media=[_photo("b"), _photo("a"), _photo("b")],
        forward_origin={"type": "channel", "chat_id": -1001111111111, "message_id": 590},
    )

    found = keys_of([post])

    assert found == sorted(set(found))
    assert found == [
        "tg_file:unique-a",
        "tg_file:unique-b",
        "tg_fwd:-1001111111111:590",
        "url:https://example.com/post",
    ]


def test_a_very_long_address_gets_a_short_key() -> None:
    long_address = "https://example.com/search?q=" + "a" * 3000
    post = _capture(urls=[long_address])

    (key,) = keys_of([post])

    assert key.startswith("url:sha256:")
    assert len(key.encode()) <= keys.MAX_KEY_BYTES
    assert keys_of([_capture(8, urls=[long_address + "&utm_source=x"])]) == [key]
    assert keys_of([_capture(9, urls=[long_address + "b"])]) != [key]
