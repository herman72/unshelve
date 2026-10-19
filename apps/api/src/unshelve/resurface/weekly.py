"""The weekly digest: when it is due, and how it is sent one time for each week.

The owner chose a day and an hour (``UNSHELVE_DIGEST_DAY``, ``UNSHELVE_DIGEST_HOUR``, in
``UNSHELVE_TIMEZONE``). Each week has one row in ``digests``. The row is made in the same
transaction as the job that sends the digest, so each week gets one job at most. The job sends
two messages: the digest (resurface.selection chooses its items), then the stale prompt
(resurface.stale). The row marks each one as sent, so a second try sends neither again.

A Mac can sleep at 09:00 on Sunday. So a digest can come late: up to two days after its
time. After that the week is skipped, because an old digest is no help.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth_store
from unshelve.bot import cards
from unshelve.config import Settings
from unshelve.contracts.enums import Actor, JobKind
from unshelve.contracts.jobs import Permanent, Retry, SendDigestPayload, Services
from unshelve.db.models import Digest, Event, Item
from unshelve.queue import store as queue
from unshelve.queue.worker import Worker
from unshelve.resurface import plan, stale

log = structlog.get_logger()

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
LATE_AT_MOST = timedelta(hours=48)
CHECK_EVERY = timedelta(seconds=60)
# The type of the event of an item that a digest showed. Its payload keeps the reason.
SURFACED = "surfaced"


def last_send_time(now: datetime, zone: ZoneInfo, day: str, hour: int) -> datetime:
    """The last moment, at ``now`` or before it, when a digest was due."""
    local = now.astimezone(zone)
    days_back = (local.weekday() - DAYS.index(day)) % 7
    on = local.date() - timedelta(days=days_back)
    due = datetime.combine(on, time(hour), tzinfo=zone)
    if due > local:
        # Today is the day, but the hour has not come yet: the last one was a week ago.
        due = datetime.combine(on - timedelta(days=7), time(hour), tzinfo=zone)
    return due


def week_of(day: date) -> str:
    """The name of a week, for example "2026-W42" (ISO 8601: a week starts on Monday)."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def due_week(now: datetime, settings: Settings) -> str | None:
    """The week whose digest is due now, or None."""
    zone = ZoneInfo(settings.timezone)
    due = last_send_time(now, zone, settings.digest_day, settings.digest_hour)
    if now - due > LATE_AT_MOST:
        return None
    return week_of(due.date())


async def claim_week(session: AsyncSession, owner_id: int, week: str) -> bool:
    """Make the row of this week and its job, in the caller's transaction. Returns False when
    the week has its row already: then nothing is added."""
    claimed = await session.scalar(
        postgresql.insert(Digest)
        .values(owner_id=owner_id, period_key=week)
        .on_conflict_do_nothing(constraint="uq_digests_period")
        .returning(Digest.id)
    )
    if claimed is None:
        return False
    await queue.enqueue(
        session,
        JobKind.SEND_DIGEST,
        SendDigestPayload(owner_id=owner_id, period_key=week),
        owner_id=owner_id,
    )
    return True


async def plan_digest(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> str | None:
    """If a digest is due and has no job yet, make its job. Returns the week of a new job."""
    if settings.telegram_bot_token is None or settings.owner_telegram_id is None:
        return None
    week = due_week(now, settings)
    if week is None:
        return None
    async with sessions() as session:
        owner_id = await auth_store.ensure_owner(session, settings.owner_telegram_id)
        if not await claim_week(session, owner_id, week):
            return None
        await session.commit()
    log.info("digest.planned", week=week)
    return week


async def run_worker(worker: Worker, services: Services, stop: asyncio.Event) -> None:
    """The work of the worker process: the jobs, and next to them a look once a minute if
    the weekly digest is due."""
    await asyncio.gather(worker.run_forever(stop), plan_forever(services, stop))


async def plan_forever(services: Services, stop: asyncio.Event) -> None:
    """Look once a minute if a digest is due. Runs next to the worker loop."""
    while not stop.is_set():
        try:
            await plan_digest(services.sessions, services.settings, datetime.now(UTC))
        except Exception:
            # For example: the database restarted. The next minute tries again.
            log.exception("digest.plan_error")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=CHECK_EVERY.total_seconds())


@dataclass(frozen=True, slots=True)
class Sent:
    items: int  # the items of the digest; 0: there was nothing to show, and no message was sent
    message_id: int | None  # the message of the digest, when this try sent it
    stale: int = 0  # the items of the stale prompt; 0: no prompt was sent


async def _row(session: AsyncSession, owner_id: int, week: str) -> Digest:
    """The row of the week, locked until the end of the transaction."""
    row = await session.scalar(
        sa.select(Digest)
        .where(Digest.owner_id == owner_id, Digest.period_key == week)
        .with_for_update()
    )
    if row is None:
        raise Permanent(f"no digest row for {week}")
    return row


Deliver = Callable[[str, InlineKeyboardMarkup | None], Awaitable[int]]


async def _deliver(bot: Bot, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None) -> int:
    """Send one message to the owner. Returns its id. An error of Telegram becomes a retry
    or a permanent failure of the job."""
    try:
        message = await bot.send_message(
            chat_id,  # a chat with a person has the id of the person
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramRetryAfter as error:
        raise Retry("Telegram asks to wait", after=timedelta(seconds=error.retry_after)) from error
    except TelegramNetworkError as error:
        raise Retry("no connection to Telegram", after=timedelta(seconds=30)) from error
    except (TelegramForbiddenError, TelegramUnauthorizedError, TelegramBadRequest) as error:
        # The owner blocked the bot or never pressed "Start", the token is wrong, or Telegram
        # does not know the chat. A second try gives the same answer.
        raise Permanent(f"Telegram refuses: {error.message}") from error
    except TelegramAPIError as error:
        raise Retry(f"Telegram answered: {error.message}", after=timedelta(seconds=60)) from error
    return message.message_id


async def _send_digest(
    services: Services, deliver: Deliver, owner_id: int, week: str, now: datetime
) -> tuple[int, int | None]:
    """The digest of the week, unless it was sent before. Returns its number of items and the
    id of its message."""
    async with services.sessions() as session:
        row = await _row(session, owner_id, week)
        if row.sent_at is not None:
            return len(row.item_ids), None
        # The week is the seed: a second try of this digest shows the same random pick.
        planned = await plan.plan_digest(
            session, owner_id, now=now, seed=week, minutes=services.settings.digest_minutes
        )
        shown = [item.id for item, _ in planned.picks]
        row.item_ids = shown
        await session.commit()

    message_id: int | None = None
    if shown:
        message_id = await deliver(*cards.digest_message(planned, now=now))

    async with services.sessions() as session:
        await session.execute(
            sa.update(Digest)
            .where(Digest.owner_id == owner_id, Digest.period_key == week)
            .values(sent_at=now)
        )
        if shown:
            await session.execute(
                sa.update(Item).where(Item.id.in_(shown)).values(last_surfaced_at=now)
            )
            # Each item keeps in its history why a digest brought it back.
            session.add_all(
                Event(
                    owner_id=owner_id,
                    item_id=item.id,
                    actor=Actor.SYSTEM,
                    type=SURFACED,
                    payload={"period": week, "reason": pick.reason.value}
                    | ({"goal_id": pick.goal_id} if pick.goal_id is not None else {}),
                )
                for item, pick in planned.picks
            )
        await session.commit()
    log.info("digest.sent", week=week, items=len(shown), message_id=message_id)
    return len(shown), message_id


async def _send_stale_prompt(
    services: Services, deliver: Deliver, owner_id: int, week: str, now: datetime
) -> int:
    """The stale prompt of the week, unless it was sent before. Returns its number of items."""
    async with services.sessions() as session:
        row = await _row(session, owner_id, week)
        if row.stale_sent_at is not None:
            return len(row.stale_item_ids)
        found = await stale.stale_items(session, owner_id, now=now)
        row.stale_item_ids = [item.id for item in found]
        await session.commit()

    if found:
        await deliver(*cards.stale_message(found, now=now))

    async with services.sessions() as session:
        await session.execute(
            sa.update(Digest)
            .where(Digest.owner_id == owner_id, Digest.period_key == week)
            .values(stale_sent_at=now)
        )
        session.add_all(
            Event(
                owner_id=owner_id,
                item_id=item.id,
                actor=Actor.SYSTEM,
                type=stale.ASKED,
                payload={"period": week},
            )
            for item in found
        )
        await session.commit()
    log.info("digest.stale_sent", week=week, items=len(found))
    return len(found)


async def send(services: Services, owner_id: int, week: str, now: datetime) -> Sent:
    """Send the digest of ``week``, then its stale prompt: each one unless it was sent before.

    Each message is chosen and recorded in one transaction, then sent with no transaction
    open, then marked as sent. If the process stops between a sending and its mark, the job
    runs again and that message arrives two times. That window is short; a lost week is worse.
    """
    bot, chat_id = services.bot, services.settings.owner_telegram_id
    if bot is None or chat_id is None:
        raise Permanent(
            "no bot: UNSHELVE_TELEGRAM_BOT_TOKEN or UNSHELVE_OWNER_TELEGRAM_ID is not set"
        )

    async def deliver(text: str, keyboard: InlineKeyboardMarkup | None) -> int:
        return await _deliver(bot, chat_id, text, keyboard)

    items, message_id = await _send_digest(services, deliver, owner_id, week, now)
    prompted = await _send_stale_prompt(services, deliver, owner_id, week, now)
    return Sent(items=items, message_id=message_id, stale=prompted)
