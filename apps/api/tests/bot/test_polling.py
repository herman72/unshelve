"""The promise of the capture: a post is confirmed to Telegram only after it is stored.

The bot library confirms an update when it asks Telegram for the next updates. This test runs
the real polling loop of the library against a scripted Telegram, and looks at the database at
the moment of that second question.
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
import sqlalchemy as sa
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, GetUpdates, TelegramMethod
from aiogram.types import Update, User
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.bot.support import CHANNEL, OWNER_TELEGRAM_ID, updates
from unshelve.bot import capture
from unshelve.bot.runner import build_dispatcher, polling_options
from unshelve.config import Settings
from unshelve.db.models import Capture

Sessions = async_sessionmaker[AsyncSession]


class ScriptedTelegram(BaseSession):
    """Gives one batch of updates. At the next question, it notes how many posts are stored."""

    def __init__(self, batch: list[Update], sessions: Sessions) -> None:
        super().__init__()
        self._batch: list[Update] | None = batch
        self._sessions = sessions
        self.second_question = asyncio.Event()
        self.offset_of_second_question: int | None = None
        self.stored_at_second_question: int | None = None

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - the signature of the library
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise NotImplementedError
        yield b""

    async def make_request[T](
        self,
        bot: Bot,
        method: TelegramMethod[T],
        timeout: int | None = None,  # noqa: ASYNC109 - the signature of the library
    ) -> T:
        if isinstance(method, GetMe):
            return cast(T, User(id=42, is_bot=True, first_name="Unshelve", username="test_bot"))
        if isinstance(method, GetUpdates):
            if self._batch is not None:
                batch, self._batch = self._batch, None
                return cast(T, batch)
            async with self._sessions() as db:
                count = await db.execute(sa.select(sa.func.count()).select_from(Capture))
                self.stored_at_second_question = count.scalar_one()
            self.offset_of_second_question = method.offset
            self.second_question.set()
            await asyncio.Event().wait()  # no more updates: the question stays open
        msg = f"the scripted Telegram does not know {type(method).__name__}"
        raise AssertionError(msg)


class DownForAWhile:
    """A database that does not answer the first times, and then works."""

    def __init__(self, sessions: Sessions, failures: int) -> None:
        self._sessions = sessions
        self._failures_left = failures

    def __call__(self) -> AsyncSession:
        if self._failures_left > 0:
            self._failures_left -= 1
            raise OperationalError("SELECT 1", {}, ConnectionError("the database is down"))
        return self._sessions()


@pytest.mark.parametrize("failures", [0, 3], ids=["database up", "database down at first"])
async def test_the_bot_confirms_a_post_only_after_it_is_stored(
    private_database_url: str,
    committed_db: Sessions,
    monkeypatch: pytest.MonkeyPatch,
    failures: int,
) -> None:
    monkeypatch.setattr(capture, "RETRY_DELAYS", (0.05,))
    settings = Settings(
        env="test",
        database_url=private_database_url,
        owner_telegram_id=OWNER_TELEGRAM_ID,
        capture_chat_ids=[CHANNEL],
    )
    link_post = next(u for u in updates("seen") if u.update_id == 104)
    telegram = ScriptedTelegram([link_post], committed_db)
    bot = Bot("42:TEST", session=telegram)
    dispatcher = build_dispatcher(settings, cast(Sessions, DownForAWhile(committed_db, failures)))

    polling = asyncio.ensure_future(
        dispatcher.start_polling(  # pyright: ignore[reportUnknownMemberType]
            bot, handle_signals=False, **polling_options()
        )
    )
    try:
        await asyncio.wait_for(telegram.second_question.wait(), timeout=10)
    finally:
        await dispatcher.stop_polling()
        await asyncio.wait_for(polling, timeout=10)

    # "Anything after update 104?" tells Telegram that the bot has update 104.
    assert telegram.offset_of_second_question == 105
    # At that moment the post was in the database, also when the database was down at first.
    assert telegram.stored_at_second_question == 1
