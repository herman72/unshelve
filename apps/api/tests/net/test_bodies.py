"""Reading a body: decoded, and never longer than the limit. No network: the chunks come from
a list, as a server would send them."""

import gzip
import os
import time
import tracemalloc
import zlib
from collections.abc import AsyncIterator, Iterable

import pytest

from unshelve.net.bodies import BodyTooLargeError, BrokenBodyError, UnknownEncodingError, read_body

MEGABYTE = 1024 * 1024


class Chunks:
    """Chunks of a body, and how many of them were read."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self.read = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        try:
            chunk = next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None
        self.read += 1
        return chunk


def _pieces(data: bytes, size: int = 1000) -> list[bytes]:
    return [data[start : start + size] for start in range(0, len(data), size)]


def _gzip_of_zeros(total: int) -> bytes:
    """The gzip form of ``total`` zero bytes, made piece by piece: the zeros never exist as a
    whole in memory."""
    packer = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    zeros = bytes(MEGABYTE)
    parts = [packer.compress(zeros) for _ in range(total // MEGABYTE)]
    return b"".join([*parts, packer.flush()])


async def test_a_plain_body_comes_back_as_it_is() -> None:
    assert await read_body(Chunks([b"hel", b"lo"]), encoding=None, max_bytes=5) == b"hello"


async def test_a_plain_body_over_the_limit_stops_the_reading_early() -> None:
    chunks = Chunks([b"x" * 1000] * 100)

    with pytest.raises(BodyTooLargeError):
        await read_body(chunks, encoding="identity", max_bytes=5000)
    assert chunks.read == 6  # not 100


@pytest.mark.parametrize("encoding", ["gzip", "x-gzip", "GZIP"])
async def test_a_gzip_body_is_decompressed(encoding: str) -> None:
    text = "سلام, a text with Persian letters. " * 200
    packed = gzip.compress(text.encode())

    body = await read_body(Chunks(_pieces(packed)), encoding=encoding, max_bytes=100_000)

    assert body.decode() == text


async def test_a_gzip_bomb_stops_at_the_limit_with_little_memory() -> None:
    bomb = _gzip_of_zeros(50 * MEGABYTE)
    assert len(bomb) < 100_000  # 50 MB of zeros become less than 100 kB

    tracemalloc.start()
    started = time.perf_counter()
    try:
        # The whole bomb in one piece, as one read from a fast server can give it.
        with pytest.raises(BodyTooLargeError):
            await read_body(Chunks([bomb]), encoding="gzip", max_bytes=MEGABYTE)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # The 50 MB never exist: the reading stops after the first megabyte.
    assert peak < 5 * MEGABYTE
    assert time.perf_counter() - started < 1.0


async def test_a_compressed_body_over_the_limit_is_too_large_before_it_is_decompressed() -> None:
    noise = gzip.compress(os.urandom(5000))  # random bytes do not get smaller

    with pytest.raises(BodyTooLargeError):
        await read_body(Chunks(_pieces(noise)), encoding="gzip", max_bytes=4000)


@pytest.mark.parametrize(
    "packed",
    [
        pytest.param(zlib.compress(b"hello deflate"), id="zlib wrapper, as the standard says"),
        pytest.param(zlib.compress(b"hello deflate")[2:-4], id="raw deflate, as some servers send"),
    ],
)
async def test_a_deflate_body_is_decompressed_in_both_forms(packed: bytes) -> None:
    body = await read_body(Chunks(_pieces(packed, 3)), encoding="deflate", max_bytes=100)

    assert body == b"hello deflate"


async def test_a_gzip_body_of_two_members_is_read_to_the_end() -> None:
    packed = gzip.compress(b"first, ") + gzip.compress(b"second")

    assert await read_body(Chunks([packed]), encoding="gzip", max_bytes=100) == b"first, second"


async def test_a_gzip_body_that_ends_too_early_is_broken() -> None:
    packed = gzip.compress(b"a whole text " * 100)

    with pytest.raises(BrokenBodyError):
        await read_body(Chunks([packed[: len(packed) // 2]]), encoding="gzip", max_bytes=10_000)


async def test_bytes_that_are_not_gzip_are_a_broken_body_and_not_a_crash() -> None:
    with pytest.raises(BrokenBodyError):
        await read_body(Chunks([b"this is not gzip"]), encoding="gzip", max_bytes=100)


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "gzip, gzip"])
async def test_an_encoding_that_we_did_not_ask_for_is_unknown(encoding: str) -> None:
    with pytest.raises(UnknownEncodingError):
        await read_body(Chunks([b"data"]), encoding=encoding, max_bytes=100)


async def test_an_empty_gzip_body_is_an_empty_body() -> None:
    # Some servers name an encoding also for a body without bytes.
    assert await read_body(Chunks([]), encoding="gzip", max_bytes=100) == b""


async def test_a_compressed_body_that_never_ends_stops_at_the_limit() -> None:
    # Each "full flush" adds an empty block of 5 bytes, and no text. Without a limit on the
    # bytes that arrive, such a body is read until the time limit.
    packer = zlib.compressobj(6, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    empty_blocks = [packer.compress(b"") + packer.flush(zlib.Z_FULL_FLUSH) for _ in range(2000)]

    with pytest.raises(BodyTooLargeError):
        await read_body(Chunks(empty_blocks), encoding="gzip", max_bytes=5000)
