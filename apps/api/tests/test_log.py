"""Structured logs: with UNSHELVE_LOG_JSON=true, each line on stdout is one JSON object.

Our code logs with structlog, the libraries with the standard library. A log tool reads every
line, so a library line that is plain text is a line that the tool drops.
"""

import json
import logging
from collections.abc import Generator
from typing import Any

import pytest
import structlog

from unshelve import cli
from unshelve.bot.runner import hide_token_in_logs
from unshelve.log import configure_logging
from unshelve.net.telegram_files import hide_in_logs

TOKEN = "123456:made-up-token"  # noqa: S105 - made up for the tests


@pytest.fixture(autouse=True)
def _restore_logging() -> Generator[None]:
    """Each test configures the logging of the process. Put it back afterwards: the handler of
    a test writes to the stdout of that test, which pytest closes."""
    root, httpx = logging.getLogger(), logging.getLogger("httpx")
    handlers, level, filters = root.handlers[:], root.level, httpx.filters[:]
    config = structlog.get_config()
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    httpx.filters[:] = filters
    structlog.configure(**config)


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """Each line of stdout, read as JSON. A line that is not JSON fails the test."""
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_a_line_of_a_library_is_a_json_object(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json=True)

    logging.getLogger("httpx").info("HTTP Request: %s %s", "GET", "https://example.com/a")

    [line] = _lines(capsys)
    assert line["event"] == "HTTP Request: GET https://example.com/a"
    assert (line["level"], line["logger"]) == ("info", "httpx")
    assert line["timestamp"].endswith("Z")


def test_a_line_of_our_code_is_a_json_object_too(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json=True)

    structlog.get_logger().info("job.finished", state="done", seconds=0.25)

    [line] = _lines(capsys)
    assert (line["event"], line["state"], line["seconds"]) == ("job.finished", "done", 0.25)
    assert line["level"] == "info"


def test_the_error_of_a_library_line_carries_its_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(json=True)

    try:
        raise ValueError("the database restarted")
    except ValueError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")

    [line] = _lines(capsys)
    assert line["event"] == "Exception in ASGI application"
    assert line["level"] == "error"
    assert "Traceback" in line["exception"]
    assert "ValueError: the database restarted" in line["exception"]


def test_a_library_line_carries_the_values_bound_for_a_job(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(json=True)

    with structlog.contextvars.bound_contextvars(job_id=42, kind="fetch_item"):
        logging.getLogger("httpx").info("HTTP Request: GET https://example.com/b")
    logging.getLogger("httpx").info("HTTP Request: GET https://example.com/c")

    inside, outside = _lines(capsys)
    assert (inside["job_id"], inside["kind"]) == (42, "fetch_item")
    assert "job_id" not in outside


def test_a_second_configuration_replaces_the_handler(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json=True)
    configure_logging(json=True)

    logging.getLogger("sqlalchemy.engine").warning("one line")

    assert len(_lines(capsys)) == 1


def test_a_line_below_the_level_is_left_out(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", json=True)

    logging.getLogger("httpx").info("HTTP Request: GET https://example.com/d")
    structlog.get_logger().info("job.finished")

    assert _lines(capsys) == []


def test_the_bot_token_stays_hidden_in_a_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    # The bot puts a filter on each handler of the root logger (bot.runner).
    configure_logging(json=True)
    hide_token_in_logs(TOKEN)

    logging.getLogger("aiogram.event").info("Update from https://api.telegram.org/bot%s", TOKEN)

    [line] = _lines(capsys)
    assert TOKEN not in json.dumps(line)
    assert line["event"] == "Update from https://api.telegram.org/bot<bot token>"


def test_the_token_in_an_httpx_address_stays_hidden_in_a_json_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The worker puts a filter on the httpx logger itself (net.telegram_files).
    configure_logging(json=True)
    hide_in_logs(TOKEN)

    logging.getLogger("httpx").info(
        "HTTP Request: GET https://api.telegram.org/file/bot%s/a", TOKEN
    )

    [line] = _lines(capsys)
    assert TOKEN not in json.dumps(line)


def test_the_api_gives_uvicorn_no_log_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    # With its own configuration, uvicorn writes its lines with its own handlers, as text.
    calls: list[dict[str, Any]] = []

    def run(app: str, **options: Any) -> None:
        calls.append(options)

    monkeypatch.setattr(cli.uvicorn, "run", run)

    cli.api(host="127.0.0.1", port=8000, reload=False)

    [call] = calls
    assert call["log_config"] is None
    assert call["factory"] is True
