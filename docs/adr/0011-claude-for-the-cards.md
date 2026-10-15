# 0011. Claude for the cards: structured output, a cache in front, a daily budget

- Status: Accepted
- Date: 2026-10-15

## Context

From milestone M2, a model writes the card of each item: a title, a summary, key points, tags and the language of the text. The text comes from a web page or a message, so it can hold instructions for the model (prompt injection). A model call costs money, and the pipeline can run a step again: delivery of a job is "at least once" ([ADR 0002](0002-postgres-job-queue-with-leases.md)), and an edit starts the pipeline again ([note 12](../learn/12-resolver.md), part 9). The plan ([`docs/architecture.md`](../architecture.md)) asks for one table, `llm_calls`, as the retry cache, the cost ledger and the source of recorded answers for evaluations.

## Decision

- **The adapter** is `providers/claude.py` (`ClaudeLLM`). It uses the official Anthropic SDK and `client.beta.messages.parse` with the Pydantic schema of the caller. The API constrains the output to the JSON schema, and the SDK validates it. The call has no tools. Effort is `low` (`UNSHELVE_LLM_EFFORT`); the visible answer gets 4,000 more tokens of room for the thinking that Claude Opus 5.5 and Claude Haiku 5.5 always do.
- **Refusals:** on Claude Opus 5.5 and Claude Sonnet 5.5 the request carries `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`), so the API can run a refused request again on a different model. A refusal of the whole chain gives no card.
- **The card** is `enrich/card.py`: the schema `Card`, the system prompt and its version (`enrich-1`). The saved text goes between `<saved_text>` tags; a closing tag inside the text is changed. Summary, key points and tags are in English; the title stays in the language of the text. The prompt holds no goal text: scoring against goals is a separate step (M3).
- **The cache and the ledger** is `providers/ledger.py` (`CachedLLM`), in front of each provider. The key is the SHA-256 of the model, the task, the prompt version, the system prompt, the parts and the schema. A stored answer is returned without a call. Each new call is stored with its tokens and its price; an image is stored as its hash only.
- **The daily budget** (`UNSHELVE_LLM_DAILY_BUDGET_USD`, default $1.00) counts the costs of the day in `UNSHELVE_TIMEZONE`. When it is used, a new call fails with a wait until midnight. A stored answer needs no budget.
- **Failures** fall into three groups. A failure that can pass (429, 5xx, network, the budget) waits 2, 8, 32 and 60 minutes, or the wait of the provider (at most one day). A failure that a person must fix (401, 402, 403, 404) waits in the same way, so that the item gets its card after the fix. A failure for this item only (a 400, a refusal, an answer that does not fit) gives an empty card at once. The last attempt also gives an empty card. Each empty card after a failure has an event `enrich_failed` with the reason.
- **The model** is a setting (`UNSHELVE_LLM_MODEL`). The default stays `claude-opus-5-5`, as in the plan. The owner chooses, after a comparison of the models on real saved items.
- **Live calls** start only with `UNSHELVE_PROVIDERS=live` and `ANTHROPIC_API_KEY`. The tests use a pretend API through the real SDK, and reach no real API.

## Consequences

- A rerun of a step makes no second call, and a crash of the worker costs nothing more.
- A new prompt version makes each item a new question. A change of the prompt is a decision with a price: the items that are enriched again are paid again.
- The budget can delay the cards of a large import by days. Batch mode for an import ([issue #70](https://github.com/herman72/unshelve/issues/70)) is half the price, and it fills the same cache.
- An injection can still change the words of a summary. It cannot add fields, call a tool or reach a goal. A card is shown escaped ([note 13](../learn/13-cards-and-digest.md), part 2).
- The price table is in the code. A new price needs a new line there.

## Alternatives considered

- **Free text and parsing by hand:** fragile, and an injection could add text where a field was expected.
- **A tool with `strict: true` instead of structured output:** it gives the same guarantee, but forced tool use returns a 400 on Claude Opus 5.5, so it needs extra prompting. Structured output is the direct way.
- **The cache inside the enrich step:** only that step would profit. In front of the provider, each later task (scoring, the overview answer, evaluations) gets the cache and the ledger.
- **Prompt caching of the API** keeps a long prompt prefix on the server for some minutes. It is no answer cache, and our system prompt is shorter than its minimum length.
