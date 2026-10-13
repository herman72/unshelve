"""Command line entry point: ``unshelve <command>``."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
import typer
import uvicorn

from unshelve import __version__
from unshelve.api.app import create_app
from unshelve.auth import store as auth_store
from unshelve.bot.runner import run_bot
from unshelve.config import get_settings
from unshelve.db import migrations
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.ingest import importer
from unshelve.ingest.export_html import ExportFormatError, read_export
from unshelve.log import configure_logging
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig, new_worker_id
from unshelve.runtime import shutdown_event, wait_for_shutdown
from unshelve.services import build_services

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Unshelve service commands.")
auth_app = typer.Typer(no_args_is_help=True, help="Sign-in helpers.")
app.add_typer(auth_app, name="auth")


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


async def _work() -> None:
    settings = get_settings()
    log = _setup_logging()
    engine = create_engine(settings.database_url)
    try:
        services = build_services(settings, create_session_factory(engine))
        modules = discover()
        config = WorkerConfig(worker_id=new_worker_id())
        log.info(
            "worker.started",
            worker_id=config.worker_id,
            handler_modules=modules,
            kinds=[kind.value for kind in registry.kinds()],
        )
        await Worker(services, registry, config).run_forever(shutdown_event())
        log.info("worker.stopped")
    finally:
        await engine.dispose()


@app.command()
def worker() -> None:
    """Run queued jobs until the process is told to stop."""
    asyncio.run(_work())


async def _bot() -> None:
    settings = get_settings()
    log = _setup_logging()
    if settings.telegram_bot_token is None:
        # A new copy of the project has no bot. The process stays up and idle, so that the
        # stack starts without one.
        log.info("bot.idle", note="UNSHELVE_TELEGRAM_BOT_TOKEN is not set; waiting for shutdown")
        await wait_for_shutdown()
        log.info("bot.stopped")
        return
    engine = create_engine(settings.database_url)
    try:
        await run_bot(settings, create_session_factory(engine))
    finally:
        await engine.dispose()


@app.command()
def bot() -> None:
    """Run the Telegram bot: it stores the posts of the capture channels."""
    asyncio.run(_bot())


async def _issue_login_token(database_url: str, telegram_user_id: int) -> str:
    engine = create_engine(database_url)
    try:
        async with create_session_factory(engine)() as session:
            owner_id = await auth_store.ensure_owner(session, telegram_user_id)
            token = await auth_store.issue_login_token(session, owner_id)
            await session.commit()
            return token
    finally:
        await engine.dispose()


@auth_app.command("link")
def auth_link() -> None:
    """Print a one-time sign-in link for the owner: for development, first run and recovery.

    The bot sends the same kind of link when the owner writes /login. This command is the way
    in when there is no bot yet, or when the bot is down.
    """
    settings = get_settings()
    if settings.owner_telegram_id is None:
        # No owner, no link. An account is never made up for a caller that has no id.
        typer.echo(
            "UNSHELVE_OWNER_TELEGRAM_ID is not set. Put your numeric Telegram user id into .env "
            "(for a local trial without Telegram, any number works, for example 1).",
            err=True,
        )
        raise typer.Exit(code=1)
    token = asyncio.run(_issue_login_token(settings.database_url, settings.owner_telegram_id))
    minutes = int(auth_store.LOGIN_TOKEN_TTL.total_seconds() // 60)
    # The link goes to stdout and the note to stderr, so that a script can capture the link.
    typer.echo(f"{settings.web_base_url.rstrip('/')}/login#token={token}")
    typer.echo(f"This link works once, for {minutes} minutes.", err=True)


def _stop(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=1)


async def _import(
    database_url: str,
    owner_telegram_id: int,
    envelopes: list[importer.CaptureEnvelope],
    *,
    dry_run: bool,
) -> importer.ImportCount:
    engine = create_engine(database_url)
    try:
        sessions = create_session_factory(engine)
        if dry_run:
            return await importer.count_new(sessions, owner_telegram_id, envelopes)
        return await importer.import_envelopes(sessions, owner_telegram_id, envelopes)
    finally:
        await engine.dispose()


@app.command("import")
def import_export(
    folder: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            help="The folder of the export. It has a file messages.html.",
        ),
    ],
    timezone: Annotated[
        str,
        typer.Option(
            help="The time zone of the computer that made the export, for example "
            "Europe/Helsinki. The export has clock times only.",
        ),
    ],
    chat_id: Annotated[
        int | None,
        typer.Option(help="The id of the chat of the export. Not necessary with one capture chat."),
    ] = None,
    since: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Import the posts of this day and later."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option(min=1, help="Import this number of posts at most.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what an import would do, and change nothing.")
    ] = False,
) -> None:
    """Import the HTML export of a Telegram chat ("Export chat history").

    Each post of the export is stored like a post from the bot. A post that is stored already
    is skipped, so this command is safe to run again.
    """
    settings = get_settings()
    if settings.owner_telegram_id is None:
        raise _stop("UNSHELVE_OWNER_TELEGRAM_ID is not set. The posts need an owner.")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise _stop(
            f"{timezone!r} is not the name of a time zone. Use a name such as Europe/Helsinki."
        ) from None
    if chat_id is None:
        if len(settings.capture_chat_ids) != 1:
            known = ", ".join(str(known_id) for known_id in settings.capture_chat_ids) or "none"
            raise _stop(
                "Give the chat of the export with --chat-id. An export does not hold the id "
                f"of its chat. The capture chats in your settings: {known}."
            )
        chat_id = settings.capture_chat_ids[0]
    try:
        chat = read_export(folder)
    except ExportFormatError as error:
        raise _stop(f"This export cannot be read: {error}") from None

    # The albums are found in the whole export. So a limit that cuts an album in two parts
    # gives the two parts the same album.
    albums = importer.album_groups(chat.messages)
    messages = importer.in_range(chat.messages, since=since, limit=limit)
    envelopes = importer.to_envelopes(messages, chat_id=chat_id, zone=zone, albums=albums)
    count = asyncio.run(
        _import(settings.database_url, settings.owner_telegram_id, envelopes, dry_run=dry_run)
    )

    typer.echo(f'Export of "{chat.title}": {len(messages)} posts, into the chat {chat_id}.')
    if messages:
        first, last = messages[0].sent_at, messages[-1].sent_at
        typer.echo(
            f"From {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M}, clock time of {zone.key}."
        )
    found = {envelope.media_group_id for envelope in envelopes if envelope.media_group_id}
    if found:
        typer.echo(f"Albums found: {len(found)}. Each album becomes one item.")
    # The reader counts these in the whole export, also when this import takes a part of it.
    scope = " (in the whole export)" if since is not None or limit is not None else ""
    for kind, number in sorted(chat.left_out.items()):
        typer.echo(f"Not imported{scope}: {number} x {kind}.")
    if dry_run:
        typer.echo(
            f"Dry run: {count.stored} posts are new, {count.repeats} are stored already. "
            "Nothing was changed."
        )
    else:
        typer.echo(f"Stored {count.stored} new posts. {count.repeats} were stored already.")
