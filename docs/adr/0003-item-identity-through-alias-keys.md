# 0003. Items are identified through alias keys

- Status: Accepted
- Date: 2026-10-10

## Context

The same thing is often saved more than once: the same link twice, a forward of a post that was also saved as a link, a URL that redirects to a canonical one. Many items have no URL at all: screenshots, plain notes, albums. A unique constraint on `canonical_url` cannot express any of this.

There are two different questions:

1. Did we already receive this Telegram message? (capture identity)
2. Is this the same thing as an item we already have? (item identity)

## Decision

- **Captures** are unique on `(owner_id, chat_id, message_id, edit_seq)`. The bot and the export importer emit the same `CaptureEnvelope`, so a message seen by both produces one row.
- **Items** are found through an alias table, `item_keys`, with typed keys: `url:<canonical url>`, `tg_file:<file id>`, `tg_album:<chat>:<media group>`, `tg_fwd:<chat>:<message>`, `text:<hash>`.
- One capture resolves to exactly one item. The first URL is the primary one. Other links are kept in `extra_links`.
- Keys can be added later. When a fetch discovers the final URL after redirects, that URL becomes another key. If the new key already belongs to another item, the newer item is marked with `duplicate_of_id`.
- A repeated save does not create an item. It raises `capture_count` and updates `last_captured_at`. That is a signal of interest and is used for ranking.
- Album parts arrive as separate messages. They are grouped by one delayed `ingest_album` job, not by an in-memory timer.

## Consequences

- Deduplication works for items without URLs.
- URL canonicalisation is a pure, versioned function with a table of test cases. A new version can re-key old items.
- Merging duplicates is a soft link (`duplicate_of_id`), so no data is destroyed by a wrong match.

## Notes from the implementation (2026-10-14, package M1-3)

The package that built this decision made these points exact. [Learning note 12](../learn/12-resolver.md) explains each one.

- **Which keys a save gets.** The first link, each file that has a Telegram id, the album, and the source of a forward when Telegram gives its chat and its message. The text is a key only for a save without a link and without a file.
- **A save that joins an item** raises `capture_count` by one. The messages of one album are one save.
- **A save with keys of two items** shows that the two items are one thing. The older item stays and gets the saves of the newer one. The newer item gets `duplicate_of_id`. A search by key follows this link to its end.
- **Two workers at the same moment.** Before the resolver looks for a key, it takes a Postgres advisory lock with the name of the key, for the time of its transaction. The primary key of `item_keys` stays the last guard.
- **The canonical form is for identity.** `items.canonical_url` is always `https` and has no tracking parameters. The address as it arrived stays in `captures.urls`, and the fetch step can use it.
- **The rules have a version** (`canonical.VERSION`), and `unshelve rekey` makes the keys of each item again from its stored messages.
- **Not built in M1-3:** the key for the final address after a redirect (it needs the fetch step of M2), and the effect of an edit of a post (issue #60).

## Alternatives considered

- **Unique `canonical_url`.** Fails for items without URLs and changes meaning when a redirect is discovered.
- **Content hashing only.** Misses the same article under two URLs with different page chrome.
