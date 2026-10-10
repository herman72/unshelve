"""API errors, rendered as RFC 9457 problem details.

Handlers raise an ``ApiError`` subclass. Nothing else in the API builds an error response, so
every error has the same shape.
"""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, ClassVar

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from unshelve.api.schemas import FieldProblem, Problem
from unshelve.contracts.states import IllegalTransitionError

PROBLEM_JSON = "application/problem+json"

# Written out, not taken from http.HTTPStatus: its phrases differ between Python versions
# (422 is "Unprocessable Entity" before 3.13). The title is part of the committed OpenAPI
# document, which must not depend on the interpreter that generated it.
TITLES: Mapping[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    422: "Unprocessable Content",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
}

log = structlog.get_logger()


def title_for(status: int) -> str:
    return TITLES.get(status) or HTTPStatus(status).phrase


class ApiError(Exception):
    status: ClassVar[HTTPStatus] = HTTPStatus.INTERNAL_SERVER_ERROR

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or title_for(self.status))
        self.detail = detail


class UnauthorizedError(ApiError):
    status = HTTPStatus.UNAUTHORIZED


class NotFoundError(ApiError):
    status = HTTPStatus.NOT_FOUND


class ConflictError(ApiError):
    status = HTTPStatus.CONFLICT


class ServiceUnavailableError(ApiError):
    status = HTTPStatus.SERVICE_UNAVAILABLE


class NotImplementedYetError(ApiError):
    """The endpoint is part of the contract, and its behaviour arrives in a later package."""

    status = HTTPStatus.NOT_IMPLEMENTED


def problems(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` for the error statuses a route can return."""
    return {status: {"model": Problem, "description": title_for(status)} for status in statuses}


def problem_response(
    status: int,
    *,
    detail: str | None = None,
    errors: list[FieldProblem] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = Problem(title=title_for(status), status=status, detail=detail, errors=errors)
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status,
        media_type=PROBLEM_JSON,
        headers=dict(headers) if headers else None,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return problem_response(exc.status, detail=exc.detail)

    # Raised by the framework itself: unknown route, wrong method.
    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        # The framework fills an empty detail with the status phrase; do not repeat the title.
        repeats_title = exc.detail == HTTPStatus(exc.status_code).phrase
        return problem_response(
            exc.status_code,
            detail=None if repeats_title else exc.detail,
            headers=exc.headers,  # a 405 must say which methods are allowed (RFC 9110)
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        errors = [
            FieldProblem(pointer="/" + "/".join(str(part) for part in e["loc"]), detail=e["msg"])
            for e in exc.errors()
        ]
        return problem_response(HTTPStatus.UNPROCESSABLE_ENTITY, errors=errors)

    @app.exception_handler(IllegalTransitionError)
    async def _illegal_move(request: Request, exc: IllegalTransitionError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return problem_response(HTTPStatus.CONFLICT, detail=str(exc))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        # The detail stays in the log. A stack trace or a message can leak private data.
        log.error("api.unhandled_error", path=request.url.path, exc_info=exc)
        return problem_response(HTTPStatus.INTERNAL_SERVER_ERROR)
