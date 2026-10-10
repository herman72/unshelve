# 0001. Python API and Next.js UI in one repository

- Status: Accepted
- Date: 2026-10-10

## Context

The hard parts of the product are a data pipeline, retrieval and evaluation. The visible part is a web UI and a search component on a Next.js site. The author reads Python fluently and must be able to review every change.

## Decision

- The core is Python: FastAPI, a worker and a Telegram bot, built from one package and one container image.
- The web app is Next.js with TypeScript.
- Both live in one repository: `apps/api`, `apps/web`, `packages/api-client`.
- The API contract is the committed `openapi.json`. TypeScript types are generated from it, and CI fails when the file is stale.

## Consequences

- Two toolchains (uv and pnpm) and two images.
- A change to the API and its UI can be one pull request.
- The two sides cannot drift without a failing check.

## Alternatives considered

- **All Python with server-rendered templates.** One language and one container, but a weaker UI and no shared code with the Next.js site.
- **All TypeScript.** One deployable, but the retrieval and evaluation code would leave the ecosystem and the language the author knows best.
