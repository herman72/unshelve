from typing import LiteralString

import psycopg
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from tests.conftest import TEST_SERVER_URL
from unshelve.db import migrations
from unshelve.testing.db import create_database, drop_database, unique_name

pytestmark = pytest.mark.db

OBJECTS_LEFT: LiteralString = """
    SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('public', 'pub')
      AND c.relkind IN ('r', 'v', 'S')  -- tables, views, sequences
      AND c.relname <> 'alembic_version'
"""


def _scalar(url: str, query: LiteralString) -> object:
    parsed = make_url(url)
    with psycopg.connect(
        host=parsed.host,
        port=parsed.port,
        user=parsed.username,
        password=parsed.password,
        dbname=parsed.database,
    ) as connection:
        row = connection.execute(query).fetchone()
    assert row is not None
    return row[0]


def test_upgrade_downgrade_upgrade() -> None:
    name = unique_name("unshelve_mig")
    url = create_database(TEST_SERVER_URL, name)
    try:
        migrations.upgrade(database_url=url)
        assert _scalar(url, "SELECT u_norm('ABC')") == "abc"

        migrations.downgrade("base", database_url=url)
        assert _scalar(url, OBJECTS_LEFT) == 0
        assert _scalar(url, "SELECT count(*) FROM pg_proc WHERE proname = 'u_norm'") == 0

        migrations.upgrade(database_url=url)  # a downgrade must leave nothing in the way
    finally:
        drop_database(TEST_SERVER_URL, name)


def test_the_models_match_the_migrations(database_url: str) -> None:
    # Raises when autogenerate would produce a new migration, which means a model changed
    # without a migration (or the other way round).
    command.check(migrations.alembic_config(database_url))


def test_there_is_one_migration_head() -> None:
    script = ScriptDirectory.from_config(migrations.alembic_config())
    assert len(script.get_heads()) == 1
