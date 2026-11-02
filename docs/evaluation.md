# Evaluation report

This page gives the measured quality of Unshelve, how each number was measured, and what is not measured yet. Each number comes from a run in the repository, or from a real call in the ledger of the owner's instance. An estimate says that it is an estimate.

- **Search** is measured on a golden set, and CI checks the result on each change of the API.
- **The costs of the model** come from real calls. The two card rows used 16 real saved items; the other rows used small, made-up inputs.
- **The relevance scores, the overview, the cards and the readers of pages** have no measurement yet. The last part says what each one needs.

## 1. Search quality

### The result

`make eval` on the commit 8664b09 (1 November 2026). The fused line is the same as in [`baseline.json`](../apps/api/evals/search/baseline.json), which was recorded on 20 October 2026 (pull request #102). The baseline holds only the fused line, and the gate reads only its recall@10 and nDCG@10.

```text
search on the golden set: 39 items, 38 queries; vectors of voyage/voyage-4-large (1024 numbers)
list        recall@10  nDCG@10    MRR
full text       0.349    0.475  0.553
trigram         0.294    0.402  0.474
vector          1.000    0.963  0.987
fused           1.000    0.974  1.000
```

A relevant item has a grade of 1 or more (the grades are in [the golden set](#the-golden-set)). recall@10 and MRR count each relevant item the same. Only nDCG@10 reads the grade.

- **recall@10**: the part of the relevant items that are in the first 10 hits. 1.000 means that each relevant item is in the first 10.
- **nDCG@10**: the order of the first 10 hits, compared with the best order. An item with a high grade counts more, and a low place counts less. 1.000 is the best order.
- **MRR**: 1 divided by the place of the first relevant hit, as a mean over the queries. 1.000 means that the first hit of each query is relevant. It does not mean that the first hit answers the query: for 'reaper for expired leases', the answer is in place 2.
- **The four lists**: search asks three retrievers, then fuses their lists with reciprocal rank fusion (RRF). "Fused" is the list that you see. [Note 36](learn/36-three-retrievers.md) explains the retrievers, and [note 37](learn/37-fusion-and-metrics.md) explains the fusion and the metrics.

The fused list has the best order: nDCG@10 0.974, against 0.963 for the vector list, at the same recall@10 of 1.000. Full text finds the words of the item and their other forms ("queues" finds "queue"). Trigram compares the query with the titles, so it also finds a title with a typo. The vectors find the same meaning in other words, and the fusion moves the items that two lists agree on to the top.

### The 5 weakest queries

The number is the nDCG@10 of the fused list for that query. The text in parentheses is what the query tests (the field `what` of the golden set).

```text
0.797  'reaper for expired leases'  (a detail that only the text has)
0.899  'postgres job queue'  (plain keywords)
0.906  'split long documents into pieces for search'  (the same meaning in other words)
0.909  'graded relevance metric with a log discount'  (a description of a thing, not its name)
0.917  'events in the same transaction as the data'  (a description of a thing, not its name)
```

The weakest query asks for "expired" leases, and the text says that the lease "ended". Full text needs each word of the query, so it cannot find the item. The vectors find it, but not in the first place.

### What real vectors changed

The first recording used the fake embedder of the tests, which counts words. Then the same golden set was recorded with Voyage (`git log -- apps/api/evals/search/baseline.json` shows both):

| Vectors | recall@10 | nDCG@10 | MRR |
|---|---|---|---|
| `fake/hashed-bow-64/64` (pull request #101) | 0.716 | 0.777 | 0.855 |
| `voyage/voyage-4-large/1024` (pull request #102) | 1.000 | 0.974 | 1.000 |

### The golden set

[`apps/api/evals/search/golden.json`](../apps/api/evals/search/golden.json):

- **39 made-up items** of the kind that a developer saves: databases and queues, search and retrieval, machine learning, languages and infrastructure, 4 posts in Persian, and 3 posts on other subjects (bread, coffee, running). 6 long texts give 9 pieces, so search also finds a passage inside a long text.
- **38 queries.** 3 of them are in Persian script.
- **84 grades from 0 to 3**: 38 times 3 (the item answers the query), 7 times 2 (about the topic), 38 times 1 (it touches the topic), and 1 explicit 0. 36 queries have exactly one item with the grade 3. 'combine keyword and vector results' has two. The typo query 'postgress' has none: its best items have the grade 2.
- **Who graded.** The author of the search code wrote the items, the queries and the grades together, in pull request #101, before the first run. No independent person graded them, and no grade comes from real saved items.

[Note 40](learn/40-measuring-search.md) explains how the set was made and why each rule of the loader exists.

### How CI uses it

1. The vectors of the golden set are recorded one time with `make eval-live` (86 vectors, 241 KB in [`vectors.json`](../apps/api/evals/search/vectors.json)). CI replays them, so a run needs no key and costs nothing.
2. `make eval` runs the gate `tests/evals/test_search_gate.py`. It is part of the API suite, so CI runs it on each pull request that changes the API.
3. The gate fails when recall@10 or nDCG@10 of the fused list differs from `baseline.json` by more than 0.000001. It fails on a rise too: a better number must be written into the baseline on purpose, with `make eval-baseline`.
4. The gate also fails when the baseline was measured with other vectors. It stops when a text of the golden set, or of a card, is not in `vectors.json`. After a change of the golden set or of the text of a card, run `make eval-live`, then `make eval-baseline`.

Three planted regressions show that the gate can fail. Each one leaves recall@10 at 1.000, and each one drops nDCG@10:

| Change | nDCG@10 |
|---|---|
| None (the baseline) | 0.973533 |
| No trigram list in the fusion | 0.971169 |
| `RRF_K = 0` | 0.972854 |
| Only the vector list | 0.962847 |

### The limits of this measurement

- **recall@10 is at its ceiling.** It is 1.000, so it cannot show a gain. It falls only when a relevant item leaves the first 10 places, and it cannot see a worse order inside them. Only nDCG@10 sees the order. A harder set needs more items on the same topics.
- **The set is small.** With 38 queries, one query moves a mean by up to 1/38 = 0.026. The gate is an alarm for a change. It is not a benchmark against other systems.
- **The items are made up.** Real saved items are longer, and many are in Persian or mix two languages. Only 3 queries are in Persian script.
- **The grades and the system have one author.** The grades were written by the same author as the search, so they can share its blind spots.

## 2. The costs of the model

All calls go through one ledger (`llm_calls`). The ledger records the tokens and the price of each call, and the same question again comes from the ledger at no cost. A daily budget (`UNSHELVE_LLM_DAILY_BUDGET_USD`, $1.00 by default) stops new calls. It does not count the answers of a batch, because you accept the price of a batch by hand. `docker compose exec api unshelve llm costs` prints the costs.

The default model is Claude Opus 5.5 (`UNSHELVE_LLM_MODEL`). A real call happens only with `UNSHELVE_PROVIDERS=live` and `ANTHROPIC_API_KEY`; the default `fake` costs nothing. These are the real calls:

| What | Model | Input | Result | Source |
|---|---|---|---|---|
| A card (summary, key points, tags) | Claude Haiku 5.5 | 16 real saved items: posts of a test channel and public pages | $0.000857 a card on average, $0.0137 for the 16 | the ledger of the owner's instance, 15 October 2026; [note 17](learn/17-model-card.md), part 7, describes the run, and [note 29](learn/29-cards-in-a-batch.md) uses its prices |
| A card | Claude Opus 5.5 | the same 16 items | $0.0340 a card on average, $0.543 for the 16 | the same |
| Cards in a batch | Claude Haiku 5.5 | 3 made-up notes | $0.000270 for the 3, half of the normal price; the batch took 2.7 minutes | [note 29](learn/29-cards-in-a-batch.md) |
| A score against 2 goals | Claude Haiku 5.5 | 3 made-up cards | $0.00014 to $0.00016 for each question | [ADR 0014](adr/0014-scores-a-vector-prefilter-then-one-question-for-all-goals.md) |
| Text from a screenshot | Claude Haiku 5.5 | 2 screenshots, English and Persian | $0.00024 to $0.00039 for each reading | [note 28](learn/28-image.md) |

From these prices, note 29 calculates the import of 2,000 posts: about $1.72 with Claude Haiku 5.5, or about $68 with Claude Opus 5.5. A batch costs half. These two totals are estimates.

**Not measured:** the cost of an overview. No real overview call was made. Note 39 estimates about 7 cents with Claude Opus 5.5 and less than 1 cent with Claude Haiku 5.5, for an overview of 12 items.

Vectors: Voyage AI makes the vectors when `UNSHELVE_EMBEDDER=voyage`. The first 200 million tokens of a Voyage account are free. The recording of the golden set (86 texts) took about 17 seconds.

## 3. Not measured yet

| Quality | Why it is not measured | What it needs |
|---|---|---|
| Accuracy of the relevance scores (0 to 3) | No command compares the scores with your verdicts yet ([note 33](learn/33-labels-and-golden-set.md)). | Your "Right" and "Wrong" verdicts on real items. `docker compose exec -T api unshelve labels export` writes them as a set. Then a script that compares them with the scores. |
| Items that the prefilter gives 0 by mistake | The same: it needs your verdicts. | The same set, and a count of the relevant items that the prefilter stopped. |
| The overview: does each citation support its sentence? | No evaluation of the overview exists ([note 39](learn/39-overview-with-citations.md)). | A set of questions with the passages that answer them, and a check of each citation. |
| The cards on real items | Two models were compared on the same 16 items with the prompt `enrich-1`, and the kinds of mistakes were noted ([note 17](learn/17-model-card.md), part 7). The prompt `enrich-2` was checked on 3 bare links. No score. | A set of real items with good cards, and a way to compare a card with them. |
| The readers of pages (article, PDF, YouTube, X, GitHub, arXiv, Telegram, image) | Fixture tests in CI, and live tests on a few real pages that run only by hand (`-m live`) ([note 15](learn/15-article-extractor.md), [note 24](learn/24-pdf-file.md), [note 28](learn/28-image.md)). No set of pages with the text that each one must give. | A set of recorded pages with the text that each one must give. |
| Search time | No timing of a search or an overview. | Timings on a database of realistic size. |
| Does the digest bring items back? | The instance has too few digests to count. | The read time of each item after a digest, over some weeks. |

## How to run it yourself

```bash
make eval            # the gate: replays the recorded vectors, no key needed
make eval-live       # records the vectors again with the embedder of .env (Voyage needs VOYAGE_API_KEY)
make eval-baseline   # writes the new numbers into baseline.json
```

`make eval-live` calls Voyage when `.env` has `UNSHELVE_EMBEDDER=voyage` and `VOYAGE_API_KEY`. Without them, it writes the vectors of the fake embedder over `vectors.json`, and the gate then fails. Run `make eval-baseline` only when a change of the numbers is on purpose, and say why in the pull request.
