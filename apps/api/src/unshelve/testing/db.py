"""Throwaway databases for tests.

A migrated template database is built once per test session. Tests get clones of it:
``CREATE DATABASE … TEMPLATE …`` copies files, so it takes milliseconds. Every name has a
random suffix, so several checkouts can run their tests against one Postgres at the same time.
"""

import uuid

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

# The database of a test that did not ask for one: nothing listens on port 1. The default of the
# settings is the database of the instance on this computer, and the tests share its server.
# So a test that forgets its own database fails at once, and writes nowhere.
NO_DATABASE_URL = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none?connect_timeout=1"


def unique_name(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _admin(server_url: str) -> psycopg.Connection:
    url = make_url(server_url)
    return psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname=url.database or "postgres",
        autocommit=True,  # CREATE DATABASE cannot run inside a transaction
    )


def create_database(server_url: str, name: str, *, template: str | None = None) -> str:
    """Create a database and return its SQLAlchemy URL. Nobody may be connected to ``template``."""
    statement = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
    if template is not None:
        statement += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template))
    with _admin(server_url) as connection:
        connection.execute(statement)
    return make_url(server_url).set(database=name).render_as_string(hide_password=False)


def drop_database(server_url: str, name: str) -> None:
    statement = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    with _admin(server_url) as connection:
        connection.execute(statement)
