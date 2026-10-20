"""Snippets: a short passage of an item that shows why search found it (docs/learn/38).

The first source that has something wins:

* **text**: the words of the query in the saved text, marked. PostgreSQL's ``ts_headline``
  picks the pieces around them. It reads the text as full text search does, so "keep" marks
  "keeps".
* **summary**: the words of the query in the summary, marked, when the text has none of them.
* **passage**: the piece of the text that is closest in meaning to the query, for an item that
  only the vector retriever found. No word is marked: the match is the meaning.

An item that only the trigram retriever found (by its title), or whose closest vector is its
card, has no snippet: the title and the summary already show the reason.

A snippet is plain text in parts, each part marked as a hit or not. It is never HTML: the text
came from the internet, so the web app shows each part as text, and nothing in the text can
add a mark of its own.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import ChunkKind
from unshelve.db.models import Chunk, Item, ItemContent
from unshelve.search.hybrid import Hit

# ts_headline puts these around each word of the query. They are characters of the private use
# area of Unicode, and they are removed from the text first, so each one comes from ts_headline.
HIT_START, HIT_END = chr(0xE000), chr(0xE001)
# At most two pieces of at most 20 words each, around the words of the query.
HEADLINE_OPTIONS = (
    f'StartSel="{HIT_START}", StopSel="{HIT_END}", '
    'MaxWords=20, MinWords=8, MaxFragments=2, FragmentDelimiter=" … "'
)
# The index of the text reads only its start (db.models.CONTENT_FTS): the snippet reads the same.
CONTENT_LIMIT = 100_000
# A passage shows at most this many words of its piece of the text.
PASSAGE_WORDS = 30

_MARKS = re.compile(f"[{HIT_START}{HIT_END}]")
_SPACES = re.compile(r"\s+")


class Source(StrEnum):
    TEXT = "text"
    SUMMARY = "summary"
    PASSAGE = "passage"


@dataclass(frozen=True, slots=True)
class Part:
    text: str
    hit: bool  # a word of the query, or another form of it


@dataclass(frozen=True, slots=True)
class Snippet:
    source: Source
    parts: tuple[Part, ...]


def parts_of_headline(headline: str) -> tuple[Part, ...]:
    """The parts of a ``ts_headline`` result: between a start and an end mark is a hit.

    Line breaks and runs of spaces become one space, so a passage reads as one line.
    """
    pieces = _MARKS.split(_SPACES.sub(" ", headline).strip())
    # The marks come in pairs, so the pieces alternate: outside, hit, outside, hit, ...
    return tuple(
        Part(text=piece, hit=index % 2 == 1) for index, piece in enumerate(pieces) if piece
    )


def passage_parts(text: str, words: int = PASSAGE_WORDS) -> tuple[Part, ...]:
    """The first ``words`` words of a piece of the text, with "…" when the piece goes on."""
    found = text.split()
    shown = " ".join(found[:words]) + (" …" if len(found) > words else "")
    return (Part(text=shown, hit=False),) if shown else ()


async def _from_words(
    session: AsyncSession, owner_id: int, query: str, item_ids: Sequence[int]
) -> dict[int, Snippet]:
    words = sa.func.websearch_to_tsquery("english", sa.func.u_norm(query))
    marks = HIT_START + HIT_END
    text = sa.func.translate(sa.func.left(ItemContent.text, CONTENT_LIMIT), marks, "")
    summary = sa.func.translate(sa.func.coalesce(Item.summary, ""), marks, "")
    in_text = ItemContent.fts.bool_op("@@")(words)
    in_summary = sa.func.to_tsvector(
        "english", sa.func.u_norm(sa.func.coalesce(Item.summary, ""))
    ).bool_op("@@")(words)
    # The first source that has a word of the query wins: the order of the WHEN clauses. CASE
    # runs ts_headline only for that source: it reads a whole text.
    source = sa.case((in_text, Source.TEXT.value), (in_summary, Source.SUMMARY.value))
    headline = sa.case(
        (in_text, sa.func.ts_headline("english", text, words, HEADLINE_OPTIONS)),
        (in_summary, sa.func.ts_headline("english", summary, words, HEADLINE_OPTIONS)),
    )
    rows = await session.execute(
        sa.select(Item.id, source.label("source"), headline.label("headline"))
        .outerjoin(ItemContent, ItemContent.item_id == Item.id)
        .where(Item.id.in_(item_ids), Item.owner_id == owner_id)
    )
    return {
        item_id: Snippet(Source(found_in), parts_of_headline(found))
        for item_id, found_in, found in rows
        if found is not None
    }


async def _closest_passages(
    session: AsyncSession,
    owner_id: int,
    item_ids: Sequence[int],
    set_id: int,
    query_vector: Sequence[float],
) -> dict[int, Snippet]:
    distance = Chunk.embedding.cosine_distance(list(query_vector))
    rows = await session.execute(
        # For each item, its closest vector: DISTINCT ON keeps the first row of each item.
        sa.select(Chunk.item_id, Chunk.kind, Chunk.text)
        .where(
            Chunk.item_id.in_(item_ids),
            Chunk.owner_id == owner_id,
            Chunk.embedding_set_id == set_id,
            Chunk.dim == len(query_vector),
        )
        .order_by(Chunk.item_id, distance, Chunk.ord)
        .ext(distinct_on(Chunk.item_id))
    )
    snippets: dict[int, Snippet] = {}
    for item_id, kind, text in rows:
        # When the card is the closest, the summary already shows the reason.
        parts = passage_parts(text) if kind == ChunkKind.BODY else ()
        if parts:
            snippets[item_id] = Snippet(Source.PASSAGE, parts)
    return snippets


async def snippets(
    session: AsyncSession,
    owner_id: int,
    query: str,
    hits: Sequence[Hit],
    *,
    set_id: int | None = None,
    query_vector: Sequence[float] | None = None,
) -> dict[int, Snippet]:
    """The snippet of each hit that has one, by item id. Give it the hits of one page only:
    each snippet reads the text of its item."""
    found_by_words = [hit.item_id for hit in hits if hit.ranks.full_text is not None]
    found = await _from_words(session, owner_id, query, found_by_words) if found_by_words else {}
    found_by_meaning = [
        hit.item_id for hit in hits if hit.ranks.vector is not None and hit.item_id not in found
    ]
    if found_by_meaning and set_id is not None and query_vector is not None:
        found |= await _closest_passages(session, owner_id, found_by_meaning, set_id, query_vector)
    return found
