"""Helpers shared by the tests of the importer: the exports in the fixtures folder."""

import shutil
from pathlib import Path

from unshelve.ingest.export_html import ExportedChat, ExportedMessage, read_export

FIXTURES = Path(__file__).parent / "fixtures"
DESKTOP = FIXTURES / "export_desktop"
MACOS = FIXTURES / "export_macos"
MADE_UP = FIXTURES / "export_made_up"

# The files of the made-up export. The photos are made-up bytes: the importer only copies them.
PHOTO_ONE = b"\xff\xd8\xff\xe0 the first made-up photo of the album"
PHOTO_TWO = b"\xff\xd8\xff\xe0 the second made-up photo of the album"
PDF = (FIXTURES.parents[1] / "extract" / "fixtures" / "pdf" / "paper.pdf").read_bytes()


def by_id(chat: ExportedChat) -> dict[int, ExportedMessage]:
    return {message.message_id: message for message in chat.messages}


def made_up() -> ExportedChat:
    return read_export(MADE_UP)


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def rewrite(page: Path, old: str, new: str) -> None:
    """Change one text in a page of an export."""
    text = page.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    page.write_text(text.replace(old, new), encoding="utf-8")


def made_up_with_files(folder: Path) -> Path:
    """A copy of the made-up export in ``folder``, with three of its files: the two photos of
    message 7 and 8, and the PDF of message 23. The video (13), the voice message (20) and the
    audio file (21) are not in the folder."""
    shutil.copytree(MADE_UP, folder)
    write(folder / "photos" / "photo_1@13-10-2026_23-25-17.jpg", PHOTO_ONE)
    write(folder / "photos" / "photo_2@13-10-2026_23-25-17.jpg", PHOTO_TWO)
    write(folder / "files" / "part-one.pdf", PDF)
    return folder
