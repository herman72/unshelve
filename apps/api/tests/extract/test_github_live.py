"""The GitHub extractor against the real GitHub. It runs only when it is asked for:

    uv run pytest -m live -o addopts="" tests/extract/test_github_live.py

It reads a public repository: no key, no cost, 2 of the 60 requests an hour that the API
allows without a key. It checks that the real answers still have the form that the fixtures in
``fixtures/github`` copy.
"""

from collections.abc import AsyncIterator

import pytest

from unshelve.config import Settings
from unshelve.contracts.dto import SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.github import GitHubExtractor
from unshelve.net.fetcher import LiveFetcher
from unshelve.testing.fakes import FakeLLM, InMemoryBlobStore

pytestmark = pytest.mark.live


@pytest.fixture
async def context() -> AsyncIterator[ExtractContext]:
    fetcher = LiveFetcher.from_settings(Settings(env="test"))
    yield ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())
    await fetcher.aclose()


async def test_a_real_repository_gives_its_facts_and_its_readme(context: ExtractContext) -> None:
    found = await GitHubExtractor().extract(
        SourceRef(url="https://github.com/pgvector/pgvector"), context
    )

    assert found.kind is ItemKind.REPO
    assert found.title == "pgvector/pgvector: Open-source vector similarity search for Postgres"
    assert found.author == "pgvector"
    assert "A repository on GitHub, in C." in found.text
    assert "# pgvector" in found.text  # the README
    assert len(found.text) > 10_000
    assert found.published_at is not None
    assert found.published_at.year == 2021


async def test_a_real_repository_that_does_not_exist_ends_with_a_404(
    context: ExtractContext,
) -> None:
    with pytest.raises(FetchError) as caught:
        await GitHubExtractor().extract(
            SourceRef(url="https://github.com/unshelve-made-up/no-such-repo"), context
        )

    assert caught.value.status == 404
