"""The blob store on disk. Each test name is one rule."""

import hashlib
import os
from pathlib import Path

import pytest

from unshelve.blobs import FileBlobStore
from unshelve.contracts.protocols import BlobStore
from unshelve.testing.conformance import BlobStoreConformance


class TestFileBlobStore(BlobStoreConformance):
    @pytest.fixture
    def blobs(self, tmp_path: Path) -> BlobStore:
        return FileBlobStore(tmp_path)


def _temporary_files(root: Path) -> list[Path]:
    return list(root.rglob(".part-*"))


async def test_a_blob_is_a_file_in_two_levels_of_folders(tmp_path: Path) -> None:
    key = await FileBlobStore(tmp_path).put(b"%PDF-1.7 made-up", "application/pdf")

    assert (tmp_path / key[:2] / key[2:4] / key).read_bytes() == b"%PDF-1.7 made-up"


async def test_a_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    await FileBlobStore(tmp_path).put(b"made-up bytes", "application/octet-stream")

    assert _temporary_files(tmp_path) == []


async def test_a_failed_write_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_rename(source: str, target: Path) -> None:
        raise OSError("the disk is full (made up)")

    monkeypatch.setattr(os, "replace", no_rename)
    data = b"made-up bytes"

    with pytest.raises(OSError, match="disk is full"):
        await FileBlobStore(tmp_path).put(data, "application/octet-stream")

    # No half file under the key, and no temporary file: a reader finds nothing.
    assert _temporary_files(tmp_path) == []
    with pytest.raises(KeyError):
        await FileBlobStore(tmp_path).get(hashlib.sha256(data).hexdigest())


async def test_the_same_bytes_are_written_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = FileBlobStore(tmp_path)
    await blobs.put(b"made-up bytes", "application/octet-stream")
    writes: list[str] = []
    real_replace = os.replace

    def counting(source: str, target: Path) -> None:
        writes.append(source)
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", counting)

    await blobs.put(b"made-up bytes", "application/octet-stream")

    assert writes == []


@pytest.mark.parametrize(
    "key",
    ["../../etc/passwd", "/etc/passwd", "A" * 64, "0" * 63, "0" * 64 + "/x", ""],
)
async def test_a_text_that_is_no_key_finds_nothing(tmp_path: Path, key: str) -> None:
    with pytest.raises(KeyError):
        await FileBlobStore(tmp_path).get(key)
