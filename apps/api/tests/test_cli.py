import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner, Result

from tests.ingest.support import DESKTOP, MACOS, MADE_UP
from unshelve import __version__
from unshelve.auth import store as auth_store
from unshelve.bot import cards
from unshelve.cli import app
from unshelve.config import Settings, get_settings
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.db.models import Capture, ItemKey, Job
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services

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

    for command in ("api", "worker", "bot", "migrate", "auth", "import", "rekey", "digest"):
        assert command in _plain(result.stdout)


def _run(monkeypatch: pytest.MonkeyPatch, empty_dir: Path, *args: str, **env: str) -> Result:
    """Run a command with these settings and no others."""
    # The command reads .env relative to the current directory. A developer's own .env must
    # not reach the test, so the command runs in a directory that has none.
    monkeypatch.chdir(empty_dir)
    for name in (
        "UNSHELVE_OWNER_TELEGRAM_ID",
        "UNSHELVE_DATABASE_URL",
        "UNSHELVE_WEB_BASE_URL",
        "UNSHELVE_CAPTURE_CHAT_IDS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        return runner.invoke(app, list(args))
    finally:
        get_settings.cache_clear()


def _auth_link(monkeypatch: pytest.MonkeyPatch, empty_dir: Path, **env: str) -> Result:
    return _run(monkeypatch, empty_dir, "auth", "link", **env)


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

    assert again.stdout.splitlines()[-1] == "Stored 0 new posts. 5 were stored already."
    assert other_app.stdout.splitlines()[-1] == "Stored 0 new posts. 5 were stored already."
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
    assert one.stdout.splitlines()[-1] == "Stored 1 new post. 0 were stored already."
    assert two.stdout.splitlines()[-1] == (
        "Dry run: 1 post is new, 1 is stored already. Nothing was changed."
    )
    assert again.stdout.splitlines()[-1] == "Stored 0 new posts. 1 was stored already."


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
