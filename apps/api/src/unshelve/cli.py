"""Command line entry point: ``unshelve <command>``."""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
import structlog
import typer
import uvicorn

from unshelve import __version__
from unshelve.api.app import create_app
from unshelve.auth import store as auth_store
from unshelve.bot import cards
from unshelve.bot.runner import run_bot
from unshelve.config import Settings, get_settings
from unshelve.contracts.dto import Extracted, FetchResult, SourceRef
from unshelve.contracts.errors import ExtractError, FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.db import migrations
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.db.models import Digest, Item
from unshelve.extract.article import ArticleExtractor
from unshelve.ingest import importer, resolve
from unshelve.ingest.export_html import ExportFormatError, read_export
from unshelve.log import configure_logging
from unshelve.net.fetcher import LiveFetcher
from unshelve.net.guard import BlockedAddressError
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig, new_worker_id
from unshelve.resurface import digest, weekly
from unshelve.runtime import shutdown_event, wait_for_shutdown
from unshelve.services import build_services, close_services
from unshelve.testing.fakes import FakeLLM, InMemoryBlobStore

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Unshelve service commands.")
auth_app = typer.Typer(no_args_is_help=True, help="Sign-in helpers.")
digest_app = typer.Typer(no_args_is_help=True, help="The digest that the bot sends each week.")
app.add_typer(auth_app, name="auth")
app.add_typer(digest_app, name="digest")


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
    services = build_services(settings, create_session_factory(engine))
    try:
        modules = discover()
        config = WorkerConfig(worker_id=new_worker_id())
        log.info(
            "worker.started",
            worker_id=config.worker_id,
            handler_modules=modules,
            kinds=[kind.value for kind in registry.kinds()],
            digest=f"{settings.digest_day} {settings.digest_hour:02d}:00 {settings.timezone}",
        )
        await weekly.run_worker(Worker(services, registry, config), services, shutdown_event())
        log.info("worker.stopped")
    finally:
        await close_services(services)
        await engine.dispose()


@app.command()
def worker() -> None:
    """Run queued jobs until the process is told to stop."""
    asyncio.run(_work())


@app.command()
def fetch(
    url: str,
    max_bytes: Annotated[int, typer.Option(help="Refuse a body that is longer.")] = 5_000_000,
    accept: Annotated[
        list[str] | None,
        typer.Option(help="A content type to accept, such as text/html. Repeat it for more."),
    ] = None,
) -> None:
    """Open one link with the real fetcher and its guard, and print what came back.

    Nothing is stored. An address inside a private network is refused.
    """
    try:
        result, seconds = asyncio.run(_fetch(get_settings(), url, max_bytes, accept or []))
    except FetchError as error:
        typer.echo(f"Not fetched: {_why(error)}.")
        raise typer.Exit(1) from None
    typer.echo(f"{result.status} {result.final_url}")
    content_type = result.content_type or "no content type"
    typer.echo(f"{content_type}, {len(result.body)} bytes, {seconds:.2f} s")


@app.command()
def extract(url: str) -> None:
    """Read one link with the article extractor, and print what it found.

    Nothing is stored. It shows the title, the facts of the page and the start of the text.
    """
    try:
        found, seconds = asyncio.run(_extract(get_settings(), url))
    except FetchError as error:
        typer.echo(f"Not fetched: {_why(error)}.")
        raise typer.Exit(1) from None
    except ExtractError as error:
        typer.echo(f"No article: {error}.")
        raise typer.Exit(1) from None
    day = f"{found.published_at:%Y-%m-%d}" if found.published_at else "-"
    typer.echo(f"Title:  {found.title or '-'}")
    typer.echo(f"Site:   {found.site or '-'} · language {found.lang or '-'} · date {day}")
    typer.echo(f"Author: {found.author or '-'}")
    typer.echo(f"Text:   {len(found.text.split())} words, read in {seconds:.2f} s")
    typer.echo("")
    typer.echo(_start(found.text, 600))


async def _extract(settings: Settings, url: str) -> tuple[Extracted, float]:
    fetcher = LiveFetcher.from_settings(settings)
    # The article extractor uses only the fetcher. The other two are here for the contract.
    context = ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())
    started = time.perf_counter()
    try:
        found = await ArticleExtractor().extract(SourceRef(url=url), context)
    finally:
        await fetcher.aclose()
    return found, time.perf_counter() - started


def _start(text: str, characters: int) -> str:
    """The start of a long text, cut after a whole word."""
    if len(text) <= characters:
        return text
    return text[:characters].rsplit(" ", 1)[0] + " …"


def _why(error: FetchError) -> str:
    """The reason of a failed fetch, with the words of the guard when it refused."""
    if isinstance(error.__cause__, BlockedAddressError):
        return f"{error.reason} ({error.__cause__})"
    if error.status is not None:
        return f"{error.reason} ({error.status})"
    return error.reason


async def _fetch(
    settings: Settings, url: str, max_bytes: int, accept: list[str]
) -> tuple[FetchResult, float]:
    fetcher = LiveFetcher.from_settings(settings)
    started = time.perf_counter()
    try:
        result = await fetcher.get(url, max_bytes=max_bytes, accept=accept)
    finally:
        await fetcher.aclose()
    return result, time.perf_counter() - started


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


def _posts(number: int) -> str:
    return f"{number} post" if number == 1 else f"{number} posts"


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

    typer.echo(f'Export of "{chat.title}": {_posts(len(messages))}, into the chat {chat_id}.')
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
    one_new, one_repeat = count.stored == 1, count.repeats == 1
    if dry_run:
        typer.echo(
            f"Dry run: {_posts(count.stored)} {'is' if one_new else 'are'} new, "
            f"{count.repeats} {'is' if one_repeat else 'are'} stored already. Nothing was changed."
        )
    else:
        typer.echo(
            f"Stored {count.stored} new {'post' if one_new else 'posts'}. "
            f"{count.repeats} {'was' if one_repeat else 'were'} stored already."
        )


async def _rekey(database_url: str) -> tuple[int, int, int]:
    """Returns the number of items, of new keys, and of items that became a duplicate."""
    engine = create_engine(database_url)
    try:
        sessions = create_session_factory(engine)
        duplicates = sa.select(sa.func.count()).where(Item.duplicate_of_id.is_not(None))
        async with sessions() as session:
            # In the order of the ids, so that each run does the same work in the same order.
            item_ids = (await session.scalars(sa.select(Item.id).order_by(Item.id))).all()
            before = await session.scalar(duplicates) or 0
        keys = 0
        for item_id in item_ids:
            async with sessions() as session:
                keys += await resolve.rekey_item(session, item_id)
                await session.commit()
        async with sessions() as session:
            after = await session.scalar(duplicates) or 0
        return len(item_ids), keys, after - before
    finally:
        await engine.dispose()


@app.command()
def rekey() -> None:
    """Give each item the keys of its saves again.

    Run it one time after an update that changes how Unshelve sees that two saves are the
    same thing. Items that are the same thing are joined: the oldest stays in the list. It is
    safe to run again.
    """
    items, keys, joined = asyncio.run(_rekey(get_settings().database_url))
    typer.echo(f"Items: {items}. New keys: {keys}. Joined to an older item: {joined}.")


async def _digest(settings: Settings, *, send: bool) -> tuple[str, int]:
    """The text of the digest of now, and the number of its items. With ``send``, the bot
    sends it too."""
    if settings.owner_telegram_id is None:
        raise _stop("UNSHELVE_OWNER_TELEGRAM_ID is not set. The digest needs its owner.")
    engine = create_engine(settings.database_url)
    services = build_services(settings, create_session_factory(engine))
    now = datetime.now(UTC)
    try:
        async with services.sessions() as session:
            owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
            found = await digest.pick(session, owner_id, now=now, seed=f"{owner_id}:{now:%Y-%m-%d}")
            text, _ = cards.digest_message(
                found.recent, found.from_the_shelf, now=now, days=digest.RECENT_DAYS
            )
            if send:
                # A try has a row of its own, so the digest of the week stays as it is.
                session.add(Digest(owner_id=owner_id, period_key=f"try-{now:%Y-%m-%dT%H:%M:%S}"))
            await session.commit()
        items = len(found.recent) + (found.from_the_shelf is not None)
        if send and items:
            await weekly.send(services, owner_id, f"try-{now:%Y-%m-%dT%H:%M:%S}", now)
        return text, items
    finally:
        await close_services(services)
        await engine.dispose()


@digest_app.command("preview")
def digest_preview() -> None:
    """Print the digest of now, as the bot would send it. Nothing is sent."""
    text, items = asyncio.run(_digest(get_settings(), send=False))
    typer.echo(text if items else cards.NOTHING_YET)


@digest_app.command("send")
def digest_send() -> None:
    """Send the digest of now to the owner, to try it. The digest of the week is not changed."""
    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise _stop("UNSHELVE_TELEGRAM_BOT_TOKEN is not set. Without a bot, nothing can be sent.")
    _, items = asyncio.run(_digest(settings, send=True))
    typer.echo(f"Sent a digest with {items} items." if items else "Nothing to send: no items.")
