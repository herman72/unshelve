"""GET /stats: the items, the queue depth, the failures, and the cost of each task.

The rules about time call the view with a fixed moment, ``NOW``. The HTTP tests check the
contract: the session, the shape, and the numbers of the database.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import ClientFactory
from unshelve.api import stats_views
from unshelve.api.deps import SESSION_COOKIE
from unshelve.api.schemas import JobKindStats, Stats
from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.enums import EnrichState, ItemStatus, JobKind, JobState
from unshelve.db.models import Item, Job, LLMBatch, LLMCall

pytestmark = pytest.mark.db

Sessions = async_sessionmaker[AsyncSession]

STATS = "/api/v1/stats"
OWNER_TELEGRAM_ID = 4242
# A Monday. In Helsinki it is 14:00: the clocks went back to UTC+2 on Sunday, 25 October.
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
MINUTE, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)


@pytest.fixture
async def owner_id(committed_db: Sessions) -> int:
    async with committed_db() as db:
        found = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
        await db.commit()
    return found


@pytest.fixture
async def client(
    client_for: ClientFactory, private_database_url: str, committed_db: Sessions, owner_id: int
) -> AsyncGenerator[httpx.AsyncClient]:
    """A client that is signed in as the owner."""
    async with committed_db() as db:
        secret = await auth.open_session(db, owner_id)
        await db.commit()
    async with client_for(Settings(env="test", database_url=private_database_url)) as client:
        client.cookies.set(SESSION_COOKIE, secret)
        yield client


async def _stats(
    sessions: Sessions, owner_id: int, settings: Settings | None = None, *, now: datetime = NOW
) -> Stats:
    async with sessions() as db:
        return await stats_views.stats(db, owner_id, settings or Settings(env="test"), now=now)


async def _add(sessions: Sessions, *rows: Any) -> None:
    async with sessions() as db:
        db.add_all(rows)
        await db.commit()


def _job(
    kind: JobKind,
    state: JobState,
    *,
    run_after: datetime = NOW - MINUTE,
    finished_at: datetime | None = None,
    last_error: str | None = None,
) -> Job:
    running = state is JobState.RUNNING
    return Job(
        kind=kind,
        state=state,
        payload={},
        run_after=run_after,
        finished_at=finished_at,
        last_error=last_error,
        attempts=0 if state is JobState.PENDING and last_error is None else 1,
        # The database refuses a running job without a lease, and a lease on any other job.
        locked_by="w1" if running else None,
        locked_until=NOW + MINUTE if running else None,
    )


def _call(
    task: str,
    cost: str,
    *,
    at: datetime = NOW - HOUR,
    tokens: tuple[int, int] = (0, 0),
    batch_id: int | None = None,
) -> LLMCall:
    return LLMCall(
        cache_key=f"{task}-{cost}-{at.isoformat()}-{batch_id}-{tokens}",
        task=task,
        model="claude-opus-5-5",
        prompt_version="v1",
        request={},
        response={},
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        cost_usd=Decimal(cost),
        created_at=at,
        batch_id=batch_id,
    )


def _item(
    owner_id: int,
    status: ItemStatus = ItemStatus.INBOX,
    enrich_state: EnrichState = EnrichState.READY,
    *,
    saved: datetime = NOW - DAY,
    first_saved: datetime | None = None,
    read_at: datetime | None = None,
    duplicate_of_id: int | None = None,
) -> Item:
    if status is ItemStatus.DONE and read_at is None:
        read_at = NOW - HOUR  # the database refuses "done" without it
    return Item(
        owner_id=owner_id,
        status=status,
        enrich_state=enrich_state,
        first_captured_at=first_saved or saved,
        last_captured_at=saved,
        read_at=read_at,
        duplicate_of_id=duplicate_of_id,
    )


def _kind(stats: Stats, kind: JobKind) -> JobKindStats:
    [found] = [row for row in stats.queue.by_kind if row.kind is kind]
    return found


# --- Over HTTP ---------------------------------------------------------------------------


async def test_an_empty_instance_has_zeros_and_no_ages(client: httpx.AsyncClient) -> None:
    response = await client.get(STATS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == {
        "by_status": {"inbox": 0, "queued": 0, "reading": 0, "done": 0, "archived": 0},
        "by_enrich_state": {"pending": 0, "fetched": 0, "enriched": 0, "ready": 0, "failed": 0},
        "read_last_7_days": 0,
        "median_unread_age_days": None,
    }
    assert body["queue"]["depth"] == 0
    assert body["queue"]["dead"] == 0
    assert body["queue"]["oldest_ready_seconds"] is None
    # Each kind has a row, in the order of the enum, so a page can show a fixed table.
    assert [row["kind"] for row in body["queue"]["by_kind"]] == [kind.value for kind in JobKind]
    assert body["llm"] == {
        "spent_today_usd": 0.0,
        "daily_budget_usd": 1.0,
        "spent_last_30_days_usd": 0.0,
        "by_task": [],
    }
    # The moment of the numbers is the clock of the database: now.
    as_of = datetime.fromisoformat(body["as_of"])
    assert abs(as_of - datetime.now(UTC)) < MINUTE


async def test_the_answer_has_the_numbers_of_the_database(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    now = datetime.now(UTC)
    await _add(
        committed_db,
        _item(owner_id, ItemStatus.QUEUED, saved=now - 2 * DAY),
        _job(JobKind.ENRICH_ITEM, JobState.PENDING, run_after=now - MINUTE),
        _job(JobKind.FETCH_ITEM, JobState.DEAD, finished_at=now - HOUR, last_error="404"),
        _call("enrich_item", "0.012345", at=now - MINUTE, tokens=(1200, 300)),
    )

    body = (await client.get(STATS)).json()

    assert body["items"]["by_status"]["queued"] == 1
    assert body["items"]["median_unread_age_days"] == pytest.approx(2.0, abs=0.01)
    assert (body["queue"]["depth"], body["queue"]["dead"]) == (1, 1)
    enrich = next(row for row in body["queue"]["by_kind"] if row["kind"] == "enrich_item")
    assert enrich["ready"] == 1
    assert enrich["oldest_ready_seconds"] == pytest.approx(60, abs=30)
    assert body["llm"]["by_task"] == [
        {
            "task": "enrich_item",
            "calls": 1,
            "input_tokens": 1200,
            "output_tokens": 300,
            "cost_usd": 0.012345,
        }
    ]
    # Not "today": one minute after midnight, the call would be yesterday's.
    assert body["llm"]["spent_last_30_days_usd"] == 0.012345


# --- The queue: depth --------------------------------------------------------------------


async def test_the_depth_counts_ready_scheduled_and_running_jobs_but_not_dead_or_done(
    committed_db: Sessions, owner_id: int
) -> None:
    await _add(
        committed_db,
        _job(JobKind.FETCH_ITEM, JobState.PENDING, run_after=NOW - MINUTE),  # ready
        _job(JobKind.FETCH_ITEM, JobState.PENDING, run_after=NOW - 10 * MINUTE),  # ready
        _job(JobKind.FETCH_ITEM, JobState.PENDING, run_after=NOW + 5 * MINUTE),  # scheduled
        _job(JobKind.FETCH_ITEM, JobState.RUNNING),
        _job(JobKind.FETCH_ITEM, JobState.DEAD, finished_at=NOW - HOUR, last_error="404"),
        _job(JobKind.FETCH_ITEM, JobState.DONE, finished_at=NOW - HOUR),
        _job(JobKind.ENRICH_ITEM, JobState.PENDING),  # ready
    )

    stats = await _stats(committed_db, owner_id)

    fetch = _kind(stats, JobKind.FETCH_ITEM)
    assert (fetch.ready, fetch.scheduled, fetch.running, fetch.dead) == (2, 1, 1, 1)
    assert _kind(stats, JobKind.ENRICH_ITEM).ready == 1
    # 2 + 1 + 1 of fetch_item, and 1 of enrich_item. The dead job and the done job are not in it.
    assert stats.queue.depth == 5
    assert stats.queue.dead == 1


async def test_a_job_whose_time_is_now_is_ready(committed_db: Sessions, owner_id: int) -> None:
    await _add(committed_db, _job(JobKind.EMBED_ITEM, JobState.PENDING, run_after=NOW))

    embed = _kind(await _stats(committed_db, owner_id), JobKind.EMBED_ITEM)

    assert (embed.ready, embed.scheduled) == (1, 0)  # claim takes run_after <= now


async def test_the_oldest_ready_job_says_how_long_it_has_waited(
    committed_db: Sessions, owner_id: int
) -> None:
    await _add(
        committed_db,
        _job(JobKind.FETCH_ITEM, JobState.PENDING, run_after=NOW - 90 * timedelta(seconds=1)),
        _job(JobKind.FETCH_ITEM, JobState.PENDING, run_after=NOW - 30 * timedelta(seconds=1)),
        _job(JobKind.ENRICH_ITEM, JobState.PENDING, run_after=NOW - 10 * timedelta(seconds=1)),
        # Not ready, so their times do not count, though they are older.
        _job(JobKind.ENRICH_ITEM, JobState.DEAD, run_after=NOW - DAY, finished_at=NOW - HOUR),
        _job(JobKind.EMBED_ITEM, JobState.RUNNING, run_after=NOW - DAY),
        _job(JobKind.SCORE_ITEM, JobState.DONE, run_after=NOW - DAY, finished_at=NOW - HOUR),
    )

    stats = await _stats(committed_db, owner_id)

    assert _kind(stats, JobKind.FETCH_ITEM).oldest_ready_seconds == 90.0
    assert _kind(stats, JobKind.ENRICH_ITEM).oldest_ready_seconds == 10.0
    assert _kind(stats, JobKind.EMBED_ITEM).oldest_ready_seconds is None
    assert _kind(stats, JobKind.SCORE_ITEM).oldest_ready_seconds is None
    assert stats.queue.oldest_ready_seconds == 90.0


# --- The queue: failures -----------------------------------------------------------------


async def test_a_failed_job_is_not_counted_as_done(committed_db: Sessions, owner_id: int) -> None:
    await _add(
        committed_db,
        _job(JobKind.EMBED_ITEM, JobState.DONE, finished_at=NOW - HOUR),
        _job(JobKind.EMBED_ITEM, JobState.DONE, finished_at=NOW - 23 * HOUR),
        _job(JobKind.EMBED_ITEM, JobState.DONE, finished_at=NOW - 25 * HOUR),  # too old
        _job(JobKind.EMBED_ITEM, JobState.DEAD, finished_at=NOW - 2 * HOUR, last_error="x"),
        _job(JobKind.EMBED_ITEM, JobState.DEAD, finished_at=NOW - 30 * HOUR, last_error="x"),
    )

    embed = _kind(await _stats(committed_db, owner_id), JobKind.EMBED_ITEM)

    assert embed.done_last_24_hours == 2
    assert embed.dead_last_24_hours == 1
    assert embed.dead == 2  # every dead job, also the old one


async def test_retrying_counts_the_pending_jobs_after_a_failed_attempt(
    committed_db: Sessions, owner_id: int
) -> None:
    budget = "model: the daily budget of $1.00 is used"
    await _add(
        committed_db,
        _job(JobKind.ENRICH_ITEM, JobState.PENDING, last_error="the site answered 503"),
        _job(JobKind.ENRICH_ITEM, JobState.PENDING, run_after=NOW + HOUR, last_error=budget),
        _job(JobKind.ENRICH_ITEM, JobState.PENDING),  # no failed attempt yet
        # Not pending: a dead job, and a job that succeeded at its second attempt.
        _job(JobKind.ENRICH_ITEM, JobState.DEAD, finished_at=NOW - HOUR, last_error="x"),
        _job(JobKind.ENRICH_ITEM, JobState.DONE, finished_at=NOW - HOUR, last_error="x"),
    )

    enrich = _kind(await _stats(committed_db, owner_id), JobKind.ENRICH_ITEM)

    assert enrich.retrying == 2
    assert (enrich.ready, enrich.scheduled) == (2, 1)


# --- What the model calls cost -----------------------------------------------------------


async def test_the_cost_is_summed_for_each_task(committed_db: Sessions, owner_id: int) -> None:
    await _add(
        committed_db,
        _call("enrich_item", "0.10", tokens=(1000, 200)),
        _call("enrich_item", "0.20", tokens=(1500, 300)),
        _call("score_item", "0.05", tokens=(800, 50)),
        _call("read_image", "0.01", at=NOW - 2 * HOUR),
        _call("read_image", "0.02", at=NOW - 3 * HOUR),
        _call("overview", "0.03"),
    )

    llm = (await _stats(committed_db, owner_id)).llm

    assert [
        (t.task, t.calls, t.input_tokens, t.output_tokens, t.cost_usd) for t in llm.by_task
    ] == [
        ("enrich_item", 2, 2500, 500, 0.30),
        ("score_item", 1, 800, 50, 0.05),
        # The same cost: the name decides.
        ("overview", 1, 0, 0, 0.03),
        ("read_image", 2, 0, 0, 0.03),
    ]
    assert llm.spent_last_30_days_usd == 0.41
    assert llm.spent_today_usd == 0.41


async def test_a_call_before_the_30_days_is_left_out(committed_db: Sessions, owner_id: int) -> None:
    # Today starts at 00:00 UTC on 26 October. The 30 days start at 00:00 UTC on 26 September.
    await _add(
        committed_db,
        _call("enrich_item", "0.50", at=datetime(2026, 9, 26, 0, 0, tzinfo=UTC)),
        _call("enrich_item", "7.00", at=datetime(2026, 9, 25, 23, 59, tzinfo=UTC)),
    )

    llm = (await _stats(committed_db, owner_id)).llm

    assert llm.spent_last_30_days_usd == 0.50
    assert [(task.task, task.calls) for task in llm.by_task] == [("enrich_item", 1)]


async def test_today_starts_at_midnight_in_the_time_zone_of_the_settings(
    committed_db: Sessions, owner_id: int
) -> None:
    # 23:00 UTC on 25 October is 01:00 on 26 October in Helsinki: today. 21:00 UTC is 23:00 on
    # 25 October in Helsinki: yesterday.
    await _add(
        committed_db,
        _call("enrich_item", "0.25", at=datetime(2026, 10, 25, 23, 0, tzinfo=UTC)),
        _call("enrich_item", "0.75", at=datetime(2026, 10, 25, 21, 0, tzinfo=UTC)),
    )

    helsinki = Settings(env="test", timezone="Europe/Helsinki")
    in_helsinki = (await _stats(committed_db, owner_id, helsinki)).llm
    in_utc = (await _stats(committed_db, owner_id)).llm

    assert (in_helsinki.spent_today_usd, in_helsinki.spent_last_30_days_usd) == (0.25, 1.0)
    assert (in_utc.spent_today_usd, in_utc.spent_last_30_days_usd) == (0.0, 1.0)


async def test_the_answers_of_a_batch_count_in_the_30_days_but_not_in_today(
    committed_db: Sessions, owner_id: int
) -> None:
    # You accepted the price of the batch by hand: it does not use the daily budget.
    async with committed_db() as db:
        batch = LLMBatch(
            model="claude-opus-5-5",
            task="enrich_item",
            prompt_version="v1",
            questions=1,
            estimated_cost_usd=Decimal("5.00"),
        )
        db.add(batch)
        await db.flush()
        db.add_all([_call("enrich_item", "5.00", batch_id=batch.id), _call("overview", "0.40")])
        await db.commit()

    llm = (await _stats(committed_db, owner_id)).llm

    assert llm.spent_today_usd == 0.40
    assert llm.spent_last_30_days_usd == 5.40
    assert [(task.task, task.cost_usd) for task in llm.by_task] == [
        ("enrich_item", 5.0),
        ("overview", 0.40),
    ]


async def test_the_daily_budget_comes_from_the_settings(
    committed_db: Sessions, owner_id: int
) -> None:
    settings = Settings(env="test", llm_daily_budget_usd=Decimal("2.50"))

    assert (await _stats(committed_db, owner_id, settings)).llm.daily_budget_usd == 2.5


# --- Your items --------------------------------------------------------------------------


async def test_items_are_counted_without_duplicates_and_without_other_users(
    committed_db: Sessions, owner_id: int
) -> None:
    async with committed_db() as db:
        other_id = await auth.ensure_owner(db, 99)
        first = _item(owner_id, ItemStatus.INBOX, EnrichState.PENDING)
        db.add(first)
        await db.flush()
        db.add_all(
            [
                _item(owner_id, ItemStatus.INBOX, EnrichState.READY),
                _item(owner_id, ItemStatus.QUEUED, EnrichState.READY),
                _item(owner_id, ItemStatus.DONE, EnrichState.READY),
                _item(owner_id, ItemStatus.ARCHIVED, EnrichState.FAILED),
                # Left out: a duplicate of the first item, and an item of another user.
                _item(owner_id, ItemStatus.INBOX, EnrichState.FETCHED, duplicate_of_id=first.id),
                _item(other_id, ItemStatus.READING, EnrichState.ENRICHED),
            ]
        )
        await db.commit()

    items = (await _stats(committed_db, owner_id)).items

    assert items.by_status == {
        ItemStatus.INBOX: 2,
        ItemStatus.QUEUED: 1,
        ItemStatus.READING: 0,
        ItemStatus.DONE: 1,
        ItemStatus.ARCHIVED: 1,
    }
    assert items.by_enrich_state == {
        EnrichState.PENDING: 1,
        EnrichState.FETCHED: 0,
        EnrichState.ENRICHED: 0,
        EnrichState.READY: 3,
        EnrichState.FAILED: 1,
    }


async def test_read_counts_the_items_read_in_the_last_7_days(
    committed_db: Sessions, owner_id: int
) -> None:
    await _add(
        committed_db,
        _item(owner_id, ItemStatus.DONE, read_at=NOW - DAY),
        _item(owner_id, ItemStatus.DONE, read_at=NOW - 7 * DAY),  # just in
        _item(owner_id, ItemStatus.DONE, read_at=NOW - 7 * DAY - MINUTE),  # just out
        # An item keeps read_at when it leaves done: it was read on that day.
        _item(owner_id, ItemStatus.QUEUED, read_at=NOW - 2 * DAY),
        _item(owner_id, ItemStatus.INBOX),
    )

    assert (await _stats(committed_db, owner_id)).items.read_last_7_days == 3


async def test_the_median_age_is_of_the_unread_items_that_are_not_archived(
    committed_db: Sessions, owner_id: int
) -> None:
    await _add(
        committed_db,
        # Unread: 1, 3 and 10 days since the last save. The median is 3, the mean is 4.67.
        _item(owner_id, ItemStatus.INBOX, saved=NOW - DAY),
        _item(owner_id, ItemStatus.QUEUED, saved=NOW - 3 * DAY, first_saved=NOW - 40 * DAY),
        _item(owner_id, ItemStatus.READING, saved=NOW - 10 * DAY),
        # Left out: archived without a read, read, and read before it left done.
        _item(owner_id, ItemStatus.ARCHIVED, saved=NOW - 100 * DAY),
        _item(owner_id, ItemStatus.DONE, saved=NOW - 50 * DAY),
        _item(owner_id, ItemStatus.QUEUED, saved=NOW - 60 * DAY, read_at=NOW - 30 * DAY),
    )

    assert (await _stats(committed_db, owner_id)).items.median_unread_age_days == 3.0
