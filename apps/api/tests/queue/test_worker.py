"""The worker loop: what happens around a handler. The store tests cover the rows themselves."""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.config import Settings
from unshelve.contracts.enums import JobKind, JobState
from unshelve.contracts.jobs import FetchItemPayload, JobContext, Permanent, Retry, Services
from unshelve.db.models import Job
from unshelve.queue import store
from unshelve.queue.registry import Registry
from unshelve.queue.worker import Worker, WorkerConfig, backoff
from unshelve.testing.fakes import FakeEmbedder, FakeFetcher, FakeLLM, InMemoryBlobStore

pytestmark = pytest.mark.db

Sessions = async_sessionmaker[AsyncSession]
SECOND = timedelta(seconds=1)


def make_worker(
    sessions: Sessions, registry: Registry, name: str = "w1", **config: timedelta
) -> Worker:
    services = Services(
        settings=Settings(env="test"),
        sessions=sessions,
        fetcher=FakeFetcher(),
        llm=FakeLLM(),
        embedder=FakeEmbedder(),
        blobs=InMemoryBlobStore(),
    )
    return Worker(services, registry, WorkerConfig(worker_id=name, **config))


async def add(sessions: Sessions, item_id: int = 1, *, max_attempts: int = 5) -> int:
    async with sessions() as session:
        job_id = await store.enqueue(
            session,
            JobKind.FETCH_ITEM,
            FetchItemPayload(item_id=item_id),
            max_attempts=max_attempts,
        )
        await session.commit()
    assert job_id is not None
    return job_id


async def row(sessions: Sessions, job_id: int) -> Job:
    async with sessions() as session:
        job = await session.get(Job, job_id)
    assert job is not None
    return job


async def give_to_another_worker(sessions: Sessions, job_id: int) -> None:
    """What the row looks like after the reaper took the job back and another worker claimed it."""
    async with sessions() as session:
        await session.execute(
            sa.update(Job)
            .where(Job.id == job_id)
            .values(locked_by="other", attempts=Job.attempts + 1)
        )
        await session.commit()


# --- The normal case ---------------------------------------------------------------------


async def test_a_job_runs_and_is_completed(committed_db: Sessions) -> None:
    registry = Registry()
    seen: list[tuple[int, int]] = []

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        seen.append((payload.item_id, ctx.job.attempts))

    job_id = await add(committed_db, item_id=7)

    assert await make_worker(committed_db, registry).run_one() is True

    assert seen == [(7, 1)]  # the handler got a typed payload, and this was attempt 1
    job = await row(committed_db, job_id)
    assert (job.state, job.locked_by, job.last_error) == (JobState.DONE, None, None)


async def test_an_empty_queue_is_not_an_error(committed_db: Sessions) -> None:
    assert await make_worker(committed_db, Registry()).run_one() is False


async def test_a_worker_takes_only_the_kinds_it_has_a_handler_for(committed_db: Sessions) -> None:
    job_id = await add(committed_db)  # a fetch_item job, but this worker has no handler at all

    assert await make_worker(committed_db, Registry()).run_one() is False
    assert (await row(committed_db, job_id)).state is JobState.PENDING


# --- How a handler reports a problem -----------------------------------------------------


async def test_retry_puts_the_job_back_for_later(committed_db: Sessions) -> None:
    registry = Registry()

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        raise Retry("the site answered 503", after=timedelta(minutes=5))

    job_id = await add(committed_db)
    worker = make_worker(committed_db, registry)

    await worker.run_one()

    job = await row(committed_db, job_id)
    assert (job.state, job.last_error, job.attempts) == (
        JobState.PENDING,
        "the site answered 503",
        1,
    )
    assert job.run_after > datetime.now(UTC) + timedelta(minutes=4)
    assert await worker.run_one() is False  # it is too early to take it again


async def test_permanent_ends_the_job_at_once(committed_db: Sessions) -> None:
    registry = Registry()

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        raise Permanent("the item does not exist")

    job_id = await add(committed_db)

    await make_worker(committed_db, registry).run_one()

    job = await row(committed_db, job_id)
    assert (job.state, job.last_error, job.attempts) == (
        JobState.DEAD,
        "the item does not exist",
        1,
    )


async def test_an_unexpected_error_is_tried_again_until_the_attempts_are_used_up(
    committed_db: Sessions,
) -> None:
    registry = Registry()

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        raise ValueError("boom")

    job_id = await add(committed_db, max_attempts=2)
    worker = make_worker(committed_db, registry, backoff_base=timedelta(0))  # no wait in the test

    await worker.run_one()
    first = await row(committed_db, job_id)
    await worker.run_one()
    second = await row(committed_db, job_id)

    assert (first.state, first.last_error) == (JobState.PENDING, "ValueError: boom")
    assert (second.state, second.attempts) == (JobState.DEAD, 2)


async def test_a_payload_of_the_wrong_shape_is_dead_and_the_handler_does_not_run(
    committed_db: Sessions,
) -> None:
    registry = Registry()
    ran = False

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        nonlocal ran
        ran = True

    async with committed_db() as session:
        job = Job(kind=JobKind.FETCH_ITEM, payload={"wrong_name": 1})
        session.add(job)
        await session.commit()

    await make_worker(committed_db, registry).run_one()

    stored = await row(committed_db, job.id)
    assert not ran
    assert stored.state is JobState.DEAD
    assert stored.last_error is not None
    assert stored.last_error.startswith("invalid payload")


async def test_a_handler_that_takes_too_long_is_stopped_and_tried_again(
    committed_db: Sessions,
) -> None:
    registry = Registry()

    @registry.handler(JobKind.FETCH_ITEM, timeout_s=0.05)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        await asyncio.sleep(30)

    job_id = await add(committed_db)

    await asyncio.wait_for(make_worker(committed_db, registry).run_one(), timeout=5)

    job = await row(committed_db, job_id)
    assert (job.state, job.last_error) == (JobState.PENDING, "timed out after 0.05 s")


# --- The lease ---------------------------------------------------------------------------


async def test_the_lease_is_extended_while_the_handler_runs(committed_db: Sessions) -> None:
    registry = Registry()
    lease_ends: list[datetime] = []

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        lease_ends.append(ctx.job.locked_until)
        await asyncio.sleep(0.5)  # longer than the lease of 0.3 s
        current = (await row(committed_db, ctx.job.id)).locked_until
        assert current is not None
        lease_ends.append(current)

    job_id = await add(committed_db)

    await make_worker(committed_db, registry, lease=timedelta(seconds=0.3)).run_one()

    at_claim, later = lease_ends
    assert later > at_claim  # heartbeats moved the end of the lease
    job = await row(committed_db, job_id)
    assert (job.state, job.attempts) == (JobState.DONE, 1)


async def test_a_worker_that_lost_its_job_cannot_write_the_result(committed_db: Sessions) -> None:
    registry = Registry()

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        await give_to_another_worker(committed_db, ctx.job.id)

    job_id = await add(committed_db)

    await make_worker(committed_db, registry).run_one()  # must not raise

    job = await row(committed_db, job_id)
    assert (job.state, job.locked_by, job.attempts) == (JobState.RUNNING, "other", 2)


async def test_a_lost_lease_stops_the_handler_early(committed_db: Sessions) -> None:
    registry = Registry()
    finished = False

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        nonlocal finished
        await give_to_another_worker(committed_db, ctx.job.id)
        await asyncio.sleep(30)  # the next heartbeat sees the loss and stops this
        finished = True

    await add(committed_db)
    worker = make_worker(committed_db, registry, lease=timedelta(seconds=0.15))

    await asyncio.wait_for(worker.run_one(), timeout=5)

    assert not finished


async def test_a_killed_worker_leaves_the_job_for_the_reaper(committed_db: Sessions) -> None:
    registry = Registry()
    started = asyncio.Event()
    runs = 0

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        nonlocal runs
        runs += 1
        started.set()
        if runs == 1:
            await asyncio.sleep(30)  # the first worker is killed during this

    job_id = await add(committed_db)

    first = asyncio.create_task(make_worker(committed_db, registry, "first").run_one())
    await asyncio.wait_for(started.wait(), timeout=5)
    first.cancel()  # the nearest thing to `kill -9`: nothing is written on the way out
    with pytest.raises(asyncio.CancelledError):
        await first
    orphan = await row(committed_db, job_id)

    async with committed_db() as session:  # later, the lease has ended
        reaped = await store.reap_expired(session, now=datetime.now(UTC) + timedelta(hours=1))
        await session.commit()
    await make_worker(committed_db, registry, "second").run_one()

    assert (orphan.state, orphan.locked_by) == (JobState.RUNNING, "first")
    assert reaped == 1
    job = await row(committed_db, job_id)
    assert (job.state, job.attempts, runs) == (JobState.DONE, 2, 2)


# --- Many workers, and the loop ----------------------------------------------------------


async def test_four_workers_run_two_hundred_jobs_once_each(committed_db: Sessions) -> None:
    registry = Registry()
    runs: Counter[int] = Counter()

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        runs[payload.item_id] += 1
        await asyncio.sleep(0)

    async with committed_db() as session:
        for item_id in range(200):
            await store.enqueue(session, JobKind.FETCH_ITEM, FetchItemPayload(item_id=item_id))
        await session.commit()

    async def drain(name: str) -> None:
        worker = make_worker(committed_db, registry, name)
        while await worker.run_one():
            pass

    await asyncio.gather(*(drain(f"w{n}") for n in range(4)))

    assert len(runs) == 200
    assert set(runs.values()) == {1}


async def test_run_forever_works_until_it_is_told_to_stop(committed_db: Sessions) -> None:
    registry = Registry()
    done = asyncio.Event()
    seen: set[int] = set()

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        seen.add(payload.item_id)
        if len(seen) == 3:
            done.set()

    stop = asyncio.Event()
    worker = make_worker(committed_db, registry, poll_interval=timedelta(seconds=0.01))
    loop = asyncio.create_task(worker.run_forever(stop))
    for item_id in (1, 2, 3):  # the jobs arrive while the worker is already waiting
        await add(committed_db, item_id)

    await asyncio.wait_for(done.wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(loop, timeout=5)

    assert seen == {1, 2, 3}


# --- Waiting between attempts ------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        (1, 10 * SECOND),
        (2, 20 * SECOND),
        (3, 40 * SECOND),
        (4, 60 * SECOND),
        (10, 60 * SECOND),
        # A job of 300 tries (the collection of a batch): 2 ** 299 x 10 s fits no timedelta.
        (300, 60 * SECOND),
    ],
)
def test_backoff_doubles_and_stops_at_the_cap(attempts: int, expected: timedelta) -> None:
    assert backoff(attempts, base=10 * SECOND, cap=60 * SECOND) == expected
