"""The backup of the database and of the files (deploy/backup/backup.sh, docs/learn/50).

Each test runs the real script in the image of the database, as the service `backup` does: with
the pg_dump, psql and bash of that image, and at the paths of the service. The script works on a
database of the test and on folders of the test, so a test never touches the instance on this
computer.
"""

import asyncio
import functools
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import TEST_SERVER_URL
from tests.deploy.support import POSTGRES_IMAGE, REPO, docker_available
from unshelve.auth import store as auth
from unshelve.contracts.enums import CaptureSource
from unshelve.db.models import Asset, Capture, Item

Sessions = async_sessionmaker[AsyncSession]

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not docker_available("image", "inspect", POSTGRES_IMAGE), reason="needs docker"
    ),
]
# Docker Desktop shows each file of a shared folder as the file of the user, whoever wrote it.
# Only Linux shows who owns the files, so a pass on macOS would prove nothing.
needs_real_owners = pytest.mark.skipif(
    sys.platform == "darwin", reason="Docker Desktop hides the owner of a file"
)

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
STORED = b"a photo of a whiteboard"
STORED_KEY = hashlib.sha256(STORED).hexdigest()
# A file that a capture names, but that the store lost before the backup.
LOST_KEY = hashlib.sha256(b"a file that the store lost").hexdigest()
# The PATH of the image; a test puts its own pg_dump in front of it.
IMAGE_PATH = (
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/postgresql/17/bin"
)
FILES_OF_A_BACKUP = [
    "SHA256SUMS",
    "blob-keys.txt",
    "info.txt",
    "manifest.tsv",
    "missing-blobs.txt",
    "unshelve.dump",
]


@dataclass(frozen=True, slots=True)
class Place:
    database: str
    backups: Path
    blobs: Path
    state: Path


def _blob(root: Path, key: str) -> Path:
    return root / key[:2] / key[2:4] / key


def _in_image(
    place: Place,
    *command: str,
    environment: dict[str, str] | None = None,
    as_root: bool = False,
    backups: str | None = None,
    fakes: dict[str, str] | None = None,
    detach_as: str | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[bytes]:
    """A command in the image of the database, at the paths of the service `backup`.

    `backups` replaces the bind mount of /backups with another `--mount`, for example a tmpfs.
    `fakes` maps a command to the text of a shell script that comes first in PATH under its
    name: a test can make pg_dump fail or slow, or give `date` a taken second.
    `detach_as` starts the container in the background with this name, and does not remove it.
    """
    server = make_url(TEST_SERVER_URL)
    # On Linux the test server listens on 127.0.0.1 only, so the container shares the network of
    # this computer. Docker Desktop reaches this computer as host.docker.internal.
    on_linux = sys.platform == "linux"
    settings = {
        "PGHOST": "127.0.0.1" if on_linux else "host.docker.internal",
        "PGPORT": str(server.port),
        "PGUSER": str(server.username),
        "PGDATABASE": place.database,
        "UNSHELVE_DB_PASSWORD": str(server.password),
    } | (environment or {})
    wrapper: list[str] = []
    if fakes:
        folder = place.state.parent / "test-bin"
        folder.mkdir(exist_ok=True)
        for name, script in fakes.items():
            (folder / name).write_text(script)
            (folder / name).chmod(0o755)
        wrapper = ["-v", f"{folder}:/test-bin:ro"]
        settings["PATH"] = f"/test-bin:{IMAGE_PATH}"
    docker = [
        "docker", "run",
        *(["--detach", "--name", detach_as] if detach_as else ["--rm"]),
        *(["--network=host"] if on_linux else []),
        *([] if as_root else ["--user", f"{os.getuid()}:{os.getgid()}"]),
        *(item for name, value in settings.items() for item in ("-e", f"{name}={value}")),
        "-v", f"{REPO / 'deploy' / 'backup'}:/deploy/backup:ro",
        *(["--mount", backups] if backups else ["-v", f"{place.backups}:/backups"]),
        "-v", f"{place.blobs}:/var/lib/unshelve/blobs:ro",
        "-v", f"{place.state}:/var/lib/unshelve/backup-status",
        *wrapper,
        POSTGRES_IMAGE, *command,
    ]  # fmt: skip
    return subprocess.run(docker, capture_output=True, check=False, timeout=timeout)  # noqa: S603


def _script(
    place: Place,
    *args: str,
    environment: dict[str, str] | None = None,
    as_root: bool = False,
    backups: str | None = None,
    fakes: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[bytes]:
    """backup.sh <args> in the image of the database, as the service runs it."""
    return _in_image(
        place,
        "bash",
        "/deploy/backup/backup.sh",
        *args,
        environment=environment,
        as_root=as_root,
        backups=backups,
        fakes=fakes,
        timeout=timeout,
    )


def _backups(place: Place) -> list[Path]:
    folder = place.backups / "db"
    if not folder.exists():
        return []
    return sorted(path for path in folder.iterdir() if path.name[0].isdigit())


def _parts(place: Place) -> list[Path]:
    return [*place.backups.glob("db/.part-*"), *place.backups.glob("blobs/*/*/*.part")]


def _status(place: Place) -> dict[str, str]:
    lines = (place.state / "status").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _old_backup(place: Place, moment: datetime) -> Path:
    """A complete backup folder of an earlier time, with a small dump for the check of the room."""
    folder = place.backups / "db" / _stamp(moment)
    folder.mkdir(parents=True)
    (folder / "unshelve.dump").write_bytes(b"an old dump")
    (folder / "SHA256SUMS").write_text("")
    return folder


@pytest.fixture
def place(tmp_path: Path, private_database_url: str) -> Place:
    folders = [tmp_path / name for name in ("backups", "blobs", "state")]
    for folder in folders:
        folder.mkdir()
    database = make_url(private_database_url).database
    assert database is not None
    return Place(database, *folders)


async def _seed(sessions: Sessions, place: Place) -> None:
    """Two items, a stored file with its asset, and a capture that names a lost file."""
    path = _blob(place.blobs, STORED_KEY)
    path.parent.mkdir(parents=True)
    path.write_bytes(STORED)
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 4646)
        db.add_all(
            Item(owner_id=owner_id, first_captured_at=NOW, last_captured_at=NOW, title=title)
            for title in ("A", "B")
        )
        db.add(
            Asset(
                owner_id=owner_id,
                sha256=STORED_KEY,
                mime="image/jpeg",
                size_bytes=len(STORED),
                storage_key=STORED_KEY,
            )
        )
        db.add(
            Capture(
                owner_id=owner_id,
                source=CaptureSource.EXPORT,
                chat_id=-1001,
                message_id=1,
                posted_at=NOW,
                text="a photo",
                media=[{"kind": "photo", "sha256": LOST_KEY}],
            )
        )
        await db.commit()


# --- A backup ----------------------------------------------------------------------------


async def test_a_backup_holds_the_dump_the_facts_and_each_file(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)

    done = _script(place, "once")

    assert done.returncode == 0, done.stderr.decode()
    [backup] = _backups(place)
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
    assert _blob(place.backups / "blobs", STORED_KEY).read_bytes() == STORED
    assert (backup / "missing-blobs.txt").read_text().split() == [LOST_KEY]
    manifest = (backup / "manifest.tsv").read_text().splitlines()
    assert any(line.startswith("table\tpublic.items\t2\t") for line in manifest)
    assert "role\tunshelve_publisher\tf" in manifest
    assert _status(place)["last_name"] == backup.name
    # The folder of the backups keeps itself out of git, also inside a clone.
    assert (place.backups / ".gitignore").read_text() == "*\n"
    assert _parts(place) == []


async def test_the_facts_of_the_backup_are_the_facts_of_the_database_until_a_row_changes(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    _script(place, "once")
    [backup] = _backups(place)
    saved = (backup / "manifest.tsv").read_text().splitlines()

    def manifest_now(**environment: str) -> list[str]:
        query = _script(place, "_manifest", environment=environment)
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
    await _seed(committed_db, place)
    # Another session deletes a row and commits after the snapshot, just before pg_dump starts.
    late_write = """#!/bin/sh
psql -X --quiet -v ON_ERROR_STOP=1 -c "DELETE FROM items WHERE title = 'B'" >/dev/null || exit
exec /usr/bin/pg_dump "$@"
"""

    done = _script(place, "once", fakes={"pg_dump": late_write})

    assert done.returncode == 0, done.stderr.decode()
    [backup] = _backups(place)
    manifest = (backup / "manifest.tsv").read_text().splitlines()
    assert any(line.startswith("table\tpublic.items\t2\t") for line in manifest)
    rows = _in_image(
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
    await _seed(committed_db, place)
    loop = asyncio.get_running_loop()
    async with committed_db() as db:
        # A migration rewrites the table captures and holds its lock for 4 seconds. The backup
        # starts during those seconds: its lock waits for the commit.
        await db.execute(text("ALTER TABLE captures ALTER COLUMN raw TYPE json USING raw::json"))
        backup = loop.run_in_executor(None, functools.partial(_script, place, "once"))
        await asyncio.sleep(4)
        await db.commit()
    done = await backup

    assert done.returncode == 0, done.stderr.decode()
    [backup_folder] = _backups(place)
    # A snapshot from before the commit would see the rewritten table as empty.
    manifest = (backup_folder / "manifest.tsv").read_text().splitlines()
    assert any(line.startswith("table\tpublic.captures\t1\t") for line in manifest)


async def test_a_pg_dump_that_fails_stops_the_backup(committed_db: Sessions, place: Place) -> None:
    await _seed(committed_db, place)
    # A dump that is written, and a pg_dump that reports a failure all the same.
    fails = """#!/bin/sh
/usr/bin/pg_dump "$@"
exit 1
"""

    done = _script(place, "once", fakes={"pg_dump": fails})

    assert done.returncode == 1
    assert b"pg_dump did not finish" in done.stderr
    assert _backups(place) == []
    assert _parts(place) == []


async def test_a_backup_keeps_the_status_that_it_does_not_write(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    (place.state / "status").write_text("started=1761469200\nlast_error=1761469300\n")

    done = _script(place, "once")

    assert done.returncode == 0, done.stderr.decode()
    status = _status(place)
    assert (status["started"], status["last_error"]) == ("1761469200", "1761469300")
    assert int(status["last_ok"]) > 1761469300


async def test_a_second_backup_copies_no_file_again(committed_db: Sessions, place: Place) -> None:
    await _seed(committed_db, place)

    first = _script(place, "once")
    second = _script(place, "once")

    assert (first.returncode, second.returncode) == (0, 0)
    assert len(_backups(place)) == 2
    assert _status(place)["last_blobs_copied"] == "0"


async def test_a_mirror_copy_with_the_wrong_size_is_copied_again(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    # What a crash between the copy and the disk can leave: the name, with too few bytes.
    cut = _blob(place.backups / "blobs", STORED_KEY)
    cut.parent.mkdir(parents=True)
    cut.write_bytes(b"")

    done = _script(place, "once")

    assert done.returncode == 0, done.stderr.decode()
    assert cut.read_bytes() == STORED


async def test_a_backup_in_a_taken_second_takes_the_next_free_one(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    taken = _old_backup(place, NOW)
    # The clock gives the name of the taken backup for the first two names, then the real time.
    clock = f"""#!/bin/sh
if [ "$*" = "-u +%Y%m%dT%H%M%SZ" ]; then
    calls=$(cat /tmp/names 2>/dev/null || echo 0)
    echo $((calls + 1)) >/tmp/names
    if [ "$calls" -lt 2 ]; then echo {taken.name}; exit 0; fi
fi
exec /usr/bin/date "$@"
"""

    done = _script(place, "once", fakes={"date": clock})

    assert done.returncode == 0, done.stderr.decode()
    [new] = [backup for backup in _backups(place) if backup != taken]
    assert new.name > taken.name
    # The taken folder got no file of the new backup.
    assert sorted(path.name for path in taken.iterdir()) == ["SHA256SUMS", "unshelve.dump"]


async def test_a_backup_removes_the_backups_that_the_rule_does_not_keep(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for days in (1, 2, 3, 30):
        _old_backup(place, today - timedelta(days=days))

    done = _script(
        place,
        "once",
        environment={"UNSHELVE_BACKUP_KEEP_DAILY": "2", "UNSHELVE_BACKUP_KEEP_WEEKLY": "0"},
    )

    assert done.returncode == 0, done.stderr.decode()
    # The new backup and the newest old one stay.
    [kept_old, _new] = _backups(place)
    assert kept_old.name == _stamp(today - timedelta(days=1))


# --- A failure ---------------------------------------------------------------------------


async def test_a_file_with_other_bytes_than_its_name_stops_the_backup(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    _blob(place.blobs, STORED_KEY).write_bytes(b"other bytes")

    done = _script(place, "once")
    health = _script(place, "health")

    assert done.returncode == 1
    assert b"does not have the bytes of its name" in done.stderr
    assert _backups(place) == []
    assert not _blob(place.backups / "blobs", STORED_KEY).exists()
    assert health.returncode == 1
    assert health.stdout.startswith(b"the last backup failed")


async def test_a_failed_backup_leaves_no_part_behind(committed_db: Sessions, place: Place) -> None:
    await _seed(committed_db, place)
    _blob(place.blobs, STORED_KEY).write_bytes(b"other bytes")
    # What an earlier run left when a stop cut it short.
    (place.backups / "db" / ".part-20261001T000000Z").mkdir(parents=True)

    first = _script(place, "once")
    second = _script(place, "once")

    assert (first.returncode, second.returncode) == (1, 1)
    # A lasting failure must not fill the disk with one dump for each try.
    assert _parts(place) == []


async def test_a_success_after_a_failure_makes_the_backup_healthy_again(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    _blob(place.blobs, STORED_KEY).write_bytes(b"other bytes")
    failed = _script(place, "once")
    assert failed.returncode == 1
    assert _script(place, "health").returncode == 1
    _blob(place.blobs, STORED_KEY).write_bytes(STORED)

    done = _script(place, "once")
    health = _script(place, "health")

    assert done.returncode == 0, done.stderr.decode()
    assert (health.returncode, health.stdout) == (0, b"ok\n")


async def test_a_backup_stops_when_the_disk_has_too_little_room(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)

    # 50 MB of room, and a backup asks for at least 100 MB.
    done = _script(
        place,
        "once",
        backups="type=tmpfs,destination=/backups,tmpfs-size=52428800,tmpfs-mode=1777",
    )

    assert done.returncode == 1
    assert b"KB are free in the backup folder" in done.stderr
    assert _status(place)["last_error_text"].startswith("only ")


async def test_a_disk_that_became_read_only_stops_the_backup_with_the_reason(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    assert _script(place, "once").returncode == 0
    # The folders of the backups exist; then the disk refuses each write, as after an I/O error.
    read_only = f"type=bind,source={place.backups},destination=/backups,readonly"

    done = _script(place, "once", backups=read_only, timeout=60)

    assert done.returncode == 1
    assert _status(place)["last_error_text"].endswith("Read-only file system")


async def test_the_room_for_a_backup_counts_the_files_that_it_must_copy(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    big = os.urandom(20 * 1024 * 1024)
    path = _blob(place.blobs, hashlib.sha256(big).hexdigest())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(big)

    # 110 MB of room: enough for the 100 MB of reserve, not for the reserve and 20 MB of files.
    done = _script(
        place,
        "once",
        backups="type=tmpfs,destination=/backups,tmpfs-size=115343360,tmpfs-mode=1777",
    )

    assert done.returncode == 1
    assert b"a backup needs" in done.stderr


async def test_the_room_for_a_backup_counts_two_dumps_of_the_last_size(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    # 140 MB of room, and an earlier backup with a dump of 30 MB: 110 MB stay free. That is
    # enough for the reserve of 100 MB, not for the reserve and two dumps of 30 MB.
    earlier = """set -e
mkdir -p /backups/db/20261001T000000Z
head -c 31457280 /dev/zero >/backups/db/20261001T000000Z/unshelve.dump
exec bash /deploy/backup/backup.sh once
"""

    done = _in_image(
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
    await _seed(committed_db, place)

    # As the service runs: as root. On Linux, the owner could not read the files of root.
    done = _script(place, "once", as_root=True)

    assert done.returncode == 0, done.stderr.decode()
    assert {path.stat().st_uid for path in place.backups.rglob("*")} == {os.getuid()}


@needs_real_owners
async def test_a_failed_backup_also_gives_its_files_to_the_owner(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    _blob(place.blobs, STORED_KEY).write_bytes(b"other bytes")

    done = _script(place, "once", as_root=True)

    # The owner must be able to look for the cause without sudo.
    assert done.returncode == 1
    assert {path.stat().st_uid for path in place.backups.rglob("*")} == {os.getuid()}


async def test_a_backup_that_you_start_waits_for_the_one_that_runs(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
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
    done = _in_image(
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
    assert len(_backups(place)) == 2
    assert _parts(place) == []


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
    await _seed(committed_db, place)
    slow = """#!/bin/sh
sleep 5
exec /usr/bin/pg_dump "$@"
"""
    name = f"unshelve-backup-test-{place.database}"
    started = _in_image(
        place, "bash", "/deploy/backup/backup.sh", "loop", fakes={"pg_dump": slow}, detach_as=name
    )
    assert started.returncode == 0, started.stderr.decode()
    try:
        for _ in range(300):
            if _parts(place):
                break
            await asyncio.sleep(0.1)
        assert _parts(place), "the first backup of the loop did not start"

        code = _stop(name)
    finally:
        _remove(name)

    assert code == "0"
    assert _parts(place) == []
    assert _backups(place) == []


# --- The schedule and the health ---------------------------------------------------------


def test_a_backup_is_due_when_the_newest_is_older_than_a_day(place: Place) -> None:
    _old_backup(place, datetime.now(UTC) - timedelta(hours=25))

    assert _script(place, "_due").returncode == 0

    _old_backup(place, datetime.now(UTC) - timedelta(hours=1))

    assert _script(place, "_due").returncode == 1


async def test_a_round_of_the_loop_makes_no_backup_when_none_is_due(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    _old_backup(place, datetime.now(UTC) - timedelta(hours=1))

    done = _script(place, "_tick")

    assert done.returncode == 0, done.stderr.decode()
    assert len(_backups(place)) == 1


async def test_a_round_of_the_loop_waits_after_a_recent_failure(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    ten_minutes_ago = int((datetime.now(UTC) - timedelta(minutes=10)).timestamp())
    (place.state / "status").write_text(f"last_error={ten_minutes_ago}\n")

    _script(place, "_tick", environment={"UNSHELVE_BACKUP_RETRY_MINUTES": "60"})

    assert _backups(place) == []

    _script(place, "_tick", environment={"UNSHELVE_BACKUP_RETRY_MINUTES": "5"})

    assert len(_backups(place)) == 1


def test_the_backup_is_healthy_before_its_first_run(place: Place) -> None:
    done = _script(place, "health")

    assert (done.returncode, done.stdout) == (0, b"ok\n")


@pytest.mark.parametrize(("newest_hours_ago", "healthy"), [(None, False), (72, False), (1, True)])
def test_the_backup_is_unhealthy_after_two_days_without_a_backup(
    place: Place, newest_hours_ago: int | None, healthy: bool
) -> None:
    started = int((datetime.now(UTC) - timedelta(days=5)).timestamp())
    (place.state / "status").write_text(f"started={started}\n")
    if newest_hours_ago is not None:
        _old_backup(place, datetime.now(UTC) - timedelta(hours=newest_hours_ago))

    done = _script(place, "health")

    expected = (0, b"ok\n") if healthy else (1, b"no backup in 48 hours\n")
    assert (done.returncode, done.stdout) == expected


@pytest.mark.parametrize(("started_minutes_ago", "healthy"), [(10, True), (120, False)])
def test_a_restart_gives_the_backup_one_hour_and_not_two_days(
    place: Place, started_minutes_ago: int, healthy: bool
) -> None:
    started = int((datetime.now(UTC) - timedelta(minutes=started_minutes_ago)).timestamp())
    (place.state / "status").write_text(f"started={started}\n")
    _old_backup(place, datetime.now(UTC) - timedelta(days=30))

    done = _script(place, "health")

    expected = (0, b"ok\n") if healthy else (1, b"no backup in 48 hours\n")
    assert (done.returncode, done.stdout) == expected


def test_a_backup_named_in_the_future_shows_and_does_not_stop_the_schedule(place: Place) -> None:
    # A clock that was 3 days ahead named this backup.
    future = _old_backup(place, datetime.now(UTC) + timedelta(days=3))

    due = _script(place, "_due")
    health = _script(place, "health")

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
    done = _script(place, "health", environment={f"UNSHELVE_BACKUP_{setting}": value})

    assert done.returncode == 2
    assert f"UNSHELVE_BACKUP_{setting} {message}".encode() in done.stderr


# --- The export --------------------------------------------------------------------------


async def test_the_export_holds_the_backup_and_the_files_that_it_needs(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    _script(place, "once")
    [backup] = _backups(place)

    done = _script(place, "export", "latest")

    assert done.returncode == 0, done.stderr.decode()
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
        names = set(archive.getnames())
    assert f"db/{backup.name}/unshelve.dump" in names
    assert f"blobs/{STORED_KEY[:2]}/{STORED_KEY[2:4]}/{STORED_KEY}" in names
    assert not [name for name in names if LOST_KEY in name]


async def test_the_export_of_a_damaged_backup_gives_no_tar(
    committed_db: Sessions, place: Place
) -> None:
    await _seed(committed_db, place)
    _script(place, "once")
    [backup] = _backups(place)
    # One changed byte in a file of the backup, as a disk that rots can give.
    facts = (backup / "manifest.tsv").read_bytes()
    (backup / "manifest.tsv").write_bytes(facts[:-2] + bytes([facts[-2] ^ 1]) + facts[-1:])

    done = _script(place, "export", "latest")

    assert done.returncode != 0
    assert done.stdout == b""
