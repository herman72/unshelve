from collections.abc import AsyncGenerator

import httpx
import pytest

from tests.api.support import OFFLINE
from tests.conftest import ClientFactory


@pytest.fixture
async def client(client_for: ClientFactory) -> AsyncGenerator[httpx.AsyncClient]:
    """A client for the whole application, without a database."""
    async with client_for(OFFLINE) as client:
        yield client
