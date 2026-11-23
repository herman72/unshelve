"""The sign of life of the bot (unshelve.alive, docs/learn/55).

The polling loop of aiogram catches each error and asks again, for ever. So a bot that cannot
reach Telegram, or whose token was revoked, still runs. Only an answer of getUpdates is a sign
that the bot still receives. These tests use a Telegram that is not real (tests/bot/support.py).
"""

import asyncio
import itertools
import os
import signal
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetChatMember, GetMe, GetUpdates, SendMessage, TelegramMethod
from aiogram.types import ChatMemberAdministrator, ChatMemberLeft, User
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.bot.support import OWNER_TELEGRAM_ID, PretendTelegram, offline_bot
from unshelve import alive
from unshelve.bot import runner
from unshelve.config import Settings

ME = User(id=2222222222, is_bot=True, first_name="Unshelve", username="example_unshelve_bot")
ADMIN_OF, MEMBER_OF = -1001, -1002


class PollingTelegram(PretendTelegram):
    """Also answers the calls of the start and of the polling: who the bot is, its chats, and
    getUpdates. getUpdates gives no update, or raises ``fail`` as aiogram does for a failure."""

    def __init__(self, fail: type[TelegramAPIError] | None = None) -> None:
        super().__init__()
        self.fail = fail
        self.polls = 0

    async def make_request[T](
        self,
        bot: Bot,
        method: TelegramMethod[T],
        timeout: int | None = None,  # noqa: ASYNC109 - the signature of the library
    ) -> T:
        if isinstance(method, GetUpdates):
            self.polls += 1
            await asyncio.sleep(0.01)  # a long poll of Telegram waits up to 10 s
            if self.fail is not None:
                raise self.fail(method=method, message="the request failed")
            return cast(T, [])
        if isinstance(method, GetMe):
            return cast(T, ME)
        if isinstance(method, GetChatMember):
            if method.chat_id == ADMIN_OF:
                return cast(T, ChatMemberAdministrator.model_construct(status="administrator"))
            if method.chat_id == MEMBER_OF:
                return cast(T, ChatMemberLeft(user=ME))
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        return await super().make_request(bot, method, timeout)


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    place = tmp_path / "alive"
    monkeypatch.setenv("UNSHELVE_ALIVE_DIR", str(place))
    return place


def watched(telegram: PollingTelegram) -> tuple[Bot, runner.MarkAlive]:
    """A bot that never talks to Telegram, with the middleware of run_bot."""
    bot = offline_bot(telegram)
    middleware = runner.MarkAlive()
    bot.session.middleware(middleware)
    return bot, middleware


def fields(file: Path) -> dict[str, str]:
    lines = file.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)


async def until(condition: Callable[[], bool], seconds: float = 5) -> None:
    """Wait until ``condition()`` is true, at most ``seconds``. A file gives no event, so the
    test looks again every 10 ms."""
    try:
        async with asyncio.timeout(seconds):
            for _ in itertools.count():
                if condition():
                    return
                await asyncio.sleep(0.01)
    except TimeoutError:
        pytest.fail(f"not true after {seconds} s")


# --- The middleware ----------------------------------------------------------------------


async def test_an_answer_of_get_updates_marks_the_file(folder: Path) -> None:
    bot, _ = watched(PollingTelegram())

    assert await bot(GetUpdates(timeout=10)) == []

    written = fields(folder / "bot")
    assert sorted(written) == ["pid", "started"]
    assert written["pid"] == str(os.getpid())


@pytest.mark.parametrize("fail", [TelegramNetworkError, TelegramUnauthorizedError])
async def test_a_get_updates_that_failed_does_not_mark(
    folder: Path, fail: type[TelegramAPIError]
) -> None:
    # A network that is down, or a token that was revoked: aiogram asks again for ever.
    bot, _ = watched(PollingTelegram(fail))

    with pytest.raises(fail):
        await bot(GetUpdates(timeout=10))

    assert not (folder / "bot").exists()


async def test_a_sent_message_does_not_mark(folder: Path) -> None:
    telegram = PollingTelegram()
    bot, _ = watched(telegram)

    await bot.send_message(OWNER_TELEGRAM_ID, "hello")

    assert [type(call) for call in telegram.calls] == [SendMessage]
    assert not (folder / "bot").exists()


async def test_the_ready_chats_go_into_the_file_with_the_next_mark(folder: Path) -> None:
    bot, middleware = watched(PollingTelegram())
    middleware.chats_ready = "1/2"

    await bot(GetUpdates(timeout=10))

    assert fields(folder / "bot")["chats_ready"] == "1/2"


# --- The whole run, from the start to a clean stop ---------------------------------------


async def test_the_bot_marks_its_chats_and_a_clean_stop(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    telegram = PollingTelegram()

    def connect(token: str) -> Bot:
        return offline_bot(telegram)

    monkeypatch.setattr(runner, "Bot", connect)
    dispatchers: list[Dispatcher] = []
    build = runner.build_dispatcher

    def keep(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> Dispatcher:
        dispatchers.append(build(settings, sessions))
        return dispatchers[-1]

    monkeypatch.setattr(runner, "build_dispatcher", keep)
    settings = Settings(
        env="test",
        telegram_bot_token=SecretStr("42:TEST"),
        capture_chat_ids=[ADMIN_OF, MEMBER_OF],
    )
    loop = asyncio.get_running_loop()

    def marked() -> bool:
        return (folder / "bot").exists() and "chats_ready" in fields(folder / "bot")

    run = asyncio.create_task(runner.run_bot(settings, async_sessionmaker()))
    try:
        await until(marked)
        # What a SIGTERM of `docker compose stop` does, without a signal to the test process.
        await dispatchers[0].stop_polling()
        await asyncio.wait_for(run, timeout=5)
    finally:
        run.cancel()
        # aiogram takes SIGINT and SIGTERM while it polls, and keeps them. Give them back, so
        # that Ctrl-C stops the tests again.
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)

    written = fields(folder / "bot")
    # One of the two capture chats is ready: the bot is an administrator there.
    assert written["chats_ready"] == "1/2"
    assert int(written["stopped"]) >= int(written["started"])
    assert telegram.polls >= 1


# --- A bot without a token ---------------------------------------------------------------


async def test_an_idle_bot_marks_at_once_and_a_clean_stop(folder: Path) -> None:
    # The stack of CI has no token, and `docker compose up --wait` waits for the healthcheck.
    # A first mark only after 30 s would keep the stack waiting.
    stop = asyncio.Event()
    idle = asyncio.create_task(runner.idle(stop))
    try:
        await until((folder / "bot").exists, seconds=2)
        stop.set()
        await asyncio.wait_for(idle, timeout=5)
    finally:
        idle.cancel()

    assert "stopped" in fields(folder / "bot")


async def test_an_idle_bot_marks_again_on_its_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    marks: list[str] = []

    def record(name: str, **facts: Any) -> bool:
        marks.append(name)
        return True

    monkeypatch.setattr(alive, "mark", record)
    stop = asyncio.Event()
    idle = asyncio.create_task(runner.idle(stop, every=timedelta(seconds=0.01)))
    try:
        await until(lambda: len(marks) >= 3)
    finally:
        stop.set()
        await asyncio.wait_for(idle, timeout=5)

    assert set(marks) == {"bot"}
