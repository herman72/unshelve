"""The specification of the job queue. Read it top to bottom: it tells the story of a job."""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.contracts.enums import JobKind, JobState
from unshelve.contracts.jobs import ClaimedJob, EnrichItemPayload, FetchItemPayload
from unshelve.db.models import Job
from unshelve.queue import store
from unshelve.queue.store import LeaseLostError

pytestmark = pytest.mark.db

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
LEASE = timedelta(seconds=60)


def at(seconds: float) -> datetime:
    """A moment ``seconds`` after the start of the test's clock."""
    return T0 + timedelta(seconds=seconds)


async def add(db: AsyncSession, item_id: int = 1, **options: object) -> int:
    options.setdefault("run_after", T0)
    job_id = await store.enqueue(
        db,
        JobKind.FETCH_ITEM,
        FetchItemPayload(item_id=item_id),
        **options,  # pyright: ignore[reportArgumentType]
    )
    assert job_id is not None
    return job_id


async def take(db: AsyncSession, worker: str = "w1", now: datetime = T0) -> ClaimedJob:
    job = await store.claim(db, worker_id=worker, lease=LEASE, now=now)
    assert job is not None
    return job


async def row(db: AsyncSession, job_id: int) -> Job:
    job = await db.get(Job, job_id, populate_existing=True)
    assert job is not None
    return job


# --- enqueue (already implemented) -------------------------------------------------------


async def test_a_new_job_waits_with_no_attempts(db: AsyncSession) -> None:
    job = await row(db, await add(db, item_id=7))

    assert job.state is JobState.PENDING
    assert job.attempts == 0
    assert job.payload == {"item_id": 7}
    assert job.locked_by is None


async def test_the_same_work_is_enqueued_once_while_it_waits(db: AsyncSession) -> None:
    first = await add(db, dedupe_key="item:1")

    again = await store.enqueue(
        db, JobKind.FETCH_ITEM, FetchItemPayload(item_id=1), dedupe_key="item:1"
    )
    other_kind = await store.enqueue(
        db, JobKind.ENRICH_ITEM, EnrichItemPayload(item_id=1), dedupe_key="item:1"
    )

    assert again is None
    assert other_kind not in (None, first)


async def test_a_job_with_a_delay_starts_later(db: AsyncSession) -> None:
    job_id = await add(db, delay=timedelta(seconds=5))

    assert (await row(db, job_id)).run_after == at(5)
    assert await store.claim(db, worker_id="w1", lease=LEASE, now=at(4)) is None
    assert (await take(db, now=at(5))).id == job_id


# --- claim -------------------------------------------------------------------------------


async def test_claiming_from_an_empty_queue_returns_none(db: AsyncSession) -> None:
    assert await store.claim(db, worker_id="w1", lease=LEASE, now=T0) is None


async def test_a_claim_starts_a_lease_and_counts_an_attempt(db: AsyncSession) -> None:
    job_id = await add(db, item_id=7)

    claimed = await take(db, "w1", now=at(5))

    assert claimed.id == job_id
    assert claimed.kind is JobKind.FETCH_ITEM
    assert claimed.payload == {"item_id": 7}
    assert (claimed.attempts, claimed.locked_by) == (1, "w1")
    assert claimed.locked_until == at(5) + LEASE
    stored = await row(db, job_id)
    assert stored.state is JobState.RUNNING
    assert (stored.attempts, stored.locked_by, stored.locked_until) == (1, "w1", at(65))


async def test_a_claimed_job_is_not_claimed_again(db: AsyncSession) -> None:
    await add(db)
    await take(db, "w1")

    assert await store.claim(db, worker_id="w2", lease=LEASE, now=T0) is None


async def test_jobs_are_claimed_by_priority_then_age_then_id(db: AsyncSession) -> None:
    late = await add(db, 1, run_after=at(-10))
    early = await add(db, 2, run_after=at(-30))
    early_twin = await add(db, 3, run_after=at(-30))
    urgent = await add(db, 4, run_after=at(-1), priority=-1)

    order = [(await take(db)).id for _ in range(4)]

    assert order == [urgent, early, early_twin, late]


async def test_a_job_is_not_claimed_before_its_time(db: AsyncSession) -> None:
    job_id = await add(db, run_after=at(30))

    assert await store.claim(db, worker_id="w1", lease=LEASE, now=at(29)) is None
    assert (await take(db, now=at(30))).id == job_id


async def test_a_worker_can_claim_only_some_kinds(db: AsyncSession) -> None:
    await add(db)
    enrich = await store.enqueue(
        db, JobKind.ENRICH_ITEM, EnrichItemPayload(item_id=1), run_after=T0
    )

    claimed = await store.claim(
        db, worker_id="w1", lease=LEASE, kinds=[JobKind.ENRICH_ITEM], now=T0
    )

    assert claimed is not None
    assert claimed.id == enrich
    assert (
        await store.claim(db, worker_id="w1", lease=LEASE, kinds=[JobKind.ENRICH_ITEM], now=T0)
        is None
    )


# --- complete ----------------------------------------------------------------------------


async def test_completing_a_job_finishes_it_and_releases_the_lease(db: AsyncSession) -> None:
    job_id = await add(db)
    claimed = await take(db)

    await store.complete(db, claimed, now=at(3))

    stored = await row(db, job_id)
    assert stored.state is JobState.DONE
    assert stored.finished_at == at(3)
    assert (stored.locked_by, stored.locked_until) == (None, None)


async def test_a_job_cannot_be_completed_twice(db: AsyncSession) -> None:
    await add(db)
    claimed = await take(db)
    await store.complete(db, claimed, now=at(3))

    with pytest.raises(LeaseLostError):
        await store.complete(db, claimed, now=at(4))


# --- fail --------------------------------------------------------------------------------


async def test_a_failed_job_waits_and_then_runs_again(db: AsyncSession) -> None:
    job_id = await add(db)
    claimed = await take(db)

    state = await store.fail(db, claimed, "timeout", retry_after=timedelta(seconds=30), now=at(2))

    assert state is JobState.PENDING
    stored = await row(db, job_id)
    assert (stored.state, stored.last_error, stored.run_after) == (
        JobState.PENDING,
        "timeout",
        at(32),
    )
    assert (stored.locked_by, stored.locked_until, stored.finished_at) == (None, None, None)
    assert await store.claim(db, worker_id="w1", lease=LEASE, now=at(31)) is None
    assert (await take(db, now=at(32))).attempts == 2


async def test_a_job_that_used_all_its_attempts_is_dead(db: AsyncSession) -> None:
    job_id = await add(db, max_attempts=2)
    first = await take(db)
    await store.fail(db, first, "first", retry_after=timedelta(0), now=at(1))
    second = await take(db, now=at(2))

    state = await store.fail(db, second, "second", retry_after=timedelta(0), now=at(3))

    assert state is JobState.DEAD
    stored = await row(db, job_id)
    assert (stored.state, stored.last_error, stored.finished_at) == (
        JobState.DEAD,
        "second",
        at(3),
    )
    assert (stored.locked_by, stored.locked_until) == (None, None)
    assert await store.claim(db, worker_id="w1", lease=LEASE, now=at(100)) is None


async def test_a_permanent_failure_is_dead_at_once(db: AsyncSession) -> None:
    job_id = await add(db, max_attempts=5)
    claimed = await take(db)

    state = await store.fail(db, claimed, "the item does not exist", retry_after=None, now=at(1))

    assert state is JobState.DEAD
    assert (await row(db, job_id)).attempts == 1


# --- reap_expired: what happens when a worker dies ---------------------------------------


async def test_an_expired_lease_is_reaped_and_the_job_runs_again(db: AsyncSession) -> None:
    job_id = await add(db)
    await take(db, "w1", now=T0)  # the worker dies here: no heartbeat, no complete

    assert await store.reap_expired(db, now=at(60)) == 0  # the lease ends at 60, not before
    assert await store.reap_expired(db, now=at(61)) == 1

    stored = await row(db, job_id)
    assert stored.state is JobState.PENDING
    assert (stored.locked_by, stored.locked_until) == (None, None)
    assert stored.last_error is not None
    assert "lease" in stored.last_error
    second = await take(db, "w2", now=at(61))
    assert (second.id, second.attempts, second.locked_by) == (job_id, 2, "w2")


async def test_reaping_leaves_live_leases_and_other_states_alone(db: AsyncSession) -> None:
    await add(db, 1)
    await add(db, 2)
    await add(db, 3)
    expired = await take(db, "w1", now=T0)
    alive = await take(db, "w2", now=at(30))

    assert await store.reap_expired(db, now=at(70)) == 1

    assert (await row(db, expired.id)).state is JobState.PENDING
    assert (await row(db, alive.id)).state is JobState.RUNNING
    assert await store.reap_expired(db, now=at(70)) == 0  # running it again changes nothing


async def test_a_job_whose_last_attempt_expired_is_dead(db: AsyncSession) -> None:
    job_id = await add(db, max_attempts=1)
    await take(db, now=T0)

    assert await store.reap_expired(db, now=at(61)) == 1

    stored = await row(db, job_id)
    assert stored.state is JobState.DEAD
    assert stored.finished_at == at(61)


# --- fencing: a worker that lost its lease cannot write ----------------------------------


async def test_a_worker_that_was_replaced_cannot_complete_or_fail(db: AsyncSession) -> None:
    job_id = await add(db)
    slow = await take(db, "slow", now=T0)
    await store.reap_expired(db, now=at(61))
    fast = await take(db, "fast", now=at(61))

    with pytest.raises(LeaseLostError):
        await store.complete(db, slow, now=at(62))
    with pytest.raises(LeaseLostError):
        await store.fail(db, slow, "late", retry_after=timedelta(0), now=at(62))

    stored = await row(db, job_id)
    assert (stored.state, stored.locked_by) == (JobState.RUNNING, "fast")
    await store.complete(db, fast, now=at(63))  # the new holder is not affected


async def test_the_same_worker_id_does_not_revive_an_old_claim(db: AsyncSession) -> None:
    await add(db)
    old = await take(db, "w1", now=T0)
    await store.reap_expired(db, now=at(61))
    await take(db, "w1", now=at(61))  # the same id, restarted: a different claim

    with pytest.raises(LeaseLostError):
        await store.complete(db, old, now=at(62))


async def test_an_expired_lease_that_was_not_reaped_can_still_finish(db: AsyncSession) -> None:
    # Nobody else took the job, so the finished work is kept. The fencing token decides,
    # not the clock.
    job_id = await add(db)
    claimed = await take(db, now=T0)

    await store.complete(db, claimed, now=at(500))

    assert (await row(db, job_id)).state is JobState.DONE


# --- heartbeat (already implemented) -----------------------------------------------------


def _running(worker: str, locked_until: datetime) -> Job:
    return Job(
        kind=JobKind.FETCH_ITEM,
        payload={"item_id": 1},
        state=JobState.RUNNING,
        attempts=1,
        locked_by=worker,
        locked_until=locked_until,
    )


def _claim_of(job: Job) -> ClaimedJob:
    assert job.locked_by is not None
    assert job.locked_until is not None
    return ClaimedJob(
        id=job.id,
        kind=job.kind,
        payload=job.payload,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        locked_by=job.locked_by,
        locked_until=job.locked_until,
    )


async def test_a_heartbeat_extends_the_lease(db: AsyncSession) -> None:
    job = _running("w1", at(60))
    db.add(job)
    await db.flush()
    await db.refresh(job)

    renewed = await store.heartbeat(db, _claim_of(job), lease=LEASE, now=at(50))

    assert renewed.locked_until == at(110)
    assert (await row(db, job.id)).locked_until == at(110)


async def test_a_heartbeat_on_a_lost_lease_raises(db: AsyncSession) -> None:
    job = _running("w1", at(60))
    db.add(job)
    await db.flush()
    await db.refresh(job)
    claim = _claim_of(job)
    await db.execute(sa.update(Job).where(Job.id == job.id).values(locked_by="w2", attempts=2))

    with pytest.raises(LeaseLostError):
        await store.heartbeat(db, claim, lease=LEASE, now=at(50))


# --- concurrency: real commits on several connections ------------------------------------


async def test_a_second_worker_skips_a_row_that_is_being_claimed(
    committed_db: async_sessionmaker[AsyncSession],
) -> None:
    async with committed_db() as setup:
        await add(setup)
        await setup.commit()

    async with committed_db() as first, committed_db() as second:
        assert await store.claim(first, worker_id="w1", lease=LEASE, now=T0) is not None
        # `first` has not committed. `second` must return at once with nothing: it neither
        # waits for the row lock nor takes the same job.
        other = await asyncio.wait_for(
            store.claim(second, worker_id="w2", lease=LEASE, now=T0), timeout=2
        )
        assert other is None
        await first.commit()


async def test_four_workers_run_two_hundred_jobs_exactly_once(
    committed_db: async_sessionmaker[AsyncSession],
) -> None:
    async with committed_db() as setup:
        for item_id in range(200):
            await add(setup, item_id)
        await setup.commit()

    runs: Counter[int] = Counter()

    async def worker(name: str) -> None:
        while True:
            async with committed_db() as session:
                job = await store.claim(session, worker_id=name, lease=LEASE, now=T0)
                await session.commit()
            if job is None:
                return
            runs[job.id] += 1
            await asyncio.sleep(0)  # let the other workers interleave
            async with committed_db() as session:
                await store.complete(session, job, now=T0)
                await session.commit()

    await asyncio.gather(*(worker(f"w{n}") for n in range(4)))

    assert len(runs) == 200
    assert set(runs.values()) == {1}
    async with committed_db() as check:
        done = await check.scalar(
            sa.select(sa.func.count()).select_from(Job).where(Job.state == JobState.DONE)
        )
    assert done == 200
