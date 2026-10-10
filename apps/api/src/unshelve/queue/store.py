"""The job queue: a table, and six functions that are the only code that changes job state.

Read docs/adr/0002 first. The model in one paragraph: a worker *claims* a job and gets a
*lease* on it for a limited time. While it works, it extends the lease with a *heartbeat*.
When it is done it calls *complete* or *fail*. If the worker dies, nobody extends the lease,
it expires, and the *reaper* gives the job to someone else.

Every function runs in the caller's transaction and does not commit. The caller commits.
That is what lets a handler write its result and enqueue the next step atomically.

Time: the queue has one clock, the database's ``now()``. Workers on different machines do not
agree on the time, and the lease arithmetic must. Tests pass ``now`` to move the clock.
"""

import dataclasses
from collections.abc import Sequence
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import JobKind, JobState
from unshelve.contracts.jobs import ClaimedJob, JobPayload
from unshelve.db.models import TIMESTAMP, Job


class LeaseLostError(Exception):
    """The caller no longer holds the job: it was reaped, and perhaps claimed by another worker.

    The right reaction is to stop and discard the result. The other worker's result wins.
    """

    def __init__(self, job: ClaimedJob) -> None:
        super().__init__(f"job {job.id} is no longer held by {job.locked_by!r}")
        self.job = job


def _now(now: datetime | None) -> sa.ColumnElement[datetime]:
    return sa.func.now() if now is None else sa.literal(now, TIMESTAMP)


def _held_by(job: ClaimedJob) -> sa.ColumnElement[bool]:
    """True for the row while ``job`` is still this worker's claim.

    ``attempts`` is the fencing token. It goes up by one on every claim, so a claim that was
    reaped and taken again, even by a worker with the same id, no longer matches. The clock is
    deliberately not part of this test: a lease that has run out but has not been reaped is
    still ours, and finishing the work is better than throwing it away.
    """
    return sa.and_(
        Job.id == job.id,
        Job.state == JobState.RUNNING,
        Job.locked_by == job.locked_by,
        Job.attempts == job.attempts,
    )


async def enqueue(
    session: AsyncSession,
    kind: JobKind,
    payload: JobPayload,
    *,
    owner_id: int | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    priority: int = 0,
    max_attempts: int = 5,
) -> int | None:
    """Add a job. Returns its id, or None when the same work is already waiting.

    "The same work" is a pending job with the same ``kind`` and ``dedupe_key``. A job that is
    running does not count: by the time it finishes, the data may have changed again.
    """
    statement = (
        postgresql.insert(Job)
        .values(
            owner_id=owner_id,
            kind=kind,
            payload=payload.model_dump(mode="json"),
            dedupe_key=dedupe_key,
            run_after=_now(run_after),
            priority=priority,
            max_attempts=max_attempts,
        )
        .on_conflict_do_nothing(
            index_elements=[Job.kind, Job.dedupe_key],
            index_where=sa.text("state = 'pending' AND dedupe_key IS NOT NULL"),
        )
        .returning(Job.id)
    )
    return await session.scalar(statement)


async def heartbeat(
    session: AsyncSession, job: ClaimedJob, *, lease: timedelta, now: datetime | None = None
) -> ClaimedJob:
    """Extend the lease to ``now + lease``. Raises ``LeaseLostError`` when it is not ours."""
    locked_until = await session.scalar(
        sa.update(Job)
        .where(_held_by(job))
        .values(locked_until=_now(now) + lease)
        .returning(Job.locked_until)
    )
    if locked_until is None:
        raise LeaseLostError(job)
    return dataclasses.replace(job, locked_until=locked_until)


# ------------------------------------------------------------------------------------------
# The four functions below are the M0-4 exercise. The tests in tests/queue/test_store.py are
# the specification: make them pass, one function at a time, in this order.
#
#     cd apps/api && uv run pytest tests/queue -x
#
# Use ``_now(now)`` wherever you need the current time, and ``_held_by(job)`` to test that a
# claim is still valid. The CHECK constraints on ``jobs`` will tell you when a transition
# leaves the row in an impossible state.
# ------------------------------------------------------------------------------------------


async def claim(
    session: AsyncSession,
    *,
    worker_id: str,
    lease: timedelta,
    kinds: Sequence[JobKind] | None = None,
    now: datetime | None = None,
) -> ClaimedJob | None:
    """Take the next runnable job and return it, or None when there is nothing to do.

    Runnable: ``pending``, ``run_after <= now``, and of one of ``kinds`` (any kind when None).
    Order: lowest ``priority`` first, then earliest ``run_after``, then lowest ``id``.

    The claimed row becomes ``running`` with ``attempts + 1``, ``locked_by = worker_id`` and
    ``locked_until = now + lease``.

    Two workers that call this at the same moment must get two different jobs, and neither
    may wait for the other. That is what ``FOR UPDATE SKIP LOCKED`` is for: select one row id
    with it, then update that row. Think about what happens without ``SKIP LOCKED`` (the
    second worker waits, then finds the row already taken) and without ``FOR UPDATE`` (both
    workers take the same row).
    """
    raise NotImplementedError("M0-4 exercise")


async def complete(session: AsyncSession, job: ClaimedJob, *, now: datetime | None = None) -> None:
    """Mark the job ``done``, set ``finished_at`` and release the lease.

    Raises ``LeaseLostError`` when the claim is no longer valid. An UPDATE with
    ``_held_by(job)`` in its WHERE clause does the check and the change in one statement, so
    there is no gap between them for another worker to slip into. How do you know whether the
    UPDATE matched a row?
    """
    raise NotImplementedError("M0-4 exercise")


async def fail(
    session: AsyncSession,
    job: ClaimedJob,
    error: str,
    *,
    retry_after: timedelta | None,
    now: datetime | None = None,
) -> JobState:
    """Record a failed attempt and return the job's new state.

    * ``retry_after`` is None: the failure is permanent. The job is ``dead``.
    * The job has used all its attempts (``attempts >= max_attempts``): ``dead``.
    * Otherwise: ``pending`` again, with ``run_after = now + retry_after``.

    In every case ``last_error`` is set and the lease is released. A dead job also gets
    ``finished_at``. Raises ``LeaseLostError`` when the claim is no longer valid.
    """
    raise NotImplementedError("M0-4 exercise")


async def reap_expired(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Take back every job whose lease has run out. Returns how many.

    A ``running`` job with ``locked_until < now`` belongs to a worker that died or hung. It
    goes back to ``pending`` (runnable at once), or to ``dead`` when its attempts are used up,
    with ``last_error`` saying that the lease expired. The lease is released in both cases.

    Several workers may run the reaper at the same time. Make sure that this is harmless.
    """
    raise NotImplementedError("M0-4 exercise")
