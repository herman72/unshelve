import time

import pytest

from tests.conftest import ClientFactory
from unshelve import __version__
from unshelve.config import Settings

# Nothing listens on port 1, so the connection is refused immediately.
UNREACHABLE_DB = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none?connect_timeout=1"
# Non-routable: packets are dropped, so only connect_timeout ends the wait (libpq rounds 1 up to 2).
BLACKHOLE_DB = "postgresql+psycopg://nobody:nothing@10.255.255.1:5432/none?connect_timeout=1"


async def test_healthz_does_not_need_the_database(client_for: ClientFactory) -> None:
    async with client_for(Settings(env="test", database_url=UNREACHABLE_DB)) as client:
        response = await client.get("/api/v1/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


async def test_readyz_reports_an_unreachable_database(client_for: ClientFactory) -> None:
    async with client_for(Settings(env="test", database_url=UNREACHABLE_DB)) as client:
        response = await client.get("/api/v1/readyz")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Service Unavailable",
        "status": 503,
        "detail": "database is not reachable",
    }


@pytest.mark.db
async def test_readyz_is_ok_when_the_database_answers(
    client_for: ClientFactory, settings: Settings
) -> None:
    async with client_for(settings) as client:
        response = await client.get("/api/v1/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.slow
async def test_readyz_gives_up_when_the_database_does_not_answer(
    client_for: ClientFactory,
) -> None:
    async with client_for(Settings(env="test", database_url=BLACKHOLE_DB)) as client:
        start = time.monotonic()
        response = await client.get("/api/v1/readyz")
        elapsed = time.monotonic() - start

    assert response.status_code == 503
    assert elapsed < 5  # connect_timeout bounds the wait
