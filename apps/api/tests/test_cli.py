import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from unshelve import __version__
from unshelve.auth import store as auth_store
from unshelve.cli import app
from unshelve.config import get_settings
from unshelve.db.engine import create_engine, create_session_factory

runner = CliRunner()


def test_version_prints_the_installed_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_every_service_command_is_registered() -> None:
    result = runner.invoke(app, ["--help"])

    for command in ("api", "worker", "bot", "migrate", "auth"):
        assert command in result.stdout


def _auth_link(monkeypatch: pytest.MonkeyPatch, empty_dir: Path, **env: str) -> Result:
    """Run ``unshelve auth link`` with these settings and no others."""
    # The command reads .env relative to the current directory. A developer's own .env must
    # not reach the test, so the command runs in a directory that has none.
    monkeypatch.chdir(empty_dir)
    for name in ("UNSHELVE_OWNER_TELEGRAM_ID", "UNSHELVE_DATABASE_URL", "UNSHELVE_WEB_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        return runner.invoke(app, ["auth", "link"])
    finally:
        get_settings.cache_clear()


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
