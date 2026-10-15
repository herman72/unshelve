import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.config import Settings
from unshelve.contracts.enums import JobKind
from unshelve.contracts.jobs import EnrichItemPayload, FetchItemPayload, JobContext
from unshelve.providers.ledger import CachedLLM
from unshelve.queue.registry import Registry, discover
from unshelve.services import build_services
from unshelve.testing.fakes import FakeLLM


async def _fetch(ctx: JobContext, payload: FetchItemPayload) -> None:
    pass


async def _enrich(ctx: JobContext, payload: EnrichItemPayload) -> None:
    pass


def test_a_handler_is_found_by_its_kind_with_its_payload_type() -> None:
    registry = Registry()
    registry.handler(JobKind.FETCH_ITEM, timeout_s=30)(_fetch)

    spec = registry.get(JobKind.FETCH_ITEM)

    assert spec is not None
    assert (spec.run, spec.payload_type, spec.timeout.total_seconds()) == (
        _fetch,
        FetchItemPayload,
        30,
    )
    assert registry.get(JobKind.ENRICH_ITEM) is None


def test_a_kind_has_one_handler() -> None:
    registry = Registry()
    registry.handler(JobKind.FETCH_ITEM)(_fetch)

    with pytest.raises(ValueError, match="fetch_item already has a handler"):
        registry.handler(JobKind.FETCH_ITEM)(_fetch)


def test_kinds_lists_what_a_worker_can_run() -> None:
    registry = Registry()
    registry.handler(JobKind.FETCH_ITEM)(_fetch)
    registry.handler(JobKind.ENRICH_ITEM)(_enrich)

    assert registry.kinds() == [JobKind.ENRICH_ITEM, JobKind.FETCH_ITEM]


def test_discover_imports_the_pipeline_package() -> None:
    # No handler modules exist yet; the point is that discovery itself works.
    assert discover("unshelve.pipeline") >= 0


def test_services_are_fakes_by_default() -> None:
    sessions: async_sessionmaker[AsyncSession] = async_sessionmaker()

    services = build_services(Settings(env="test"), sessions)

    assert services.embedder.spec.provider == "fake"
    assert isinstance(services.llm, CachedLLM)
    assert isinstance(services.llm.inner, FakeLLM)
