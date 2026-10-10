"""Command line entry point: ``unshelve <command>``."""

import asyncio
import json
from pathlib import Path

import structlog
import typer
import uvicorn

from unshelve import __version__
from unshelve.api.app import create_app
from unshelve.config import get_settings
from unshelve.db import migrations
from unshelve.log import configure_logging
from unshelve.runtime import wait_for_shutdown

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Unshelve service commands.")


def _setup_logging() -> structlog.stdlib.BoundLogger:
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)
    return structlog.get_logger()


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def api(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Serve the HTTP API."""
    uvicorn.run("unshelve.api.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command()
def openapi(output: Path = Path("openapi.json")) -> None:
    """Write the OpenAPI document. The committed copy is the contract with the web app."""
    document = create_app().openapi()
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@app.command()
def migrate(revision: str = "head") -> None:
    """Apply database migrations."""
    _setup_logging().info("migrate.start", revision=revision)
    migrations.upgrade(revision)


async def _idle(name: str) -> None:
    log = _setup_logging()
    log.info(f"{name}.started", note="not implemented yet; waiting for shutdown")
    await wait_for_shutdown()
    log.info(f"{name}.stopped")


@app.command()
def worker() -> None:
    """Run queued jobs. The job loop arrives with the queue (M0-4)."""
    asyncio.run(_idle("worker"))


@app.command()
def bot() -> None:
    """Run the Telegram bot. Capture arrives in M1."""
    asyncio.run(_idle("bot"))
