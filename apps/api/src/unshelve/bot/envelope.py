"""From a Telegram message to a capture envelope. Pure: no network and no database.

The importer builds the same envelope from an export. So the rest of the system does not
know, and must not know, which of the two a capture came from.
"""

import re

from aiogram.types import (
    Animation,
    Audio,
    Document,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginUser,
    Video,
)

from unshelve.contracts.dto import CaptureEnvelope, ForwardOrigin, MediaKind, MediaRef
from unshelve.contracts.enums import CaptureSource

_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _urls(message: Message) -> list[str]:
    """The links of a message, in the order of the text, each one time."""
    text = message.text or message.caption or ""
    found: list[str] = []
    for entity in message.entities or message.caption_entities or []:
        if entity.type == "url":
            # Telegram counts the position of an entity in UTF-16 units. extract_from knows
            # that, so an emoji or a Persian letter before the link does not shift it.
            url = entity.extract_from(text)
            # Telegram also marks "example.com/page" as a link. A browser needs a scheme.
            found.append(url if _HAS_SCHEME.match(url) else f"https://{url}")
        elif entity.type == "text_link" and entity.url:
            found.append(entity.url)
    return list(dict.fromkeys(found))


def _media(message: Message) -> list[MediaRef]:
    """The file of a message. A message has one file at most; an album is several messages."""
    if message.photo:
        # Telegram sends one photo in several sizes. The largest is the photo itself.
        largest = max(message.photo, key=lambda size: size.width * size.height)
        return [
            MediaRef(
                kind="photo",
                file_id=largest.file_id,
                file_unique_id=largest.file_unique_id,
                size_bytes=largest.file_size,
            )
        ]
    # An animation comes with a copy of itself in `document`. It is asked first, so the file
    # is listed one time.
    attachments: tuple[tuple[MediaKind, Animation | Video | Document | Audio | None], ...] = (
        ("animation", message.animation),
        ("video", message.video),
        ("document", message.document),
        ("audio", message.audio),
    )
    for kind, file in attachments:
        if file is not None:
            return [
                MediaRef(
                    kind=kind,
                    file_id=file.file_id,
                    file_unique_id=file.file_unique_id,
                    mime=file.mime_type,
                    size_bytes=file.file_size,
                    file_name=file.file_name,
                )
            ]
    if message.voice:
        voice = message.voice
        return [
            MediaRef(
                kind="voice",
                file_id=voice.file_id,
                file_unique_id=voice.file_unique_id,
                mime=voice.mime_type,
                size_bytes=voice.file_size,
            )
        ]
    if message.video_note:
        note = message.video_note
        return [
            MediaRef(
                kind="video",
                file_id=note.file_id,
                file_unique_id=note.file_unique_id,
                size_bytes=note.file_size,
            )
        ]
    if message.sticker:
        sticker = message.sticker
        return [
            MediaRef(
                kind="sticker",
                file_id=sticker.file_id,
                file_unique_id=sticker.file_unique_id,
                size_bytes=sticker.file_size,
            )
        ]
    return []


def _forward_origin(message: Message) -> ForwardOrigin | None:
    origin = message.forward_origin
    if origin is None:
        return None
    if isinstance(origin, MessageOriginChannel):
        return ForwardOrigin(
            type="channel",
            date=origin.date,
            chat_id=origin.chat.id,
            message_id=origin.message_id,
            name=origin.chat.title,
        )
    if isinstance(origin, MessageOriginChat):
        return ForwardOrigin(
            type="chat",
            date=origin.date,
            chat_id=origin.sender_chat.id,
            name=origin.sender_chat.title,
        )
    if isinstance(origin, MessageOriginUser):
        return ForwardOrigin(type="user", date=origin.date, name=origin.sender_user.full_name)
    return ForwardOrigin(type="hidden_user", date=origin.date, name=origin.sender_user_name)


def envelope_from_post(message: Message) -> CaptureEnvelope:
    """The envelope of a channel post, or of an edit of a channel post.

    An edit carries ``edit_date``. That time is its edit number: the same edit gives the same
    number each time Telegram delivers it, so it is stored one time.
    """
    return CaptureEnvelope(
        source=CaptureSource.BOT,
        chat_id=message.chat.id,
        message_id=message.message_id,
        edit_seq=message.edit_date or 0,
        posted_at=message.date,
        text=message.text or message.caption or "",
        urls=_urls(message),
        media=_media(message),
        media_group_id=message.media_group_id,
        forward_origin=_forward_origin(message),
        # Exactly the fields that Telegram sent, so that a later version can read them again.
        raw=message.model_dump(mode="json", by_alias=True, exclude_unset=True),
    )
