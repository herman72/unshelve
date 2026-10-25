# 0004. Public data is a pushed, static subset

- Status: Accepted
- Date: 2026-10-10

## Context

The user wants some items to be visible and searchable on a personal site, and the rest to stay private. The private data includes more than the items: goal names, relevance reasons, private notes and private tags describe the user's plans.

A public search endpoint on the main database would need rate limiting, abuse protection and a query-level filter that must never have a bug.

## Decision

- A schema `pub` contains one view with an explicit column list. It selects only items with `visibility = 'public'`.
- The publisher job connects with a database role that can read that view and nothing else.
- The publisher writes a static `feed.json` and pushes it to a target (a file, a git repository or object storage). The site reads the feed and searches it in the browser.
- There is no public API.
- Enrichment is goal-agnostic: the model that writes summaries and tags never sees goal text. Tags are public only when `tags.is_public` is set. `note` is private; `public_note` is the only user-written text that can be published.
- Changing visibility shows the exact public record before the user confirms.

### How the publisher uses the role (2026-10-22, M5-1)

The publisher does not log in as `unshelve_publisher`. It switches to the role for one transaction with `SET LOCAL ROLE`, and the role ends with that transaction. The rights are the same as for a login: the feed query can read `pub.items` and nothing else, so a mistake in it is refused by the database. There is no second password to keep, and the role needs no login on the shared database server. Code that wants private data on purpose could switch back with `RESET ROLE`; such code has the main connection anyway. See [note 42](../learn/42-public-feed.md).

### How the preview is made (2026-10-25, M5-2)

The rule "changing visibility shows the exact public record before the user confirms" is kept by one path, not by a copy of the rules ([note 45](../learn/45-publishing-an-item.md)):

- **The preview** makes the item public inside a savepoint, reads its record from `pub.items` as the publisher role, and rolls the savepoint back, with the role. The preview is the record of the feed by construction.
- **Publishing** takes the record that the owner saw. The item becomes public only when the record that it would have now is the same, byte for byte; otherwise the answer is a 409 and nothing changes.
- **The name and the time** of a public item (`public_slug`, `published_at`) are kept for good. Made private and public again, the item has the same name and its first time.
- **Each change** queues one publish job for the owner, which waits 30 seconds, so a series of changes gives one publish.

## Consequences

- A private item cannot be in the feed, because the role that builds the feed cannot read it.
- Public search costs nothing to run and has no abuse surface.
- Public visitors get keyword search over a few hundred curated items, not semantic search or generated answers. That is enough at this size.
- Published data may be cached or indexed by others. Unpublishing removes it from the next feed, not from the internet.

## Alternatives considered

- **Public API with a `WHERE visibility = 'public'` filter.** One missing filter leaks everything, and the endpoint needs protection.
- **Separate public database.** Stronger isolation than needed, with a synchronisation problem.
