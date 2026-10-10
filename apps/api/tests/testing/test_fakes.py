"""The fakes pass the same conformance suites that the real adapters will."""

from enum import StrEnum

import pytest
from pydantic import BaseModel

from unshelve.contracts.dto import SourceRef, TextPart
from unshelve.contracts.protocols import BlobStore, Embedder, ExtractContext, LLMProvider
from unshelve.testing.conformance import (
    BlobStoreConformance,
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


class TestFakeLLM(LLMProviderConformance):
    @pytest.fixture
    def llm(self) -> LLMProvider:
        return FakeLLM()


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
