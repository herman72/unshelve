# 0007. Embedding sets, and exact search first

- Status: Accepted
- Date: 2026-10-10

## Context

Saved content is a mix of English and Persian. Public benchmarks do not name a clear best embedding model for that mix, so the model will be chosen by measurement on the user's own data, and it will probably change at least once.

A `vector(1024)` column fixes the dimension in the schema. Changing the model would then need a migration and a period where search is broken.

The corpus is small: thousands of items, not millions.

## Decision

- An `embedding_sets` table describes each model in use: provider, model, dimension, distance, prefixes. Exactly one set is active.
- `chunks.embedding` is an untyped `vector` column. Each row stores its `embedding_set_id` and `dim`, with `CHECK (vector_dims(embedding) = dim)`.
- Each item has one `card` vector (title, summary, key points). Long text also gets `body` chunks, at most 40 per item.
- Changing the model: create a new set, backfill it in the background, then switch the active set. Search always filters on the active set.
- Search uses exact nearest-neighbour scans. An approximate index (HNSW) is added only after a benchmark shows it is needed, and that decision gets its own record.
- Retrievers return ranked item ids. Fusion happens at item level.

## Consequences

- Model changes are data operations with no downtime.
- Exact search has perfect recall and no tuning parameters. At a few thousand rows it takes milliseconds.
- An untyped column cannot have a plain HNSW index. A later index must be a partial expression index per set.

## Alternatives considered

- **`vector(N)` with an HNSW index from day one.** Faster at a scale the product does not have, at the cost of rigid dimensions and approximate recall on filtered queries.
- **A dedicated vector database.** Another service to run for a few thousand vectors.
