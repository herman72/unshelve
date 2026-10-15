"""A pretend Anthropic API for the tests: the real SDK talks to it, and no test reaches the
real API. It answers each request with a card that fits the schema of the request, unless a
test gives a different answer."""

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2

Answer = Callable[[dict[str, Any]], httpx2.Response]


def message(
    text: str,
    *,
    model: str = "claude-opus-5-5",
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
) -> httpx2.Response:
    """An answer as the Messages API gives it: a thinking block, then the text."""
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": text},
            ],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage
            or {
                "input_tokens": 1200,
                "output_tokens": 150,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    )


def refusal(category: str) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [],
            "stop_reason": "refusal",
            "stop_sequence": None,
            "stop_details": {"type": "refusal", "category": category, "explanation": "no"},
            "usage": {"input_tokens": 1200, "output_tokens": 0},
        },
    )


def error(status: int, kind: str, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status,
        headers=headers or {},
        json={"type": "error", "error": {"type": kind, "message": f"pretend {kind}"}},
    )


def card_for_schema(body: dict[str, Any]) -> httpx2.Response:
    """A valid answer for the schema of the request."""
    schema = body["output_config"]["format"]["schema"]
    value = {name: _value(spec) for name, spec in schema["properties"].items()}
    return message(json.dumps(value))


def _value(spec: dict[str, Any]) -> object:
    match spec.get("type"):
        case "array":
            return ["a point"]
        case "integer":
            return 3
        case "number":
            return 0.5
        case "boolean":
            return False
        case _:
            return "a value"


class PretendAPI:
    """Answers each request of the SDK with ``answer``, and keeps each request."""

    def __init__(self, answer: Answer = card_for_schema) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self._answer = answer

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        self.headers.append(dict(request.headers))
        return self._answer(body)

    def client(self) -> anthropic.AsyncAnthropic:
        # No retries in the SDK: a test sees each answer once.
        return anthropic.AsyncAnthropic(
            api_key="test-key",
            base_url="http://pretend.test",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(self._handle)
            ),
        )
