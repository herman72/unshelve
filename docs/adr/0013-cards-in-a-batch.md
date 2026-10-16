# 0013. Cards in a batch: the card waits, and a person sends the batch

- Status: Accepted
- Date: 2026-10-16

## Context

An export of a channel can hold thousands of posts. Each post gets a card from the model ([ADR 0011](0011-claude-for-the-cards.md)). The Message Batches API of Anthropic answers the same requests at half the price. It answers within 24 hours, and most batches end within one hour.

Three facts shaped the decision:

- **The worker asks for each card at once.** The fetch step adds the job of the card step, and a worker runs it in seconds. So a card never waits by itself for a batch.
- **The ledger is the cache** ([ADR 0011](0011-claude-for-the-cards.md)). A card step that finds the answer of its question in `llm_calls` asks no model. A batch answer under the same cache key is that answer.
- **The daily budget counts the costs of the day.** A batch of $5 would stop the calls of the rest of the day.

## Decision

- **A setting chooses the mode:** `UNSHELVE_CARDS=live|batch`, `live` by default. In batch mode the cache does not ask the model for a card question without a stored answer. It raises `WaitsForBatch`, and the card step ends: the item stays `fetched`, and no job tries again.
- **A person sends the batch:** `unshelve llm batch send` collects the waiting questions, shows their number and the estimated price, and asks for a yes. Two items with one text have one question. A question has its cache key as its `custom_id`, so its answer goes under that key. The command refuses in live mode, where the worker could also ask for the same card at once.
- **The worker collects the batch:** the job `collect_batch` looks at the batch every 5 minutes, at most 300 times (25 hours). When the batch has ended, it keeps each answer in `llm_calls`, with the batch price and the id of the batch, and adds the job of the card step for each item. That step finds the answer. A question without an answer leaves its item waiting for the next batch.
- **The request of a question in a batch is the request of a call now:** the same model, system prompt, content, effort and JSON schema (`transform_schema` of the SDK). A test compares the two requests, field by field.
- **A batch does not use the daily budget:** a row of the ledger that names a batch does not count. You accepted its price by hand.
- **The tables:** `llm_batches` (one row for each batch, with its state: sending, sent, collected, failed) and `llm_batch_items` (the items whose question is in a batch). A batch that a command stored, but never sent, holds its questions for 10 minutes only.

## Consequences

- The import of a large export costs half, and the cards come within hours. Each new save also waits in batch mode, so batch mode is for an import, not for the days after it.
- The images of an import are read in the fetch step, at once and at the normal price: they are not in a batch.
- A collection can run again after a crash: an answer that is stored already stays, and a collected batch is not collected again.
- The worker had a fault that a job of 300 tries showed: the wait between two tries was computed as `10 s × 2^(tries − 1)` before the cap, and that does not fit in a `timedelta`. It is computed in seconds now.

## Alternatives

- **The daily budget as the wait.** With a budget of $0, each card waits until midnight, and a batch could ask for them first. But the jobs try again each night, and a new save waits too. A mode says what it means.
- **A batch every hour, by itself.** The cheapest for each card, but each card waits up to an hour, and the price of a batch is never shown before it is paid.
- **`unshelve import --batch`.** The cards of an import wait in the fetch step and the card step. But the import ends before the fetch step has read the pages, so the command cannot know when to send.
