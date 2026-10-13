"""Capture: each post of an allowed channel is stored as it arrives.

The handler does one small thing: it stores the message and adds a job. Everything slow
happens later, on the worker. So the bot is ready for the next message at once.
"""

import asyncio

import structlog
from aiogram import Router
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth_store
from unshelve.bot.envelope import envelope_from_post
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope
from unshelve.ingest import resolve

log = structlog.get_logger()

# Seconds to wait before the next try, when the database does not answer. The last value is
# used again and again.
RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)
DATABASE_IS_DOWN = (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError, OSError)


async def _save_until_stored(
    sessions: async_sessionmaker[AsyncSession], owner_telegram_id: int, envelope: CaptureEnvelope
) -> int | None:
    """Store the message. When the database is down, wait and try again, without an end.

    The bot library marks an update as done when its handler ends, also when the handler ends
    with an error. An error here would thus lose the post. While this function waits, the bot
    asks Telegram for nothing, so Telegram keeps the update. If the bot stops in that time,
    Telegram sends the update again after the restart.

    A second try after a save that did work is harmless: the message is stored already, and
    the answer is None, as for each repeat.
    """
    attempt = 0
    while True:
        try:
            async with sessions() as session:
                owner_id = await auth_store.ensure_owner(session, owner_telegram_id)
                capture_id = await resolve.accept_capture(session, owner_id, envelope)
                # The message and its job are saved together.
                await session.commit()
        except DATABASE_IS_DOWN as error:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            attempt += 1
            log.error(
                "capture.database_down",
                chat_id=envelope.chat_id,
                message_id=envelope.message_id,
                attempt=attempt,
                retry_in_s=delay,
                reason=type(error).__name__,
            )
            await asyncio.sleep(delay)
        else:
            return capture_id


async def _store(
    message: Message, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    chat_id = message.chat.id
    if chat_id not in settings.capture_chat_ids:
        # Anybody can make a bot an administrator of a channel. Only the channels of the
        # owner are stored. A channel post names no person, so the channel is the check.
        log.warning(
            "capture.chat_not_allowed",
            chat_id=chat_id,
            chat_title=message.chat.title,
            hint="to capture from this channel, add its id to UNSHELVE_CAPTURE_CHAT_IDS",
        )
        return
    if settings.owner_telegram_id is None:
        log.error("capture.no_owner", hint="set UNSHELVE_OWNER_TELEGRAM_ID")
        return

    envelope = envelope_from_post(message)
    capture_id = await _save_until_stored(sessions, settings.owner_telegram_id, envelope)
    log.info(
        "capture.stored" if capture_id is not None else "capture.repeat",
        chat_id=chat_id,
        message_id=envelope.message_id,
        edit_seq=envelope.edit_seq,
        album=envelope.media_group_id,
        capture_id=capture_id,
    )


async def on_channel_post(
    message: Message, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await _store(message, settings, sessions)


async def on_edited_channel_post(
    message: Message, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await _store(message, settings, sessions)


async def on_membership(update: ChatMemberUpdated, settings: Settings) -> None:
    """The bot was added to a chat, removed from it, or its rights changed."""
    log.info(
        "bot.membership",
        chat_id=update.chat.id,
        chat_title=update.chat.title,
        chat_type=update.chat.type,
        status=update.new_chat_member.status,
        allowed=update.chat.id in settings.capture_chat_ids,
    )


def build_router() -> Router:
    """A router with the capture handlers. A router belongs to one dispatcher, so each
    dispatcher gets a new one."""
    router = Router(name="capture")
    router.channel_post.register(on_channel_post)
    router.edited_channel_post.register(on_edited_channel_post)
    router.my_chat_member.register(on_membership)
    return router
