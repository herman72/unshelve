"""A sign of life of a process: a small file whose time says "I still work" (docs/learn/55).

The worker and the bot mark their file while they work. The healthcheck of Docker runs
``python -m unshelve.alive check NAME --max-age S``, and it fails when the mark is too old.
The word "heartbeat" means something else in this project: the message that keeps the lease of
a job (docs/learn/05). So this module says "liveness file" and "sign of life".

The file is a contract. The check of Docker reads it, and a later watcher will read it too.

Where: ``$UNSHELVE_ALIVE_DIR/<name>``, by default ``/app/var/alive/<name>``, a volume of the
stack. A name is a plain word: ``worker``, ``bot``.

What, in UTF-8, one ``key=value`` on each line. A reader ignores a key that it does not know::

    started=1795392000   the start of the process, in whole seconds since 1970 (UTC)
    pid=1                the id of the process in its container
    chats_ready=1/1      a fact of the process, if it has one (the bot writes this one)
    stopped=1795395600   only after a clean stop

A key matches ``[a-z][a-z0-9_]{0,31}``. A value has no line break, but it can hold "=", so a
reader splits each line at the first "=".

When: the time of the last change of the file (its mtime) is the time of the last sign of life.

The rules of the check:

- No file, or a time older than the limit: the check fails.
- A file with ``stopped`` belongs to a process that ended. The check fails on it, also when its
  time is new: the stop itself gave the file its time. After a restart, the volume still holds
  this file, so the new container is healthy only after the first mark of the new process.
- A crash writes no ``stopped``. Then the file of the old process passes the check of a new
  container until the file is older than the limit.

The rules of the writer:

- The first mark of a process writes the whole file: ``started``, ``pid`` and the facts, and
  no ``stopped``.
- A later mark only changes the time of the file, at most once in 10 seconds.
- A new value of a fact writes the whole file again, at once.
- A clean stop adds ``stopped`` once.
- Each write of the whole file is atomic: a temporary file in the same folder, whose name
  starts with ".", then fsync, then a rename. So a reader never sees half a file. A reader
  ignores each name that starts with ".".
- The file holds no secret, and its mode is 0644.
- A mark never raises an OSError. A file that cannot be written gives one warning in the log,
  and the check of Docker then shows the problem.

This module uses only the standard library. Docker starts the check every 30 seconds, and the
modules of the app (SQLAlchemy, aiogram, pydantic) would take far longer to load than the
check takes to run. A test makes sure of it.
"""

import argparse
import contextlib
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

FOLDER_VARIABLE = "UNSHELVE_ALIVE_DIR"
DEFAULT_FOLDER = Path("/app/var/alive")
# A mark changes the time of the file at most this often, in seconds. A loop that turns each
# second must not write to the disk each second.
MARK_EVERY = 10.0

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
_OWN_KEYS = frozenset({"started", "pid", "stopped"})

type Clock = Callable[[], float]

log = logging.getLogger(__name__)


class _Mark:
    """What this process keeps about one of its files. A plain class: a dataclass would load
    the module inspect, and the check must load little."""

    __slots__ = ("facts", "marked", "started", "stopped")

    def __init__(self, started: int) -> None:
        self.started = started
        self.facts: dict[str, str] = {}
        # The clock at the last write or mark that worked. None: the next mark writes the file.
        self.marked: float | None = None
        self.stopped: int | None = None


_marks: dict[Path, _Mark] = {}
_failing: set[Path] = set()


def folder() -> Path:
    """The folder of the liveness files."""
    return Path(os.environ.get(FOLDER_VARIABLE) or DEFAULT_FOLDER)


def path(name: str) -> Path:
    """The liveness file of ``name``. A name is a plain word, so it cannot leave the folder."""
    if not _NAME.fullmatch(name):
        msg = f"a liveness name is a plain word of a-z, 0-9, _ and -, not {name!r}"
        raise ValueError(msg)
    return folder() / name


def mark(name: str, *, clock: Clock = time.time, **facts: str | int) -> bool:
    """Leave a sign of life in the file of ``name``. Returns False when the file could not be
    written; it never raises an OSError.

    ``facts`` are values that the file shows next to the start, for example
    ``chats_ready="1/1"``. A fact stays in the file until a new value replaces it.
    """
    file = path(name)
    new = _facts(facts)
    now = clock()
    known = _marks.get(file)
    if known is None:
        known = _marks[file] = _Mark(started=int(now))
    changed = {key: value for key, value in new.items() if known.facts.get(key) != value}
    known.facts.update(changed)
    if not changed and known.marked is not None and 0 <= now - known.marked < MARK_EVERY:
        return True
    try:
        if changed or known.marked is None:
            _write(file, known, now)
        else:
            try:
                os.utime(file, (now, now))
            except FileNotFoundError:
                # Somebody removed the file, for example with its volume. Write it again.
                _write(file, known, now)
    except OSError as error:
        known.marked = None
        _cannot_write(file, error)
        return False
    known.marked = now
    _failing.discard(file)
    return True


def stopped(name: str, *, clock: Clock = time.time) -> bool:
    """Write ``stopped`` into the file of ``name``: the process stops cleanly. Only the first
    call writes. Returns False when this process never marked the file, or when the write
    failed; it never raises an OSError."""
    file = path(name)
    known = _marks.get(file)
    if known is None:
        return False
    if known.stopped is not None:
        return True
    now = clock()
    known.stopped = int(now)
    try:
        _write(file, known, now)
    except OSError as error:
        _cannot_write(file, error)
        return False
    known.marked = now
    _failing.discard(file)
    return True


def age(name: str, *, clock: Clock = time.time) -> float | None:
    """The seconds since the last sign of life of ``name``, or None when there is no file.

    The age uses ``time.time()``, the clock of the time of the file. A mark from a clock that
    was ahead counts as new (age 0), not as a negative age.
    """
    try:
        changed = path(name).stat().st_mtime
    except OSError:
        return None
    return max(clock() - changed, 0.0)


def check(name: str, max_age: float, *, clock: Clock = time.time) -> tuple[bool, str]:
    """Whether the sign of life of ``name`` is at most ``max_age`` seconds old, and one line
    that says why. The file of a process that stopped fails, whatever its time."""
    seconds = age(name, clock=clock)
    if seconds is None:
        return False, f"{name}: no liveness file at {path(name)}"
    stop = _stopped_at(path(name))
    if stop is not None:
        return False, f"{name}: stopped at {stop}, no sign of life of a new process yet"
    if seconds > max_age:
        return False, f"{name}: no sign of life for {seconds:.1f} s (limit {max_age:g} s)"
    return True, f"{name}: a sign of life {seconds:.1f} s ago (limit {max_age:g} s)"


def _stopped_at(file: Path) -> str | None:
    """The value of ``stopped`` in ``file``, or None. A file that cannot be read gives None:
    then only its time counts."""
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key == "stopped":
            return value
    return None


def _facts(facts: dict[str, str | int]) -> dict[str, str]:
    """The facts as text. A key or a value that would break the format is refused."""
    text: dict[str, str] = {}
    for key, value in facts.items():
        if not _KEY.fullmatch(key) or key in _OWN_KEYS:
            msg = f"a fact needs a key of a-z, 0-9 and _ that is not {sorted(_OWN_KEYS)}: {key!r}"
            raise ValueError(msg)
        shown = str(value)
        if "\n" in shown or "\r" in shown:
            msg = f"the value of the fact {key} has a line break"
            raise ValueError(msg)
        text[key] = shown
    return text


def _write(file: Path, known: _Mark, now: float) -> None:
    """Replace ``file`` atomically, with ``now`` as its time."""
    import tempfile  # a writer needs it, the check of Docker does not

    lines = [f"started={known.started}", f"pid={os.getpid()}"]
    lines += [f"{key}={value}" for key, value in known.facts.items()]
    if known.stopped is not None:
        lines.append(f"stopped={known.stopped}")
    file.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=file.parent, prefix=f".{file.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.utime(temporary, (now, now))
        temporary.replace(file)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _cannot_write(file: Path, error: OSError) -> None:
    """One warning for each file, until a write of it works again."""
    if file not in _failing:
        _failing.add(file)
        log.warning("alive.cannot_write: %s (%s)", file, error)


# --- The command of the healthcheck ------------------------------------------------------


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """A wrong command is exit code 1, not the 2 of argparse: Docker reserves 2."""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _positive(text: str) -> float:
    value = float(text)
    if not value > 0:
        msg = f"must be more than 0, not {text}"
        raise argparse.ArgumentTypeError(msg)
    return value


def main(argv: Sequence[str] | None = None, *, clock: Clock = time.time) -> int:
    """``python -m unshelve.alive check NAME --max-age S``: exit code 0 when the sign of life
    of NAME is at most S seconds old, else 1, with one line on stderr."""
    parser = _Parser(prog="python -m unshelve.alive", description="Check a sign of life.")
    commands = parser.add_subparsers(dest="command", required=True)
    check_command = commands.add_parser("check", help="check the age of the sign of life")
    check_command.add_argument("name", help="worker or bot")
    check_command.add_argument(
        "--max-age", type=_positive, required=True, metavar="SECONDS", help="the oldest mark"
    )
    try:
        arguments = parser.parse_args(argv)
        ok, line = check(arguments.name, arguments.max_age, clock=clock)
    except (_UsageError, ValueError) as error:
        sys.stderr.write(f"{parser.prog}: {error}\n")
        return 1
    (sys.stdout if ok else sys.stderr).write(line + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
