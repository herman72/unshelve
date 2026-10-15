"""The blob store on disk: the bytes of the files that you send, under their SHA-256.

The key of a blob is the SHA-256 of its bytes, in hex. So the same bytes have one file, and a
key cannot name other bytes. The file of a key is ``<root>/ab/cd/<key>``: two levels of
folders keep each folder small.

A write goes to a temporary file in the same folder, and then gets its name in one step
(``os.replace``). A reader never sees half a file, also when the worker stops during a write.
The type of a file is in the table ``assets``; the store keeps only bytes.

In the stack, the root is a volume of Docker (``deploy/compose.yaml``), so the files stay when
a container is made again. A backup must copy that volume too.
"""

import asyncio
import hashlib
import os
import re
import tempfile
from pathlib import Path

_KEY = re.compile(r"[0-9a-f]{64}")


class FileBlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, data: bytes, mime: str) -> str:
        key = hashlib.sha256(data).hexdigest()
        await asyncio.to_thread(self._write, key, data)
        return key

    async def get(self, key: str) -> bytes:
        """The bytes of ``key``. ``KeyError`` for an unknown key, and for a text that is no key."""
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise KeyError(key) from None

    def _path(self, key: str) -> Path:
        # Only 64 hex digits: a key can never name a file outside the store ("../", "/").
        if not _KEY.fullmatch(key):
            raise KeyError(key)
        return self._root / key[:2] / key[2:4] / key

    def _write(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if path.exists():
            return  # the same bytes are there already
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".part-")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            Path(temporary).replace(path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
