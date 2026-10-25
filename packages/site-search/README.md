# @unshelve/site-search

Search the public feed of Unshelve in a browser, with the text normalisation of the server. A site needs no server for its search: it reads `feed.json` and makes the index itself. [Note 46](../../docs/learn/46-site-search.md) explains how it works.

```ts
import { createFeedSearch, parseFeed } from "@unshelve/site-search";

const feed = parseFeed(await (await fetch("/feed.json")).json());
const index = createFeedSearch(feed);

index.search("صف كارها"); // typed with an Arabic kaf: finds "صف کارها در پستگرس"
```

## What is here

| File | What it is | Where it comes from |
|---|---|---|
| `src/normalize.ts` | `uNorm()`: the text as `u_norm()` of the server makes it | by hand |
| `src/u-norm-table.ts` | each character that `u_norm()` changes, as runs | `make site-search`, from the database |
| `src/feed.ts` | the types of the feed, and `parseFeed()` | by hand; a test compares it with the schema |
| `src/search.ts` | `createFeedSearch()`: an index made with [MiniSearch](https://github.com/lucaong/minisearch) | by hand |
| `schema/feed-v1.schema.json` | the JSON Schema of version 1 of the feed | `make site-search`, from `apps/api/src/unshelve/publish/feed_schema.py` |
| `fixtures/feed.json` | a feed that the server wrote, for the tests | `make site-search`, from the code that writes the real feed |
| `test/u-norm-cases.json` | cases that the database and `uNorm()` must both pass | by hand |

## Tests

```bash
pnpm --filter @unshelve/site-search test
```

Node runs the tests on the `.ts` files themselves, so it must be Node 22.18 or newer. That is also why the files import each other with the `.ts` extension.

## When a test fails

- **`... is not what the server gives now`** (a test of `apps/api`): the schema or the feed changed in `apps/api`. If the change is right, run `make site-search` and commit the files that it writes.
- **`u_norm() gives another result for N characters`** (a test of `apps/api`): the database answers differently now, usually because its image has a new C library. Run `make site-search`. The indexes of each database that uses `u_norm()` are stale too: see [note 46](../../docs/learn/46-site-search.md).
- **A test of this package**: `uNorm()` or the search does not do what the server does. The name of the test says which rule.
