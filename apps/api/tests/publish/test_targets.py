"""The targets of the public feed. Each test name is one rule."""

import errno
import os
from pathlib import Path

import pytest

from unshelve.config import Settings
from unshelve.contracts.errors import PublishError
from unshelve.contracts.protocols import FeedTarget
from unshelve.publish.targets import FileTarget, build_feed_target
from unshelve.testing.conformance import FeedTargetConformance

FEED = b'{\n  "version": 1,\n  "items": []\n}\n'


class TestFileTarget(FeedTargetConformance):
    @pytest.fixture
    def target(self, tmp_path: Path) -> FeedTarget:
        return FileTarget(tmp_path / "feed.json")


def _names(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


async def test_a_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    await FileTarget(tmp_path / "feed.json").publish(FEED)

    assert _names(tmp_path) == ["feed.json"]


async def test_a_failed_write_keeps_the_old_feed_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = FileTarget(tmp_path / "feed.json")
    await target.publish(FEED)

    def full_disk(self: Path, target: Path) -> Path:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Path, "replace", full_disk)
    with pytest.raises(PublishError, match="No space left on device") as raised:
        await target.publish(b'{"version": 1, "items": [{"id": "new"}]}\n')

    assert raised.value.retryable is True  # a full disk can pass
    assert (tmp_path / "feed.json").read_bytes() == FEED
    assert _names(tmp_path) == ["feed.json"]


async def test_the_feed_can_be_read_by_other_users(tmp_path: Path) -> None:
    await FileTarget(tmp_path / "feed.json").publish(FEED)

    assert (tmp_path / "feed.json").stat().st_mode & 0o777 == 0o644


async def test_the_folders_of_the_feed_are_made(tmp_path: Path) -> None:
    path = tmp_path / "site" / "public" / "feed.json"

    await FileTarget(path).publish(FEED)

    assert path.read_bytes() == FEED


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write each folder")
async def test_a_folder_that_cannot_be_written_is_a_lasting_fault(tmp_path: Path) -> None:
    folder = tmp_path / "read-only"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        with pytest.raises(PublishError, match="cannot write the feed") as raised:
            await FileTarget(folder / "feed.json").publish(FEED)
    finally:
        folder.chmod(0o700)

    assert raised.value.retryable is False


def test_the_settings_choose_the_target(tmp_path: Path) -> None:
    assert build_feed_target(Settings(env="test")) is None
    target = build_feed_target(
        Settings(env="test", publish_target="file", publish_file=tmp_path / "feed.json")
    )
    assert isinstance(target, FileTarget)
