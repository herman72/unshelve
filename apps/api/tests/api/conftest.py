from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.support import OFFLINE, ClientWith
from tests.conftest import ClientFactory
from tests.search.corpus import Corpus, seed
from unshelve.api.app import create_app
from unshelve.api.deps import SESSION_COOKIE
from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.protocols import CitingLLM, Embedder

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def client(client_for: ClientFactory) -> AsyncGenerator[httpx.AsyncClient]:
    """A client for the whole application, without a database."""
    async with client_for(OFFLINE) as client:
        yield client


@pytest.fixture
async def corpus(committed_db: Sessions) -> Corpus:
    """The made-up corpus of tests/search, committed in the test's own database."""
    async with committed_db() as db:
        corpus = await seed(db)
        await db.commit()
    return corpus


@pytest.fixture
def client_with(private_database_url: str, committed_db: Sessions, corpus: Corpus) -> ClientWith:
    @asynccontextmanager
    async def factory(
        embedder: Embedder, llm: CitingLLM | None = None
    ) -> AsyncGenerator[httpx.AsyncClient]:
        async with committed_db() as db:
            secret = await auth.open_session(db, corpus.owner_id)
            await db.commit()
        settings = Settings(env="test", database_url=private_database_url)
        app = create_app(settings, embedder=embedder, llm=llm)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            client.cookies.set(SESSION_COOKIE, secret)
            yield client

    return factory
