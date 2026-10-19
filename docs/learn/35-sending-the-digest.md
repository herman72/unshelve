# 35. Sending the digest

> Pull request: [#96](https://github.com/herman72/unshelve/pull/96) · Code: [`resurface/weekly.py`](../../apps/api/src/unshelve/resurface/weekly.py), [`resurface/stale.py`](../../apps/api/src/unshelve/resurface/stale.py), [`resurface/plan.py`](../../apps/api/src/unshelve/resurface/plan.py) (`shown_score`), [`bot/cards.py`](../../apps/api/src/unshelve/bot/cards.py), [`bot/private.py`](../../apps/api/src/unshelve/bot/private.py) · Tests: [`tests/resurface/test_weekly.py`](../../apps/api/tests/resurface/test_weekly.py), [`tests/resurface/test_stale.py`](../../apps/api/tests/resurface/test_stale.py), [`tests/bot/test_cards.py`](../../apps/api/tests/bot/test_cards.py), [`tests/bot/test_private.py`](../../apps/api/tests/bot/test_private.py)

## In one minute

It is Sunday, 09:00 in Helsinki. Take the made-up inbox of [note 34](34-choosing-a-digest.md). Two messages come from the bot. The first is the digest:

```text
Your digest
3 things, about 59 minutes.

Your focus goal:
Database internals

Leases for job queues in Postgres
#1 · 40 min
For your focus goal. Score: 3 of 3.

B-tree pages, drawn
#2 · 15 min
For your focus goal. Score: 2 of 3.

Ten terminal shortcuts
#3 · 4 min
A random pick from your inbox: saved 6 months ago.
For your focus goal. Score: 1 of 3.

Right or Wrong: is the score of the goal right?

[Done #1]
[Right #1] [Wrong #1]
[Done #2]
[Right #2] [Wrong #2]
[Done #3]
[Right #3] [Wrong #3]
```

The second message asks about old things of the inbox:

```text
Still in your inbox
These things wait since 30 days or more. Archive, or keep?

A long essay on attention
#1 · 90 min
saved 10 months ago

A note about a bike route
#2 · 5 min
saved 4 months ago
…

[Archive #1] [Keep #1]
[Archive #2] [Keep #2]
…
```

| | Before (M1) | Now |
|---|---|---|
| The choice | up to 5 items of the last 7 days, and one older item | the rules of note 34, for `UNSHELVE_DIGEST_MINUTES` (60 by default) |
| Each item | the card | the card, and a line with its reason |
| Buttons | "Open" and "Done" | also "Right" and "Wrong" under a score without a verdict |
| After the digest | nothing | the *stale prompt*: archive or keep, for up to 5 old things |
| The page of an item | nothing about digests | "Shown in your digest of week 43, 2026, for your focus goal" |

`/digest` shows the same digest at any time. It sends no stale prompt, and it records nothing.

## 1. One job, two messages, each one time

Each week has one row in the table `digests`, with a unique key: the owner and the *period key*, for example `2026-W43` ([note 13](13-cards-and-digest.md), part 8). One job sends the digest of the week. Since this pull request, the job does two steps, and the row has a mark for each one:

```text
row 2026-W43: sent_at = none, stale_sent_at = none

step 1, the digest     choose the items, write item_ids          commit
                       send message 1 to Telegram
                       sent_at = now, items shown, events        commit

step 2, the prompt     choose the stale items, write them        commit
                       send message 2 (only when there is one)
                       stale_sent_at = now, events               commit
```

Each step first reads its own mark. If the mark is there, the step does nothing. So a job that runs again sends neither message again: the job is *idempotent* (doing it two times has the same effect as doing it one time).

A test shows why two marks are better than one. The pretend Telegram of the test refuses only the second message. The job fails and runs again. The second try finds `sent_at`, so step 1 sends nothing and marks nothing again. Step 2 sends only the stale prompt. With one mark for both messages, the second try would send the digest a second time.

The order "send, then mark" stays from M1. If the process stops between the sending and the mark, the message comes two times. The other order, "mark, then send", loses the message. A message two times is better than no message.

**A comparison from daily life:** a post office stamps each letter in a book after it goes out. A clerk who comes back after a break reads the book first, and sends only the letters without a stamp.

## 2. Shown: the history of each item

When the digest is sent, step 1 also does two things for each item:

- It sets `last_surfaced_at` of the item to the time of sending.
- It writes an event `surfaced` with the reason, for example `{"period": "2026-W43", "reason": "focus_goal", "goal_id": 1}`.

The page of an item shows the event: "Shown in your digest of week 43, 2026, for your focus goal". The rule "no repeats for 3 weeks" of note 34 reads the rows of `digests`, not the events.

## 3. The stale prompt

A thing is *stale* when all of these are true:

| Rule | Why |
|---|---|
| It is in the inbox, and it is not a merged copy | Only the inbox needs a decision. |
| Its last save was 30 days ago or more | A thing that you saved again is not old. |
| No digest of the last 3 weeks showed it | A digest just gave it a chance. |
| No stale prompt of the last 3 weeks asked about it | No answer is not a reason to ask each week. |
| You did not keep it in the last 90 days | "Keep" is a clear answer. |

The digest of the same week counts too. Step 1 marks it as sent before step 2 chooses, so the prompt never asks to archive a thing that the message before it brought back. A prompt asks about at most 5 things, the oldest first.

- **Archive** moves the thing to the status "archived". Nothing is deleted: on the page of the item, "Move to the inbox" brings it back.
- **Keep** leaves the thing in the inbox and writes an event `kept`. The prompt does not ask about it for 90 days.

Why an event, and not a column "kept_at" on the item? The events are the history of the item, and the page shows them: "Kept in the inbox, in Telegram". A column would keep only the last answer.

## 4. Verdicts in the digest

"Right" and "Wrong" are under each score that the digest shows and that has no verdict yet. A verdict from a digest has the source `digest` ([note 33](33-labels-and-golden-set.md), part 3). As in note 33, the button carries the score that you saw, and a changed score is refused.

The random pick shows its score for the focus goal. This is the reason for the random slot (note 34, part 2). The rules show only high scores, so verdicts from them say only how often a high score is right. A verdict on the low score of the random pick says how often the model misses a useful thing.

A verdict in a digest asks no next question. That is the work of `/label`.

## A choice that we made

The contract of M0 has a job kind `stale_sweep` for the prompt. Why does the job of the digest send the prompt?

| | One job, two steps (ours) | Two jobs |
|---|---|---|
| The order of the messages | the prompt always comes after the digest | the queue decides, and the prompt can come first |
| A failure of the prompt | the job runs again, and step 1 is skipped | only the job of the prompt runs again |
| The code | one handler, one row with two marks | two handlers, and each needs its own mark |

The job kind `stale_sweep` stays unused.

## What a senior engineer checks here

- **Can a week get two digests?** No. One row for each week, one job for each row, and a mark for each message. A test runs the job two times.
- **Can a failed prompt send the digest again?** No. A test fails only the prompt, and the second try sends only the prompt. The items are not marked two times.
- **Does `/digest` change anything?** No. A test counts the digests and the events after `/digest`.
- **Can a stranger archive a thing?** No. Each button checks the owner, and a press must match a button that the message still has.
- **Does a button stay within 64 bytes?** Yes. Telegram gives a button at most 64 bytes of data, and a test uses the largest ids.
- **Can the tests fail?** I broke the code in 52 places, one at a time: 49 in Python and 3 in the web app. Before the run, I read the list of mistakes against the tests. The list showed a guard that no test could see, and I removed it: the 3-week rule already does its work. It also showed three missing tests: an event other than "kept" that blocks the prompt, and `/digest` with a new seed or a fixed reading time. With those tests, the run found all 52.
- **What is not done:**
  - `GET /digests`, the list of sent digests, is still a stub.
  - Only Telegram asks the stale question; the web app has no such page.
  - One reading time for each week: no reading time for each day.

## Try it yourself

1. After the pull request is merged, run `git pull`, and then `make up`.
2. Wait for Sunday 09:00. Or send a try now; the items of a try count as shown for 3 weeks:

```bash
docker compose exec worker unshelve digest send
```

3. Press "Right" or "Wrong", and "Archive" or "Keep". Then open the item on the web: the history shows each step.

## Exercise (15 minutes)

1. Open `apps/api/src/unshelve/resurface/weekly.py`, and go to `_send_stale_prompt`.
2. Remove these two lines:

```python
        if row.stale_sent_at is not None:
            return len(row.stale_item_ids)
```

3. Run the tests of the weekly digest in `apps/api`:

```bash
uv run pytest tests/resurface/test_weekly.py -q
```

Which test fails? The second try sends no message. Why is it still a problem?

<details>
<summary>The answer</summary>

```text
FAILED tests/resurface/test_weekly.py::test_a_job_that_runs_again_sends_neither_message_again
E       assert (1, None, 0) == (1, None, 1)
```

Without the mark, the second try chooses the stale items again. The first prompt asked about them a moment ago, so the rule "not asked in the last 3 weeks" leaves them out: the second try finds none, and it sends no message. But it writes an empty list over `stale_item_ids` of the row. Next week, the rule cannot see the first prompt any more, and the bot asks about the same things after one week, not after three.

So the mark protects the record of the prompt, not only the message.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Period key | The name of a period, for example `2026-W43`: one digest for each period |
| Idempotent | Doing it two times has the same effect as doing it one time |
| Mark | A time in the row (`sent_at`, `stale_sent_at`) that says that a message went out |
| Stale | A thing that waits in the inbox for 30 days or more, and that nothing brought back |
| Stale prompt | The message that asks about stale things: archive or keep? |
| Surfaced | Shown in a digest; the event of an item keeps the reason |

## To read more

- [Designing robust and predictable APIs with idempotency](https://stripe.com/blog/idempotency), Brandur Leach, Stripe. Read the part about the idempotency key: it is the same idea as the period key of a digest.

Previous: [34. Choosing a digest](34-choosing-a-digest.md) · Next: [36. Three retrievers](36-three-retrievers.md) · Back to the [list of notes](README.md)
