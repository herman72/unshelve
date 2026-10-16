"""Claude, through the official Anthropic SDK. It implements ``LLMProvider``.

One call gives one answer in the schema of the caller: the API constrains the output to the
JSON schema (structured output), and the SDK validates it with Pydantic (``messages.parse``).
The call has no tools, so a page that asks the model to do something has nothing to do it
with.

Each failure becomes a ``ProviderError`` that says if a second try can help. The SDK itself
tries a 429, a 5xx or a network error two times; the longer waits are for the job queue.
"""

import base64
from collections.abc import Sequence
from typing import Literal

import anthropic
from anthropic.types.beta import BetaImageBlockParam, BetaOutputConfigParam, BetaTextBlockParam
from pydantic import BaseModel, ValidationError

from unshelve.contracts.dto import ImagePart, LLMResult, LLMUsage, TextPart
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
        if message.stop_reason == "refusal":
            category = message.stop_details.category if message.stop_details else None
            raise ProviderError(f"the model declined ({category})", retryable=False)
        if message.parsed_output is None:
            raise ProviderError(f"no answer ({message.stop_reason})", retryable=False)
        usage = message.usage
        return LLMResult[schema](
            value=message.parsed_output,
            model=message.model,
            usage=LLMUsage(
                input_tokens=usage.input_tokens + (usage.cache_creation_input_tokens or 0),
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
            ),
        )


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
