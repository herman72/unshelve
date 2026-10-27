"""The fixtures of the tests of the backup and the restore."""

from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from tests.deploy.backup_support import Place


@pytest.fixture
def place(tmp_path: Path, private_database_url: str) -> Place:
    folders = [tmp_path / name for name in ("backups", "blobs", "state")]
    for folder in folders:
        folder.mkdir()
    database = make_url(private_database_url).database
    assert database is not None
    return Place(database, *folders)
