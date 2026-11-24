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
from collections.abc import Mapping
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


# The items of a backup in its info.txt. None: the backup has no info.txt.
Items = Mapping[str, int | str | None]


def _make(folder: Path, names: list[str], items: Items | None = None) -> None:
    """Folders with these names. Each backup gets an info.txt, as snapshot.sql writes it, with 1
    item, or with the items that `items` gives for its name."""
    for name in names:
        (folder / name).mkdir()
        count = (items or {}).get(name, 1)
        if re.fullmatch(r"\d{8}T\d{6}Z", name) and count is not None:
            (folder / name / "info.txt").write_text(
                f"server\t17.11\nrevision\t0006\nitems\t{count}\nassets\t0\n"
            )


def _run_prune(folder: Path, daily: int, weekly: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the script of this repository
        ["sh", str(SCRIPTS / "prune.sh"), str(folder), str(daily), str(weekly)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )


def _names(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


def _prune(
    folder: Path, names: list[str], daily: int, weekly: int, items: Items | None = None
) -> list[str]:
    _make(folder, names, items)
    done = _run_prune(folder, daily, weekly)
    assert (done.returncode, done.stderr) == (0, "")
    return _names(folder)


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


# --- The pin: the newest backup with items stays -----------------------------------------

# Ten days of backups, the oldest first: 2026-10-17 to 2026-10-26.
TEN_DAYS = [f"202610{day:02}T090000Z" for day in range(17, 27)]


def test_the_newest_backup_with_items_stays_when_each_newer_backup_is_empty(
    tmp_path: Path,
) -> None:
    # The database became empty after the backup of 10-19, and nobody saw it.
    items = {"20261019T090000Z": 2} | dict.fromkeys(TEN_DAYS[3:], 0)

    kept = _prune(tmp_path, TEN_DAYS, daily=3, weekly=0, items=items)

    # By its age, 10-19 would go. Without it, each backup that stays would be empty.
    assert kept == ["20261019T090000Z", "20261024T090000Z", "20261025T090000Z", "20261026T090000Z"]


def test_with_the_defaults_the_pin_stays_inside_the_weekly_window(tmp_path: Path) -> None:
    # KEEP_DAILY=7 and KEEP_WEEKLY=6. 2026-11-16 is a Monday: week 0 is 11-16 to 11-22, week 1 is
    # 11-09 to 11-15, and week 2 is 11-02 to 11-08. The database became empty after 11-10.
    names = [f"202611{day:02}T090000Z" for day in range(8, 19)]
    items = {"20261108T090000Z": 5, "20261109T090000Z": 5, "20261110T090000Z": 6}
    items |= dict.fromkeys(names[3:], 0)

    kept = _prune(tmp_path, names, daily=7, weekly=6, items=items)

    # 11-12 to 11-18 stay by their age, and 11-08 as the newest backup of week 2. The newest
    # backup of week 1 is 11-15, so only the pin keeps 11-10.
    assert kept == ["20261108T090000Z", "20261110T090000Z", *names[4:]]


def test_the_old_pin_goes_when_a_newer_backup_has_items_again(tmp_path: Path) -> None:
    items = {"20261019T090000Z": 2} | dict.fromkeys(TEN_DAYS[3:], 0)
    _prune(tmp_path, TEN_DAYS, daily=3, weekly=0, items=items)

    kept = _prune(tmp_path, ["20261027T090000Z"], daily=3, weekly=0, items={"20261027T090000Z": 4})

    assert kept == ["20261025T090000Z", "20261026T090000Z", "20261027T090000Z"]


def test_without_a_backup_with_items_the_prune_keeps_by_age_only(tmp_path: Path) -> None:
    kept = _prune(tmp_path, TEN_DAYS, daily=3, weekly=0, items=dict.fromkeys(TEN_DAYS, 0))

    assert kept == ["20261024T090000Z", "20261025T090000Z", "20261026T090000Z"]


def test_of_two_backups_with_items_on_one_day_only_the_newer_one_is_the_pin(
    tmp_path: Path,
) -> None:
    # The pin is one backup, not a day: the backup of 09:00 goes by its age.
    names = ["20261019T090000Z", "20261019T210000Z"] + [
        f"202610{day}T090000Z" for day in range(20, 24)
    ]
    items = {"20261019T090000Z": 2, "20261019T210000Z": 2} | dict.fromkeys(names[2:], 0)

    kept = _prune(tmp_path, names, daily=3, weekly=0, items=items)

    assert kept == ["20261019T210000Z", "20261021T090000Z", "20261022T090000Z", "20261023T090000Z"]


def test_a_newest_backup_with_one_item_is_the_pin_and_no_older_backup_stays(
    tmp_path: Path,
) -> None:
    # 1 is the smallest count of a pin. Each older backup has more items, so a walk that skipped
    # the newest backup, or a pin that needed more than 1 item, would keep 10-25.
    items = dict.fromkeys(TEN_DAYS, 5) | {"20261026T090000Z": 1}

    kept = _prune(tmp_path, TEN_DAYS, daily=1, weekly=0, items=items)

    assert kept == ["20261026T090000Z"]


def test_a_folder_whose_name_only_contains_a_backup_name_is_not_a_backup(tmp_path: Path) -> None:
    # A person can leave a copy of a backup, or a renamed one, in the folder. backup.sh and
    # restore.sh do not see such a folder as a backup, so it cannot be the pin, and the prune
    # does not remove it. Each of the two has 9 items: as a backup, the copy would become the pin,
    # and the folder with a prefix would come first in the walk.
    items = {"20261019T090000Z": 2} | dict.fromkeys(TEN_DAYS[3:], 0)
    _make(tmp_path, TEN_DAYS, items)
    for other in ("20261024T090000Z.copy", "old-20261025T090000Z"):
        (tmp_path / other).mkdir()
        (tmp_path / other / "info.txt").write_text("items\t9\n")

    done = _run_prune(tmp_path, daily=3, weekly=0)

    assert (done.returncode, done.stderr) == (0, "")
    assert _names(tmp_path) == [
        "20261019T090000Z",
        "20261024T090000Z",
        "20261024T090000Z.copy",
        "20261025T090000Z",
        "20261026T090000Z",
        "old-20261025T090000Z",
    ]


@pytest.mark.parametrize(
    ("info", "pin"),
    [
        ("items\t007\n", "20261025T090000Z"),
        ("items\t00\n", "20261024T090000Z"),
        ("items_total\t5\nitems\t0\n", "20261024T090000Z"),
        ("items\t0\nitems\t5\n", "20261024T090000Z"),
        # Each digit of the count matters, not only the last one, and 9 is a digit too.
        ("items\t10\n", "20261025T090000Z"),
        ("items\t99\n", "20261025T090000Z"),
    ],
    ids=[
        "a leading zero",
        "only zeros",
        "another name that starts with items",
        "two lines of items",
        "a count that ends in 0",
        "a count of only nines",
    ],
)
def test_the_count_is_the_value_of_the_first_line_named_items(
    tmp_path: Path, info: str, pin: str
) -> None:
    # The newest backup is empty, so the walk reads 10-25. 10-24 has 1 item: it is the pin when
    # 10-25 has no items.
    _make(tmp_path, TEN_DAYS, items={"20261026T090000Z": 0})
    (tmp_path / "20261025T090000Z" / "info.txt").write_text(info)

    done = _run_prune(tmp_path, daily=1, weekly=0)

    assert (done.returncode, done.stderr) == (0, "")
    assert _names(tmp_path) == sorted([pin, "20261026T090000Z"])


@pytest.mark.parametrize(
    ("info", "reason"),
    [
        (None, "no file"),
        # The file is there, so the line is not "no file": the file is damaged.
        ("", "no count of items in"),
        ("server\t17.11\nassets\t0\n", "no count of items in"),
        ("items\ttwo\n", "no count of items in"),
        ("items\t\n", "no count of items in"),
        # The name and the value are separated by a TAB, as snapshot.sql writes them. The value
        # is the rest of the line after that TAB.
        ("items 5\n", "no count of items in"),
        ("items\t42\r\n", "no count of items in"),
        ("items\t42 \n", "no count of items in"),
        ("items\t42\t\n", "no count of items in"),
        ("items\t5\t7\n", "no count of items in"),
        ("items\t\t5\n", "no count of items in"),
        # A character that is not a digit, in front of the digits.
        ("items\t 5\n", "no count of items in"),
        ("items\t-5\n", "no count of items in"),
        ("items\t1.5\n", "no count of items in"),
        # The first line named items counts, also when a later one has a good value.
        ("items\ttwo\nitems\t5\n", "no count of items in"),
        ("items\t5\t7\nitems\t3\n", "no count of items in"),
        ("items\t\nitems\t5\n", "no count of items in"),
        # The name is items in small letters, as snapshot.sql writes it.
        ("Items\t5\n", "no count of items in"),
    ],
    ids=[
        "no info.txt",
        "an empty info.txt",
        "no line of items",
        "a word",
        "no value",
        "a space, not a tab",
        "a carriage return",
        "a trailing space",
        "a trailing tab",
        "a second tab and a second number",
        "two tabs in front of the value",
        "a leading space",
        "a minus sign",
        "a decimal point",
        "a bad first line and a good second one",
        "a first line with a second tab and a good second one",
        "an empty first value and a good second one",
        "a name with a capital letter",
    ],
)
def test_a_count_that_the_walk_cannot_get_stops_the_prune_before_any_change(
    tmp_path: Path, info: str | None, reason: str
) -> None:
    # The newest backup is empty, so the walk reads the next one, and cannot use its count.
    _make(tmp_path, TEN_DAYS, items={"20261026T090000Z": 0, "20261025T090000Z": None})
    file = tmp_path / "20261025T090000Z" / "info.txt"
    if info is not None:
        file.write_text(info)
    old_part = tmp_path / ".part-20261020T090000Z"
    old_part.mkdir()
    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(old_part, (two_days_ago, two_days_ago))

    done = _run_prune(tmp_path, daily=1, weekly=0)

    # A count that the prune cannot get is not 0 items: that backup can be the newest backup with
    # items.
    assert done.returncode == 1
    assert _names(tmp_path) == sorted([*TEN_DAYS, old_part.name])
    [line] = done.stderr.splitlines()
    assert line.startswith("prune.sh removed nothing: ")
    assert f"{reason} {file}" in line


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a file without a right to read")
def test_an_info_txt_that_cannot_be_read_stops_the_prune(tmp_path: Path) -> None:
    _make(tmp_path, TEN_DAYS, items={"20261026T090000Z": 0})
    file = tmp_path / "20261025T090000Z" / "info.txt"
    file.chmod(0)

    done = _run_prune(tmp_path, daily=1, weekly=0)

    assert done.returncode == 1
    assert _names(tmp_path) == TEN_DAYS
    assert done.stderr == f"prune.sh removed nothing: cannot read {file}\n"


def test_a_count_older_than_the_pin_is_never_read(tmp_path: Path) -> None:
    # A sync tool can take the bytes of an old file off the disk. Such a file must not stop the
    # prune: the walk ends at the pin, 10-25.
    items = {
        "20261026T090000Z": 0,
        "20261025T090000Z": 3,
        "20261024T090000Z": None,
        "20261022T090000Z": "two",
    }
    _make(tmp_path, TEN_DAYS, items=items)
    unreadable = tmp_path / "20261020T090000Z" / "info.txt"
    unreadable.chmod(0)

    done = _run_prune(tmp_path, daily=1, weekly=0)

    assert (done.returncode, done.stderr) == (0, "")
    # The backups without a count go by their age, as each other old backup does.
    assert _names(tmp_path) == ["20261025T090000Z", "20261026T090000Z"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can remove a folder without rights")
def test_an_rm_that_fails_stops_the_prune_and_the_older_backups_stay(tmp_path: Path) -> None:
    # The prune removes the old backups from the newest to the oldest. 10-18 holds a folder
    # without rights, so rm cannot remove it: the prune stops with the error of rm and exit 1,
    # and backup.sh then fails the run. A prune that went on would remove 10-17 too, and exit 0.
    _make(tmp_path, TEN_DAYS)
    locked = tmp_path / "20261018T090000Z" / "locked"
    locked.mkdir()
    (locked / "file").touch()
    locked.chmod(0)
    try:
        done = _run_prune(tmp_path, daily=1, weekly=0)
    finally:
        locked.chmod(0o700)

    assert done.returncode == 1
    assert "20261018T090000Z" in done.stderr
    # The error comes from rm, after the walk: other old backups are already gone.
    assert not done.stderr.startswith("prune.sh removed nothing")
    assert _names(tmp_path) == ["20261017T090000Z", "20261018T090000Z", "20261026T090000Z"]


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
