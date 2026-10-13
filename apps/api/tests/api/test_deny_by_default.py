"""Deny by default: every route needs a session unless it is on the short public list.

The routes come from the application's own route table, so a route added later is covered
without anyone remembering to add a test for it.
"""

import re
from collections.abc import AsyncGenerator

import httpx
import pytest

from tests.api.support import PUBLIC_OPERATIONS, api_operations, build_app, problem_body
from tests.conftest import TEST_SERVER_URL, ClientFactory
from unshelve.api.deps import SESSION_COOKIE
from unshelve.auth import store
from unshelve.config import Settings
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.testing.db import create_database, drop_database, unique_name

OPERATIONS = [op for op in api_operations(build_app()) if op.operation_id not in PUBLIC_OPERATIONS]
PROTECTED = [pytest.param(op.method, op.path, id=f"{op.method} {op.path}") for op in OPERATIONS]
# The same routes without sign-out: it would end the one session that the last test below
# uses for every route. Sign-out has its own tests in test_auth.py.
KEEP_THE_SESSION = [
    pytest.param(op.method, op.path, id=f"{op.method} {op.path}")
    for op in OPERATIONS
    if op.operation_id != "logout"
]


def with_ids(path: str) -> str:
    """Fill every path parameter with 1, which is a valid id."""
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.fixture(scope="module")
async def signed_in(template_db: str) -> AsyncGenerator[tuple[Settings, str]]:
    """Settings for a database with one live session, and the secret of that session.

    One database serves every test of this module: they only read the session.
    """
    name = unique_name("unshelve_door")
    url = create_database(TEST_SERVER_URL, name, template=template_db)
    engine = create_engine(url)
    try:
        async with create_session_factory(engine)() as db:
            owner_id = await store.ensure_owner(db, telegram_user_id=4242)
            secret = await store.open_session(db, owner_id)
            await db.commit()
        yield Settings(env="test", database_url=url), secret
    finally:
        await engine.dispose()
        drop_database(TEST_SERVER_URL, name)


def test_the_tests_below_cover_the_route_table() -> None:
    # Guards for the two lists above. A public list with a stale name, or an empty list of
    # protected routes, would let the parametrised tests pass without checking anything.
    operation_ids = {op.operation_id for op in api_operations(build_app())}

    assert PUBLIC_OPERATIONS.issubset(operation_ids)
    assert PROTECTED
    assert len(KEEP_THE_SESSION) == len(PROTECTED) - 1


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_a_protected_route_answers_401_without_a_session(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    # This client has no database. So the test also shows that an anonymous request is
    # turned away before the database is asked anything.
    response = await client.request(method, with_ids(path))

    problem_body(response, 401)


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_a_protected_route_answers_401_for_a_cookie_that_is_no_session(
    client_for: ClientFactory, signed_in: tuple[Settings, str], method: str, path: str
) -> None:
    settings, _ = signed_in
    async with client_for(settings) as client:
        response = await client.request(
            method, with_ids(path), headers={"Cookie": f"{SESSION_COOKIE}={store.new_secret()}"}
        )

    problem_body(response, 401)


@pytest.mark.parametrize(("method", "path"), KEEP_THE_SESSION)
async def test_a_protected_route_lets_a_live_session_in(
    client_for: ClientFactory, signed_in: tuple[Settings, str], method: str, path: str
) -> None:
    settings, secret = signed_in
    async with client_for(settings) as client:
        response = await client.request(
            method, with_ids(path), headers={"Cookie": f"{SESSION_COOKIE}={secret}"}
        )

    # What comes after the door is the route's own business: an answer, a validation error,
    # or 501 while the route is a stub. It is never "sign in first".
    assert response.status_code != 401
