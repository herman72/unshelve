"""Build the things that job handlers use: fetcher, model provider, embedder, blob store."""

import os

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.blobs import FileBlobStore
from unshelve.config import Settings
from unshelve.contracts.dto import EmbeddingSpec
from unshelve.contracts.jobs import Services
from unshelve.contracts.protocols import Embedder, Fetcher
from unshelve.net.fetcher import LiveFetcher
from unshelve.providers.claude import ClaudeLLM
from unshelve.providers.ledger import CachedLLM
from unshelve.providers.voyage import VoyageEmbedder, voyage_spec
from unshelve.testing.fakes import FakeEmbedder, FakeFetcher, FakeLLM

log = structlog.get_logger()


def build_services(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> Services:
    """The real fetcher with ``UNSHELVE_FETCHER=live``, Claude with ``UNSHELVE_PROVIDERS=live``,
    and Voyage with ``UNSHELVE_EMBEDDER=voyage``."""
    fetcher: Fetcher = (
        LiveFetcher.from_settings(settings) if settings.fetcher == "live" else FakeFetcher()
    )
    return Services(
        settings=settings,
        sessions=sessions,
        fetcher=fetcher,
        llm=build_model(settings, sessions),
        embedder=build_embedder(settings),
        # The files that you send stay on disk, in UNSHELVE_BLOB_DIR (a volume in the stack).
        blobs=FileBlobStore(settings.blob_dir),
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


def build_embedder(settings: Settings) -> Embedder:
    """The embedder. Its spec names the embedding set of the vectors that it makes."""
    if settings.embedder == "voyage":
        key = settings.voyage_api_key.get_secret_value() if settings.voyage_api_key else ""
        if not key:
            # Each request then fails with a 401, and the step waits for the key.
            log.warning("embed.no_key", hint="put VOYAGE_API_KEY into .env")
        return VoyageEmbedder(model=settings.embed_model, dim=settings.embed_dim, api_key=key)
    return FakeEmbedder()


def embedder_spec(settings: Settings) -> EmbeddingSpec:
    """The embedding set of the vectors that ``build_embedder`` makes, without a client."""
    if settings.embedder == "voyage":
        return voyage_spec(settings.embed_model, settings.embed_dim)
    return FakeEmbedder().spec


async def close_services(services: Services) -> None:
    """Close the connections that ``build_services`` opened."""
    if isinstance(services.fetcher, LiveFetcher):
        await services.fetcher.aclose()
    if isinstance(services.llm, CachedLLM):
        await services.llm.aclose()
    if isinstance(services.embedder, VoyageEmbedder):
        await services.embedder.aclose()
    if services.bot is not None:
        await services.bot.session.close()
