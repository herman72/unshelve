"""The start of the bot: its setup check, and the token that must stay out of the logs."""

import logging
from typing import cast

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from aiogram.types import ChatMemberAdministrator, ChatMemberLeft, User
from structlog.testing import capture_logs

from unshelve.bot.runner import ALLOWED_UPDATES, HideToken, check_capture_chats
from unshelve.config import Settings

ME = User(id=2222222222, is_bot=True, first_name="Unshelve", username="example_unshelve_bot")
ADMIN_OF, MEMBER_OF, UNKNOWN = -1001, -1002, -1003


class FakeTelegram:
    """Answers the two questions that the setup check asks."""

    async def get_me(self) -> User:
        return ME

    async def get_chat_member(self, chat_id: int, user_id: int) -> object:
        if chat_id == ADMIN_OF:
            return ChatMemberAdministrator.model_construct(status="administrator", user=ME)
        if chat_id == MEMBER_OF:
            return ChatMemberLeft(user=ME)
        raise TelegramBadRequest(
            method=GetChatMember(chat_id=chat_id, user_id=user_id),
            message="Bad Request: chat not found",
        )


async def test_the_setup_check_names_each_chat_that_is_not_ready() -> None:
    settings = Settings(env="test", capture_chat_ids=[ADMIN_OF, MEMBER_OF, UNKNOWN])

    with capture_logs() as logs:
        ready = await check_capture_chats(cast(Bot, FakeTelegram()), settings)

    assert ready == [ADMIN_OF]
    assert [(entry["event"], entry["chat_id"]) for entry in logs] == [
        ("bot.chat_ready", ADMIN_OF),
        ("bot.not_an_administrator", MEMBER_OF),
        ("bot.chat_not_reachable", UNKNOWN),
    ]
    assert logs[2]["reason"] == "Bad Request: chat not found"


def test_the_bot_asks_for_the_kinds_of_updates_of_the_decision_record() -> None:
    assert ALLOWED_UPDATES == [
        "message",
        "channel_post",
        "edited_channel_post",
        "my_chat_member",
        "callback_query",
    ]


def test_a_token_in_a_log_line_of_a_library_is_replaced() -> None:
    token = "1234567890:AAExampleExampleExampleExampleExampl"  # noqa: S105 - a made-up value
    record = logging.LogRecord(
        "aiohttp",
        logging.ERROR,
        __file__,
        1,
        "cannot open %s",
        (f"https://api.telegram.org/bot{token}/getMe",),
        None,
    )

    assert HideToken(token).filter(record) is True

    assert token not in record.getMessage()
    assert record.getMessage() == "cannot open https://api.telegram.org/bot<bot token>/getMe"


def test_a_log_line_without_the_token_is_not_changed() -> None:
    record = logging.LogRecord("aiogram", logging.INFO, __file__, 1, "Start polling", None, None)

    HideToken("1234567890:AAExample").filter(record)

    assert record.getMessage() == "Start polling"
