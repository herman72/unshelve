"""Helpers shared by the tests of the importer: the exports in the fixtures folder."""

from pathlib import Path

from unshelve.ingest.export_html import ExportedChat, ExportedMessage, read_export

FIXTURES = Path(__file__).parent / "fixtures"
DESKTOP = FIXTURES / "export_desktop"
MACOS = FIXTURES / "export_macos"
MADE_UP = FIXTURES / "export_made_up"


def by_id(chat: ExportedChat) -> dict[int, ExportedMessage]:
    return {message.message_id: message for message in chat.messages}


def made_up() -> ExportedChat:
    return read_export(MADE_UP)
