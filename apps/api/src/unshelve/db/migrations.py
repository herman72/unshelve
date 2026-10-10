"""Run Alembic migrations from code, so the ``unshelve migrate`` command needs no shell setup."""

from pathlib import Path

from alembic import command
from alembic.config import Config

# apps/api when the package is installed from the source tree (development and the image).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def alembic_config(database_url: str | None = None) -> Config:
    """Build the Alembic config. ``database_url`` overrides the configured database."""
    for candidate in (Path.cwd() / "alembic.ini", _PROJECT_ROOT / "alembic.ini"):
        if candidate.is_file():
            config = Config(str(candidate))
            if database_url is not None:
                # configparser treats % as interpolation, so escape it.
                config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
            return config
    msg = "alembic.ini not found; run from apps/api or install from the source tree"
    raise FileNotFoundError(msg)


def upgrade(revision: str = "head", *, database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(revision: str, *, database_url: str | None = None) -> None:
    command.downgrade(alembic_config(database_url), revision)
