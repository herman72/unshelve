"""Every error leaves the API as an RFC 9457 problem, whoever raised it."""

import httpx
from fastapi import FastAPI

from tests.api.support import problem_body
from unshelve.api.errors import install_error_handlers
from unshelve.contracts.states import IllegalTransitionError

CONSUME = "/api/v1/auth/magic/consume"
VALID_TOKEN = "a" * 32  # a placeholder: the schema only checks the length


# --- The real application ----------------------------------------------------------------


async def test_an_unknown_path_is_a_404_problem(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/no-such-thing")

    problem_body(response, 404)


async def test_a_wrong_method_is_a_405_problem(client: httpx.AsyncClient) -> None:
    response = await client.delete("/api/v1/healthz")

    problem_body(response, 405)


async def test_a_missing_field_is_a_422_that_points_at_the_field(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(CONSUME, json={})

    body = problem_body(response, 422)
    assert body["errors"][0]["pointer"] == "/body/token"


async def test_an_unknown_field_is_a_422(client: httpx.AsyncClient) -> None:
    # Request bodies reject unknown fields, so a typo is an error and not a silent no-op.
    response = await client.post(CONSUME, json={"token": VALID_TOKEN, "tokne": "typo"})

    body = problem_body(response, 422)
    assert body["errors"][0]["pointer"] == "/body/tokne"


async def test_a_body_that_is_not_json_is_a_422(client: httpx.AsyncClient) -> None:
    response = await client.post(
        CONSUME, content=b"{not json", headers={"Content-Type": "application/json"}
    )

    problem_body(response, 422)


# --- Errors that the real routes cannot raise yet ----------------------------------------


def build_probe_app() -> FastAPI:
    """A small application with the same handlers and routes that fail on purpose."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/illegal-move")
    async def illegal_move() -> None:
        raise IllegalTransitionError("done", "inbox")

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("secret text")

    return app


async def test_an_illegal_status_move_is_a_409_problem() -> None:
    app = build_probe_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as probe:
        response = await probe.get("/illegal-move")

    body = problem_body(response, 409)
    assert body["detail"] == "cannot move from 'done' to 'inbox'"


async def test_an_unexpected_error_is_a_500_problem_that_leaks_nothing() -> None:
    app = build_probe_app()
    # The server re-raises the error after it sends the answer. The test client must not.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as probe:
        response = await probe.get("/crash")

    body = problem_body(response, 500)
    assert "detail" not in body
    assert "secret text" not in response.text
