"""Helpers for the tests of the deploy files: Compose, and the image of the database."""

import os
import shutil
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[4]
# A plain word, so that no scanner takes it for a real secret.
TEST_PASSWORD = "unshelvetest"  # noqa: S105 - a made-up password for the tests


def _postgres_image() -> str:
    """The image of the database and of its backups, as deploy/compose.yaml names it."""
    compose = yaml.safe_load((REPO / "deploy" / "compose.yaml").read_text(encoding="utf-8"))
    image = compose.get("x-postgres", {}).get("image")
    assert isinstance(image, str), "deploy/compose.yaml names no image in x-postgres"
    return image


POSTGRES_IMAGE = _postgres_image()


def docker_available(*command: str) -> bool:
    """Whether `docker <command>` works on this computer."""
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(  # noqa: S603 - docker with the arguments of a test
        ["docker", *command],  # noqa: S607 - the docker of this computer
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def compose_config(
    tmp_path: Path, file: str, *, dotenv: str | None = None, **variables: str
) -> subprocess.CompletedProcess[str]:
    """`docker compose config` of `file` in a copy of the stack, with only `variables` set.

    The copy has no `.env` unless a test gives its text as `dotenv`, so neither the settings nor
    the secrets of the developer reach a test, and the shell of the developer gives no UNSHELVE_*
    variable either.
    """
    copy = tmp_path / "repo"
    if not copy.exists():
        shutil.copytree(REPO / "deploy", copy / "deploy")
        for name in ("compose.yaml", "compose.prod.yaml"):
            shutil.copy(REPO / name, copy / name)
    if dotenv is not None:
        (copy / ".env").write_text(dotenv, encoding="utf-8")
    keep = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")
    environment = {name: os.environ[name] for name in keep if name in os.environ}
    return subprocess.run(  # noqa: S603 - docker with arguments of this test
        ["docker", "compose", "-f", str(copy / file), "config", "--format", "json"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=environment | variables,
    )
