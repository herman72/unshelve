# 40. Measuring search

> Pull request: [#101](https://github.com/herman72/unshelve/pull/101) · Code: [`evals/golden.py`](../../apps/api/src/unshelve/evals/golden.py), [`evals/cassette.py`](../../apps/api/src/unshelve/evals/cassette.py), [`evals/search.py`](../../apps/api/src/unshelve/evals/search.py) · Data: [`apps/api/evals/search/`](../../apps/api/evals/search/) · Tests: [`tests/evals/`](../../apps/api/tests/evals/)

## In one minute

`make eval` prints this:

```text
search on the golden set: 39 items, 38 queries; vectors of fake/hashed-bow-64 (64 numbers)
list        recall@10  nDCG@10    MRR
full text       0.349    0.475  0.553
trigram         0.294    0.402  0.474
vector          0.646    0.510  0.487
fused           0.716    0.777  0.855
the 5 weakest queries (nDCG@10 of the fused list):
  0.000  'reaper for expired leases'  (a detail that only the text has)
  0.249  'fine-tune a large model cheaply'  (the same meaning in other words)
  0.278  'combine keyword and vector results'  (two topics in one question)
  0.439  'retry a payment without charging twice'  (a description of a thing, not its name)
  0.458  'nearest neighbour index for embeddings in a database'  (the same meaning in other words)
```

Each retriever alone finds a part of the answers. The fused list finds more than each of them: recall@10 is 0.716, against 0.349 for full text alone. This is the reason for reciprocal rank fusion ([note 37](37-fusion-and-metrics.md)), now in numbers.

The same numbers are a gate. CI runs the evaluation with the other tests, and it fails when the fused recall@10 or nDCG@10 is not the number of the committed baseline.

## 1. The golden set

A *golden set* is a fixed list of questions with answers that a person checked. Ours is [`golden.json`](../../apps/api/evals/search/golden.json): 39 made-up items and 38 queries. The real items are private, so they cannot be in the repository or in CI.

A person gives each query its *grades*:

- 3: the item answers the query;
- 2: it is about the topic;
- 1: it touches the topic;
- 0: it does not help.

An item without a grade counts as 0. An example:

| Query | Item | Grade |
|---|---|---|
| how do several workers avoid taking the same task | Postgres as a job queue | 3 |
| | Go channels and worker pools | 2 |
| | Leases and fencing tokens | 2 |
| | Idempotency keys for payment APIs | 1 |

Each query tests one thing, and says so in its field `what`:

| What the query tests | Example |
|---|---|
| An exact technical term | `SKIP LOCKED` |
| Another form of a word | `queues` finds "queue" |
| A typo | `postgress`, `kubernets operater` |
| The same meaning in other words | `fine-tune a large model cheaply` |
| Persian with and without the zero-width non-joiner | `کتابهای یادگیری ماشین` |
| A title typed with Arabic letters | `یادگیری عمیق` |
| A detail that only the text of an item has | `garbage collection pause` |

**The loader refuses a set that the metrics cannot measure.** It refuses a grade outside 0 to 3. That was an open point of [note 37](37-fusion-and-metrics.md): a negative grade gave an nDCG above 1. It also refuses:

- a grade for an item that does not exist;
- a query without an item that helps;
- two items with one key, or two queries with the same words;
- a misspelt field.

## 2. Recorded vectors

The vector retriever needs the vectors of the items and of each query. A real embedder (Voyage) is a call over the internet with a key. CI has neither, and each run would cost tokens.

So the vectors are recorded one time and replayed. A *cassette* ([`vectors.json`](../../apps/api/evals/search/vectors.json)) holds the spec of the embedder and one vector for each text that it embedded. Ours has 86 vectors: 39 cards, 9 pieces and 38 queries. A text is found by a hash of its kind and its words. The kind is part of it, because many embedders give a document and a query of the same words different vectors.

**A text that the cassette does not hold stops the run.** Then the golden set changed, or the text of a card changed, and `make eval-live` must record again. Nothing can change the input of the embedder in silence.

**The vectors are half-precision floats in base64.** One number takes 2 bytes. The 86 vectors of the fake embedder (64 numbers each) take 21 KB. With Voyage (1,024 numbers each), they would take about 235 KB, below the 500 KB that the commit hook allows. The recorder rounds each vector in the same way before it uses it, so a live run and its replay rank the items the same.

The name comes from VCR.py, a tool that records HTTP calls into "cassettes" and plays them back in tests.

## 3. Four lists, three metrics

For each query, the evaluation runs hybrid search ([note 38](38-search-api.md)) and measures four lists: full text, trigram, vector, and the fused list. It uses the functions of [note 37](37-fusion-and-metrics.md):

- **recall@10**: the share of the relevant items in the first 10 places. Relevant means a grade of 1 or more.
- **nDCG@10**: the order of the first 10 places, against the best order of the grades.
- **MRR**: one over the place of the first relevant item, as a mean over the queries.

A score of a list is the mean of its metric over the 38 queries.

## 4. The gate

The gate is a test, [`test_search_gate.py`](../../apps/api/tests/evals/test_search_gate.py). CI runs `uv run pytest`, so it runs the gate with no new step. The test loads the golden set with its cassette, measures, and compares the fused list with [`baseline.json`](../../apps/api/evals/search/baseline.json).

**A drop fails.** To check the gate itself, I removed the trigram list from the fusion and ran it:

```text
AssertionError: recall@10 dropped from 0.716228 to 0.667982
  nDCG@10 dropped from 0.776610 to 0.681291
```

**A rise fails too.** A better number must be written into the baseline. Otherwise the baseline stays at the old number, and a later change could lose the gain again with no failure. The message says what to do, for example:

```text
nDCG@10 rose from 0.776610 to 0.790000: write the new baseline with make eval-baseline, and commit it
```

The replay is exact: the same vectors, and ties broken by the item id. So the gate allows a difference of only 0.000001 (for the last bits of a float). One query that moves one place changes a mean by far more.

| Command | What it does |
|---|---|
| `make eval` | The gate, with the report of each list. The same test as in CI. |
| `make eval-live` | Records the vectors again, with the embedder of `.env`. |
| `make eval-baseline` | Writes the scores of the recorded vectors as the new baseline. |

## 5. What the first run found

**"reaper for expired leases" scores 0.** The text of the job queue says "A reaper finds the jobs whose lease ended". Full text asks for each word of the query, and "expired" is not in the text. The fake embedder knows words, not meanings, so the vector list does not help either. A real embedder may find it; full text that needs only some of the words could find it too. Each of these is now a change that can be measured, not guessed.

**Questions in other words are weak with the fake embedder.** "fine-tune a large model cheaply" scores 0.249. Full text needs each word, and no item has "cheaply". The fake embedder only counts words, folded into 64 numbers. So three shared words ("fine", "large", "model") do not lift the LoRA item to the top. A real embedder compares meanings: this is the reason to record the real vectors (section "Try it yourself").

## A choice that we made

**The gate is a test, not a step of its own in CI.**

| | A test (ours) | A script with its own CI step |
|---|---|---|
| Database | The test databases of pytest | Its own setup |
| CI | No change | A new step to keep up |
| Output | `capsys.disabled()` shows the report in each CI log | Free |

**The gate fails on a rise, not only on a drop.** This is a *ratchet*: the number moves one way only, and only when you move it on purpose.

| | Fail on a drop and a rise (ours) | Fail on a drop only |
|---|---|---|
| A better change | Fails until `make eval-baseline` is committed | Passes |
| The baseline | Always the true number | Can stay below the true number |
| A later loss of the gain | Fails | Passes, down to the old baseline |

## What a senior engineer checks here

- **Can the gate fail?** Yes: a planted regression (no trigram in the fusion) drops recall@10 from 0.716 to 0.668, and the test fails.
- **Is the run the same each time?** The vectors are recorded and rounded, ties go by the item id, and the gate allows only the last bits of a float.
- **Is the golden set large enough?** No golden set is. With 38 queries, one query changes a mean by up to 1/38 = 0.026. The gate is an alarm for a change, not a benchmark against other systems.
- **Are the grades independent of the system?** Write the grades from the question and the item, before you look at what search returns. Grades written from the results only teach the system its own mistakes.
- **Can the tests of the harness fail?** I put 24 typical mistakes into the evaluation code, one at a time. Before the run, my list showed two gaps in the tests. No test had two queries with the same words. And the full-text list and the trigram list of the small set had the same numbers, so a swap of the two could pass. The first run found two more. A test of a misspelt field passed for the wrong reason, and the small set had no item with the grade 0. Four tests close them. Now each of the 24 mistakes fails a test.
- **What is not done:**
  - The cassette holds the vectors of the fake embedder. The real ones need one run of `make eval-live` with your Voyage key.
  - No evaluation of the overview ([note 39](39-overview-with-citations.md)): whether each citation supports its sentence.
  - No evaluation of the relevance scores against your verdicts ([note 33](33-labels-and-golden-set.md)).

## Try it yourself

1. After the pull request is merged, run `git pull`.
2. Run the gate (it needs Postgres, which `make db` starts):

```bash
make eval
```

3. Record the real vectors. `UNSHELVE_EMBEDDER=voyage` and `VOYAGE_API_KEY` must be in `.env`. The run embeds 86 short texts, far below the free tokens of a Voyage account:

```bash
make eval-live
make eval-baseline
```

The gate now measures your embedder. Commit `vectors.json` and `baseline.json`.

## Exercise (10 minutes)

1. Open `apps/api/src/unshelve/search/fusion.py`, and change `RRF_K = 60` to `RRF_K = 0`.
2. Run the gate in `apps/api`:

```bash
uv run pytest tests/evals/test_search_gate.py -q
```

Which metric changes, and which one stays the same? Why can one of them not see this change?

<details>
<summary>The answer</summary>

```text
fused           0.716    0.771  0.855
AssertionError: nDCG@10 dropped from 0.776610 to 0.771267
```

recall@10 stays at 0.716, and the MRR stays at 0.855. Only nDCG@10 drops.

With k = 0, one first place counts much more than agreement ([note 37](37-fusion-and-metrics.md)), so the order of the fused list changes. But the same items stay in the first 10 places, so recall@10 cannot see it: it counts the items, not their order. The first relevant item of each query also stays at its place, so the MRR does not move. nDCG@10 reads the order of all 10 places, and the order got worse. That is why the gate checks two metrics: each one sees what the other cannot.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Golden set | A fixed list of questions with answers that a person checked |
| Grade | How well an item answers a query, from 0 to 3, by a person |
| Cassette | A file of recorded calls (here: vectors) that a test plays back |
| Replay | Answering from the recording instead of the real service |
| Baseline | The committed numbers that the gate compares with |
| Ratchet | A rule that lets a number move one way only, and only on purpose |
| Regression | A change that makes the results worse |

## To read more

- [VCR.py](https://vcrpy.readthedocs.io/en/latest/), documentation. Read the first page: how a cassette records HTTP calls one time and replays them in each test.
- [Your AI Product Needs Evals](https://hamel.dev/blog/posts/evals/), Hamel Husain. Read the parts about unit tests and about looking at your data. They show why a small set of judged examples finds most problems.

Previous: [39. An overview with citations](39-overview-with-citations.md) · Next: [41. The search page](41-search-page.md) · Back to the [list of notes](README.md)
