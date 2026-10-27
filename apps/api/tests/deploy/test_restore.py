"""The restore of a backup into an empty instance (deploy/backup/restore.sh, docs/learn/50).

Each test makes a real backup of a seeded database with backup.sh, and restores it with
restore.sh into an empty database of the test server and an empty folder of files, in the image
of the database (tests/deploy/backup_support.py).
"""

import hashlib
import os
import shutil
import subprocess
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from tests.conftest import TEST_SERVER_URL
from tests.deploy.backup_support import (
    BACKUP_MARKS,
    FILES_OF_A_BACKUP,
    MIGRATIONS,
    STORED,
    STORED_KEY,
    Place,
    Sessions,
    Target,
    blob,
    complete_backups,
    in_image,
    needs_real_owners,
    restore,
    script,
    seed,
)
from unshelve.testing.db import create_database, drop_database, unique_name

pytestmark = BACKUP_MARKS


@pytest.fixture
def target(tmp_path: Path) -> Generator[Target]:
    """An empty database, as `docker compose up -d --wait db` makes it, and an empty volume."""
    name = unique_name("unshelve_r")
    create_database(TEST_SERVER_URL, name)
    (tmp_path / "restored").mkdir()
    try:
        yield Target(name, tmp_path / "restored")
    finally:
        drop_database(TEST_SERVER_URL, name)


def _query(place: Place, target: Target, statement: str) -> str:
    """One statement in the database of the target, with its rows as text."""
    done = in_image(
        place,
        "psql",
        "-X",
        "--no-align",
        "--tuples-only",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        statement,
        environment={
            "PGDATABASE": target.database,
            "PGPASSWORD": str(make_url(TEST_SERVER_URL).password),
        },
    )
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout.decode().strip()


def _tables(place: Place, target: Target) -> int:
    return int(_query(place, target, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"))


async def _backup(sessions: Sessions, place: Place) -> Path:
    await seed(sessions, place)
    done = script(place, "once")
    assert done.returncode == 0, done.stderr.decode()
    [backup] = complete_backups(place)
    return backup


def _reseal(backup: Path) -> None:
    """New checksums after a test changed a file of the backup on purpose."""
    names = [name for name in FILES_OF_A_BACKUP if name != "SHA256SUMS"]
    sums = [f"{hashlib.sha256((backup / name).read_bytes()).hexdigest()}  {name}" for name in names]
    (backup / "SHA256SUMS").write_text("\n".join(sums) + "\n")


# --- A restore ---------------------------------------------------------------------------


async def test_a_restore_into_an_empty_instance_passes_the_data_check(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    backup = await _backup(committed_db, place)

    done = restore(place, target, "latest")

    output = done.stdout.decode()
    assert done.returncode == 0, output + done.stderr.decode()
    assert "not ok" not in output
    assert output.endswith(
        f"the data check passed: the database and the files equal the backup {backup.name}\n"
    )
    assert "ok: the facts of the database equal the facts of the backup" in output
    assert _query(place, target, "SELECT string_agg(title, ',' ORDER BY title) FROM items") == "A,B"
    assert blob(target.blobs, STORED_KEY).read_bytes() == STORED
    # No unfinished copy stays in the volume.
    assert [path.name for path in target.blobs.rglob(".part-*")] == []


async def test_the_list_shows_the_complete_backups_only(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    backup = await _backup(committed_db, place)
    (place.backups / "db" / ".part-20261027T000000Z").mkdir()

    done = restore(place, target, "list")

    # The rows of each backup: an empty backup is easy to see.
    made = datetime.strptime(backup.name, "%Y%m%dT%H%M%S%z").strftime("%Y-%m-%d %H:%M")
    expected = f"{backup.name}\tmade {made} UTC\t2 items\t1 files (1 more lost before the backup)\n"
    assert (done.returncode, done.stdout.decode()) == (0, expected)


async def test_a_restore_that_stopped_after_the_database_goes_on_with_the_files(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    await _backup(committed_db, place)
    assert restore(place, target, "latest").returncode == 0
    # The state after a stop between pg_restore and the files: a copy that did not finish.
    shutil.rmtree(target.blobs / STORED_KEY[:2])
    (target.blobs / ".part-1234").write_bytes(b"half a file")

    done = restore(place, target, "latest")

    output = done.stdout.decode()
    assert done.returncode == 0, output + done.stderr.decode()
    assert "the database already holds the backup" in output
    assert blob(target.blobs, STORED_KEY).read_bytes() == STORED
    assert not (target.blobs / ".part-1234").exists()


# --- A refusal changes nothing -----------------------------------------------------------


async def test_a_restore_refuses_a_database_that_is_not_empty(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    await _backup(committed_db, place)
    # A stack that `migrate` already prepared: its tables exist, with other facts.
    _query(place, target, "CREATE TABLE items (id int)")

    done = restore(place, target, "latest")

    assert done.returncode == 1
    assert b"is not empty" in done.stderr
    assert _query(place, target, "SELECT count(*) FROM items") == "0"


async def test_a_damaged_file_in_the_mirror_stops_the_restore_before_any_change(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    await _backup(committed_db, place)
    mirror = blob(place.backups / "blobs", STORED_KEY)
    mirror.write_bytes(b"x" + mirror.read_bytes()[1:])

    done = restore(place, target, "latest")

    assert done.returncode == 1
    assert f"not ok: blob {STORED_KEY}".encode() in done.stdout
    assert b"nothing was changed" in done.stderr
    assert _tables(place, target) == 0


async def test_a_damaged_backup_stops_the_restore_before_any_change(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    backup = await _backup(committed_db, place)
    facts = (backup / "manifest.tsv").read_bytes()
    (backup / "manifest.tsv").write_bytes(facts.replace(b"public.items\t2", b"public.items\t3"))

    done = restore(place, target, "latest")

    assert done.returncode == 1
    assert b"is damaged: a file does not match its checksum" in done.stderr
    assert _tables(place, target) == 0


async def test_a_backup_from_newer_code_is_refused(
    committed_db: Sessions, place: Place, target: Target, tmp_path: Path
) -> None:
    await _backup(committed_db, place)
    # A checkout from before the last migration of the backup.
    older = tmp_path / "older-checkout"
    older.mkdir()
    for path in sorted(MIGRATIONS.glob("*.py"))[:-1]:
        shutil.copy(path, older / path.name)

    done = restore(place, target, "latest", migrations=older)

    assert done.returncode == 1
    assert b"check out a newer commit" in done.stderr
    assert _tables(place, target) == 0


async def test_a_backup_that_names_a_role_with_a_login_is_refused(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    backup = await _backup(committed_db, place)
    facts = (backup / "manifest.tsv").read_text()
    (backup / "manifest.tsv").write_text(
        facts.replace("role\tunshelve_publisher\tf", "role\tunshelve_publisher\tt")
    )
    _reseal(backup)

    done = restore(place, target, "latest")

    assert done.returncode == 1
    assert b"names a role that can log in (unshelve_publisher)" in done.stderr
    assert _tables(place, target) == 0


async def test_a_volume_of_files_from_another_instance_is_refused(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    await _backup(committed_db, place)
    stranger = blob(target.blobs, hashlib.sha256(b"a file of another instance").hexdigest())
    stranger.parent.mkdir(parents=True)
    stranger.write_bytes(b"a file of another instance")

    done = restore(place, target, "latest")

    assert done.returncode == 1
    assert b"holds files that are not in the backup" in done.stderr
    assert _tables(place, target) == 0
    assert [path for path in target.blobs.rglob("*") if path.is_file()] == [stranger]


@pytest.mark.parametrize(
    ("line", "changed", "message"),
    [
        (
            "server\t17.",
            "server\t16.",
            "the backup comes from Postgres 16, and this server is Postgres 17",
        ),
        (
            "extension\tvector\t0.8.7",
            "extension\tvector\t9.9.9",
            "the extension vector 9.9.9 of the backup is not available here (0.8.7)",
        ),
    ],
)
async def test_a_backup_from_another_version_is_refused(
    committed_db: Sessions, place: Place, target: Target, line: str, changed: str, message: str
) -> None:
    backup = await _backup(committed_db, place)
    info = (backup / "info.txt").read_text()
    assert line in info
    (backup / "info.txt").write_text(info.replace(line, changed, 1))
    _reseal(backup)

    done = restore(place, target, "latest")

    assert done.returncode == 1
    assert message.encode() in done.stderr
    assert _tables(place, target) == 0


async def test_a_pg_restore_that_fails_leaves_the_database_empty(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    await _backup(committed_db, place)
    # No table, so the target counts as empty; but the name of an index of the dump is taken.
    _query(place, target, "CREATE VIEW ix_items_title_trgm AS SELECT 1")

    done = restore(place, target, "latest")

    # One transaction: the tables that pg_restore made before the error are gone again.
    assert done.returncode != 0
    assert _tables(place, target) == 0


async def test_the_facts_come_from_the_manifest_sql_of_the_backup(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    backup = await _backup(committed_db, place)
    # An older manifest.sql, without the indexes: the facts of the backup lack them too.
    manifest = (backup / "manifest.sql").read_text()
    start = manifest.index("SELECT 'index'")
    (backup / "manifest.sql").write_text(
        manifest[:start] + manifest[manifest.index(";", start) + 1 :]
    )
    facts = (backup / "manifest.tsv").read_text().splitlines(keepends=True)
    (backup / "manifest.tsv").write_text(
        "".join(line for line in facts if not line.startswith("index\t"))
    )
    _reseal(backup)

    done = restore(place, target, "latest")

    assert done.returncode == 0, done.stdout.decode() + done.stderr.decode()


# --- The data check names each fault -----------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "named"),
    [
        (
            "UPDATE items SET title = 'changed' WHERE title = 'A'",
            "not ok: table public.items differs",
        ),
        ("DELETE FROM captures", "not ok: table public.captures differs"),
        ("DROP INDEX ix_items_title_trgm", "not ok: index ix_items_title_trgm differs"),
        ("ALTER TABLE events DISABLE TRIGGER events_append_only", "not ok: trigger events differs"),
        (
            "ALTER TABLE events DISABLE TRIGGER events_no_truncate",
            "not ok: the triggers events_append_only and events_no_truncate are on",
        ),
        (
            "ALTER TABLE events DISABLE TRIGGER events_no_truncate",
            "not ok: the table events refuses TRUNCATE",
        ),
        ("ALTER FUNCTION u_norm(text) VOLATILE", "not ok: u_norm() is IMMUTABLE"),
        (
            "REVOKE SELECT ON pub.items FROM unshelve_publisher",
            "not ok: unshelve_publisher reads pub.items",
        ),
        ("SELECT setval('items_id_seq', 1)", "not ok: the sequence items_id_seq is below an id"),
        (
            "SELECT setval('capture_api_seq', 7)",
            "not ok: the sequence public.capture_api_seq is at 7 true",
        ),
        # A rule that cannot run is a failure too, not a rule that is left out.
        ("ALTER TABLE events RENAME TO old_events", "not ok: checks.sql stopped"),
    ],
)
async def test_the_data_check_names_each_fault(
    committed_db: Sessions, place: Place, target: Target, fault: str, named: str
) -> None:
    await _backup(committed_db, place)
    assert restore(place, target, "latest").returncode == 0
    _query(place, target, fault)

    done = restore(place, target, "check", "latest")

    assert done.returncode == 1
    assert named in done.stdout.decode()
    assert b"the data check failed" in done.stderr


async def test_the_data_check_names_a_file_with_other_bytes(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    await _backup(committed_db, place)
    assert restore(place, target, "latest").returncode == 0
    blob(target.blobs, STORED_KEY).write_bytes(b"other bytes")

    done = restore(place, target, "check", "latest")

    assert done.returncode == 1
    assert (
        f"not ok: blob {STORED_KEY}: the volume lacks it, or holds other bytes".encode()
        in done.stdout
    )
    assert (
        f"not ok: blob {STORED_KEY} has 11 bytes, and assets says {len(STORED)}".encode()
        in done.stdout
    )


async def test_the_data_check_names_files_of_another_user(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    await _backup(committed_db, place)
    assert restore(place, target, "latest").returncode == 0

    # The files belong to the user of the test; the check expects another user of the app.
    done = restore(place, target, "check", "latest", environment={"UNSHELVE_APP_UID": "12345"})

    assert done.returncode == 1
    assert (
        b"not ok: the volume of the files has files that do not belong to the user 12345"
        in done.stdout
    )


@needs_real_owners
async def test_the_restored_files_belong_to_the_user_of_the_app(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    await _backup(committed_db, place)

    def as_root(*command: str, uid: str) -> subprocess.CompletedProcess[bytes]:
        return in_image(
            place,
            *command,
            as_root=True,
            environment={"PGDATABASE": target.database, "UNSHELVE_APP_UID": uid},
            backups=f"type=bind,source={place.backups},destination=/backups,readonly",
            blobs=f"{target.blobs}:/var/lib/unshelve/blobs",
            mounts=(f"{MIGRATIONS}:/deploy/migrations:ro",),
        )

    # As the service runs: as root, for the user 10001 of Dockerfile.api.
    done = as_root("bash", "/deploy/backup/restore.sh", "latest", uid="10001")
    try:
        assert done.returncode == 0, done.stdout.decode() + done.stderr.decode()
        assert {path.stat().st_uid for path in target.blobs.rglob("*")} == {10001}
    finally:
        # The files go back to the user of the test, who must remove them after the test.
        as_root("chown", "-R", str(os.getuid()), "/var/lib/unshelve/blobs", uid="10001")


async def test_the_data_check_names_an_unfinished_file_and_an_older_extension(
    committed_db: Sessions, place: Place, target: Target
) -> None:
    backup = await _backup(committed_db, place)
    assert restore(place, target, "latest").returncode == 0
    (target.blobs / ".part-1234").write_bytes(b"half a file")
    info = (backup / "info.txt").read_text()
    (backup / "info.txt").write_text(
        info.replace("extension\tpg_trgm\t1.6", "extension\tpg_trgm\t9.9")
    )
    _reseal(backup)

    # check runs no check of the versions first: the database is as it is now.
    done = restore(place, target, "check", "latest")

    output = done.stdout.decode()
    assert done.returncode == 1
    assert "not ok: the volume of the files has unfinished files (.part-*)" in output
    assert "not ok: extension pg_trgm is 1.6, and the backup had 9.9" in output
