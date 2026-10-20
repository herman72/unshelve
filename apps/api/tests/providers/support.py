"""A pretend Anthropic API for the tests: the real SDK talks to it, and no test reaches the
real API. It answers each request with a card that fits the schema of the request, unless a
test gives a different answer."""

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2

Answer = Callable[[dict[str, Any]], httpx2.Response]


def message_json(
    text: str,
    *,
    model: str = "claude-opus-5-5",
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """An answer as the Messages API gives it: a thinking block, then the text."""
    return {
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
    }


def message(
    text: str,
    *,
    model: str = "claude-opus-5-5",
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
) -> httpx2.Response:
    return httpx2.Response(
        200, json=message_json(text, model=model, stop_reason=stop_reason, usage=usage)
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


def cited_answer(body: dict[str, Any]) -> httpx2.Response:
    """An answer with native citations: it quotes the first sentence of the first document."""
    first = body["messages"][0]["content"][0]
    data = first["source"]["data"]
    end = data.find(". ") + 1 or len(data)
    answer = message_json("unused")
    answer["content"] = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "The first source says that "},
        {
            "type": "text",
            "text": data[:end].lower(),
            "citations": [
                {
                    "type": "char_location",
                    "cited_text": data[:end],
                    "document_index": 0,
                    "document_title": first.get("title"),
                    "start_char_index": 0,
                    "end_char_index": end,
                }
            ],
        },
    ]
    return httpx2.Response(200, json=answer)


def card_for_schema(body: dict[str, Any]) -> httpx2.Response:
    """A valid answer for the schema of the request."""
    return message(json.dumps(value_for_schema(body)))


def value_for_schema(params: dict[str, Any]) -> dict[str, object]:
    """A value that fits the schema of a request."""
    schema = params["output_config"]["format"]["schema"]
    return {name: _value(spec) for name, spec in schema["properties"].items()}


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


# --- The Message Batches API ------------------------------------------------------------------

BATCH_ID = "msgbatch_test"
BatchLine = dict[str, Any]


def answered(request: dict[str, Any]) -> BatchLine:
    """A line of the results: the request was answered with a value of its schema."""
    value = value_for_schema(request["params"])
    return {
        "custom_id": request["custom_id"],
        "result": {
            "type": "succeeded",
            "message": message_json(json.dumps(value), model=request["params"]["model"]),
        },
    }


def answer_each(requests: list[dict[str, Any]]) -> list[BatchLine]:
    return [answered(request) for request in requests]


class PretendBatchAPI:
    """The Message Batches API for the tests: the real SDK talks to it. It keeps each batch
    that it gets, says that a batch has ended when ``ended`` is True, and gives the results
    that ``lines`` makes from the requests, as JSON lines."""

    def __init__(
        self,
        lines: Callable[[list[dict[str, Any]]], list[BatchLine]] | None = None,
        *,
        ended: bool = True,
        fail: httpx2.Response | None = None,
    ) -> None:
        self.created: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.ended = ended
        self._lines = lines or answer_each
        self._fail = fail

    def _batch(self, status: str) -> dict[str, Any]:
        done = status == "ended"
        return {
            "id": BATCH_ID,
            "type": "message_batch",
            "processing_status": status,
            "request_counts": {
                "processing": 0 if done else 1,
                "succeeded": 1 if done else 0,
                "errored": 0,
                "canceled": 0,
                "expired": 0,
            },
            "created_at": "2026-10-16T12:00:00Z",
            "expires_at": "2026-10-17T12:00:00Z",
            "ended_at": "2026-10-16T12:30:00Z" if done else None,
            "archived_at": None,
            "cancel_initiated_at": None,
            "results_url": f"http://pretend.test/v1/messages/batches/{BATCH_ID}/results"
            if done
            else None,
        }

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.headers.append(dict(request.headers))
        if self._fail is not None:
            return self._fail
        path = request.url.path
        if request.method == "POST" and path == "/v1/messages/batches":
            self.created.append(json.loads(request.content))
            return httpx2.Response(200, json=self._batch("in_progress"))
        if request.method == "GET" and path == f"/v1/messages/batches/{BATCH_ID}":
            return httpx2.Response(200, json=self._batch("ended" if self.ended else "in_progress"))
        if request.method == "GET" and path == f"/v1/messages/batches/{BATCH_ID}/results":
            lines = self._lines(self.created[-1]["requests"])
            return httpx2.Response(200, content="\n".join(json.dumps(line) for line in lines))
        return error(404, "not_found_error")

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key="test-key",
            base_url="http://pretend.test",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(self._handle)
            ),
        )
