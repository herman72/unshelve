"""Run the bot: check the setup, then ask Telegram for updates until the process stops.

The bot uses long polling (docs/adr/0008): it asks Telegram "anything new?" and Telegram
answers when there is something, or after some seconds. No address of this computer must be
open to the internet. Updates that arrived while the bot was down wait at Telegram for up to
24 hours, so a restart loses nothing.
"""

import logging
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.bot import capture
from unshelve.config import Settings

log = structlog.get_logger()

# The kinds of updates that the bot uses (docs/adr/0009). Telegram sends no other kind.
ALLOWED_UPDATES = [
    "message",
    "channel_post",
    "edited_channel_post",
    "my_chat_member",
    "callback_query",
]
ADMIN_STATUSES = frozenset({"administrator", "creator"})


def polling_options() -> dict[str, Any]:
    """How the bot asks Telegram for updates."""
    return {
        "allowed_updates": ALLOWED_UPDATES,
        # One update after the other. The question for the next updates is what tells
        # Telegram that the bot has the earlier ones. With the default of the library, several
        # handlers run at the same time, and that question can leave before a message is
        # stored. Then a bot that stops at the wrong moment loses a post.
        "handle_as_tasks": False,
    }


class HideToken(logging.Filter):
    """Replace the bot token in log lines of libraries. A token in a log is a leaked token."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._token in message:
            record.msg = message.replace(self._token, "<bot token>")
            record.args = None
        return True


def hide_token_in_logs(token: str) -> None:
    for handler in logging.getLogger().handlers:
        handler.addFilter(HideToken(token))


def build_dispatcher(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> Dispatcher:
    """A dispatcher with the handlers of the bot. Handlers get ``settings`` and ``sessions``."""
    dispatcher = Dispatcher(settings=settings, sessions=sessions)
    dispatcher.include_router(capture.build_router())
    return dispatcher


async def check_capture_chats(bot: Bot, settings: Settings) -> list[int]:
    """Check that the bot is an administrator of each capture chat. Returns the ids that are fine.

    A bot gets the posts of a channel only as an administrator. A wrong setup gives no error
    later: the posts do not arrive, and nothing says why. So the bot says it at the start.
    """
    me = await bot.get_me()
    ready: list[int] = []
    for chat_id in settings.capture_chat_ids:
        try:
            member = await bot.get_chat_member(chat_id, me.id)
        except TelegramAPIError as error:
            log.error("bot.chat_not_reachable", chat_id=chat_id, reason=error.message)
            continue
        if member.status in ADMIN_STATUSES:
            ready.append(chat_id)
            log.info("bot.chat_ready", chat_id=chat_id, status=member.status)
        else:
            log.error(
                "bot.not_an_administrator",
                chat_id=chat_id,
                status=member.status,
                hint="make the bot an administrator of this channel",
            )
    return ready


async def run_bot(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> None:
    """Run until the process gets SIGINT or SIGTERM."""
    if settings.telegram_bot_token is None:
        msg = "run_bot needs UNSHELVE_TELEGRAM_BOT_TOKEN"
        raise RuntimeError(msg)
    token = settings.telegram_bot_token.get_secret_value()
    hide_token_in_logs(token)
    bot = Bot(token)
    dispatcher = build_dispatcher(settings, sessions)

    me = await bot.get_me()
    log.info(
        "bot.started",
        username=me.username,
        capture_chats=len(settings.capture_chat_ids),
        owner_is_set=settings.owner_telegram_id is not None,
    )
    if not settings.capture_chat_ids:
        log.warning(
            "bot.no_capture_chats",
            hint="post in your channel; this log then shows the id for UNSHELVE_CAPTURE_CHAT_IDS",
        )
    await check_capture_chats(bot, settings)
    # start_polling handles SIGINT and SIGTERM, finishes the handlers that run, and closes
    # the connection to Telegram.
    await dispatcher.start_polling(  # pyright: ignore[reportUnknownMemberType]
        bot, **polling_options()
    )
    log.info("bot.stopped")
