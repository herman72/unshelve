"""An overview with citations, on the made-up corpus. Each test name is one rule.

The corpus has chunks with the text "piece N of item X": the fake model quotes the first
sentence of each of the first three sources, so each quote is the whole text of a chunk.
"""

from collections.abc import Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.search.corpus import QUEUES, SAVED, Corpus, TopicEmbedder, seed
from unshelve.contracts.dto import CitedAnswer, CitedSegment, Quote, SourceText
from unshelve.contracts.enums import ChunkKind, ItemStatus
from unshelve.contracts.errors import ProviderError
from unshelve.db.models import Chunk, Item
from unshelve.search import overview
from unshelve.search.hybrid import VectorSearch
from unshelve.search.overview import Segment, plain_text
from unshelve.search.retrievers import NO_FILTERS, Filters
from unshelve.testing.fakes import FakeLLM, FakeLLMCall

# --- The text of the model, without a database ----------------------------------------------


def test_links_and_images_keep_their_words_and_lose_their_addresses() -> None:
    written = (
        "See ![a pixel](https://evil.example/p.png?d=1) and [the page](https://evil.example/)."
    )

    assert plain_text(written) == "See a pixel and the page."


def test_a_web_address_goes_also_as_plain_text() -> None:
    written = "Go to https://evil.example/x?y=1 or <https://evil.example/z> now."

    assert plain_text(written) == "Go to or now."


def test_the_second_form_of_a_markdown_image_loses_its_address() -> None:
    # Markdown has a second form of an image: ![x][1], with "[1]: address" on its own line. A
    # filter of the first form alone lets it pass; without its address, it loads nothing.
    written = "See ![x][1].\n\n[1]: https://evil.example/p.png?d=1"

    assert plain_text(written) == "See ![x][1].\n\n[1]: "


def test_plain_text_stays_as_it_is() -> None:
    written = "A plain sentence, with (brackets), [notes], a<b and an address-like word.com."

    assert plain_text(written) == written


# --- Overviews of the corpus ----------------------------------------------------------------


@pytest.fixture
async def corpus(db: AsyncSession) -> Corpus:
    return await seed(db)


class Seen:
    """Keeps the sources that the fake model read, and lets the fake answer as it does."""

    def __init__(self) -> None:
        self.sources: list[SourceText] = []

    def __call__(self, call: FakeLLMCall, sources: Sequence[SourceText]) -> CitedAnswer | None:
        self.sources.extend(sources)
        return None


async def _overview(
    db: AsyncSession,
    corpus: Corpus,
    question: str = "postgres",
    llm: FakeLLM | None = None,
    *,
    filters: Filters = NO_FILTERS,
    limit: int = 12,
) -> overview.Overview:
    return await overview.overview(
        db, TopicEmbedder(), llm or FakeLLM(), corpus.owner_id, question, filters, limit=limit
    )


def _citations(result: overview.Overview) -> list[overview.Citation]:
    return [citation for segment in result.segments for citation in segment.citations]


async def test_every_citation_maps_to_a_retrieved_chunk(db: AsyncSession, corpus: Corpus) -> None:
    seen = Seen()

    result = await _overview(db, corpus, llm=FakeLLM(cited_responder=seen))

    citations = _citations(result)
    assert len(citations) == 3  # the fake quotes the first three sources
    sent = [source.text for source in seen.sources]
    for citation in citations:
        chunk = await db.get(Chunk, citation.chunk_id)
        assert chunk is not None
        assert chunk.item_id == citation.item_id
        assert chunk.embedding_set_id == corpus.set_id  # the active set
        assert citation.quote in chunk.text
        assert chunk.text in sent  # the model read it
        assert citation.item_id in result.item_ids


async def test_the_model_reads_each_card_then_the_pieces_in_the_order_of_search(
    db: AsyncSession, corpus: Corpus
) -> None:
    seen = Seen()

    result = await _overview(db, corpus, llm=FakeLLM(cited_responder=seen))

    queue, pgvector, rust = corpus["queue"], corpus["pgvector"], corpus["rust"]
    assert [(source.title, source.context, source.text) for source in seen.sources] == [
        ("Postgres as a job queue", "Saved item 1: its card", f"piece 0 of item {queue}"),
        ("How pgvector stores vectors", "Saved item 2: its card", f"piece 0 of item {pgvector}"),
        (
            "How pgvector stores vectors",
            "Saved item 2: piece 2 of its text",  # its position 1, counted from 1
            f"piece 1 of item {pgvector}",
        ),
        ("Ownership in Rust explained", "Saved item 3: its card", f"piece 0 of item {rust}"),
    ]
    # The weekend notes are a hit too, but they have no chunk: nothing of them can be cited.
    assert result.item_ids == (queue, pgvector, rust)


# Three distances from the question "worker" (queues): the closest, a middle one, the farthest.
CLOSE, MIDDLE, FAR = QUEUES, (0.5, 0.0, 1.0), (0.0, 1.0, 0.0)


async def _long_read(
    db: AsyncSession, corpus: Corpus, pieces: list[tuple[str, tuple[float, ...]]]
) -> list[tuple[str, str]]:
    """Save an item with a card and these pieces (position 0, 1, ...), ask "worker", and return
    (context, text) of each of its passages that the model read."""
    item = Item(
        owner_id=corpus.owner_id,
        title="A long read about workers",
        first_captured_at=SAVED,
        last_captured_at=SAVED,
    )
    db.add(item)
    await db.flush()
    chunks = [(ChunkKind.CARD, 0, "The card of the long read.", CLOSE)]
    chunks += [(ChunkKind.BODY, n, text, vector) for n, (text, vector) in enumerate(pieces)]
    db.add_all(
        Chunk(
            owner_id=corpus.owner_id,
            item_id=item.id,
            embedding_set_id=corpus.set_id,
            kind=kind,
            ord=position,
            text=text,
            dim=3,
            embedding=list(vector),
        )
        for kind, position, text, vector in chunks
    )
    await db.flush()
    seen = Seen()
    await _overview(db, corpus, "worker", llm=FakeLLM(cited_responder=seen))
    return [
        (source.context.split(": ", 1)[1], source.text)
        for source in seen.sources
        if source.context is not None and source.title == "A long read about workers"
    ]


async def test_a_long_text_gives_its_card_then_its_best_pieces_in_the_order_of_the_text(
    db: AsyncSession, corpus: Corpus
) -> None:
    # The closest piece is the first one, at the position of the card; the piece with the word
    # of the question is the last one. The rank says "the word, then the closest", the text
    # says the other way round.
    read = await _long_read(
        db,
        corpus,
        [
            ("Queues hold jobs.", CLOSE),
            ("Bread wants time.", MIDDLE),
            ("Rust has owners.", MIDDLE),
            ("A worker takes a lease.", FAR),
        ],
    )

    # The model counts the pieces from 1: the position 0 is "piece 1".
    assert read == [
        ("its card", "The card of the long read."),
        ("piece 1 of its text", "Queues hold jobs."),
        ("piece 4 of its text", "A worker takes a lease."),
    ]


async def test_the_words_of_the_question_count_first_then_the_meaning(
    db: AsyncSession, corpus: Corpus
) -> None:
    # The piece with the word is the farthest in meaning, and the closest piece is the last
    # one: by meaning alone, or by position alone, one of them would be left out.
    read = await _long_read(
        db,
        corpus,
        [
            ("A worker takes a lease.", FAR),
            ("Bread wants time.", MIDDLE),
            ("Rust has owners.", MIDDLE),
            ("Queues hold jobs.", CLOSE),
        ],
    )

    assert [text for _, text in read] == [
        "The card of the long read.",
        "A worker takes a lease.",
        "Queues hold jobs.",
    ]


async def test_the_limit_counts_the_items(db: AsyncSession, corpus: Corpus) -> None:
    seen = Seen()

    result = await _overview(db, corpus, llm=FakeLLM(cited_responder=seen), limit=1)

    assert result.item_ids == (corpus["queue"],)
    assert {source.title for source in seen.sources} == {"Postgres as a job queue"}


async def test_without_a_passage_no_model_is_asked(db: AsyncSession, corpus: Corpus) -> None:
    llm = FakeLLM()
    nothing = Filters(statuses=frozenset({ItemStatus.DONE}))  # no item of the corpus is done

    result = await _overview(db, corpus, llm=llm, filters=nothing)

    assert result == overview.Overview(segments=(), item_ids=(), vector_search=VectorSearch.USED)
    assert llm.calls == []


async def test_a_citation_outside_the_passages_is_dropped(db: AsyncSession, corpus: Corpus) -> None:
    answer = CitedAnswer(
        segments=[
            CitedSegment(
                text="Workers share a table. ",
                quotes=[Quote(source=99, start=0, end=4, text="fake")],
            ),
            CitedSegment(
                text="Leases end.", quotes=[Quote(source=0, start=5, end=999, text="far")]
            ),
        ]
    )

    result = await _overview(db, corpus, llm=FakeLLM(cited_responder=lambda call, s: answer))

    # The two parts have no citation left, so they are one text of the model.
    assert result.segments == (Segment("Workers share a table. Leases end."),)


async def test_the_text_of_the_model_loses_its_links_and_images(
    db: AsyncSession, corpus: Corpus
) -> None:
    answer = CitedAnswer(
        segments=[CitedSegment(text="See ![x](https://evil.example/p.png) [now](https://e.x).")]
    )

    result = await _overview(db, corpus, llm=FakeLLM(cited_responder=lambda call, s: answer))

    assert result.segments == (Segment("See x now."),)


async def test_no_transaction_is_open_while_the_model_writes(
    db: AsyncSession, corpus: Corpus
) -> None:
    open_during_call: list[bool] = []

    def watch(call: FakeLLMCall, sources: Sequence[SourceText]) -> None:
        open_during_call.append(db.in_transaction())

    await _overview(db, corpus, llm=FakeLLM(cited_responder=watch))

    assert open_during_call == [False]


async def test_a_failing_embedder_still_gives_an_overview_from_the_words(
    db: AsyncSession, corpus: Corpus
) -> None:
    down = TopicEmbedder(error=ProviderError("voyage: 503", retryable=True))

    result = await overview.overview(db, down, FakeLLM(), corpus.owner_id, "postgres")

    assert result.vector_search is VectorSearch.FAILED
    # Full text and trigram: the job queue, pgvector and the weekend notes (no chunk).
    assert result.item_ids == (corpus["queue"], corpus["pgvector"])
    assert result.answer is not None


async def test_only_the_chunks_of_the_active_set_are_read(db: AsyncSession, corpus: Corpus) -> None:
    # After a change of model, the job queue has a card and a piece in the old set too. The
    # old piece is the closest to the question; the overview reads the active set only.
    db.add_all(
        Chunk(
            owner_id=corpus.owner_id,
            item_id=corpus["queue"],
            embedding_set_id=corpus.other_set_id,
            kind=kind,
            ord=0,
            text=text,
            dim=3,
            embedding=list(QUEUES),
        )
        for kind, text in [(ChunkKind.CARD, "An old card."), (ChunkKind.BODY, "An old piece.")]
    )
    await db.flush()
    seen = Seen()

    await _overview(db, corpus, llm=FakeLLM(cited_responder=seen))

    read = [source.text for source in seen.sources]
    assert "An old card." not in read
    assert "An old piece." not in read
