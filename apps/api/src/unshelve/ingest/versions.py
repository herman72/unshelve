"""The versions of a message, and what an item reads from its messages. Pure: no database.

Telegram sends an edit of a post as a new version of the same message. Each version is a
capture of its own (``edit_seq`` is the time of the edit, and 0 for the first version). The
stored versions are never changed. An item reads the latest version of each of its messages.

An item can have many messages: an album, or the same thing saved two times. The item reads
each different text, link and file one time. So a save with the same text changes nothing
that the item reads, and no step runs again.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from unshelve.db.models import Capture


def latest_versions(captures: Sequence[Capture]) -> list[Capture]:
    """Each message one time, in its latest version, in the order of the messages."""
    latest: dict[tuple[int, int], Capture] = {}
    for capture in captures:
        message = (capture.chat_id, capture.message_id)
        if message not in latest or capture.edit_seq > latest[message].edit_seq:
            latest[message] = capture
    return sorted(latest.values(), key=lambda capture: (capture.chat_id, capture.message_id))


@dataclass(frozen=True, slots=True)
class Source:
    """What the pipeline reads from the messages of an item."""

    texts: tuple[str, ...]
    urls: tuple[str, ...]
    media: tuple[dict[str, Any], ...]
    forward_origin: dict[str, Any] | None

    @property
    def text(self) -> str:
        return "\n\n".join(self.texts)

    def code(self) -> str:
        """A short code. When it changes, the item must be processed again."""
        encoded = json.dumps(
            [self.texts, self.urls, self.media, self.forward_origin],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(encoded.encode()).hexdigest()


def _same(text: str) -> str:
    """Two texts that differ only in capital letters and white space are the same text."""
    return " ".join(text.split()).casefold()


def source(captures: Sequence[Capture]) -> Source:
    """What the item reads: the latest versions, each different text, link and file one time,
    in the order of the messages."""
    texts: dict[str, str] = {}
    urls: dict[str, None] = {}
    media: dict[str, dict[str, Any]] = {}
    forward_origin: dict[str, Any] | None = None
    for capture in latest_versions(captures):
        if capture.text.strip():
            texts.setdefault(_same(capture.text), capture.text)
        for url in capture.urls:
            urls.setdefault(url, None)
        for file in capture.media:
            name = (
                file.get("file_unique_id")
                or file.get("local_path")
                or json.dumps(file, sort_keys=True)
            )
            media.setdefault(str(name), file)
        if forward_origin is None and capture.forward_origin:
            forward_origin = capture.forward_origin
    return Source(
        texts=tuple(texts.values()),
        urls=tuple(urls),
        media=tuple(media.values()),
        forward_origin=forward_origin,
    )
