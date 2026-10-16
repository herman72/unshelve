import os
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
import pytest
from hypothesis import settings as property_test_settings
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from unshelve.api.app import create_app
from unshelve.config import Settings
from unshelve.db import migrations
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.testing.db import NO_DATABASE_URL, create_database, drop_database, unique_name

# Any database on the server; test databases are created next to it. `make db` starts it.
TEST_SERVER_URL = os.environ.get(
    "UNSHELVE_TEST_SERVER_URL", "postgresql+psycopg://unshelve:unshelve@localhost:54329/postgres"
)

ClientFactory = Callable[[Settings], AbstractAsyncContextManager[httpx.AsyncClient]]

# A test never reads a .env file: the settings of a test are in the test. Without this line,
# the .env of a developer (a live fetcher, a real key) changes results, or reaches a service.
Settings.model_config["env_file"] = None


@pytest.fixture(scope="session", autouse=True)
def _no_database() -> Generator[None]:
    """Settings without a database of their own get one that does not exist, never the database
    of the instance that the default names. A test gives its own: ``private_database_url``."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("UNSHELVE_DATABASE_URL", NO_DATABASE_URL)
        yield


@pytest.fixture(scope="session", autouse=True)
def _blob_dir(tmp_path_factory: pytest.TempPathFactory) -> Generator[None]:
    """The blob store of the tests is a folder of their own, not ``var/blobs`` of the checkout."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("UNSHELVE_BLOB_DIR", str(tmp_path_factory.mktemp("blobs")))
        yield


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """A test marked ``live`` talks to a real service with a real key. It runs only when it
    is asked for: ``uv run pytest -m live -o addopts=""``. CI never asks."""
    if "live" in str(config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="a live test: run it with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


# A property test makes its own inputs. By default it makes the same inputs in each run and
# on each computer, so a failure on CI can be repeated here. To let it look for new problems,
# with new inputs each time: HYPOTHESIS_PROFILE=explore uv run pytest tests/ingest
property_test_settings.register_profile(
    "repeatable", derandomize=True, database=None, max_examples=500, deadline=None
)
property_test_settings.register_profile("explore", max_examples=50_000, deadline=None)
property_test_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "repeatable"))


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
def private_database_url(template_db: str) -> Generator[str]:
    """The URL of a database that one test owns, for work that is really committed."""
    name = unique_name("unshelve_c")
    try:
        yield create_database(TEST_SERVER_URL, name, template=template_db)
    finally:
        drop_database(TEST_SERVER_URL, name)


@pytest.fixture
async def committed_db(
    private_database_url: str,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """Sessions on the test's private database, for real commits on several connections."""
    engine = create_engine(private_database_url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


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
