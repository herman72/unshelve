"""Alembic environment.

Migrations run synchronously on the same psycopg driver that the application uses
asynchronously. The URL comes from the settings unless the caller sets ``sqlalchemy.url``
(the tests do, to point at a throwaway database).
"""

from alembic import context
from sqlalchemy import create_engine, pool

import unshelve.db.models  # noqa: F401  # pyright: ignore[reportUnusedImport] - registers the tables
from unshelve.config import get_settings
from unshelve.db.base import Base

target_metadata = Base.metadata


def _database_url() -> str:
    return context.config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
