"""The sign of life of the worker (unshelve.alive): while it waits for a job, while a long job
runs, and when it stops. Docker checks the file (docs/learn/55)."""

import asyncio
import contextlib
import itertools
import os
from collections.abc import AsyncGenerator, Callable
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from tests.queue.test_worker import add, make_worker, row
from unshelve import alive
from unshelve.contracts.enums import JobKind, JobState
from unshelve.contracts.jobs import FetchItemPayload, JobContext
from unshelve.db.engine import create_engine, create_session_factory
from unshelve.queue.registry import Registry
from unshelve.queue.worker import Worker
from unshelve.testing.db import NO_DATABASE_URL

Sessions = async_sessionmaker[AsyncSession]
QUICK = timedelta(seconds=0.01)


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    place = tmp_path / "alive"
    monkeypatch.setenv("UNSHELVE_ALIVE_DIR", str(place))
    return place


@contextlib.asynccontextmanager
async def looping(worker: Worker, stop: asyncio.Event) -> AsyncGenerator[asyncio.Task[None]]:
    """``worker.run_forever(stop)`` as a task. A task that still runs at the end is cancelled,
    also when the test failed, so that no worker outlives its test database."""
    task = asyncio.create_task(worker.run_forever(stop))
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def until(condition: Callable[[], bool], seconds: float = 5) -> None:
    """Wait until ``condition()`` is true, at most ``seconds``. A file gives no event, so the
    test looks again every 10 ms."""
    try:
        async with asyncio.timeout(seconds):
            for _ in itertools.count():
                if condition():
                    return
                await asyncio.sleep(0.01)
    except TimeoutError:
        pytest.fail(f"not true after {seconds} s")


def fields(file: Path) -> dict[str, str]:
    lines = file.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)


@pytest.mark.db
async def test_the_worker_marks_while_the_queue_is_empty_and_writes_a_clean_stop(
    committed_db: Sessions, folder: Path
) -> None:
    stop = asyncio.Event()

    async with looping(make_worker(committed_db, Registry()), stop) as loop:
        await until((folder / "worker").exists)
        stop.set()
        await asyncio.wait_for(asyncio.shield(loop), timeout=5)

    written = fields(folder / "worker")
    assert written["pid"] == str(os.getpid())
    assert int(written["stopped"]) >= int(written["started"])


@pytest.mark.db
async def test_a_long_job_keeps_the_mark_new_through_the_keep_alive_of_its_lease(
    committed_db: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the lease of 0.3 s, the keep-alive runs every 0.1 s. In production the lease is 60 s,
    # so a job of 120 s still marks every 20 s.
    marks_in_the_job = 0
    running = False
    three = asyncio.Event()

    def record(name: str, **facts: str | int) -> bool:
        nonlocal marks_in_the_job
        assert (name, facts) == ("worker", {})
        if running:
            marks_in_the_job += 1
            if marks_in_the_job == 3:
                three.set()
        return True

    monkeypatch.setattr(alive, "mark", record)
    registry = Registry()

    @registry.handler(JobKind.FETCH_ITEM)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:  # pyright: ignore[reportUnusedFunction]
        nonlocal running
        running = True
        # The job ends only after three marks while it runs. Without them, it times out.
        await asyncio.wait_for(three.wait(), timeout=5)
        running = False

    job_id = await add(committed_db)

    await make_worker(committed_db, registry, lease=timedelta(seconds=0.3)).run_one()

    assert marks_in_the_job == 3
    assert (await row(committed_db, job_id)).state is JobState.DONE


async def test_the_worker_marks_also_while_the_database_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sign of life says that the loop turns. It does not say that the work succeeds: the
    # database has a healthcheck of its own.
    rounds = 0

    def record(name: str, **facts: str | int) -> bool:
        nonlocal rounds
        rounds += 1
        return True

    monkeypatch.setattr(alive, "mark", record)
    engine = create_engine(NO_DATABASE_URL)
    worker = make_worker(create_session_factory(engine), Registry(), poll_interval=QUICK)
    try:
        # Each round logs the error of the connection. The test keeps those lines.
        with capture_logs() as lines:
            async with looping(worker, asyncio.Event()):
                await until(lambda: rounds >= 3)
    finally:
        await engine.dispose()
    assert {line["event"] for line in lines} == {"worker.loop_error"}


@pytest.mark.db
async def test_a_worker_that_is_killed_writes_no_clean_stop(
    committed_db: Sessions, folder: Path
) -> None:
    async with looping(make_worker(committed_db, Registry()), asyncio.Event()) as loop:
        await until((folder / "worker").exists)
        loop.cancel()  # the nearest thing to `kill -9`: nothing is written on the way out

    assert "stopped" not in fields(folder / "worker")
