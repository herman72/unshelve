"""The scripts of the backup, without a database (docs/learn/50): their syntax, the helpers that
they share in lib.sh, and the rule of prune.sh about which backups stay. prune.sh is POSIX sh and
awk, so it runs here as it runs in the service `backup`."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tests.deploy.support import REPO

SCRIPTS = REPO / "deploy" / "backup"


@pytest.mark.parametrize(
    ("shell", "script"),
    [
        ("bash", "backup.sh"),
        ("bash", "lib.sh"),
        ("sh", "prune.sh"),
        ("bash", "restore.sh"),
        ("bash", "drill.sh"),
    ],
)
def test_each_script_has_a_valid_syntax(shell: str, script: str) -> None:
    done = subprocess.run(  # noqa: S603 - a shell of this computer on a script of this repository
        [shell, "-n", str(SCRIPTS / script)], capture_output=True, text=True, check=False
    )

    assert (done.returncode, done.stderr) == (0, "")


# The helpers of backup.sh that a second script of the backup can use too.
SHARED_HELPERS = ["log", "status_get", "status_set", "epoch_of", "give_to_owner"]


def _functions(script: Path) -> list[str]:
    """The name of each shell function that a script defines, once for each definition."""
    return re.findall(r"^\s*([A-Za-z_]\w*)\s*\(\)\s*\{", script.read_text(), flags=re.MULTILINE)


def test_each_shared_helper_is_defined_once_and_in_lib_sh() -> None:
    places = {
        helper: [
            script.name
            for script in sorted(SCRIPTS.glob("*.sh"))
            for function in _functions(script)
            if function == helper
        ]
        for helper in SHARED_HELPERS
    }

    # A second copy in backup.sh would hide the copy of lib.sh, and the two could drift apart.
    assert places == {helper: ["lib.sh"] for helper in SHARED_HELPERS}


def _with_lib(
    tmp_path: Path, commands: str, fakes: dict[str, str] | None = None, **environment: str
) -> subprocess.CompletedProcess[str]:
    """Commands in a bash of this computer that loads lib.sh as backup.sh does. `fakes` maps a
    command to the text of a script that comes first in PATH under its name."""
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    for name, text in (fakes or {}).items():
        (bin_folder / name).write_text(text)
        (bin_folder / name).chmod(0o755)
    program = f'set -euo pipefail\n. "$1"\n{commands}'
    return subprocess.run(  # noqa: S603 - a shell of this computer on a script of this repository
        ["bash", "-c", program, "lib", str(SCRIPTS / "lib.sh")],  # noqa: S607
        env=os.environ
        | {"PATH": f"{bin_folder}:{os.environ['PATH']}", "STATE": str(tmp_path / "state")}
        | environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_log_prints_one_json_line_for_a_double_quote_a_backslash_and_a_newline(
    tmp_path: Path,
) -> None:
    done = _with_lib(
        tmp_path, 'log backup.failed "$TEXT"', TEXT='pg_dump: "items" failed\nin C:\\dumps\\x'
    )

    assert (done.returncode, done.stderr) == (0, "")
    [line] = done.stdout.splitlines()
    record = json.loads(line)
    # A newline and a double quote become a space, and a backslash becomes a single quote.
    assert (record["event"], record["detail"]) == (
        "backup.failed",
        "pg_dump:  items  failed in C:'dumps'x",
    )
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record["time"])


def test_log_writes_the_time_in_utc_with_a_24_hour_clock(tmp_path: Path) -> None:
    # A date that always answers for 2026-11-24 13:01:02 UTC stands in for the clock: the date of
    # macOS takes the second with -r, the date of Linux with -d @. In Helsinki that second is
    # 15:01, and a 12-hour clock gives 01.
    second = int(datetime(2026, 11, 24, 13, 1, 2, tzinfo=UTC).timestamp())
    moment = f"-r {second}" if sys.platform == "darwin" else f"-d @{second}"
    clock = f'#!/bin/sh\nexec {shutil.which("date")} {moment} "$@"\n'

    done = _with_lib(tmp_path, "log backup.ok done", fakes={"date": clock}, TZ="Europe/Helsinki")

    assert (done.returncode, done.stderr) == (0, "")
    assert json.loads(done.stdout)["time"] == "2026-11-24T13:01:02Z"


def test_epoch_of_gives_date_the_utc_time_of_a_name(tmp_path: Path) -> None:
    # The date of macOS has no -d, so a date that prints its arguments stands in. The hours, the
    # minutes and the seconds of the name are all different: a swap of two of them shows.
    done = _with_lib(
        tmp_path, "epoch_of 20261124T090102Z", fakes={"date": '#!/bin/sh\nprintf "%s\\n" "$@"\n'}
    )

    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.splitlines() == ["-u", "-d", "2026-11-24T09:01:02Z", "+%s"]


def test_status_set_keeps_one_line_for_each_key(tmp_path: Path) -> None:
    # macOS has no flock, and one writer needs no lock: a flock that does nothing stands in.
    # test_backup.py runs two writers with the flock of the image.
    done = _with_lib(
        tmp_path,
        "status_set k1 a k2 b\nstatus_set k1 c k2 d\nstatus_set k e\n"
        "status_set last_ok 1\nstatus_set ok 2\nstatus_get k2",
        fakes={"flock": "#!/bin/sh\nexit 0\n"},
    )

    assert (done.returncode, done.stdout, done.stderr) == (0, "d\n", "")
    # Each change replaces the old line of each key that it sets, not only of the first one. The
    # key k is the start of k1 and k2, and the key ok is the end of last_ok: each one replaces
    # only its own line.
    assert sorted((tmp_path / "state" / "status").read_text().splitlines()) == [
        "k1=c",
        "k2=d",
        "k=e",
        "last_ok=1",
        "ok=2",
    ]


def test_status_get_gives_back_each_equals_sign_of_a_value(tmp_path: Path) -> None:
    # The text of an error can hold "=". The health check and make backup-status show the whole
    # value, not only the text before its first "=".
    value = "error: a=b and c=d"
    done = _with_lib(
        tmp_path,
        'status_set last_error_text "$VALUE"\nstatus_get last_error_text',
        fakes={"flock": "#!/bin/sh\nexit 0\n"},
        VALUE=value,
    )

    assert (done.returncode, done.stdout, done.stderr) == (0, f"{value}\n", "")


def test_status_get_prints_nothing_before_the_first_status(tmp_path: Path) -> None:
    # The health check and make backup-status also run before the first backup.
    done = _with_lib(tmp_path, "status_get last_ok")

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_status_set_puts_a_new_file_in_place_and_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    # The health check reads the status without the lock. A change writes a new file and renames
    # it over the status, so a reader sees the old file or the new one, never half of one. After
    # each change the status is another file, with another inode, and no temporary file stays.
    # The new file lies in the folder of the status: in the service, /tmp is another file system,
    # and a mv from there copies the file. A mv that writes down its arguments, then runs the real
    # one, shows the place of the new file.
    calls = tmp_path / "mv-calls"
    mv = f'#!/bin/sh\nprintf "%s\\t%s\\n" "$1" "$2" >>{calls}\nexec {shutil.which("mv")} "$@"\n'
    done = _with_lib(
        tmp_path,
        'status_set k1 a\nls -i "$STATE/status"\nstatus_set k1 b k2 c\nls -i "$STATE/status"',
        fakes={"flock": "#!/bin/sh\nexit 0\n", "mv": mv},
    )

    assert (done.returncode, done.stderr) == (0, "")
    [first, second] = [line.split()[0] for line in done.stdout.splitlines()]
    assert first != second
    assert sorted(path.name for path in (tmp_path / "state").iterdir()) == ["status", "status.lock"]
    status = tmp_path / "state" / "status"
    moves = [line.split("\t") for line in calls.read_text().splitlines()]
    assert [(Path(source).parent, Path(target)) for source, target in moves] == [
        (status.parent, status)
    ] * 2


def test_lib_sh_only_defines_functions_when_it_loads(tmp_path: Path) -> None:
    # One shell writes its state into a file before it loads lib.sh, and again after it. The
    # state is the options, the umask, the traps, the folder, its files, the names of the
    # variables, the exported variables, and the values of IFS, PATH and STATE. A subshell, such
    # as $(...), could hide a change of the shell that loads lib.sh. STATE is not exported here,
    # so an "export STATE" in lib.sh changes the state.
    probe = """
state() {
    echo "flags $-"
    set +o
    shopt -p
    umask
    trap -p
    pwd
    ls -A
    compgen -v
    export -p
    declare -p IFS PATH STATE
}
STATE=$3
state >"$2/before"
. "$1"
state >"$2/after"
diff "$2/before" "$2/after"
"""
    work = tmp_path / "work"
    work.mkdir()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()

    done = subprocess.run(  # noqa: S603 - a shell of this computer on a script of this repository
        [  # noqa: S607
            "bash",
            "-c",
            probe,
            "probe",
            str(SCRIPTS / "lib.sh"),
            str(snapshots),
            # status_set makes the folder STATE when it runs; loading lib.sh must not make it.
            str(work / "state"),
        ],
        cwd=work,
        env={key: value for key, value in os.environ.items() if key != "STATE"},
        capture_output=True,
        text=True,
        check=False,
    )

    # The caller sets its own options, such as set -euo pipefail.
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def _prune(folder: Path, names: list[str], daily: int, weekly: int) -> list[str]:
    for name in names:
        (folder / name).mkdir()
    subprocess.run(  # noqa: S603 - the script of this repository
        ["sh", str(SCRIPTS / "prune.sh"), str(folder), str(daily), str(weekly)],  # noqa: S607
        check=True,
    )
    return sorted(path.name for path in folder.iterdir())


def test_the_newest_backups_stay(tmp_path: Path) -> None:
    names = [f"202610{day:02}T090000Z" for day in range(20, 27)]

    kept = _prune(tmp_path, names, daily=3, weekly=0)

    assert kept == ["20261024T090000Z", "20261025T090000Z", "20261026T090000Z"]


def test_the_newest_backup_of_each_calendar_week_stays(tmp_path: Path) -> None:
    # 2026-10-26 is a Monday. Week 0 is Monday 10-26 to Sunday 11-01, week 1 starts on 10-19.
    names = [
        "20261026T090000Z",  # the newest: week 0
        "20261023T090000Z",  # a Friday: the newest of week 1
        "20261012T090000Z",  # a Monday: the newest of week 2
        "20261011T090000Z",  # a Sunday: the newest of week 3
        "20261001T090000Z",  # week 4: older than 4 weeks
        "20260915T090000Z",  # week 6
    ]

    kept = _prune(tmp_path, names, daily=1, weekly=4)

    assert kept == ["20261011T090000Z", "20261012T090000Z", "20261023T090000Z", "20261026T090000Z"]


def test_with_the_defaults_the_oldest_backup_is_29_to_35_days_old(tmp_path: Path) -> None:
    # One backup each day for 70 days, and a prune after each one, as the service does with
    # KEEP_DAILY=7 and KEEP_WEEKLY=6.
    first = date(2026, 9, 1)
    ages: dict[str, int] = {}
    for offset in range(70):
        today = first + timedelta(days=offset)
        _prune(tmp_path, [f"{today:%Y%m%d}T090000Z"], daily=7, weekly=6)
        oldest = min(path.name for path in tmp_path.iterdir())
        ages[f"{today:%a %m-%d}"] = (
            today - date(int(oldest[:4]), int(oldest[4:6]), int(oldest[6:8]))
        ).days

    # Once there is a long enough history: 29 days on a Monday, 35 on a Sunday. A rule of weeks
    # counted back from the newest backup would lose the old ones, and 4 calendar weeks would
    # keep only 15 to 21 days.
    assert {day: age for day, age in list(ages.items())[35:] if not 29 <= age <= 35} == {}
    assert min(list(ages.values())[35:]) == 29
    assert max(list(ages.values())[35:]) == 35


def test_prune_always_keeps_the_newest_backup(tmp_path: Path) -> None:
    kept = _prune(tmp_path, ["20261025T090000Z", "20261026T090000Z"], daily=0, weekly=0)

    assert kept == ["20261026T090000Z"]


def test_prune_touches_no_other_folder_and_no_new_part(tmp_path: Path) -> None:
    names = ["20261026T090000Z", "20261025T090000Z", ".part-20261026T100000Z", "notes"]

    kept = _prune(tmp_path, names, daily=1, weekly=0)

    # A backup that runs now writes into .part-<time>: only a part older than a day goes.
    assert kept == [".part-20261026T100000Z", "20261026T090000Z", "notes"]


def test_prune_removes_a_part_that_is_older_than_a_day(tmp_path: Path) -> None:
    old = tmp_path / ".part-20261020T090000Z"
    old.mkdir()
    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(old, (two_days_ago, two_days_ago))

    kept = _prune(tmp_path, ["20261026T090000Z"], daily=1, weekly=0)

    assert kept == ["20261026T090000Z"]


def test_a_year_boundary_and_a_leap_day_count_right(tmp_path: Path) -> None:
    names = ["20280301T000000Z", "20280229T000000Z", "20280222T000000Z", "20271230T000000Z"]

    kept = _prune(tmp_path, names, daily=1, weekly=10)

    # 2028-03-01 is a Wednesday in the week of Monday 02-28: the leap day shares it. 02-22 is in
    # week 1, and 2027-12-30 in week 9, across the end of the year.
    assert kept == ["20271230T000000Z", "20280222T000000Z", "20280301T000000Z"]


def _drill(
    tmp_path: Path, script: Path = SCRIPTS / "drill.sh", **environment: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """drill.sh with a docker that only writes down each call, and the folder of the backups."""
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    calls = tmp_path / "calls"
    (bin_folder / "docker").write_text(
        f'#!/bin/sh\necho "$*" >>{calls}\necho "$UNSHELVE_BACKUP_DIR" >{tmp_path / "folder"}\n'
    )
    (bin_folder / "docker").chmod(0o755)
    done = subprocess.run(  # noqa: S603 - the script of this repository
        ["bash", str(script)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_folder}:{os.environ['PATH']}", "HOME": str(tmp_path)} | environment,
    )
    return done, calls.read_text().splitlines() if calls.exists() else []


def _backups_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "backups"
    (folder / "db").mkdir(parents=True)
    return folder


def test_a_drill_works_only_in_its_own_project(tmp_path: Path) -> None:
    done, calls = _drill(tmp_path, UNSHELVE_BACKUP_DIR=str(_backups_folder(tmp_path)))

    assert done.returncode == 0, done.stderr
    # Each call names the project of the drill: its `down --volumes` never reaches the real stack.
    assert [call for call in calls if not call.startswith("compose -p unshelve-drill ")] == []
    assert [call.split(" --profile restore ")[1] for call in calls] == [
        "down --volumes",
        "up -d --wait db",
        "run --rm restore latest",
        "down --volumes",
    ]


def test_a_drill_refuses_the_project_of_the_real_stack(tmp_path: Path) -> None:
    done, calls = _drill(
        tmp_path,
        UNSHELVE_DRILL_PROJECT="unshelve",
        UNSHELVE_BACKUP_DIR=str(_backups_folder(tmp_path)),
    )

    assert done.returncode == 2
    assert 'the project of a drill must end with -drill, not "unshelve"' in done.stderr
    assert calls == []


def test_a_drill_reads_the_folder_of_the_backups_from_env_as_the_stack_does(tmp_path: Path) -> None:
    # A clone of a server: .env names a folder outside the clone (docs/quickstart.md, B).
    clone = tmp_path / "clone"
    shutil.copytree(SCRIPTS, clone / "deploy" / "backup")
    folder = _backups_folder(tmp_path)
    (clone / ".env").write_text(f"UNSHELVE_BACKUP_DIR=/nowhere\nUNSHELVE_BACKUP_DIR={folder}\n")

    done, _ = _drill(tmp_path, clone / "deploy" / "backup" / "drill.sh")

    assert done.returncode == 0, done.stderr
    # The last line of a name counts, as in Compose.
    assert (tmp_path / "folder").read_text().strip() == str(folder)


def test_a_drill_without_backups_says_where_it_looked(tmp_path: Path) -> None:
    done, calls = _drill(tmp_path, UNSHELVE_BACKUP_DIR=str(tmp_path / "empty"))

    assert done.returncode == 1
    assert f"no backups in {tmp_path / 'empty'}" in done.stderr
    assert calls == []
