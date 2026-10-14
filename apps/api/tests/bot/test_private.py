"""The private chat with the bot. Each test name is one rule.

No message goes to Telegram. The bot of these tests has a pretend connection, which keeps the
messages that the bot wanted to send.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import sqlalchemy as sa
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage, TelegramMethod
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from tests.bot.support import (
    CHANNEL,
    OWNER_TELEGRAM_ID,
    PretendTelegram,
    offline_bot,
    raw_updates,
)
from unshelve.auth import store as auth_store
from unshelve.bot import cards, private
from unshelve.bot.runner import build_dispatcher
from unshelve.config import Settings
from unshelve.contracts.enums import ItemStatus
from unshelve.db.models import AuthToken, Capture, Event, Item

Sessions = async_sessionmaker[AsyncSession]

STRANGER = 222222222
GROUP = -1005555555555


@pytest.fixture
def telegram() -> PretendTelegram:
    return PretendTelegram()


@pytest.fixture
def settings(private_database_url: str) -> Settings:
    return Settings(
        env="test",
        database_url=private_database_url,
        owner_telegram_id=OWNER_TELEGRAM_ID,
        capture_chat_ids=[CHANNEL],
    )


@pytest.fixture
def dispatcher(settings: Settings, committed_db: Sessions) -> Dispatcher:
    return build_dispatcher(settings, committed_db)


def _message(
    text: str, *, sender: int = OWNER_TELEGRAM_ID, chat: int | None = None, kind: str = "private"
) -> dict[str, Any]:
    """An update with one message, in the shape that Telegram sends (tests/bot/fixtures)."""
    return {
        "update_id": 500,
        "message": {
            "message_id": 21,
            "from": {"id": sender, "is_bot": False, "first_name": "Somebody"},
            "chat": {"id": chat if chat is not None else sender, "type": kind},
            "date": 1791921712,
            "text": text,
        },
    }


async def _arrive(dispatcher: Dispatcher, telegram: PretendTelegram, raw: dict[str, Any]) -> None:
    bot = offline_bot(telegram)
    await dispatcher.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))


def _link(sent: SendMessage) -> str:
    return sent.text.splitlines()[-1]


async def _tokens(sessions: Sessions) -> int:
    async with sessions() as db:
        return (await db.execute(sa.select(sa.func.count()).select_from(AuthToken))).scalar_one()


async def test_login_gives_the_owner_a_link_that_signs_in_one_time(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    await _arrive(dispatcher, telegram, _message("/login"))

    (sent,) = telegram.sent
    assert sent.chat_id == OWNER_TELEGRAM_ID
    assert sent.text.startswith("Open this link to sign in. It works one time, for 5 minutes.")
    address, _, token = _link(sent).partition("#token=")
    assert address == "http://localhost:3000/login"
    async with committed_db() as db:
        owner_id = await auth_store.ensure_owner(db, OWNER_TELEGRAM_ID)
        assert await auth_store.consume_login_token(db, token) == owner_id
        assert await auth_store.consume_login_token(db, token) is None
        await db.commit()


async def test_the_message_with_the_link_asks_for_no_preview_and_is_plain_text(
    dispatcher: Dispatcher, telegram: PretendTelegram
) -> None:
    await _arrive(dispatcher, telegram, _message("/login"))

    (sent,) = telegram.sent
    # A preview is a small picture of the page, which Telegram makes by opening the address.
    assert isinstance(sent.link_preview_options, LinkPreviewOptions)
    assert sent.link_preview_options.is_disabled is True
    assert sent.parse_mode is None
    assert sent.reply_markup is None


@pytest.mark.parametrize(
    "text", ["/login", "/start", "/help", "/digest", "hello", "/search queues"]
)
async def test_a_stranger_gets_no_answer_and_no_link(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions, text: str
) -> None:
    with capture_logs() as logs:
        await _arrive(dispatcher, telegram, _message(text, sender=STRANGER))

    assert telegram.sent == []
    assert await _tokens(committed_db) == 0
    # The log says that somebody wrote, and not who.
    assert [entry["event"] for entry in logs] == ["private.ignored"]
    assert str(STRANGER) not in str(logs)


async def test_the_owner_gets_no_link_in_a_group(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    # In a group, each member sees the answer of the bot.
    await _arrive(dispatcher, telegram, _message("/login", chat=GROUP, kind="supergroup"))

    assert telegram.sent == []
    assert await _tokens(committed_db) == 0


@pytest.mark.parametrize("text", ["/login", "/start", "hello"])
async def test_nobody_gets_an_answer_when_no_owner_is_set(
    private_database_url: str, committed_db: Sessions, telegram: PretendTelegram, text: str
) -> None:
    settings = Settings(env="test", database_url=private_database_url, capture_chat_ids=[CHANNEL])

    await _arrive(build_dispatcher(settings, committed_db), telegram, _message(text))

    assert telegram.sent == []
    assert await _tokens(committed_db) == 0


@pytest.mark.parametrize("text", ["/start", "/help"])
async def test_start_and_help_say_what_the_bot_does(
    dispatcher: Dispatcher, telegram: PretendTelegram, text: str
) -> None:
    await _arrive(dispatcher, telegram, _message(text))

    (sent,) = telegram.sent
    assert sent.text == private.HELP
    assert "/login" in sent.text
    assert sent.parse_mode is None


@pytest.mark.parametrize("text", ["hello", "/search queues", "https://example.com/a-page"])
async def test_a_message_that_the_bot_does_not_know_gets_a_hint(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions, text: str
) -> None:
    await _arrive(dispatcher, telegram, _message(text))

    (sent,) = telegram.sent
    assert sent.text == private.UNKNOWN
    # A link in the private chat is not a save. Only a post in the channel is.
    async with committed_db() as db:
        assert (await db.execute(sa.select(sa.func.count()).select_from(Capture))).scalar_one() == 0


async def test_login_with_the_name_of_the_bot_works_too(
    dispatcher: Dispatcher, telegram: PretendTelegram
) -> None:
    await _arrive(dispatcher, telegram, _message("/login@mattsh_unshelve_bot"))

    (sent,) = telegram.sent
    assert "/login#token=" in sent.text


async def test_each_login_gives_a_new_link_and_the_two_work(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    await _arrive(dispatcher, telegram, _message("/login"))
    await _arrive(dispatcher, telegram, _message("/login"))

    first, second = (_link(sent).partition("#token=")[2] for sent in telegram.sent)
    assert first != second
    async with committed_db() as db:
        assert await auth_store.consume_login_token(db, first) is not None
        assert await auth_store.consume_login_token(db, second) is not None


async def test_the_link_uses_the_address_of_the_web_app_from_the_settings(
    private_database_url: str, committed_db: Sessions, telegram: PretendTelegram
) -> None:
    settings = Settings(
        env="test",
        database_url=private_database_url,
        owner_telegram_id=OWNER_TELEGRAM_ID,
        web_base_url="https://unshelve.example/",
    )

    await _arrive(build_dispatcher(settings, committed_db), telegram, _message("/login"))

    (sent,) = telegram.sent
    assert _link(sent).startswith("https://unshelve.example/login#token=")


async def test_the_link_is_not_in_the_log(
    dispatcher: Dispatcher, telegram: PretendTelegram
) -> None:
    with capture_logs() as logs:
        await _arrive(dispatcher, telegram, _message("/login"))

    token = _link(telegram.sent[0]).partition("#token=")[2]
    assert [entry["event"] for entry in logs] == ["login.link_sent"]
    assert token not in str(logs)


async def test_a_database_that_is_down_gives_an_excuse_and_no_link(
    settings: Settings, telegram: PretendTelegram
) -> None:
    def down() -> AsyncSession:
        raise OperationalError("SELECT 1", {}, ConnectionError("the database is down"))

    dispatcher = build_dispatcher(settings, cast(Sessions, down))

    with capture_logs() as logs:
        await _arrive(dispatcher, telegram, _message("/login"))

    (sent,) = telegram.sent
    assert sent.text == private.NO_LINK_NOW
    assert [entry["event"] for entry in logs] == ["login.database_down"]


async def test_a_channel_post_with_the_word_login_is_a_save_and_gets_no_answer(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    post = {
        "update_id": 501,
        "channel_post": {
            "message_id": 30,
            "sender_chat": {"id": CHANNEL, "title": "Unshelve test", "type": "channel"},
            "chat": {"id": CHANNEL, "title": "Unshelve test", "type": "channel"},
            "date": 1791921712,
            "text": "/login",
        },
    }

    await _arrive(dispatcher, telegram, post)

    assert telegram.sent == []
    assert await _tokens(committed_db) == 0
    async with committed_db() as db:
        assert (await db.execute(sa.select(Capture.text))).scalars().all() == ["/login"]


async def test_the_recorded_start_of_the_owner_gets_the_help_text(
    dispatcher: Dispatcher, telegram: PretendTelegram
) -> None:
    # The first line of the recorded updates: the owner pressed "Start" in the chat of the bot.
    start = next(update for update in raw_updates("seen") if "message" in update)

    await _arrive(dispatcher, telegram, start)

    (sent,) = telegram.sent
    assert (sent.chat_id, sent.text) == (OWNER_TELEGRAM_ID, private.HELP)


# --- The digest and its buttons ---------------------------------------------------------------


async def _saved(sessions: Sessions, *titles: str, days_ago: float = 1) -> list[int]:
    """Items of the owner, saved some days ago. Returns their ids, the oldest first."""
    at = datetime.now(UTC) - timedelta(days=days_ago)
    async with sessions() as db:
        owner_id = await auth_store.ensure_owner(db, OWNER_TELEGRAM_ID)
        ids = [
            (
                await db.execute(
                    sa.insert(Item)
                    .values(
                        owner_id=owner_id,
                        title=title,
                        canonical_url=f"https://example.com/{number}",
                        first_captured_at=at + timedelta(minutes=number),
                        last_captured_at=at + timedelta(minutes=number),
                    )
                    .returning(Item.id)
                )
            ).scalar_one()
            for number, title in enumerate(titles)
        ]
        await db.commit()
    return ids


def _press(data: str, *, sender: int = OWNER_TELEGRAM_ID, markup: Any = None) -> dict[str, Any]:
    """An update with a button press under a message of the bot."""
    message: dict[str, Any] = {
        "message_id": 1001,
        "chat": {"id": sender, "type": "private"},
        "date": 1791921712,
        "text": "Your digest",
    }
    if markup is not None:
        message["reply_markup"] = markup.model_dump(exclude_none=True)
    return {
        "update_id": 600,
        "callback_query": {
            "id": "4140561234567890",
            "from": {"id": sender, "is_bot": False, "first_name": "Somebody"},
            "message": message,
            "chat_instance": "-1234567890123456789",
            "data": data,
        },
    }


async def _item(sessions: Sessions, item_id: int) -> Item:
    async with sessions() as db:
        return await db.get_one(Item, item_id)


async def _item_status(sessions: Sessions, item_id: int) -> ItemStatus:
    async with sessions() as db:
        return (await db.get_one(Item, item_id)).status


async def _events(sessions: Sessions) -> list[tuple[int, str | None]]:
    async with sessions() as db:
        rows = await db.execute(sa.select(Event.item_id, Event.to_status).order_by(Event.id))
        return [(row.item_id, row.to_status) for row in rows]


async def test_digest_shows_the_owner_the_recent_things_with_their_buttons(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    first, second = await _saved(committed_db, "Vector search in Postgres", "Queues & <locks>")

    await _arrive(dispatcher, telegram, _message("/digest"))

    (sent,) = telegram.sent
    assert sent.parse_mode == "HTML"
    assert sent.text.startswith("<b>Your digest</b>\nYou saved 2 things in the last 7 days.")
    assert "<b>Queues &amp; &lt;locks&gt;</b>" in sent.text  # the newest first, escaped
    assert sent.text.index("Queues") < sent.text.index("Vector search")
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    done = [row[-1].callback_data for row in sent.reply_markup.inline_keyboard]
    assert done == [f"done:{second}", f"done:{first}"]
    assert isinstance(sent.link_preview_options, LinkPreviewOptions)
    assert sent.link_preview_options.is_disabled is True


async def test_digest_without_saved_things_says_how_to_start(
    dispatcher: Dispatcher, telegram: PretendTelegram
) -> None:
    await _arrive(dispatcher, telegram, _message("/digest"))

    (sent,) = telegram.sent
    assert sent.text == cards.NOTHING_YET


async def test_the_done_button_marks_the_item_and_then_goes_away(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    first, second = await _saved(committed_db, "One", "Two")
    markup = cards.buttons([await _item(committed_db, second), await _item(committed_db, first)])

    await _arrive(dispatcher, telegram, _press(f"done:{first}", markup=markup))

    answer, edit = telegram.calls
    # The answer comes first: Telegram forgets a press that gets no answer.
    assert isinstance(answer, AnswerCallbackQuery)
    assert answer.text is None
    assert isinstance(edit, EditMessageReplyMarkup)
    assert isinstance(edit.reply_markup, InlineKeyboardMarkup)
    fewer = cards.without_button(markup, f"done:{first}")
    assert fewer is not None
    assert edit.reply_markup.model_dump(exclude_none=True) == fewer.model_dump(exclude_none=True)
    assert await _item_status(committed_db, first) is ItemStatus.DONE
    assert await _item_status(committed_db, second) is ItemStatus.INBOX
    assert await _events(committed_db) == [(first, "done")]


async def test_a_button_press_of_a_stranger_is_refused(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    (item_id,) = await _saved(committed_db, "One")
    markup = cards.buttons([await _item(committed_db, item_id)])

    with capture_logs() as logs:
        await _arrive(
            dispatcher, telegram, _press(f"done:{item_id}", sender=STRANGER, markup=markup)
        )

    # The press gets an answer without a text, so that the spinner stops. Nothing else.
    (answer,) = telegram.calls
    assert isinstance(answer, AnswerCallbackQuery)
    assert answer.text is None
    assert await _item_status(committed_db, item_id) is ItemStatus.INBOX
    assert await _events(committed_db) == []
    assert [entry["event"] for entry in logs] == ["button.ignored"]
    assert str(STRANGER) not in str(logs)


async def test_a_second_press_changes_nothing(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    item_id, other = await _saved(committed_db, "One", "Two")
    markup = cards.buttons([await _item(committed_db, item_id), await _item(committed_db, other)])
    await _arrive(dispatcher, telegram, _press(f"done:{item_id}", markup=markup))

    # The phone showed the old message, and the owner pressed again. The press brings the
    # message as Telegram has it now: without the button.
    after = cards.without_button(markup, f"done:{item_id}")
    await _arrive(dispatcher, telegram, _press(f"done:{item_id}", markup=after))

    assert await _events(committed_db) == [(item_id, "done")]
    kinds = [type(call).__name__ for call in telegram.calls]
    assert kinds == ["AnswerCallbackQuery", "EditMessageReplyMarkup", "AnswerCallbackQuery"]


async def test_a_press_on_a_message_that_has_no_such_button_changes_no_buttons(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    (item_id,) = await _saved(committed_db, "One")

    # Telegram refuses a change of buttons that changes nothing ("message is not modified").
    await _arrive(dispatcher, telegram, _press(f"done:{item_id}", markup=None))

    assert [type(call).__name__ for call in telegram.calls] == ["AnswerCallbackQuery"]
    assert await _item_status(committed_db, item_id) is ItemStatus.DONE


@pytest.mark.parametrize("data", ["spike:link", "done:", "done:abc", "something else"])
async def test_a_button_that_the_bot_does_not_know_is_answered_and_ignored(
    dispatcher: Dispatcher, telegram: PretendTelegram, committed_db: Sessions, data: str
) -> None:
    await _arrive(dispatcher, telegram, _press(data))

    assert [type(call).__name__ for call in telegram.calls] == ["AnswerCallbackQuery"]
    assert await _events(committed_db) == []


async def test_a_database_that_is_down_keeps_the_button(
    settings: Settings, telegram: PretendTelegram
) -> None:
    def down() -> AsyncSession:
        raise OperationalError("SELECT 1", {}, ConnectionError("the database is down"))

    markup = cards.buttons([Item(id=7, canonical_url=None, title="One", capture_count=1)])

    with capture_logs() as logs:
        await _arrive(
            build_dispatcher(settings, cast(Sessions, down)),
            telegram,
            _press("done:7", markup=markup),
        )

    assert [type(call).__name__ for call in telegram.calls] == ["AnswerCallbackQuery"]
    assert [entry["event"] for entry in logs] == ["button.database_down"]


class RefusingTelegram(PretendTelegram):
    """Says "message is not modified" to each change of buttons, as the real one can."""

    async def make_request[T](
        self,
        bot: Bot,
        method: TelegramMethod[T],
        timeout: int | None = None,  # noqa: ASYNC109 - the signature of the library
    ) -> T:
        if isinstance(method, EditMessageReplyMarkup):
            self.calls.append(method)
            raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
        return await super().make_request(bot, method, timeout)


async def test_a_change_of_buttons_that_telegram_refuses_is_logged_and_not_an_error(
    dispatcher: Dispatcher, committed_db: Sessions
) -> None:
    telegram = RefusingTelegram()
    (item_id,) = await _saved(committed_db, "One")
    markup = cards.buttons([await _item(committed_db, item_id)])

    with capture_logs() as logs:
        await _arrive(dispatcher, telegram, _press(f"done:{item_id}", markup=markup))

    assert await _item_status(committed_db, item_id) is ItemStatus.DONE
    assert [entry["event"] for entry in logs] == ["button.done", "button.not_changed"]
