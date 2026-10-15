"""Build the things that job handlers use: fetcher, model provider, embedder, blob store."""

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.config import Settings
from unshelve.contracts.jobs import Services
from unshelve.contracts.protocols import Fetcher
from unshelve.net.fetcher import LiveFetcher
from unshelve.testing.fakes import FakeEmbedder, FakeFetcher, FakeLLM, InMemoryBlobStore


def build_services(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> Services:
    """The fake model providers for now: no keys, the same answer every time. The fetcher is
    the real one with ``UNSHELVE_FETCHER=live``."""
    if settings.providers != "fake":
        msg = "live model providers arrive later in milestone M2; use UNSHELVE_PROVIDERS=fake"
        raise NotImplementedError(msg)
    fetcher: Fetcher = (
        LiveFetcher.from_settings(settings) if settings.fetcher == "live" else FakeFetcher()
    )
    return Services(
        settings=settings,
        sessions=sessions,
        fetcher=fetcher,
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


async def close_services(services: Services) -> None:
    """Close the connections that ``build_services`` opened."""
    if isinstance(services.fetcher, LiveFetcher):
        await services.fetcher.aclose()
    if services.bot is not None:
        await services.bot.session.close()
