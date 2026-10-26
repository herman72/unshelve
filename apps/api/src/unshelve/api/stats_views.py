"""The numbers of ``GET /stats``: your items, the job queue and what the model calls cost.

The items are the user's. The queue and the ledger belong to the whole instance: one pool of
workers and one bill, whoever started a job (most jobs and calls have no owner).

All numbers use one moment, ``as_of``: the clock of the database, which the queue also uses
for its leases. Each number comes from one query; two queries can see the queue a few
milliseconds apart.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.api.schemas import (
    ItemStats,
    JobKindStats,
    LLMCostStats,
    QueueStats,
    Stats,
    TaskCost,
)
from unshelve.config import Settings
from unshelve.contracts.enums import EnrichState, ItemStatus, JobKind, JobState
from unshelve.db.models import TIMESTAMP, Item, Job, LLMCall
from unshelve.providers.ledger import budget_spent, start_of_day

READ_WINDOW = timedelta(days=7)
JOB_WINDOW = timedelta(hours=24)
# The same 30 days as `unshelve llm costs`: from midnight 30 days before today.
COST_DAYS = timedelta(days=30)
_SECONDS_IN_A_DAY = 86_400


class _NoJobs(NamedTuple):
    """The counts of a kind without jobs: the query has no row for it."""

    ready: int = 0
    scheduled: int = 0
    running: int = 0
    dead: int = 0
    retrying: int = 0
    done_recent: int = 0
    dead_recent: int = 0
    oldest_ready: datetime | None = None


async def stats(
    db: AsyncSession, owner_id: int, settings: Settings, *, now: datetime | None = None
) -> Stats:
    """The stats of this moment. ``now`` is for the tests; the database clock otherwise."""
    as_of = now or (await db.execute(sa.select(sa.func.now()))).scalar_one()
    return Stats(
        as_of=as_of,
        items=await _items(db, owner_id, as_of),
        queue=await _queue(db, as_of),
        llm=await _llm(db, settings, as_of),
    )


async def _items(db: AsyncSession, owner_id: int, now: datetime) -> ItemStats:
    mine = sa.and_(Item.owner_id == owner_id, Item.duplicate_of_id.is_(None))
    by_status = dict.fromkeys(ItemStatus, 0)
    for row in await db.execute(
        sa.select(Item.status, sa.func.count().label("total")).where(mine).group_by(Item.status)
    ):
        by_status[row.status] = row.total
    by_enrich_state = dict.fromkeys(EnrichState, 0)
    for row in await db.execute(
        sa.select(Item.enrich_state, sa.func.count().label("total"))
        .where(mine)
        .group_by(Item.enrich_state)
    ):
        by_enrich_state[row.enrich_state] = row.total

    at = sa.literal(now, TIMESTAMP)
    unread = sa.and_(Item.read_at.is_(None), Item.status != ItemStatus.ARCHIVED)
    row = (
        await db.execute(
            sa.select(
                sa.func.count().filter(Item.read_at >= at - READ_WINDOW).label("read"),
                # The median of the waits, as an interval, then in seconds.
                sa.extract(
                    "epoch",
                    sa.func.percentile_cont(0.5)
                    .within_group(at - Item.last_captured_at)
                    .filter(unread),
                ).label("median_seconds"),
            ).where(mine)
        )
    ).one()
    return ItemStats(
        by_status=by_status,
        by_enrich_state=by_enrich_state,
        read_last_7_days=row.read,
        median_unread_age_days=None
        if row.median_seconds is None
        else round(float(row.median_seconds) / _SECONDS_IN_A_DAY, 2),
    )


async def _queue(db: AsyncSession, now: datetime) -> QueueStats:
    at = sa.literal(now, TIMESTAMP)
    pending = Job.state == JobState.PENDING
    ready = sa.and_(pending, Job.run_after <= at)
    recent = Job.finished_at >= at - JOB_WINDOW
    rows = {
        row.kind: row
        for row in await db.execute(
            sa.select(
                Job.kind,
                sa.func.count().filter(ready).label("ready"),
                sa.func.count().filter(pending, Job.run_after > at).label("scheduled"),
                sa.func.count().filter(Job.state == JobState.RUNNING).label("running"),
                sa.func.count().filter(Job.state == JobState.DEAD).label("dead"),
                sa.func.count().filter(pending, Job.last_error.is_not(None)).label("retrying"),
                sa.func.count().filter(Job.state == JobState.DONE, recent).label("done_recent"),
                sa.func.count().filter(Job.state == JobState.DEAD, recent).label("dead_recent"),
                sa.func.min(Job.run_after).filter(ready).label("oldest_ready"),
            ).group_by(Job.kind)
        )
    }
    kinds = [_kind_stats(kind, rows.get(kind) or _NoJobs(), now) for kind in JobKind]
    waits = [kind.oldest_ready_seconds for kind in kinds if kind.oldest_ready_seconds is not None]
    return QueueStats(
        depth=sum(kind.ready + kind.scheduled + kind.running for kind in kinds),
        dead=sum(kind.dead for kind in kinds),
        oldest_ready_seconds=max(waits, default=None),
        by_kind=kinds,
    )


def _kind_stats(
    kind: JobKind, counts: _NoJobs | sa.Row[*tuple[Any, ...]], now: datetime
) -> JobKindStats:
    oldest: datetime | None = counts.oldest_ready
    return JobKindStats(
        kind=kind,
        ready=counts.ready,
        scheduled=counts.scheduled,
        running=counts.running,
        dead=counts.dead,
        retrying=counts.retrying,
        done_last_24_hours=counts.done_recent,
        dead_last_24_hours=counts.dead_recent,
        oldest_ready_seconds=None if oldest is None else (now - oldest).total_seconds(),
    )


async def _llm(db: AsyncSession, settings: Settings, now: datetime) -> LLMCostStats:
    today = start_of_day(now, ZoneInfo(settings.timezone))
    cost = sa.func.sum(LLMCall.cost_usd)
    rows = list(
        await db.execute(
            sa.select(
                LLMCall.task,
                sa.func.count().label("calls"),
                sa.func.sum(LLMCall.input_tokens).label("input_tokens"),
                sa.func.sum(LLMCall.output_tokens).label("output_tokens"),
                cost.label("cost"),
            )
            .where(LLMCall.created_at >= today - COST_DAYS)
            .group_by(LLMCall.task)
            .order_by(cost.desc(), LLMCall.task)
        )
    )
    return LLMCostStats(
        spent_today_usd=_dollars(await budget_spent(db, since=today)),
        daily_budget_usd=_dollars(settings.llm_daily_budget_usd),
        spent_last_30_days_usd=_dollars(sum((row.cost for row in rows), Decimal(0))),
        by_task=[
            TaskCost(
                task=row.task,
                calls=row.calls,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cost_usd=_dollars(row.cost),
            )
            for row in rows
        ],
    )


def _dollars(amount: Decimal) -> float:
    """The ledger keeps six decimals of a dollar. JSON has numbers, not decimals."""
    return float(round(amount, 6))
