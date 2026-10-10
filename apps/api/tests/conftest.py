from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
import pytest

from unshelve.api.app import create_app
from unshelve.config import Settings

ClientFactory = Callable[[Settings], AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
def settings() -> Settings:
    """Settings for tests. Environment variables still apply (CI sets the database URL)."""
    return Settings(env="test")


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
