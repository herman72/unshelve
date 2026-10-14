"""Build the things that job handlers use: fetcher, model provider, embedder, blob store."""

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.config import Settings
from unshelve.contracts.jobs import Services
from unshelve.testing.fakes import FakeEmbedder, FakeFetcher, FakeLLM, InMemoryBlobStore


def build_services(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> Services:
    """The fake providers for now: no network, no keys, the same answer every time."""
    if settings.providers != "fake" or settings.fetcher != "fixtures":
        msg = "live providers and live fetching arrive in milestone M2; use the defaults"
        raise NotImplementedError(msg)
    return Services(
        settings=settings,
        sessions=sessions,
        fetcher=FakeFetcher(),
        llm=FakeLLM(),
        embedder=FakeEmbedder(),
        blobs=InMemoryBlobStore(),
        # The bot sends messages from a job: the weekly digest. It reads no updates here, so
        # it does not compete with the bot process for them.
        bot=(
            Bot(settings.telegram_bot_token.get_secret_value())
            if settings.telegram_bot_token is not None
            else None
        ),
    )
