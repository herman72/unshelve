"""The worker loop: take a job, run its handler, report the result. Then do it again.

``store.py`` knows how to change one job row safely. This module decides *when* to call it:

1. ``claim`` a job. If there is none, sleep a little.
2. Run the handler. While it runs, send a heartbeat so the lease does not end.
3. The handler finished: ``complete``. It failed: ``fail``, with a wait before the next try.
4. From time to time, run the reaper for jobs whose worker died.

To see these steps move, open docs/queue-simulator.html in a browser.
"""

import asyncio
import contextlib
import os
import secrets
import socket
import time
from dataclasses import dataclass
from datetime import timedelta

import structlog
from pydantic import ValidationError

from unshelve.contracts.enums import JobState
from unshelve.contracts.jobs import ClaimedJob, JobContext, Permanent, Retry, Services
from unshelve.queue import store
from unshelve.queue.registry import HandlerSpec, Registry
from unshelve.queue.store import LeaseLostError

log = structlog.get_logger()

MAX_ERROR_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    worker_id: str
    lease: timedelta = timedelta(seconds=60)
    poll_interval: timedelta = timedelta(seconds=1)  # sleep this long when the queue is empty
    reap_interval: timedelta = timedelta(seconds=15)
    backoff_base: timedelta = timedelta(seconds=10)
    backoff_cap: timedelta = timedelta(minutes=10)


def new_worker_id() -> str:
    """A name that is different for each process, and that a person can read in the table."""
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(2)}"


def backoff(attempts: int, *, base: timedelta, cap: timedelta) -> timedelta:
    """How long to wait before the next try: base, 2 x base, 4 x base, ... but never over cap.

    A service that is down needs time to come back. Trying again at once only adds load.
    """
    # In seconds, as a float: a timedelta cannot hold 2 ** 300 times 10 seconds, and a job can
    # have 300 tries (the collection of a batch).
    seconds = base.total_seconds() * 2.0 ** min(max(attempts - 1, 0), 1000)
    return min(timedelta(seconds=min(seconds, cap.total_seconds())), cap)


@dataclass(frozen=True, slots=True)
class _Failure:
    error: str
    retry_after: timedelta | None  # None: do not try again


class Worker:
    def __init__(self, services: Services, registry: Registry, config: WorkerConfig) -> None:
        self._services = services
        self._registry = registry
        self._config = config

    # --- The loop ------------------------------------------------------------------------

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Work until ``stop`` is set. A job that is running at that moment is finished first."""
        loop = asyncio.get_running_loop()
        next_reap = loop.time()
        while not stop.is_set():
            worked = False
            try:
                if loop.time() >= next_reap:
                    await self.reap()
                    next_reap = loop.time() + self._config.reap_interval.total_seconds()
                worked = await self.run_one()
            except Exception:
                # For example: the database restarted. Stay alive and try again after a pause.
                log.exception("worker.loop_error")
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=self._config.poll_interval.total_seconds()
                    )

    async def run_one(self) -> bool:
        """Take one job and run it. Returns False when there was no job to take."""
        async with self._services.sessions() as session:
            job = await store.claim(
                session,
                worker_id=self._config.worker_id,
                lease=self._config.lease,
                kinds=self._registry.kinds(),  # only jobs that this worker can run
            )
            await session.commit()  # commit at once: the row lock is held only for an instant
        if job is None:
            return False
        await self._run(job)
        return True

    async def reap(self) -> int:
        """Give back the jobs of workers that died. Any worker may do this."""
        async with self._services.sessions() as session:
            count = await store.reap_expired(session)
            await session.commit()
        if count:
            log.warning("queue.reaped", count=count)
        return count

    # --- One job -------------------------------------------------------------------------

    async def _run(self, job: ClaimedJob) -> None:
        events = log.bind(job_id=job.id, kind=job.kind.value, attempt=job.attempts)
        started = time.monotonic()
        try:
            failure = await self._attempt(job)
            state = await self._settle(job, failure)
        except LeaseLostError:
            # Another worker has the job now. Its result counts, so this one is thrown away.
            events.warning("job.lease_lost")
            return
        events.info(
            "job.finished",
            state=state.value,
            seconds=round(time.monotonic() - started, 3),
            error=failure.error if failure else None,
        )

    async def _attempt(self, job: ClaimedJob) -> _Failure | None:
        """Run the handler. Returns None when it succeeded, or what went wrong."""
        spec = self._registry.get(job.kind)
        if spec is None:  # cannot happen: claim asks only for kinds that have a handler
            return _Failure(f"no handler for {job.kind.value}", None)
        try:
            payload = spec.payload_type.model_validate(job.payload)
        except ValidationError as exc:
            return _Failure(f"invalid payload: {exc.error_count()} error(s)", None)

        retry_after = backoff(
            job.attempts, base=self._config.backoff_base, cap=self._config.backoff_cap
        )
        context = JobContext(
            job=job, services=self._services, heartbeat=lambda: self._heartbeat(job)
        )
        try:
            await self._run_with_lease(spec, context, payload)
        except Retry as signal:
            return _Failure(signal.reason, signal.after)
        except Permanent as signal:
            return _Failure(signal.reason, None)
        except TimeoutError:
            return _Failure(f"timed out after {spec.timeout.total_seconds():g} s", retry_after)
        except LeaseLostError:
            raise
        except Exception as exc:
            return _Failure(f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH], retry_after)
        return None

    async def _run_with_lease(
        self, spec: HandlerSpec, context: JobContext, payload: object
    ) -> None:
        """Run the handler and keep its lease alive. Stop the handler if the lease is lost."""
        job = context.job
        handler = asyncio.ensure_future(spec.run(context, payload))
        lease_lost = False

        async def keep_alive() -> None:
            nonlocal lease_lost
            while True:
                # Three heartbeats per lease: two can be lost before the lease ends.
                await asyncio.sleep(self._config.lease.total_seconds() / 3)
                try:
                    await self._heartbeat(job)
                except LeaseLostError:
                    lease_lost = True
                    handler.cancel()  # its result would be refused; do not waste the work
                    return

        heartbeats = asyncio.create_task(keep_alive())
        try:
            async with asyncio.timeout(spec.timeout.total_seconds()):
                await handler
        except asyncio.CancelledError:
            if lease_lost:
                raise LeaseLostError(job) from None
            handler.cancel()  # the worker itself is stopping
            raise
        finally:
            heartbeats.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeats

    async def _heartbeat(self, job: ClaimedJob) -> None:
        async with self._services.sessions() as session:
            await store.heartbeat(session, job, lease=self._config.lease)
            await session.commit()

    async def _settle(self, job: ClaimedJob, failure: _Failure | None) -> JobState:
        """Write the result. Raises LeaseLostError when the job is no longer ours."""
        async with self._services.sessions() as session:
            if failure is None:
                await store.complete(session, job)
                state = JobState.DONE
            else:
                state = await store.fail(
                    session, job, failure.error, retry_after=failure.retry_after
                )
            await session.commit()
        return state
