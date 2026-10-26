"""Structured logging: JSON lines in production, readable lines in development.

Two kinds of loggers write to stdout. Our code logs with structlog. The libraries (uvicorn,
httpx, SQLAlchemy, aiogram, Alembic) log with the standard library. Both go through the same
processors and the same renderer, so with ``UNSHELVE_LOG_JSON=true`` each line is one JSON
object, and a log tool can read every line.
"""

import logging
import sys
from typing import TextIO

import structlog
from structlog.typing import Processor


class _LibraryHandler(logging.StreamHandler[TextIO]):
    """Writes the records of the standard library. A class of its own: a second call of
    ``configure_logging`` replaces this handler and leaves other handlers alone (pytest adds
    its own)."""


def configure_logging(*, level: str = "INFO", json: bool = False) -> None:
    """Configure structlog, and render the records of standard-library loggers the same way."""
    shared: list[Processor] = [
        # The values bound for a job (queue.worker) go into each line, also of a library.
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    handler = _LibraryHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # A record of a library becomes an event: its message, level, time and logger.
            foreign_pre_chain=[*shared, structlog.stdlib.add_logger_name],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    for old in [known for known in root.handlers if isinstance(known, _LibraryHandler)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
