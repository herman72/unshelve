"""The fakes pass the same conformance suites that the real adapters will."""

from enum import StrEnum

import pytest
from pydantic import BaseModel

from unshelve.contracts.dto import CitedAnswer, CitedSegment, Quote, SourceRef, SourceText, TextPart
from unshelve.contracts.protocols import (
    BlobStore,
    CitingLLM,
    Embedder,
    ExtractContext,
    LLMProvider,
)
from unshelve.testing.conformance import (
    BlobStoreConformance,
    CitingLLMConformance,
    EmbedderConformance,
    ExtractorCase,
    ExtractorConformance,
    FetcherCase,
    FetcherConformance,
    LLMProviderConformance,
)
from unshelve.testing.fakes import (
    EchoExtractor,
    FakeEmbedder,
    FakeFetcher,
    FakeLLM,
    FakeLLMCall,
    InMemoryBlobStore,
    synthesize,
)


class TestFakeEmbedder(EmbedderConformance):
    @pytest.fixture
    def embedder(self) -> Embedder:
        return FakeEmbedder()


class TestInMemoryBlobStore(BlobStoreConformance):
    @pytest.fixture
    def blobs(self) -> BlobStore:
        return InMemoryBlobStore()


class TestFakeFetcher(FetcherConformance):
    @pytest.fixture
    def case(self) -> FetcherCase:
        fetcher = FakeFetcher()
        fetcher.add("https://ok.test/", "hello", "text/plain")
        fetcher.add("https://large.test/", b"x" * 1001, "text/plain")
        return FetcherCase(
            fetcher, "https://ok.test/", "https://missing.test/", "https://large.test/"
        )


class TestFakeLLM(LLMProviderConformance, CitingLLMConformance):
    @pytest.fixture
    def llm(self) -> LLMProvider:
        return FakeLLM()

    @pytest.fixture
    def citing_llm(self) -> CitingLLM:
        return FakeLLM()


async def _cite(llm: FakeLLM, *texts: str) -> CitedAnswer:
    sources = [SourceText(title=f"Item {n}", text=text) for n, text in enumerate(texts, start=1)]
    result = await llm.cite(
        task="test",
        prompt_version="1",
        system="Answer.",
        sources=sources,
        question="What is it?",
        max_output_tokens=100,
    )
    return result.value


async def test_the_fake_quotes_the_first_sentence_of_the_first_three_sources() -> None:
    answer = await _cite(FakeLLM(), "  One. Two.", "Three? Four.", "Five", "Six.")

    assert answer.segments == [
        CitedSegment(text="Item 1: "),
        CitedSegment(text="One.", quotes=[Quote(source=0, start=2, end=6, text="One.")]),
        CitedSegment(text="Item 2: "),
        CitedSegment(text="Three?", quotes=[Quote(source=1, start=0, end=6, text="Three?")]),
        CitedSegment(text="Item 3: "),
        CitedSegment(text="Five", quotes=[Quote(source=2, start=0, end=4, text="Five")]),
    ]


async def test_the_fake_says_when_no_source_can_be_quoted() -> None:
    answer = await _cite(FakeLLM(), "   ")

    assert answer.segments == [CitedSegment(text="The sources do not answer the question.")]


async def test_a_scripted_cited_answer_comes_back_as_it_is() -> None:
    scripted = CitedAnswer(segments=[CitedSegment(text="See ![x](https://example.com/x.png).")])
    llm = FakeLLM(cited_responder=lambda call, sources: scripted)

    assert await _cite(llm, "One.") == scripted
    assert llm.calls[0].text == "What is it?\nOne."


class TestEchoExtractor(ExtractorConformance):
    @pytest.fixture
    def case(self) -> ExtractorCase:
        ctx = ExtractContext(fetcher=FakeFetcher(), blobs=InMemoryBlobStore(), llm=FakeLLM())
        return ExtractorCase(EchoExtractor(), ctx, SourceRef(text="a saved note"), SourceRef())


class _Kind(StrEnum):
    ARTICLE = "article"
    VIDEO = "video"


class _Point(BaseModel):
    text: str
    weight: float


class _Summary(BaseModel):
    summary: str
    tags: list[str]
    kind: _Kind
    minutes: int
    points: list[_Point]
    note: str | None


def test_synthesize_fills_nested_schemas_from_the_input() -> None:
    value = synthesize(_Summary, "Postgres queues need leases, not only SKIP LOCKED")

    assert value.summary.startswith("postgres queues need leases")
    assert value.tags == ["postgres", "queues", "need"]
    assert value.kind is _Kind.ARTICLE
    assert value.points[0].weight == 0.5
    assert value.note is not None


def test_synthesize_is_deterministic() -> None:
    assert synthesize(_Summary, "same input") == synthesize(_Summary, "same input")


async def test_fake_llm_records_calls_and_can_be_scripted() -> None:
    def responder(call: FakeLLMCall, schema: type[BaseModel]) -> BaseModel | None:
        if call.task == "scripted":
            return _Point(text="from the script", weight=1.0)
        return None

    llm = FakeLLM(responder=responder)
    part = [TextPart(text="some input")]

    scripted = await llm.generate(
        task="scripted",
        prompt_version="1",
        system="s",
        parts=part,
        schema=_Point,
        max_output_tokens=50,
    )
    free = await llm.generate(
        task="free", prompt_version="1", system="s", parts=part, schema=_Point, max_output_tokens=50
    )

    assert scripted.value.text == "from the script"
    assert free.value.text == "some input"
    assert [c.task for c in llm.calls] == ["scripted", "free"]


async def test_fake_fetcher_records_what_was_requested() -> None:
    fetcher = FakeFetcher()
    fetcher.add("https://a.test/", "<p>hi</p>")

    await fetcher.get("https://a.test/", max_bytes=100, accept=["text/html"])

    assert fetcher.requested == ["https://a.test/"]
