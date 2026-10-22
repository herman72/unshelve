"""The job that publishes the public feed of an owner. Each test name is one rule."""

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.publish.support import LEASES_SLUG, PERSIAN_SLUG, seed_world
from unshelve.config import Settings
from unshelve.contracts.dto import Published
from unshelve.contracts.enums import JobKind, JobState
from unshelve.contracts.errors import PublishError
from unshelve.contracts.jobs import PublishFeedPayload, Services
from unshelve.db.models import Job
from unshelve.queue import store as queue
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services
from unshelve.testing.fakes import InMemoryFeedTarget

pytestmark = pytest.mark.db

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def target() -> InMemoryFeedTarget:
    return InMemoryFeedTarget()


@pytest.fixture
def services(
    committed_db: Sessions, private_database_url: str, target: InMemoryFeedTarget
) -> Services:
    settings = Settings(env="test", database_url=private_database_url)
    return dataclasses.replace(build_services(settings, committed_db), feed=target)


class _FailingTarget:
    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable

    async def publish(self, data: bytes) -> Published:
        raise PublishError("cannot write the feed at /site/feed.json", retryable=self.retryable)

    async def read(self) -> bytes | None:
        return None


async def _queue_and_run(services: Services) -> Job:
    """Queue the job of the owner of the made-up world, run it one time, and read it again."""
    world = await seed_world(services.sessions)
    async with services.sessions() as db:
        job_id = await queue.enqueue(
            db,
            JobKind.PUBLISH_FEED,
            PublishFeedPayload(owner_id=world.owner_id),
            owner_id=world.owner_id,
        )
        await db.commit()
    discover()
    assert await Worker(services, registry, WorkerConfig(worker_id="w1")).run_one()
    async with services.sessions() as db:
        return await db.get_one(Job, job_id)


def test_the_worker_knows_the_job_of_the_feed() -> None:
    discover()
    assert registry.get(JobKind.PUBLISH_FEED) is not None


async def test_the_job_publishes_the_feed_of_its_owner(
    services: Services, target: InMemoryFeedTarget
) -> None:
    job = await _queue_and_run(services)

    assert job.state == JobState.DONE
    [data] = target.feeds
    assert [record["id"] for record in json.loads(data)["items"]] == [PERSIAN_SLUG, LEASES_SLUG]


async def test_without_a_target_the_job_ends_and_publishes_nothing(services: Services) -> None:
    job = await _queue_and_run(dataclasses.replace(services, feed=None))

    assert job.state == JobState.DONE


async def test_a_fault_that_can_pass_is_tried_again_later(services: Services) -> None:
    started = datetime.now(UTC)

    job = await _queue_and_run(dataclasses.replace(services, feed=_FailingTarget(retryable=True)))

    assert (job.state, job.attempts) == (JobState.PENDING, 1)
    assert job.run_after >= started + timedelta(minutes=5)
    assert job.last_error is not None
    assert "cannot write the feed" in job.last_error


async def test_a_lasting_fault_ends_the_job(services: Services) -> None:
    job = await _queue_and_run(dataclasses.replace(services, feed=_FailingTarget(retryable=False)))

    assert (job.state, job.attempts) == (JobState.DEAD, 1)
