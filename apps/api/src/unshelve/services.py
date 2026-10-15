"""Build the things that job handlers use: fetcher, model provider, embedder, blob store."""

import os

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.config import Settings
from unshelve.contracts.jobs import Services
from unshelve.contracts.protocols import Fetcher
from unshelve.net.fetcher import LiveFetcher
from unshelve.providers.claude import ClaudeLLM
from unshelve.providers.ledger import CachedLLM
from unshelve.testing.fakes import FakeEmbedder, FakeFetcher, FakeLLM, InMemoryBlobStore

log = structlog.get_logger()


def build_services(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> Services:
    """The real fetcher with ``UNSHELVE_FETCHER=live``, and Claude with
    ``UNSHELVE_PROVIDERS=live``. The embedder stays fake until milestone M2-5."""
    fetcher: Fetcher = (
        LiveFetcher.from_settings(settings) if settings.fetcher == "live" else FakeFetcher()
    )
    return Services(
        settings=settings,
        sessions=sessions,
        fetcher=fetcher,
        llm=build_model(settings, sessions),
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


def build_model(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> CachedLLM:
    """The model, behind its cache and cost ledger (``llm_calls``)."""
    if settings.providers == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            # The client starts without a key; each call then fails with a 401, and waits.
            log.warning("llm.no_key", hint="put ANTHROPIC_API_KEY into .env")
        claude = ClaudeLLM(model=settings.llm_model, effort=settings.llm_effort)
        return CachedLLM(
            claude,
            sessions,
            model=settings.llm_model,
            budget_usd=settings.llm_daily_budget_usd,
            timezone=settings.timezone,
        )
    return CachedLLM(FakeLLM(), sessions, model=FakeLLM.model)


async def close_services(services: Services) -> None:
    """Close the connections that ``build_services`` opened."""
    if isinstance(services.fetcher, LiveFetcher):
        await services.fetcher.aclose()
    if isinstance(services.llm, CachedLLM):
        await services.llm.aclose()
    if services.bot is not None:
        await services.bot.session.close()
