"""The backup of the database and of the files (deploy/backup/backup.sh, docs/learn/50).

Each test runs the real script in the image of the database, as the service `backup` does
(tests/deploy/backup_support.py).
"""

import asyncio
import functools
import hashlib
import io
import os
import re
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.deploy.backup_support import (
    BACKUP_MARKS,
    FILES_OF_A_BACKUP,
    LOST_KEY,
    NOW,
    STORED,
    STORED_KEY,
    Place,
    Sessions,
    blob,
    complete_backups,
    in_image,
    needs_real_owners,
    old_backup,
    parts,
    script,
    seed,
    stamp,
    status_of,
)

pytestmark = BACKUP_MARKS


# --- A backup ----------------------------------------------------------------------------


async def test_a_backup_holds_the_dump_the_facts_and_each_file(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)

    done = script(place, "once")

    assert done.returncode == 0, done.stderr.decode()
    [backup] = complete_backups(place)
    assert sorted(path.name for path in backup.iterdir()) == FILES_OF_A_BACKUP
    # The checksums cover each other file of the backup, and they hold.
    sums: dict[str, str] = {}
    for line in (backup / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        sums[name] = digest
    assert sorted(sums) == FILES_OF_A_BACKUP[1:]
    for name, digest in sums.items():
        assert hashlib.sha256((backup / name).read_bytes()).hexdigest() == digest, name
    assert (backup / "blob-keys.txt").read_text().split() == sorted([STORED_KEY, LOST_KEY])
    # The stored file is in the mirror, and the lost one is noted, not invented.
    assert blob(place.backups / "blobs", STORED_KEY).read_bytes() == STORED
    assert (backup / "missing-blobs.txt").read_text().split() == [LOST_KEY]
    manifest = (backup / "manifest.tsv").read_text().splitlines()
    assert any(line.startswith("table\tpublic.items\t2\t") for line in manifest)
    assert "role\tunshelve_publisher\tf" in manifest
    assert status_of(place)["last_name"] == backup.name
    # The folder of the backups keeps itself out of git, also inside a clone.
    assert (place.backups / ".gitignore").read_text() == "*\n"
    assert parts(place) == []


async def test_the_facts_of_the_backup_are_the_facts_of_the_database_until_a_row_changes(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    script(place, "once")
    [backup] = complete_backups(place)
    saved = (backup / "manifest.tsv").read_text().splitlines()

    def manifest_now(**environment: str) -> list[str]:
        query = script(place, "_manifest", environment=environment)
        assert query.returncode == 0, query.stderr.decode()
        return query.stdout.decode().splitlines()

    # The same data gives the same facts: in another time zone of the client, and after an
    # update that moves a row inside its table without a change.
    assert manifest_now(PGTZ="Asia/Tehran") == saved
    async with committed_db() as db:
        await db.execute(text("UPDATE items SET title = title WHERE title = 'A'"))
        await db.commit()
    assert manifest_now() == saved

    async with committed_db() as db:
        await db.execute(text("UPDATE items SET title = 'C' WHERE title = 'A'"))
        await db.commit()
    changed = set(manifest_now()) ^ set(saved)

    assert {line.split("\t")[1] for line in changed} == {"public.items"}


async def test_the_dump_and_the_facts_come_from_one_snapshot(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    # Another session deletes a row and commits after the snapshot, just before pg_dump starts.
    late_write = """#!/bin/sh
psql -X --quiet -v ON_ERROR_STOP=1 -c "DELETE FROM items WHERE title = 'B'" >/dev/null || exit
exec /usr/bin/pg_dump "$@"
"""

    done = script(place, "once", fakes={"pg_dump": late_write})

    assert done.returncode == 0, done.stderr.decode()
    [backup] = complete_backups(place)
    manifest = (backup / "manifest.tsv").read_text().splitlines()
    assert any(line.startswith("table\tpublic.items\t2\t") for line in manifest)
    rows = in_image(
        place,
        "pg_restore",
        "--data-only",
        "--table=items",
        "--file=-",
        f"/backups/db/{backup.name}/unshelve.dump",
    )
    assert rows.returncode == 0, rows.stderr.decode()
    # The rows of the COPY block: between its first line and the line "\.".
    data = rows.stdout.decode().split("COPY public.items ", 1)[1].split("\n\\.\n", 1)[0]
    assert len(data.splitlines()) - 1 == 2


async def test_a_table_that_a_migration_rewrites_is_whole_in_the_backup(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    loop = asyncio.get_running_loop()
    async with committed_db() as db:
        # A migration rewrites the table captures and holds its lock for 4 seconds. The backup
        # starts during those seconds: its lock waits for the commit.
        await db.execute(text("ALTER TABLE captures ALTER COLUMN raw TYPE json USING raw::json"))
        backup = loop.run_in_executor(None, functools.partial(script, place, "once"))
        await asyncio.sleep(4)
        await db.commit()
    done = await backup

    assert done.returncode == 0, done.stderr.decode()
    [backup_folder] = complete_backups(place)
    # A snapshot from before the commit would see the rewritten table as empty.
    manifest = (backup_folder / "manifest.tsv").read_text().splitlines()
    assert any(line.startswith("table\tpublic.captures\t1\t") for line in manifest)


async def test_a_pg_dump_that_fails_stops_the_backup(committed_db: Sessions, place: Place) -> None:
    await seed(committed_db, place)
    # A dump that is written, and a pg_dump that reports a failure all the same.
    fails = """#!/bin/sh
/usr/bin/pg_dump "$@"
exit 1
"""

    done = script(place, "once", fakes={"pg_dump": fails})

    assert done.returncode == 1
    assert b"pg_dump did not finish" in done.stderr
    assert complete_backups(place) == []
    assert parts(place) == []


async def test_a_backup_keeps_the_status_that_it_does_not_write(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    (place.state / "status").write_text("started=1761469200\nlast_error=1761469300\n")

    done = script(place, "once")

    assert done.returncode == 0, done.stderr.decode()
    status = status_of(place)
    assert (status["started"], status["last_error"]) == ("1761469200", "1761469300")
    assert int(status["last_ok"]) > 1761469300


async def test_a_second_backup_copies_no_file_again(committed_db: Sessions, place: Place) -> None:
    await seed(committed_db, place)

    first = script(place, "once")
    second = script(place, "once")

    assert (first.returncode, second.returncode) == (0, 0)
    assert len(complete_backups(place)) == 2
    assert status_of(place)["last_blobs_copied"] == "0"


async def test_a_mirror_copy_with_the_wrong_size_is_copied_again(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    # What a crash between the copy and the disk can leave: the name, with too few bytes.
    cut = blob(place.backups / "blobs", STORED_KEY)
    cut.parent.mkdir(parents=True)
    cut.write_bytes(b"")

    done = script(place, "once")

    assert done.returncode == 0, done.stderr.decode()
    assert cut.read_bytes() == STORED


async def test_a_backup_in_a_taken_second_takes_the_next_free_one(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    taken = old_backup(place, NOW)
    # The clock gives the name of the taken backup for the first two names, then the real time.
    clock = f"""#!/bin/sh
if [ "$*" = "-u +%Y%m%dT%H%M%SZ" ]; then
    calls=$(cat /tmp/names 2>/dev/null || echo 0)
    echo $((calls + 1)) >/tmp/names
    if [ "$calls" -lt 2 ]; then echo {taken.name}; exit 0; fi
fi
exec /usr/bin/date "$@"
"""

    done = script(place, "once", fakes={"date": clock})

    assert done.returncode == 0, done.stderr.decode()
    [new] = [backup for backup in complete_backups(place) if backup != taken]
    assert new.name > taken.name
    # The taken folder got no file of the new backup.
    assert sorted(path.name for path in taken.iterdir()) == ["SHA256SUMS", "unshelve.dump"]


async def test_a_backup_removes_the_backups_that_the_rule_does_not_keep(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for days in (1, 2, 3, 30):
        old_backup(place, today - timedelta(days=days))

    done = script(
        place,
        "once",
        environment={"UNSHELVE_BACKUP_KEEP_DAILY": "2", "UNSHELVE_BACKUP_KEEP_WEEKLY": "0"},
    )

    assert done.returncode == 0, done.stderr.decode()
    # The new backup and the newest old one stay.
    [kept_old, _new] = complete_backups(place)
    assert kept_old.name == stamp(today - timedelta(days=1))


# --- A failure ---------------------------------------------------------------------------


async def test_a_file_with_other_bytes_than_its_name_stops_the_backup(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    blob(place.blobs, STORED_KEY).write_bytes(b"other bytes")

    done = script(place, "once")
    health = script(place, "health")

    assert done.returncode == 1
    assert b"does not have the bytes of its name" in done.stderr
    assert complete_backups(place) == []
    assert not blob(place.backups / "blobs", STORED_KEY).exists()
    assert health.returncode == 1
    assert health.stdout.startswith(b"the last backup failed")


async def test_a_failed_backup_leaves_no_part_behind(committed_db: Sessions, place: Place) -> None:
    await seed(committed_db, place)
    blob(place.blobs, STORED_KEY).write_bytes(b"other bytes")
    # What an earlier run left when a stop cut it short.
    (place.backups / "db" / ".part-20261001T000000Z").mkdir(parents=True)

    first = script(place, "once")
    second = script(place, "once")

    assert (first.returncode, second.returncode) == (1, 1)
    # A lasting failure must not fill the disk with one dump for each try.
    assert parts(place) == []


async def test_a_success_after_a_failure_makes_the_backup_healthy_again(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    blob(place.blobs, STORED_KEY).write_bytes(b"other bytes")
    failed = script(place, "once")
    assert failed.returncode == 1
    assert script(place, "health").returncode == 1
    blob(place.blobs, STORED_KEY).write_bytes(STORED)

    done = script(place, "once")
    health = script(place, "health")

    assert done.returncode == 0, done.stderr.decode()
    assert (health.returncode, health.stdout) == (0, b"ok\n")


async def test_a_backup_stops_when_the_disk_has_too_little_room(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)

    # 50 MB of room, and a backup asks for at least 100 MB.
    done = script(
        place,
        "once",
        backups="type=tmpfs,destination=/backups,tmpfs-size=52428800,tmpfs-mode=1777",
    )

    assert done.returncode == 1
    assert b"KB are free in the backup folder" in done.stderr
    assert status_of(place)["last_error_text"].startswith("only ")


async def test_a_disk_that_became_read_only_stops_the_backup_with_the_reason(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    assert script(place, "once").returncode == 0
    # The folders of the backups exist; then the disk refuses each write, as after an I/O error.
    read_only = f"type=bind,source={place.backups},destination=/backups,readonly"

    done = script(place, "once", backups=read_only, timeout=60)

    assert done.returncode == 1
    assert status_of(place)["last_error_text"].endswith("Read-only file system")


async def test_the_room_for_a_backup_counts_the_files_that_it_must_copy(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    big = os.urandom(20 * 1024 * 1024)
    path = blob(place.blobs, hashlib.sha256(big).hexdigest())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(big)

    # 110 MB of room: enough for the 100 MB of reserve, not for the reserve and 20 MB of files.
    done = script(
        place,
        "once",
        backups="type=tmpfs,destination=/backups,tmpfs-size=115343360,tmpfs-mode=1777",
    )

    assert done.returncode == 1
    assert b"a backup needs" in done.stderr


async def test_the_room_for_a_backup_counts_two_dumps_of_the_last_size(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    # 140 MB of room, and an earlier backup with a dump of 30 MB: 110 MB stay free. That is
    # enough for the reserve of 100 MB, not for the reserve and two dumps of 30 MB.
    earlier = """set -e
mkdir -p /backups/db/20261001T000000Z
head -c 31457280 /dev/zero >/backups/db/20261001T000000Z/unshelve.dump
exec bash /deploy/backup/backup.sh once
"""

    done = in_image(
        place,
        "bash",
        "-c",
        earlier,
        backups="type=tmpfs,destination=/backups,tmpfs-size=146800640,tmpfs-mode=1777",
    )

    assert done.returncode == 1
    assert b"a backup needs" in done.stderr


@needs_real_owners
async def test_the_files_of_a_backup_belong_to_the_owner_of_its_folder(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)

    # As the service runs: as root. On Linux, the owner could not read the files of root.
    done = script(place, "once", as_root=True)

    assert done.returncode == 0, done.stderr.decode()
    assert {path.stat().st_uid for path in place.backups.rglob("*")} == {os.getuid()}


@needs_real_owners
async def test_a_failed_backup_also_gives_its_files_to_the_owner(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    blob(place.blobs, STORED_KEY).write_bytes(b"other bytes")

    done = script(place, "once", as_root=True)

    # The owner must be able to look for the cause without sudo.
    assert done.returncode == 1
    assert {path.stat().st_uid for path in place.backups.rglob("*")} == {os.getuid()}


async def test_a_backup_that_you_start_waits_for_the_one_that_runs(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    slow = """#!/bin/sh
sleep 3
exec /usr/bin/pg_dump "$@"
"""
    # A scheduled backup runs. A round of the loop comes during it, and then a backup by hand.
    both = """
bash /deploy/backup/backup.sh _once & first=$!
sleep 1.5
before=$(date +%s%N)
bash /deploy/backup/backup.sh _tick
echo "the round took $((($(date +%s%N) - before) / 1000000)) ms"
bash /deploy/backup/backup.sh once
wait "$first"
echo "the first backup ended with $?"
"""

    # The lock lies on a folder of the container, as on the volume of the service: on a shared
    # folder of Docker Desktop, a lock taken by the call that made the file keeps nobody out.
    done = in_image(
        place,
        "bash",
        "-c",
        both,
        fakes={"pg_dump": slow},
        environment={"UNSHELVE_BACKUP_STATE": "/tmp/backup-status"},  # noqa: S108
    )

    assert done.returncode == 0, done.stderr.decode()
    # The round saw the lock and did not wait for the 3 seconds of the first backup; the backup
    # by hand waited, and then ran.
    took = re.search(rb"the round took (\d+) ms", done.stdout)
    assert took is not None, done.stdout
    assert int(took.group(1)) < 1000
    assert b"the first backup ended with 0" in done.stdout
    assert len(complete_backups(place)) == 2
    assert parts(place) == []


def _stop(name: str) -> str:
    """docker stop, as `docker compose stop` does it: the exit code of the container."""
    subprocess.run(["docker", "stop", "-t", "60", name], check=True, capture_output=True)  # noqa: S603, S607
    done = subprocess.run(  # noqa: S603
        ["docker", "inspect", "--format", "{{.State.ExitCode}}", name],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _remove(name: str) -> None:
    subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)  # noqa: S603, S607


async def test_a_stop_during_a_backup_removes_its_part_and_ends_the_service(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    slow = """#!/bin/sh
sleep 5
exec /usr/bin/pg_dump "$@"
"""
    name = f"unshelve-backup-test-{place.database}"
    started = in_image(
        place, "bash", "/deploy/backup/backup.sh", "loop", fakes={"pg_dump": slow}, detach_as=name
    )
    assert started.returncode == 0, started.stderr.decode()
    try:
        for _ in range(300):
            if parts(place):
                break
            await asyncio.sleep(0.1)
        assert parts(place), "the first backup of the loop did not start"

        code = _stop(name)
    finally:
        _remove(name)

    assert code == "0"
    assert parts(place) == []
    assert complete_backups(place) == []


# --- The schedule and the health ---------------------------------------------------------


def test_a_backup_is_due_when_the_newest_is_older_than_a_day(place: Place) -> None:
    old_backup(place, datetime.now(UTC) - timedelta(hours=25))

    assert script(place, "_due").returncode == 0

    old_backup(place, datetime.now(UTC) - timedelta(hours=1))

    assert script(place, "_due").returncode == 1


async def test_a_round_of_the_loop_makes_no_backup_when_none_is_due(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    old_backup(place, datetime.now(UTC) - timedelta(hours=1))

    done = script(place, "_tick")

    assert done.returncode == 0, done.stderr.decode()
    assert len(complete_backups(place)) == 1


async def test_a_round_of_the_loop_waits_after_a_recent_failure(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    ten_minutes_ago = int((datetime.now(UTC) - timedelta(minutes=10)).timestamp())
    (place.state / "status").write_text(f"last_error={ten_minutes_ago}\n")

    script(place, "_tick", environment={"UNSHELVE_BACKUP_RETRY_MINUTES": "60"})

    assert complete_backups(place) == []

    script(place, "_tick", environment={"UNSHELVE_BACKUP_RETRY_MINUTES": "5"})

    assert len(complete_backups(place)) == 1


def test_the_backup_is_healthy_before_its_first_run(place: Place) -> None:
    done = script(place, "health")

    assert (done.returncode, done.stdout) == (0, b"ok\n")


@pytest.mark.parametrize(("newest_hours_ago", "healthy"), [(None, False), (72, False), (1, True)])
def test_the_backup_is_unhealthy_after_two_days_without_a_backup(
    place: Place, newest_hours_ago: int | None, healthy: bool
) -> None:
    started = int((datetime.now(UTC) - timedelta(days=5)).timestamp())
    (place.state / "status").write_text(f"started={started}\n")
    if newest_hours_ago is not None:
        old_backup(place, datetime.now(UTC) - timedelta(hours=newest_hours_ago))

    done = script(place, "health")

    expected = (0, b"ok\n") if healthy else (1, b"no backup in 48 hours\n")
    assert (done.returncode, done.stdout) == expected


@pytest.mark.parametrize(("started_minutes_ago", "healthy"), [(10, True), (120, False)])
def test_a_restart_gives_the_backup_one_hour_and_not_two_days(
    place: Place, started_minutes_ago: int, healthy: bool
) -> None:
    started = int((datetime.now(UTC) - timedelta(minutes=started_minutes_ago)).timestamp())
    (place.state / "status").write_text(f"started={started}\n")
    old_backup(place, datetime.now(UTC) - timedelta(days=30))

    done = script(place, "health")

    expected = (0, b"ok\n") if healthy else (1, b"no backup in 48 hours\n")
    assert (done.returncode, done.stdout) == expected


def test_a_backup_named_in_the_future_shows_and_does_not_stop_the_schedule(place: Place) -> None:
    # A clock that was 3 days ahead named this backup.
    future = old_backup(place, datetime.now(UTC) + timedelta(days=3))

    due = script(place, "_due")
    health = script(place, "health")

    assert due.returncode == 0
    assert health.returncode == 1
    assert f"the backup {future.name} has a name in the future".encode() in health.stdout


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("EVERY_HOURS", "1.5", "must be a whole number"),
        # bash would read 010 as the octal number 8, and 08 as an error.
        ("EVERY_HOURS", "08", "must be a whole number without a leading zero"),
        ("RETRY_MINUTES", "010", "must be a whole number without a leading zero"),
        # No backup would stay, also not the new one.
        ("KEEP_DAILY", "0", "must be 1 or more"),
        ("EVERY_HOURS", "0", "must be 1 or more"),
    ],
)
def test_a_wrong_setting_stops_the_script_with_its_name(
    place: Place, setting: str, value: str, message: str
) -> None:
    done = script(place, "health", environment={f"UNSHELVE_BACKUP_{setting}": value})

    assert done.returncode == 2
    assert f"UNSHELVE_BACKUP_{setting} {message}".encode() in done.stderr


# --- The export --------------------------------------------------------------------------


async def test_the_export_holds_the_backup_and_the_files_that_it_needs(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    script(place, "once")
    [backup] = complete_backups(place)

    done = script(place, "export", "latest")

    assert done.returncode == 0, done.stderr.decode()
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
        names = set(archive.getnames())
    assert f"db/{backup.name}/unshelve.dump" in names
    assert f"blobs/{STORED_KEY[:2]}/{STORED_KEY[2:4]}/{STORED_KEY}" in names
    assert not [name for name in names if LOST_KEY in name]


async def test_the_export_of_a_damaged_backup_gives_no_tar(
    committed_db: Sessions, place: Place
) -> None:
    await seed(committed_db, place)
    script(place, "once")
    [backup] = complete_backups(place)
    # One changed byte in a file of the backup, as a disk that rots can give.
    facts = (backup / "manifest.tsv").read_bytes()
    (backup / "manifest.tsv").write_bytes(facts[:-2] + bytes([facts[-2] ^ 1]) + facts[-1:])

    done = script(place, "export", "latest")

    assert done.returncode != 0
    assert done.stdout == b""
