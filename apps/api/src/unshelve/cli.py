"""Command line entry point: ``unshelve <command>``."""

import asyncio
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
import structlog
import typer
import uvicorn
from pydantic import BaseModel

from unshelve import __version__
from unshelve.api.app import create_app
from unshelve.auth import store as auth_store
from unshelve.blobs import FileBlobStore
from unshelve.bot import cards
from unshelve.bot.runner import run_bot
from unshelve.config import Settings, get_settings
from unshelve.contracts.dto import (
    EmbeddingSpec,
    Extracted,
    FetchResult,
    LLMResult,
    MediaRef,
    SourceRef,
    TextPart,
)
from unshelve.contracts.errors import ExtractError, FetchError, ProviderError
from unshelve.contracts.protocols import ExtractContext, Extractor
from unshelve.db import migrations
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.db.models import Digest, EmbeddingSet, Item, LLMBatch, LLMCall
from unshelve.embed import sets
from unshelve.extract.image import image_type
from unshelve.extract.registry import FALLBACK, choose
from unshelve.ingest import export_files, importer, resolve
from unshelve.ingest.export_html import ExportedMessage, ExportFormatError, read_export
from unshelve.log import configure_logging
from unshelve.net.fetcher import LiveFetcher
from unshelve.net.guard import BlockedAddressError
from unshelve.pipeline import batches
from unshelve.providers.batches import ClaudeBatches
from unshelve.providers.claude import ClaudeLLM
from unshelve.providers.ledger import CachedLLM, cost_usd
from unshelve.providers.voyage import VoyageEmbedder
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig, new_worker_id
from unshelve.relevance import golden, rescore
from unshelve.resurface import plan, weekly
from unshelve.runtime import shutdown_event, wait_for_shutdown
from unshelve.search import hybrid
from unshelve.services import (
    build_batches,
    build_embedder,
    build_model,
    build_services,
    close_services,
    embedder_spec,
)
from unshelve.testing.fakes import FakeLLM, InMemoryBlobStore

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Unshelve service commands.")
auth_app = typer.Typer(no_args_is_help=True, help="Sign-in helpers.")
digest_app = typer.Typer(no_args_is_help=True, help="The digest that the bot sends each week.")
llm_app = typer.Typer(no_args_is_help=True, help="The model: a test call, and what the calls cost.")
embeddings_app = typer.Typer(
    no_args_is_help=True, help="The vectors for search: their sets, a backfill, and a test call."
)
batch_app = typer.Typer(
    no_args_is_help=True,
    help="Cards in one batch, at half the price (UNSHELVE_CARDS=batch).",
)
app.add_typer(auth_app, name="auth")
app.add_typer(digest_app, name="digest")
app.add_typer(llm_app, name="llm")
llm_app.add_typer(batch_app, name="batch")
app.add_typer(embeddings_app, name="embeddings")
goals_app = typer.Typer(no_args_is_help=True, help="Your goals, and the scores of the items.")
app.add_typer(goals_app, name="goals")
labels_app = typer.Typer(
    no_args_is_help=True, help="Your verdicts on the scores, as an evaluation set."
)
app.add_typer(labels_app, name="labels")
search_app = typer.Typer(no_args_is_help=True, help="Search your items.")
app.add_typer(search_app, name="search")


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
def extract(
    url: Annotated[str | None, typer.Argument(help="The link to read.")] = None,
    file: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            readable=True,
            help="A file on this computer, read as a file that you sent: an image or a PDF.",
        ),
    ] = None,
) -> None:
    """Read one link, or one file, with the extractor that the worker would choose, and print
    what it found.

    Nothing is stored. It shows the reader, the title, the facts and the start of the text. An
    image is read by the model of UNSHELVE_PROVIDERS: with "live" that is a paid call, and the
    ledger keeps it.
    """
    if (url is None) == (file is None):
        typer.echo("Give one link, or one file with --file.")
        raise typer.Exit(2)
    ref = SourceRef(url=url) if file is None else _sent(file)
    extractor = choose(ref)
    if extractor is FALLBACK:
        what = "link" if file is None else "file"
        typer.echo(f"No extractor reads this {what} yet: the item keeps its saved text.")
        raise typer.Exit(1)
    try:
        found, seconds = asyncio.run(_extract(get_settings(), extractor, ref, file))
    except FetchError as error:
        typer.echo(f"Not fetched: {_why(error)}.")
        raise typer.Exit(1) from None
    except ExtractError as error:
        typer.echo(f"Nothing read ({extractor.name}): {error}.")
        raise typer.Exit(1) from None
    except ProviderError as error:
        typer.echo(f"No answer from the model ({extractor.name}): {error}.")
        raise typer.Exit(1) from None
    day = f"{found.published_at:%Y-%m-%d}" if found.published_at else "-"
    typer.echo(f"Reader: {extractor.name}")
    typer.echo(f"Title:  {found.title or '-'}")
    typer.echo(f"Site:   {found.site or '-'} · language {found.lang or '-'} · date {day}")
    typer.echo(f"Author: {found.author or '-'}")
    if found.minutes is not None:
        typer.echo(f"Length: {found.minutes} min")
    typer.echo(f"Text:   {len(found.text.split())} words, read in {seconds:.2f} s")
    typer.echo("")
    typer.echo(_start(found.text, 600))


def _sent(path: Path) -> SourceRef:
    """A file on this computer, as a message that sends it: a photo when its bytes are an
    image, else a document with its name."""
    with path.open("rb") as handle:
        start = handle.read(16)
    kind = "photo" if image_type(start) is not None else "document"
    return SourceRef(media=[MediaRef(kind=kind, file_name=path.name)])


class _OneFile:
    """The file of ``unshelve extract --file``, as the files of a message."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def read(self, media: MediaRef, *, max_bytes: int) -> bytes:
        if self._path.stat().st_size > max_bytes:
            raise FetchError("too_large", self._path.name)
        return await asyncio.to_thread(self._path.read_bytes)


async def _extract(
    settings: Settings, extractor: Extractor, ref: SourceRef, file: Path | None
) -> tuple[Extracted, float]:
    fetcher = LiveFetcher.from_settings(settings)
    # A link needs only the fetcher. A file can need the model: the one of the worker, behind
    # its cache and its ledger. No connection is opened before the first question.
    engine = create_engine(settings.database_url) if file is not None else None
    llm = build_model(settings, create_session_factory(engine)) if engine else FakeLLM()
    context = ExtractContext(
        fetcher=fetcher,
        blobs=InMemoryBlobStore(),
        llm=llm,
        files=_OneFile(file) if file is not None else None,
    )
    started = time.perf_counter()
    try:
        found = await extractor.extract(ref, context)
    finally:
        await fetcher.aclose()
        if isinstance(llm, CachedLLM):
            await llm.aclose()
        if engine is not None:
            await engine.dispose()
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
    settings: Settings,
    owner_telegram_id: int,
    folder: Path,
    messages: Sequence[ExportedMessage],
    *,
    chat_id: int,
    zone: ZoneInfo,
    albums: dict[int, str],
    dry_run: bool,
) -> importer.Imported:
    engine = create_engine(settings.database_url)
    try:
        return await importer.import_folder(
            create_session_factory(engine),
            FileBlobStore(settings.blob_dir),
            owner_telegram_id,
            folder,
            messages,
            chat_id=chat_id,
            zone=zone,
            albums=albums,
            dry_run=dry_run,
        )
    finally:
        await engine.dispose()


def _echo_files(result: importer.Imported, *, dry_run: bool) -> None:
    """The lines about the files of an import, when the export has files."""
    files = result.files
    largest = export_files.MAX_FILE_BYTES // 1_000_000
    others = [
        (files.known, "copied before"),
        (files.not_needed, "not needed (the bot stored their posts)"),
        (files.too_large, f"too large (over {largest} MB)"),
        (files.refused, "refused (a path out of the folder)"),
        (files.missing, "not in the folder"),
        (files.unreadable, "not readable"),
    ]
    if not files.copied and not any(number for number, _ in others):
        return
    parts = [f"{files.copied} {'to copy' if dry_run else 'copied'}"]
    parts += [f"{number} {what}" for number, what in others if number]
    typer.echo(f"Files: {', '.join(parts)}.")
    if result.filled:
        typer.echo(
            f"Posts of an earlier import that got their files: {result.filled}. "
            "Their items are read again."
        )


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
    result = asyncio.run(
        _import(
            settings,
            settings.owner_telegram_id,
            folder,
            messages,
            chat_id=chat_id,
            zone=zone,
            albums=albums,
            dry_run=dry_run,
        )
    )
    envelopes, count = result.envelopes, result.count

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
    _echo_files(result, dry_run=dry_run)


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


async def _digest_preview(settings: Settings) -> str:
    """The text of the digest of now, as /digest shows it. Nothing is recorded."""
    if settings.owner_telegram_id is None:
        raise _stop("UNSHELVE_OWNER_TELEGRAM_ID is not set. The digest needs its owner.")
    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    now = datetime.now(UTC)
    try:
        async with sessions() as session:
            owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
            planned = await plan.plan_digest(
                session,
                owner_id,
                now=now,
                seed=f"{owner_id}:{now:%Y-%m-%d}",
                minutes=settings.digest_minutes,
            )
            await session.commit()
        return cards.NOTHING_YET if planned.empty else cards.digest_message(planned, now=now)[0]
    finally:
        await engine.dispose()


async def _digest_send(settings: Settings) -> weekly.Sent:
    """Send a digest and its stale prompt now, as the week would send them. A try has a row
    of its own, so the digest of the week stays as it is. Like each sent digest, it marks its
    items as shown: they do not come again for three weeks."""
    if settings.owner_telegram_id is None:
        raise _stop("UNSHELVE_OWNER_TELEGRAM_ID is not set. The digest needs its owner.")
    engine = create_engine(settings.database_url)
    services = build_services(settings, create_session_factory(engine))
    now = datetime.now(UTC)
    period = f"try-{now:%Y-%m-%dT%H:%M:%S}"
    try:
        async with services.sessions() as session:
            owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
            session.add(Digest(owner_id=owner_id, period_key=period))
            await session.commit()
        return await weekly.send(services, owner_id, period, now)
    finally:
        await close_services(services)
        await engine.dispose()


@digest_app.command("preview")
def digest_preview() -> None:
    """Print the digest of now, as /digest shows it. Nothing is sent or recorded."""
    typer.echo(asyncio.run(_digest_preview(get_settings())))


@digest_app.command("send")
def digest_send() -> None:
    """Send a digest and its stale prompt to the owner now, to try them. The digest of the
    week is not changed, but the items of the try count as shown."""
    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise _stop("UNSHELVE_TELEGRAM_BOT_TOKEN is not set. Without a bot, nothing can be sent.")
    sent = asyncio.run(_digest_send(settings))

    def things(count: int) -> str:
        return f"{count} item" if count == 1 else f"{count} items"

    typer.echo(
        f"Sent a digest with {things(sent.items)} and a stale prompt with {things(sent.stale)}."
        if sent.items or sent.stale
        else "Nothing to send: no items."
    )


class _Title(BaseModel):
    title: str


@llm_app.command("check")
def llm_check(
    model: Annotated[
        str | None, typer.Option(help="A model other than UNSHELVE_LLM_MODEL.")
    ] = None,
) -> None:
    """Ask the model one short question, and print the answer and its price.

    It needs ANTHROPIC_API_KEY. It costs a fraction of a cent, and the ledger keeps it.
    """
    settings = get_settings()
    try:
        result = asyncio.run(_llm_check(settings, model or settings.llm_model))
    except ProviderError as error:
        typer.echo(f"No answer: {error}.")
        raise typer.Exit(1) from None
    price = cost_usd(result.model, result.usage)
    typer.echo(f"{result.model} answered: {result.value.title}")
    typer.echo(
        f"{result.usage.input_tokens} input and {result.usage.output_tokens} output tokens, "
        f"${price if price is not None else '?'}"
    )


async def _llm_check(settings: Settings, model: str) -> LLMResult[_Title]:
    engine = create_engine(settings.database_url)
    llm = CachedLLM(
        ClaudeLLM(model=model, effort="low"),
        create_session_factory(engine),
        model=model,
        budget_usd=settings.llm_daily_budget_usd,
        timezone=settings.timezone,
    )
    try:
        return await llm.generate(
            task="llm_check",
            # A new version each time: a check is always a real call, never a stored answer.
            prompt_version=f"check-{datetime.now(UTC).isoformat()}",
            system="Give the text a short title.",
            parts=[TextPart(text="Postgres can be a job queue: SKIP LOCKED lets workers share.")],
            schema=_Title,
            max_output_tokens=100,
        )
    finally:
        await llm.aclose()
        await engine.dispose()


@llm_app.command("costs")
def llm_costs() -> None:
    """Print what the calls of the model cost today and in the last 30 days."""
    settings = get_settings()
    today, month = asyncio.run(_llm_costs(settings))
    budget = settings.llm_daily_budget_usd
    for label, rows in (
        (f"Today ({settings.timezone}), budget ${budget}", today),
        ("Last 30 days", month),
    ):
        calls = sum(row[1] for row in rows)
        total = sum((row[4] for row in rows), Decimal(0))
        typer.echo(f"{label}: ${total:.4f}, {_calls(calls)}")
        for model, count, tokens_in, tokens_out, cost in rows:
            typer.echo(f"  {model}: ${cost:.4f}, {_calls(count)}, {tokens_in} in, {tokens_out} out")


def _calls(count: int) -> str:
    return "1 call" if count == 1 else f"{count} calls"


type _CostRow = tuple[str, int, int, int, Decimal]


async def _llm_costs(settings: Settings) -> tuple[list[_CostRow], list[_CostRow]]:
    zone = ZoneInfo(settings.timezone)
    midnight = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            found: list[list[_CostRow]] = []
            for since in (midnight, midnight - timedelta(days=30)):
                rows = await session.execute(
                    sa.select(
                        LLMCall.model,
                        sa.func.count(),
                        sa.func.sum(LLMCall.input_tokens),
                        sa.func.sum(LLMCall.output_tokens),
                        sa.func.sum(LLMCall.cost_usd),
                    )
                    .where(LLMCall.created_at >= since)
                    .group_by(LLMCall.model)
                    .order_by(LLMCall.model)
                )
                found.append(
                    [
                        (model, count, int(tokens_in), int(tokens_out), Decimal(cost))
                        for model, count, tokens_in, tokens_out, cost in rows.all()
                    ]
                )
            return found[0], found[1]
    finally:
        await engine.dispose()


# --- Batches of cards --------------------------------------------------------------------


@batch_app.command("send")
def batch_send(
    yes: Annotated[bool, typer.Option("--yes", help="Send without the question.")] = False,
) -> None:
    """Send the cards that wait as one batch, at half the price. It shows the price first.

    It needs UNSHELVE_CARDS=batch: in live mode the worker asks the model for each card at once,
    so a batch could pay for a card two times. The worker reads the answers when the batch has
    ended: most batches end within an hour, each within 24 hours.
    """
    settings = get_settings()
    if settings.cards != "batch":
        typer.echo(
            "Batch mode is off (UNSHELVE_CARDS=live): the worker asks the model for each card "
            "at once, so a batch could pay two times. Set UNSHELVE_CARDS=batch, start the "
            "worker again, and run this command again."
        )
        raise typer.Exit(1)
    asyncio.run(_batch_send(settings, yes=yes))


async def _batch_send(settings: Settings, *, yes: bool) -> None:
    engine = create_engine(settings.database_url)
    provider = build_batches(settings)
    try:
        sessions = create_session_factory(engine)
        waiting = await batches.waiting_cards(sessions, provider.model)
        if not waiting.questions:
            typer.echo("No card waits for a batch.")
            return
        cards = len(waiting.items)
        questions = len(waiting.questions)
        typer.echo(
            f"{_count(cards, 'card')} wait: {_count(questions, 'question')} to {provider.model}."
        )
        typer.echo(
            f"Estimated price, at half the normal price: ${waiting.estimated_cost_usd:.4f} for "
            f"about {waiting.input_tokens} input and {waiting.output_tokens} output tokens."
        )
        if not yes and not typer.confirm("Send this batch?"):
            typer.echo("Nothing was sent.")
            return
        try:
            batch_id = await batches.send_batch(sessions, provider, waiting)
        except ProviderError as error:
            typer.echo(f"Not sent: {error}.")
            raise typer.Exit(1) from None
        typer.echo(
            f"Batch {batch_id} sent. The worker reads its answers when it has ended: most "
            "batches end within an hour, each within 24 hours."
        )
    finally:
        if isinstance(provider, ClaudeBatches):
            await provider.aclose()
        await engine.dispose()


@batch_app.command("status")
def batch_status() -> None:
    """Each batch: its state, its answers and its price."""
    rows = asyncio.run(_batch_status(get_settings()))
    if not rows:
        typer.echo("No batch yet.")
        return
    for row in rows:
        line = (
            f"Batch {row.id}: {row.state}, {_count(row.questions, 'question')}, "
            f"{row.answered} answered, {row.not_answered} not answered, ${row.cost_usd:.4f} "
            f"(estimate ${row.estimated_cost_usd:.4f}), {row.created_at:%Y-%m-%d %H:%M} UTC."
        )
        typer.echo(f"{line} {row.error}." if row.error else line)


async def _batch_status(settings: Settings) -> list[LLMBatch]:
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            found = await session.scalars(sa.select(LLMBatch).order_by(LLMBatch.id))
            return list(found.all())
    finally:
        await engine.dispose()


def _count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


# --- Embeddings --------------------------------------------------------------------------


def _set_name(spec: EmbeddingSpec) -> str:
    return f"{spec.provider} {spec.model} {spec.dim}"


@embeddings_app.command("status")
def embeddings_status() -> None:
    """Print each embedding set with its vectors, and the set that search reads."""
    settings = get_settings()
    spec = embedder_spec(settings)
    found, ready, own, missing = asyncio.run(_embeddings_status(settings, spec))
    typer.echo(f"Embedder: {_set_name(spec)}, " + (f"set {own}" if own else "no vector yet"))
    typer.echo(f"Ready items: {ready}")
    for row in found:
        active = ", active" if row.is_active else ""
        typer.echo(
            f"Set {row.id}: {row.provider} {row.model} {row.dim}{active}, "
            f"{row.items} of {ready} items, {row.pieces} pieces"
        )
    if own is None or missing:
        typer.echo("Next: unshelve embeddings backfill")
    elif not next(row.is_active for row in found if row.id == own):
        typer.echo(f"Next: unshelve embeddings activate {own}")


async def _embeddings_status(
    settings: Settings, spec: EmbeddingSpec
) -> tuple[list[sets.SetSummary], int, int | None, int]:
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            found = await sets.summaries(session)
            ready = await sets.ready_items(session)
            own = await sets.find_set(session, spec)
            missing = await sets.missing(session, own) if own is not None else ready
            return found, ready, own, missing
    finally:
        await engine.dispose()


@embeddings_app.command("backfill")
def embeddings_backfill(
    every_item: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Each ready item, also one with its vectors. A text that did not change "
            "costs nothing.",
        ),
    ] = False,
) -> None:
    """Add an embed job for each ready item without a vector of the embedder.

    The worker runs the jobs. A new set starts inactive: search reads it after `activate`.
    """
    settings = get_settings()
    spec = embedder_spec(settings)
    set_id, added = asyncio.run(_embeddings_backfill(settings, spec, every_item=every_item))
    jobs = "1 embed job" if added == 1 else f"{added} embed jobs"
    typer.echo(f"Added {jobs} for set {set_id} ({_set_name(spec)}).")
    if added:
        typer.echo("The worker runs them. `unshelve embeddings status` shows the progress.")


async def _embeddings_backfill(
    settings: Settings, spec: EmbeddingSpec, *, every_item: bool
) -> tuple[int, int]:
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            set_id = await sets.ensure_set(session, spec)
            added = await sets.backfill(session, set_id, every_item=every_item)
            await session.commit()
            return set_id, added
    finally:
        await engine.dispose()


@embeddings_app.command("activate")
def embeddings_activate(
    set_id: int,
    force: Annotated[
        bool, typer.Option("--force", help="Also when ready items have no vector in the set.")
    ] = False,
) -> None:
    """Make an embedding set the one that search reads."""
    settings = get_settings()
    try:
        missing, ready = asyncio.run(_embeddings_activate(settings, set_id, force=force))
    except LookupError:
        raise _stop(f"There is no embedding set {set_id}.") from None
    if missing and not force:
        raise _stop(
            f"Set {set_id} has no vector for {missing} of {ready} ready items. "
            "Run `unshelve embeddings backfill` first, or add --force."
        )
    typer.echo(f"Search now reads set {set_id}.")


async def _embeddings_activate(settings: Settings, set_id: int, *, force: bool) -> tuple[int, int]:
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            if await session.get(EmbeddingSet, set_id) is None:
                raise LookupError(set_id)
            missing = await sets.missing(session, set_id)
            ready = await sets.ready_items(session)
            if missing and not force:
                return missing, ready
            await sets.activate(session, set_id)
            await session.commit()
            return missing, ready
    finally:
        await engine.dispose()


# A Persian question ("a job queue in Postgres"), a text on its topic, and a text on another.
_CHECK_QUERY = "صف کارها در پستگرس"
_CHECK_TEXTS = (
    "Postgres can be a job queue: SKIP LOCKED lets many workers share one table.",
    "A recipe for banana bread with walnuts and brown sugar.",
)


@embeddings_app.command("check")
def embeddings_check() -> None:
    """Embed three short texts with the embedder, and print how close they are.

    With Voyage, it needs VOYAGE_API_KEY, and it uses fewer than 100 of the free tokens.
    """
    settings = get_settings()
    spec = embedder_spec(settings)
    try:
        related, unrelated, dim = asyncio.run(_embeddings_check(settings))
    except ProviderError as error:
        raise _stop(f"No vectors: {str(error).rstrip('.')}.") from None
    typer.echo(f"{_set_name(spec)}: vectors of {dim} numbers")
    typer.echo(f"A Persian question and an English text on its topic: {related:.2f}")
    typer.echo(f"The same question and a text on another topic: {unrelated:.2f}")


async def _embeddings_check(settings: Settings) -> tuple[float, float, int]:
    embedder = build_embedder(settings)
    try:
        query = await embedder.embed_query(_CHECK_QUERY)
        related, unrelated = await embedder.embed_documents(list(_CHECK_TEXTS))
    finally:
        if isinstance(embedder, VoyageEmbedder):
            await embedder.aclose()
    return _cosine(query, related), _cosine(query, unrelated), len(query)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# --- Scores against the goals ------------------------------------------------------------


def _scoring_model(settings: Settings) -> str:
    """The model that the worker asks for the scores."""
    return settings.llm_model if settings.providers == "live" else FakeLLM.model


@goals_app.command("rescore")
def goals_rescore(
    yes: Annotated[bool, typer.Option("--yes", help="Start without the question.")] = False,
    every_item: Annotated[
        bool,
        typer.Option(
            "--every-item",
            help="Score each ready item again, also with scores of the current goals: after a "
            "change of the model, for example.",
        ),
    ] = False,
) -> None:
    """Score the items again against your goals, after you add a goal or change its meaning. It
    shows the price first.

    The worker scores each new item by itself. An item with scores keeps them when a goal
    changes, because a new score can cost a question to the model. A score with a label stays.
    """
    settings = get_settings()
    if settings.owner_telegram_id is None:
        raise _stop("No owner: set UNSHELVE_OWNER_TELEGRAM_ID.")
    asyncio.run(
        _goals_rescore(settings, settings.owner_telegram_id, yes=yes, every_item=every_item)
    )


async def _goals_rescore(
    settings: Settings, telegram_user_id: int, *, yes: bool, every_item: bool
) -> None:
    engine = create_engine(settings.database_url)
    embedder = build_embedder(settings)
    model = _scoring_model(settings)
    try:
        sessions = create_session_factory(engine)
        async with sessions() as session:
            owner_id = await auth_store.ensure_owner(session, telegram_user_id)
            await session.commit()
        try:
            found = await rescore.plan(
                sessions,
                embedder,
                owner_id,
                model=model,
                prefilter=settings.score_prefilter,
                every_item=every_item,
            )
        except ProviderError as error:
            raise _stop(f"The goals got no vectors: {str(error).rstrip('.')}.") from None
        if not found.goals:
            typer.echo("No active goal. Add your goals on the page Goals first.")
            return
        if not found.items:
            typer.echo(f"Each item has scores of the {_count(found.goals, 'active goal')}.")
            return
        typer.echo(
            f"{_count(found.goals, 'active goal')}: {_count(len(found.items), 'item')} to score."
        )
        if found.far:
            typer.echo(
                f"- {found.far} far from each goal get 0 without a question "
                f"(UNSHELVE_SCORE_PREFILTER={settings.score_prefilter})."
            )
        if found.stored:
            typer.echo(f"- {found.stored} have their answer in the ledger already: no cost.")
        price = "an unknown price" if found.cost_usd is None else f"about ${found.cost_usd:.4f}"
        typer.echo(
            f"- {_count(found.questions, 'question')} to {model}: {price} for about "
            f"{found.input_tokens} input and {found.output_tokens} output tokens."
        )
        if found.cost_usd is not None and found.cost_usd > settings.llm_daily_budget_usd:
            typer.echo(
                f"The price is more than the daily budget of ${settings.llm_daily_budget_usd}: "
                "the worker asks until the budget of the day is used, and the rest waits for the "
                "next day."
            )
        if not yes and not typer.confirm("Score them?"):
            typer.echo("Nothing was started.")
            return
        started = await rescore.start(sessions, owner_id, found.items)
        typer.echo(f"The worker scores {_count(started, 'item')} now.")
    finally:
        if isinstance(embedder, VoyageEmbedder):
            await embedder.aclose()
        await engine.dispose()


# --- The golden set ----------------------------------------------------------------------


@labels_app.command("export")
def labels_export() -> None:
    """Write your verdicts on the scores as an evaluation set: one JSON line for each.

    The lines go to the standard output, and a summary to the standard error. So a redirect
    gives a clean file, for example:

        docker compose exec -T api unshelve labels export > exports/golden-set.jsonl

    The same database gives the same file. The file holds your goals and your items: keep it out
    of git (the folder exports/ is ignored).
    """
    settings = get_settings()
    if settings.owner_telegram_id is None:
        raise _stop("No owner: set UNSHELVE_OWNER_TELEGRAM_ID.")
    lines = asyncio.run(_labels_export(settings, settings.owner_telegram_id))
    sys.stdout.write(golden.to_jsonl(lines))
    sys.stdout.flush()
    confirmed = sum(line["label"] == "confirmed" for line in lines)
    sources = Counter(str(line["label_source"]) for line in lines)
    typer.echo(
        f"{_count(len(lines), 'verdict')}: {confirmed} confirmed, {len(lines) - confirmed} "
        f"rejected; {sum(bool(line['relevant']) for line in lines)} relevant. Sources: "
        + (", ".join(f"{source} {count}" for source, count in sorted(sources.items())) or "none")
        + ".",
        err=True,
    )


async def _labels_export(settings: Settings, telegram_user_id: int) -> list[dict[str, object]]:
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            owner_id = await auth_store.ensure_owner(session, telegram_user_id)
            lines = await golden.golden_set(session, owner_id)
            await session.commit()
            return lines
    finally:
        await engine.dispose()


# --- unshelve search ---------------------------------------------------------------------

# What the vector list says when the vector retriever did not take part.
_NO_VECTOR_LIST = {
    hybrid.VectorSearch.NO_VECTORS: (
        "  no vectors of this embedder in the active set: unshelve embeddings status"
    ),
    hybrid.VectorSearch.FAILED: "  the embedder did not answer: the fused list has the words only",
}


def _list_of(ranking: hybrid.Ranking, place: Callable[[hybrid.Ranks], int | None]) -> list[int]:
    """The list of one retriever, read back from the places that the hits keep."""
    placed = [(rank, hit.item_id) for hit in ranking.hits if (rank := place(hit.ranks)) is not None]
    return [item_id for _, item_id in sorted(placed)]


@search_app.command("retrievers")
def search_retrievers(
    query: Annotated[str, typer.Argument(help="What to look for, as in a web search.")],
    limit: Annotated[
        int, typer.Option(min=1, max=50, help="At most this many items in each list.")
    ] = 10,
) -> None:
    """Print what each of the three retrievers finds, and their fused list, the best first
    (docs/learn/36 and 38).

    The fused list is the ranking of the search API. With a live embedder, the query costs one
    call of the embedder.
    """
    settings = get_settings()
    if settings.owner_telegram_id is None:
        raise _stop("UNSHELVE_OWNER_TELEGRAM_ID is not set. Search needs its owner.")
    ranking, titles = asyncio.run(_search(settings, settings.owner_telegram_id, query))
    lists: list[tuple[str, Callable[[hybrid.Ranks], int | None]]] = [
        ("full text", lambda ranks: ranks.full_text),
        ("trigram", lambda ranks: ranks.trigram),
        ("vector", lambda ranks: ranks.vector),
    ]
    for name, place in lists:
        typer.echo(f"{name}:")
        if name == "vector" and ranking.vector_search in _NO_VECTOR_LIST:
            typer.echo(_NO_VECTOR_LIST[ranking.vector_search])
            continue
        found = _list_of(ranking, place)[:limit]
        if not found:
            typer.echo("  nothing")
        for rank, item_id in enumerate(found, start=1):
            typer.echo(f"  {rank}. #{item_id} {titles[item_id]}")
    typer.echo("fused:")
    if not ranking.hits:
        typer.echo("  nothing")
    for rank, hit in enumerate(ranking.hits[:limit], start=1):
        typer.echo(f"  {rank}. #{hit.item_id} {hit.score:.4f} {titles[hit.item_id]}")


async def _search(
    settings: Settings, owner_telegram_id: int, query: str
) -> tuple[hybrid.Ranking, dict[int, str]]:
    engine = create_engine(settings.database_url)
    services = build_services(settings, create_session_factory(engine))
    try:
        async with services.sessions() as session:
            owner_id = await auth_store.ensure_owner(session, owner_telegram_id)
            await session.commit()
            ranking = await hybrid.search(session, services.embedder, owner_id, query)
            rows = await session.execute(
                sa.select(Item.id, Item.title, Item.canonical_url).where(
                    Item.id.in_([hit.item_id for hit in ranking.hits])
                )
            )
            titles = {row.id: row.title or row.canonical_url or "(no title)" for row in rows}
        return ranking, titles
    finally:
        await close_services(services)
        await engine.dispose()
