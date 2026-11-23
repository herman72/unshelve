"""Run the bot: check the setup, then ask Telegram for updates until the process stops.

The bot uses long polling (docs/adr/0008): it asks Telegram "anything new?" and Telegram
answers when there is something, or after some seconds. No address of this computer must be
open to the internet. Updates that arrived while the bot was down wait at Telegram for up to
24 hours, so a restart loses nothing.

Each answer of getUpdates marks the liveness file ``bot`` (``unshelve.alive``), and the
healthcheck of Docker reads it (docs/learn/55). A bot without a token marks it on a timer.
"""

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import GetUpdates, Response, TelegramMethod
from aiogram.methods.base import TelegramType
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve import alive
from unshelve.bot import capture, private
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
# The name of the liveness file of the bot (unshelve.alive, deploy/compose.yaml).
ALIVE_NAME = "bot"
# A bot without a token marks its liveness file this often.
IDLE_MARK_EVERY = timedelta(seconds=30)


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


class MarkAlive(BaseRequestMiddleware):
    """Marks the liveness file of the bot after each answer of getUpdates.

    Only getUpdates counts. The polling loop of aiogram catches each error and asks again for
    ever, so a bot that cannot reach Telegram, or whose token was revoked, still runs and only
    logs "Failed to fetch updates". A message that the bot sent is no sign that it receives.
    On a quiet chat, Telegram answers getUpdates after 10 s with no update, so the bot marks
    at least every 10 s while it can poll.
    """

    def __init__(self) -> None:
        # "N/M": N of the M capture chats send their posts to the bot. The next mark writes it.
        self.chats_ready: str | None = None

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        answer = await make_request(bot, method)
        if isinstance(method, GetUpdates):
            if self.chats_ready is None:
                alive.mark(ALIVE_NAME)
            else:
                alive.mark(ALIVE_NAME, chats_ready=self.chats_ready)
        return answer


async def idle(stop: asyncio.Event, *, every: timedelta = IDLE_MARK_EVERY) -> None:
    """A bot without a token: no Telegram, only a sign of life, until ``stop`` is set.

    The first mark comes at once: `docker compose up --wait` waits for the healthcheck.
    """
    while not stop.is_set():
        alive.mark(ALIVE_NAME)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every.total_seconds())
    alive.stopped(ALIVE_NAME)


def build_dispatcher(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> Dispatcher:
    """A dispatcher with the handlers of the bot. Handlers get ``settings`` and ``sessions``."""
    dispatcher = Dispatcher(settings=settings, sessions=sessions)
    dispatcher.include_router(capture.build_router())
    dispatcher.include_router(private.build_router())
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
    liveness = MarkAlive()
    bot.session.middleware(liveness)
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
    ready = await check_capture_chats(bot, settings)
    # The first answer of getUpdates writes it: N of the M capture chats send their posts.
    liveness.chats_ready = f"{len(ready)}/{len(settings.capture_chat_ids)}"
    # start_polling handles SIGINT and SIGTERM, finishes the handlers that run, and closes
    # the connection to Telegram.
    await dispatcher.start_polling(  # pyright: ignore[reportUnknownMemberType]
        bot, **polling_options()
    )
    alive.stopped(ALIVE_NAME)
    log.info("bot.stopped")
