"""Helpers for the tests of the backup and the restore (deploy/backup/, docs/learn/50).

Each test runs the real script in the image of the database, as the services `backup` and
`restore` do: with the pg_dump, pg_restore, psql and bash of that image, and at the paths of the
services. The scripts work on databases of the test and on folders of the test, so a test never
touches the instance on this computer.
"""

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import TEST_SERVER_URL
from tests.deploy.support import POSTGRES_IMAGE, REPO, docker_available
from unshelve.auth import store as auth
from unshelve.contracts.enums import CaptureSource
from unshelve.db.models import Asset, Capture, Item

Sessions = async_sessionmaker[AsyncSession]

# The marks of each test module of the backup and the restore.
BACKUP_MARKS = [
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
    "manifest.sql",
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


def blob(root: Path, key: str) -> Path:
    return root / key[:2] / key[2:4] / key


def in_image(
    place: Place,
    *command: str,
    environment: dict[str, str] | None = None,
    as_root: bool = False,
    backups: str | None = None,
    blobs: str | None = None,
    mounts: tuple[str, ...] = (),
    fakes: dict[str, str] | None = None,
    detach_as: str | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[bytes]:
    """A command in the image of the database, at the paths of the service `backup`.

    `backups` replaces the bind mount of /backups with another `--mount`, for example a tmpfs.
    `blobs` replaces the read-only mount of the files with another `-v`, and `mounts` adds more.
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
        "-v", blobs or f"{place.blobs}:/var/lib/unshelve/blobs:ro",
        "-v", f"{place.state}:/var/lib/unshelve/backup-status",
        *(item for mount in mounts for item in ("-v", mount)),
        *wrapper,
        POSTGRES_IMAGE, *command,
    ]  # fmt: skip
    return subprocess.run(docker, capture_output=True, check=False, timeout=timeout)  # noqa: S603


def script(
    place: Place,
    *args: str,
    environment: dict[str, str] | None = None,
    as_root: bool = False,
    backups: str | None = None,
    fakes: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[bytes]:
    """backup.sh <args> in the image of the database, as the service runs it."""
    return in_image(
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


def complete_backups(place: Place) -> list[Path]:
    folder = place.backups / "db"
    if not folder.exists():
        return []
    return sorted(path for path in folder.iterdir() if path.name[0].isdigit())


def parts(place: Place) -> list[Path]:
    return [*place.backups.glob("db/.part-*"), *place.backups.glob("blobs/*/*/*.part")]


def status_of(place: Place) -> dict[str, str]:
    lines = (place.state / "status").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def old_backup(place: Place, moment: datetime) -> Path:
    """A complete backup folder of an earlier time, with a small dump for the check of the room."""
    folder = place.backups / "db" / stamp(moment)
    folder.mkdir(parents=True)
    (folder / "unshelve.dump").write_bytes(b"an old dump")
    (folder / "SHA256SUMS").write_text("")
    return folder


async def seed(sessions: Sessions, place: Place) -> None:
    """Two items, a stored file with its asset, and a capture that names a lost file."""
    path = blob(place.blobs, STORED_KEY)
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


# --- The restore -------------------------------------------------------------------------

MIGRATIONS = REPO / "apps" / "api" / "migrations" / "versions"


@dataclass(frozen=True, slots=True)
class Target:
    """An empty instance for a restore: a database, and the folder of its volume of files."""

    database: str
    blobs: Path


def restore(
    place: Place,
    target: Target,
    *args: str,
    migrations: Path = MIGRATIONS,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """restore.sh <args> in the image of the database, as the service `restore` runs it: the
    backups read-only, the volume of the files of the target, and the revisions of a checkout.
    The user of the test plays the user of the app."""
    return in_image(
        place,
        "bash",
        "/deploy/backup/restore.sh",
        *args,
        environment={"PGDATABASE": target.database, "UNSHELVE_APP_UID": str(os.getuid())}
        | (environment or {}),
        backups=f"type=bind,source={place.backups},destination=/backups,readonly",
        blobs=f"{target.blobs}:/var/lib/unshelve/blobs",
        mounts=(f"{migrations}:/deploy/migrations:ro",),
    )
