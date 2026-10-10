# 0002. Postgres is the job queue, with leases

- Status: Accepted
- Date: 2026-10-10

## Context

Every saved item goes through several slow, failure-prone steps: fetch, summarise, embed, score. The load is tiny (a few items a day, a few thousand in a backfill) and the system must be easy to self-host.

## Decision

Jobs live in a `jobs` table in the same Postgres database.

- **Claim:** `SELECT … FOR UPDATE SKIP LOCKED`, then mark the row `running` with `locked_by` and `locked_until`.
- **Lease:** a worker extends `locked_until` with a heartbeat. A reaper returns expired jobs to `pending`.
- **Fencing:** `complete` and `fail` succeed only if the caller still holds the lease. A worker that lost its lease cannot overwrite the result of the worker that took over.
- **Retries:** handlers raise `Retry(after)` or `Permanent(reason)`. Any other error backs off exponentially until `max_attempts`, then the job is `dead`.
- **Chaining:** one job per pipeline step. A step enqueues the next step in the same transaction that writes its result.
- **Dedupe:** a partial unique index on `(kind, dedupe_key)` for pending jobs coalesces duplicates.

Delivery is at-least-once. Handlers are idempotent, and exactly-once effects come from domain keys, not from the queue.

## Consequences

- No extra service to run. Enqueueing is transactional with the data it refers to.
- `SKIP LOCKED` alone would lose jobs when a worker dies mid-job. The lease and the reaper are required parts, not extras.
- Throughput is limited by Postgres, which is far above what this product needs.

## Alternatives considered

- **Redis with Celery or RQ.** One more service, and enqueueing is no longer in the same transaction as the write.
- **Procrastinate.** A good Postgres-based library, but it manages its own schema outside our migrations, and the queue is small enough to own and understand completely.
