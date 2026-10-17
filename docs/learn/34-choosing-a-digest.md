# 34. Choosing a digest

> Pull request: [#95](https://github.com/herman72/unshelve/pull/95) · Code: [`resurface/selection.py`](../../apps/api/src/unshelve/resurface/selection.py), [`resurface/plan.py`](../../apps/api/src/unshelve/resurface/plan.py), [`api/item_views.py`](../../apps/api/src/unshelve/api/item_views.py) (`preview_digest`), [`app/digest/page.tsx`](../../apps/web/src/app/digest/page.tsx) · Tests: [`tests/resurface/test_selection.py`](../../apps/api/tests/resurface/test_selection.py), [`tests/resurface/test_plan.py`](../../apps/api/tests/resurface/test_plan.py), [`tests/api/test_digests.py`](../../apps/api/tests/api/test_digests.py), [`apps/web/e2e/digest.spec.ts`](../../apps/web/e2e/digest.spec.ts) · Interactive page: [Digest simulator](../digest-simulator.html)

## In one minute

You have one hour to read. Ten items wait in your inbox, and your focus goal is "Database internals". Which items must the digest bring back?

| # | Item | Minutes | Saved | Database internals | Learn Finnish |
|---|---|---|---|---|---|
| 1 | Leases for job queues in Postgres | 40 | 30 days ago | 3 | 0 |
| 2 | How MVCC keeps old rows | 30 | 45 days ago | 3 | |
| 3 | B-tree pages, drawn | 15 | 60 days ago | 2 | |
| 4 | Finnish cases in ten minutes | 10 | 20 days ago | 0 | 3 |
| 5 | A talk on write-ahead logs | 25 | 90 days ago | 3, you said "wrong" | |
| 6 | Sourdough at home (saved 3 times) | 8 | 50 days ago | 0 | 0 |
| 7 | A new paper on vector indexes | 20 | 2 days ago | | |
| 8 | A note about a bike route | 5 | 120 days ago | 0 | 0 |
| 9 | Ten terminal shortcuts | 4 | 200 days ago | 1 | |
| 10 | A long essay on attention | 90 | 300 days ago | 1 | 1 |

The digest of this inbox for one hour, with the seed `1:2026-10-18`:

1. Leases for job queues in Postgres (40 minutes): for your focus goal.
2. B-tree pages, drawn (15 minutes): for your focus goal.
3. Ten terminal shortcuts (4 minutes): a random pick from your inbox.

That is 59 of 60 minutes. Item 2 also helps the focus goal, but it does not fit into the 16 minutes that are left after item 1.

A *digest* is a short list of items that you saved earlier. The *budget* is the reading time of the digest, in minutes. The new page "Digest" shows the digest of your own inbox for a budget that you choose (made-up items of the tests here):

![The page "Digest": "Time to read: 1 hour", then "3 items, about 24 of 60 minutes. Focus goal: Database internals." Each item has a line above its card: "For your focus goal: Database internals", "You saved it in the last 7 days" and "A random pick from your inbox"](images/34-digest.png)

| | Before (M1) | Now |
|---|---|---|
| What comes | up to 5 items of the last 7 days, and one older item of the inbox | items for your focus goal first, then for other goals, items that you saved again, recent items, and one random pick |
| Which items | the inbox and the queue | only the inbox: the page "Queue" shows your queue |
| The time | not used | the items fit the budget |
| Repeats | possible: no digest knows what the last one showed | no item that a digest showed in the last 3 weeks |
| A second try | the same digest (a seed) | the same digest (a seed), also after a Python upgrade (part 3) |

The weekly digest in Telegram still uses the old choice. M3-5 (#29) moves it to the new choice.

## 1. The rules, in order

Open the [digest simulator](../digest-simulator.html) next to this note. It does these steps one at a time, with the example above.

0. **Who can come.** Only items of the inbox: not a merged copy, and not an item that a digest showed in the last 3 weeks.
1. **The random slot.** It is reserved first (part 2).
2. **The focus goal.** Items with an effective score of 2 or 3 for it, the highest first.
3. **Other goals.** Items with an effective score of 2 or 3 for another active goal.
4. **Saved again.** Items that you saved two times or more.
5. **Recent.** Items of the last 7 days, the newest first.
6. **The random pick goes last.**

Each rule takes its items one at a time. An item that does not fit into the minutes that are left is skipped, and a shorter item after it can still come. Each item comes at most one time, and a digest has at most 8 items.

In the example, 56 minutes are left after the random slot:

| Rule | Item | Minutes left | What happens |
|---|---|---|---|
| the focus goal | 1 (40 minutes, score 3) | 56 | it comes in: 16 are left |
| the focus goal | 2 (30 minutes, score 3) | 16 | skipped: it does not fit |
| the focus goal | 3 (15 minutes, score 2) | 16 | it comes in: 1 is left |
| other goals | 4 (10 minutes, a 3 for Finnish) | 1 | skipped |
| saved again | 6 (8 minutes) | 1 | skipped |
| recent | 7 (20 minutes) | 1 | skipped |

Item 5 has a 3 for the focus goal, but you said "wrong". So it counts as 0. The *effective score* is the score after your verdict. The golden set uses the same rule ([note 33](33-labels-and-golden-set.md), part 4).

**A comparison from daily life:** you pack a bag that has a weight limit. The most important things go in first. A heavy thing that does not fit stays out, but a small thing can still go in.

## 2. The random slot

The random slot brings back one item that no other rule chooses. In the example, items 8 and 9 are such items: no score of 2 or more, saved one time, and not recent. The seed chose item 9.

Three details make it work:

- **It is chosen first.** If it came last, the rules could use all the minutes, and the random pick would not fit.
- **It is short.** It takes at most a quarter of the budget: 15 minutes of 60. It is never less than 5 minutes, and never more than the budget. So it cannot push out the items of the focus goal.
- **It takes only items without a rule.** The first version took any short item. Then a test of the API showed a digest with one item that helps the focus goal, under the reason "A random pick from your inbox". The reason was wrong, and the digest showed nothing new.

Why does a digest have a random slot? The rules show the high scores again and again. A wrong 0 from the model hides a useful item for ever. The random pick gives such an item a chance. Later (M3-5), your verdict on it can show that the 0 was wrong. Verdicts only on the items that the model likes teach you only about those items: the plan calls this *label bias*.

**A comparison from daily life:** a shop keeps one shelf for a surprise product. The shelf is always there, so the best sellers cannot fill it.

## 3. The same input gives the same digest

`select` is a *pure function*: its result comes only from its inputs. It reads no database, no clock and no network. The caller gives it the time (`now`) and a *seed*: a text that fixes the random numbers.

| The digest | Its seed |
|---|---|
| the preview of the page | the owner and the day, for example `1:2026-10-18` |
| the weekly digest (M3-5) | the week |

So a second preview on the same day shows the same random pick. And a second try of a weekly digest gives the same digest.

Two more details keep it so:

- **The order of the rows does not count.** The database gives its rows in no fixed order. So `select` sorts the items by their id first.
- **Python keeps `random()` the same, not `choice()`.** The [documentation of the random module](https://docs.python.org/3/library/random.html#notes-on-reproducibility) promises the same `random()` numbers for a seed in each new Python version. It does not promise that for `choice()`. So the code calls `random()` and computes the place in the list itself. A test pins the pick of the seed `2026-W47`: item 26. With `choice()`, it would be item 29.

This split has a name: *functional core, imperative shell*. The core (`selection.py`) decides. The shell (`plan.py`) reads the database and gives the core its inputs. The core has tests on made-up inputs, without a database.

## 4. Property-based tests

An *example test* gives one input and checks one answer. A *property test* states a rule that must hold for each input. The library [Hypothesis](https://hypothesis.readthedocs.io/en/latest/) makes hundreds of inputs and checks the rule for each one.

The inputs of `test_selection.py` have up to 40 items with any status, minutes from 0 to 180 or none, scores with and without verdicts, budgets from 0 to 600 minutes, and any seed. These are the properties:

| Property | What it checks |
|---|---|
| A digest never passes its budget | the minutes of the picks are at most the budget |
| A digest repeats nothing | no item two times, nothing that a digest showed recently, only the inbox |
| The same input and seed give the same digest | two runs give equal digests |
| The order of the candidates does not change the digest | Hypothesis shuffles the items |
| Each pick has its reason | the rules come in their order, at most one random pick, a pick for a goal helps that goal |
| The random slot is there when a short item without a rule is there | and the random pick is the last item |

By default, each property gets 500 inputs, and they are the same in each run and on each computer (the profile "repeatable" in `tests/conftest.py`). So a failure on CI can be repeated on your computer. To look for new problems with 50,000 new inputs for each property, run this in `apps/api`:

```bash
HYPOTHESIS_PROFILE=explore uv run pytest tests/resurface/test_selection.py
```

When a property fails, Hypothesis makes the input smaller, step by step, while the property still fails. This is *shrinking*. You get the smallest input that shows the mistake.

Here is a real example. I changed one line, so that the random pick did not count against the budget. Hypothesis found this input (shortened):

```text
Failing test case: test_a_digest_never_passes_its_budget(
    inputs=Inputs(candidates=[
        Candidate(item_id=1, status=INBOX, saved_at=2026-10-18 09:00, saves=1, minutes=None, scores={}),
        Candidate(item_id=2, status=INBOX, saved_at=2026-10-11 08:59, saves=1, minutes=None, scores={})],
     focus_goal_id=None, budget=5, seed='', shown_recently=frozenset()),
)
AssertionError: assert 10 <= 5
```

Here is what happened. The budget is 5 minutes. Item 2 was saved 7 days and 1 minute ago, so no rule takes it, and it is the random pick (5 minutes: it has no estimate). Item 1 was saved now, so the rule "recent" takes it. With the mistake, the rules still had 5 minutes for item 1. That gives 10 minutes for a budget of 5. Hypothesis started from a large random input and removed each part that the failure did not need. It even found the edge of "recent": 1 minute after 7 days.

**A comparison from daily life:** a mechanic looks for the cause of a noise. They remove one part at a time, and they keep a part out when the noise stays.

## A choice that we made

How does a digest fill its minutes?

| | In the order of the rules (ours) | The best fit (a *knapsack*) |
|---|---|---|
| A more important item | is never left out for two less important ones | can be left out when two smaller items fill the minutes better |
| The minutes | some can stay free (1 of 60 in the example) | as many as possible are used |
| The reason of a pick | one rule for each pick, and "it does not fit" for each skip | "this set has the highest total value" |
| The code | one loop | dynamic programming over the minutes |

## What a senior engineer checks here

- **Can a digest pass its budget?** No. A property test checks 500 made-up inputs, and the random slot counts its minutes before the rules start.
- **Can an item come two times, or again within 3 weeks?** No. Property tests check it. A database test has a digest of 21 days minus 1 second ago and one of 21 days plus 1 second ago.
- **Does the same input give the same digest?** Yes: also with the rows in another order, and in a new Python version (the pinned seed).
- **Can the random pick take an item that a rule wants?** No. That was a real mistake of the first version, and a test of the API found it.
- **Does the preview change anything?** No. A test counts the digests and reads `last_surfaced_at` after a preview.
- **Can a test see a changed constant?** A test that imports `RECENT_DAYS` changes with it, so it cannot fail. The tests of "7 days", "3 weeks", "8 items", "5 minutes" and "a quarter" use the numbers themselves.
- **Can the tests fail?** I broke the code in 69 places, one at a time: 37 in the choice, 17 in the loader, 9 in the API and 6 in the web app. The first run of the Python mistakes found 58 of 63. Three misses showed weak tests:
  - The made-up inputs seldom had a random pick. So "the random pick does not count against the budget" and "the order of the items changes the random pick" passed. The inputs now have more items for the random slot, and two example tests show each mistake.
  - The test of the seed compared 07:00 and 22:00 of one day. An hourly seed gave the same pick for these two hours by chance (1 in 30). The test now compares four times of the day.

  Two misses cannot fail a test, because a second filter does the same work. The loader reads only the inbox, and `select` also takes only the inbox. The loader reads only the scores of the owner, and a goal id already belongs to one owner.

  The browser tests found each of the 6 mistakes in the web app. Now the tests find 67 of the 69 mistakes, and the 2 others change no result.
- **What is not done:**
  - The weekly digest and `/digest` of the bot still use the old choice. M3-5 (#29) moves them to the new choice, marks the items as shown, and keeps the reason of each pick.
  - The page has no choice of the goal yet. The API takes a `goal_id`.
  - The digest has no buttons of a verdict yet (M3-5).

## Try it yourself

1. Open `docs/digest-simulator.html` in a browser. Press "Next step" until the digest is ready.
2. Choose "2 hours". Then mark item 9 as "Shown lately". Which item takes the random slot now?
3. After the pull request is merged, run `git pull`, and then `make up`.
4. Open `http://localhost:3000/digest`, and choose a time to read.

Your items have no scores yet. Until they have scores, the page shows recent items and a random pick. To score the old items, run this command. It shows the price first, and it asks before it calls the model:

```bash
docker compose exec api unshelve goals rescore
```

## Exercise (15 minutes)

1. Open `apps/api/src/unshelve/resurface/selection.py`, and go to `select`.
2. In the line `if len(picks) >= room or candidate.item_id in taken or minutes > left:`, remove `candidate.item_id in taken or `.
3. Run the property tests in `apps/api`:

```bash
uv run pytest tests/resurface/test_selection.py -q -k "repeats or reason"
```

Which property fails? Read the input that Hypothesis shows: why does one item come two times?

<details>
<summary>The answer</summary>

The property "A digest repeats nothing" fails. Hypothesis shows this input (shortened):

```text
FAILED tests/resurface/test_selection.py::test_a_digest_repeats_nothing
    inputs=Inputs(candidates=[
        Candidate(item_id=1, status=INBOX, saved_at=2026-10-18 09:00, saves=2, minutes=None, scores={})],
     focus_goal_id=None, budget=10, seed='', shown_recently=frozenset())
E       assert 2 == 1
```

One item, saved two times, the last time now. Two rules take it: "saved again" (two saves) and "recent" (saved now). Without the check of `taken`, the second rule takes it again, and the budget of 10 minutes holds both copies. The digest shows the same item two times.

The property "Each pick has its reason" still passes: each of the two picks has a correct reason. Only a property about the whole digest sees the mistake.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Digest | A short list of saved items, for a reading time that you choose |
| Budget | The reading time of a digest, in minutes |
| Effective score | The score after your verdict: "wrong" turns it |
| Random slot | The one place in a digest for an item that no other rule chooses |
| Pure function | A function whose result comes only from its inputs |
| Seed | A text that fixes the random numbers: the same seed gives the same numbers |
| Property test | A test of a rule that must hold for each input |
| Shrinking | Hypothesis makes a failing input smaller, until it is the smallest input that still fails |
| Knapsack | The problem of the best set of things for a fixed size |

## To read more

- [Introduction to Hypothesis](https://hypothesis.readthedocs.io/en/latest/tutorial/introduction.html). Read "Defining a simple test" and "When to use Hypothesis and property-based testing".
- [Notes on Reproducibility](https://docs.python.org/3/library/random.html#notes-on-reproducibility), in the Python documentation of `random`. Read the two promises: they are the reason why the code calls `random()` and not `choice()`.

Previous: [33. Your verdicts, and the golden set](33-labels-and-golden-set.md) · Back to the [list of notes](README.md)
