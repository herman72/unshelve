# 36. Three retrievers

> Pull request: [#97](https://github.com/herman72/unshelve/pull/97) · Code: [`search/retrievers.py`](../../apps/api/src/unshelve/search/retrievers.py), [`cli.py`](../../apps/api/src/unshelve/cli.py) (`search retrievers`) · Tests: [`tests/search/test_retrievers.py`](../../apps/api/tests/search/test_retrievers.py), [`tests/search/corpus.py`](../../apps/api/tests/search/corpus.py)

## In one minute

You search your saved items. Each of three *retrievers* returns a list of items, the best first. A retriever is one SQL query. Here are four searches on the made-up items of the tests:

| You type | Full text | Trigram | Why |
|---|---|---|---|
| `queues` | Postgres as a job queue | nothing | "queues" and "queue" have the same stem |
| `postgress` | nothing | Postgres as a job queue | a typo: no word "postgress" exists |
| `کتابهای` | کتابهای پایگاه داده | 1. کتابهای پایگاه داده, 2. معرفي كتاب‌هاي يادگيري ماشين | the second title has a zero-width non-joiner: "کتاب‌های" |
| `search` | nothing | nothing | "research" is a different word |

The third retriever, *vector*, compares meanings. For a question about job queues, it returns the item about queues, then the pgvector item (one piece of its text is about queues), and last the Rust item.

M4-2 joins the three lists into one list (*reciprocal rank fusion*). This note is about the lists.

| Retriever | Reads | Finds |
|---|---|---|
| Full text | the card (title, summary, author, site) and the fetched text | the words, in each English form |
| Trigram | the titles | a typo, a part of a word, Persian with or without a non-joiner |
| Vector | the vectors of the card and of the pieces of the text | the meaning, also in other words |

Each retriever sees only your items, and no merged copy. Try the three lists on your own items:

```bash
docker compose exec api unshelve search retrievers "postgres"
```

With the Voyage embedder, each run makes one embedding of the query: a very small cost, and it shows on the bill of Voyage. With the fake embedder, the vector list is empty until the fake set is the active set.

## 1. Full text: words and their stems

Postgres stores a *tsvector* for each item: the words of the card, each one cut to its *stem* (its base form), with its place and a weight:

```text
Postgres as a job queue  →  'job':4A 'postgr':1A 'queue':5A
```

The weight says where the word is: A for the title, B for the summary, C for the author and the site. "as" and "a" are missing: they are *stop words*, too common to help.

The query becomes a *tsquery* in the same way: `queues` becomes `'queue'`, so it finds the item. A query can use the syntax of a web search: `"job queue" -vector` becomes `'job' <-> 'queue' & !'vector'`: the phrase "job queue", and not the word "vector".

The rank (`ts_rank`) follows the weight: "Postgres" in a title gives 0.61, and in a summary 0.24. The card decides the order. The fetched text has its own tsvector, and its rank only orders the items whose cards rank the same: two items with the same title, or items found only in their text. Postgres divides the rank of a text by the logarithm of its length, so a long page does not win only by its size.

Persian and Arabic keyboards type some letters differently: "ي" or "ی", "ك" or "ک". The function `u_norm()` makes them one form, in the stored text and in the query ([note 3](03-database-schema.md)). So `پايگاه` with the Arabic yeh finds `پایگاه` with the Persian yeh.

## 2. Trigram: pieces of three letters

A *trigram* is a piece of three letters of a word. Postgres adds two spaces before each word and one after it:

```text
queue  →  "  q", " qu", "que", "ueu", "eue", "ue "
```

Two spellings with many common trigrams are close. A typo changes only a few trigrams, so `postgress` is close to `postgres`. Full text sees two different words, and finds nothing.

The Persian case shows why this retriever exists. The query `کتابهای` (joined) has 8 trigrams. The title `کتاب‌های` has a zero-width non-joiner, which `u_norm()` turns into a space: `کتاب های`, with 9 trigrams. They share 6:

```text
similarity = shared ÷ all the different trigrams = 6 ÷ (8 + 9 − 6) = 6 ÷ 11 = 0.55
```

Full text cannot see this: for it, `کتابهای` and `کتاب های` are different words.

The retriever reads only the titles: they are short, and an index (`ix_items_title_trgm`) serves the query.

## 3. Vector: the closest piece

An item has more than one vector: one for its card, and one for each piece of a long text. The distance of an item is the distance of its closest vector. So a long page about many things is found by the one part that matches.

Two rules keep the vectors comparable:

- **One model.** A vector of one model means nothing next to a vector of another model. Search reads only the active embedding set, and only when the embedder of the query belongs to that set (`searchable_set`). Else the vector list is empty.
- **No transaction during the call.** The embedder of the query can be a slow network call. The query vector is made first, with no transaction open, and then the SQL runs.

The vector retriever compares the query with each of your vectors: an *exact* search. With some thousand items it is fast. An index for an approximate search comes only after a measurement (the plan of the project, ADR 0007).

It always returns the nearest items, also when none is near. The fusion of M4-2 and the evaluation of M4-5 decide what is too far.

**A comparison from daily life:** you look for a book in three ways. The index at the back of the book finds an exact word (full text). Your eye finds a title on the shelf, also when the title is spelled a little differently (trigram). A librarian finds the book about your question, also when it has none of your words (vector).

## A choice that we made

pg_trgm has two measures of how close a query is to a title. I measured both on wanted and unwanted matches:

| Query and title | `word_similarity` | `strict_word_similarity` | Wanted? |
|---|---|---|---|
| `کتابهای` and `کتاب‌های …` | 0.55 | 0.55 | yes |
| `postgress` and `Postgres as a job queue` | 0.80 | 0.73 | yes |
| `pgvec` and `How pgvector stores vectors` | 0.83 | 0.50 | yes |
| `search` and `A research agenda` | 0.71 | 0.45 | no |
| `دانش` (knowledge) and `دانشگاه تهران` (university) | 0.80 | 0.44 | no |
| `gpu` and `Notes on GPT models` | 0.50 | 0.33 | no |

`word_similarity` also matches a part of a longer word, so "search" finds "research" at each threshold. `strict_word_similarity` compares whole words: with the threshold 0.5 (its default), each wanted match passes and each unwanted match fails. The operator `<<%` uses this measure and the index.

## What a senior engineer checks here

- **Does each query use its index?** Yes: `EXPLAIN` shows `ix_items_fts` for full text and `ix_items_title_trgm` for trigram (through `%>>`, the same operator from the other side).
- **Can a query break the SQL?** No. The query is a bound parameter, and `websearch_to_tsquery` accepts any text: a query of stop words only finds nothing, with no error.
- **Are the Persian spellings equal on both sides?** Yes: tests type the query with Arabic letters and the title with Persian letters, and the other way round.
- **Can vectors of two models meet?** No: only the active set, only for the same model, and only vectors of the same length (pgvector refuses others).
- **Can the tests fail?** I broke the code in 24 places, one at a time. Before the run, the list of mistakes against the tests showed three gaps: no query with Arabic letters, no test of the rank of a text, and no test that the closest piece counts, not the farthest. Three tests close them. The first run found 22 of 24:
  - One miss showed a number that changed nothing. The rank of a text was multiplied by 0.5, and 5.0 gave the same order. The rank of a text is about 0.02 and the rank of a summary 0.24, so any weight below 10 gave the same result. The order is now explicit: the card first, then the text.
  - The other miss cannot fail a test. The vector query checks the owner two times, for the items and for the vectors. The second check lets the index of the vectors serve the query.

  Now 23 of 24.
- **What is not done:**
  - No fusion yet: that is M4-2, and you write it.
  - No search API and no snippets: M4-3.
  - No limit of distance for the vector retriever: M4-5 measures one.

## Exercise (15 minutes)

1. Open `apps/api/src/unshelve/search/retrievers.py`, and go to `trigram`.
2. Change the operator `"<<%"` to `"<%"`. It uses `word_similarity` and its default threshold, 0.6.
3. Run the trigram tests in `apps/api`:

```bash
uv run pytest tests/search/test_retrievers.py -q -k trigram
```

Which tests fail? Read the table of the choice above, and say why each one fails.

<details>
<summary>The answer</summary>

```text
FAILED tests/search/test_retrievers.py::test_trigram_meets_a_persian_word_with_and_without_the_non_joiner
E       assert [25] == [25, 24]
FAILED tests/search/test_retrievers.py::test_trigram_does_not_take_a_part_of_a_longer_word
E       assert [46] == []
```

The numbers are item ids of the test database; yours can be different.

One change breaks the rule in two directions:

- **A wanted match is lost.** The Persian title with the zero-width non-joiner has a similarity of 0.55, below the new threshold of 0.6. The search for `کتابهای` finds one title, not two.
- **An unwanted match comes in.** `word_similarity` also matches a part of a longer word: `search` and `research` have 0.71, above 0.6. The search for `search` finds the research item.

A higher threshold cannot fix both: the wanted match (0.55) is lower than the unwanted one (0.71). Only a different measure separates them.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Retriever | One way to find items: a query that returns ranked item ids |
| tsvector | The words of a text as stems, with their places and weights |
| Stem | The base form of a word: "queues" and "queue" have the stem "queue" |
| Stop word | A word too common to help a search, such as "the" |
| Trigram | A piece of three letters of a word |
| Strict word similarity | How close the words of a query are to whole words of a text, from 0 to 1 |
| Exact search | A search that compares the query with each vector |

## To read more

- [pg_trgm](https://www.postgresql.org/docs/current/pgtrgm.html#PGTRGM-FUNCS-OPS), PostgreSQL documentation. Read the table of functions and operators: `word_similarity`, `strict_word_similarity` and their operators.
- [Ranking search results](https://www.postgresql.org/docs/current/textsearch-controls.html#TEXTSEARCH-RANKING), PostgreSQL documentation. Read about the weights A to D and the normalisation of `ts_rank`.

Previous: [35. Sending the digest](35-sending-the-digest.md) · Next: [37. Fusion and metrics](37-fusion-and-metrics.md) · Back to the [list of notes](README.md)
