# Architecture decision records

Each record states one decision, why it was made, and what it costs. A record is never edited to change the decision. A new record supersedes it.

| # | Decision | Status |
|---|---|---|
| [0001](0001-python-api-nextjs-ui-monorepo.md) | Python API and Next.js UI in one repository | Accepted |
| [0002](0002-postgres-job-queue-with-leases.md) | Postgres is the job queue, with leases | Accepted |
| [0003](0003-item-identity-through-alias-keys.md) | Items are identified through alias keys | Accepted |
| [0004](0004-public-data-is-a-pushed-static-subset.md) | Public data is a pushed, static subset | Accepted |
| [0005](0005-single-tenant-first-owner-id-everywhere.md) | Single tenant first, `owner_id` everywhere | Accepted |
| [0006](0006-one-async-database-access-style.md) | One async database access style | Accepted |
| [0007](0007-embedding-sets-and-exact-search-first.md) | Embedding sets, and exact search first | Accepted |
| [0008](0008-long-polling-and-bot-issued-login-links.md) | Telegram long polling and bot-issued login links | Accepted |
| [0009](0009-capture-rules-from-a-real-bot-and-real-exports.md) | Capture rules from a test with a real bot and real exports | Accepted |
| [0010](0010-one-guarded-way-to-the-internet.md) | One guarded way to the internet | Accepted |
| [0011](0011-claude-for-the-cards.md) | Claude for the cards: structured output, a cache in front, a daily budget | Accepted |
| [0012](0012-voyage-vectors-through-a-client-for-fixed-services.md) | Voyage for the vectors, through a client for fixed services | Accepted |
| [0013](0013-cards-in-a-batch.md) | Cards in a batch: the card waits, and a person sends the batch | Accepted |
| [0014](0014-scores-a-vector-prefilter-then-one-question-for-all-goals.md) | Scores: a vector prefilter, then one question to the model for all goals | Accepted |
| [0015](0015-backups-a-logical-dump-and-a-mirror-of-the-files.md) | Backups: a logical dump from one snapshot, and a mirror of the files | Accepted |

## Template

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Superseded by NNNN
- Date: YYYY-MM-DD

## Context
## Decision
## Consequences
## Alternatives considered
```
