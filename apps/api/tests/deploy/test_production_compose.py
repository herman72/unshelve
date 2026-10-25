"""The production stack, as Docker Compose reads it (docs/quickstart.md, docs/learn/47).

Each test asks `docker compose config` for the stack, so it checks the files after Compose has
joined, merged and filled them in, and it starts no container. The files are copied to a
folder of their own first: Compose then reads no `.env` of the checkout, so neither the
settings nor the secrets of the developer reach a test. The quickstart jobs of CI start the
real stack.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[4]
DOMAIN = "unshelve.test"
# A plain word, so that no scanner takes it for a real secret.
PASSWORD = "unshelvetest"  # noqa: S105 - a made-up password for the test
APP_SERVICES = ("migrate", "api", "worker", "bot")


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(
        ["docker", "compose", "version"],  # noqa: S607 - the docker of this computer
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


pytestmark = pytest.mark.skipif(not _compose_available(), reason="needs docker compose")


def _config(tmp_path: Path, file: str, **variables: str) -> subprocess.CompletedProcess[str]:
    """`docker compose config` of `file` in a copy of the stack, with only `variables` set."""
    copy = tmp_path / "repo"
    if not copy.exists():
        shutil.copytree(REPO / "deploy", copy / "deploy")
        for name in ("compose.yaml", "compose.prod.yaml"):
            shutil.copy(REPO / name, copy / name)
    # Only what docker needs to run: no UNSHELVE_* variable of the shell of the developer.
    keep = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")
    environment = {name: os.environ[name] for name in keep if name in os.environ}
    return subprocess.run(  # noqa: S603 - docker with arguments of this test
        ["docker", "compose", "-f", str(copy / file), "config", "--format", "json"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=environment | variables,
    )


def _services(tmp_path: Path, file: str = "compose.prod.yaml") -> dict[str, Any]:
    done = _config(tmp_path, file, UNSHELVE_DOMAIN=DOMAIN, UNSHELVE_DB_PASSWORD=PASSWORD)
    assert done.returncode == 0, done.stderr
    services: dict[str, Any] = json.loads(done.stdout)["services"]
    return services


def _published(service: dict[str, Any]) -> list[tuple[str | None, str, str]]:
    return [
        (port.get("host_ip"), str(port["published"]), port.get("protocol", "tcp"))
        for port in service.get("ports", [])
    ]


def test_only_caddy_has_a_port_and_it_has_80_and_443(tmp_path: Path) -> None:
    services = _services(tmp_path)

    published = {name: _published(service) for name, service in services.items()}

    assert {name for name, ports in published.items() if ports} == {"caddy"}
    assert sorted(published["caddy"]) == [
        (None, "443", "tcp"),
        (None, "443", "udp"),
        (None, "80", "tcp"),
    ]


def test_each_app_service_gets_the_https_address_and_the_password(tmp_path: Path) -> None:
    services = _services(tmp_path)

    for name in APP_SERVICES:
        environment = services[name]["environment"]
        # https:// gives the session cookie its Secure flag (api/routes/auth.py).
        assert environment["UNSHELVE_WEB_BASE_URL"] == f"https://{DOMAIN}", name
        assert environment["UNSHELVE_DATABASE_URL"] == (
            f"postgresql+psycopg://unshelve:{PASSWORD}@db:5432/unshelve"
        ), name
        # The settings of deploy/compose.yaml stay: the override adds to them.
        assert environment["UNSHELVE_LOG_JSON"] == "true", name
    assert services["db"]["environment"]["POSTGRES_PASSWORD"] == PASSWORD
    assert services["caddy"]["environment"]["UNSHELVE_DOMAIN"] == DOMAIN


@pytest.mark.parametrize("missing", ["UNSHELVE_DOMAIN", "UNSHELVE_DB_PASSWORD"])
def test_compose_stops_without_the_domain_or_the_password(tmp_path: Path, missing: str) -> None:
    variables = {"UNSHELVE_DOMAIN": DOMAIN, "UNSHELVE_DB_PASSWORD": PASSWORD}
    del variables[missing]

    done = _config(tmp_path, "compose.prod.yaml", **variables)

    assert done.returncode != 0
    assert f"set {missing} in .env, see docs/quickstart.md" in done.stderr


def test_each_service_rotates_its_log(tmp_path: Path) -> None:
    services = _services(tmp_path)

    for name, service in services.items():
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "5"},
        }, name


def test_caddy_keeps_its_certificates_and_only_reads_the_caddyfile(tmp_path: Path) -> None:
    caddy = _services(tmp_path)["caddy"]

    mounts = {volume["target"]: volume for volume in caddy["volumes"]}

    assert mounts["/data"]["type"] == "volume"
    assert mounts["/data"]["source"] == "caddy-data"
    assert mounts["/etc/caddy/Caddyfile"]["source"].endswith("deploy/Caddyfile")
    assert mounts["/etc/caddy/Caddyfile"]["read_only"] is True
    assert caddy["depends_on"]["web"]["condition"] == "service_healthy"


def test_the_local_stack_opens_each_port_to_this_computer_only(tmp_path: Path) -> None:
    # The stack of A in the quickstart: no production file, no domain, no password.
    done = _config(tmp_path, "compose.yaml")
    assert done.returncode == 0, done.stderr
    services: dict[str, Any] = json.loads(done.stdout)["services"]

    published = [port for service in services.values() for port in _published(service)]

    assert published
    assert {host for host, _, _ in published} == {"127.0.0.1"}
