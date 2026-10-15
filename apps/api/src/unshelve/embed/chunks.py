"""The pieces of a text, for the embedder (ADR 0007, ADR 0012).

Each item has one vector for its card: the title, the summary and the key points. A long text
also gets one vector for each piece of it, so that a search can find a part of the text that
the card does not name.

The rules:

- A text of at most ``MIN_BODY_CHARS`` characters gets no piece. Its card is enough.
- A piece has at most ``PIECE_CHARS`` characters. It ends after a paragraph if it can, else
  after a sentence or a line, else after a word. Only a word that is longer than a piece is
  cut inside.
- A text that needs more than ``MAX_PIECES`` pieces gets longer pieces, up to
  ``LONGEST_PIECE_CHARS``. If that is not enough, the end of the text gets no piece. The
  full-text search still reads all of it.
- Each piece knows where it is: ``text[piece.start : piece.end] == piece.text``.

The limits count characters, not tokens: the tokenizer of the provider is not here. A Persian
text has about 1.5 characters for each token, an English text about 3. So a piece of 4,000
characters is at most about 3,000 tokens, far below the 32,000 tokens that the model reads.
"""

import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

MIN_BODY_CHARS = 1_000
PIECE_CHARS = 1_500
LONGEST_PIECE_CHARS = 4_000
MAX_PIECES = 40

# A paragraph ends at an empty line.
_PARAGRAPH_BREAK = re.compile(r"\n[^\S\n]*\n\s*")
# A sentence ends with one of these marks before a space: U+061F is the Persian question
# mark, U+06D4 the full stop of Urdu. The end of a line is as good as the end of a sentence.
_SENTENCE_END = re.compile(r"[.!?\u061f\u06d4\u2026\u3002\uff01\uff1f](?=\s)|\n")


@dataclass(frozen=True)
class Piece:
    start: int
    end: int
    text: str


def card_text(title: str | None, summary: str | None, key_points: Sequence[str]) -> str:
    """The text of the card vector: the title, the summary and each key point, one a line."""
    parts = (title, summary, *key_points)
    return "\n".join(part.strip() for part in parts if part and part.strip())


def pieces_of(text: str) -> list[Piece]:
    """The pieces of a long text, in order. An empty list for a short text."""
    if len(text.strip()) <= MIN_BODY_CHARS:
        return []
    size = min(max(PIECE_CHARS, math.ceil(len(text) / MAX_PIECES)), LONGEST_PIECE_CHARS)
    spans = _pack(text, size)
    while len(spans) > MAX_PIECES and size < LONGEST_PIECE_CHARS:
        size = min(size + size // 4, LONGEST_PIECE_CHARS)
        spans = _pack(text, size)
    return [Piece(start=start, end=end, text=text[start:end]) for start, end in spans[:MAX_PIECES]]


def _pack(text: str, size: int) -> list[tuple[int, int]]:
    """Whole paragraphs, and the parts of the long ones, put together into pieces."""
    pieces: list[tuple[int, int]] = []
    for start, end in _parts(text, size):
        if pieces and end - pieces[-1][0] <= size:
            pieces[-1] = (pieces[-1][0], end)
        else:
            pieces.append((start, end))
    return pieces


def _parts(text: str, size: int) -> Iterator[tuple[int, int]]:
    """The paragraphs of the text. A paragraph that is longer than ``size`` comes in parts."""
    start = 0
    for found in _PARAGRAPH_BREAK.finditer(text):
        yield from _cut(text, start, found.start(), size)
        start = found.end()
    yield from _cut(text, start, len(text), size)


def _cut(text: str, start: int, end: int, size: int) -> Iterator[tuple[int, int]]:
    """``text[start:end]`` without the spaces around it, in parts of at most ``size``."""
    start, end = _strip(text, start, end)
    while end - start > size:
        cut = _cut_point(text, start, start + size)
        yield _strip(text, start, cut)
        start, end = _strip(text, cut, end)
    if start < end:
        yield start, end


def _cut_point(text: str, start: int, limit: int) -> int:
    """Where a part that starts at ``start`` ends: at most at ``limit``.

    After the last sentence in the second half of the part, or else after the last word.
    A word that fills the whole part is cut at ``limit``.
    """
    sentence = None
    for found in _SENTENCE_END.finditer(text, start, limit + 1):
        sentence = found.end()
    if sentence is not None and sentence - start > (limit - start) // 2:
        return sentence
    for index in range(limit, start, -1):
        if text[index].isspace():
            return index
    return limit


def _strip(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
