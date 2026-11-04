"""The folder `keys/` of the stack: the worker reads the deploy key of the git target from it
(keys/README.md, docs/learn/43). No container starts."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.deploy.support import REPO, compose_config, docker_available

needs_compose = pytest.mark.skipif(
    not docker_available("compose", "version"), reason="needs docker compose"
)
# A plain word, so that no scanner takes it for a real secret.
PASSWORD = "unshelvetest"  # noqa: S105 - a made-up password for the test


def _services(tmp_path: Path, file: str = "compose.yaml", **variables: str) -> dict[str, Any]:
    done = compose_config(tmp_path, file, **variables)
    assert done.returncode == 0, done.stderr
    services: dict[str, Any] = json.loads(done.stdout)["services"]
    return services


def _keys(service: dict[str, Any]) -> dict[str, Any] | None:
    """The mount at /keys of `service`, or None."""
    return next((mount for mount in service.get("volumes", []) if mount["target"] == "/keys"), None)


def _with_keys(services: dict[str, Any]) -> set[str]:
    return {name for name, service in services.items() if _keys(service) is not None}


@needs_compose
def test_only_the_worker_reads_the_keys_of_the_clone_and_it_cannot_change_them(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path, COMPOSE_PROFILES="restore")

    # Only the worker publishes: the API, the bot and the backup get no key.
    assert _with_keys(services) == {"worker"}
    keys = _keys(services["worker"])
    assert keys is not None
    # A folder of the clone, not a volume: the key is a file that a person makes and replaces.
    assert (keys["type"], keys["source"], keys["read_only"]) == (
        "bind",
        str(tmp_path / "repo" / "keys"),
        True,
    )


@needs_compose
def test_the_folder_of_the_keys_can_be_any_absolute_path(tmp_path: Path) -> None:
    folder = tmp_path / "elsewhere"

    keys = _keys(_services(tmp_path, UNSHELVE_KEYS_DIR=str(folder))["worker"])

    assert keys is not None
    assert keys["source"] == str(folder)


@needs_compose
def test_the_production_stack_keeps_the_keys_of_the_worker(tmp_path: Path) -> None:
    services = _services(
        tmp_path,
        "compose.prod.yaml",
        UNSHELVE_DOMAIN="unshelve.test",
        UNSHELVE_DB_PASSWORD=PASSWORD,
        COMPOSE_PROFILES="restore",
    )

    assert _with_keys(services) == {"worker"}
    keys = _keys(services["worker"])
    assert keys is not None
    assert (keys["source"], keys["read_only"]) == (str(tmp_path / "repo" / "keys"), True)


def test_git_ignores_each_key_and_keeps_the_guide(tmp_path: Path) -> None:
    # The .gitignore of the clone alone, in a new repository with no git config: a rule of this
    # computer (its info/exclude, its global ignore file) cannot hide a missing rule.
    shutil.copy(REPO / ".gitignore", tmp_path / ".gitignore")
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    git = shutil.which("git") or "git"
    subprocess.run([git, "init", "--quiet"], cwd=tmp_path, env=environment, check=True)  # noqa: S603
    names = ["keys/deploy_key", "keys/deploy_key.pub", "keys/known_hosts", "keys/README.md"]

    done = subprocess.run(  # noqa: S603 - git with the names of this test
        [git, "check-ignore", "--no-index", *names],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.stdout.splitlines() == names[:3]
    assert (REPO / "keys" / "README.md").is_file()


def test_docker_build_does_not_send_the_keys() -> None:
    # The build context is the root of the clone: without this line, a key would reach the
    # builder, and a later COPY could put it into an image.
    assert "keys" in (REPO / ".dockerignore").read_text().splitlines()
