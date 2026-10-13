"""Sign-in: a one-time login token is traded for a browser session.

Read docs/adr/0008 first. The model in one paragraph: the owner asks for a *login token*, from
the bot or from ``unshelve auth link``. The token travels inside a link, works once, and stops
working after five minutes. The web app trades it for a *session*, and the browser keeps the
session in a cookie. Both are random secrets. The database keeps only a hash of each one, so a
copy of the database is not enough to sign in.

Every function runs in the caller's transaction and does not commit. The caller commits.

Time: one clock, the database's ``now()``, as in the job queue. Tests pass ``now`` to move it.
"""

import dataclasses
import hashlib
import secrets
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.db.models import TIMESTAMP, AuthToken, User, WebSession

LOGIN_TOKEN_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(days=30)
# How often a session row records that it was used. One page makes several requests, and one
# write in this interval is enough to know when a session was last active.
SEEN_INTERVAL = timedelta(minutes=5)
# The browser sends this text and nothing checks it, so only the start is kept.
USER_AGENT_MAX_LENGTH = 300


@dataclasses.dataclass(frozen=True, slots=True)
class SessionUser:
    """The user that a live session belongs to."""

    id: int
    telegram_user_id: int


def _now(now: datetime | None) -> sa.ColumnElement[datetime]:
    return sa.func.now() if now is None else sa.literal(now, TIMESTAMP)


def new_secret() -> str:
    """256 random bits, as text that is safe in a URL and in a cookie."""
    return secrets.token_urlsafe(32)


def digest(secret: str) -> str:
    """What the database stores in place of a secret.

    Plain SHA-256 is enough, and a slow password hash would add nothing: the secret is 256
    random bits, so there is nothing to guess.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


async def ensure_owner(session: AsyncSession, telegram_user_id: int) -> int:
    """The id of the user with this Telegram id. The row is created the first time."""
    await session.execute(
        postgresql.insert(User)
        .values(telegram_user_id=telegram_user_id)
        .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
    )
    found = await session.execute(
        sa.select(User.id).where(User.telegram_user_id == telegram_user_id)
    )
    return found.scalar_one()


async def issue_login_token(
    session: AsyncSession,
    owner_id: int,
    *,
    ttl: timedelta = LOGIN_TOKEN_TTL,
    now: datetime | None = None,
) -> str:
    """Create a login token for this user and return it. This is the only time it is known."""
    token = new_secret()
    await session.execute(
        sa.insert(AuthToken).values(
            owner_id=owner_id, token_hash=digest(token), expires_at=_now(now) + ttl
        )
    )
    return token


async def consume_login_token(
    session: AsyncSession, token: str, *, now: datetime | None = None
) -> int | None:
    """Use up a login token. Returns the id of its user, or None when the token does not work.

    One UPDATE checks the token and marks it as used. So two requests with the same token
    cannot both succeed: the second one waits for the first, then finds ``used_at`` set.

    A wrong token, a used token and an expired token all give None. The caller must not tell
    the client which one it was.
    """
    return await session.scalar(
        sa.update(AuthToken)
        .where(
            AuthToken.token_hash == digest(token),
            AuthToken.used_at.is_(None),
            AuthToken.expires_at > _now(now),
        )
        .values(used_at=_now(now))
        .returning(AuthToken.owner_id)
    )


async def open_session(
    session: AsyncSession,
    owner_id: int,
    *,
    user_agent: str | None = None,
    ttl: timedelta = SESSION_TTL,
    now: datetime | None = None,
) -> str:
    """Start a session for this user and return its secret, which goes into the cookie.

    The server makes the secret. A value that a client sends is never turned into a session.
    """
    secret = new_secret()
    await session.execute(
        sa.insert(WebSession).values(
            owner_id=owner_id,
            session_hash=digest(secret),
            user_agent=user_agent[:USER_AGENT_MAX_LENGTH] if user_agent else None,
            expires_at=_now(now) + ttl,
            last_seen_at=_now(now),
        )
    )
    return secret


async def find_session(
    session: AsyncSession, secret: str, *, now: datetime | None = None
) -> SessionUser | None:
    """The user of a live session, or None. A session is live until it expires or is closed.

    At most once in ``SEEN_INTERVAL``, this also records that the session was used. The caller
    commits to keep that mark.
    """
    current = _now(now)
    not_seen_lately = sa.or_(
        WebSession.last_seen_at.is_(None), WebSession.last_seen_at < current - SEEN_INTERVAL
    )
    found = await session.execute(
        sa.select(WebSession.id, User.id, User.telegram_user_id, not_seen_lately)
        .join(User, User.id == WebSession.owner_id)
        .where(
            WebSession.session_hash == digest(secret),
            WebSession.revoked_at.is_(None),
            WebSession.expires_at > current,
        )
    )
    row = found.one_or_none()
    if row is None:
        return None
    session_id, user_id, telegram_user_id, mark_as_seen = row
    if mark_as_seen:
        await session.execute(
            sa.update(WebSession).where(WebSession.id == session_id).values(last_seen_at=current)
        )
    return SessionUser(id=user_id, telegram_user_id=telegram_user_id)


async def close_session(session: AsyncSession, secret: str, *, now: datetime | None = None) -> bool:
    """End a session. Returns False when no open session has this secret.

    The row stays, with ``revoked_at`` set. The cookie may still be in a browser, and from now
    on it is worth nothing.
    """
    closed = await session.scalar(
        sa.update(WebSession)
        .where(WebSession.session_hash == digest(secret), WebSession.revoked_at.is_(None))
        .values(revoked_at=_now(now))
        .returning(WebSession.id)
    )
    return closed is not None
