# 0006. One async database access style

- Status: Accepted
- Date: 2026-10-10

## Context

The API, the worker and the bot all wait on the network: Telegram, web pages, model providers. Several contributors, some of them coding agents, will write database code in parallel. Mixed sync and async styles, or lazy loads fired from unexpected places, would produce bugs that only appear under load.

## Decision

- SQLAlchemy 2 with the async engine on `postgresql+psycopg`. The same driver serves the synchronous Alembic migrations.
- Relationships are declared with `lazy="raise"`. Related rows are loaded explicitly.
- Sessions use `expire_on_commit=False`.
- One session per HTTP request and one per job.
- No transaction is held open across network I/O. Read, close, call the network, then open a new transaction to write.
- Pure logic is synchronous and has no session. CPU-heavy parsing runs in a thread.

## Consequences

- Queries are visible where they are written. A forgotten eager load fails immediately in tests, not slowly in production.
- A job that calls a model provider cannot hold a row lock for seconds.
- Handlers need a clear split between "decide" (pure), "read" and "write".

## Alternatives considered

- **Synchronous SQLAlchemy with thread pools.** Simpler stack traces, but two concurrency models in one process once the bot and HTTP clients are async.
- **Raw SQL only.** Full control, but more code to write and review for ordinary CRUD.
