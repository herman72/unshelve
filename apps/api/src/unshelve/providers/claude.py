"""Claude, through the official Anthropic SDK. It implements ``LLMProvider``.

One call of ``generate`` gives one answer in the schema of the caller: the API constrains the
output to the JSON schema (structured output), and the SDK validates it with Pydantic
(``messages.parse``). One call of ``cite`` gives an answer in free text with native citations:
each source is a plain text document, and the API says which characters of it support each
part of the answer. The calls have no tools, so a page that asks the model to do something has
nothing to do it with.

Each failure becomes a ``ProviderError`` that says if a second try can help. The SDK itself
tries a 429, a 5xx or a network error two times; the longer waits are for the job queue.
"""

import base64
from collections.abc import Sequence
from typing import Literal

import anthropic
from anthropic.types.beta import (
    BetaCitationCharLocation,
    BetaImageBlockParam,
    BetaMessage,
    BetaOutputConfigParam,
    BetaRequestDocumentBlockParam,
    BetaTextBlock,
    BetaTextBlockParam,
)
from pydantic import BaseModel, ValidationError

from unshelve.contracts.dto import (
    CitedAnswer,
    CitedSegment,
    ImagePart,
    LLMResult,
    LLMUsage,
    Quote,
    SourceText,
    TextPart,
)
from unshelve.contracts.errors import ProviderError

# Claude Opus 5.5 and Claude Haiku 5.5 always think a little, and the thinking counts toward
# max_tokens. This room comes on top of the visible answer that the caller asks for.
THINKING_ROOM = 4_000
# On these models the API can run a refused request again on a different model, in the
# same call. Claude Haiku 5.5 has no such fallback.
FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# A second try can help: the API is busy, or the network failed.
_CAN_PASS = (
    anthropic.RateLimitError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
    anthropic.DeadlineExceededError,
    anthropic.InternalServerError,
    anthropic.ConflictError,
    anthropic.APIConnectionError,
)
# A person must fix these: the key, the permissions or the name of the model. The item
# waits, so that it gets its card after the fix.
_FIX_BY_HAND = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
)
_NO_CREDIT = 402  # "billing_error": the account has no credit
# The API takes a document title of 1 to 500 characters.
TITLE_LIMIT = 500

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ImageType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]
_IMAGE_TYPES: dict[str, ImageType] = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}


class ClaudeLLM:
    def __init__(
        self,
        *,
        model: str,
        effort: Effort = "low",
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self._effort: Effort = effort
        # The key comes from ANTHROPIC_API_KEY in the environment.
        self._client = client or anthropic.AsyncAnthropic(max_retries=2)

    async def aclose(self) -> None:
        await self._client.close()

    async def generate[T: BaseModel](
        self,
        *,
        task: str,
        prompt_version: str,
        system: str,
        parts: Sequence[TextPart | ImagePart],
        schema: type[T],
        max_output_tokens: int,
    ) -> LLMResult[T]:
        fallback = self.model in FALLBACK_MODELS
        content = content_of(parts)
        config: BetaOutputConfigParam = {"effort": self._effort}
        try:
            message = await self._client.beta.messages.parse(
                model=self.model,
                max_tokens=max_output_tokens + THINKING_ROOM,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_format=schema,
                output_config=config,
                betas=[FALLBACK_BETA] if fallback else anthropic.omit,
                fallbacks="default" if fallback else anthropic.omit,
            )
        except anthropic.APIError as error:
            raise provider_error(error) from error
        except (ValidationError, ValueError) as error:
            # The answer is no JSON, or it does not fit the schema (also a cut answer).
            raise ProviderError(
                f"the answer does not fit {schema.__name__}", retryable=False
            ) from error
        _check_refusal(message)
        if message.parsed_output is None:
            raise ProviderError(f"no answer ({message.stop_reason})", retryable=False)
        return LLMResult[schema](
            value=message.parsed_output, model=message.model, usage=_usage(message)
        )

    async def cite(
        self,
        *,
        task: str,
        prompt_version: str,
        system: str,
        sources: Sequence[SourceText],
        question: str,
        max_output_tokens: int,
    ) -> LLMResult[CitedAnswer]:
        fallback = self.model in FALLBACK_MODELS
        # The sources first, then the question: the documentation of citations does the same.
        content: list[BetaRequestDocumentBlockParam | BetaTextBlockParam] = [
            *(document_of(source) for source in sources),
            {"type": "text", "text": question},
        ]
        config: BetaOutputConfigParam = {"effort": self._effort}
        try:
            # No output format: citations and structured output cannot go together (a 400).
            message = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=max_output_tokens + THINKING_ROOM,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config=config,
                betas=[FALLBACK_BETA] if fallback else anthropic.omit,
                fallbacks="default" if fallback else anthropic.omit,
            )
        except anthropic.APIError as error:
            raise provider_error(error) from error
        _check_refusal(message)
        if message.stop_reason == "max_tokens":
            raise ProviderError("the answer was cut at its length limit", retryable=False)
        # Thinking blocks and the blocks of a fallback carry no text of the answer.
        segments = [
            CitedSegment(text=block.text, quotes=_quotes(block))
            for block in message.content
            if isinstance(block, BetaTextBlock)
        ]
        return LLMResult[CitedAnswer](
            value=CitedAnswer(segments=segments), model=message.model, usage=_usage(message)
        )


def _check_refusal(message: BetaMessage) -> None:
    if message.stop_reason == "refusal":
        category = message.stop_details.category if message.stop_details else None
        raise ProviderError(f"the model declined ({category})", retryable=False)


def _usage(message: BetaMessage) -> LLMUsage:
    usage = message.usage
    return LLMUsage(
        input_tokens=usage.input_tokens + (usage.cache_creation_input_tokens or 0),
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
    )


def document_of(source: SourceText) -> BetaRequestDocumentBlockParam:
    """A source as a plain text document with citations. The API cuts it into sentences, and a
    citation quotes one sentence or more, by their characters."""
    document: BetaRequestDocumentBlockParam = {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": source.text},
        "citations": {"enabled": True},
    }
    # The API refuses an empty title or context, and a title of more than 500 characters.
    if title := source.title.strip()[:TITLE_LIMIT]:
        document["title"] = title
    if source.context:
        document["context"] = source.context
    return document


def _quotes(block: BetaTextBlock) -> list[Quote]:
    # Each source is a plain text document, so each citation has a range of characters.
    return [
        Quote(
            source=citation.document_index,
            start=citation.start_char_index,
            end=citation.end_char_index,
            text=citation.cited_text,
        )
        for citation in block.citations or []
        if isinstance(citation, BetaCitationCharLocation)
    ]


def provider_error(error: anthropic.APIError) -> ProviderError:
    """The error of the API as a ``ProviderError`` that says if a second try can help."""
    if isinstance(error, _CAN_PASS):
        return ProviderError(_name(error), retryable=True, retry_after=_wait(error))
    if isinstance(error, _FIX_BY_HAND):
        return ProviderError(f"{_name(error)}: a person must fix it", retryable=True)
    retryable = isinstance(error, anthropic.APIStatusError) and error.status_code == _NO_CREDIT
    return ProviderError(_name(error), retryable=retryable)


def content_of(
    parts: Sequence[TextPart | ImagePart],
) -> list[BetaTextBlockParam | BetaImageBlockParam]:
    """The content of the user message: the parts in their order."""
    return [_block(part) for part in parts]


def _block(part: TextPart | ImagePart) -> BetaTextBlockParam | BetaImageBlockParam:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    media_type = _IMAGE_TYPES.get(part.mime)
    if media_type is None:
        raise ProviderError(f"the model cannot read an image of type {part.mime}", retryable=False)
    data = base64.standard_b64encode(part.data).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def _name(error: anthropic.APIError) -> str:
    if isinstance(error, anthropic.APIStatusError):
        return f"{error.status_code} {error.type or 'error'}"
    return type(error).__name__


def _wait(error: anthropic.APIError) -> float | None:
    """The seconds of "retry-after", when the API sends them."""
    if not isinstance(error, anthropic.APIStatusError):
        return None
    value = error.response.headers.get("retry-after", "")
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None
