"""The services that the job handlers use, as the settings choose them."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.config import Settings
from unshelve.net.fetcher import LiveFetcher
from unshelve.services import build_services, close_services
from unshelve.testing.fakes import FakeFetcher

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


def test_live_model_providers_are_not_built_yet() -> None:
    with pytest.raises(NotImplementedError, match="UNSHELVE_PROVIDERS=fake"):
        build_services(Settings(env="test", providers="live"), NO_DATABASE)
