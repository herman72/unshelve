# Contributing

## Ground rules

- **Small commits.** One logical change per commit, written as a [Conventional Commit](https://www.conventionalcommits.org/): `feat(queue): reap expired leases`.
- **One branch per work package.** Branch names follow `m<milestone>-<n>/<short-name>`, for example `m1-2/export-importer`.
- **Pull requests are rebase-merged**, so the small commits stay visible in a linear history.
- **CI must pass.** Lint, type check, tests and the OpenAPI drift check.
- **No private data in the repository.** Fixtures are synthetic. Exports, roadmaps and `.env` files stay outside git.

## Frozen interfaces

Two areas are changed only by the maintainer, in their own pull requests:

- `apps/api/src/unshelve/contracts/`: Protocols, DTOs, job payloads and the item state machine.
- `apps/api/migrations/`: one linear migration history.

Everything else depends on these. If your change needs a new field or a new method, open an issue first.

## Where things go

| You are adding | It goes in |
|---|---|
| A source type (for example a new site) | `apps/api/src/unshelve/extract/<name>.py` plus recorded fixtures |
| A job handler | `apps/api/src/unshelve/pipeline/` |
| An API endpoint | `apps/api/src/unshelve/api/`, then regenerate `openapi.json` |
| A real provider adapter | `apps/api/src/unshelve/providers/` and it must pass the conformance suite |
| UI | `apps/web/` |

## Rules that keep the system honest

- Only `unshelve.net` opens arbitrary URLs. Extractors receive a `Fetcher`; they never create their own HTTP client.
- Database code is async, loads relationships explicitly and never holds a transaction across a network call. See [ADR 0006](docs/adr/0006-one-async-database-access-style.md).
- Job handlers are idempotent. A job can run twice. See [ADR 0002](docs/adr/0002-postgres-job-queue-with-leases.md).
- Every implementation of a Protocol passes the same conformance tests as its fake.
- Text that came from the internet is untrusted. It is never given to a model that has tools, and it is escaped when rendered.

## Learning notes

A pull request that adds a feature also adds or updates one note in [`docs/learn/`](docs/learn/README.md). The note explains the feature in simple words: an example with real values first, each technical word explained in one sentence, a picture where it helps, one exercise, and one to three links for more reading.

The note is reviewed together with the code. The description of the pull request can then be short and link to the note.

## Pull request description

Each pull request explains:

1. What changed and why.
2. How it was verified (commands and results).
3. One alternative that was considered, and its trade-off.
4. What a reviewer should look at first.
