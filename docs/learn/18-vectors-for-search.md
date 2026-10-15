# 18. Vectors for search: pieces, sets and Voyage

> Pull request: [#73](https://github.com/herman72/unshelve/pull/73) · Decision record: [ADR 0012](../adr/0012-voyage-vectors-through-a-client-for-fixed-services.md) · Code: [`embed/chunks.py`](../../apps/api/src/unshelve/embed/chunks.py), [`embed/sets.py`](../../apps/api/src/unshelve/embed/sets.py), [`providers/voyage.py`](../../apps/api/src/unshelve/providers/voyage.py), [`net/service.py`](../../apps/api/src/unshelve/net/service.py), [`pipeline/items.py`](../../apps/api/src/unshelve/pipeline/items.py) (`embed_item`) · Tests: [`tests/embed/`](../../apps/api/tests/embed/), [`tests/pipeline/test_embed.py`](../../apps/api/tests/pipeline/test_embed.py), [`tests/providers/test_voyage.py`](../../apps/api/tests/providers/test_voyage.py), [`tests/net/test_service.py`](../../apps/api/tests/net/test_service.py)

## In one minute

Martin Fowler's article "Event Sourcing" has 37,910 characters. After this pull request, the item of that article has 31 vectors:

- one vector for its card (the title, the summary and the key points);
- 30 vectors, one for each piece of its text.

A **vector** (also "embedding") is a list of numbers that stands for the meaning of a text. Voyage gives 1,024 numbers for each text. Two texts with a similar meaning get vectors that point in a similar direction. Search (milestone M4) will compare the vector of your question with these vectors.

Four public pages, cut by the new code:

| Page | Characters | Pieces | Characters in one piece |
|---|---|---|---|
| Martin Fowler, "Event Sourcing" | 37,910 | 30 | 913 to 1,487 |
| English Wikipedia, "Exponential backoff" | 20,275 | 18 | 447 to 1,497 |
| Persian Wikipedia, "Artificial neural network" | 11,400 | 10 | 354 to 1,445 |
| Persian Wikipedia, "Artificial intelligence" | 102,043 | 35 | 1,821 to 3,190 |
| A note of 300 characters | 300 | 0 | the card is enough |

The real vectors come from Voyage AI when `.env` has `UNSHELVE_EMBEDDER=voyage`. Without it, the fake embedder makes them, as before.

## 1. A vector is a direction of meaning

Here is a small example with 2 numbers instead of 1,024. The numbers are made up, to show the idea:

```text
"a job queue in Postgres"             (0.95, 0.31)
"SKIP LOCKED lets workers share"      (0.90, 0.44)   close to the first one
"banana bread with walnuts"           (0.10, 0.99)   far from both
```

The closeness of two vectors is their **cosine similarity**: 1 is the same direction, 0 is no relation. The first two texts have a cosine similarity of 0.99. The first and the third have 0.40.

A good model gives close vectors also to texts in two languages: "صف کارها در پستگرس" (Persian for "a job queue in Postgres") is close to the English text about SKIP LOCKED. `unshelve embeddings check` measures exactly this, with a real call. A real run on 15 October 2026 printed:

```text
voyage voyage-4-large 1024: vectors of 1024 numbers
A Persian question and an English text on its topic: 0.57
The same question and a text on another topic: 0.00
```

**A comparison from daily life:** a library puts books on the same topic on the same shelf. The vector is the place of a text on a very large map of shelves.

## 2. Why one item has many vectors

One vector for a whole article is an average of everything in it. A question about one paragraph is far from that average.

The second piece of Fowler's article is about a ship that sends its position:

```text
piece 0   characters 0 to 1,324       "# Event Sourcing ... Capture all changes to an application state ..."
piece 1   characters 1,326 to 2,587   "Let's consider a simple example to do with shipping notifications ..."
```

The question "an example with ships and ports" is close to piece 1. It is far from the card, because the card says "event sourcing", not "ships". So the item gets a card vector and one vector for each piece, and search finds the item through the piece.

## 3. How a text is cut into pieces

The rules are in [`embed/chunks.py`](../../apps/api/src/unshelve/embed/chunks.py):

| Rule | Value |
|---|---|
| A text with no piece | at most 1,000 characters: its card is enough |
| The largest piece | 1,500 characters |
| Where a piece ends | after a paragraph; else after a sentence or a line; else after a word |
| A word that is longer than a piece | cut inside the word (a long link, for example) |
| The most pieces for one item | 40 |
| A text that needs more than 40 pieces | longer pieces, up to 4,000 characters |
| A text that needs more than 40 pieces of 4,000 | its end gets no piece; full-text search still reads it |

```text
the text      | paragraph 1 | paragraph 2 | paragraph 3 ............................ |
                \_______ piece 0 _______/  \____ piece 1 ____/\____ piece 2 ____/
                  two whole paragraphs       a long paragraph: cut after a sentence
```

Each piece keeps its place in the text (`char_start` and `char_end`), so a later search result can show the part of the article that matched.

The Persian page "Artificial intelligence" has 102,043 characters. With pieces of 1,500 characters, it would need about 70. So the pieces grow: the 35 pieces have 1,821 to 3,190 characters, and the whole text has vectors.

A Persian sentence can end with "؟", the Persian question mark. A test checks that a piece ends there. Another test checks that a word with a zero-width non-joiner, such as "کتاب‌ها" ("books"), is never cut: the zero-width non-joiner is part of the word, not a space.

## 4. Characters, not tokens

A **token** is the unit that a model counts: a part of a word. The model of Voyage reads at most 32,000 tokens of one text. The limits of the pieces count characters, because the tokenizer of Voyage is not in this project.

Is that safe? In the comparison of the models (note 17), Persian had 1.4 to 1.6 characters for each token, and English 2.6 to 3.4. So:

| Piece | English | Persian |
|---|---|---|
| 1,500 characters | about 500 tokens | about 1,000 tokens |
| 4,000 characters | about 1,300 tokens | about 2,900 tokens |

Both are far below 32,000. The cost is that a Persian piece of 1,500 characters holds fewer words than an English one. For search, that difference is small.

## 5. The embed step: a second run costs nothing

The step sends all new texts of one item in one request. Before that, it compares the texts with the vectors that the item has already:

| What happened | Request to Voyage | What the step writes |
|---|---|---|
| A new item with a long text | 1 request: the card and 30 pieces | 31 vectors |
| The same step a second time | none | nothing |
| A new summary (a new card) | 1 request: the card only | 1 vector; the pieces stay |
| A shorter text now | none, if the card did not change | the pieces of the old text are deleted |

A test runs the step two times and counts the requests: one. A second test changes the summary and checks that only the card goes to the embedder.

One more check happens just before the write. The step reads the item, asks Voyage, and waits for the answer. If the item gets a new card during that time, the answer is for the old card. So the write happens only if the card and the text of the item are still the ones that the step read. A test changes the summary while a slow pretend embedder waits, and checks that no vector of the old card is written.

When Voyage fails, the step uses the three groups of [note 17](17-model-card.md), part 5: it can pass (429, 5xx, no connection), a person must fix it (401: the key), or this request cannot work (400). After the last attempt, the item is ready without its new vectors, and an event `embed_failed` says why. A vector of an old text is removed: it is worse than none, because a search would find the item for words that it does not have now.

## 6. Embedding sets: a new model without a broken search

The vectors of two models cannot be compared. A vector of the fake embedder (64 numbers) and a vector of Voyage (1,024 numbers) are in two different spaces. So each model has its own **embedding set** ([ADR 0007](../adr/0007-embedding-sets-and-exact-search-first.md)), and search reads only one set: the **active** one.

Your instance has one set now: the fake one, active. Here is the change to Voyage, step by step:

```text
$ unshelve embeddings status
Embedder: voyage voyage-4-large 1024, no vector yet
Ready items: 18
Set 1: fake hashed-bow-64 64, active, 18 of 18 items, 3 pieces
Next: unshelve embeddings backfill

$ unshelve embeddings backfill
Added 18 embed jobs for set 2 (voyage voyage-4-large 1024).
The worker runs them. `unshelve embeddings status` shows the progress.

$ unshelve embeddings activate 2
Search now reads set 2.
```

(The numbers are an example. The output has exactly this form; the tests check it.)

A **backfill** is the work that gives the old items their vectors in the new set. Until the backfill is complete, `activate` refuses, and it names the number of items that have no vector yet. `--force` activates anyway.

**A comparison from daily life:** a library makes a new catalog. Readers use the old catalog while the librarians write the new one. When the new catalog is complete, the library puts it on the desk.

## 7. One way out, also for an API

[Note 14](14-fetcher-and-guard.md) made one rule: only the package `unshelve.net` opens a connection. Voyage is an API on the internet, so its request also goes through `unshelve.net`, but not through the fetcher. A new class, `ServiceClient`, talks to one fixed service:

- its address is in the code, and it must be https;
- a request goes only to a path, such as `/v1/embeddings`, never to a whole address;
- it follows no redirect;
- it reads at most a set number of bytes, counted after decompression too;
- it stops after a set time.

A test sends the path `https://other.example/v1/x` and checks that no request leaves.

Voyage AI is part of MongoDB now. A key of the Voyage platform starts with `pa-` and goes to `api.voyageai.com`. A model key of MongoDB Atlas starts with `al-` and goes to `ai.mongodb.com`. The two addresses take the same request, and the adapter chooses by the start of the key, as the official client does.

Why not the official SDK of Voyage? It would add 24 packages that the project does not have now, for one request. ADR 0012 lists the other choices.

## A choice that we made

How do we cut a text?

| | Our pieces | Fixed length with overlap | One vector for each sentence |
|---|---|---|---|
| Where a piece starts and ends | after a paragraph or a sentence | at a fixed number of characters | at each sentence |
| Vectors for Fowler's article | 31 | about 31 (pieces of 1,500 characters, 15 % overlap) | several hundred |
| A sentence that a cut splits | almost never | kept whole in the next piece, thanks to the overlap | never |
| Our case | a piece is a part of the text that a person would quote | a piece can start in the middle of a sentence | each vector has too little context, and the item has too many vectors |

## What a senior engineer checks here

- **Can a second run cost money?** No. A test runs the step two times: one request.
- **Can a vector of an old text stay?** No. A test changes the card during the request; a second test makes Voyage fail after a change.
- **Can a new model break search?** No. It gets its own set, and search reads the active set until you switch.
- **Can a request leave the host of the service?** No. A test tries a whole address and a path that starts with `//`.
- **Can a test reach Voyage, or read your `.env`?** No. The live tests run only with `-m live`. My review found that the tests read the `.env` of the developer: with the settings of your `.env` (a live fetcher, the Haiku model), two tests of the services failed. Now no test reads a `.env` file.
- **Is a real model a pure function?** No. A model on a GPU gives the same text numbers that differ a little: a cosine of 0.998 or more, from call to call and between a batch and a single text. The first live run failed two tests that asked for 0.999. The tests now ask for "nearer than 0.99, and the nearest to its own text"; two different short texts were at 0.79. The embed step keeps a stored vector, so this noise never changes your vectors.
- **Can the tests fail?** I broke the new code in 79 places, one at a time. The first run found 68. For each of the other 11, I added a test: for example, a proxy in the environment, a Persian question mark, and an item that goes back to the start during the request. Now the tests find each of the 79.
- **What is not done:**
  - The live tests passed on 15 October 2026 (8 of 8, after the change of the two limits above). Your own items are not embedded by Voyage yet.
  - Where do the texts go? Voyage can keep them for training until you opt out (part "Try it yourself", step 4).
  - Search itself (M4) does not read the vectors yet.
  - A title that you change by hand (M2-6) must add an embed job, or the card vector stays old.
  - An old set keeps its vectors; there is no command to remove them yet.

## Try it yourself

Do this after the pull request is merged. Steps 3 and 4 make real calls to Voyage: fewer than 100 tokens, then the tokens of your items. The first 200 million tokens of an account are free.

1. Run `git pull`, and then `make up`.
2. Look at the sets:

```bash
docker compose exec worker unshelve embeddings status
```

3. Add `UNSHELVE_EMBEDDER=voyage` to `.env` (your `VOYAGE_API_KEY` is there already). Run `make up` again. Then make one test call:

```bash
docker compose exec worker unshelve embeddings check
```

You see the length of a vector, and two numbers: the Persian question and its English text should be closer than the question and the text about bread.

4. Before this step, open the dashboard of Voyage and opt out of training. By default, Voyage can keep the texts that it gets and use them to train its models; after the opt-out it keeps them for zero days. The opt-out needs a payment method, and it cannot be undone in the dashboard. Step 3 sends only made-up sentences, so it can come first. Then give your items their vectors:

```bash
docker compose exec worker unshelve embeddings backfill
```

5. Run `status` again until each ready item has its vector. Then switch to the set that `backfill` named. Its number can be higher than 2 on your instance; use the number that the command printed:

```bash
docker compose exec worker unshelve embeddings activate 2
```

## Exercise (15 minutes)

Remove the check just before the write, and see which test finds it.

1. Open `apps/api/src/unshelve/pipeline/items.py`, and go to `_still_ready`.
2. In its `where`, remove `unchanged`.
3. Run the tests of the step:

```bash
cd apps/api && uv run pytest tests/pipeline/test_embed.py -q
```

4. Read the names of the tests that fail. Then put the line back with the undo of your editor.

Then answer: what would you see in search, if this check were not there?

<details>
<summary>The answer</summary>

Two tests fail:

```text
FAILED tests/pipeline/test_embed.py::test_an_item_that_changed_during_the_call_gets_no_vectors_of_its_old_text[card]
FAILED tests/pipeline/test_embed.py::test_an_item_that_changed_during_the_call_gets_no_vectors_of_its_old_text[text]
```

The third case of the same test, `[state]`, still passes: the item went back to the start, and the step also checks the state.

Without the check, a slow request writes the vectors of the old card after the item has a new card. Search would then find the item for the words of its old summary, until the card changes again. Nothing would show an error: the item looks ready, and its vectors look complete.

The lesson: a step that waits for a slow service must check, before it writes, that its input is still the input of now.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Vector (embedding) | A list of numbers that stands for the meaning of a text |
| Cosine similarity | How close the directions of two vectors are: 1 is the same, 0 is no relation |
| Piece (chunk) | A part of a long text that gets its own vector |
| Card vector | The vector of the title, the summary and the key points of an item |
| Embedding set | All vectors of one model. Only vectors of one set are compared. |
| Active set | The set that search reads |
| Backfill | The work that gives old items their vectors in a new set |
| Token | The unit that a model counts: a part of a word |
| Input type | Tells Voyage if a text is a document or a question |

## To read more

- [Text embeddings](https://docs.voyageai.com/docs/embeddings), Voyage AI documentation. Read the table of the models and the part about `input_type`: why a question and a document get different instructions.
- [pgvector](https://github.com/pgvector/pgvector), the README. Read the part "Querying": the operator `<=>` gives the cosine distance that search will use in M4.

Previous: [17. Asking the model](17-model-card.md) · Next: [19. Reading a YouTube video](19-youtube-video.md) · Back to the [list of notes](README.md)
