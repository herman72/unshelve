"""The files of a saved message for the fetch step: downloaded once, then read from a copy.
A pretend source of files; a real database and a real blob store on disk."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.blobs import FileBlobStore
from unshelve.contracts.dto import MediaRef
from unshelve.contracts.errors import FetchError
from unshelve.db.models import Asset
from unshelve.pipeline.files import StoredFiles
from unshelve.testing.fakes import FakeFiles

Sessions = async_sessionmaker[AsyncSession]

PDF = b"%PDF-1.7 made-up bytes of a document"
DOCUMENT = MediaRef(
    kind="document",
    file_id="BQACAgMadeUpFileId",
    file_unique_id="AgADmadeup",
    mime="application/pdf",
    size_bytes=len(PDF),
    file_name="leases.pdf",
)


async def _owner(sessions: Sessions, telegram_user_id: int = 1) -> int:
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, telegram_user_id)
        await db.commit()
    return owner_id


async def _assets(sessions: Sessions) -> list[Asset]:
    async with sessions() as db:
        return list((await db.scalars(sa.select(Asset).order_by(Asset.id))).all())


@pytest.fixture
def source() -> FakeFiles:
    return FakeFiles({str(DOCUMENT.file_id): PDF})


async def test_a_file_is_downloaded_once_and_then_read_from_its_copy(
    committed_db: Sessions, source: FakeFiles, tmp_path: Path
) -> None:
    owner_id = await _owner(committed_db)
    files = StoredFiles(source, FileBlobStore(tmp_path), committed_db, owner_id)

    first = await files.read(DOCUMENT, max_bytes=1_000)
    second = await files.read(DOCUMENT, max_bytes=1_000)

    assert first == second == PDF
    assert source.read_ids == [DOCUMENT.file_id]  # one download
    [asset] = await _assets(committed_db)
    assert (asset.owner_id, asset.file_unique_id, asset.mime, asset.size_bytes) == (
        owner_id,
        "AgADmadeup",
        "application/pdf",
        len(PDF),
    )
    assert await FileBlobStore(tmp_path).get(asset.storage_key) == PDF


async def test_a_copy_is_read_also_without_a_source(
    committed_db: Sessions, source: FakeFiles, tmp_path: Path
) -> None:
    # Without a bot token there is no source; a file that was read before still comes.
    owner_id = await _owner(committed_db)
    await StoredFiles(source, FileBlobStore(tmp_path), committed_db, owner_id).read(
        DOCUMENT, max_bytes=1_000
    )

    without = StoredFiles(None, FileBlobStore(tmp_path), committed_db, owner_id)

    assert await without.read(DOCUMENT, max_bytes=1_000) == PDF


async def test_a_new_file_without_a_source_is_unsupported(
    committed_db: Sessions, tmp_path: Path
) -> None:
    owner_id = await _owner(committed_db)

    with pytest.raises(FetchError) as caught:
        await StoredFiles(None, FileBlobStore(tmp_path), committed_db, owner_id).read(
            DOCUMENT, max_bytes=1_000
        )

    assert (caught.value.reason, caught.value.url) == ("unsupported", "telegram file AgADmadeup")


async def test_a_copy_whose_file_is_gone_is_downloaded_again(
    committed_db: Sessions, source: FakeFiles, tmp_path: Path
) -> None:
    owner_id = await _owner(committed_db)
    await StoredFiles(source, FileBlobStore(tmp_path / "old"), committed_db, owner_id).read(
        DOCUMENT, max_bytes=1_000
    )

    # A new, empty volume: the row is there, the file is not.
    files = StoredFiles(source, FileBlobStore(tmp_path / "new"), committed_db, owner_id)

    assert await files.read(DOCUMENT, max_bytes=1_000) == PDF
    assert source.read_ids == [DOCUMENT.file_id, DOCUMENT.file_id]
    assert len(await _assets(committed_db)) == 1


async def test_a_copy_over_the_limit_of_the_reader_is_refused(
    committed_db: Sessions, source: FakeFiles, tmp_path: Path
) -> None:
    owner_id = await _owner(committed_db)
    files = StoredFiles(source, FileBlobStore(tmp_path), committed_db, owner_id)
    await files.read(DOCUMENT, max_bytes=1_000)

    with pytest.raises(FetchError) as caught:
        await files.read(DOCUMENT, max_bytes=10)

    assert caught.value.reason == "too_large"


async def test_the_copy_of_another_owner_is_not_used(
    committed_db: Sessions, source: FakeFiles, tmp_path: Path
) -> None:
    first = await _owner(committed_db, 1)
    second = await _owner(committed_db, 2)
    blobs = FileBlobStore(tmp_path)
    await StoredFiles(source, blobs, committed_db, first).read(DOCUMENT, max_bytes=1_000)

    await StoredFiles(source, blobs, committed_db, second).read(DOCUMENT, max_bytes=1_000)

    assert source.read_ids == [DOCUMENT.file_id, DOCUMENT.file_id]
    assert sorted(asset.owner_id for asset in await _assets(committed_db)) == [first, second]


async def test_a_failure_of_the_source_goes_up_unchanged_and_keeps_nothing(
    committed_db: Sessions, tmp_path: Path
) -> None:
    busy = FetchError("http_error", "telegram file AgADmadeup", status=429, retry_after=7)
    owner_id = await _owner(committed_db)
    files = StoredFiles(
        FakeFiles({str(DOCUMENT.file_id): busy}), FileBlobStore(tmp_path), committed_db, owner_id
    )

    with pytest.raises(FetchError) as caught:
        await files.read(DOCUMENT, max_bytes=1_000)

    assert caught.value is busy
    assert await _assets(committed_db) == []


async def test_two_files_with_the_same_bytes_share_one_copy(
    committed_db: Sessions, tmp_path: Path
) -> None:
    again = DOCUMENT.model_copy(update={"file_id": "BQACAgAgain", "file_unique_id": "AgADagain"})
    source = FakeFiles({str(DOCUMENT.file_id): PDF, "BQACAgAgain": PDF})
    owner_id = await _owner(committed_db)
    files = StoredFiles(source, FileBlobStore(tmp_path), committed_db, owner_id)

    assert await files.read(DOCUMENT, max_bytes=1_000) == PDF
    assert await files.read(again, max_bytes=1_000) == PDF

    [asset] = await _assets(committed_db)
    assert asset.file_unique_id == "AgADmadeup"  # the first one


# --- A file of an export: the importer copied it, and the media names the copy --------------


def _exported(key: str) -> MediaRef:
    return MediaRef(
        kind="document",
        local_path="files/leases.pdf",
        mime="application/pdf",
        file_name="leases.pdf",
        sha256=key,
    )


async def test_the_copy_that_the_media_names_is_read_without_a_source(
    committed_db: Sessions, tmp_path: Path
) -> None:
    blobs = FileBlobStore(tmp_path)
    key = await blobs.put(PDF, "application/pdf")
    owner_id = await _owner(committed_db)

    files = StoredFiles(None, blobs, committed_db, owner_id)

    assert await files.read(_exported(key), max_bytes=1_000) == PDF


async def test_an_empty_copy_is_an_empty_file(committed_db: Sessions, tmp_path: Path) -> None:
    blobs = FileBlobStore(tmp_path)
    key = await blobs.put(b"", "text/plain")
    owner_id = await _owner(committed_db)

    files = StoredFiles(None, blobs, committed_db, owner_id)

    assert await files.read(_exported(key), max_bytes=1_000) == b""


async def test_a_named_copy_over_the_limit_of_the_reader_is_refused(
    committed_db: Sessions, tmp_path: Path
) -> None:
    blobs = FileBlobStore(tmp_path)
    key = await blobs.put(PDF, "application/pdf")
    owner_id = await _owner(committed_db)

    with pytest.raises(FetchError) as caught:
        await StoredFiles(None, blobs, committed_db, owner_id).read(_exported(key), max_bytes=10)

    assert (caught.value.reason, caught.value.url) == ("too_large", f"exported file {key[:12]}")


async def test_a_named_copy_that_is_gone_cannot_be_read_without_a_source(
    committed_db: Sessions, tmp_path: Path
) -> None:
    # A new, empty volume: the export folder is gone, and Telegram has no id for this file.
    owner_id = await _owner(committed_db)
    files = StoredFiles(None, FileBlobStore(tmp_path), committed_db, owner_id)

    with pytest.raises(FetchError) as caught:
        await files.read(_exported("ab" * 32), max_bytes=1_000)

    assert (caught.value.reason, caught.value.url) == ("unsupported", "exported file abababababab")
