"""The keys of a saved thing: the names under which Unshelve finds it again (docs/adr/0003).

A *save* is one message, or the messages of one album. Its keys say what it is:

* ``url:`` the canonical address of its first link
* ``tg_fwd:`` the post that it was forwarded from
* ``tg_file:`` a file in it
* ``tg_album:`` the album
* ``text:`` its text, for a note without a link and without a file

Two saves that share a key are the same thing, and they get one item.

This module is pure: it reads stored messages and gives texts.
"""

import hashlib
from collections.abc import Sequence

from unshelve.db.models import Capture
from unshelve.ingest.canonical import canonicalize

# The database keeps the keys in an index, and an index refuses a very long entry. A long key
# is replaced by its hash, which finds the same thing.
MAX_KEY_BYTES = 900


def _bounded(kind: str, value: str) -> str:
    key = f"{kind}:{value}"
    if len(key.encode()) <= MAX_KEY_BYTES:
        return key
    return f"{kind}:sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def primary_url(captures: Sequence[Capture]) -> str | None:
    """The canonical address of the first link of a save, or None for a save without a link."""
    for capture in captures:
        if capture.urls:
            return canonicalize(capture.urls[0])
    return None


def text_key(text: str) -> str | None:
    """The key of a note. Capital letters and the amount of white space do not change it."""
    words = " ".join(text.split()).casefold()
    if not words:
        return None
    return f"text:{hashlib.sha256(words.encode()).hexdigest()[:32]}"


def keys_of(captures: Sequence[Capture]) -> list[str]:
    """The keys of one save, in a fixed order. ``captures``: its messages, the first one first."""
    keys: set[str] = set()
    url = primary_url(captures)
    if url is not None:
        keys.add(_bounded("url", url))
    has_file = False
    for capture in captures:
        if capture.media_group_id is not None:
            keys.add(_bounded("tg_album", f"{capture.chat_id}:{capture.media_group_id}"))
        origin = capture.forward_origin or {}
        # Only the bot knows the ids of the source. An export gives its name only.
        if origin.get("chat_id") is not None and origin.get("message_id") is not None:
            keys.add(f"tg_fwd:{origin['chat_id']}:{origin['message_id']}")
        for file in capture.media:
            has_file = True
            if file.get("file_unique_id"):
                keys.add(_bounded("tg_file", file["file_unique_id"]))
    if url is None and not has_file:
        # The text is the thing itself only when nothing else is.
        note = text_key("\n".join(capture.text for capture in captures))
        if note is not None:
            keys.add(note)
    return sorted(keys)
