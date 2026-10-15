"""The GitHub extractor, on made-up answers in the form of the real ones. A pretend fetcher.
Each test name is one rule."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from unshelve.contracts.dto import Extracted, FetchResult, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import FetchError
from unshelve.contracts.protocols import ExtractContext
from unshelve.extract.article import GENERIC_SCORE, ArticleExtractor
from unshelve.extract.github import GitHubExtractor
from unshelve.testing.conformance import ExtractorCase, ExtractorConformance
from unshelve.testing.fakes import FakeFetcher, FakeLLM, InMemoryBlobStore

FIXTURES = Path(__file__).parent / "fixtures"
API = "https://api.github.com/repos/made-up-org/queue-kit"
README = f"{API}/readme"
PAGE = "https://github.com/made-up-org/queue-kit"
SAVED = "https://github.com/Made-Up-Org/queue-kit.git"


def _repo(**change: Any) -> bytes:
    answer = json.loads((FIXTURES / "github" / "repo.json").read_text(encoding="utf-8"))
    answer.update(change)
    return json.dumps(answer).encode()


class Fetcher(FakeFetcher):
    """A pretend fetcher that serves the fixtures, and writes down what it was asked."""

    def __init__(self, *, repo: bytes | None = None, readme: bool = True) -> None:
        super().__init__()
        self.asked: list[str] = []
        self.add(API, repo if repo is not None else _repo(), "application/json; charset=utf-8")
        if readme:
            self.add(README, (FIXTURES / "github" / "readme.json").read_bytes(), "application/json")
        # The page of the repository, for the article extractor.
        self.add(PAGE, (FIXTURES / "article_en.html").read_bytes(), "text/html; charset=utf-8")

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        self.asked.append(url)
        return await super().get(url, max_bytes=max_bytes, accept=accept)


def _context(fetcher: FakeFetcher) -> ExtractContext:
    return ExtractContext(fetcher=fetcher, blobs=InMemoryBlobStore(), llm=FakeLLM())


async def _extract(fetcher: Fetcher, url: str = SAVED) -> Extracted:
    return await GitHubExtractor().extract(SourceRef(url=url, text=url), _context(fetcher))


class TestGitHubExtractor(ExtractorConformance):
    @pytest.fixture
    def case(self) -> ExtractorCase:
        return ExtractorCase(
            GitHubExtractor(),
            _context(Fetcher()),
            matching=SourceRef(url=SAVED),
            unrelated=SourceRef(url="https://blog.example.com/queues"),
        )


# --- Which links are repositories --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        SAVED,
        PAGE,
        f"{PAGE}/",
        "http://www.github.com/made-up-org/queue-kit?tab=readme-ov-file",
        f"{PAGE}#readme",
    ],
)
def test_each_address_of_a_repository_is_claimed_before_the_article_extractor(url: str) -> None:
    ref = SourceRef(url=url)

    assert GitHubExtractor().match(ref) > GENERIC_SCORE
    assert ArticleExtractor().match(ref) == GENERIC_SCORE


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/made-up-org",
        f"{PAGE}/issues/7",
        f"{PAGE}/pull/8",
        f"{PAGE}/blob/main/README.md",
        f"{PAGE}/tree/main/docs",
        "https://github.com/topics/postgres",
        "https://github.com/orgs/made-up-org/repositories",
        "https://gist.github.com/made-up-org/0123456789abcdef",
        "https://github.com/",
        None,
    ],
)
def test_other_pages_and_notes_are_left_to_other_extractors(url: str | None) -> None:
    assert GitHubExtractor().match(SourceRef(url=url, text="a note")) == 0


# --- What a repository gives -------------------------------------------------------------


async def test_a_repository_gives_its_facts_and_its_readme() -> None:
    fetcher = Fetcher()

    found = await _extract(fetcher)

    assert fetcher.asked == [API, README]
    assert found.kind is ItemKind.REPO
    assert found.title == "made-up-org/queue-kit: A job queue on one Postgres table"
    assert found.author == "made-up-org"
    assert found.site == "github.com"
    assert found.lang is None
    assert found.final_url == PAGE
    assert found.published_at == datetime(2025, 4, 20, 21, 13, 52, tzinfo=UTC)
    assert found.extra_links == ["https://queue-kit.example.com"]
    assert found.text.splitlines()[:3] == [
        "made-up-org/queue-kit: A job queue on one Postgres table",
        "A repository on GitHub, in Python, under the MIT License. "
        "Topics: postgres, job-queue, skip-locked.",
        "Homepage: https://queue-kit.example.com",
    ]


async def test_the_readme_keeps_its_words_and_loses_its_badges_logos_and_comments() -> None:
    found = await _extract(Fetcher())

    readme = found.text.split("\n\n", 1)[1]
    assert readme == (
        "# queue-kit\n\n"
        "A job queue on one Postgres table. Workers take jobs with `FOR UPDATE SKIP LOCKED`, "
        "so two workers never take the same job.\n\n"
        "## Install\n\n"
        "    pip install queue-kit\n\n"
        "## How it works\n\n"
        "Each job is one row. A worker holds a lease, and the [reaper](docs/reaper.md) frees a "
        "job when its lease runs out."
    )


async def test_the_numbers_that_change_each_day_are_not_in_the_text() -> None:
    found = await _extract(Fetcher())

    assert "4321" not in found.text  # the stars
    assert "210" not in found.text  # the forks


async def test_a_repository_without_a_readme_gives_its_facts() -> None:
    found = await _extract(Fetcher(readme=False))

    assert found.text == (
        "made-up-org/queue-kit: A job queue on one Postgres table\n"
        "A repository on GitHub, in Python, under the MIT License. "
        "Topics: postgres, job-queue, skip-locked.\n"
        "Homepage: https://queue-kit.example.com"
    )


async def test_a_fork_and_an_archive_say_so() -> None:
    repo = _repo(fork=True, parent={"full_name": "first-org/queue-kit"}, archived=True)

    found = await _extract(Fetcher(repo=repo))

    assert found.text.splitlines()[1] == (
        "A repository on GitHub, in Python, under the MIT License. "
        "Topics: postgres, job-queue, skip-locked. It is a fork of first-org/queue-kit. "
        "It is archived: its owner no longer changes it."
    )


async def test_facts_that_are_missing_are_left_out() -> None:
    repo = _repo(
        description=None,
        language=None,
        license={"key": "other", "name": "Other", "spdx_id": "NOASSERTION"},
        topics=[],
        homepage="",
    )

    found = await _extract(Fetcher(repo=repo, readme=False))

    assert found.title == "made-up-org/queue-kit"
    assert found.text == "made-up-org/queue-kit\nA repository on GitHub."
    assert found.extra_links == []


@pytest.mark.parametrize(
    "homepage", ["javascript:alert(1)", "queue-kit.example.com", "ftp://example.com/queue-kit"]
)
async def test_a_homepage_that_is_no_web_address_is_left_out(homepage: str) -> None:
    # The web app shows the extra links as links. An address such as "javascript:…" would run
    # code there, so only http and https come in.
    found = await _extract(Fetcher(repo=_repo(homepage=homepage), readme=False))

    assert found.extra_links == []
    assert "Homepage" not in found.text


# --- When the API does not answer --------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        FetchError("http_error", API, status=403),  # the limit of 60 requests an hour
        FetchError("http_error", API, status=429, retry_after=60),
        FetchError("http_error", API, status=503),
        FetchError("timeout", API),
    ],
    ids=["403", "429", "503", "timeout"],
)
async def test_an_api_that_does_not_answer_gives_way_to_the_page(failure: FetchError) -> None:
    fetcher = Fetcher()
    fetcher.pages[API] = failure

    found = await _extract(fetcher)

    assert fetcher.asked == [API, PAGE]
    assert found.kind is ItemKind.REPO  # the article extractor read the page
    assert found.title == "Queues in Postgres"


async def test_a_readme_that_cannot_be_read_gives_way_to_the_page() -> None:
    fetcher = Fetcher(readme=False)
    fetcher.pages[README] = FetchError("http_error", README, status=403)

    found = await _extract(fetcher)

    assert fetcher.asked == [API, README, PAGE]
    assert found.title == "Queues in Postgres"


async def test_a_broken_answer_of_the_api_gives_way_to_the_page() -> None:
    fetcher = Fetcher(repo=b"{not json")

    found = await _extract(fetcher)

    assert fetcher.asked == [API, PAGE]
    assert found.title == "Queues in Postgres"


async def test_a_repository_that_does_not_exist_ends_with_the_404_of_its_page() -> None:
    fetcher = Fetcher()
    fetcher.pages[API] = FetchError("http_error", API, status=404)
    fetcher.pages[PAGE] = FetchError("http_error", PAGE, status=404)

    with pytest.raises(FetchError) as caught:
        await _extract(fetcher)

    # The fetch step keeps the saved text for a 404, and the item is partial.
    assert (caught.value.status, caught.value.retryable) == (404, False)
