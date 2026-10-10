"""Errors that cross module boundaries."""

from typing import Literal

FetchFailure = Literal[
    "blocked_address",  # the SSRF guard refused the destination
    "too_large",
    "timeout",
    "http_error",
    "unsupported",  # content type or scheme we do not handle
    "network",
]

# Failures that can succeed on a later attempt.
_RETRYABLE_FETCH: frozenset[FetchFailure] = frozenset({"timeout", "network"})
_RETRYABLE_HTTP: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


class UnshelveError(Exception):
    """Base class for errors raised by this package."""


class FetchError(UnshelveError):
    def __init__(self, reason: FetchFailure, url: str, *, status: int | None = None) -> None:
        detail = f"{reason} ({status})" if status is not None else reason
        super().__init__(f"fetch failed: {detail}: {url}")
        self.reason: FetchFailure = reason
        self.url = url
        self.status = status

    @property
    def retryable(self) -> bool:
        if self.reason == "http_error":
            return self.status in _RETRYABLE_HTTP
        return self.reason in _RETRYABLE_FETCH


class ExtractError(UnshelveError):
    """The content was fetched but could not be turned into text."""


class ProviderError(UnshelveError):
    """A model or embedding provider failed."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable
