"""Capture: recorded updates go through the handlers of the bot. Each test name is one rule."""

import asyncio
from typing import cast

import pytest
import sqlalchemy as sa
from aiogram import Dispatcher
from aiogram.types import Update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from tests.bot.support import CHANNEL, OWNER_TELEGRAM_ID, offline_bot, updates
from unshelve.bot import capture
from unshelve.bot.runner import build_dispatcher
from unshelve.config import Settings
from unshelve.contracts.enums import CaptureSource, EnrichState, JobKind, JobState
from unshelve.db.models import Capture, Item, Job, User
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def settings(private_database_url: str) -> Settings:
    return Settings(
        env="test",
        database_url=private_database_url,
        owner_telegram_id=OWNER_TELEGRAM_ID,
        capture_chat_ids=[CHANNEL],
    )


@pytest.fixture
def dispatcher(settings: Settings, committed_db: Sessions) -> Dispatcher:
    return build_dispatcher(settings, committed_db)


async def _deliver(dispatcher: Dispatcher, some: list[Update]) -> None:
    bot = offline_bot()
    for update in some:
        await dispatcher.feed_update(bot, update)


async def _captures(sessions: Sessions) -> list[Capture]:
    async with sessions() as db:
        found = await db.execute(sa.select(Capture).order_by(Capture.message_id, Capture.edit_seq))
        return list(found.scalars().all())


async def _job_kinds(sessions: Sessions) -> list[JobKind]:
    async with sessions() as db:
        return list((await db.execute(sa.select(Job.kind).order_by(Job.id))).scalars().all())


async def test_the_recorded_updates_give_one_capture_for_each_post(
    dispatcher: Dispatcher, committed_db: Sessions
) -> None:
    await _deliver(dispatcher, updates("seen"))

    captures = await _captures(committed_db)
    # Eight updates. Five of them are posts: three photos of one album, a link, a forward.
    assert [capture.message_id for capture in captures] == [7, 8, 9, 10, 11]
    assert {capture.source for capture in captures} == {CaptureSource.BOT}
    assert {capture.chat_id for capture in captures} == {CHANNEL}
    assert sorted(await _job_kinds(committed_db)) == [
        JobKind.INGEST_ALBUM,
        JobKind.INGEST_CAPTURE,
        JobKind.INGEST_CAPTURE,
    ]


async def test_updates_that_arrive_again_store_nothing(
    dispatcher: Dispatcher, committed_db: Sessions
) -> None:
    await _deliver(dispatcher, updates("seen"))

    await _deliver(dispatcher, updates("seen"))  # Telegram sends them again after a crash

    assert len(await _captures(committed_db)) == 5
    assert len(await _job_kinds(committed_db)) == 3


async def test_a_post_of_a_channel_that_is_not_allowed_is_not_stored(
    dispatcher: Dispatcher, committed_db: Sessions
) -> None:
    foreign = [u for u in updates("made_up") if u.update_id == 206]

    await _deliver(dispatcher, foreign)

    assert await _captures(committed_db) == []
    assert await _job_kinds(committed_db) == []


async def test_nothing_is_stored_when_no_owner_is_set(
    private_database_url: str, committed_db: Sessions
) -> None:
    settings = Settings(env="test", database_url=private_database_url, capture_chat_ids=[CHANNEL])

    await _deliver(build_dispatcher(settings, committed_db), updates("seen"))

    assert await _captures(committed_db) == []


async def test_the_first_post_creates_the_owner(
    dispatcher: Dispatcher, committed_db: Sessions
) -> None:
    await _deliver(dispatcher, updates("seen"))

    async with committed_db() as db:
        owners = (await db.execute(sa.select(User.telegram_user_id))).scalars().all()
    assert owners == [OWNER_TELEGRAM_ID]


async def test_an_edit_is_stored_next_to_the_first_version_and_gets_a_job(
    dispatcher: Dispatcher, committed_db: Sessions
) -> None:
    link_post = [u for u in updates("seen") if u.update_id == 104]
    edit = [u for u in updates("made_up") if u.update_id == 205]

    await _deliver(dispatcher, link_post + edit + edit)

    versions = [(c.message_id, c.edit_seq) for c in await _captures(committed_db)]
    assert versions == [(10, 0), (10, 1791930240)]
    # One job for the post, one for its edit. The edit that arrived again adds nothing.
    assert await _job_kinds(committed_db) == [JobKind.INGEST_CAPTURE, JobKind.INGEST_CAPTURE]


async def test_the_parts_of_an_album_that_arrive_together_get_one_job(
    dispatcher: Dispatcher, committed_db: Sessions
) -> None:
    bot = offline_bot()
    album = [u for u in updates("seen") if u.channel_post and u.channel_post.media_group_id]
    assert len(album) == 3

    await asyncio.gather(*(dispatcher.feed_update(bot, update) for update in album))

    assert len(await _captures(committed_db)) == 3
    assert await _job_kinds(committed_db) == [JobKind.INGEST_ALBUM]


class DownForAWhile:
    """A database that does not answer the first times, and then works."""

    def __init__(self, sessions: Sessions, failures: int) -> None:
        self._sessions = sessions
        self.failures_left = failures

    def __call__(self) -> AsyncSession:
        if self.failures_left > 0:
            self.failures_left -= 1
            raise OperationalError("SELECT 1", {}, ConnectionError("the database is down"))
        return self._sessions()


async def test_a_database_that_is_down_delays_a_post_and_does_not_lose_it(
    settings: Settings, committed_db: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "RETRY_DELAYS", (0.0,))
    database = DownForAWhile(committed_db, failures=3)
    dispatcher = build_dispatcher(settings, cast(Sessions, database))
    link_post = [u for u in updates("seen") if u.update_id == 104]

    with capture_logs() as logs:
        await _deliver(dispatcher, link_post)

    assert [entry["event"] for entry in logs] == ["capture.database_down"] * 3 + ["capture.stored"]
    assert [entry["attempt"] for entry in logs[:3]] == [1, 2, 3]
    assert [c.message_id for c in await _captures(committed_db)] == [10]


async def test_a_mistake_in_the_code_is_not_tried_again_for_ever(
    settings: Settings, committed_db: Sessions
) -> None:
    def broken() -> AsyncSession:
        raise ValueError("a bug, not a database that is down")

    dispatcher = build_dispatcher(settings, cast(Sessions, broken))

    with pytest.raises(ValueError, match="a bug"):
        await _deliver(dispatcher, [u for u in updates("seen") if u.update_id == 104])

    assert await _captures(committed_db) == []


async def test_every_kind_of_post_is_stored(dispatcher: Dispatcher, committed_db: Sessions) -> None:
    await _deliver(dispatcher, updates("made_up"))

    stored = {c.message_id: c for c in await _captures(committed_db) if c.edit_seq == 0}
    # The post of the channel that is not allowed (message 3) is not in the list.
    assert sorted(stored) == [12, 13, 14, 15, 16, 17, 18]
    assert stored[13].media[0]["file_name"] == "paper.pdf"
    assert stored[17].forward_origin is not None
    assert stored[17].forward_origin["name"] == "Ada Lovelace"
    assert stored[12].raw["text"].startswith("👋")


async def test_the_recorded_posts_become_three_finished_items(
    settings: Settings, dispatcher: Dispatcher, committed_db: Sessions
) -> None:
    await _deliver(dispatcher, updates("seen"))
    async with committed_db() as db:  # the wait for the other parts of the album is over
        await db.execute(
            sa.update(Job).where(Job.state == JobState.PENDING).values(run_after=sa.func.now())
        )
        await db.commit()
    discover()
    worker = Worker(build_services(settings, committed_db), registry, WorkerConfig(worker_id="w1"))

    while await worker.run_one():
        pass

    async with committed_db() as db:
        items = (
            (await db.execute(sa.select(Item).order_by(Item.first_captured_at, Item.id)))
            .scalars()
            .all()
        )
        parts = (
            (await db.execute(sa.select(Capture.item_id).order_by(Capture.message_id)))
            .scalars()
            .all()
        )
    assert [item.enrich_state for item in items] == [EnrichState.READY] * 3
    album, link, forward = items
    assert parts == [album.id, album.id, album.id, link.id, forward.id]
    assert album.canonical_url is None
    # The items have the canonical form of each address: no "www.", and https.
    assert link.canonical_url == "https://youtube.com/watch?v=UPtmKh1vMN8"
    assert forward.canonical_url == "https://github.com/cloudflare/security-audit-skill"
