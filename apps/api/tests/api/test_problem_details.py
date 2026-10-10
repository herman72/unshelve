"""Every error leaves the API as an RFC 9457 problem, whoever raised it."""

import httpx
from fastapi import FastAPI

from tests.api.support import build_app, problem_body
from unshelve.api.errors import TITLES, install_error_handlers, title_for
from unshelve.contracts.states import IllegalTransitionError

CONSUME = "/api/v1/auth/magic/consume"
VALID_TOKEN = "a" * 32  # a placeholder: the schema only checks the length


# --- The real application ----------------------------------------------------------------


async def test_an_unknown_path_is_a_404_problem(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/no-such-thing")

    problem_body(response, 404)


async def test_a_wrong_method_is_a_405_problem_that_names_the_allowed_methods(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/api/v1/healthz")

    problem_body(response, 405)
    assert response.headers["allow"] == "GET"  # required by RFC 9110


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


# --- Titles ------------------------------------------------------------------------------


def test_titles_do_not_depend_on_the_python_version() -> None:
    # http.HTTPStatus calls 422 "Unprocessable Entity" before Python 3.13.
    assert title_for(422) == "Unprocessable Content"
    assert title_for(418) == "I'm a Teapot"  # a status without a title of ours still gets one


def test_every_documented_error_status_has_a_title_of_our_own() -> None:
    # The descriptions in openapi.json come from these titles. A status that is missing here
    # would take its text from the interpreter, and the committed document could then differ
    # from one machine to the next.
    documented = {
        int(status)
        for path in build_app().openapi()["paths"].values()
        for operation in path.values()
        for status in operation["responses"]
        if int(status) >= 400
    }
    assert documented <= set(TITLES)
