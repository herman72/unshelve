"""The services that the job handlers use, as the settings choose them."""

from decimal import Decimal

import pytest
import structlog
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from unshelve import services as services_module
from unshelve.config import Settings
from unshelve.net.fetcher import LiveFetcher
from unshelve.providers.claude import ClaudeLLM
from unshelve.providers.ledger import CachedLLM
from unshelve.providers.voyage import VoyageEmbedder
from unshelve.services import build_services, close_services
from unshelve.testing.fakes import FakeEmbedder, FakeFetcher

# The services do not open the database when they are built, so no database is needed here.
NO_DATABASE = async_sessionmaker[AsyncSession]()


async def test_the_default_fetcher_serves_recorded_pages_only() -> None:
    services = build_services(Settings(env="test"), NO_DATABASE)

    assert isinstance(services.fetcher, FakeFetcher)


async def test_the_live_fetcher_is_built_from_the_settings_and_closed_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[LiveFetcher] = []

    async def record(self: LiveFetcher) -> None:
        closed.append(self)

    monkeypatch.setattr(LiveFetcher, "aclose", record)
    services = build_services(Settings(env="test", fetcher="live"), NO_DATABASE)

    await close_services(services)

    assert isinstance(services.fetcher, LiveFetcher)
    assert closed == [services.fetcher]


def test_the_live_model_is_claude_behind_its_cache_with_the_settings() -> None:
    settings = Settings(
        env="test",
        providers="live",
        llm_model="claude-haiku-5-5",
        llm_effort="medium",
        llm_daily_budget_usd=Decimal("0.50"),
    )

    services = build_services(settings, NO_DATABASE)

    assert isinstance(services.llm, CachedLLM)
    assert isinstance(services.llm.inner, ClaudeLLM)
    assert services.llm.inner.model == "claude-haiku-5-5"
    assert services.llm.budget_usd == Decimal("0.50")
    assert services.embedder.spec.provider == "fake"  # the embedder has its own switch


async def test_the_model_is_closed_at_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    async def record(self: ClaudeLLM) -> None:
        closed.append(self.model)

    monkeypatch.setattr(ClaudeLLM, "aclose", record)
    services = build_services(Settings(env="test", providers="live"), NO_DATABASE)

    await close_services(services)

    assert closed == ["claude-opus-5-5"]


def test_the_default_embedder_is_the_fake_one() -> None:
    services = build_services(Settings(env="test"), NO_DATABASE)

    assert isinstance(services.embedder, FakeEmbedder)


async def test_voyage_is_built_from_the_settings_and_closed_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    async def record(self: VoyageEmbedder) -> None:
        closed.append(self.spec.model)

    monkeypatch.setattr(VoyageEmbedder, "aclose", record)
    settings = Settings(
        env="test",
        embedder="voyage",
        embed_model="voyage-4-lite",
        embed_dim=512,
        # The key has its usual name, without the prefix: the field reads it by its alias.
        VOYAGE_API_KEY=SecretStr("made-up"),  # pyright: ignore[reportCallIssue]
    )

    services = build_services(settings, NO_DATABASE)
    await close_services(services)

    assert isinstance(services.embedder, VoyageEmbedder)
    assert (services.embedder.spec.provider, services.embedder.spec.dim) == ("voyage", 512)
    assert closed == ["voyage-4-lite"]


def test_voyage_without_a_key_says_so_in_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    # A new logger: one that an earlier test configured and cached would not be captured.
    monkeypatch.setattr(services_module, "log", structlog.get_logger())
    with capture_logs() as logs:
        services = build_services(Settings(env="test", embedder="voyage"), NO_DATABASE)

    assert isinstance(services.embedder, VoyageEmbedder)
    assert [entry["event"] for entry in logs] == ["embed.no_key"]
