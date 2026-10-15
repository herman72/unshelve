"""Read the text of a PDF file, in a process of its own.

A PDF comes from anywhere. A broken or hostile file can keep a reader busy for minutes, or
fill the memory. A thread cannot be stopped from outside, but a process can. So the worker
starts this module for each PDF, sends the bytes on stdin, and stops the process after a time
limit (``extract.pdf``). The process also limits its own CPU time and, on Linux, its memory.
Only this process imports pypdf.

    python -I -m unshelve.extract.pdf_reader MAX_PAGES MAX_CHARS < file.pdf > reading.json

The answer is one JSON object: the number of pages, the text of each page that was read, the
title, the author, the date and the links of the file. Or an error: "password" for a file
that needs one, "broken" for a file that cannot be read.
"""

import io
import json
import logging
import resource
import sys
from typing import Any, cast

from pypdf import PageObject, PasswordType, PdfReader
from pypdf.generic import DictionaryObject

# A second limit, under the time limit of the worker: after this much CPU time, the system
# stops the process.
CPU_SECONDS = 60
# The memory that the process may reserve (Linux). In the worker image, the made-up paper of
# the tests needs 40 MB. A page whose stream expands to 70 MB needs 170 MB, and pypdf keeps
# each page that it has read: ten such pages need 770 MB.
MEMORY_BYTES = 500_000_000
# The links of the annotations that the answer keeps. The worker keeps 20 links in all.
MAX_LINKS = 100


def read(data: bytes, *, max_pages: int, max_chars: int) -> dict[str, Any]:
    """The text of the first pages, ``max_chars`` characters at most, and the facts of the file.

    A page that cannot be read gives no text, and the next pages are still read.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        # Many files have an empty user password: they open without one, and only limit
        # printing or copying. That is no lock for reading.
        if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            return {"error": "password"}
        pages = len(reader.pages)
        facts = _facts(reader)
    except Exception as error:  # each failure of a broken file gives the same answer
        return {"error": "broken", "detail": _valid(f"{type(error).__name__}: {error}")[:200]}
    texts: list[str] = []
    links: list[str] = []
    size = 0
    for number in range(min(pages, max_pages)):
        try:
            page = reader.pages[number]
            text = _valid(page.extract_text())
            links.extend(_links(page))
        except Exception:  # one broken page: the other pages can still be read
            text = ""
        text = text[: max_chars - size]
        texts.append(text)
        size += len(text)
        if size >= max_chars:
            break
    return {"pages": pages, "read": len(texts), "texts": texts, "links": links[:MAX_LINKS], **facts}


def _facts(reader: PdfReader) -> dict[str, str | None]:
    """The title, the author and the date of the file, as the file states them."""
    info = reader.metadata
    if info is None:
        return {"title": None, "author": None, "created": None}
    created = info.get("/CreationDate")
    return {
        "title": _valid(info.title) if info.title else None,
        "author": _valid(info.author) if info.author else None,
        "created": _valid(str(created)) if created is not None else None,
    }


def _links(page: PageObject) -> list[str]:
    """The addresses of the link annotations of a page: the words that open a web page."""
    found: list[str] = []
    for annotation in cast("list[Any]", page.annotations or []):
        target = annotation.get_object()
        if not isinstance(target, DictionaryObject):
            continue
        action = target.get("/A")
        action = action.get_object() if action is not None else None
        if isinstance(action, DictionaryObject):
            address = action.get("/URI")
            if isinstance(address, str):
                found.append(_valid(address))
    return found


def _valid(text: str) -> str:
    """The text without half characters. A broken font can give one, and JSON cannot hold it."""
    return text.encode("utf-8", errors="replace").decode("utf-8")


def main() -> None:
    _limit(resource.RLIMIT_CPU, CPU_SECONDS)
    if sys.platform == "linux":
        # macOS does not keep this limit, so it is set only where it works.
        _limit(resource.RLIMIT_AS, MEMORY_BYTES)
    # pypdf writes a warning for each broken detail of a file. The answer says enough.
    logging.disable(logging.WARNING)
    max_pages, max_chars = int(sys.argv[1]), int(sys.argv[2])
    reading = read(sys.stdin.buffer.read(), max_pages=max_pages, max_chars=max_chars)
    sys.stdout.buffer.write(json.dumps(reading, ensure_ascii=False).encode())


def _limit(kind: int, value: int) -> None:
    """Lower a limit of this process. A limit that is lower already stays."""
    soft, hard = resource.getrlimit(kind)
    if soft != resource.RLIM_INFINITY and soft <= value:
        return
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    resource.setrlimit(kind, (value, hard))


if __name__ == "__main__":
    main()
