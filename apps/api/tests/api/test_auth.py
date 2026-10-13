"""Sign-in over HTTP: the login link, the session cookie, sign-out.

These tests talk to the application the way the web app does. The application commits its
work, so each test has a database of its own.
"""

from collections.abc import AsyncGenerator
from datetime import timedelta
from http.cookies import SimpleCookie

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.support import problem_body
from tests.conftest import ClientFactory
from unshelve.api.deps import SESSION_COOKIE
from unshelve.auth import store
from unshelve.config import Settings
from unshelve.db.models import AuthToken, WebSession

TELEGRAM_ID = 4242
CONSUME = "/api/v1/auth/magic/consume"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/me"


@pytest.fixture
async def client(
    client_for: ClientFactory, private_database_url: str
) -> AsyncGenerator[httpx.AsyncClient]:
    async with client_for(Settings(env="test", database_url=private_database_url)) as client:
        yield client


async def _login_token(
    committed_db: async_sessionmaker[AsyncSession], ttl: timedelta = store.LOGIN_TOKEN_TTL
) -> str:
    """What ``unshelve auth link`` does: make sure the owner exists, and issue a token."""
    async with committed_db() as db:
        owner_id = await store.ensure_owner(db, TELEGRAM_ID)
        token = await store.issue_login_token(db, owner_id, ttl=ttl)
        await db.commit()
    return token


def _session_cookie(response: httpx.Response) -> SimpleCookie:
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie


async def _sign_in(
    client: httpx.AsyncClient, committed_db: async_sessionmaker[AsyncSession]
) -> httpx.Response:
    response = await client.post(CONSUME, json={"token": await _login_token(committed_db)})
    assert response.status_code == 204
    return response


# --- Signing in --------------------------------------------------------------------------


async def test_a_login_link_signs_the_owner_in(
    client: httpx.AsyncClient, committed_db: async_sessionmaker[AsyncSession]
) -> None:
    await _sign_in(client, committed_db)

    me = await client.get(ME)

    assert me.status_code == 200
    assert me.json() == {
        "id": 1,
        "telegram_user_id": TELEGRAM_ID,
        "display_name": None,
        "timezone": "UTC",
        "queue_limit": 5,
    }


async def test_the_session_cookie_is_hidden_from_scripts_and_from_other_sites(
    client: httpx.AsyncClient, committed_db: async_sessionmaker[AsyncSession]
) -> None:
    response = await _sign_in(client, committed_db)

    cookie = _session_cookie(response)[SESSION_COOKIE]

    assert cookie["httponly"] is True
    assert cookie["samesite"] == "lax"
    assert cookie["path"] == "/"
    assert cookie["max-age"] == str(30 * 24 * 60 * 60)
    assert cookie["secure"] == ""  # the test settings serve the web app over plain http
    assert len(cookie.value) >= 43


async def test_the_session_cookie_is_https_only_when_the_web_app_is(
    client_for: ClientFactory,
    private_database_url: str,
    committed_db: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        env="test", database_url=private_database_url, web_base_url="https://unshelve.example"
    )
    async with client_for(settings) as client:
        response = await _sign_in(client, committed_db)

    assert _session_cookie(response)[SESSION_COOKIE]["secure"] is True


async def test_a_login_link_works_once(
    client: httpx.AsyncClient, committed_db: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login_token(committed_db)
    assert (await client.post(CONSUME, json={"token": token})).status_code == 204

    replay = await client.post(CONSUME, json={"token": token})

    assert problem_body(replay, 401)["detail"] == "this link is not valid or has expired"
    assert "set-cookie" not in replay.headers


async def test_an_expired_and_an_unknown_link_get_the_same_answer(
    client: httpx.AsyncClient, committed_db: async_sessionmaker[AsyncSession]
) -> None:
    expired = await _login_token(committed_db, ttl=timedelta(seconds=-1))

    answers = [
        await client.post(CONSUME, json={"token": expired}),
        await client.post(CONSUME, json={"token": store.new_secret()}),
    ]

    bodies = [problem_body(answer, 401) for answer in answers]
    assert bodies[0] == bodies[1]
    assert all("set-cookie" not in answer.headers for answer in answers)


async def test_a_token_that_is_too_short_is_a_validation_error(client: httpx.AsyncClient) -> None:
    response = await client.post(CONSUME, json={"token": "short"})

    problem_body(response, 422)


async def test_signing_in_uses_up_the_token_and_stores_no_secret(
    client: httpx.AsyncClient, committed_db: async_sessionmaker[AsyncSession]
) -> None:
    response = await _sign_in(client, committed_db)
    secret = _session_cookie(response)[SESSION_COOKIE].value

    async with committed_db() as db:
        used_at = await db.scalar(sa.select(AuthToken.used_at))
        session_hash = await db.scalar(sa.select(WebSession.session_hash))

    assert used_at is not None
    assert session_hash == store.digest(secret)
    assert session_hash != secret


async def test_each_sign_in_gets_a_new_session(
    client: httpx.AsyncClient, committed_db: async_sessionmaker[AsyncSession]
) -> None:
    first = _session_cookie(await _sign_in(client, committed_db))[SESSION_COOKIE].value
    second = _session_cookie(await _sign_in(client, committed_db))[SESSION_COOKIE].value

    assert first != second


# --- Using and ending a session ----------------------------------------------------------


async def test_a_made_up_cookie_is_not_a_session(client: httpx.AsyncClient) -> None:
    response = await client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}={store.new_secret()}"})

    assert problem_body(response, 401)["detail"] == "sign in first"


async def test_signing_out_ends_the_session_on_the_server(
    client: httpx.AsyncClient, committed_db: async_sessionmaker[AsyncSession]
) -> None:
    secret = _session_cookie(await _sign_in(client, committed_db))[SESSION_COOKIE].value

    response = await client.post(LOGOUT)

    assert response.status_code == 204
    cleared = _session_cookie(response)[SESSION_COOKIE]
    assert cleared.value == ""
    assert cleared["max-age"] == "0"
    # A copy of the old cookie is worth nothing: the session is closed in the database.
    client.cookies.clear()
    again = await client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}={secret}"})
    problem_body(again, 401)


async def test_signing_out_needs_a_session(client: httpx.AsyncClient) -> None:
    response = await client.post(LOGOUT)

    problem_body(response, 401)


async def test_an_expired_session_is_signed_out(
    client: httpx.AsyncClient, committed_db: async_sessionmaker[AsyncSession]
) -> None:
    async with committed_db() as db:
        owner_id = await store.ensure_owner(db, TELEGRAM_ID)
        secret = await store.open_session(db, owner_id, ttl=timedelta(seconds=-1))
        await db.commit()

    response = await client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}={secret}"})

    problem_body(response, 401)


async def test_a_signed_in_request_fails_safe_when_the_database_is_down(
    client_for: ClientFactory,
) -> None:
    nowhere = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none?connect_timeout=1"
    async with client_for(Settings(env="test", database_url=nowhere)) as client:
        response = await client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}=anything"})

    # Not 200, and not 401 either: the session may be fine, the database cannot say.
    assert problem_body(response, 503)["detail"] == "the database is not available; try again"
