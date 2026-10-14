"""Helpers shared by the bot tests: the recorded updates, and a Telegram that is not real."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Message, Update

FIXTURES = Path(__file__).parent / "fixtures"
CHANNEL = -1001234567890
OWNER_TELEGRAM_ID = 111111111


class PretendTelegram(BaseSession):
    """The connection of a test bot. It keeps each message that the bot wants to send, and it
    says "sent". It opens no connection, so no test can reach the real Telegram."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []

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
        if isinstance(method, SendMessage):
            self.sent.append(method)
            answer = Message(
                message_id=1000 + len(self.sent),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
            )
            return cast(T, answer)
        msg = f"the pretend Telegram does not know {type(method).__name__}"
        raise AssertionError(msg)


def offline_bot(telegram: PretendTelegram | None = None) -> Bot:
    """A bot object that never talks to Telegram. What it sends is kept in ``telegram``."""
    return Bot("42:TEST", session=telegram or PretendTelegram())


def raw_updates(name: str) -> list[dict[str, Any]]:
    lines = (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def updates(name: str) -> list[Update]:
    bot = offline_bot()
    return [Update.model_validate(raw, context={"bot": bot}) for raw in raw_updates(name)]


def post(name: str, message_id: int) -> Message:
    """The channel post (or the edit of a post) with this message id, from a fixture file."""
    for update in updates(name):
        message = update.channel_post or update.edited_channel_post
        if message is not None and message.message_id == message_id:
            return message
    msg = f"no post {message_id} in {name}.jsonl"
    raise LookupError(msg)
