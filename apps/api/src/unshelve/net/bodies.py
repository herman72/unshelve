"""Reading the body of a response: decoded, and never longer than a limit.

A server can compress a body (``Content-Encoding: gzip``). A small compressed body can hold
a very large text: 50 MB of zeros become less than 100 kB. This is a "gzip bomb". So the
limit counts the bytes two times: as they arrive, and as they come out of the decompressor.
The decompressor is asked for at most one byte more than the limit allows, so it never
makes the large text.
"""

import zlib
from collections.abc import AsyncIterable

# The encodings that the request offers ("Accept-Encoding"). The standard library reads both.
ACCEPT_ENCODING = "gzip, deflate"


class BodyTooLargeError(Exception):
    """The body, compressed or decompressed, is longer than the limit."""


class BrokenBodyError(Exception):
    """The body ends too early, or it is not in the encoding that the server named."""


class UnknownEncodingError(Exception):
    """The server used an encoding that the request did not offer."""


async def read_body(chunks: AsyncIterable[bytes], *, encoding: str | None, max_bytes: int) -> bytes:
    """The decoded body. Raises ``BodyTooLargeError`` as soon as it is longer than ``max_bytes``."""
    decoder = _decoder(encoding)
    received = 0
    body = bytearray()
    async for chunk in chunks:
        received += len(chunk)
        if received > max_bytes:
            raise BodyTooLargeError
        # One byte more than the limit allows is enough to know that the body is too large.
        body += decoder.feed(chunk, max_bytes - len(body) + 1)
        if len(body) > max_bytes:
            raise BodyTooLargeError
    decoder.end()
    return bytes(body)


class _Plain:
    def feed(self, data: bytes, limit: int) -> bytes:
        return data

    def end(self) -> None:
        return None


class _Decompressor:
    """gzip, or deflate. Each call gives at most ``limit`` bytes."""

    def __init__(self, wbits: int, *, raw_after_error: bool) -> None:
        self._wbits = wbits
        self._zlib = zlib.decompressobj(wbits)
        self._received = False
        # "deflate" must have a zlib wrapper, but some servers send raw deflate. Until the
        # first output, the input is kept, so that it can be read again as raw deflate.
        self._kept: bytearray | None = bytearray() if raw_after_error else None

    def feed(self, data: bytes, limit: int) -> bytes:
        self._received = self._received or bool(data)
        if self._kept is not None:
            self._kept += data
        try:
            out = self._decompress(data, limit)
        except zlib.error as error:
            if self._kept is None:
                raise BrokenBodyError from error
            kept, self._kept = bytes(self._kept), None
            self._wbits = -zlib.MAX_WBITS
            self._zlib = zlib.decompressobj(self._wbits)
            try:
                return self._decompress(kept, limit)
            except zlib.error as second_error:
                raise BrokenBodyError from second_error
        if out:
            self._kept = None
        return out

    def _decompress(self, data: bytes, limit: int) -> bytes:
        out = bytearray()
        while data and len(out) < limit:
            # The second argument is never 0 here: 0 would mean "no limit".
            out += self._zlib.decompress(data, limit - len(out))
            if self._zlib.unconsumed_tail:
                # The limit stopped the decompressor. The rest of the input waits here.
                data = self._zlib.unconsumed_tail
            elif self._zlib.eof and self._zlib.unused_data:
                # A gzip body can have more than one part ("member"). The next one follows.
                data = self._zlib.unused_data
                self._zlib = zlib.decompressobj(self._wbits)
            else:
                data = b""
        return bytes(out)

    def end(self) -> None:
        if self._received and not self._zlib.eof:
            raise BrokenBodyError


def _decoder(encoding: str | None) -> _Plain | _Decompressor:
    codings = [
        name.strip().lower()
        for name in (encoding or "").split(",")
        if name.strip().lower() not in ("", "identity")
    ]
    match codings:
        case []:
            return _Plain()
        case ["gzip" | "x-gzip"]:
            return _Decompressor(zlib.MAX_WBITS | 16, raw_after_error=False)
        case ["deflate"]:
            return _Decompressor(zlib.MAX_WBITS, raw_after_error=True)
        case _:
            raise UnknownEncodingError(encoding)
