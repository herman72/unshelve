from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from unshelve.contracts.dto import CaptureEnvelope, EmbeddingSpec, MediaRef
from unshelve.contracts.enums import CaptureSource, JobKind
from unshelve.contracts.errors import FetchError, ProviderError
from unshelve.contracts.jobs import PAYLOAD_TYPES, FetchItemPayload, Permanent, Retry
from unshelve.contracts.protocols import accepts

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _envelope(**overrides: object) -> CaptureEnvelope:
    fields: dict[str, object] = {
        "source": "bot",
        "chat_id": -1001234567890,
        "message_id": 42,
        "posted_at": NOW,
        "text": "worth reading https://example.com/a",
        "urls": ["https://example.com/a"],
    }
    return CaptureEnvelope.model_validate(fields | overrides)


def _key(envelope: CaptureEnvelope) -> tuple[int, int, int]:
    return (envelope.chat_id, envelope.message_id, envelope.edit_seq)


def test_bot_and_export_build_the_same_envelope() -> None:
    from_bot = _envelope(source="bot")
    from_export = _envelope(source="export")
    assert _key(from_bot) == _key(from_export)
    assert from_bot.source is CaptureSource.BOT


def test_an_envelope_needs_a_timezone() -> None:
    with pytest.raises(ValidationError):
        _envelope(posted_at=datetime(2026, 10, 10, 12, 0))  # noqa: DTZ001 - the point of the test


def test_envelopes_are_immutable_and_reject_unknown_fields() -> None:
    envelope = _envelope(media=[MediaRef(kind="photo", file_unique_id="AQAD")])
    with pytest.raises(ValidationError):
        envelope.text = "changed"  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError):
        _envelope(surprise=True)


def test_every_job_kind_has_a_payload_type() -> None:
    assert set(PAYLOAD_TYPES) == set(JobKind)


def test_payloads_hold_ids_only_and_reject_extras() -> None:
    assert FetchItemPayload(item_id=7).model_dump() == {"item_id": 7}
    with pytest.raises(ValidationError):
        FetchItemPayload.model_validate({"item_id": 7, "url": "https://example.com"})


def test_failure_signals_carry_their_reason() -> None:
    retry = Retry("rate limited", after=timedelta(seconds=30))
    assert (retry.reason, retry.after) == ("rate limited", timedelta(seconds=30))
    assert Permanent("item was deleted").reason == "item was deleted"


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (FetchError("timeout", "u"), True),
        (FetchError("network", "u"), True),
        (FetchError("http_error", "u", status=503), True),
        (FetchError("http_error", "u", status=429), True),
        (FetchError("http_error", "u", status=404), False),
        (FetchError("blocked_address", "u"), False),
        (FetchError("too_large", "u"), False),
        (FetchError("unsupported", "u"), False),
        # A name without an address is a dead link. Asking again in a minute does not help.
        (FetchError("no_such_host", "u"), False),
    ],
)
def test_only_transient_fetch_failures_are_retryable(error: FetchError, retryable: bool) -> None:
    assert error.retryable is retryable


def test_a_provider_error_keeps_its_wait() -> None:
    error = ProviderError("rate limited", retryable=True, retry_after=30.0)

    assert (error.retryable, error.retry_after) == (True, 30.0)
    assert ProviderError("declined", retryable=False).retry_after is None


def test_a_fetch_error_keeps_the_wait_that_the_server_asked_for() -> None:
    error = FetchError("http_error", "u", status=429, retry_after=120.0)

    assert (error.status, error.retry_after, error.retryable) == (429, 120.0, True)
    assert FetchError("timeout", "u").retry_after is None


@pytest.mark.parametrize(
    ("content_type", "accept", "expected"),
    [
        ("text/html; charset=utf-8", ["text/html"], True),
        ("TEXT/HTML", ["text/*"], True),
        ("application/pdf", ["text/*", "application/pdf"], True),
        ("image/png", ["text/*"], False),
        ("image/png", [], True),
        (None, [], True),
        (None, ["*/*"], True),
        (None, ["text/*"], False),
    ],
)
def test_accepts(content_type: str | None, accept: list[str], expected: bool) -> None:
    assert accepts(content_type, accept) is expected


def test_an_embedding_spec_needs_a_positive_dimension() -> None:
    with pytest.raises(ValidationError):
        EmbeddingSpec(provider="x", model="y", dim=0)
