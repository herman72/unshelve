# 4. The job queue

> Pull request: [#47](https://github.com/herman72/unshelve/pull/47) · Code: [`apps/api/src/unshelve/queue/store.py`](../../apps/api/src/unshelve/queue/store.py) · Tests: [`apps/api/tests/queue/test_store.py`](../../apps/api/tests/queue/test_store.py) · Interactive: [job queue simulator](../queue-simulator.html)

## In one minute

When you save a link, Unshelve must download the page and ask an AI model for a summary. That is slow, and it can fail. So this work is not done at once. It is written down as a *job*: one row in a table. A *worker* (a program that runs in the background) takes the jobs one by one.

Four small functions change a job row: `claim`, `complete`, `fail` and `reap_expired`. Together they make two promises:

1. Every job is done, also when a worker dies in the middle.
2. Two workers never do the same job at the same time.

Open the [simulator](../queue-simulator.html) next to this note. It shows each idea below as a story that you play step by step.

## 1. A job is one row in a table

```text
id | state   | attempts | run_after | locked_by | locked_until | last_error
1  | pending | 0 of 3   | 12:00:00  | –         | –            | –
```

| Column | Meaning |
|---|---|
| `state` | `pending` (waits), `running` (a worker has it), `done`, or `dead` (gave up) |
| `attempts` | How many times a worker took this job |
| `run_after` | Do not start before this time |
| `locked_by`, `locked_until` | Which worker has the job, and until when |
| `last_error` | What went wrong the last time |

## 2. `claim`: a worker takes the next job

Worker `w1` asks for a job at 12:00:00.

```text
before | 1 | pending | 0 of 3 | –  | –
after  | 1 | running | 1 of 3 | w1 | 12:01:00
```

Three things changed: the state, the attempt counter, and the lock.

**The hard question:** two workers ask at the same moment, and there is one job. What must happen?

| How the query is written | What happens | Good? |
|---|---|---|
| A normal `SELECT`, then an `UPDATE` | Both workers read "job 1 is pending". Both take it. The job runs two times at once. | No |
| `SELECT ... FOR UPDATE` | The second worker waits until the first one is finished. Then it finds that the job is gone. | It works, but workers wait in a line |
| `SELECT ... FOR UPDATE SKIP LOCKED` | The second worker jumps over job 1 and takes job 2 at once. If there is no job 2, it gets "nothing" at once. | Yes |

**A comparison from daily life:** two cashiers call "next customer, please". Each customer goes to one cashier only. A cashier does not wait for a customer who is already at the other cash register. She calls the next one.

## 3. The lease: a lock with an end time

`locked_until` is the important column. The worker does not get the job for ever. It gets it for 60 seconds. This is called a *lease*.

- A worker that is alive asks for more time, again and again. This is a *heartbeat*.
- A worker that died asks for nothing. At 12:01:00 the lease ends.

**A comparison from daily life:** a book from a library. You get it for three weeks. You can extend the loan. If you do nothing, the loan ends, and the library can give the book to the next person.

Why not a normal lock without an end? Because a dead worker cannot open its lock. The job would stay "running" for ever.

## 4. `reap_expired`: take back the jobs of dead workers

Nobody tells the database that a worker died. So a small routine, the *reaper*, looks at the table from time to time:

```text
"Which jobs are running, and their lease ended before now?"
```

It gives these jobs back:

```text
before | 1 | running | 1 of 3 | w1 | 12:01:00      (it is now 12:01:10)
after  | 1 | pending | 1 of 3 | –  | –             last_error: the lease expired
```

`attempts` stays at 1. The lost attempt counts. When a job has no attempts left, the reaper marks it `dead`, and a person must look at it.

## 5. The ticket: a slow worker cannot destroy the work of another worker

This is the hardest idea. A worker can be slow without being dead. Here is the story with times:

```text
12:00:00  w1 takes job 1                 attempts = 1.  w1 remembers: "my ticket is attempt 1"
12:00:05  w1 freezes (a long pause)
12:01:10  the reaper gives the job back  pending
12:01:11  w2 takes job 1                 attempts = 2.  w2 remembers: "my ticket is attempt 2"
12:01:30  w1 wakes up and says "done!"   with the ticket "attempt 1"
```

If we accept the message of `w1`, the job is marked `done` while `w2` is still working on it. So each write must show its ticket:

```sql
UPDATE jobs SET state = 'done', ...
WHERE id = 1 AND state = 'running'
  AND locked_by = 'w1' AND attempts = 1      -- the ticket check
```

The row now says `locked_by = 'w2'` and `attempts = 2`. No row matches. Nothing changes. `w1` gets the error `LeaseLostError` and throws its result away.

In the code, this check is the function `_held_by`. The technical name for the ticket is *fencing token*.

**A comparison from daily life:** a cloakroom. You get a number for your coat. If you come back with an old number from last week, you do not get the coat that hangs there now.

## 6. `fail`: try again later, or stop

A worker reports a failure. Two things can happen.

| Situation | The job becomes | Example |
|---|---|---|
| It can work later | `pending`, with `run_after` in the future | The website answered "busy". Try again in 30 seconds. |
| It can never work, or no attempts are left | `dead` | The page does not exist. |

`run_after` in the future is the reason that no worker takes the job immediately again.

## 7. The database itself refuses an impossible row

The table has this rule:

```text
state is 'running'   exactly when   locked_by and locked_until are filled
```

Example: you write `complete` and forget to empty `locked_by`. The database refuses the change, and the test shows the name of the rule: `ck_jobs_running_has_lease`. Without the rule, the mistake would stay hidden for weeks.

## What a senior engineer checks here

- **Which clock is used?** Only the clock of the database (`now()`). Example: the clock of worker A is 30 seconds fast. With its own clock, A would think that a lease of B has ended, and it would take a job that B still holds. With one clock, all workers agree.
- **Can a job run two times?** Yes: a worker can die after the work and before `complete`. The queue cannot prevent this. So each job must be safe to repeat (see [note 5](05-worker-loop.md)).
- **Do the tests fail when the code is wrong?** Try the exercise.

## Exercise (15 minutes)

1. In `claim`, remove `.with_for_update(skip_locked=True)`.
2. Run `cd apps/api && uv run pytest tests/queue`.
3. Two tests fail. Read their names. Explain in one sentence each why they fail.
4. Put the line back in your editor (undo). Run the tests again, and make sure that all of them pass.

Then open the simulator, choose "Free play", and make a refusal happen with your own clicks.

## Words used in this note

| Word | Meaning |
|---|---|
| Job | One piece of slow work, stored as a row |
| Worker | A program in the background that takes jobs and does them |
| Claim | To take a job, so that no other worker takes it |
| Lease | A lock with an end time |
| Heartbeat | A short message "I am alive" that moves the end of the lease |
| Reaper | The routine that gives back the jobs of dead workers |
| Fencing token | A number that goes up with each claim. An old number is refused. Here it is `attempts`. |
| At-least-once | Each job runs one time or more, never zero times |

## To read more

- [PostgreSQL manual, "The Locking Clause"](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE). Read the paragraph about `SKIP LOCKED`.
- [How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) by Martin Kleppmann. Read "Making the lock safe with fencing tokens". It has a picture of the story in part 5.
- [Amazon SQS visibility timeout](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html). The same lease idea in a well-known product.

Previous: [3. The database schema](03-database-schema.md) · Next: [5. The worker loop](05-worker-loop.md)
