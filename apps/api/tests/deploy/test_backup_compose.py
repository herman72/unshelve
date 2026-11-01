"""The service `backup`, as Docker Compose reads it (docs/learn/50). No container starts."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.deploy.support import (
    POSTGRES_IMAGE,
    REPO,
    TEST_PASSWORD,
    compose_config,
    docker_available,
)

pytestmark = pytest.mark.skipif(
    not docker_available("compose", "version"), reason="needs docker compose"
)


def _services(tmp_path: Path, *, dotenv: str | None = None, **variables: str) -> dict[str, Any]:
    done = compose_config(tmp_path, "compose.yaml", dotenv=dotenv, **variables)
    assert done.returncode == 0, done.stderr
    services: dict[str, Any] = json.loads(done.stdout)["services"]
    return services


def test_the_backup_and_the_database_use_one_image_and_so_does_ci(tmp_path: Path) -> None:
    services = _services(tmp_path)
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "api.yml").read_text())

    # pg_dump must not be older than the server: one image gives both the same version.
    assert services["db"]["image"] == services["backup"]["image"] == POSTGRES_IMAGE
    # The tests of the backup run against the database of CI: the same image again.
    assert workflow["jobs"]["api"]["services"]["postgres"]["image"] == POSTGRES_IMAGE


@pytest.mark.parametrize("service", ["backup", "restore"])
def test_the_backup_and_the_restore_get_the_password_of_the_database_and_no_other_secret(
    tmp_path: Path, service: str
) -> None:
    # A .env as the owner writes it: the password of the database, and the keys of the other
    # services. Each key is a word that a test can look for.
    dotenv = "\n".join(
        [
            f"UNSHELVE_DB_PASSWORD={TEST_PASSWORD}",
            "UNSHELVE_TELEGRAM_BOT_TOKEN=sentinel-bot-token",
            "ANTHROPIC_API_KEY=sentinel-model-key",
            "VOYAGE_API_KEY=sentinel-embedding-key",
            "UNSHELVE_PUBLISH_GIT_SSH_KEY=sentinel-deploy-key",
        ]
    )

    services = _services(tmp_path, dotenv=dotenv, COMPOSE_PROFILES="restore")

    environment = services[service]["environment"]
    assert environment["UNSHELVE_DB_PASSWORD"] == TEST_PASSWORD
    assert [name for name, value in environment.items() if "sentinel" in str(value)] == []
    # The .env reached the stack: the worker got its keys from it.
    assert services["worker"]["environment"]["ANTHROPIC_API_KEY"] == "sentinel-model-key"


def test_the_backup_reads_the_files_and_writes_to_a_folder_of_this_computer(
    tmp_path: Path,
) -> None:
    backup = _services(tmp_path)["backup"]

    mounts = {volume["target"]: volume for volume in backup["volumes"]}

    assert mounts["/var/lib/unshelve/blobs"] == {
        "type": "volume",
        "source": "blobs",
        "target": "/var/lib/unshelve/blobs",
        "read_only": True,
        "volume": {},
    }
    assert mounts["/deploy/backup"]["read_only"] is True
    # The lock and the status lie on a Docker volume: on a shared folder of Docker Desktop, a
    # lock taken by the call that made the file keeps nobody out.
    assert mounts["/var/lib/unshelve/backup-status"]["type"] == "volume"
    # A folder, not a volume: `docker compose down --volumes` cannot delete the backups.
    assert mounts["/backups"]["type"] == "bind"
    assert mounts["/backups"]["source"] == str(tmp_path / "repo" / "backups")
    assert "ports" not in backup


def test_the_folder_of_the_backups_can_be_any_absolute_path(tmp_path: Path) -> None:
    folder = tmp_path / "elsewhere"

    backup = _services(tmp_path, UNSHELVE_BACKUP_DIR=str(folder))["backup"]

    assert {volume["target"]: volume["source"] for volume in backup["volumes"]}["/backups"] == str(
        folder
    )


def test_the_backup_reports_its_health_and_starts_after_the_migration(tmp_path: Path) -> None:
    backup = _services(tmp_path)["backup"]

    assert backup["healthcheck"]["test"] == ["CMD", "bash", "/deploy/backup/backup.sh", "health"]
    assert backup["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert backup["command"] == ["bash", "/deploy/backup/backup.sh", "loop"]
    # A stop waits for a dump that runs; the backup then removes its unfinished part.
    assert backup["stop_grace_period"] == "2m0s"


def test_the_defaults_make_a_backup_each_day_and_keep_a_month(tmp_path: Path) -> None:
    environment = _services(tmp_path)["backup"]["environment"]

    # With 7 and 6 calendar weeks, the oldest backup is 29 to 35 days old
    # (test_backup_scripts.py checks the window).
    assert {
        name: environment[name] for name in environment if name.startswith("UNSHELVE_BACKUP_")
    } == {
        "UNSHELVE_BACKUP_EVERY_HOURS": "24",
        "UNSHELVE_BACKUP_KEEP_DAILY": "7",
        "UNSHELVE_BACKUP_KEEP_WEEKLY": "6",
        "UNSHELVE_BACKUP_RETRY_MINUTES": "60",
    }


def test_the_restore_starts_only_when_a_person_runs_it(tmp_path: Path) -> None:
    assert "restore" not in _services(tmp_path)

    restore = _services(tmp_path, COMPOSE_PROFILES="restore")["restore"]

    assert restore["profiles"] == ["restore"]
    assert restore["image"] == POSTGRES_IMAGE
    assert restore["entrypoint"] == ["bash", "/deploy/backup/restore.sh"]
    # The database only: `migrate` would make tables, and a restore needs an empty database.
    assert {name: rule["condition"] for name, rule in restore["depends_on"].items()} == {
        "db": "service_healthy"
    }
    assert restore["restart"] == "no"


def test_the_restore_reads_the_backups_and_writes_the_volume_of_the_files(tmp_path: Path) -> None:
    restore = _services(tmp_path, COMPOSE_PROFILES="restore")["restore"]

    mounts = {volume["target"]: volume for volume in restore["volumes"]}

    # A restore never changes a backup.
    assert (mounts["/backups"]["type"], mounts["/backups"]["read_only"]) == ("bind", True)
    assert mounts["/var/lib/unshelve/blobs"] == {
        "type": "volume",
        "source": "blobs",
        "target": "/var/lib/unshelve/blobs",
        "volume": {},
    }
    # The revisions of this checkout: a backup from newer code is refused.
    assert mounts["/deploy/migrations"]["read_only"] is True
    assert mounts["/deploy/migrations"]["source"].endswith("apps/api/migrations/versions")


def test_the_restored_files_belong_to_the_user_of_the_app(tmp_path: Path) -> None:
    restore = _services(tmp_path, COMPOSE_PROFILES="restore")["restore"]
    dockerfile = (REPO / "deploy" / "Dockerfile.api").read_text()

    found = re.search(r"useradd --system --uid (\d+)", dockerfile)

    assert found is not None
    assert restore["environment"]["UNSHELVE_APP_UID"] == found.group(1)
