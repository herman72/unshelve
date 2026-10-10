import os
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from unshelve.api.app import create_app
from unshelve.config import Settings
from unshelve.db import migrations
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.testing.db import create_database, drop_database, unique_name

# Any database on the server; test databases are created next to it. `make db` starts it.
TEST_SERVER_URL = os.environ.get(
    "UNSHELVE_TEST_SERVER_URL", "postgresql+psycopg://unshelve:unshelve@localhost:54329/postgres"
)

ClientFactory = Callable[[Settings], AbstractAsyncContextManager[httpx.AsyncClient]]


# --- Databases ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def template_db() -> Generator[str]:
    """The name of a migrated database. Tests clone it and never connect to it."""
    name = unique_name("unshelve_tpl")
    url = create_database(TEST_SERVER_URL, name)
    try:
        migrations.upgrade(database_url=url)
        yield name
    finally:
        drop_database(TEST_SERVER_URL, name)


@pytest.fixture(scope="session")
def database_url(template_db: str) -> Generator[str]:
    name = unique_name("unshelve_test")
    try:
        yield create_database(TEST_SERVER_URL, name, template=template_db)
    finally:
        drop_database(TEST_SERVER_URL, name)


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    """A session whose work is rolled back after the test, including its commits."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
async def committed_db(template_db: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """A private database for tests that need real commits on several connections."""
    name = unique_name("unshelve_c")
    engine = create_engine(create_database(TEST_SERVER_URL, name, template=template_db))
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()
        drop_database(TEST_SERVER_URL, name)


# --- Application -------------------------------------------------------------------------


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(env="test", database_url=database_url)


@pytest.fixture
def client_for() -> ClientFactory:
    """Build an HTTP client bound to an app with the given settings, with lifespan events."""

    @asynccontextmanager
    async def factory(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            yield client

    return factory
