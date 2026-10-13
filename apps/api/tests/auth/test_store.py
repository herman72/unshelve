"""The sign-in store: login tokens and sessions.

Each test name is one rule. The clock is passed in, so no test waits for real time.
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store
from unshelve.auth.store import SessionUser
from unshelve.db.models import AuthToken, User, WebSession

NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
TELEGRAM_ID = 4242


async def _owner(db: AsyncSession) -> int:
    return await store.ensure_owner(db, TELEGRAM_ID)


# --- Secrets -----------------------------------------------------------------------------


def test_a_secret_is_long_random_and_safe_in_a_url() -> None:
    first, second = store.new_secret(), store.new_secret()

    assert first != second
    assert len(first) >= 43  # 32 bytes in base64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first)


def test_the_digest_of_a_secret_is_not_the_secret() -> None:
    secret = store.new_secret()

    assert store.digest(secret) != secret
    assert store.digest(secret) == store.digest(secret)
    assert re.fullmatch(r"[0-9a-f]{64}", store.digest(secret))


# --- The owner ---------------------------------------------------------------------------


async def test_the_owner_is_created_once(db: AsyncSession) -> None:
    first = await store.ensure_owner(db, TELEGRAM_ID)
    second = await store.ensure_owner(db, TELEGRAM_ID)

    assert first == second
    assert await db.scalar(sa.select(sa.func.count()).select_from(User)) == 1


# --- Login tokens ------------------------------------------------------------------------


async def test_a_login_token_works_once(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    token = await store.issue_login_token(db, owner_id)

    assert await store.consume_login_token(db, token) == owner_id
    assert await store.consume_login_token(db, token) is None


async def test_a_login_token_stops_working_after_five_minutes(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    in_time = await store.issue_login_token(db, owner_id, now=NOON)
    too_late = await store.issue_login_token(db, owner_id, now=NOON)

    almost = NOON + timedelta(minutes=4, seconds=59)
    assert await store.consume_login_token(db, in_time, now=almost) == owner_id
    assert await store.consume_login_token(db, too_late, now=NOON + timedelta(minutes=5)) is None


async def test_a_wrong_token_does_not_work(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    await store.issue_login_token(db, owner_id)

    assert await store.consume_login_token(db, store.new_secret()) is None


async def test_the_database_never_holds_the_login_token(db: AsyncSession) -> None:
    token = await store.issue_login_token(db, await _owner(db))

    row = await db.scalar(sa.text("SELECT row_to_json(t)::text FROM auth_tokens t"))

    assert isinstance(row, str)
    assert token not in row
    assert await db.scalar(sa.select(AuthToken.token_hash)) == store.digest(token)


async def test_two_requests_with_one_token_give_one_sign_in(
    committed_db: async_sessionmaker[AsyncSession],
) -> None:
    async with committed_db() as setup:
        owner_id = await _owner(setup)
        token = await store.issue_login_token(setup, owner_id)
        await setup.commit()

    async with committed_db() as first, committed_db() as second:
        # The first request has used the token and has not committed yet.
        winner = await store.consume_login_token(first, token)
        # The second request must wait for it: both want the same row.
        waiting = asyncio.create_task(store.consume_login_token(second, token))
        await asyncio.sleep(0.2)
        assert not waiting.done()

        await first.commit()
        loser = await waiting
        await second.commit()

    assert winner == owner_id
    assert loser is None


async def test_a_request_that_fails_does_not_use_up_the_token(
    committed_db: async_sessionmaker[AsyncSession],
) -> None:
    async with committed_db() as setup:
        owner_id = await _owner(setup)
        token = await store.issue_login_token(setup, owner_id)
        await setup.commit()

    async with committed_db() as failing:
        assert await store.consume_login_token(failing, token) == owner_id
        await failing.rollback()  # the request died before it could answer

    async with committed_db() as retry:
        assert await store.consume_login_token(retry, token) == owner_id


# --- Sessions ----------------------------------------------------------------------------


async def test_a_session_names_its_user(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    secret = await store.open_session(db, owner_id)

    user = await store.find_session(db, secret)

    assert user == SessionUser(id=owner_id, telegram_user_id=TELEGRAM_ID)


async def test_an_unknown_secret_is_no_session(db: AsyncSession) -> None:
    await store.open_session(db, await _owner(db))

    assert await store.find_session(db, store.new_secret()) is None


async def test_a_session_ends_after_thirty_days(db: AsyncSession) -> None:
    secret = await store.open_session(db, await _owner(db), now=NOON)

    almost = NOON + timedelta(days=29, hours=23)
    assert await store.find_session(db, secret, now=almost) is not None
    assert await store.find_session(db, secret, now=NOON + timedelta(days=30)) is None


async def test_a_closed_session_is_dead_and_its_row_stays(db: AsyncSession) -> None:
    secret = await store.open_session(db, await _owner(db), now=NOON)
    later = NOON + timedelta(hours=1)

    assert await store.close_session(db, secret, now=later) is True

    assert await store.find_session(db, secret, now=later) is None
    assert await db.scalar(sa.select(WebSession.revoked_at)) == later
    assert await store.close_session(db, secret, now=later) is False


async def test_closing_one_session_leaves_the_others_open(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    laptop = await store.open_session(db, owner_id)
    phone = await store.open_session(db, owner_id)

    await store.close_session(db, laptop)

    assert await store.find_session(db, laptop) is None
    assert await store.find_session(db, phone) is not None


async def test_the_database_never_holds_the_session_secret(db: AsyncSession) -> None:
    secret = await store.open_session(db, await _owner(db))

    row = await db.scalar(sa.text("SELECT row_to_json(s)::text FROM sessions s"))

    assert isinstance(row, str)
    assert secret not in row
    assert await db.scalar(sa.select(WebSession.session_hash)) == store.digest(secret)


async def test_a_session_notes_its_use_at_most_every_five_minutes(db: AsyncSession) -> None:
    secret = await store.open_session(db, await _owner(db), now=NOON)

    await store.find_session(db, secret, now=NOON + timedelta(minutes=1))
    assert await db.scalar(sa.select(WebSession.last_seen_at)) == NOON

    much_later = NOON + timedelta(minutes=6)
    await store.find_session(db, secret, now=much_later)
    assert await db.scalar(sa.select(WebSession.last_seen_at)) == much_later


async def test_a_long_user_agent_is_cut(db: AsyncSession) -> None:
    await store.open_session(db, await _owner(db), user_agent="x" * 1000)

    saved = await db.scalar(sa.select(WebSession.user_agent))

    assert saved == "x" * store.USER_AGENT_MAX_LENGTH
