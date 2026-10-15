# 16. The fetch step: the best reader, a second try, and the saved text

> Pull request: [#69](https://github.com/herman72/unshelve/pull/69) · Code: [`pipeline/items.py`](../../apps/api/src/unshelve/pipeline/items.py) (`fetch_item`), [`extract/registry.py`](../../apps/api/src/unshelve/extract/registry.py), [`extract/saved_text.py`](../../apps/api/src/unshelve/extract/saved_text.py) · Tests: [`tests/pipeline/test_fetch.py`](../../apps/api/tests/pipeline/test_fetch.py), [`tests/extract/test_registry.py`](../../apps/api/tests/extract/test_registry.py)

## In one minute

Until now, the first step of the pipeline gave each item the text of the saved message. It never opened a link. Now it reads the page of the link, with the fetcher of [note 14](14-fetcher-and-guard.md) and the extractor of [note 15](15-article-extractor.md).

A real run with five saved links:

| Saved link | Result |
|---|---|
| an article of Martin Fowler | the article: 6,241 words, title "Event Sourcing" |
| a page of the Persian Wikipedia | the article, language `fa` |
| a page of Wikipedia that does not exist | the saved text, partial. Reason: 404 |
| a name that DNS does not know | the saved text, partial. Reason: `no_such_host` |
| x.com | the saved text, partial. Reason: only 106 characters of text |

Each of the five items was ready after less than 3 seconds. An item that has only the saved text is still enriched: the model gets the saved text.

## 1. Who reads a source?

Each extractor gives a score to the source. The highest score wins ([`extract/registry.py`](../../apps/api/src/unshelve/extract/registry.py)):

| Source | Article extractor | Saved text | Winner |
|---|---|---|---|
| `https://blog.example.com/queues` | 10 | 1 | article |
| `https://example.com/paper.pdf` | 0 | 1 | saved text: no reader for a PDF yet |
| a note without a link | 0 | 1 | saved text |
| a photo without a caption | 0 | 0 | saved text: the last resort |

The new extractor "saved text" replaces the old fake `EchoExtractor`. The pipeline no longer imports code from the test helpers. A new extractor joins with one line in the list `EXTRACTORS`.

## 2. The link is opened as you saved it

The item has a canonical address: `https://blog.example.com/2026/queues`. That form is made for identity ([note 12](12-resolver.md)): it drops `www.`, the tracking parameters and `http`. A site can answer only on the form that you saved, so the fetch step opens the saved form:

```text
saved      http://www.blog.example.com/2026/queues?utm_source=telegram     ← opened
canonical  https://blog.example.com/2026/queues                            ← for identity
```

Only a saved link without `http` or `https` is opened in its canonical form, because it cannot be opened as it is.

## 3. A failure that can pass, and a failure that cannot

| What happened | Example | What the step does |
|---|---|---|
| The page was read | an article | the text of the page |
| A failure that cannot pass | 404, 410, `no_such_host`, `blocked_address`, `too_large` | the saved text at once |
| A page that is no article | a frame for JavaScript | the saved text at once |
| A failure that can pass | `timeout`, `network`, 429, 503 | try again later |

"Try again later" uses the queue of [note 4](04-job-queue.md): the step raises `Retry`, and the job waits. The waits are 1, 2, 4 and 8 minutes. A server that sends `Retry-After` gets its own wait, but never more than one hour.

A job has 5 attempts. On the last one, the step stops waiting and keeps the saved text:

```text
12:00  attempt 1   timeout   → next try at 12:01
12:01  attempt 2   timeout   → next try at 12:03
12:03  attempt 3   timeout   → next try at 12:07
12:07  attempt 4   timeout   → next try at 12:15
12:15  attempt 5   timeout   → the saved text: the item is partial, and it goes on
```

Without the rule for the last attempt, the queue would put the job to "dead" after the fifth `Retry`, and the item would wait in "pending" for ever.

**A comparison from daily life:** you phone a shop, and the line is busy. You phone again after one minute, then after two, then after four. After the fifth call you stop, and you use what you already know.

## 4. The log of the item says why

An item with only the saved text gets one row in the event log (`events`, append-only):

```json
{"extractor": "article", "reason": "http_error", "status": 404, "attempt": 1}
```

For a page without an article, the row keeps the words of the extractor:

```json
{"extractor": "article", "reason": "no_text", "detail": "the page has only 106 characters of text"}
```

The web app can show this later: "the page was not found". The log of the worker gets the same fields, and not the link: a link can hold private words in its path or query.

## 5. What "partial" means

`items.partial` is true when the item has a link, but its text is only the saved text. Two cases give it: the page could not be read, or no extractor reads such a link yet (a PDF). A note without a link is never partial.

## A choice that we made

What does the step do with a failure that can pass?

| | Try again in the same job (ours) | Fall back at once | A separate job "fetch again tomorrow" |
|---|---|---|---|
| A short break of the network | the next try reads the page | the item keeps only the saved text | the item is partial until tomorrow |
| Code | the queue already has `Retry` and attempts | none | a new job kind and a plan for it |
| The item is ready | after 15 minutes at most | at once | at once, and changes tomorrow |

Trying again in the same job uses what the queue already has. The rule for the last attempt keeps the worst case short.

## What a senior engineer checks here

- **Can an item wait for ever?** No. The last attempt always gives the saved text. A test makes the first attempt the last one.
- **Can a server make us wait for ever?** No. A `Retry-After` of one day becomes one hour.
- **Is each kind of failure tested?** Yes: 404, 410, `no_such_host`, `blocked_address`, `too_large`, a timeout, a network error, a 429 and a 503 with their waits, and a page without an article.
- **Does a log keep private words?** No. The log has the reason and the code, not the link.
- **Was it tried with the real internet?** Yes: the five links of the table at the start, through the whole pipeline.
- **Can the tests fail?** I broke the new code in 19 places, one at a time. The tests found each of the 19.
- **What is not done:**
  - The final address after redirects is not a key of the item yet ([issue #68](https://github.com/herman72/unshelve/issues/68)).
  - The date of an article is not stored. `items.published_at` is the time of publishing to the public feed, so it cannot hold the date of a page.
  - With the default setting `UNSHELVE_FETCHER=fixtures` the worker has no web: the pretend web answers 404 for each link. Set `UNSHELVE_FETCHER=live` to read real pages.
  - The web app does not show why an item is partial yet (package M2-6).

## Try it yourself

Do this after the pull request is merged.

1. Add this line to `.env`, so that the worker reads real pages:

```text
UNSHELVE_FETCHER=live
```

2. Run `git pull`, and then `make up`.
3. Post an article link in the channel "Unshelve test". After some seconds, the card in the web app has the title of the page.
4. Post a link to a page that does not exist, for example `https://en.wikipedia.org/wiki/This_page_does_not_exist`. Then read the last rows of the event log:

```bash
docker compose exec db psql -U unshelve -c "SELECT item_id, type, payload FROM events ORDER BY id DESC LIMIT 3"
```

You see a row `fetch_failed` with the reason `http_error` and the status 404.

## Exercise (10 minutes)

Remove the rule for the last attempt, and see what happens to the item.

1. Open `apps/api/src/unshelve/pipeline/items.py`, and go to `fetch_item`.
2. Change `ctx.job.attempts < ctx.job.max_attempts` to `ctx.job.attempts <= ctx.job.max_attempts`.
3. Run the tests of the fetch step:

```bash
cd apps/api && uv run pytest tests/pipeline/test_fetch.py -q
```

4. Read the name of the test that fails. Then put the line back with the undo of your editor.

Then answer this question: the test fails on the state of the item. In which state is the item, and in which state is its job?

<details>
<summary>The answer</summary>

One test fails, and 19 pass:

```text
FAILED tests/pipeline/test_fetch.py::test_the_last_attempt_keeps_the_saved_text
E       At index 0 diff: <EnrichState.PENDING: 'pending'> != <EnrichState.FETCHED: 'fetched'>
```

The item is still `pending`. Its job is `dead`, after 1 attempt, with the error `fetch: timeout`.

On the last attempt the step raised `Retry` again. The queue had no attempt left, so it put the job to "dead". No other job will move the item, so the item waits for ever. That is the case that part 3 prevents.

The lesson: a retry needs an end, and the end needs a plan for the thing that waits.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Extractor | The code that reads one kind of source into text |
| Score | The number with which an extractor claims a source. The highest wins. |
| Partial | An item with a link whose text is only the saved text |
| Attempt | One run of a job. A job has 5. |
| Retry-After | A header in which a server says how long to wait before the next try |
| Event log | The table `events`: one row for each thing that happened to an item. No row is ever changed. |

## To read more

- [Retry-After](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Retry-After), MDN Web Docs. It shows the two forms of the header: a number of seconds, and a date.
- [Exponential backoff](https://en.wikipedia.org/wiki/Exponential_backoff), Wikipedia. Read the first part: why the wait doubles after each failure.

Previous: [15. Reading an article](15-article-extractor.md) · Back to the [list of notes](README.md)
