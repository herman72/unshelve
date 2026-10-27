"""The scripts of the backup, without a database (docs/learn/50): their syntax, and the rule of
prune.sh about which backups stay. prune.sh is POSIX sh and awk, so it runs here as it runs in the
service `backup`."""

import os
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.deploy.support import REPO

SCRIPTS = REPO / "deploy" / "backup"


@pytest.mark.parametrize(("shell", "script"), [("bash", "backup.sh"), ("sh", "prune.sh")])
def test_each_script_has_a_valid_syntax(shell: str, script: str) -> None:
    done = subprocess.run(  # noqa: S603 - a shell of this computer on a script of this repository
        [shell, "-n", str(SCRIPTS / script)], capture_output=True, text=True, check=False
    )

    assert (done.returncode, done.stderr) == (0, "")


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
