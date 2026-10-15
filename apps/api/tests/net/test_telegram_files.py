"""The download of a file of a message from Telegram, from a pretend Telegram in the same
process. Each test name is one rule. Each failure is also checked for the bot token: it must
reach no error and no line of the log."""

import json
import logging
import traceback
from collections.abc import Callable

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from unshelve.contracts.dto import MediaRef
from unshelve.contracts.errors import FetchError
from unshelve.net import telegram_files as telegram_module
from unshelve.net.telegram_files import HIDDEN, MAX_FILE_BYTES, TelegramFiles

TOKEN = "123456789:MADE-UP-token_for_the_tests-only"  # noqa: S105 - made up for the tests
SECRET = TOKEN.split(":", 1)[1]
PDF = b"%PDF-1.7 made-up bytes of a document"
FILE_PATH = "documents/file_7.pdf"
GET_FILE = f"/bot{TOKEN}/getFile"
DOWNLOAD = f"/file/bot{TOKEN}/{FILE_PATH}"
DOCUMENT = MediaRef(
    kind="document",
    file_id="BQACAgMadeUpFileId",
    file_unique_id="AgADmadeup",
    mime="application/pdf",
    size_bytes=len(PDF),
    file_name="leases.pdf",
)

type Answer = Callable[[httpx.Request], httpx.Response]


def _bytes(status: int, body: bytes, headers: dict[str, str] | None = None) -> Answer:
    # A stream, as from a real connection: the client reads the bytes as they arrive.
    return lambda _: httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def _json(status: int, data: object) -> Answer:
    return _bytes(status, json.dumps(data).encode(), {"content-type": "application/json"})


def _found(path: str = FILE_PATH, size: int | None = len(PDF)) -> Answer:
    result: dict[str, object] = {"file_id": DOCUMENT.file_id, "file_path": path}
    if size is not None:
        result["file_size"] = size
    return _json(200, {"ok": True, "result": result})


def _refused(status: int, description: str, retry_after: int | None = None) -> Answer:
    body: dict[str, object] = {"ok": False, "error_code": status, "description": description}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return _json(status, body)


class PretendTelegram:
    """Answers getFile and the download of its path, and writes down each request."""

    def __init__(self, get_file: Answer | None = None, download: Answer | None = None) -> None:
        self.get_file = get_file or _found()
        self.download = download or _bytes(200, PDF)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == GET_FILE:
            return self.get_file(request)
        if request.url.path == DOWNLOAD:
            return self.download(request)
        return _refused(404, "Not Found")(request)


def _files(telegram: PretendTelegram) -> TelegramFiles:
    return TelegramFiles(TOKEN, transport=httpx.MockTransport(telegram))


def _all_text_of(error: BaseException) -> str:
    """Everything that a log can print of an error: its trace with each cause, and its fields."""
    return "".join(traceback.format_exception(error)) + repr(error) + repr(vars(error))


async def _failure(
    telegram: PretendTelegram, media: MediaRef = DOCUMENT, max_bytes: int = 30_000_000
) -> FetchError:
    with pytest.raises(FetchError) as caught:
        await _files(telegram).read(media, max_bytes=max_bytes)
    error = caught.value
    assert error.url == f"telegram file {media.file_unique_id or 'without an id'}"
    assert SECRET not in _all_text_of(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    return error


# --- A file that comes -------------------------------------------------------------------


async def test_a_file_is_asked_for_by_its_id_and_then_downloaded_from_its_path() -> None:
    telegram = PretendTelegram()

    data = await _files(telegram).read(DOCUMENT, max_bytes=30_000_000)

    assert data == PDF
    asked, downloaded = telegram.requests
    assert (asked.method, asked.url.host, asked.url.path) == ("POST", "api.telegram.org", GET_FILE)
    assert json.loads(asked.content) == {"file_id": DOCUMENT.file_id}
    assert (downloaded.method, downloaded.url.path) == ("GET", DOWNLOAD)


# --- Files that are not downloaded -------------------------------------------------------


async def test_a_file_without_an_id_of_the_bot_api_is_unsupported() -> None:
    # A file of an export has a path in the export (#57), not an id of the Bot API.
    telegram = PretendTelegram()
    exported = MediaRef(kind="document", local_path="files/leases.pdf", file_name="leases.pdf")

    error = await _failure(telegram, exported)

    assert error.reason == "unsupported"
    assert telegram.requests == []


@pytest.mark.parametrize(
    ("size", "max_bytes"),
    [(MAX_FILE_BYTES + 1, 30_000_000), (101, 100)],
    ids=["over the 20 MB of the Bot API", "over the limit of the reader"],
)
async def test_a_file_that_is_too_large_is_refused_without_a_request(
    size: int, max_bytes: int
) -> None:
    telegram = PretendTelegram()
    large = DOCUMENT.model_copy(update={"size_bytes": size})

    error = await _failure(telegram, large, max_bytes)

    assert error.reason == "too_large"
    assert telegram.requests == []


async def test_a_size_that_only_get_file_gives_is_checked_before_the_download() -> None:
    telegram = PretendTelegram(get_file=_found(size=101))

    error = await _failure(telegram, DOCUMENT.model_copy(update={"size_bytes": None}), 100)

    assert error.reason == "too_large"
    assert len(telegram.requests) == 1


async def test_a_download_longer_than_the_limit_is_refused() -> None:
    telegram = PretendTelegram(get_file=_found(size=None), download=_bytes(200, b"x" * 101))

    error = await _failure(telegram, DOCUMENT.model_copy(update={"size_bytes": None}), 100)

    assert error.reason == "too_large"


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "documents/../x", ""])
async def test_a_path_that_leaves_the_folder_of_the_file_is_refused(path: str) -> None:
    telegram = PretendTelegram(get_file=_found(path=path))

    error = await _failure(telegram)

    assert error.reason == "unsupported"
    assert len(telegram.requests) == 1  # no download


# --- Failures --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "reason", "status", "wait"),
    [
        (_refused(400, "Bad Request: file is too big"), "too_large", None, None),
        (_refused(400, "Bad Request: wrong file_id or the file is temporarily unavailable"),
         "http_error", 400, None),
        (_refused(401, "Unauthorized"), "http_error", 401, None),
        (_refused(429, "Too Many Requests: retry after 7", retry_after=7), "http_error", 429, 7),
        (_bytes(502, b"<html>Bad Gateway</html>"), "http_error", 502, None),
        (_bytes(200, b"not the answer of the Bot API"), "network", None, None),
    ],
    ids=["too big", "wrong id", "wrong token", "too many requests", "bad gateway", "not json"],
)  # fmt: skip
async def test_a_failure_of_get_file_is_a_fetch_error_with_its_reason(
    answer: Answer, reason: str, status: int | None, wait: float | None
) -> None:
    error = await _failure(PretendTelegram(get_file=answer))

    assert (error.reason, error.status, error.retry_after) == (reason, status, wait)


@pytest.mark.parametrize(
    ("answer", "status", "wait"),
    [(_bytes(404, b"Not Found"), 404, None), (_bytes(503, b"", {"retry-after": "30"}), 503, 30)],
    ids=["gone", "busy"],
)
async def test_a_failure_of_the_download_is_an_http_error(
    answer: Answer, status: int, wait: float | None
) -> None:
    error = await _failure(PretendTelegram(download=answer))

    assert (error.reason, error.status, error.retry_after) == ("http_error", status, wait)


def _raises(error: type[httpx.TransportError]) -> Answer:
    def answer(request: httpx.Request) -> httpx.Response:
        # The worst case: the text of the error of the client holds the whole address.
        raise error(f"made-up failure of {request.url}", request=request)

    return answer


@pytest.mark.parametrize(
    ("failure", "reason"),
    [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")],
)
async def test_a_failure_of_the_connection_can_pass_and_keeps_nothing_of_its_text(
    failure: type[httpx.TransportError], reason: str
) -> None:
    error = await _failure(PretendTelegram(get_file=_raises(failure)))

    assert error.reason == reason
    assert error.retryable


async def test_another_error_of_the_http_client_is_a_network_failure_and_keeps_nothing() -> None:
    # The client of the service turns the errors of a connection into its own error. An error
    # of httpx that it does not know comes through, and it can hold the address too.
    def answer(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError(f"made-up failure of {request.url}", request=request)

    error = await _failure(PretendTelegram(get_file=answer))

    assert error.reason == "network"


# --- The token ---------------------------------------------------------------------------


async def test_the_token_is_in_no_line_of_the_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    # A new logger: one that an earlier test configured and cached would not be captured.
    monkeypatch.setattr(telegram_module, "log", structlog.get_logger())

    with capture_logs() as events:
        await _files(PretendTelegram()).read(DOCUMENT, max_bytes=30_000_000)

    lines = [record.getMessage() for record in caplog.records]
    # httpx logs each request with its address, and the filter hides the token in it.
    assert sum("HTTP Request" in line for line in lines) == 2
    assert all(SECRET not in line for line in lines)
    assert sum(HIDDEN in line for line in lines) == 2
    [event] = [entry for entry in events if entry["event"] == "telegram.file"]
    assert (event["file"], event["bytes"]) == ("AgADmadeup", len(PDF))
    assert SECRET not in repr(events)


def test_the_filter_of_a_token_is_added_once() -> None:
    _files(PretendTelegram())
    _files(PretendTelegram())

    httpx_logger = logging.getLogger("httpx")
    assert sum(getattr(known, "token", None) == TOKEN for known in httpx_logger.filters) == 1


@pytest.mark.parametrize("token", ["123:made-up/part", "made-up-token", ""])
def test_a_token_of_a_wrong_form_is_refused_without_its_value(token: str) -> None:
    with pytest.raises(ValueError, match="form") as caught:
        TelegramFiles(token)

    assert "made-up" not in str(caught.value)
