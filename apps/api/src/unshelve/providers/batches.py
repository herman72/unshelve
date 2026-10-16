"""Batches of questions to Claude, through the Message Batches API of the official SDK. It
implements ``BatchProvider``.

A batch costs half of the price of the same calls, and the API answers it within 24 hours;
most batches end within one hour. Each question goes as the request that ``ClaudeLLM`` sends
for it: the same model, system prompt, content, effort and JSON schema of the answer. So its
answer is the same kind of answer, and it is stored under the same cache key.
"""

from collections.abc import AsyncIterator, Sequence

import anthropic
from anthropic import transform_schema
from anthropic.types.beta import BetaJSONOutputFormatParam, BetaOutputConfigParam
from anthropic.types.beta.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.beta.messages import BetaMessageBatchIndividualResponse
from anthropic.types.beta.messages.batch_create_params import Request
from pydantic import BaseModel, ValidationError

from unshelve.contracts.dto import BatchAnswer, BatchQuestion, LLMResult, LLMUsage
from unshelve.providers.claude import (
    FALLBACK_BETA,
    FALLBACK_MODELS,
    THINKING_ROOM,
    Effort,
    content_of,
    provider_error,
)


class ClaudeBatches:
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

    async def send(self, questions: Sequence[BatchQuestion], schema: type[BaseModel]) -> str:
        fallback = self.model in FALLBACK_MODELS
        output_config: BetaOutputConfigParam = {
            "effort": self._effort,
            "format": BetaJSONOutputFormatParam(
                type="json_schema", schema=transform_schema(schema)
            ),
        }
        requests: list[Request] = []
        for question in questions:
            params = MessageCreateParamsNonStreaming(
                model=self.model,
                max_tokens=question.max_output_tokens + THINKING_ROOM,
                system=question.system,
                messages=[{"role": "user", "content": content_of(question.parts)}],
                output_config=output_config,
            )
            if fallback:
                params["fallbacks"] = "default"
            requests.append(Request(custom_id=question.custom_id, params=params))
        try:
            batch = await self._client.beta.messages.batches.create(
                requests=requests, betas=[FALLBACK_BETA] if fallback else anthropic.omit
            )
        except anthropic.APIError as error:
            raise provider_error(error) from error
        return batch.id

    async def ended(self, batch_id: str) -> bool:
        try:
            batch = await self._client.beta.messages.batches.retrieve(batch_id)
        except anthropic.APIError as error:
            raise provider_error(error) from error
        return batch.processing_status == "ended"

    async def answers[T: BaseModel](
        self, batch_id: str, schema: type[T]
    ) -> AsyncIterator[BatchAnswer[T]]:
        try:
            results = await self._client.beta.messages.batches.results(batch_id)
            async for line in results:
                yield answer_of(line, schema)
        except anthropic.APIError as error:
            raise provider_error(error) from error


def answer_of[T: BaseModel](
    line: BetaMessageBatchIndividualResponse, schema: type[T]
) -> BatchAnswer[T]:
    """One line of the results: the answer in the schema, or why there is none."""
    outcome = line.result
    if outcome.type == "errored":
        return BatchAnswer[schema](
            custom_id=line.custom_id, error=f"errored: {outcome.error.error.type}"
        )
    if outcome.type != "succeeded":
        # canceled, or expired: the batch ended before this question was answered.
        return BatchAnswer[schema](custom_id=line.custom_id, error=outcome.type)
    message = outcome.message
    if message.stop_reason == "refusal":
        category = message.stop_details.category if message.stop_details else None
        return BatchAnswer[schema](
            custom_id=line.custom_id, error=f"the model declined ({category})"
        )
    text = next((block.text for block in message.content if block.type == "text"), "")
    try:
        value = schema.model_validate_json(text)
    except ValidationError:
        # No JSON, or not the schema: also an answer that max_tokens cut.
        return BatchAnswer[schema](
            custom_id=line.custom_id, error=f"the answer does not fit {schema.__name__}"
        )
    usage = message.usage
    return BatchAnswer[schema](
        custom_id=line.custom_id,
        result=LLMResult[schema](
            value=value,
            model=message.model,
            usage=LLMUsage(
                input_tokens=usage.input_tokens + (usage.cache_creation_input_tokens or 0),
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
            ),
        ),
    )
