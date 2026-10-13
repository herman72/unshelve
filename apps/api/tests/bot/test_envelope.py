"""From a Telegram message to a capture envelope. Each test name is one rule."""

from datetime import UTC, datetime
from typing import Any

import pytest

from tests.bot.support import CHANNEL, post, raw_updates, updates
from unshelve.bot.envelope import envelope_from_post
from unshelve.contracts.dto import ForwardOrigin, MediaRef
from unshelve.contracts.enums import CaptureSource


def test_a_post_with_a_link() -> None:
    envelope = envelope_from_post(post("seen", 10))

    assert envelope.source is CaptureSource.BOT
    assert (envelope.chat_id, envelope.message_id, envelope.edit_seq) == (CHANNEL, 10, 0)
    assert envelope.posted_at == datetime(2026, 10, 13, 20, 26, 7, tzinfo=UTC)
    assert envelope.text == "https://www.youtube.com/watch?v=UPtmKh1vMN8"
    assert envelope.urls == ["https://www.youtube.com/watch?v=UPtmKh1vMN8"]
    assert envelope.media == []
    assert envelope.media_group_id is None
    assert envelope.forward_origin is None


SENT_POSTS = [
    pytest.param(
        name, raw.get("channel_post") or raw["edited_channel_post"], id=f"{name}-{raw['update_id']}"
    )
    for name in ("seen", "made_up")
    for raw in raw_updates(name)
    if "channel_post" in raw or "edited_channel_post" in raw
]


@pytest.mark.parametrize(("name", "sent"), SENT_POSTS)
def test_the_envelope_keeps_the_message_as_telegram_sent_it(
    name: str, sent: dict[str, Any]
) -> None:
    message = next(
        m
        for u in updates(name)
        if (m := u.channel_post or u.edited_channel_post) is not None
        and (m.chat.id, m.message_id, m.edit_date)
        == (sent["chat"]["id"], sent["message_id"], sent.get("edit_date"))
    )

    # Nothing is added and nothing is lost: a later version can read the message again.
    assert envelope_from_post(message).raw == sent


def test_a_message_of_an_album_has_the_largest_photo_and_the_album_number() -> None:
    envelope = envelope_from_post(post("seen", 7))

    assert envelope.media_group_id == "14335384938541892"
    assert envelope.text == ""
    assert envelope.media == [
        MediaRef(
            kind="photo",
            file_id="AgACAgQA-a-y",
            file_unique_id="AQAD-a-y",
            size_bytes=105571,
        )
    ]


def test_a_forwarded_post_names_its_source() -> None:
    envelope = envelope_from_post(post("seen", 11))

    assert envelope.forward_origin == ForwardOrigin(
        type="channel",
        date=datetime(2026, 9, 20, 15, 15, 2, tzinfo=UTC),
        chat_id=-1001111111111,
        message_id=590,
        name="Tech Notes",
    )
    assert envelope.urls == ["http://github.com/cloudflare/security-audit-skill"]


def test_a_link_is_found_after_an_emoji_and_persian_text() -> None:
    envelope = envelope_from_post(post("made_up", 12))

    # The first link had no "https://" in the text. The second was hidden behind a word.
    assert envelope.urls == ["https://example.com/fa", "https://example.org/hidden?x=1"]
    assert envelope.text.startswith("👋 سلام")


def test_a_file_with_a_caption() -> None:
    envelope = envelope_from_post(post("made_up", 13))

    assert envelope.text == "The paper as a file https://example.com/paper"
    assert envelope.urls == ["https://example.com/paper"]
    assert envelope.media == [
        MediaRef(
            kind="document",
            file_id="BQACAgQA-doc",
            file_unique_id="AgAD-doc",
            mime="application/pdf",
            size_bytes=548412,
            file_name="paper.pdf",
        )
    ]


def test_a_video() -> None:
    (video,) = envelope_from_post(post("made_up", 14)).media

    assert (video.kind, video.mime, video.file_name) == ("video", "video/mp4", "clip.mp4")


def test_an_animation_is_one_file_and_not_two() -> None:
    (animation,) = envelope_from_post(post("made_up", 15)).media

    assert animation.kind == "animation"


def test_an_edit_has_the_time_of_the_edit_as_its_number() -> None:
    envelope = envelope_from_post(post("made_up", 10))

    assert envelope.edit_seq == 1791930240
    assert envelope.message_id == 10
    assert envelope.text.endswith("watch the first hour")


@pytest.mark.parametrize(
    ("message_id", "kind", "name", "chat_id"),
    [
        (16, "hidden_user", "A hidden person", None),
        (17, "user", "Ada Lovelace", None),
        (18, "chat", "A study group", -1005555555555),
    ],
    ids=["hidden person", "person", "group"],
)
def test_the_other_kinds_of_forward(
    message_id: int, kind: str, name: str, chat_id: int | None
) -> None:
    origin = envelope_from_post(post("made_up", message_id)).forward_origin

    assert origin is not None
    assert (origin.type, origin.name, origin.chat_id, origin.message_id) == (
        kind,
        name,
        chat_id,
        None,
    )
