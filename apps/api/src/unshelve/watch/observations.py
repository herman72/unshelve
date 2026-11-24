"""What one check of the watcher saw: the liveness files, the database, the web app and the
backup (docs/learn/55).

Each time is in seconds since 1970 on the clock of ``time.time()``, the clock of the times of
files. A count is an ``int``, and money is a ``Decimal``. A field of ``Observation`` that is
None is unknown, except ``database_error``: there, None means that no error is known. Inside a
value, the meaning of None depends on the field. For example, ``Liveness.mtime`` None means that
the file is missing, ``Queue.oldest_ready_at`` None means that no job is ready, and a provider
field of ``DatabaseView`` that is None is not in use.
"""

from dataclasses import dataclass
from decimal import Decimal

type KindCount = tuple[str, int]  # (a job kind or an event type, a count)


@dataclass(frozen=True, slots=True)
class Liveness:
    """One liveness file (unshelve.alive). mtime None: the file is missing."""

    mtime: float | None
    started: int | None = None
    stopped: int | None = None
    chats_ready: tuple[int, int] | None = None  # (N, M): N of the M capture chats are ready


@dataclass(frozen=True, slots=True)
class Refusals:
    """The errors that a person must fix of one provider, and its newest success."""

    # The newest marked failure in view: a pending job or a *_failed event.
    newest_failure_at: float | None
    # The newest proof that the provider works.
    newest_success_at: float | None
    # The text of the newest marked failure, raw, at most 300 characters.
    error: str | None = None
    # The pending marked jobs that are newer than the success, by kind.
    waiting: tuple[KindCount, ...] = ()
    next_try_at: float | None = None  # min(run_after) of those jobs
    last_try_at: float | None = None  # max(run_after) of those jobs
    # The marked *_failed events that are newer than the success: (type, items).
    gave_up: tuple[KindCount, ...] = ()


@dataclass(frozen=True, slots=True)
class Budget:
    spent_usd: Decimal
    budget_usd: Decimal
    waiting: int  # pending jobs that wait for the next day


@dataclass(frozen=True, slots=True)
class Queue:
    ready: int
    oldest_ready_at: float | None  # min(run_after) of the ready jobs
    # The newest end of an attempt: finished, failed or rescheduled.
    newest_attempt_end_at: float | None


@dataclass(frozen=True, slots=True)
class DeadJobs:
    totals: tuple[KindCount, ...]  # dead jobs by kind, sorted by kind; they only grow
    newest_kind: str | None = None
    newest_error: str | None = None  # raw, at most 300 characters


@dataclass(frozen=True, slots=True)
class DatabaseView:
    """Everything that the check read from the database, in one transaction."""

    queue: Queue
    dead: DeadJobs
    model: Refusals | None = None  # None: UNSHELVE_PROVIDERS is not "live"
    voyage: Refusals | None = None  # None: UNSHELVE_EMBEDDER is not "voyage"
    budget: Budget | None = None  # None: UNSHELVE_PROVIDERS is not "live"


@dataclass(frozen=True, slots=True)
class BackupVerdict:
    ok: bool
    text: str
    at: float  # the time of the verdict


@dataclass(frozen=True, slots=True)
class Observation:
    """What one check saw. For ``worker``, ``bot``, ``database``, ``web_ok`` and ``backup``, None
    is unknown. For ``database_error``, None means that no error is known."""

    worker: Liveness | None
    bot: Liveness | None
    database: DatabaseView | None = None
    database_error: str | None = None  # set when the connection or a query failed
    web_ok: bool | None = None
    backup: BackupVerdict | None = None
