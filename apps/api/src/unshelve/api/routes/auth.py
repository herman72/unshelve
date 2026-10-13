"""Sign-in, sign-out and the current user."""

from fastapi import APIRouter, Request, Response

from unshelve.api.deps import SESSION_COOKIE, CurrentUserDep, DbDep, SessionSecretDep, SettingsDep
from unshelve.api.errors import UnauthorizedError, problems
from unshelve.api.schemas import MagicConsumeRequest, Me
from unshelve.auth import store
from unshelve.config import Settings
from unshelve.db.models import User

router = APIRouter(tags=["auth"])


def _cookie_is_https_only(settings: Settings) -> bool:
    # A "Secure" cookie is sent over HTTPS only. A browser on plain http://localhost would
    # not send it back, so the flag follows the address that the web app is served from.
    return settings.web_base_url.startswith("https://")


@router.post("/auth/magic/consume", status_code=204, responses=problems(401, 422))
async def consume_magic_link(
    body: MagicConsumeRequest,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> None:
    """Trade the one-time token from a login link for a session cookie."""
    owner_id = await store.consume_login_token(db, body.token)
    if owner_id is None:
        # One message for a wrong, a used and an expired token: the answer must not say
        # whether a token ever existed.
        raise UnauthorizedError("this link is not valid or has expired")
    secret = await store.open_session(db, owner_id, user_agent=request.headers.get("user-agent"))
    # The token is used up and the session exists, or neither: one commit covers both.
    await db.commit()
    response.set_cookie(
        SESSION_COOKIE,
        secret,
        max_age=int(store.SESSION_TTL.total_seconds()),
        path="/",
        httponly=True,  # scripts on the page cannot read it
        samesite="lax",  # other sites cannot make the browser send it with a POST
        secure=_cookie_is_https_only(settings),
    )


@router.post("/auth/logout", status_code=204, responses=problems(401))
async def logout(
    user: CurrentUserDep,
    secret: SessionSecretDep,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> None:
    """End the current session."""
    if secret is not None:  # always true here: current_user has checked this cookie
        await store.close_session(db, secret)
        await db.commit()
    # The same attributes as at sign-in, so that the browser finds the cookie to remove.
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_cookie_is_https_only(settings),
    )


@router.get("/me", responses=problems(401))
async def get_me(user: CurrentUserDep, db: DbDep) -> Me:
    """The signed-in user and their settings."""
    row = await db.get_one(User, user.id)
    return Me.model_validate(row)
