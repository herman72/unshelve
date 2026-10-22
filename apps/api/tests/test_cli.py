import asyncio
import dataclasses
import json
import re
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner, Result

from tests.bot.support import PretendTelegram, offline_bot
from tests.ingest.support import DESKTOP, MACOS, MADE_UP, made_up_with_files
from tests.publish.support import LEASES_SLUG, OWNER_TELEGRAM_ID, PERSIAN_SLUG, seed_world
from unshelve import __version__, cli
from unshelve.auth import store as auth_store
from unshelve.bot import cards
from unshelve.cli import app
from unshelve.config import Settings, get_settings
from unshelve.contracts.dto import (
    CaptureEnvelope,
    Extracted,
    FetchResult,
    LLMResult,
    LLMUsage,
    SourceRef,
)
from unshelve.contracts.enums import (
    CaptureSource,
    EnrichState,
    GoalLabel,
    ItemKind,
    JobKind,
    JobState,
    LabelSource,
)
from unshelve.contracts.errors import ExtractError, ProviderError
from unshelve.contracts.protocols import ExtractContext
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.db.models import (
    Base,
    Capture,
    Digest,
    Goal,
    Item,
    ItemContent,
    ItemGoal,
    ItemKey,
    Job,
    LLMBatch,
    LLMCall,
)
from unshelve.extract import image as image_extractor
from unshelve.extract.article import ArticleExtractor
from unshelve.extract.youtube import YouTubeExtractor
from unshelve.ingest import resolve
from unshelve.net.fetcher import LiveFetcher
from unshelve.providers.claude import ClaudeLLM
from unshelve.providers.voyage import VoyageEmbedder, voyage_spec
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.relevance import rescore
from unshelve.services import build_services
from unshelve.testing.conformance import TINY_PNG
from unshelve.testing.fakes import FakeBatches, FakeEmbedder

runner = CliRunner()

_COLOUR_CODE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """The text without colour codes.

    Typer writes its own messages (the help, a missing option) in colour when it runs on
    GitHub Actions, and not on a developer's computer. A colour code can be in the middle
    of a word, so a test that reads such a message removes the codes first.
    """
    return _COLOUR_CODE.sub("", text)


def test_version_prints_the_installed_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_every_service_command_is_registered() -> None:
    result = runner.invoke(app, ["--help"])

    for command in (
        "api",
        "worker",
        "bot",
        "migrate",
        "auth",
        "import",
        "rekey",
        "digest",
        "fetch",
        "extract",
        "llm",
        "embeddings",
    ):
        assert command in _plain(result.stdout)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    empty_dir: Path,
    *args: str,
    answer: str | None = None,
    **env: str,
) -> Result:
    """Run a command with these settings and no others. ``answer``: what a person types."""
    # The command reads .env relative to the current directory. A developer's own .env must
    # not reach the test, so the command runs in a directory that has none. The database stays
    # the one of conftest, which does not exist, unless the test gives its own.
    monkeypatch.chdir(empty_dir)
    for name in (
        "UNSHELVE_OWNER_TELEGRAM_ID",
        "UNSHELVE_WEB_BASE_URL",
        "UNSHELVE_CAPTURE_CHAT_IDS",
        "UNSHELVE_EMBEDDER",
        "VOYAGE_API_KEY",
        "UNSHELVE_PUBLISH_TARGET",
        "UNSHELVE_PUBLISH_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        return runner.invoke(app, list(args), input=answer)
    finally:
        get_settings.cache_clear()


def _auth_link(monkeypatch: pytest.MonkeyPatch, empty_dir: Path, **env: str) -> Result:
    return _run(monkeypatch, empty_dir, "auth", "link", **env)


def test_fetch_refuses_an_address_inside_the_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The metadata service of a cloud machine. The guard refuses it before any connection.
    result = _run(monkeypatch, tmp_path, "fetch", "http://169.254.169.254/latest/meta-data/")

    assert result.exit_code == 1
    assert "Not fetched: blocked_address (169.254.169.254: link-local)." in _plain(result.output)


def test_extract_prints_the_facts_and_the_start_of_the_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def pretend(self: ArticleExtractor, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        return Extracted(
            kind=ItemKind.ARTICLE,
            text="word " * 300,
            title="Queues in Postgres",
            site="blog.example.com",
            lang="en",
            published_at=datetime(2026, 9, 30, tzinfo=UTC),
        )

    monkeypatch.setattr(ArticleExtractor, "extract", pretend)

    result = _run(monkeypatch, tmp_path, "extract", "https://blog.example.com/queues")

    assert result.exit_code == 0
    lines = _plain(result.output).splitlines()
    assert "Reader: article" in lines
    assert "Title:  Queues in Postgres" in lines
    assert "Site:   blog.example.com · language en · date 2026-09-30" in lines
    assert any(line.startswith("Text:   300 words, read in ") for line in lines)
    assert lines[-1].endswith("word …")  # the start of the text, cut after a whole word


def test_extract_says_why_a_page_is_no_article(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def no_article(self: ArticleExtractor, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        raise ExtractError("the page has only 47 characters of text")

    monkeypatch.setattr(ArticleExtractor, "extract", no_article)

    result = _run(monkeypatch, tmp_path, "extract", "https://app.example.com/")

    assert result.exit_code == 1
    assert "Nothing read (article): the page has only 47 characters of text." in _plain(
        result.output
    )


def test_extract_reads_a_video_with_the_youtube_extractor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def pretend(self: YouTubeExtractor, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        return Extracted(
            kind=ItemKind.VIDEO,
            text="Job queues in Postgres\nA video by Made-up Channel on YouTube, 31 minutes.",
            title="Job queues in Postgres",
            author="Made-up Channel",
            site="youtube.com",
            minutes=31,
        )

    monkeypatch.setattr(YouTubeExtractor, "extract", pretend)

    result = _run(monkeypatch, tmp_path, "extract", "https://youtu.be/dEmOvIdEo01")

    assert result.exit_code == 0, result.output
    lines = _plain(result.output).splitlines()
    assert "Reader: youtube" in lines
    assert "Length: 31 min" in lines


def test_extract_says_when_no_extractor_reads_a_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run(monkeypatch, tmp_path, "extract", "https://example.com/queue-data.zip")

    assert result.exit_code == 1
    assert "No extractor reads this link yet" in _plain(result.output)


def test_extract_reads_a_pdf_file_of_this_computer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paper = Path(__file__).parent / "extract" / "fixtures" / "pdf" / "paper.pdf"

    result = _run(monkeypatch, tmp_path, "extract", "--file", str(paper))

    assert result.exit_code == 0, result.output
    lines = _plain(result.output).splitlines()
    assert "Reader: pdf" in lines
    assert "Title:  Leases for Job Queues in Postgres" in lines


def test_extract_reads_an_image_with_the_model_of_the_worker_and_its_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    # The pretend model of UNSHELVE_PROVIDERS=fake: no network, and the ledger keeps the call.
    # The name has no type: the first bytes of the file say that it is an image.
    image = tmp_path / "whiteboard"
    image.write_bytes(TINY_PNG)

    result = _run(
        monkeypatch,
        tmp_path,
        "extract",
        "--file",
        str(image),
        UNSHELVE_DATABASE_URL=private_database_url,
    )

    assert result.exit_code == 0, result.output
    lines = _plain(result.output).splitlines()
    assert "Reader: image" in lines
    assert any(line.startswith("An image: ") for line in lines)
    assert asyncio.run(_count_llm_calls(private_database_url)) == 1


@pytest.mark.parametrize("arguments", [[], ["https://example.com/a", "--file", "x.png"]])
def test_extract_needs_one_link_or_one_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]
) -> None:
    (tmp_path / "x.png").write_bytes(TINY_PNG)

    result = _run(monkeypatch, tmp_path, "extract", *arguments)

    assert result.exit_code == 2
    assert "Give one link, or one file with --file." in _plain(result.output)


def test_extract_says_when_a_file_is_too_large_for_its_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(image_extractor, "MAX_IMAGE_BYTES", len(TINY_PNG) - 1)
    image = tmp_path / "whiteboard.png"
    image.write_bytes(TINY_PNG)

    result = _run(monkeypatch, tmp_path, "extract", "--file", str(image))

    assert result.exit_code == 1
    assert "Not fetched: too_large." in _plain(result.output)


def test_a_command_without_a_database_of_its_own_reaches_no_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An image goes to the model through the ledger. Without UNSHELVE_DATABASE_URL, the ledger
    # gets the database of conftest, where nothing listens: the command fails, and writes nothing.
    image = tmp_path / "whiteboard.png"
    image.write_bytes(TINY_PNG)

    result = _run(monkeypatch, tmp_path, "extract", "--file", str(image))

    assert result.exit_code == 1
    assert isinstance(result.exception, OperationalError)


def test_extract_says_when_no_extractor_reads_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("A list of things to read.", encoding="utf-8")

    result = _run(monkeypatch, tmp_path, "extract", "--file", str(notes))

    assert result.exit_code == 1
    assert "No extractor reads this file yet" in _plain(result.output)


def _waiting_cards(database_url: str, *texts: str) -> None:
    """Items that the fetch step read, whose cards wait for a batch."""

    async def store() -> None:
        engine = create_engine(database_url)
        try:
            async with create_session_factory(engine)() as db:
                owner_id = await auth_store.ensure_owner(db, 4242)
                for number, text in enumerate(texts, start=1):
                    item = Item(
                        owner_id=owner_id,
                        canonical_url=f"https://example.com/{number}",
                        first_captured_at=datetime(2026, 10, 16, tzinfo=UTC),
                        last_captured_at=datetime(2026, 10, 16, tzinfo=UTC),
                        enrich_state=EnrichState.FETCHED,
                    )
                    db.add(item)
                    await db.flush()
                    db.add(
                        ItemContent(
                            item_id=item.id,
                            owner_id=owner_id,
                            text=text,
                            content_hash=f"{number:064x}",
                            extractor="article",
                            extractor_version=1,
                        )
                    )
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(store())


async def _batches(database_url: str) -> list[tuple[int, str]]:
    engine = create_engine(database_url)
    try:
        async with create_session_factory(engine)() as db:
            rows = await db.execute(sa.select(LLMBatch.id, LLMBatch.state).order_by(LLMBatch.id))
            return [(row.id, row.state) for row in rows]
    finally:
        await engine.dispose()


BATCH_MODE = {"UNSHELVE_CARDS": "batch", "UNSHELVE_PROVIDERS": "fake"}


def test_llm_batch_send_needs_batch_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _run(monkeypatch, tmp_path, "llm", "batch", "send", UNSHELVE_CARDS="live")

    assert result.exit_code == 1
    assert "Batch mode is off (UNSHELVE_CARDS=live)" in _plain(result.output)


def test_llm_batch_send_says_when_no_card_waits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    result = _run(
        monkeypatch,
        tmp_path,
        "llm",
        "batch",
        "send",
        UNSHELVE_DATABASE_URL=private_database_url,
        **BATCH_MODE,
    )

    assert result.exit_code == 0, result.output
    assert _plain(result.output).strip() == "No card waits for a batch."


def test_llm_batch_send_shows_the_price_and_sends_nothing_after_a_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    # The price of Claude Haiku 5.5, with a pretend batch API: no request leaves the computer.
    _waiting_cards(private_database_url, "Leases make a queue safe.", "A heartbeat moves it.")

    def haiku_batches(settings: Settings) -> FakeBatches:
        return FakeBatches(model="claude-haiku-5-5")

    monkeypatch.setattr(cli, "build_batches", haiku_batches)

    result = _run(
        monkeypatch,
        tmp_path,
        "llm",
        "batch",
        "send",
        answer="n\n",
        UNSHELVE_DATABASE_URL=private_database_url,
        **BATCH_MODE,
    )

    assert result.exit_code == 0, result.output
    assert _plain(result.output).splitlines() == [
        "2 cards wait: 2 questions to claude-haiku-5-5.",
        "Estimated price, at half the normal price: $0.0003 for about 1841 input and 800 "
        "output tokens.",
        "Send this batch? [y/N]: n",
        "Nothing was sent.",
    ]
    assert asyncio.run(_batches(private_database_url)) == []


def test_llm_batch_send_sends_after_a_yes_and_status_lists_the_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _waiting_cards(private_database_url, "Leases make a queue safe.", "A heartbeat moves it.")
    database = {"UNSHELVE_DATABASE_URL": private_database_url}
    before = _run(monkeypatch, tmp_path, "llm", "batch", "status", **database, **BATCH_MODE)

    sent = _run(
        monkeypatch, tmp_path, "llm", "batch", "send", answer="y\n", **database, **BATCH_MODE
    )
    after = _run(monkeypatch, tmp_path, "llm", "batch", "status", **database, **BATCH_MODE)

    assert _plain(before.output).strip() == "No batch yet."
    assert sent.exit_code == 0, sent.output
    assert _plain(sent.output).splitlines()[-1] == (
        "Batch 1 sent. The worker reads its answers when it has ended: most batches end "
        "within an hour, each within 24 hours."
    )
    assert asyncio.run(_batches(private_database_url)) == [(1, "sent")]
    [line] = _plain(after.output).splitlines()
    assert line.startswith(
        "Batch 1: sent, 2 questions, 0 answered, 0 not answered, $0.0000 (estimate $0.0000), "
    )


def test_llm_check_prints_the_answer_and_its_price(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    async def pretend(self: ClaudeLLM, **question: Any) -> LLMResult[Any]:
        schema = question["schema"]
        return LLMResult[schema](
            value=schema(title="Job queues in Postgres"),
            model="claude-haiku-5-5",
            usage=LLMUsage(input_tokens=1200, output_tokens=40),
        )

    monkeypatch.setattr(ClaudeLLM, "generate", pretend)

    result = _run(
        monkeypatch,
        tmp_path,
        "llm",
        "check",
        "--model",
        "claude-haiku-5-5",
        UNSHELVE_DATABASE_URL=private_database_url,
    )

    assert result.exit_code == 0, result.output
    lines = _plain(result.output).splitlines()
    assert "claude-haiku-5-5 answered: Job queues in Postgres" in lines
    # 1,200 x $0.10 and 40 x $0.50 for one million tokens.
    assert "1200 input and 40 output tokens, $0.00014" in lines
    assert asyncio.run(_count_llm_calls(private_database_url)) == 1  # the ledger keeps it


def test_llm_check_says_why_there_is_no_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    async def refused(self: ClaudeLLM, **question: Any) -> LLMResult[Any]:
        raise ProviderError("401 authentication_error: a person must fix it", retryable=True)

    monkeypatch.setattr(ClaudeLLM, "generate", refused)

    result = _run(monkeypatch, tmp_path, "llm", "check", UNSHELVE_DATABASE_URL=private_database_url)

    assert result.exit_code == 1
    assert "No answer: 401 authentication_error: a person must fix it." in _plain(result.output)


async def _count_llm_calls(database_url: str) -> int:
    engine = create_engine(database_url)
    try:
        async with create_session_factory(engine)() as db:
            return (await db.execute(sa.select(sa.func.count()).select_from(LLMCall))).scalar_one()
    finally:
        await engine.dispose()


def test_llm_costs_prints_today_and_the_last_30_days(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    async def spend() -> None:
        engine = create_engine(private_database_url)
        now = datetime.now(UTC)
        try:
            async with create_session_factory(engine)() as db:
                for key, model, cost, at in (
                    ("today", "claude-opus-5-5", "0.03", now),
                    ("ten days ago", "claude-opus-5-5", "0.02", now - timedelta(days=10)),
                    ("forty days ago", "claude-haiku-5-5", "0.001", now - timedelta(days=40)),
                ):
                    db.add(
                        LLMCall(
                            cache_key=key,
                            task="enrich_item",
                            model=model,
                            prompt_version="enrich-1",
                            request={},
                            response={},
                            input_tokens=1000,
                            output_tokens=100,
                            cost_usd=Decimal(cost),
                            created_at=at,
                        )
                    )
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(spend())

    result = _run(
        monkeypatch,
        tmp_path,
        "llm",
        "costs",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_TIMEZONE="UTC",
    )

    assert result.exit_code == 0, result.output
    assert _plain(result.output).splitlines() == [
        "Today (UTC), budget $1.00: $0.0300, 1 call",
        "  claude-opus-5-5: $0.0300, 1 call, 1000 in, 100 out",
        "Last 30 days: $0.0500, 2 calls",
        "  claude-opus-5-5: $0.0500, 2 calls, 2000 in, 200 out",
    ]


def test_fetch_prints_what_came_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def pretend(
        self: LiveFetcher, url: str, *, max_bytes: int, accept: list[str]
    ) -> FetchResult:
        assert (max_bytes, accept) == (5_000_000, ["text/html"])
        return FetchResult(
            url=url,
            final_url="https://example.com/new",
            status=200,
            content_type="text/html; charset=utf-8",
            body=b"<p>hello</p>",
        )

    monkeypatch.setattr(LiveFetcher, "get", pretend)

    result = _run(
        monkeypatch, tmp_path, "fetch", "https://example.com/old", "--accept", "text/html"
    )

    assert result.exit_code == 0
    lines = _plain(result.output).splitlines()
    assert "200 https://example.com/new" in lines
    assert any(line.startswith("text/html; charset=utf-8, 12 bytes, ") for line in lines)


def test_auth_link_prints_a_link_that_signs_the_owner_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    result = _auth_link(
        monkeypatch,
        tmp_path,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_WEB_BASE_URL="http://localhost:3000/",
    )

    assert result.exit_code == 0
    assert "works once, for 5 minutes" in result.stderr
    link = result.stdout.strip()
    # The token is in the part after "#", which a browser does not send to a server.
    address, _, token = link.partition("#token=")
    assert address == "http://localhost:3000/login"

    async def consume() -> int | None:
        engine = create_engine(private_database_url)
        try:
            async with create_session_factory(engine)() as db:
                return await auth_store.consume_login_token(db, token)
        finally:
            await engine.dispose()

    assert asyncio.run(consume()) == 1


def test_auth_link_refuses_to_run_without_an_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _auth_link(
        monkeypatch,
        tmp_path,
        # Nothing listens here. The command must stop before it needs the database.
        UNSHELVE_DATABASE_URL="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
    )

    assert result.exit_code == 1
    assert "UNSHELVE_OWNER_TELEGRAM_ID is not set" in result.stderr
    assert result.stdout == ""


# --- unshelve import -----------------------------------------------------------------------

CHANNEL = "-1001234567890"
NOWHERE = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"  # nothing listens here


def _import(
    monkeypatch: pytest.MonkeyPatch,
    empty_dir: Path,
    database_url: str,
    folder: Path,
    *options: str,
    chats: str = f"[{CHANNEL}]",
) -> Result:
    return _run(
        monkeypatch,
        empty_dir,
        "import",
        str(folder),
        *options,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
        UNSHELVE_DATABASE_URL=database_url,
        UNSHELVE_CAPTURE_CHAT_IDS=chats,
    )


def _stored(database_url: str) -> list[tuple[int, int, datetime]]:
    """The chat, the message id and the moment of each stored post."""

    async def read() -> list[tuple[int, int, datetime]]:
        engine = create_engine(database_url)
        try:
            async with create_session_factory(engine)() as db:
                rows = await db.execute(
                    sa.select(Capture.chat_id, Capture.message_id, Capture.posted_at).order_by(
                        Capture.message_id
                    )
                )
                return [(row.chat_id, row.message_id, row.posted_at) for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(read())


def _albums(database_url: str) -> list[tuple[int, str | None]]:
    """The message id and the album of each stored post."""

    async def read() -> list[tuple[int, str | None]]:
        engine = create_engine(database_url)
        try:
            async with create_session_factory(engine)() as db:
                rows = await db.execute(
                    sa.select(Capture.message_id, Capture.media_group_id).order_by(
                        Capture.message_id
                    )
                )
                return [(row.message_id, row.media_group_id) for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_import_stores_the_posts_of_an_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    result = _import(
        monkeypatch, tmp_path, private_database_url, DESKTOP, "--timezone", "Europe/Helsinki"
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        'Export of "Unshelve test": 5 posts, into the chat -1001234567890.',
        "From 2026-10-13 22:40 to 2026-10-13 22:44, clock time of Europe/Helsinki.",
        "Not imported: 1 x service message.",
        "Stored 5 new posts. 0 were stored already.",
        # The recorded export names a photo, and its folder does not have it (no media).
        "Files: 0 copied, 1 not in the folder.",
    ]
    stored = _stored(private_database_url)
    assert [(chat, message) for chat, message, _ in stored] == [
        (int(CHANNEL), message) for message in (2, 3, 4, 5, 6)
    ]
    # 22:40:37 on the clock of a computer in Helsinki, in summer time.
    assert stored[0][2] == datetime(2026, 10, 13, 19, 40, 37, tzinfo=UTC)


def test_import_is_safe_to_run_again_and_with_the_export_of_the_other_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _import(monkeypatch, tmp_path, private_database_url, DESKTOP, "--timezone", "Europe/Helsinki")

    again = _import(
        monkeypatch, tmp_path, private_database_url, DESKTOP, "--timezone", "Europe/Helsinki"
    )
    other_app = _import(
        monkeypatch, tmp_path, private_database_url, MACOS, "--timezone", "Europe/Helsinki"
    )

    assert "Stored 0 new posts. 5 were stored already." in again.stdout.splitlines()
    assert "Stored 0 new posts. 5 were stored already." in other_app.stdout.splitlines()
    assert len(_stored(private_database_url)) == 5


def test_import_dry_run_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    result = _import(
        monkeypatch,
        tmp_path,
        private_database_url,
        MADE_UP,
        "--timezone",
        "Europe/Helsinki",
        "--dry-run",
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        'Export of "Unshelve test": 16 posts, into the chat -1001234567890.',
        "From 2026-10-13 23:25 to 2026-10-14 00:15, clock time of Europe/Helsinki.",
        "Albums found: 2. Each album becomes one item.",
        "Not imported: 1 x location.",
        "Not imported: 1 x poll.",
        "Not imported: 1 x rich message.",
        "Not imported: 1 x service message.",
        "Dry run: 16 posts are new, 0 are stored already. Nothing was changed.",
        "Files: 0 to copy, 6 not in the folder.",
    ]
    assert _stored(private_database_url) == []


def test_import_can_take_a_part_of_an_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    result = _import(
        monkeypatch,
        tmp_path,
        private_database_url,
        MADE_UP,
        "--timezone",
        "Europe/Helsinki",
        "--since",
        "2026-10-14",
        "--limit",
        "2",
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        'Export of "Unshelve test": 2 posts, into the chat -1001234567890.',
        "From 2026-10-14 00:10 to 2026-10-14 00:10, clock time of Europe/Helsinki.",
        "Albums found: 1. Each album becomes one item.",
        # The poll is in the part of the export that this import did not take.
        "Not imported (in the whole export): 1 x location.",
        "Not imported (in the whole export): 1 x poll.",
        "Not imported (in the whole export): 1 x rich message.",
        "Not imported (in the whole export): 1 x service message.",
        "Stored 2 new posts. 0 were stored already.",
        "Files: 0 copied, 1 not in the folder.",
    ]
    assert [message for _, message, _ in _stored(private_database_url)] == [23, 24]


def test_import_keeps_an_album_that_the_limit_cut(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    options = (MADE_UP, "--timezone", "Europe/Helsinki")

    first = _import(monkeypatch, tmp_path, private_database_url, *options, "--limit", "1")
    _import(monkeypatch, tmp_path, private_database_url, *options)

    # Post 7 is the first photo of an album of three. Alone, it does not look like an album.
    assert "Albums found: 1. Each album becomes one item." in first.stdout
    assert _albums(private_database_url)[:4] == [
        (7, "export:7"),
        (8, "export:7"),
        (9, "export:7"),
        (10, None),
    ]


def test_import_counts_one_post_as_one_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    options = (MADE_UP, "--timezone", "Europe/Helsinki")

    one = _import(monkeypatch, tmp_path, private_database_url, *options, "--limit", "1")
    two = _import(
        monkeypatch, tmp_path, private_database_url, *options, "--limit", "2", "--dry-run"
    )
    again = _import(monkeypatch, tmp_path, private_database_url, *options, "--limit", "1")

    assert one.stdout.splitlines()[0] == (
        'Export of "Unshelve test": 1 post, into the chat -1001234567890.'
    )
    assert "Stored 1 new post. 0 were stored already." in one.stdout.splitlines()
    assert (
        "Dry run: 1 post is new, 1 is stored already. Nothing was changed."
        in two.stdout.splitlines()
    )
    assert "Stored 0 new posts. 1 was stored already." in again.stdout.splitlines()


def test_import_copies_the_files_of_an_export_one_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    # The first run finds no file; then the files are put into the folder.
    folder = tmp_path / "export"
    shutil.copytree(MADE_UP, folder)
    empty = tmp_path / "empty"
    empty.mkdir()

    def run() -> list[str]:
        result = _import(
            monkeypatch, empty, private_database_url, folder, "--timezone", "Europe/Helsinki"
        )
        assert result.exit_code == 0, result.output
        return result.stdout.splitlines()

    without = run()
    made_up_with_files(tmp_path / "with-files")
    for name in ("photos", "files"):
        shutil.copytree(tmp_path / "with-files" / name, folder / name)
    filled = run()
    again = run()

    assert without[-2:] == [
        "Stored 16 new posts. 0 were stored already.",
        "Files: 0 copied, 6 not in the folder.",
    ]
    assert filled[-3:] == [
        "Stored 0 new posts. 16 were stored already.",
        "Files: 3 copied, 3 not in the folder.",
        "Posts of an earlier import that got their files: 3. Their items are read again.",
    ]
    assert again[-2:] == [
        "Stored 0 new posts. 16 were stored already.",
        "Files: 0 copied, 3 copied before, 3 not in the folder.",
    ]

    async def pdf_key() -> str | None:
        engine = create_engine(private_database_url)
        try:
            async with create_session_factory(engine)() as db:
                media = await db.scalar(sa.select(Capture.media).where(Capture.message_id == 23))
                return media[0]["sha256"] if media else None
        finally:
            await engine.dispose()

    assert asyncio.run(pdf_key()) is not None


def test_import_takes_the_chat_from_an_option(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    result = _import(
        monkeypatch,
        tmp_path,
        private_database_url,
        DESKTOP,
        "--timezone",
        "Europe/Helsinki",
        "--chat-id",
        "-1009999999999",  # a negative number, and not an option
        chats="[-1001234567890, -1005555555555]",
    )

    assert result.exit_code == 0, result.output
    assert {chat for chat, _, _ in _stored(private_database_url)} == {-1009999999999}


@pytest.mark.parametrize("chats", ["[]", "[-1001234567890, -1005555555555]"])
def test_import_asks_for_the_chat_when_the_settings_do_not_name_one_chat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chats: str
) -> None:
    result = _import(
        monkeypatch, tmp_path, NOWHERE, DESKTOP, "--timezone", "Europe/Helsinki", chats=chats
    )

    assert result.exit_code == 1
    assert "Give the chat of the export with --chat-id" in result.stderr
    assert result.stdout == ""


def test_import_refuses_a_name_that_is_not_a_time_zone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _import(monkeypatch, tmp_path, NOWHERE, DESKTOP, "--timezone", "Helsinki")

    assert result.exit_code == 1
    assert "'Helsinki' is not the name of a time zone" in result.stderr


def test_import_needs_the_time_zone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _import(monkeypatch, tmp_path, NOWHERE, DESKTOP)

    assert result.exit_code == 2
    assert "Missing option '--timezone'" in _plain(result.stderr)


def test_import_refuses_to_run_without_an_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run(
        monkeypatch,
        tmp_path,
        "import",
        str(DESKTOP),
        "--timezone",
        "Europe/Helsinki",
        UNSHELVE_DATABASE_URL=NOWHERE,
    )

    assert result.exit_code == 1
    assert "UNSHELVE_OWNER_TELEGRAM_ID is not set" in result.stderr


def test_import_explains_a_folder_that_is_not_an_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "not-an-export"
    folder.mkdir()
    (folder / "result.json").write_text("{}")

    result = _import(monkeypatch, tmp_path, NOWHERE, folder, "--timezone", "Europe/Helsinki")

    assert result.exit_code == 1
    assert "This export cannot be read" in result.stderr
    assert "has no file messages.html" in result.stderr


def test_import_names_the_post_that_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "broken"
    folder.mkdir()
    page = (DESKTOP / "messages.html").read_text(encoding="utf-8")
    (folder / "messages.html").write_text(
        page.replace('title="13.10.2026 22:41:29 UTC+02:00"', 'title="soon"'), encoding="utf-8"
    )

    result = _import(monkeypatch, tmp_path, NOWHERE, folder, "--timezone", "Europe/Helsinki")

    assert result.exit_code == 1
    assert "messages.html: message 3 has a date in a form" in result.stderr


# --- unshelve rekey ------------------------------------------------------------------------


def test_rekey_gives_keys_to_the_items_that_exist_and_joins_the_same_things(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    # The made-up export has 16 posts in 13 saves. Each save is an item with its keys.
    _import(monkeypatch, tmp_path, private_database_url, MADE_UP, "--timezone", "Europe/Helsinki")

    async def work_and_forget_the_keys() -> None:
        engine = create_engine(private_database_url)
        try:
            sessions = create_session_factory(engine)
            async with sessions() as db:
                await db.execute(sa.update(Job).values(run_after=sa.func.now()))
                await db.commit()
            discover()
            settings = Settings(env="test", database_url=private_database_url)
            worker = Worker(build_services(settings, sessions), registry, WorkerConfig("w1"))
            while await worker.run_one():
                pass
            async with sessions() as db:
                await db.execute(sa.delete(ItemKey))
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(work_and_forget_the_keys())

    first = _run(monkeypatch, tmp_path, "rekey", UNSHELVE_DATABASE_URL=private_database_url)
    again = _run(monkeypatch, tmp_path, "rekey", UNSHELVE_DATABASE_URL=private_database_url)

    assert first.exit_code == 0, first.output
    # Eight saves have a key: two albums, four posts with a link and two notes. The other
    # five are one file each, and a file of an export has no key.
    assert first.stdout.strip() == "Items: 13. New keys: 8. Joined to an older item: 0."
    assert again.stdout.strip() == "Items: 13. New keys: 0. Joined to an older item: 0."


# --- unshelve digest -----------------------------------------------------------------------


def test_digest_preview_prints_the_digest_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _import(monkeypatch, tmp_path, private_database_url, DESKTOP, "--timezone", "Europe/Helsinki")

    result = _run(
        monkeypatch,
        tmp_path,
        "digest",
        "preview",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
    )

    # The five posts of the export are from 2026-10-13. Without the worker they have no item,
    # so the digest has nothing to show.
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == cards.NOTHING_YET


def _count(database_url: str, model: type[Base]) -> int:
    async def count() -> int:
        engine = create_engine(database_url)
        try:
            async with create_session_factory(engine)() as db:
                return await db.scalar(sa.select(sa.func.count()).select_from(model)) or 0
        finally:
            await engine.dispose()

    return asyncio.run(count())


def _saved_items(database_url: str, *titles_and_days: tuple[str, int]) -> None:
    """Items of the owner 4242, each saved that many days ago, with 20 minutes to read."""

    async def save() -> None:
        engine = create_engine(database_url)
        try:
            async with create_session_factory(engine)() as db:
                owner_id = await auth_store.ensure_owner(db, 4242)
                for title, days in titles_and_days:
                    at = datetime.now(UTC) - timedelta(days=days)
                    db.add(
                        Item(
                            owner_id=owner_id,
                            title=title,
                            est_minutes=20,
                            first_captured_at=at,
                            last_captured_at=at,
                        )
                    )
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(save())


def test_digest_preview_prints_the_digest_of_now_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _saved_items(private_database_url, ("A new note", 2))

    result = _run(
        monkeypatch,
        tmp_path,
        "digest",
        "preview",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("<b>Your digest</b>\n1 thing, about 20 minutes.")
    assert "New in your inbox: saved 2 days ago." in result.stdout
    assert _count(private_database_url, Digest) == 0


def test_digest_send_sends_a_try_with_its_stale_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _saved_items(private_database_url, ("A new note", 2), ("An old essay", 60))
    telegram = PretendTelegram()

    def offline(settings: Settings, sessions: Any) -> Any:
        return dataclasses.replace(build_services(settings, sessions), bot=offline_bot(telegram))

    monkeypatch.setattr(cli, "build_services", offline)

    result = _run(
        monkeypatch,
        tmp_path,
        "digest",
        "send",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
        UNSHELVE_TELEGRAM_BOT_TOKEN="42:TEST",  # noqa: S106 - the made-up token of the tests
    )

    assert result.exit_code == 0, result.output
    # The lines before it are the log lines of the sending.
    last = result.stdout.strip().splitlines()[-1]
    assert last == "Sent a digest with 1 item and a stale prompt with 1 item."
    assert [m.text.split("\n")[0] for m in telegram.sent] == [
        "<b>Your digest</b>",
        "<b>Still in your inbox</b>",
    ]
    # A try has a row of its own: the row of the week is not touched.
    assert _count(private_database_url, Digest) == 1


def test_digest_send_needs_a_bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _run(
        monkeypatch,
        tmp_path,
        "digest",
        "send",
        UNSHELVE_DATABASE_URL=NOWHERE,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
    )

    assert result.exit_code == 1
    assert "UNSHELVE_TELEGRAM_BOT_TOKEN is not set" in result.stderr


def test_digest_needs_an_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _run(monkeypatch, tmp_path, "digest", "preview", UNSHELVE_DATABASE_URL=NOWHERE)

    assert result.exit_code == 1
    assert "UNSHELVE_OWNER_TELEGRAM_ID is not set" in result.stderr


# --- unshelve embeddings -------------------------------------------------------------------

_LONG = "\n\n".join(
    f"Part {n}. " + "Postgres keeps each job in one row, and a worker locks the row. " * 8
    for n in range(6)
)


def _two_ready_items(database_url: str) -> None:
    """A long text (three pieces) and a short note, through the whole pipeline."""

    async def save_and_work() -> None:
        engine = create_engine(database_url)
        try:
            sessions = create_session_factory(engine)
            for text in (_LONG, "A short note about vector search."):
                async with sessions() as db:
                    owner_id = await auth_store.ensure_owner(db, 1)
                    envelope = CaptureEnvelope(
                        source=CaptureSource.API,
                        chat_id=resolve.API_CHAT_ID,
                        message_id=await resolve.next_api_message_id(db),
                        posted_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
                        text=text,
                        urls=[],
                    )
                    capture_id = await resolve.store_capture(db, owner_id, envelope)
                    assert capture_id is not None
                    await resolve.resolve_capture(db, capture_id)
                    await db.commit()
            discover()
            settings = Settings(env="test", database_url=database_url)
            worker = Worker(build_services(settings, sessions), registry, WorkerConfig("w1"))
            while await worker.run_one():
                pass
        finally:
            await engine.dispose()

    asyncio.run(save_and_work())


def test_embeddings_status_prints_each_set_and_the_set_that_search_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _two_ready_items(private_database_url)

    result = _run(
        monkeypatch, tmp_path, "embeddings", "status", UNSHELVE_DATABASE_URL=private_database_url
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "Embedder: fake hashed-bow-64 64, set 1",
        "Ready items: 2",
        "Set 1: fake hashed-bow-64 64, active, 2 of 2 items, 3 pieces",
    ]


class _VoyageStandIn:
    """Vectors with the spec of Voyage, made by the fake embedder: no request leaves."""

    def __init__(self) -> None:
        self.spec = voyage_spec("voyage-4-large", 1024)
        self._inner = FakeEmbedder(1024)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        return await self._inner.embed_query(text)


def _run_the_jobs_with_voyage(database_url: str) -> None:
    async def work() -> None:
        engine = create_engine(database_url)
        try:
            sessions = create_session_factory(engine)
            settings = Settings(env="test", database_url=database_url)
            services = dataclasses.replace(
                build_services(settings, sessions), embedder=_VoyageStandIn()
            )
            discover()
            worker = Worker(services, registry, WorkerConfig("w1"))
            while await worker.run_one():
                pass
        finally:
            await engine.dispose()

    asyncio.run(work())


def test_a_new_embedder_is_filled_by_a_backfill_and_then_activated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _two_ready_items(private_database_url)
    voyage = {"UNSHELVE_DATABASE_URL": private_database_url, "UNSHELVE_EMBEDDER": "voyage"}

    def embeddings(*args: str) -> Result:
        return _run(monkeypatch, tmp_path, "embeddings", *args, **voyage)

    before = embeddings("status")
    backfill = embeddings("backfill")
    again = embeddings("backfill")
    early = embeddings("activate", "2")
    waiting = embeddings("status")
    _run_the_jobs_with_voyage(private_database_url)
    filled = embeddings("status")
    activated = embeddings("activate", "2")
    after = embeddings("status")

    assert before.stdout.splitlines() == [
        "Embedder: voyage voyage-4-large 1024, no vector yet",
        "Ready items: 2",
        "Set 1: fake hashed-bow-64 64, active, 2 of 2 items, 3 pieces",
        "Next: unshelve embeddings backfill",
    ]
    assert backfill.stdout.splitlines() == [
        "Added 2 embed jobs for set 2 (voyage voyage-4-large 1024).",
        "The worker runs them. `unshelve embeddings status` shows the progress.",
    ]
    assert again.stdout.splitlines() == [
        "Added 0 embed jobs for set 2 (voyage voyage-4-large 1024)."
    ]
    assert early.exit_code == 1
    assert "Set 2 has no vector for 2 of 2 ready items." in early.stderr
    assert waiting.stdout.splitlines()[2:] == [
        "Set 1: fake hashed-bow-64 64, active, 2 of 2 items, 3 pieces",  # still the active set
        "Set 2: voyage voyage-4-large 1024, 0 of 2 items, 0 pieces",
        "Next: unshelve embeddings backfill",
    ]
    assert filled.stdout.splitlines()[2:] == [
        "Set 1: fake hashed-bow-64 64, active, 2 of 2 items, 3 pieces",
        "Set 2: voyage voyage-4-large 1024, 2 of 2 items, 3 pieces",
        "Next: unshelve embeddings activate 2",
    ]
    assert activated.exit_code == 0, activated.output
    assert activated.stdout.strip() == "Search now reads set 2."
    assert after.stdout.splitlines()[2:] == [
        "Set 1: fake hashed-bow-64 64, 2 of 2 items, 3 pieces",
        "Set 2: voyage voyage-4-large 1024, active, 2 of 2 items, 3 pieces",
    ]


def test_embeddings_activate_with_force_does_not_wait_for_the_backfill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _two_ready_items(private_database_url)
    voyage = {"UNSHELVE_DATABASE_URL": private_database_url, "UNSHELVE_EMBEDDER": "voyage"}
    _run(monkeypatch, tmp_path, "embeddings", "backfill", **voyage)

    forced = _run(monkeypatch, tmp_path, "embeddings", "activate", "2", "--force", **voyage)

    assert forced.exit_code == 0, forced.output
    assert forced.stdout.strip() == "Search now reads set 2."


def test_embeddings_activate_refuses_a_set_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    result = _run(
        monkeypatch,
        tmp_path,
        "embeddings",
        "activate",
        "9",
        UNSHELVE_DATABASE_URL=private_database_url,
    )

    assert result.exit_code == 1
    assert "There is no embedding set 9." in result.stderr


def test_embeddings_check_prints_the_length_of_a_vector_and_the_similarities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked: list[str] = []
    closed: list[bool] = []

    async def query(self: VoyageEmbedder, text: str) -> list[float]:
        asked.append(text)
        return [1.0, 0.0, 0.0]

    async def documents(self: VoyageEmbedder, texts: list[str]) -> list[list[float]]:
        return [[0.8, 0.6, 0.0], [0.0, 0.6, 0.8]]

    async def close(self: VoyageEmbedder) -> None:
        closed.append(True)

    monkeypatch.setattr(VoyageEmbedder, "embed_query", query)
    monkeypatch.setattr(VoyageEmbedder, "embed_documents", documents)
    monkeypatch.setattr(VoyageEmbedder, "aclose", close)

    result = _run(
        monkeypatch,
        tmp_path,
        "embeddings",
        "check",
        UNSHELVE_EMBEDDER="voyage",
        VOYAGE_API_KEY="made-up",
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "voyage voyage-4-large 1024: vectors of 3 numbers",
        "A Persian question and an English text on its topic: 0.80",
        "The same question and a text on another topic: 0.00",
    ]
    assert len(asked) == 1  # the question goes as a query
    assert closed == [True]


def test_embeddings_check_says_why_there_are_no_vectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def refused(self: VoyageEmbedder, text: str) -> list[float]:
        raise ProviderError("voyage 401: Provided API key is invalid.", retryable=True)

    monkeypatch.setattr(VoyageEmbedder, "embed_query", refused)

    result = _run(monkeypatch, tmp_path, "embeddings", "check", UNSHELVE_EMBEDDER="voyage")

    assert result.exit_code == 1
    assert "No vectors: voyage 401: Provided API key is invalid." in result.stderr


# --- Scores against the goals ------------------------------------------------------------


def _ready_items(database_url: str, *texts: str) -> list[int]:
    """Saved notes of the owner 4242 that went through the pipeline, with the pretend model."""

    async def make() -> list[int]:
        engine = create_engine(database_url)
        try:
            sessions = create_session_factory(engine)
            ids: list[int] = []
            async with sessions() as db:
                owner_id = await auth_store.ensure_owner(db, 4242)
                for text in texts:
                    envelope = CaptureEnvelope(
                        source=CaptureSource.API,
                        chat_id=resolve.API_CHAT_ID,
                        message_id=await resolve.next_api_message_id(db),
                        posted_at=datetime(2026, 10, 16, tzinfo=UTC),
                        text=text,
                        urls=[],
                    )
                    capture_id = await resolve.store_capture(db, owner_id, envelope)
                    assert capture_id is not None
                    item_id = await resolve.resolve_capture(db, capture_id)
                    assert item_id is not None
                    ids.append(item_id)
                await db.commit()
            await _work(database_url, sessions)
            return ids
        finally:
            await engine.dispose()

    return asyncio.run(make())


async def _work(database_url: str, sessions: Any) -> None:
    """Run each job, with the pretend model and without the prefilter of the scores."""
    discover()
    settings = Settings(env="test", database_url=database_url, score_prefilter=-1)
    services = build_services(settings, sessions)
    worker = Worker(services, registry, WorkerConfig(worker_id="w1"))
    while await worker.run_one():
        pass


def _goal(database_url: str, title: str) -> None:
    async def add() -> None:
        engine = create_engine(database_url)
        try:
            async with create_session_factory(engine)() as db:
                owner_id = await auth_store.ensure_owner(db, 4242)
                db.add(Goal(owner_id=owner_id, slug=title.lower().replace(" ", "-"), title=title))
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(add())


def _score_now(database_url: str, item_id: int) -> None:
    """The score step for one item, with the pretend model: its answer goes to the ledger."""

    async def score() -> None:
        engine = create_engine(database_url)
        try:
            sessions = create_session_factory(engine)
            await rescore.start(sessions, 1, [item_id])
            await _work(database_url, sessions)
        finally:
            await engine.dispose()

    asyncio.run(score())


def _score_jobs(database_url: str) -> int:
    async def count() -> int:
        engine = create_engine(database_url)
        try:
            async with create_session_factory(engine)() as db:
                found = await db.scalar(
                    sa.select(sa.func.count())
                    .select_from(Job)
                    .where(Job.kind == JobKind.SCORE_ITEM, Job.state == JobState.PENDING)
                )
                return found or 0
        finally:
            await engine.dispose()

    return asyncio.run(count())


HAIKU = {"UNSHELVE_PROVIDERS": "live", "UNSHELVE_LLM_MODEL": "claude-haiku-5-5"}


def test_goals_rescore_needs_an_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _run(monkeypatch, tmp_path, "goals", "rescore")

    assert result.exit_code == 1
    assert "No owner: set UNSHELVE_OWNER_TELEGRAM_ID." in _plain(result.output)


def test_goals_rescore_without_a_goal_says_where_to_add_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    result = _run(
        monkeypatch,
        tmp_path,
        "goals",
        "rescore",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
    )

    assert result.exit_code == 0, result.output
    assert _plain(result.output) == "No active goal. Add your goals on the page Goals first.\n"


def test_goals_rescore_shows_the_price_and_starts_nothing_after_a_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    # The price of Claude Haiku 5.5. The command asks no model: the price is an estimate.
    _ready_items(private_database_url, "Leases make a job queue safe.", "A heartbeat moves it.")
    _goal(private_database_url, "Database internals")

    result = _run(
        monkeypatch,
        tmp_path,
        "goals",
        "rescore",
        answer="n\n",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
        UNSHELVE_SCORE_PREFILTER="-1",
        **HAIKU,
    )

    assert result.exit_code == 0, result.output
    lines = _plain(result.output).splitlines()
    assert lines[0] == "1 active goal: 2 items to score."
    assert lines[1] == (
        "- 2 questions to claude-haiku-5-5: about $0.0002 for about 1775 input and 120 output "
        "tokens."
    )
    assert lines[2:] == ["Score them? [y/N]: n", "Nothing was started."]
    assert _score_jobs(private_database_url) == 0


def test_goals_rescore_starts_a_score_for_each_item_after_a_yes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _ready_items(private_database_url, "Leases make a job queue safe.", "A heartbeat moves it.")
    _goal(private_database_url, "Database internals")

    result = _run(
        monkeypatch,
        tmp_path,
        "goals",
        "rescore",
        answer="y\n",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
        **HAIKU,
    )

    assert result.exit_code == 0, result.output
    assert _plain(result.output).splitlines()[-1] == "The worker scores 2 items now."
    assert _score_jobs(private_database_url) == 2


def test_goals_rescore_counts_the_items_that_the_prefilter_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _ready_items(private_database_url, "Leases make a job queue safe.", "A heartbeat moves it.")
    _goal(private_database_url, "Run a marathon")

    result = _run(
        monkeypatch,
        tmp_path,
        "goals",
        "rescore",
        answer="n\n",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
        UNSHELVE_SCORE_PREFILTER="0.9",
        **HAIKU,
    )

    assert result.exit_code == 0, result.output
    assert _plain(result.output).splitlines()[:3] == [
        "1 active goal: 2 items to score.",
        "- 2 far from each goal get 0 without a question (UNSHELVE_SCORE_PREFILTER=0.9).",
        "- 0 questions to claude-haiku-5-5: about $0.0000 for about 0 input and 0 output tokens.",
    ]


def test_goals_rescore_leaves_out_current_scores_and_finds_stored_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    scored, other = _ready_items(
        private_database_url, "Leases make a job queue safe.", "A heartbeat moves it."
    )
    _goal(private_database_url, "Database internals")
    _score_now(private_database_url, scored)  # the real step: its answer is in the ledger
    options = {
        "UNSHELVE_DATABASE_URL": private_database_url,
        "UNSHELVE_OWNER_TELEGRAM_ID": "4242",
        "UNSHELVE_SCORE_PREFILTER": "-1",
    }

    new_only = _run(monkeypatch, tmp_path, "goals", "rescore", answer="n\n", **options)
    each = _run(monkeypatch, tmp_path, "goals", "rescore", "--every-item", answer="n\n", **options)

    # The scored item has a score of the current goal: only the other item waits for one.
    assert _plain(new_only.output).splitlines()[0] == "1 active goal: 1 item to score."
    # The plan finds the question that the score step asked: both build the same question.
    lines = _plain(each.output).splitlines()
    assert lines[:2] == [
        "1 active goal: 2 items to score.",
        "- 1 have their answer in the ledger already: no cost.",
    ]
    assert lines[2].startswith("- 1 question to fake-llm: about $0.0000 for about ")
    assert other != scored


def test_goals_rescore_says_when_the_price_is_more_than_the_daily_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _ready_items(private_database_url, "Leases make a job queue safe.")
    _goal(private_database_url, "Database internals")

    result = _run(
        monkeypatch,
        tmp_path,
        "goals",
        "rescore",
        answer="n\n",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_OWNER_TELEGRAM_ID="4242",
        UNSHELVE_SCORE_PREFILTER="-1",
        UNSHELVE_LLM_DAILY_BUDGET_USD="0.00001",
        **HAIKU,
    )

    assert result.exit_code == 0, result.output
    assert (
        "The price is more than the daily budget of $0.00001: the worker asks until the budget "
        "of the day is used, and the rest waits for the next day."
    ) in _plain(result.output).replace("\n", " ")


# --- The golden set ----------------------------------------------------------------------


def _verdicts(database_url: str) -> None:
    """Three scores of the owner 4242: one confirmed in the chat, one rejected on the web, and
    one without a verdict."""

    async def store() -> None:
        engine = create_engine(database_url)
        try:
            async with create_session_factory(engine)() as db:
                owner_id = await auth_store.ensure_owner(db, 4242)
                goal = Goal(owner_id=owner_id, slug="rust", title="Rust", version=1)
                db.add(goal)
                for number, (score, label) in enumerate(
                    [(3, GoalLabel.CONFIRMED), (2, GoalLabel.REJECTED), (1, None)], start=1
                ):
                    item = Item(
                        owner_id=owner_id,
                        title=f"Item {number}",
                        enrich_state=EnrichState.READY,
                        first_captured_at=datetime(2026, 10, 17, tzinfo=UTC),
                        last_captured_at=datetime(2026, 10, 17, tzinfo=UTC),
                    )
                    db.add(item)
                    await db.flush()
                    source = LabelSource.DM if number == 1 else LabelSource.WEB
                    db.add(
                        ItemGoal(
                            item_id=item.id,
                            goal_id=goal.id,
                            owner_id=owner_id,
                            score=score,
                            reason="A reason.",
                            scorer_version="score-1:fake-llm",
                            goal_version=1,
                            label=label,
                            label_source=source if label else None,
                            labelled_at=datetime(2026, 10, 17, 12, tzinfo=UTC) if label else None,
                        )
                    )
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(store())


def test_labels_export_needs_an_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _run(monkeypatch, tmp_path, "labels", "export")

    assert result.exit_code == 1
    assert "No owner: set UNSHELVE_OWNER_TELEGRAM_ID." in _plain(result.output)


def test_labels_export_writes_one_line_for_each_verdict_and_the_summary_apart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _verdicts(private_database_url)
    options = {
        "UNSHELVE_DATABASE_URL": private_database_url,
        "UNSHELVE_OWNER_TELEGRAM_ID": "4242",
    }

    first = _run(monkeypatch, tmp_path, "labels", "export", **options)
    second = _run(monkeypatch, tmp_path, "labels", "export", **options)

    assert first.exit_code == 0, first.output
    lines = [json.loads(line) for line in first.stdout.splitlines()]
    assert [(line["label"], line["label_source"], line["relevant"]) for line in lines] == [
        ("confirmed", "dm", True),
        ("rejected", "web", False),
    ]
    # The summary is on the standard error: a redirect of the standard output gets only lines.
    assert (
        first.stderr.strip()
        == "2 verdicts: 1 confirmed, 1 rejected; 1 relevant. Sources: dm 1, web 1."
    )
    assert first.stdout == second.stdout  # the same database gives the same file


# --- unshelve search ---------------------------------------------------------------------


def test_search_retrievers_prints_what_each_retriever_finds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _saved_items(private_database_url, ("Postgres as a job queue", 1))
    env = {"UNSHELVE_DATABASE_URL": private_database_url, "UNSHELVE_OWNER_TELEGRAM_ID": "4242"}

    exact = _run(monkeypatch, tmp_path, "search", "retrievers", "postgres", **env)
    typo = _run(monkeypatch, tmp_path, "search", "retrievers", "postgress", **env)

    assert exact.exit_code == 0, exact.output
    no_vectors = "  no vectors of this embedder in the active set: unshelve embeddings status"
    assert exact.stdout.splitlines() == [
        "full text:",
        "  1. #1 Postgres as a job queue",
        "trigram:",
        "  1. #1 Postgres as a job queue",
        "vector:",
        no_vectors,
        # First in two lists: 1/61 + 1/61.
        "fused:",
        "  1. #1 0.0328 Postgres as a job queue",
    ]
    # A typo: full text finds nothing, trigram finds the title.
    assert typo.stdout.splitlines()[:4] == [
        "full text:",
        "  nothing",
        "trigram:",
        "  1. #1 Postgres as a job queue",
    ]


def test_search_retrievers_reads_the_vectors_of_the_active_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _two_ready_items(private_database_url)  # the fake pipeline: vectors in the active set

    result = _run(
        monkeypatch,
        tmp_path,
        "search",
        "retrievers",
        "vector search",
        "--limit",
        "5",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_OWNER_TELEGRAM_ID="1",
    )

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    near = lines[lines.index("vector:") + 1 : lines.index("fused:")]
    fused = lines[lines.index("fused:") + 1 :]
    # Both items have vectors; the nearest come back, also when they are not near.
    assert [line.split(".")[0] for line in near] == ["  1", "  2"]
    assert [line.split(".")[0] for line in fused] == ["  1", "  2"]


def test_search_overview_numbers_each_citation_and_lists_its_quote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _two_ready_items(private_database_url)  # the fake pipeline: cards and pieces, with vectors
    env = {"UNSHELVE_DATABASE_URL": private_database_url, "UNSHELVE_OWNER_TELEGRAM_ID": "1"}

    first = _run(monkeypatch, tmp_path, "search", "overview", "vector search", **env)
    second = _run(monkeypatch, tmp_path, "search", "overview", "vector search", **env)

    assert first.exit_code == 0, first.output
    lines = first.stdout.splitlines()
    # The fake quotes the first sentence of the first three sources: the card of the note, the
    # card of the long text, and its first piece.
    sources = [line for line in lines if re.match(r"\[\d\] #", line)]
    assert [line.split(":")[0] for line in sources] == [
        "[1] #2 a short note about vector search",
        "[2] #1 part 0 postgres keeps each job in one row and a worker",
        "[3] #1 part 0 postgres keeps each job in one row and a worker",
    ]
    assert sources[2].endswith(": 'Part 0.'")
    assert re.fullmatch(
        r"Read 2 items\. fake-llm: \d+ input and \d+ output tokens, \$0\.", lines[-1]
    )
    # The same question on the same items: the ledger answers, and the answer is the same.
    assert second.stdout.splitlines()[-1].endswith("tokens, from the ledger, no cost.")
    assert [line for line in second.stdout.splitlines() if line.startswith("[")] == sources


def test_search_overview_says_when_no_item_can_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    env = {"UNSHELVE_DATABASE_URL": private_database_url, "UNSHELVE_OWNER_TELEGRAM_ID": "1"}

    result = _run(monkeypatch, tmp_path, "search", "overview", "anything", **env)

    assert result.exit_code == 0, result.output
    assert result.stdout == "No saved item could be read for this question.\n"


def test_search_needs_an_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _run(monkeypatch, tmp_path, "search", "retrievers", "x", UNSHELVE_DATABASE_URL=NOWHERE)

    assert result.exit_code == 1
    assert "UNSHELVE_OWNER_TELEGRAM_ID is not set" in result.stderr


# --- The public feed ---------------------------------------------------------------------


def _public_world(database_url: str) -> None:
    async def seed() -> None:
        engine = create_engine(database_url)
        try:
            await seed_world(create_session_factory(engine))
        finally:
            await engine.dispose()

    asyncio.run(seed())


def test_publish_show_prints_the_feed_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _public_world(private_database_url)

    result = _run(
        monkeypatch,
        tmp_path,
        "publish",
        "show",
        UNSHELVE_DATABASE_URL=private_database_url,
        UNSHELVE_OWNER_TELEGRAM_ID=str(OWNER_TELEGRAM_ID),
        UNSHELVE_PUBLISH_TARGET="file",
    )

    assert result.exit_code == 0, result.output
    items = json.loads(result.stdout)["items"]
    assert [record["id"] for record in items] == [PERSIAN_SLUG, LEASES_SLUG]
    assert not (tmp_path / "var").exists()  # the default file of the feed was not written


def test_publish_run_writes_the_feed_and_then_sees_no_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_database_url: str
) -> None:
    _public_world(private_database_url)
    feed = tmp_path / "site" / "feed.json"
    env = {
        "UNSHELVE_DATABASE_URL": private_database_url,
        "UNSHELVE_OWNER_TELEGRAM_ID": str(OWNER_TELEGRAM_ID),
        "UNSHELVE_PUBLISH_TARGET": "file",
        "UNSHELVE_PUBLISH_FILE": str(feed),
    }

    first = _run(monkeypatch, tmp_path, "publish", "run", **env)
    second = _run(monkeypatch, tmp_path, "publish", "run", **env)

    assert first.exit_code == 0, first.output
    assert first.stdout == f"Published 2 items to {feed}.\n"
    assert json.loads(feed.read_bytes())["items"][0]["id"] == PERSIAN_SLUG
    assert second.stdout == f"No change: {feed} holds this feed already (2 items).\n"


def test_publish_run_without_a_target_says_what_to_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run(monkeypatch, tmp_path, "publish", "run", UNSHELVE_DATABASE_URL=NOWHERE)

    assert result.exit_code == 1
    assert "UNSHELVE_PUBLISH_TARGET is none" in result.stderr


def test_publish_show_needs_the_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _run(monkeypatch, tmp_path, "publish", "show", UNSHELVE_DATABASE_URL=NOWHERE)

    assert result.exit_code == 1
    assert "UNSHELVE_OWNER_TELEGRAM_ID is not set" in result.stderr
