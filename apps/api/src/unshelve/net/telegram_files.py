"""Download a file of a saved message from Telegram, with no bot token in a log or an error.

The bot keeps only the ids of a file (``MediaRef``). Two requests of the Bot API give the bytes
(https://core.telegram.org/bots/api#getfile):

1. ``getFile`` with the id of the file gives its path on the server of Telegram.
2. ``/file/bot<token>/<path>`` gives the bytes.

The bot token is in the address of both requests. So:

- An error names the file with a label ("telegram file <file_unique_id>"), never with the
  address. The exception of the HTTP client is not kept, not even as the context of the error.
- httpx logs each address at INFO. A filter on its logger writes "<bot token>" in place of the
  token, also at DEBUG.

The Bot API gives files of 20 MB at most. A larger file is refused before any request: the
message says its size.
"""

import logging
import re
import time
from collections.abc import Awaitable

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from unshelve.contracts.dto import MediaRef
from unshelve.contracts.errors import FetchError, FetchFailure
from unshelve.net.service import ServiceAnswer, ServiceClient, ServiceUnreachableError, Transport

log = structlog.get_logger()

API = "https://api.telegram.org"
MAX_FILE_BYTES = 20 * 1024 * 1024
# One request may take this long, from the connection to the last byte: 20 MB in 60 seconds
# needs a third of a megabyte each second.
TIMEOUT = 60.0
HIDDEN = "<bot token>"
_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]+")
# A path that Telegram gives, such as "documents/file_12.pdf": nothing that leaves the folder.
_FILE_PATH = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*")
_TOO_BIG = "file is too big"
_REASONS: dict[str, FetchFailure] = {"timeout": "timeout", "too_large": "too_large"}


class _File(BaseModel):
    file_path: str | None = None
    file_size: int | None = None


class _Parameters(BaseModel):
    retry_after: float | None = None


class _Answer(BaseModel):
    """The answer of the Bot API: ``ok`` and ``result``, or ``ok`` false and the reason."""

    ok: bool
    result: _File | None = None
    error_code: int | None = None
    description: str = ""
    parameters: _Parameters | None = None


class _HideToken(logging.Filter):
    """Writes HIDDEN in place of the token, in each line that a library logs."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.token in message:
            record.msg = message.replace(self.token, HIDDEN)
            record.args = ()
        return True


def hide_in_logs(token: str) -> None:
    """Hide ``token`` in the log of httpx, which logs the address of each request at INFO."""
    logger = logging.getLogger("httpx")
    if not any(isinstance(known, _HideToken) and known.token == token for known in logger.filters):
        logger.addFilter(_HideToken(token))


class TelegramFiles:
    def __init__(self, token: str, *, transport: Transport | None = None) -> None:
        """``transport`` is for the tests: a pretend Telegram in the same process."""
        if not _TOKEN.fullmatch(token):
            # Without the value: a wrong token can still be a secret.
            raise ValueError("the bot token does not have the form <number>:<letters>")
        self._token = token
        hide_in_logs(token)
        self._client = ServiceClient(
            API, headers={}, timeout=TIMEOUT, max_bytes=MAX_FILE_BYTES, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def read(self, media: MediaRef, *, max_bytes: int) -> bytes:
        """The bytes of the file of ``media``, ``max_bytes`` at most."""
        label = f"telegram file {media.file_unique_id or 'without an id'}"
        if media.file_id is None:
            # A file of an export has a path in the export, not an id of the Bot API.
            raise FetchError("unsupported", label)
        limit = min(max_bytes, MAX_FILE_BYTES)
        if media.size_bytes is not None and media.size_bytes > limit:
            raise FetchError("too_large", label)
        started = time.perf_counter()
        path = await self._path_of(media.file_id, label, limit)
        answer = await _call(self._client.get(f"/file/bot{self._token}/{path}"), label)
        if answer.status != 200:
            raise FetchError(
                "http_error", label, status=answer.status, retry_after=answer.retry_after
            )
        if len(answer.body) > limit:
            raise FetchError("too_large", label)
        log.info(
            "telegram.file",
            file=media.file_unique_id,
            bytes=len(answer.body),
            seconds=round(time.perf_counter() - started, 3),
        )
        return answer.body

    async def _path_of(self, file_id: str, label: str, limit: int) -> str:
        """The path of the file on the server of Telegram, from ``getFile``."""
        call = self._client.post_json(f"/bot{self._token}/getFile", {"file_id": file_id})
        answer = await _call(call, label)
        try:
            found = _Answer.model_validate_json(answer.body)
        except ValidationError:
            found = None
        if found is None:
            # No answer of the Bot API: a proxy, or a broken connection. It can pass.
            if answer.status != 200:
                raise FetchError("http_error", label, status=answer.status)
            raise FetchError("network", label)
        if not found.ok or found.result is None:
            if _TOO_BIG in found.description.lower():
                raise FetchError("too_large", label)
            wait = found.parameters.retry_after if found.parameters else None
            raise FetchError(
                "http_error",
                label,
                status=found.error_code or answer.status,
                retry_after=wait if wait is not None else answer.retry_after,
            )
        if found.result.file_size is not None and found.result.file_size > limit:
            raise FetchError("too_large", label)
        path = found.result.file_path or ""
        if not _FILE_PATH.fullmatch(path):
            raise FetchError("unsupported", label)
        return path


async def _call(request: Awaitable[ServiceAnswer], label: str) -> ServiceAnswer:
    """The answer of ``request``. A failure becomes a FetchError that keeps nothing of the
    failure of the HTTP client: its text can hold the address, and so the token."""
    reason: FetchFailure
    try:
        return await request
    except ServiceUnreachableError as error:
        reason = _REASONS.get(error.reason, "network")
    except httpx.HTTPError:
        reason = "network"
    # Raised after the except block: the error has no cause and no context.
    raise FetchError(reason, label)
