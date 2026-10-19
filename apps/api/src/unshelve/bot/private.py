"""The private chat with the bot: the owner writes a command, and the bot answers.

A bot has a public name, so anybody can write to it. This bot answers one person: the owner
(``UNSHELVE_OWNER_TELEGRAM_ID``). A message of any other person gets no answer at all. An
answer would tell a stranger that an Unshelve is here, and it would let the stranger make
the bot send messages.

The bot writes only in a private chat, and only as an answer. It writes nothing in a channel
(docs/adr/0009).
"""

import html
from datetime import UTC, datetime

import structlog
from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth_store
from unshelve.bot import cards
from unshelve.bot.capture import DATABASE_IS_DOWN
from unshelve.config import Settings
from unshelve.contracts.enums import Actor, GoalLabel, ItemStatus, LabelSource
from unshelve.relevance import labels
from unshelve.resurface import plan, stale
from unshelve.track import status

log = structlog.get_logger()

HELP = (
    "This is your Unshelve bot.\n"
    "\n"
    "To save something, post it in your channel. I store each post of the channel.\n"
    "\n"
    "/digest shows what a digest would bring back now: things for your goals first, then things "
    "that you saved again or lately, and one random pick. Each week I send you one.\n"
    "/label asks you about the scores against your goals, one at a time: is the score right?\n"
    "/login gives you a link to sign in to the web app.\n"
    "/help shows this text."
)
UNKNOWN = "I do not know this command. /help shows what I can do."
NO_LINK_NOW = (
    "I cannot make a sign-in link now, because the database does not answer. Try again in a minute."
)
NO_DIGEST_NOW = (
    "I cannot read your items now, because the database does not answer. Try again in a minute."
)
NO_LABEL_NOW = (
    "I cannot read your scores now, because the database does not answer. Try again in a minute."
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


async def on_digest(
    message: Message, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Show the digest of now, as the weekly digest would choose it (note 34). Nothing is
    recorded: the items are not marked as shown."""
    if settings.owner_telegram_id is None:  # the filter of the router lets no such message in
        return
    now = datetime.now(UTC)
    try:
        async with sessions() as session:
            owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
            # The random pick changes once a day, not with each /digest; the page "Digest" of
            # the web app uses the same seed.
            planned = await plan.plan_digest(
                session,
                owner_id,
                now=now,
                seed=f"{owner_id}:{now:%Y-%m-%d}",
                minutes=settings.digest_minutes,
            )
            await session.commit()
    except DATABASE_IS_DOWN as error:
        log.error("digest.database_down", reason=type(error).__name__)
        await message.answer(NO_DIGEST_NOW, parse_mode=None)
        return
    if planned.empty:
        await message.answer(cards.NOTHING_YET, parse_mode=None)
        return
    text, keyboard = cards.digest_message(planned, now=now)
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    log.info("digest.shown", items=len(planned.picks))


async def on_done(
    callback: CallbackQuery, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The button "Done" under a card: the item is read."""
    # Telegram waits for the answer to a press, and it forgets the press after some seconds
    # (docs/adr/0009). So the answer comes first. It has no text: the button that goes away
    # is the answer that the owner sees.
    await callback.answer()
    if settings.owner_telegram_id is None or callback.from_user.id != settings.owner_telegram_id:
        # Only the owner gets the buttons, but a message can be forwarded with its buttons.
        log.info("button.ignored", reason="not the owner")
        return
    data = callback.data or ""
    number = data.partition(":")[2]
    if not number.isdigit():
        log.info("button.ignored", reason="no item in the button")
        return
    try:
        async with sessions() as session:
            owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
            change = await status.change_status(
                session, owner_id, int(number), ItemStatus.DONE, actor=Actor.USER, via="telegram"
            )
            if change.duplicate_of is not None:
                # The card shows an item that was merged into an older item later. The owner
                # read the thing, so the older item, which the lists show, is done.
                change = await status.change_status(
                    session,
                    owner_id,
                    change.duplicate_of,
                    ItemStatus.DONE,
                    actor=Actor.USER,
                    via="telegram",
                )
            await session.commit()
    except DATABASE_IS_DOWN as error:
        # The button stays. The owner can press it again.
        log.error("button.database_down", reason=type(error).__name__)
        return
    log.info("button.done", item_id=int(number), outcome=str(change.outcome))
    message = callback.message
    # A message older than 48 hours cannot be changed. And Telegram refuses a change that
    # changes nothing: after a second press, the message of Telegram has no such button.
    if isinstance(message, Message) and cards.has_button(message.reply_markup, data):
        try:
            await message.edit_reply_markup(
                reply_markup=cards.without_button(message.reply_markup, data)
            )
        except TelegramBadRequest as error:
            log.warning("button.not_changed", reason=error.message)


async def _ask(message: Message, found: labels.ToLabel | None) -> None:
    """Send the question about one score, or say that no score waits."""
    if found is None:
        await message.answer(cards.NOTHING_TO_LABEL, parse_mode=None)
        return
    await message.answer(
        cards.label_question(found.item, found.goal.title, found.score, found.reason),
        parse_mode=ParseMode.HTML,
        reply_markup=cards.label_buttons(found.item, found.goal.id, found.score),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def on_label(
    message: Message, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Ask about one score: is it right? The verdicts go to the golden set (note 33)."""
    if settings.owner_telegram_id is None:  # the filter of the router lets no such message in
        return
    try:
        async with sessions() as session:
            owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
            found = await labels.next_to_label(
                session, owner_id, seed=datetime.now(UTC).isoformat()
            )
            await session.commit()
    except DATABASE_IS_DOWN as error:
        log.error("label.database_down", reason=type(error).__name__)
        await message.answer(NO_LABEL_NOW, parse_mode=None)
        return
    await _ask(message, found)


async def on_verdict(
    callback: CallbackQuery, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """A button of a verdict: "Right", "Wrong" or "Skip". Then the next question comes."""
    await callback.answer()  # first: Telegram forgets a press without an answer (docs/adr/0009)
    if settings.owner_telegram_id is None or callback.from_user.id != settings.owner_telegram_id:
        log.info("button.ignored", reason="not the owner")
        return
    data = callback.data or ""
    parsed = cards.parse_label(data)
    message = callback.message
    if parsed is None:
        log.info("button.ignored", reason="no score in the button")
        return
    # The press brings the question as Telegram has it now. Without this button, the question
    # was answered already, and the press came from an old view of it: it changes nothing. A
    # question older than 48 hours comes without its content: /label asks again.
    if not isinstance(message, Message) or not cards.has_button(message.reply_markup, data):
        log.info("button.ignored", reason="answered or too old")
        return
    item_id, goal_id, score, verdict = parsed
    try:
        async with sessions() as session:
            owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
            line = cards.VERDICTS[verdict]
            if verdict != "skip":
                label = GoalLabel.CONFIRMED if verdict == "right" else GoalLabel.REJECTED
                change = await labels.set_label(
                    session,
                    owner_id,
                    item_id,
                    goal_id,
                    label,
                    source=LabelSource.DM,
                    shown_score=score,
                )
                if change.row is None:
                    line = "This score does not exist any more."
                elif change.outcome is labels.LabelOutcome.SCORE_CHANGED:
                    line = f"The score changed to {change.row.score} since: I ask about it again."
            following = await labels.next_to_label(
                session,
                owner_id,
                seed=datetime.now(UTC).isoformat(),
                skip=(item_id, goal_id) if verdict == "skip" else None,
            )
            await session.commit()
    except DATABASE_IS_DOWN as error:
        # The buttons stay. The owner can press again.
        log.error("button.database_down", reason=type(error).__name__)
        return
    log.info("button.verdict", item_id=item_id, goal_id=goal_id, verdict=verdict)
    try:
        await message.edit_text(
            f"{message.html_text}\n\n<i>{html.escape(line, quote=False)}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=cards.links_only(message.reply_markup),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramBadRequest as error:
        log.warning("button.not_changed", reason=error.message)
    await _ask(message, following)


async def _note(message: Message, line: str, markup: InlineKeyboardMarkup | None) -> None:
    """Add a line in italics at the end of a message, and give it new buttons."""
    try:
        await message.edit_text(
            f"{message.html_text}\n\n<i>{html.escape(line, quote=False)}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramBadRequest as error:
        log.warning("button.not_changed", reason=error.message)


def _usable_press(callback: CallbackQuery, settings: Settings) -> Message | None:
    """The message of a press of the owner on a button that the message still has, or None.
    A press on a button that is gone came from an old view of the message: it changes nothing.
    A message older than 48 hours comes without its content."""
    if settings.owner_telegram_id is None or callback.from_user.id != settings.owner_telegram_id:
        log.info("button.ignored", reason="not the owner")
        return None
    message = callback.message
    if not isinstance(message, Message) or not cards.has_button(
        message.reply_markup, callback.data or ""
    ):
        log.info("button.ignored", reason="answered or too old")
        return None
    return message


async def on_digest_verdict(
    callback: CallbackQuery, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """ "Right" or "Wrong" under a pick of a digest: a verdict on the score that the digest
    shows. No next question follows; that is what /label does."""
    await callback.answer()  # first: Telegram forgets a press without an answer (docs/adr/0009)
    message = _usable_press(callback, settings)
    data = callback.data or ""
    parsed = cards.parse_label(data, prefix=cards.DIGEST_LABEL)
    if message is None or settings.owner_telegram_id is None:
        return
    if parsed is None or parsed[3] == "skip":
        log.info("button.ignored", reason="no score in the button")
        return
    item_id, goal_id, score, verdict = parsed
    try:
        async with sessions() as session:
            owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
            change = await labels.set_label(
                session,
                owner_id,
                item_id,
                goal_id,
                GoalLabel.CONFIRMED if verdict == "right" else GoalLabel.REJECTED,
                source=LabelSource.DIGEST,
                shown_score=score,
            )
            await session.commit()
    except DATABASE_IS_DOWN as error:
        # The buttons stay. The owner can press again.
        log.error("button.database_down", reason=type(error).__name__)
        return
    number = cards.number_of(message.reply_markup, data)
    if change.row is None:
        line = f"{number}: this score does not exist any more."
    elif change.outcome is labels.LabelOutcome.SCORE_CHANGED:
        line = f"{number}: the score changed to {change.row.score} since. /label asks about it."
    else:
        line = f"{number}: you said that the score is {verdict}."
    log.info("button.digest_verdict", item_id=item_id, goal_id=goal_id, verdict=verdict)
    await _note(
        message, line, cards.without_item(message.reply_markup, cards.DIGEST_LABEL, item_id)
    )


async def on_stale(
    callback: CallbackQuery, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """A button of the stale prompt. "Archive" moves the thing out of the inbox (nothing is
    deleted); "Keep" leaves it there, and the prompt leaves it alone for 90 days."""
    await callback.answer()  # first: Telegram forgets a press without an answer (docs/adr/0009)
    message = _usable_press(callback, settings)
    data = callback.data or ""
    parsed = cards.parse_stale(data)
    if message is None or settings.owner_telegram_id is None:
        return
    if parsed is None:
        log.info("button.ignored", reason="no item in the button")
        return
    item_id, action = parsed
    try:
        async with sessions() as session:
            owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
            if action == "archive":
                change = await status.change_status(
                    session,
                    owner_id,
                    item_id,
                    ItemStatus.ARCHIVED,
                    actor=Actor.USER,
                    via="telegram",
                )
                if change.duplicate_of is not None:
                    # The thing was merged into an older item since: that one is in the lists.
                    change = await status.change_status(
                        session,
                        owner_id,
                        change.duplicate_of,
                        ItemStatus.ARCHIVED,
                        actor=Actor.USER,
                        via="telegram",
                    )
                found = change.outcome is not status.Outcome.NOT_FOUND
            else:
                found = await stale.keep(session, owner_id, item_id)
            await session.commit()
    except DATABASE_IS_DOWN as error:
        log.error("button.database_down", reason=type(error).__name__)
        return
    number = cards.number_of(message.reply_markup, data)
    if not found:
        line = f"{number}: this thing does not exist any more."
    elif action == "archive":
        line = f"{number}: archived. The web app can bring it back."
    else:
        line = f"{number}: kept in your inbox."
    log.info("button.stale", item_id=item_id, action=action, found=found)
    await _note(message, line, cards.without_item(message.reply_markup, cards.STALE, item_id))


async def on_other_button(callback: CallbackQuery) -> None:
    """A button that this version does not know, for example an old one."""
    await callback.answer()
    log.info("button.ignored", reason="unknown button")


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
    owner.message.register(on_digest, Command("digest", ignore_mention=True))
    owner.message.register(on_label, Command("label", ignore_mention=True))
    owner.message.register(on_other)
    router.include_router(owner)

    pressed = Router(name="private.buttons")
    pressed.callback_query.register(on_done, F.data.startswith(f"{cards.DONE}:"))
    pressed.callback_query.register(on_verdict, F.data.startswith(f"{cards.LABEL}:"))
    pressed.callback_query.register(on_digest_verdict, F.data.startswith(f"{cards.DIGEST_LABEL}:"))
    pressed.callback_query.register(on_stale, F.data.startswith(f"{cards.STALE}:"))
    pressed.callback_query.register(on_other_button)
    router.include_router(pressed)

    strangers = Router(name="private.strangers")
    strangers.message.filter(in_private)
    strangers.message.register(on_stranger)
    router.include_router(strangers)
    return router
