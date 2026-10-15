"""Errors that cross module boundaries."""

from typing import Literal

FetchFailure = Literal[
    "blocked_address",  # the SSRF guard refused the destination
    "too_large",
    "timeout",
    "http_error",
    "unsupported",  # content type or scheme we do not handle
    "network",
    "no_such_host",  # DNS has no address for the name: a dead link, not a broken network
]

# Failures that can succeed on a later attempt.
_RETRYABLE_FETCH: frozenset[FetchFailure] = frozenset({"timeout", "network"})
_RETRYABLE_HTTP: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


class UnshelveError(Exception):
    """Base class for errors raised by this package."""


class FetchError(UnshelveError):
    def __init__(
        self,
        reason: FetchFailure,
        url: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        detail = f"{reason} ({status})" if status is not None else reason
        super().__init__(f"fetch failed: {detail}: {url}")
        self.reason: FetchFailure = reason
        self.url = url
        self.status = status
        # Seconds that the server asked us to wait ("Retry-After"), for a 429 or a 503.
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        if self.reason == "http_error":
            return self.status in _RETRYABLE_HTTP
        return self.reason in _RETRYABLE_FETCH


class ExtractError(UnshelveError):
    """The content was fetched but could not be turned into text."""


class ProviderError(UnshelveError):
    """A model or embedding provider failed."""

    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        # Seconds to wait before the next try: the wait of a 429, or the time until the daily
        # budget is new again.
        self.retry_after = retry_after
