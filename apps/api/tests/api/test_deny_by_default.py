"""Deny by default: every route needs a session unless it is on the short public list.

The routes come from the application's own route table, so a route added later is covered
without anyone remembering to add a test for it.
"""

import re

import httpx
import pytest

from tests.api.support import PUBLIC_OPERATIONS, api_operations, build_app, problem_body
from unshelve.api.deps import SESSION_COOKIE

PROTECTED = [
    pytest.param(op.method, op.path, id=f"{op.method} {op.path}")
    for op in api_operations(build_app())
    if op.operation_id not in PUBLIC_OPERATIONS
]


def with_ids(path: str) -> str:
    """Fill every path parameter with 1, which is a valid id."""
    return re.sub(r"\{[^}]+\}", "1", path)


def test_the_tests_below_cover_the_route_table() -> None:
    # Guards for the two lists above. A public list with a stale name, or an empty list of
    # protected routes, would let the parametrised tests pass without checking anything.
    operation_ids = {op.operation_id for op in api_operations(build_app())}

    assert PUBLIC_OPERATIONS.issubset(operation_ids)
    assert PROTECTED


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_a_protected_route_answers_401_without_a_session(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, with_ids(path))

    problem_body(response, 401)


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_a_protected_route_does_not_trust_a_session_it_cannot_check(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    # Until sessions exist (M0-6), a cookie is not proof of anything: 501, never a real answer.
    response = await client.request(
        method, with_ids(path), headers={"Cookie": f"{SESSION_COOKIE}=not-checked-yet"}
    )

    problem_body(response, 501)
