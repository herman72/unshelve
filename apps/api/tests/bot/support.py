"""Helpers shared by the bot tests: the recorded updates."""

import json
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import Message, Update

FIXTURES = Path(__file__).parent / "fixtures"
CHANNEL = -1001234567890
OWNER_TELEGRAM_ID = 111111111


def offline_bot() -> Bot:
    """A bot object that never talks to Telegram: the handlers under test make no API call."""
    return Bot("42:TEST")


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
