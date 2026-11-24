"""The healthchecks of the worker and the bot, and their volume `alive` (unshelve.alive,
docs/learn/55), as Docker Compose reads them. No container starts."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from tests.deploy.support import REPO, TEST_PASSWORD, compose_config, docker_available
from unshelve import alive
from unshelve.bot.runner import IDLE_MARK_EVERY
from unshelve.queue.worker import WorkerConfig

needs_compose = pytest.mark.skipif(
    not docker_available("compose", "version"), reason="needs docker compose"
)
FILES = ["compose.yaml", "compose.prod.yaml"]
WATCHED = ("worker", "bot")
MAX_AGE = 180
# The socket of Docker on Linux. /var/run is a link to /run.
SOCKETS = (PurePosixPath("/var/run/docker.sock"), PurePosixPath("/run/docker.sock"))


def _services(tmp_path: Path, file: str) -> dict[str, Any]:
    """The services of ``file``, with the profile of the restore: each rule holds for it too."""
    done = compose_config(
        tmp_path,
        file,
        UNSHELVE_DOMAIN="unshelve.test",
        UNSHELVE_DB_PASSWORD=TEST_PASSWORD,
        COMPOSE_PROFILES="restore",
    )
    assert done.returncode == 0, done.stderr
    services: dict[str, Any] = json.loads(done.stdout)["services"]
    return services


def _alive_mount(service: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (mount for mount in service.get("volumes", []) if mount["target"] == "/app/var/alive"),
        None,
    )


@needs_compose
@pytest.mark.parametrize("file", FILES)
def test_the_worker_and_the_bot_write_their_liveness_files_to_the_volume_alive(
    tmp_path: Path, file: str
) -> None:
    services = _services(tmp_path, file)

    assert {name for name, service in services.items() if _alive_mount(service)} == set(WATCHED)
    for name in WATCHED:
        # A volume, not a folder of the computer: the files belong to the user of the app.
        assert _alive_mount(services[name]) == {
            "type": "volume",
            "source": "alive",
            "target": "/app/var/alive",
            "volume": {},
        }, name
    # The default folder of unshelve.alive: no setting is needed.
    assert str(alive.DEFAULT_FOLDER) == "/app/var/alive"


@needs_compose
@pytest.mark.parametrize("file", FILES)
def test_the_worker_and_the_bot_have_a_healthcheck_on_their_liveness_file(
    tmp_path: Path, file: str
) -> None:
    services = _services(tmp_path, file)

    for name in WATCHED:
        assert services[name]["healthcheck"] == {
            "test": [
                "CMD",
                "python",
                "-m",
                "unshelve.alive",
                "check",
                name,
                "--max-age",
                str(MAX_AGE),
            ],
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
            # A check that fails in the first minute does not count. In that minute, Docker
            # checks every 2 s, so `up --wait` sees the first mark at once.
            "start_period": "1m0s",
            "start_interval": "2s",
        }, name


@needs_compose
@pytest.mark.parametrize("file", FILES)
def test_no_service_gets_the_socket_of_docker(tmp_path: Path, file: str) -> None:
    # The socket gives root on the host. A service that restarts unhealthy containers would
    # need it, so the stack has none: an unhealthy state only shows the problem.
    services = _services(tmp_path, file)

    mounts = [
        (name, mount) for name, service in services.items() for mount in service.get("volumes", [])
    ]

    assert mounts
    assert [(name, mount) for name, mount in mounts if _gives_the_socket(mount)] == []


def _gives_the_socket(mount: dict[str, Any]) -> bool:
    """Whether the container sees the socket of Docker through ``mount``: the socket itself, or
    a folder of the computer that holds it, such as ``/var/run`` or ``/``."""
    source = mount.get("source", "")
    if "docker.sock" in f"{source} {mount['target']}":
        return True
    return mount["type"] == "bind" and any(
        socket.is_relative_to(PurePosixPath(source)) for socket in SOCKETS
    )


@pytest.mark.parametrize(
    ("mount", "gives"),
    [
        (
            {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"},
            True,
        ),
        ({"type": "bind", "source": "/var/run", "target": "/host-run"}, True),
        ({"type": "bind", "source": "/run", "target": "/host-run"}, True),
        ({"type": "bind", "source": "/", "target": "/host"}, True),
        ({"type": "bind", "source": "/srv/unshelve/keys", "target": "/keys"}, False),
        ({"type": "bind", "source": "/var/backups", "target": "/backups"}, False),
        ({"type": "volume", "source": "alive", "target": "/app/var/alive"}, False),
    ],
)
def test_a_folder_that_holds_the_socket_counts_as_the_socket(
    mount: dict[str, Any], *, gives: bool
) -> None:
    assert _gives_the_socket(mount) is gives


@needs_compose
@pytest.mark.parametrize("name", WATCHED)
def test_the_command_of_the_healthcheck_reads_the_liveness_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    folder = tmp_path / "alive"
    monkeypatch.setenv("UNSHELVE_ALIVE_DIR", str(folder))
    command = _services(tmp_path, "compose.yaml")[name]["healthcheck"]["test"]
    assert command[:2] == ["CMD", "python"]
    # The python of this test, with the rest of the command exactly as Docker runs it.
    run = [sys.executable, *command[2:]]
    environment = {**os.environ, "UNSHELVE_ALIVE_DIR": str(folder)}

    before = subprocess.run(run, env=environment, capture_output=True, text=True, check=False)  # noqa: S603
    alive.mark(name)
    after = subprocess.run(run, env=environment, capture_output=True, text=True, check=False)  # noqa: S603
    # A mark just older than the limit, on the clock of the computer.
    old = time.time() - MAX_AGE - 1
    os.utime(folder / name, (old, old))
    late = subprocess.run(run, env=environment, capture_output=True, text=True, check=False)  # noqa: S603

    assert (before.returncode, after.returncode, late.returncode) == (1, 0, 1), after.stderr


def test_the_limit_of_the_check_leaves_room_for_three_missed_marks() -> None:
    # The slowest regular mark of each process, in seconds. A mark that falls inside the 10 s
    # of the last one waits for the next turn.
    worker = WorkerConfig(worker_id="w").lease.total_seconds() / 3 + alive.MARK_EVERY
    idle_bot = IDLE_MARK_EVERY.total_seconds()
    polling_bot = 10 + alive.MARK_EVERY  # a quiet getUpdates answers after 10 s

    assert max(worker, idle_bot, polling_bot) * 3 <= MAX_AGE


def test_the_image_makes_the_folder_of_the_liveness_files_for_the_app() -> None:
    # A new volume takes the owner of its folder in the image. Without the folder, the volume
    # belongs to root, and the app cannot write its liveness file.
    dockerfile = (REPO / "deploy" / "Dockerfile.api").read_text(encoding="utf-8")

    # The step of the user, with each line that a backslash continues.
    step = re.search(r"RUN useradd(?:[^\n]*\\\n)*[^\n]*", dockerfile)

    assert step is not None
    assert re.search(r"mkdir -p [^\n]*/app/var/alive", step.group(0))
    assert "chown -R app /app/var" in step.group(0)
    assert re.search(r"^USER app$", dockerfile, re.MULTILINE)


def test_the_image_has_the_bytecode_of_python_and_of_the_app() -> None:
    # The base image has no bytecode of the standard library, and PYTHONDONTWRITEBYTECODE keeps
    # Python from writing it. Without these steps, each check compiled each module again, and
    # took about 300 ms instead of about 60 ms.
    dockerfile = (REPO / "deploy" / "Dockerfile.api").read_text(encoding="utf-8")

    assert re.search(
        r"^RUN python -m compileall .*get_path\(\"stdlib\"\)", dockerfile, re.MULTILINE
    )
    assert re.search(r"python -m compileall -q /app/src$", dockerfile, re.MULTILINE)
