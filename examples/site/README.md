# Example site

A small Next.js site that searches the public feed of Unshelve, as mattsh.dev will. It shows the fixture feed of [`packages/site-search`](../../packages/site-search) with the component `<FeedSearch>`. [Note 46](../../docs/learn/46-site-search.md) explains it.

```bash
pnpm --filter @unshelve/example-site dev
```

Then open http://localhost:3920. The page `/unsafe-feed` is for the tests: a feed with addresses that must not become links.

## Tests

```bash
pnpm --filter @unshelve/example-site test:e2e
```

The tests build the site for production, start it, and search it in the Google Chrome of the computer, in the time zone of Honolulu.
