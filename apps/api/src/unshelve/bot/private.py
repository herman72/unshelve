"""The private chat with the bot: the owner writes a command, and the bot answers.

A bot has a public name, so anybody can write to it. This bot answers one person: the owner
(``UNSHELVE_OWNER_TELEGRAM_ID``). A message of any other person gets no answer at all. An
answer would tell a stranger that an Unshelve is here, and it would let the stranger make
the bot send messages.

The bot writes only in a private chat, and only as an answer. It writes nothing in a channel
(docs/adr/0009).
"""

import structlog
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth_store
from unshelve.bot.capture import DATABASE_IS_DOWN
from unshelve.config import Settings

log = structlog.get_logger()

HELP = (
    "This is your Unshelve bot.\n"
    "\n"
    "To save something, post it in your channel. I store each post of the channel.\n"
    "\n"
    "/login gives you a link to sign in to the web app.\n"
    "/help shows this text."
)
UNKNOWN = "I do not know this command. /help shows what I can do."
NO_LINK_NOW = (
    "I cannot make a sign-in link now, because the database does not answer. Try again in a minute."
)


def from_the_owner(message: Message, settings: Settings) -> bool:
    """Is this message from the one person that the bot answers?"""
    sender = message.from_user
    return (
        sender is not None
        and settings.owner_telegram_id is not None
        and sender.id == settings.owner_telegram_id
    )


def login_text(link: str) -> str:
    minutes = int(auth_store.LOGIN_TOKEN_TTL.total_seconds() // 60)
    # The address stands alone in a line of plain text. Telegram makes a link of it when it
    # can. An address on this computer (localhost) stays text that the owner can copy.
    return f"Open this link to sign in. It works one time, for {minutes} minutes.\n\n{link}"


async def on_login(
    message: Message, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Send a one-time sign-in link (docs/adr/0008)."""
    if settings.owner_telegram_id is None:  # the filter of the router lets no such message in
        return
    try:
        async with sessions() as session:
            owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
            token = await auth_store.issue_login_token(session, owner_id)
            await session.commit()
    except DATABASE_IS_DOWN as error:
        log.error("login.database_down", reason=type(error).__name__)
        await message.answer(NO_LINK_NOW)
        return
    link = f"{settings.web_base_url.rstrip('/')}/login#token={token}"
    await message.answer(
        login_text(link),
        # Telegram opens the addresses in a message to make a small picture of the page. The
        # secret is after "#", which is never sent to a server. No picture is asked for too.
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        parse_mode=None,
    )
    # The link is a secret for five minutes. It is never in a log line.
    log.info("login.link_sent")


async def on_help(message: Message) -> None:
    await message.answer(HELP, parse_mode=None)


async def on_other(message: Message) -> None:
    await message.answer(UNKNOWN, parse_mode=None)


async def on_stranger(message: Message) -> None:
    """A private message of a person who is not the owner: no answer."""
    # No name and no id in the log: a stranger did not ask to be in the log of this instance.
    log.info("private.ignored", reason="not the owner")


def build_router() -> Router:
    """A router with the handlers of the private chat. Each dispatcher gets a new one."""
    router = Router(name="private")
    in_private = F.chat.type == ChatType.PRIVATE

    owner = Router(name="private.owner")
    owner.message.filter(in_private, from_the_owner)
    # A command with the name of the bot ("/login@some_bot") is for a group. In a private
    # chat the name is not necessary, and the bot does not ask Telegram for its own name.
    owner.message.register(on_login, Command("login", ignore_mention=True))
    owner.message.register(on_help, Command("start", "help", ignore_mention=True))
    owner.message.register(on_other)
    router.include_router(owner)

    strangers = Router(name="private.strangers")
    strangers.message.filter(in_private)
    strangers.message.register(on_stranger)
    router.include_router(strangers)
    return router
