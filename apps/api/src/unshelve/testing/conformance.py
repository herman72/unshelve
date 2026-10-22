"""Conformance suites: the behaviour every implementation of a Protocol must have.

A fake and a real adapter subclass the same suite and provide the subject through a fixture:

    class TestVoyageEmbedder(EmbedderConformance):
        @pytest.fixture
        def embedder(self) -> Embedder:
            return VoyageEmbedder(...)

If the fake and the real thing pass the same tests, code tested against the fake can be
trusted against the real thing.
"""

import hashlib
import math
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from unshelve.contracts.dto import (
    BatchQuestion,
    Extracted,
    ImagePart,
    SourceRef,
    SourceText,
    TextPart,
)
from unshelve.contracts.errors import FetchError
from unshelve.contracts.protocols import (
    BatchProvider,
    BlobStore,
    CitingLLM,
    Embedder,
    ExtractContext,
    Extractor,
    FeedTarget,
    Fetcher,
    LLMProvider,
)

# A valid 1x1 PNG, for providers that check the image they are given.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.hypot(*a) * math.hypot(*b))


# --- Embedder ----------------------------------------------------------------------------

# A real model runs on GPUs, so the numbers of one text change a little from call to call,
# and with the other texts of a batch. Voyage, measured on 2026-10-15: a cosine of 0.998 or
# more for the same text, and 0.79 for two different short texts. So "the same vector" means
# "nearer than this", and the order of a batch is checked by the nearest vector.
SAME_TEXT = 0.99


class EmbedderConformance:
    @pytest.fixture
    def embedder(self) -> Embedder:
        raise NotImplementedError

    async def test_vectors_have_the_declared_dimension(self, embedder: Embedder) -> None:
        [vector] = await embedder.embed_documents(["a note about vector search"])
        assert len(vector) == embedder.spec.dim
        assert len(await embedder.embed_query("vector search")) == embedder.spec.dim

    async def test_same_text_gives_the_same_vector(self, embedder: Embedder) -> None:
        [first] = await embedder.embed_documents(["postgres indexes"])
        [second] = await embedder.embed_documents(["postgres indexes"])
        assert _cosine(first, second) > SAME_TEXT

    async def test_a_batch_keeps_order_and_length(self, embedder: Embedder) -> None:
        texts = ["alpha release notes", "beta testing guide", "gamma ray physics"]
        batch = await embedder.embed_documents(texts)
        assert len(batch) == len(texts)
        alone = [(await embedder.embed_documents([text]))[0] for text in texts]
        for place, vector in enumerate(batch):
            similarity = [_cosine(vector, other) for other in alone]
            # The vector at each place is the nearest to the text at that place.
            assert max(range(len(alone)), key=similarity.__getitem__) == place
            assert similarity[place] > SAME_TEXT

    async def test_an_empty_batch_gives_an_empty_list(self, embedder: Embedder) -> None:
        assert await embedder.embed_documents([]) == []

    async def test_related_texts_are_closer_than_unrelated_ones(self, embedder: Embedder) -> None:
        query = await embedder.embed_query("tuning a postgres vector index")
        related, unrelated = await embedder.embed_documents(
            ["postgres vector index tuning guide", "banana bread recipe with walnuts"]
        )
        assert _cosine(query, related) > _cosine(query, unrelated)

    async def test_persian_text_is_embedded(self, embedder: Embedder) -> None:
        [vector] = await embedder.embed_documents(["یادگیری ماشین و پایگاه داده"])
        assert len(vector) == embedder.spec.dim
        assert any(x != 0.0 for x in vector)


# --- Blob store --------------------------------------------------------------------------


class BlobStoreConformance:
    @pytest.fixture
    def blobs(self) -> BlobStore:
        raise NotImplementedError

    async def test_get_returns_what_put_stored(self, blobs: BlobStore) -> None:
        key = await blobs.put(b"\x00binary\xff", "application/octet-stream")
        assert await blobs.get(key) == b"\x00binary\xff"

    async def test_the_key_is_the_sha256_of_the_content(self, blobs: BlobStore) -> None:
        data = b"same bytes, same key"
        assert await blobs.put(data, "text/plain") == hashlib.sha256(data).hexdigest()

    async def test_storing_the_same_bytes_twice_is_harmless(self, blobs: BlobStore) -> None:
        first = await blobs.put(b"twice", "text/plain")
        second = await blobs.put(b"twice", "text/plain")
        assert first == second
        assert await blobs.get(first) == b"twice"

    async def test_an_unknown_key_raises_key_error(self, blobs: BlobStore) -> None:
        with pytest.raises(KeyError):
            await blobs.get("0" * 64)


# --- Fetcher -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FetcherCase:
    fetcher: Fetcher
    ok_url: str  # serves exactly b"hello" as text/plain
    missing_url: str  # answers 404
    large_url: str  # serves more than 1000 bytes


class FetcherConformance:
    @pytest.fixture
    def case(self) -> FetcherCase:
        raise NotImplementedError

    async def test_returns_the_body_and_the_final_url(self, case: FetcherCase) -> None:
        result = await case.fetcher.get(case.ok_url, max_bytes=1000, accept=["text/*"])
        assert result.body == b"hello"
        assert result.status == 200
        assert result.final_url

    async def test_a_missing_page_is_an_http_error(self, case: FetcherCase) -> None:
        with pytest.raises(FetchError) as caught:
            await case.fetcher.get(case.missing_url, max_bytes=1000, accept=[])
        assert caught.value.reason == "http_error"
        assert caught.value.status == 404
        assert not caught.value.retryable

    async def test_a_body_over_the_limit_is_an_error_not_a_truncation(
        self, case: FetcherCase
    ) -> None:
        with pytest.raises(FetchError) as caught:
            await case.fetcher.get(case.large_url, max_bytes=1000, accept=[])
        assert caught.value.reason == "too_large"

    async def test_an_unaccepted_content_type_is_unsupported(self, case: FetcherCase) -> None:
        with pytest.raises(FetchError) as caught:
            await case.fetcher.get(case.ok_url, max_bytes=1000, accept=["image/*"])
        assert caught.value.reason == "unsupported"
        # The type that was refused: a link to a page can answer with a PDF.
        assert caught.value.content_type == "text/plain"

    async def test_the_type_is_checked_before_the_size(self, case: FetcherCase) -> None:
        # A large PDF behind a link to a page is refused for its type, so that the PDF
        # extractor can ask for it with the larger limit of a file.
        with pytest.raises(FetchError) as caught:
            await case.fetcher.get(case.large_url, max_bytes=1000, accept=["image/*"])
        assert caught.value.reason == "unsupported"


# --- LLM provider ------------------------------------------------------------------------


class _Card(BaseModel):
    title: str
    tags: list[str]
    minutes: int


# Two sources for a cited answer: the first one answers the question, the second does not.
CITED_SOURCES = [
    SourceText(
        title="Postgres as a job queue",
        context="A saved article",
        text="Postgres can be a job queue. SKIP LOCKED lets workers share one table of jobs.",
    ),
    SourceText(title="Sourdough", text="Sourdough needs flour, water, salt and time."),
]


class CitingLLMConformance:
    @pytest.fixture
    def citing_llm(self) -> CitingLLM:
        raise NotImplementedError

    async def _cite(self, citing_llm: CitingLLM) -> list[tuple[int, int, int, str]]:
        result = await citing_llm.cite(
            task="conformance",
            prompt_version="1",
            system="Answer from the sources alone, and cite them.",
            sources=CITED_SOURCES,
            question="How can Postgres share jobs between workers?",
            max_output_tokens=400,
        )
        assert result.model
        assert result.usage.input_tokens > 0
        assert result.usage.output_tokens > 0
        assert "".join(segment.text for segment in result.value.segments).strip()
        return [
            (quote.source, quote.start, quote.end, quote.text)
            for segment in result.value.segments
            for quote in segment.quotes
        ]

    async def test_a_cited_answer_quotes_its_sources(self, citing_llm: CitingLLM) -> None:
        assert await self._cite(citing_llm)

    async def test_each_quote_is_a_part_of_its_source(self, citing_llm: CitingLLM) -> None:
        for source, start, end, text in await self._cite(citing_llm):
            assert 0 <= source < len(CITED_SOURCES)
            assert 0 <= start < end <= len(CITED_SOURCES[source].text)
            assert text == CITED_SOURCES[source].text[start:end]


class LLMProviderConformance:
    @pytest.fixture
    def llm(self) -> LLMProvider:
        raise NotImplementedError

    async def test_returns_an_instance_of_the_schema(self, llm: LLMProvider) -> None:
        result = await llm.generate(
            task="conformance",
            prompt_version="1",
            system="Describe the text as a card.",
            parts=[TextPart(text="Postgres can be a job queue with SKIP LOCKED and leases.")],
            schema=_Card,
            max_output_tokens=300,
        )
        assert isinstance(result.value, _Card)
        assert result.value.title
        assert result.model

    async def test_reports_token_usage(self, llm: LLMProvider) -> None:
        result = await llm.generate(
            task="conformance",
            prompt_version="1",
            system="Describe the text as a card.",
            parts=[TextPart(text="A short note.")],
            schema=_Card,
            max_output_tokens=300,
        )
        assert result.usage.input_tokens > 0
        assert result.usage.output_tokens > 0

    async def test_accepts_an_image(self, llm: LLMProvider) -> None:
        result = await llm.generate(
            task="conformance",
            prompt_version="1",
            system="Describe the image as a card.",
            parts=[TextPart(text="A screenshot."), ImagePart(data=TINY_PNG, mime="image/png")],
            schema=_Card,
            max_output_tokens=300,
        )
        assert isinstance(result.value, _Card)


# --- Batches of questions ------------------------------------------------------------------


class BatchProviderConformance:
    @pytest.fixture
    def batches(self) -> BatchProvider:
        raise NotImplementedError

    async def test_answers_each_question_by_its_id(self, batches: BatchProvider) -> None:
        questions = [
            BatchQuestion(
                custom_id=name,
                system="Describe the text as a card.",
                parts=[TextPart(text=text)],
                max_output_tokens=300,
            )
            for name, text in (
                ("q1", "Postgres can be a job queue with SKIP LOCKED."),
                ("q2", "A lease ends when its worker stops."),
            )
        ]
        batch_id = await batches.send(questions, _Card)
        assert await batches.ended(batch_id)

        answers = {answer.custom_id: answer async for answer in batches.answers(batch_id, _Card)}

        assert set(answers) == {"q1", "q2"}
        for answer in answers.values():
            assert answer.result is not None
            assert isinstance(answer.result.value, _Card)
            assert answer.result.model


# --- Extractor ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractorCase:
    extractor: Extractor
    ctx: ExtractContext
    matching: SourceRef  # a source this extractor is meant for
    unrelated: SourceRef  # a source it must leave to others


class ExtractorConformance:
    @pytest.fixture
    def case(self) -> ExtractorCase:
        raise NotImplementedError

    def test_declares_a_name_and_a_version(self, case: ExtractorCase) -> None:
        assert case.extractor.name
        assert case.extractor.version >= 1

    def test_claims_its_own_sources_and_no_others(self, case: ExtractorCase) -> None:
        assert case.extractor.match(case.matching) > 0
        assert case.extractor.match(case.unrelated) == 0

    async def test_extracts_text(self, case: ExtractorCase) -> None:
        extracted = await case.extractor.extract(case.matching, case.ctx)
        assert isinstance(extracted, Extracted)
        assert extracted.text.strip()


# --- Feed target -------------------------------------------------------------------------

_FEED = b'{\n  "version": 1,\n  "items": []\n}\n'
# Persian in UTF-8: a target must keep the bytes as they are, not text in another encoding.
_NEWER_FEED = '{\n  "version": 1,\n  "items": [{"title": "صف کارها"}]\n}\n'.encode()


class FeedTargetConformance:
    @pytest.fixture
    def target(self) -> FeedTarget:
        raise NotImplementedError

    async def test_a_new_target_holds_no_feed(self, target: FeedTarget) -> None:
        assert await target.read() is None

    async def test_read_returns_the_published_bytes(self, target: FeedTarget) -> None:
        published = await target.publish(_FEED)

        assert published.changed
        assert published.where
        assert await target.read() == _FEED

    async def test_the_same_bytes_again_change_nothing(self, target: FeedTarget) -> None:
        await target.publish(_FEED)

        assert not (await target.publish(_FEED)).changed
        assert await target.read() == _FEED

    async def test_new_bytes_replace_the_feed(self, target: FeedTarget) -> None:
        await target.publish(_FEED)

        assert (await target.publish(_NEWER_FEED)).changed
        assert await target.read() == _NEWER_FEED
