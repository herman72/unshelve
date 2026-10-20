# 37. Fusion and metrics

> Pull request: [#98](https://github.com/herman72/unshelve/pull/98) (your exercise) · Code: [`search/fusion.py`](../../apps/api/src/unshelve/search/fusion.py), [`search/metrics.py`](../../apps/api/src/unshelve/search/metrics.py) · Tests: [`tests/search/test_fusion.py`](../../apps/api/tests/search/test_fusion.py), [`tests/search/test_metrics.py`](../../apps/api/tests/search/test_metrics.py)

This note gives you the formulas and worked examples. The code is your exercise: the tests are its specification.

## In one minute

The search "postgres" of [note 36](36-three-retrievers.md) gives three lists (item 1 is the job queue, 2 pgvector, 3 Rust, 8 the weekend notes):

| Rank | Full text | Trigram | Vector |
|---|---|---|---|
| 1 | 1 | 1 | 1 |
| 2 | 2 | | 2 |
| 3 | 8 | | 3 |

Search must show one list. The scores of the retrievers cannot be added: a rank of full text (0.61 for a title), a similarity of trigrams (0.73 for a typo) and a cosine distance measure different things. *Reciprocal rank fusion* (RRF) uses only the places. With k = 60:

| Item | Score | Result |
|---|---|---|
| 1 | 1/61 + 1/61 + 1/61 = 0.0492 | first: first in each list |
| 2 | 1/62 + 1/62 = 0.0323 | second: second in two lists |
| 3 | 1/63 = 0.0159 | third: a tie with 8, and 3 is the lower id |
| 8 | 1/63 = 0.0159 | fourth |

Then, how good is a ranking? Three *metrics* answer it, against the answers that a person checked: recall@k, MRR and nDCG@k (parts 2 to 4). M4-5 uses them to compare the retrievers and the fusion on the same questions.

## 1. Reciprocal rank fusion

The score of an item is the sum, over the lists that contain it, of

```text
1 / (k + rank)        rank 1 for the first place
```

A list without the item adds nothing. The highest score comes first; on equal scores, the lower id comes first.

**Why the sum of 1/(k + rank)?** Each list gives a small vote, and a higher place gives a larger vote. An item that several retrievers find is very likely what you look for, so the votes add up.

**What k does.** Take two lists: `[1, 7, 2]` and `[8, 9, 2]`. Item 1 is first in one list; item 2 is third in both.

| k | Item 1 | Item 2 | Winner |
|---|---|---|---|
| 0 | 1/1 = 1.0 | 1/3 + 1/3 = 0.67 | 1: one first place |
| 60 | 1/61 = 0.0164 | 1/63 + 1/63 = 0.0317 | 2: agreement |

A large k makes the first places of a list count less against the later ones. The authors of RRF chose k = 60 (Cormack, Clarke and Büttcher, 2009), and many search engines use it.

**Copies.** If an id is two times in one list, only its first place counts. The copy keeps its place: in `[5, 6, 5, 7]`, item 7 has the rank 4.

**A comparison from daily life:** three friends each give you their list of the best cafés. You do not average their stars, because each friend gives stars differently. You count the places: a café that is high on all three lists wins.

## 2. recall@k

Recall@k is the share of the relevant items that are in the first k places. The ranking is `[3, 1, 4, 5, 9]`, and the relevant items are 1, 5 and 7:

| k | First k places | Relevant in them | recall@k |
|---|---|---|---|
| 1 | 3 | none | 0/3 = 0.0 |
| 2 | 3, 1 | 1 | 1/3 = 0.33 |
| 4 | 3, 1, 4, 5 | 1, 5 | 2/3 = 0.67 |
| 10 | the 5 places | 1, 5 | 2/3 = 0.67: item 7 is not in the ranking |

The divisor is the number of relevant items (3), not k. Recall answers: "Did the search find what exists?"

## 3. Reciprocal rank and MRR

The reciprocal rank is 1 / the place of the first relevant item. For `[3, 1, 4, 5, 9]` with the relevant items 1, 5 and 7, item 1 is at place 2: 1/2 = 0.5. Without a relevant item in the ranking, it is 0.

The MRR (mean reciprocal rank) is the mean over many queries:

| Ranking | Relevant | Reciprocal rank |
|---|---|---|
| `[3, 1, 4]` | 4 | 1/3 |
| `[2]` | 2 | 1 |
| `[9, 8]` | 7 | 0 |
| | **MRR** | (1/3 + 1 + 0) / 3 = 0.44 |

MRR answers: "How soon does the first good answer come?"

## 4. nDCG@k

Some items are more relevant than others. A person gives each judged item a *grade*: 0 (not relevant) to 3 (very relevant). nDCG@k compares the first k places with the best possible order.

1. The *gain* of a place is the grade of its item (0 for an item without a grade).
2. The *discount* of place i is `log2(i + 1)`: 1 for place 1, 1.58 for place 2, 2 for place 3. A lower place counts less.
3. DCG@k is the sum of gain / discount over the first k places.
4. IDCG@k is the DCG@k of the best order: all judged items, the highest grade first.
5. nDCG@k = DCG@k / IDCG@k, from 0 to 1.

An example with the grades 1: 3, 2: 2, 3: 1 and 4: 0, and the ranking `[2, 4, 1, 9, 3]`:

| Place | Item | Grade | Discount | Gain / discount |
|---|---|---|---|---|
| 1 | 2 | 2 | 1.00 | 2.00 |
| 2 | 4 | 0 | 1.58 | 0 |
| 3 | 1 | 3 | 2.00 | 1.50 |
| 4 | 9 | 0 (no grade) | 2.32 | 0 |
| 5 | 3 | 1 | 2.58 | 0.39 |
| | | | **DCG** | **3.89** |

The best order is items 1, 2, 3: 3/1 + 2/1.58 + 1/2 = **4.76** (IDCG). So nDCG@10 = 3.89 / 4.76 = **0.82**.

The best order reads the judgements, not the ranking. If the ranking misses a relevant item, the best order still has it, and nDCG goes down. That is the point: a search that misses an item is worse.

## A choice that we made

**Which gain?** Two forms of DCG are common:

| | Gain = grade (ours) | Gain = 2^grade − 1 |
|---|---|---|
| A grade 3 against a grade 2 | 3 against 2 | 7 against 3 |
| Use | scikit-learn's `ndcg_score`; easy to check by hand | web search, where only the best answers matter |

With grades from 0 to 3 that come from one person, the plain grade is clearer, and you can check each value with scikit-learn.

**A query without relevant items** cannot be measured: each function raises `ValueError`, and the evaluation (M4-5) leaves the query out. Giving it 0 would punish the search for a question without an answer; giving it 1 would reward it.

## Your exercise

The pull request [#98](https://github.com/herman72/unshelve/pull/98) has the contracts in the docstrings and the tests. Run the tests from `apps/api`; `-x` stops at the first failure, so you always see the next thing to do:

```bash
uv run pytest tests/search/test_fusion.py tests/search/test_metrics.py -x
```

## What a senior engineer checks here

- **Off by one.** The first place is 1, in each formula: `1/(k + 1)`, `1/1` and `log2(2)`.
- **Copies.** Real lists can have them. A copy counts one time, and it keeps its place.
- **The best order.** IDCG comes from the judgements, not from the items that the ranking found.
- **Equal floats.** The tests compare scores with `pytest.approx`, and they never permute the lists: adding floats in a different order can change the last digit.
- **Can the tests fail for the usual mistakes?** I checked them against a reference implementation (on a branch that is not pushed) and 21 typical mistakes. The reference passes; each mistake fails at least one test. One of them, "nDCG@k with k = 5 by default", passed at first; a test now puts graded items at the places 10 and 11.

## Words used in this note

| Word | Meaning |
|---|---|
| Fusion | Joining several ranked lists into one |
| RRF | Reciprocal rank fusion: the sum of 1 / (k + rank) over the lists |
| Relevant item | An item that a person marked as a good answer to the query |
| Recall@k | The share of the relevant items in the first k places |
| Reciprocal rank | 1 / the place of the first relevant item |
| MRR | The mean reciprocal rank over many queries |
| Grade | How relevant a judged item is, from 0 to 3 |
| DCG, IDCG, nDCG | The discounted gain of a ranking, of the best ranking, and their ratio |

## To read more

- [Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf), Cormack, Clarke and Büttcher, SIGIR 2009. Read the first page: the formula, and the sentence about how k = 60 was chosen.
- [Discounted cumulative gain](https://en.wikipedia.org/wiki/Discounted_cumulative_gain#Normalized_DCG), Wikipedia. Read "Normalized DCG": the same steps as part 4, with another example.

Previous: [36. Three retrievers](36-three-retrievers.md) · Back to the [list of notes](README.md)
