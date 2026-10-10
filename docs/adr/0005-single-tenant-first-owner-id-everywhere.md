# 0005. Single tenant first, `owner_id` everywhere

- Status: Accepted
- Date: 2026-10-10

## Context

Version 1 serves one person on a self-hosted instance. A hosted, multi-user version is possible later. Building tenancy, onboarding, quotas and billing now would delay the personal tool and could be wasted.

Retrofitting an owner column onto every table and every query later is expensive and error-prone.

## Decision

- Every user-owned table has an `owner_id` foreign key from the first migration, and every unique key includes it where uniqueness is per user.
- Version 1 allows exactly one owner: the Telegram user id in the configuration. The bot ignores everyone else.
- Nothing else of multi-tenancy is built: no sign-up, no quotas, no billing, no row-level security.

## Consequences

- The cost today is one column and one filter per query.
- A later multi-user version adds isolation (for example row-level security) on top of a schema that is already shaped for it.
- Jobs and embedding sets are instance-wide, so they carry no owner or an optional one.

## Alternatives considered

- **No owner column.** Simplest today, a schema rewrite later.
- **Full multi-tenancy now.** Solves a problem the product does not have yet.
