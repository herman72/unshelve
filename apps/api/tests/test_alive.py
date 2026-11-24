"""The liveness file of a process (unshelve.alive): its marks, its age, and the check that
Docker runs. A fake clock gives each time, so no test waits."""

import logging
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from unshelve import alive

T = 1_795_392_000.0  # 2026-11-23 00:00 UTC, in seconds since 1970


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder of liveness files that this test owns. For unshelve.alive, a new folder is a
    new start: this process has written nothing there yet."""
    place = tmp_path / "alive"
    monkeypatch.setenv("UNSHELVE_ALIVE_DIR", str(place))
    return place


def at(moment: float) -> Callable[[], float]:
    """A clock that always says ``moment``."""
    return lambda: moment


def lines(file: Path) -> list[str]:
    return file.read_text(encoding="utf-8").splitlines()


def fields(file: Path) -> dict[str, str]:
    """The file as a reader of the contract sees it: one ``key=value`` on each line."""
    return dict(line.split("=", 1) for line in lines(file))


def mtime(file: Path) -> float:
    return file.stat().st_mtime


def close(moment: float) -> object:
    """Equal to ``moment`` up to 10 ms: a file system keeps the time in its own steps. Only
    an absolute limit: the relative limit of pytest.approx is 1795 s at this size of time."""
    return pytest.approx(moment, abs=0.01)


# --- The mark ----------------------------------------------------------------------------


def test_the_first_mark_writes_the_start_and_the_process(folder: Path) -> None:
    assert alive.mark("worker", clock=at(T)) is True

    file = folder / "worker"
    assert lines(file) == [f"started={int(T)}", f"pid={os.getpid()}"]
    # The time of the file is the time of the sign of life, on the clock of the mark.
    assert mtime(file) == close(T)


def test_a_later_mark_moves_only_the_time_and_keeps_the_start(folder: Path) -> None:
    alive.mark("worker", clock=at(T))
    before = (folder / "worker").read_text(encoding="utf-8")

    alive.mark("worker", clock=at(T + 15))

    assert (folder / "worker").read_text(encoding="utf-8") == before
    assert fields(folder / "worker")["started"] == str(int(T))
    assert mtime(folder / "worker") == close(T + 15)


@pytest.mark.parametrize(("later", "moved"), [(9.9, False), (10.0, True)])
def test_a_mark_moves_the_time_at_most_once_in_ten_seconds(
    folder: Path, later: float, *, moved: bool
) -> None:
    alive.mark("worker", clock=at(T))

    alive.mark("worker", clock=at(T + later))

    assert mtime(folder / "worker") == close(T + later if moved else T)


def test_a_clock_that_went_back_does_not_stop_the_marks(folder: Path) -> None:
    alive.mark("worker", clock=at(T))

    # Without this case, a clock set back by one hour would stop the marks for one hour.
    alive.mark("worker", clock=at(T - 3600))

    assert mtime(folder / "worker") == close(T - 3600)


def test_a_new_value_of_a_fact_writes_the_file_again_and_keeps_the_start(folder: Path) -> None:
    alive.mark("bot", clock=at(T), chats_ready="0/1")

    # One second later: a new value is written, also inside the ten seconds.
    alive.mark("bot", clock=at(T + 1), chats_ready="1/1")

    assert lines(folder / "bot") == [
        f"started={int(T)}",
        f"pid={os.getpid()}",
        "chats_ready=1/1",
    ]
    assert mtime(folder / "bot") == close(T + 1)


def test_the_same_fact_again_is_only_a_mark(folder: Path) -> None:
    alive.mark("bot", clock=at(T), chats_ready="1/1")

    alive.mark("bot", clock=at(T + 1), chats_ready="1/1")

    assert mtime(folder / "bot") == close(T)


def test_a_later_mark_without_the_fact_keeps_it(folder: Path) -> None:
    alive.mark("bot", clock=at(T), chats_ready="1/1")

    alive.mark("bot", clock=at(T + 20))

    assert fields(folder / "bot")["chats_ready"] == "1/1"


def test_a_removed_file_comes_back_with_the_same_start(folder: Path) -> None:
    alive.mark("worker", clock=at(T))
    (folder / "worker").unlink()

    assert alive.mark("worker", clock=at(T + 10)) is True

    assert fields(folder / "worker")["started"] == str(int(T))
    assert mtime(folder / "worker") == close(T + 10)


def test_two_names_have_two_files(folder: Path) -> None:
    alive.mark("worker", clock=at(T))
    alive.mark("bot", clock=at(T + 5))

    assert sorted(path.name for path in folder.iterdir()) == ["bot", "worker"]
    assert mtime(folder / "worker") == close(T)


# --- The clean stop ----------------------------------------------------------------------


def test_a_clean_stop_is_written_once(folder: Path) -> None:
    alive.mark("worker", clock=at(T))

    assert alive.stopped("worker", clock=at(T + 20)) is True
    assert alive.stopped("worker", clock=at(T + 30)) is True

    assert lines(folder / "worker") == [
        f"started={int(T)}",
        f"pid={os.getpid()}",
        f"stopped={int(T) + 20}",
    ]


def test_a_stop_keeps_the_facts(folder: Path) -> None:
    alive.mark("bot", clock=at(T), chats_ready="1/1")

    alive.stopped("bot", clock=at(T + 20))

    assert fields(folder / "bot") == {
        "started": str(int(T)),
        "pid": str(os.getpid()),
        "chats_ready": "1/1",
        "stopped": str(int(T) + 20),
    }


def test_a_stop_of_a_process_that_never_marked_writes_nothing(folder: Path) -> None:
    assert alive.stopped("worker", clock=at(T)) is False

    assert not (folder / "worker").exists()


# --- The file on the disk ----------------------------------------------------------------


def test_the_writes_leave_no_temporary_file_and_any_user_can_read_the_file(folder: Path) -> None:
    alive.mark("bot", clock=at(T), chats_ready="0/1")
    alive.mark("bot", clock=at(T + 1), chats_ready="1/1")
    alive.stopped("bot", clock=at(T + 2))

    assert sorted(path.name for path in folder.iterdir()) == ["bot"]
    # The file holds no secret. A reader that runs as another user can read it.
    assert stat.S_IMODE((folder / "bot").stat().st_mode) == 0o644


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write into a folder without write rights")
def test_a_failed_write_keeps_the_old_file_and_does_not_raise(folder: Path) -> None:
    alive.mark("bot", clock=at(T), chats_ready="0/1")
    before = (folder / "bot").read_text(encoding="utf-8")

    folder.chmod(0o555)
    try:
        written = alive.mark("bot", clock=at(T + 1), chats_ready="1/1")
    finally:
        folder.chmod(0o755)

    assert written is False
    assert (folder / "bot").read_text(encoding="utf-8") == before
    assert sorted(path.name for path in folder.iterdir()) == ["bot"]
    # The next mark writes the whole file, with the new value, also inside the ten seconds.
    assert alive.mark("bot", clock=at(T + 2)) is True
    assert fields(folder / "bot")["chats_ready"] == "1/1"


def test_a_folder_that_cannot_be_made_is_logged_once_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("UNSHELVE_ALIVE_DIR", str(blocker / "alive"))

    with caplog.at_level(logging.WARNING, logger="unshelve.alive"):
        first = alive.mark("worker", clock=at(T))
        second = alive.mark("worker", clock=at(T + 60))

    assert (first, second) == (False, False)
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "alive.cannot_write" in messages[0]
    assert str(blocker / "alive" / "worker") in messages[0]


@pytest.mark.parametrize("name", ["", "Worker", "../worker", "worker/x", ".worker", "w" * 40])
def test_a_name_that_is_not_a_plain_word_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        alive.mark(name)


@pytest.mark.parametrize(
    "facts",
    [
        {"Chats": "1/1"},
        {"chats-ready": "1/1"},
        {"started": "1"},
        {"pid": "1"},
        {"stopped": "1"},
        {"note": "two\nlines"},
        {"note": "a\rb"},
    ],
)
def test_a_fact_that_would_break_the_file_is_refused(folder: Path, facts: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="fact"):
        alive.mark("bot", clock=at(T), **facts)

    assert not (folder / "bot").exists()


def test_a_value_can_hold_an_equals_sign(folder: Path) -> None:
    # A reader splits each line at the first "=" (the contract in the docstring of the module).
    alive.mark("bot", clock=at(T), note="a=b")

    assert lines(folder / "bot")[-1] == "note=a=b"
    assert fields(folder / "bot")["note"] == "a=b"


def test_the_folder_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNSHELVE_ALIVE_DIR")
    assert alive.folder() == Path("/app/var/alive")

    monkeypatch.setenv("UNSHELVE_ALIVE_DIR", "/elsewhere")
    assert alive.folder() == Path("/elsewhere")

    # An empty value means "not set", as for the settings of .env.
    monkeypatch.setenv("UNSHELVE_ALIVE_DIR", "")
    assert alive.folder() == Path("/app/var/alive")


# --- The age, and the check of Docker ----------------------------------------------------


def test_the_age_is_the_time_since_the_last_mark(folder: Path) -> None:
    assert alive.age("worker", clock=at(T)) is None

    alive.mark("worker", clock=at(T))

    assert alive.age("worker", clock=at(T + 42.5)) == pytest.approx(42.5)
    # A mark from a clock that was ahead counts as new, not as a negative age.
    assert alive.age("worker", clock=at(T - 5)) == 0


@pytest.mark.parametrize(("seconds", "code"), [(0, 0), (179, 0), (180, 0), (181, 1)])
def test_the_check_passes_while_the_mark_is_at_most_max_age_old(
    folder: Path, capsys: pytest.CaptureFixture[str], seconds: int, code: int
) -> None:
    alive.mark("worker", clock=at(T))

    assert alive.main(["check", "worker", "--max-age", "180"], clock=at(T + seconds)) == code

    out, err = capsys.readouterr()
    if code == 0:
        assert (out, err) == (f"worker: a sign of life {seconds:.1f} s ago (limit 180 s)\n", "")
    else:
        assert (out, err) == ("", f"worker: no sign of life for {seconds:.1f} s (limit 180 s)\n")


def test_the_check_fails_without_a_file(folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert alive.main(["check", "bot", "--max-age", "180"], clock=at(T)) == 1

    assert capsys.readouterr().err == f"bot: no liveness file at {folder / 'bot'}\n"


def test_the_check_fails_on_the_file_of_a_process_that_stopped(
    folder: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    command = ["check", "worker", "--max-age", "180"]
    alive.mark("worker", clock=at(T))
    alive.stopped("worker", clock=at(T + 20))

    # The stop gave the file a new time, but its process has ended. After a restart, the volume
    # still holds this file: the check must wait for the first mark of the new process.
    assert alive.main(command, clock=at(T + 22)) == 1
    assert capsys.readouterr().err == (
        f"worker: stopped at {int(T) + 20}, no sign of life of a new process yet\n"
    )

    # A new process: it has marked nothing yet, so its first mark writes the whole file.
    monkeypatch.setattr(alive, "_marks", {})
    alive.mark("worker", clock=at(T + 30))

    assert "stopped" not in fields(folder / "worker")
    assert alive.main(command, clock=at(T + 32)) == 0


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["check"],
        ["check", "worker"],
        ["check", "worker", "--max-age", "soon"],
        ["check", "worker", "--max-age", "0"],
        ["check", "../worker", "--max-age", "180"],
        ["watch", "worker"],
    ],
)
def test_a_wrong_command_exits_1_and_never_2(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Docker reserves the exit code 2, and argparse uses it for a wrong command.
    assert alive.main(argv) == 1

    assert capsys.readouterr().err.count("\n") == 1


def test_the_command_of_the_healthcheck_reads_the_file(folder: Path) -> None:
    command = [sys.executable, "-m", "unshelve.alive", "check", "worker", "--max-age", "180"]
    environment = {**os.environ, "UNSHELVE_ALIVE_DIR": str(folder)}

    def check() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - this Python, with the command of the healthcheck
            command, env=environment, capture_output=True, text=True, check=False
        )

    before = check()
    alive.mark("worker")
    after = check()
    # A mark of 181 s ago, on the clock of the computer. The command uses its own clock, not a
    # clock of this test: with a wrong clock, each file that exists would pass.
    old = time.time() - 181
    os.utime(folder / "worker", (old, old))
    late = check()

    assert (before.returncode, before.stdout) == (1, "")
    assert before.stderr == f"worker: no liveness file at {folder / 'worker'}\n"
    assert after.returncode == 0, after.stderr
    assert after.stdout.startswith("worker: a sign of life ")
    assert (late.returncode, late.stdout) == (1, "")
    assert late.stderr.startswith("worker: no sign of life for 18"), late.stderr


# The check of Docker as a separate Python: the modules that it loads, after those of the start.
PROBE = """
import sys
before = set(sys.modules)
import runpy
sys.argv = ["unshelve.alive", "check", "worker", "--max-age", "180"]
try:
    runpy.run_module("unshelve.alive", run_name="__main__", alter_sys=True)
except SystemExit:
    pass
print("\\n".join(sorted(set(sys.modules) - before)))
"""


def test_the_check_loads_only_the_standard_library(folder: Path) -> None:
    # Docker starts the check every 30 s. The app with SQLAlchemy, aiogram and pydantic would
    # take far longer to load than the check takes to run.
    environment = {**os.environ, "UNSHELVE_ALIVE_DIR": str(folder)}

    done = subprocess.run(  # noqa: S603 - this Python, with a script of this test
        [sys.executable, "-c", PROBE], env=environment, capture_output=True, text=True, check=True
    )

    loaded = {name.split(".")[0] for name in done.stdout.split()}
    assert "unshelve" in loaded
    assert loaded & {"sqlalchemy", "aiogram", "pydantic", "httpx", "structlog"} == set()
    assert loaded - sys.stdlib_module_names - {"unshelve"} == set()
    # The version of the package comes from importlib.metadata, which reads each installed
    # package. In the image, it alone took longer than the whole check.
    assert "importlib.metadata" not in done.stdout.split()
