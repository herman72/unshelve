"""The weekly digest. Each test name is one rule. No message goes to Telegram."""

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from tests.bot.support import OWNER_TELEGRAM_ID, PretendTelegram, offline_bot
from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.enums import JobKind
from unshelve.contracts.jobs import Permanent, Retry, Services
from unshelve.db.models import Digest, Item, Job
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.resurface import weekly
from unshelve.services import build_services

Sessions = async_sessionmaker[AsyncSession]
HELSINKI = ZoneInfo("Europe/Helsinki")


def _at(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    """A moment on a clock in Helsinki, in 2026."""
    return datetime(2026, month, day, hour, minute, tzinfo=HELSINKI)


def _settings(**values: Any) -> Settings:
    fields: dict[str, Any] = {
        "env": "test",
        "telegram_bot_token": "42:TEST",
        "owner_telegram_id": OWNER_TELEGRAM_ID,
        "timezone": "Europe/Helsinki",
    }
    return Settings(**(fields | values))


# --- When a digest is due -----------------------------------------------------------------
# 2026-10-18 is a Sunday. Helsinki is in summer time until 2026-10-25.


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_at(18, 9, 30), _at(18, 9)),  # Sunday, after 09:00
        (_at(18, 9, 0), _at(18, 9)),  # the moment itself
        (_at(18, 8, 59), _at(11, 9)),  # Sunday, before 09:00: the Sunday before
        (_at(21, 15), _at(18, 9)),  # Wednesday
        (_at(24, 23, 59), _at(18, 9)),  # Saturday night
    ],
)
def test_the_last_moment_of_a_digest(now: datetime, expected: datetime) -> None:
    assert weekly.last_send_time(now, HELSINKI, "sunday", 9) == expected


def test_09_00_in_helsinki_is_a_different_utc_time_in_summer_and_in_winter() -> None:
    summer = weekly.last_send_time(_at(18, 10), HELSINKI, "sunday", 9)
    winter = weekly.last_send_time(_at(1, 10, month=11), HELSINKI, "sunday", 9)

    assert summer.astimezone(UTC).hour == 6
    assert winter.astimezone(UTC).hour == 7


def test_the_day_when_the_clocks_go_back_still_has_its_digest_at_09_00() -> None:
    # On 2026-10-25 the clocks in Helsinki go back at 04:00. 09:00 is on the clock one time.
    due = weekly.last_send_time(_at(25, 9, 5), HELSINKI, "sunday", 9)

    assert due == _at(25, 9)
    assert due.astimezone(UTC) == datetime(2026, 10, 25, 7, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "week"),
    [
        (_at(18, 9), "2026-W42"),
        (_at(19, 10), "2026-W42"),  # Monday: the Mac slept on Sunday, so the digest comes late
        (_at(20, 9), "2026-W42"),  # 48 hours late: the last moment for it
        (_at(20, 9, 1), None),  # later: the week is skipped
        (_at(24, 12), None),  # Saturday
        (_at(18, 8, 59), None),  # the last Sunday was a week ago
    ],
)
def test_a_digest_is_due_from_its_moment_for_48_hours(now: datetime, week: str | None) -> None:
    assert weekly.due_week(now, _settings()) == week


def test_the_day_and_the_hour_come_from_the_settings() -> None:
    settings = _settings(digest_day="monday", digest_hour=7, timezone="UTC")

    assert weekly.due_week(datetime(2026, 10, 19, 7, 0, tzinfo=UTC), settings) == "2026-W43"
    assert weekly.due_week(datetime(2026, 10, 19, 6, 59, tzinfo=UTC), settings) is None


def test_a_sunday_at_the_start_of_a_year_belongs_to_the_last_week_of_the_year_before() -> None:
    # ISO 8601: a week belongs to the year of its Thursday. 2026 has 53 weeks.
    assert weekly.week_of(date(2027, 1, 3)) == "2026-W53"
    assert weekly.week_of(date(2026, 10, 18)) == "2026-W42"


# --- One job for each week ----------------------------------------------------------------


async def _jobs(sessions: Sessions) -> list[Job]:
    async with sessions() as db:
        return list((await db.execute(sa.select(Job).order_by(Job.id))).scalars().all())


async def test_a_due_digest_gets_its_row_and_one_job(committed_db: Sessions) -> None:
    week = await weekly.plan_digest(committed_db, _settings(), _at(18, 9, 1))

    assert week == "2026-W42"
    (job,) = await _jobs(committed_db)
    assert job.kind == JobKind.SEND_DIGEST
    assert job.payload["period_key"] == "2026-W42"
    async with committed_db() as db:
        (row,) = (await db.execute(sa.select(Digest))).scalars().all()
    assert (row.period_key, row.sent_at) == ("2026-W42", None)


async def test_each_minute_after_that_adds_nothing(committed_db: Sessions) -> None:
    await weekly.plan_digest(committed_db, _settings(), _at(18, 9, 1))

    again = [await weekly.plan_digest(committed_db, _settings(), _at(18, 9, m)) for m in (2, 3)]

    assert again == [None, None]
    assert len(await _jobs(committed_db)) == 1


async def test_two_workers_that_plan_at_the_same_moment_make_one_job(
    committed_db: Sessions,
) -> None:
    weeks = await asyncio.gather(
        *(weekly.plan_digest(committed_db, _settings(), _at(18, 9, 1)) for _ in range(5))
    )

    assert sorted(weeks, key=str) == ["2026-W42", None, None, None, None]
    assert len(await _jobs(committed_db)) == 1


@pytest.mark.parametrize(
    "settings",
    [
        _settings(telegram_bot_token=None),
        _settings(owner_telegram_id=None),
    ],
    ids=["no bot", "no owner"],
)
async def test_without_a_bot_or_an_owner_no_digest_is_planned(
    committed_db: Sessions, settings: Settings
) -> None:
    assert await weekly.plan_digest(committed_db, settings, _at(18, 9, 1)) is None
    assert await _jobs(committed_db) == []


async def test_a_digest_that_is_not_due_is_not_planned(committed_db: Sessions) -> None:
    assert await weekly.plan_digest(committed_db, _settings(), _at(21, 12)) is None
    assert await _jobs(committed_db) == []


# --- Sending --------------------------------------------------------------------------------


@pytest.fixture
def telegram() -> PretendTelegram:
    return PretendTelegram()


@pytest.fixture
def services(
    committed_db: Sessions, private_database_url: str, telegram: PretendTelegram
) -> Services:
    built = build_services(_settings(database_url=private_database_url), committed_db)
    return dataclasses.replace(built, bot=offline_bot(telegram))


async def _owner_with_items(sessions: Sessions, *titles: str, days_ago: float = 1) -> int:
    at = datetime.now(UTC) - timedelta(days=days_ago)
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
        for number, title in enumerate(titles):
            moment = at + timedelta(minutes=number)
            await db.execute(
                sa.insert(Item).values(
                    owner_id=owner_id,
                    title=title,
                    first_captured_at=moment,
                    last_captured_at=moment,
                )
            )
        await db.commit()
    return owner_id


async def _claim(sessions: Sessions, owner_id: int, week: str) -> None:
    async with sessions() as db:
        assert await weekly.claim_week(db, owner_id, week)
        await db.commit()


async def _row(sessions: Sessions, week: str) -> Digest:
    async with sessions() as db:
        return (await db.execute(sa.select(Digest).where(Digest.period_key == week))).scalar_one()


async def test_the_digest_goes_to_the_owner_and_is_marked_as_sent(
    services: Services, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    owner_id = await _owner_with_items(committed_db, "One", "Two")
    await _claim(committed_db, owner_id, "2026-W42")
    now = datetime.now(UTC)

    sent = await weekly.send(services, owner_id, "2026-W42", now)

    (message,) = telegram.sent
    assert message.chat_id == OWNER_TELEGRAM_ID
    assert message.parse_mode == "HTML"
    assert message.text.startswith("<b>Your digest</b>\nYou saved 2 things in the last 7 days.")
    assert isinstance(message.reply_markup, InlineKeyboardMarkup)
    assert sent.items == 2
    row = await _row(committed_db, "2026-W42")
    assert row.sent_at == now
    assert len(row.item_ids) == 2


async def test_a_job_that_runs_again_sends_nothing_again(
    services: Services, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    owner_id = await _owner_with_items(committed_db, "One")
    await _claim(committed_db, owner_id, "2026-W42")
    await weekly.send(services, owner_id, "2026-W42", datetime.now(UTC))

    again = await weekly.send(services, owner_id, "2026-W42", datetime.now(UTC))

    assert len(telegram.sent) == 1
    assert (again.items, again.message_id) == (1, None)


async def test_a_week_with_nothing_to_show_sends_no_message(
    services: Services, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
        await db.commit()
    await _claim(committed_db, owner_id, "2026-W42")

    sent = await weekly.send(services, owner_id, "2026-W42", datetime.now(UTC))

    assert telegram.sent == []
    assert sent.items == 0
    assert (await _row(committed_db, "2026-W42")).sent_at is not None


class FailingTelegram(PretendTelegram):
    """A Telegram that refuses each message with the error that ``refuse`` makes."""

    def __init__(self, refuse: Callable[[TelegramMethod[Any]], Exception]) -> None:
        super().__init__()
        self._refuse = refuse

    async def make_request[T](
        self,
        bot: Bot,
        method: TelegramMethod[T],
        timeout: int | None = None,  # noqa: ASYNC109 - the signature of the library
    ) -> T:
        if isinstance(method, SendMessage):
            raise self._refuse(method)
        return await super().make_request(bot, method, timeout)


@pytest.mark.parametrize(
    ("telegram", "signal"),
    [
        (
            FailingTelegram(lambda m: TelegramRetryAfter(m, "Too Many Requests", retry_after=7)),
            Retry,
        ),
        (FailingTelegram(lambda m: TelegramNetworkError(m, "no connection")), Retry),
        (
            FailingTelegram(lambda m: TelegramForbiddenError(m, "Forbidden: bot was blocked")),
            Permanent,
        ),
        (FailingTelegram(lambda m: TelegramUnauthorizedError(m, "Unauthorized")), Permanent),
        (
            FailingTelegram(lambda m: TelegramBadRequest(m, "Bad Request: chat not found")),
            Permanent,
        ),
        (FailingTelegram(lambda m: TelegramServerError(m, "Internal Server Error")), Retry),
    ],
    ids=[
        "Telegram asks to wait",
        "no connection",
        "the owner blocked the bot",
        "a wrong token",
        "an unknown chat",
        "an error of the server of Telegram",
    ],
)
async def test_an_error_of_telegram_gives_a_retry_or_a_permanent_failure(
    services: Services, committed_db: Sessions, signal: type[Exception]
) -> None:
    owner_id = await _owner_with_items(committed_db, "One")
    await _claim(committed_db, owner_id, "2026-W42")

    with pytest.raises(signal) as raised:
        await weekly.send(services, owner_id, "2026-W42", datetime.now(UTC))

    if isinstance(raised.value, Retry) and "wait" in raised.value.reason:
        assert raised.value.after == timedelta(seconds=7)
    # Not sent, so the next attempt sends it.
    assert (await _row(committed_db, "2026-W42")).sent_at is None


async def test_a_second_attempt_shows_the_same_items_as_the_first(
    committed_db: Sessions, private_database_url: str
) -> None:
    owner_id = await _owner_with_items(committed_db, "Old one", days_ago=40)
    await _owner_with_items(committed_db, "Old two", "Old three", days_ago=50)
    await _claim(committed_db, owner_id, "2026-W42")
    built = build_services(_settings(database_url=private_database_url), committed_db)
    down = FailingTelegram(lambda m: TelegramNetworkError(m, "down"))
    broken = dataclasses.replace(built, bot=offline_bot(down))
    working_telegram = PretendTelegram()
    working = dataclasses.replace(built, bot=offline_bot(working_telegram))

    with pytest.raises(Retry):
        await weekly.send(broken, owner_id, "2026-W42", datetime.now(UTC))
    first = (await _row(committed_db, "2026-W42")).item_ids
    await weekly.send(working, owner_id, "2026-W42", datetime.now(UTC))

    # The older thing is chosen at random, with the week as its seed.
    assert (await _row(committed_db, "2026-W42")).item_ids == first
    assert len(working_telegram.sent) == 1


async def test_without_a_bot_the_job_fails_for_good(
    committed_db: Sessions, private_database_url: str
) -> None:
    owner_id = await _owner_with_items(committed_db, "One")
    await _claim(committed_db, owner_id, "2026-W42")
    services = build_services(
        _settings(database_url=private_database_url, telegram_bot_token=None), committed_db
    )

    with pytest.raises(Permanent):
        await weekly.send(services, owner_id, "2026-W42", datetime.now(UTC))


async def test_the_worker_plans_and_sends_the_digest_of_the_week(
    services: Services, telegram: PretendTelegram, committed_db: Sessions
) -> None:
    await _owner_with_items(committed_db, "One")
    discover()
    worker = Worker(services, registry, WorkerConfig(worker_id="w1"))
    assert JobKind.SEND_DIGEST in registry.kinds()

    await weekly.plan_digest(committed_db, services.settings, _at(18, 9, 1))
    while await worker.run_one():
        pass

    assert len(telegram.sent) == 1


async def test_the_planner_stops_when_the_worker_stops(services: Services) -> None:
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(weekly.plan_forever(services, stop), timeout=2)


def test_the_day_is_the_day_of_the_clock_in_the_time_zone_also_near_midnight() -> None:
    # Monday 00:30 in Helsinki is still Sunday in UTC.
    settings = _settings(digest_day="monday", digest_hour=0)
    monday = datetime(2026, 10, 19, 0, 30, tzinfo=HELSINKI)

    assert weekly.last_send_time(monday, HELSINKI, "monday", 0) == datetime(
        2026, 10, 19, 0, 0, tzinfo=HELSINKI
    )
    assert weekly.due_week(monday, settings) == "2026-W43"


async def test_the_planner_goes_on_after_an_error(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    stop = asyncio.Event()

    async def fails_the_first_time(*_: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            msg = "the database restarted"
            raise ConnectionError(msg)
        stop.set()

    monkeypatch.setattr(weekly, "plan_digest", fails_the_first_time)
    monkeypatch.setattr(weekly, "CHECK_EVERY", timedelta(milliseconds=10))

    with capture_logs() as logs:
        await asyncio.wait_for(weekly.plan_forever(services, stop), timeout=5)

    assert calls == 2
    assert "digest.plan_error" in [entry["event"] for entry in logs]


async def test_the_worker_process_runs_the_jobs_and_the_planner(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []

    async def planner(_: Services, __: asyncio.Event) -> None:
        started.append("planner")

    class OneWorker(Worker):
        async def run_forever(self, stop: asyncio.Event) -> None:
            started.append("jobs")

    monkeypatch.setattr(weekly, "plan_forever", planner)
    stop = asyncio.Event()

    await weekly.run_worker(
        OneWorker(services, registry, WorkerConfig(worker_id="w1")), services, stop
    )

    assert sorted(started) == ["jobs", "planner"]


def test_the_services_have_a_bot_only_with_a_token(
    private_database_url: str, committed_db: Sessions
) -> None:
    with_token = build_services(_settings(database_url=private_database_url), committed_db)
    without = build_services(
        _settings(database_url=private_database_url, telegram_bot_token=None), committed_db
    )

    assert isinstance(with_token.bot, Bot)
    assert with_token.bot.token == "42:TEST"  # noqa: S105 - the made-up token of the tests
    assert without.bot is None


async def test_the_older_thing_is_chosen_with_the_week_as_the_seed(
    services: Services, committed_db: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_id = await _owner_with_items(committed_db, "One")
    await _claim(committed_db, owner_id, "2026-W42")
    seeds: list[str] = []
    pick = weekly.digest.pick

    async def remember_the_seed(*args: Any, **kwargs: Any) -> Any:
        seeds.append(kwargs["seed"])
        return await pick(*args, **kwargs)

    monkeypatch.setattr(weekly.digest, "pick", remember_the_seed)

    await weekly.send(services, owner_id, "2026-W42", datetime.now(UTC))

    # So each attempt of one week shows the same older thing.
    assert seeds == ["2026-W42"]
