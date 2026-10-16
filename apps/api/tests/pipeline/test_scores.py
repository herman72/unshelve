"""The score step: the prefilter, then one question to the model for all goals. Each test name
is one rule. A pretend model and a pretend embedder: no test reaches a real API."""

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.api.goal_views import slug_for
from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import (
    CaptureSource,
    ChunkKind,
    EnrichState,
    GoalLabel,
    JobKind,
    JobState,
    LabelSource,
)
from unshelve.contracts.errors import ProviderError
from unshelve.contracts.jobs import ScoreItemPayload, Services
from unshelve.db.models import Chunk, Event, Goal, GoalVector, Item, ItemGoal, Job
from unshelve.ingest import resolve
from unshelve.providers.ledger import CachedLLM
from unshelve.queue import store as queue
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.relevance.prompt import TASK, verdicts_schema
from unshelve.services import build_services
from unshelve.testing.fakes import FakeEmbedder, FakeLLM, FakeLLMCall

NOON = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
TEXT = "Leases make a job queue on one Postgres table safe. A heartbeat moves the lease."

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def services(committed_db: Sessions, private_database_url: str) -> Services:
    # No prefilter, unless a test turns it on: most tests look at the question to the model.
    settings = Settings(env="test", database_url=private_database_url, score_prefilter=-1)
    return build_services(settings, committed_db)


def _prefilter(services: Services, threshold: float) -> Services:
    settings = services.settings.model_copy(update={"score_prefilter": threshold})
    return dataclasses.replace(services, settings=settings)


def _model(
    services: Services, *verdicts: tuple[int, str], error: ProviderError | None = None
) -> tuple[Services, FakeLLM]:
    """The services with a pretend model that gives these verdicts, behind the same cache as in
    the worker. The card of an item is made up from its text, as usual."""

    def respond(call: FakeLLMCall, schema: type[BaseModel]) -> BaseModel | None:
        if call.task != TASK:
            return None
        if error is not None:
            raise error
        answer = {f"g{n + 1}": {"score": s, "reason": r} for n, (s, r) in enumerate(verdicts)}
        return verdicts_schema(len(verdicts)).model_validate(answer)

    fake = FakeLLM(responder=respond)
    model = CachedLLM(fake, services.sessions, model=FakeLLM.model)
    return dataclasses.replace(services, llm=model), fake


class CountingEmbedder:
    """The pretend embedder, with the texts of each question for a goal."""

    def __init__(self, error: ProviderError | None = None) -> None:
        self._inner = FakeEmbedder()
        self.spec = self._inner.spec
        self.queries: list[str] = []
        self.error = error

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        if self.error is not None:
            raise self.error
        return await self._inner.embed_query(text)


async def _run_all(services: Services) -> None:
    discover()
    worker = Worker(services, registry, WorkerConfig(worker_id="w1"))
    while await worker.run_one():
        pass


async def _ready_item(services: Services, text: str = TEXT) -> int:
    """A saved note that went through the pipeline: it has its card and its card vector."""
    async with services.sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        envelope = CaptureEnvelope(
            source=CaptureSource.API,
            chat_id=resolve.API_CHAT_ID,
            message_id=await resolve.next_api_message_id(db),
            posted_at=NOON,
            text=text,
            urls=[],
        )
        capture_id = await resolve.store_capture(db, owner_id, envelope)
        assert capture_id is not None
        item_id = await resolve.resolve_capture(db, capture_id)
        await db.commit()
    assert item_id is not None
    await _run_all(services)  # without a goal, the score step asks nothing
    return item_id


async def _goal(services: Services, title: str, **values: Any) -> Goal:
    async with services.sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        goal = Goal(owner_id=owner_id, slug=slug_for(title), title=title, version=1, **values)
        db.add(goal)
        await db.commit()
    return goal


async def _score(services: Services, item_id: int) -> None:
    async with services.sessions() as db:
        item = await db.get_one(Item, item_id)
        await queue.enqueue(
            db,
            JobKind.SCORE_ITEM,
            ScoreItemPayload(item_id=item_id),
            owner_id=item.owner_id,
            dedupe_key=f"item:{item_id}",
        )
        await db.commit()
    await _run_all(services)


async def _scores(services: Services, item_id: int) -> dict[str, ItemGoal]:
    async with services.sessions() as db:
        rows = await db.execute(
            sa.select(Goal.title, ItemGoal)
            .join(ItemGoal, ItemGoal.goal_id == Goal.id)
            .where(ItemGoal.item_id == item_id)
        )
        return {row.title: row.ItemGoal for row in rows}


def _questions(fake: FakeLLM) -> list[FakeLLMCall]:
    return [call for call in fake.calls if call.task == TASK]


async def _card_text(services: Services, item_id: int) -> str:
    async with services.sessions() as db:
        return (
            await db.scalar(
                sa.select(Chunk.text).where(Chunk.item_id == item_id, Chunk.kind == ChunkKind.CARD)
            )
            or ""
        )


# --- The question to the model -------------------------------------------------------------


async def test_an_item_gets_a_score_and_a_reason_for_each_active_goal(services: Services) -> None:
    item_id = await _ready_item(services)
    await _goal(services, "Database internals", description="How Postgres stores rows.")
    await _goal(services, "Learn Finnish", keywords=["suomi"])
    await _goal(services, "Run a 10k", archived_at=NOON)
    services, fake = _model(services, (3, "It explains leases\n  in Postgres. "), (0, "No link."))

    await _score(services, item_id)

    scores = await _scores(services, item_id)
    assert {title: (s.score, s.reason) for title, s in scores.items()} == {
        # The reason as one line, without the spaces around it.
        "Database internals": (3, "It explains leases in Postgres."),
        "Learn Finnish": (0, "No link."),
    }
    assert {(s.scorer_version, s.goal_version, s.label) for s in scores.values()} == {
        ("score-1:fake-llm", 1, None)
    }
    (question,) = _questions(fake)
    assert "g1: Database internals\nHow Postgres stores rows." in question.text
    assert "g2: Learn Finnish\nKeywords: suomi" in question.text
    assert "Run a 10k" not in question.text  # an archived goal is not worked on
    assert "<item>\nTitle: " in question.text


async def test_without_an_active_goal_the_model_is_not_asked(services: Services) -> None:
    item_id = await _ready_item(services)
    await _goal(services, "Run a 10k", archived_at=NOON)
    services, fake = _model(services, (3, "Never asked."))

    await _score(services, item_id)

    assert _questions(fake) == []
    assert await _scores(services, item_id) == {}


async def test_a_second_run_asks_the_model_nothing_new(services: Services) -> None:
    item_id = await _ready_item(services)
    await _goal(services, "Database internals")
    services, fake = _model(services, (2, "Background for the goal."))

    await _score(services, item_id)
    await _score(services, item_id)

    # The same question has one cache key: the ledger answers the second time, for free.
    assert len(_questions(fake)) == 1
    assert (await _scores(services, item_id))["Database internals"].score == 2


async def test_a_labelled_score_stays_as_it_was(services: Services) -> None:
    item_id = await _ready_item(services)
    labelled = await _goal(services, "Database internals")
    await _goal(services, "Learn Finnish")
    async with services.sessions() as db:
        db.add(
            ItemGoal(
                item_id=item_id,
                goal_id=labelled.id,
                owner_id=labelled.owner_id,
                score=1,
                reason="The first reason.",
                label=GoalLabel.REJECTED,
                label_source=LabelSource.WEB,
                scorer_version="score-0:old-model",
                goal_version=1,
            )
        )
        await db.commit()
    services, _ = _model(services, (3, "A new reason."), (0, "No link."))

    await _score(services, item_id)

    scores = await _scores(services, item_id)
    kept = scores["Database internals"]
    assert (kept.score, kept.reason, kept.label) == (1, "The first reason.", GoalLabel.REJECTED)
    assert kept.scorer_version == "score-0:old-model"
    assert scores["Learn Finnish"].reason == "No link."


async def test_an_item_whose_scores_all_have_labels_asks_nothing(services: Services) -> None:
    item_id = await _ready_item(services)
    goal = await _goal(services, "Database internals")
    async with services.sessions() as db:
        db.add(
            ItemGoal(
                item_id=item_id,
                goal_id=goal.id,
                owner_id=goal.owner_id,
                score=2,
                reason="Mine.",
                label=GoalLabel.CONFIRMED,
                label_source=LabelSource.DM,
                scorer_version="score-1:fake-llm",
                goal_version=1,
            )
        )
        await db.commit()
    services, fake = _model(services, (0, "Never asked."))

    await _score(services, item_id)

    # Each score has your verdict: a question would change nothing, so it is not paid.
    assert _questions(fake) == []


async def test_a_label_given_while_the_model_answers_stays(services: Services) -> None:
    item_id = await _ready_item(services)
    goal = await _goal(services, "Database internals")
    services, _ = _model(services, (3, "The answer of the model."))
    inner = services.llm

    class LabelledMeanwhile:
        """The model, while the owner confirms the score that the page shows."""

        model = FakeLLM.model

        async def generate(self, **request: Any) -> Any:
            async with services.sessions() as db:
                db.add(
                    ItemGoal(
                        item_id=item_id,
                        goal_id=goal.id,
                        owner_id=goal.owner_id,
                        score=1,
                        reason="The score that the owner saw.",
                        label=GoalLabel.CONFIRMED,
                        label_source=LabelSource.WEB,
                        scorer_version="score-1:fake-llm",
                        goal_version=1,
                    )
                )
                await db.commit()
            return await inner.generate(**request)

    services = dataclasses.replace(services, llm=LabelledMeanwhile())  # pyright: ignore[reportArgumentType]

    await _score(services, item_id)

    # The step read no label at its start. The write still skips the row with a label.
    score = (await _scores(services, item_id))["Database internals"]
    assert (score.score, score.reason, score.label) == (
        1,
        "The score that the owner saw.",
        GoalLabel.CONFIRMED,
    )


async def test_a_new_score_replaces_an_old_one_without_a_label(services: Services) -> None:
    item_id = await _ready_item(services)
    goal = await _goal(services, "Database internals")
    services, _ = _model(services, (2, "First."))
    await _score(services, item_id)
    async with services.sessions() as db:
        await db.execute(
            sa.update(Goal)
            .where(Goal.id == goal.id)
            .values(description="Indexes and locks.", version=2)
        )
        await db.commit()
    services, _ = _model(services, (3, "Second."))

    await _score(services, item_id)

    score = (await _scores(services, item_id))["Database internals"]
    assert (score.score, score.reason, score.goal_version) == (3, "Second.", 2)


# --- The prefilter -------------------------------------------------------------------------


async def test_an_item_far_from_each_goal_gets_0_without_a_question(services: Services) -> None:
    item_id = await _ready_item(services)
    await _goal(services, "Finnish grammar", keywords=["cases", "vocabulary"])
    await _goal(services, "Run a marathon")
    services, fake = _model(_prefilter(services, 0.9), (3, "Never asked."), (3, "Never asked."))

    await _score(services, item_id)

    assert _questions(fake) == []
    scores = await _scores(services, item_id)
    assert {(s.score, s.scorer_version) for s in scores.values()} == {(0, "score-1:prefilter")}
    reason = scores["Run a marathon"].reason
    assert reason.startswith("The card is far from each goal: the closest is at ")
    assert float(reason.removesuffix(".").rsplit(" ", 1)[1]) < 0.9


async def test_an_item_close_to_one_goal_is_scored_for_each_goal_by_the_model(
    services: Services,
) -> None:
    item_id = await _ready_item(services)
    # A goal in the same words as the card of the item: about the same vector.
    await _goal(services, await _card_text(services, item_id))
    await _goal(services, "Run a marathon")
    services, fake = _model(_prefilter(services, 0.9), (3, "The same topic."), (0, "No link."))

    await _score(services, item_id)

    (question,) = _questions(fake)
    assert "g2: Run a marathon" in question.text  # one question for each active goal
    scores = await _scores(services, item_id)
    assert scores["Run a marathon"].scorer_version == "score-1:fake-llm"


async def test_without_a_card_vector_the_model_reads_the_item(services: Services) -> None:
    item_id = await _ready_item(services)
    async with services.sessions() as db:
        await db.execute(sa.delete(Chunk).where(Chunk.item_id == item_id))
        await db.commit()
    await _goal(services, "Run a marathon")
    services, fake = _model(_prefilter(services, 0.99), (1, "Loosely."))

    await _score(services, item_id)

    # Nothing says that the item is far from the goal, so the model decides.
    assert len(_questions(fake)) == 1


async def test_each_goal_gets_one_vector_for_each_version(services: Services) -> None:
    embedder = CountingEmbedder()
    services = dataclasses.replace(_prefilter(services, 0.5), embedder=embedder)
    first, second = await _ready_item(services), await _ready_item(services, "A second note.")
    goal = await _goal(services, "Database internals", keywords=["postgres"])
    services, _ = _model(services, (2, "Some link."))

    await _score(services, first)
    await _score(services, second)
    async with services.sessions() as db:
        await db.execute(
            sa.update(Goal).where(Goal.id == goal.id).values(title="Indexes", version=2)
        )
        await db.commit()
    await _score(services, first)

    assert embedder.queries == [
        "Database internals\nKeywords: postgres",
        "Indexes\nKeywords: postgres",
    ]
    async with services.sessions() as db:
        vector = await db.scalar(sa.select(GoalVector))
    assert vector is not None
    assert (vector.goal_version, vector.text, vector.dim) == (2, "Indexes\nKeywords: postgres", 64)


async def test_an_embedder_that_fails_for_good_leaves_the_decision_to_the_model(
    services: Services,
) -> None:
    embedder = CountingEmbedder(error=ProviderError("bad key", retryable=False))
    services = dataclasses.replace(_prefilter(services, 0.99), embedder=embedder)
    item_id = await _ready_item(services)
    await _goal(services, "Run a marathon")
    services, fake = _model(services, (0, "No link."))

    await _score(services, item_id)

    assert len(_questions(fake)) == 1
    assert (await _scores(services, item_id))["Run a marathon"].scorer_version == "score-1:fake-llm"


# --- When the model fails, and items that get no score ---------------------------------------


async def test_a_model_that_fails_for_good_leaves_an_event_and_no_score(services: Services) -> None:
    item_id = await _ready_item(services)
    await _goal(services, "Database internals")
    services, _ = _model(services, error=ProviderError("the model declined", retryable=False))

    await _score(services, item_id)

    assert await _scores(services, item_id) == {}
    async with services.sessions() as db:
        event = await db.scalar(sa.select(Event).where(Event.type == "score_failed"))
    assert event is not None
    assert event.payload == {"reason": "the model declined", "attempt": 1}


async def test_a_model_that_fails_for_now_is_asked_again_later(services: Services) -> None:
    item_id = await _ready_item(services)
    await _goal(services, "Database internals")
    services, _ = _model(services, error=ProviderError("overloaded", retryable=True))

    await _score(services, item_id)

    async with services.sessions() as db:
        job = await db.scalar(
            sa.select(Job).where(Job.kind == JobKind.SCORE_ITEM).order_by(Job.id.desc())
        )
    assert job is not None
    assert (job.state, job.attempts, job.last_error) == (JobState.PENDING, 1, "model: overloaded")
    assert await _scores(services, item_id) == {}


async def test_an_item_that_is_not_ready_asks_nothing(services: Services) -> None:
    item_id = await _ready_item(services)
    await _goal(services, "Database internals")
    async with services.sessions() as db:
        await db.execute(
            sa.update(Item).where(Item.id == item_id).values(enrich_state=EnrichState.ENRICHED)
        )
        await db.commit()
    services, fake = _model(services, (3, "Never asked."))

    await _score(services, item_id)

    # Its card can still change: the embed step starts the score again when it is ready.
    assert _questions(fake) == []


async def test_a_merged_copy_gets_no_score_of_its_own(services: Services) -> None:
    older = await _ready_item(services)
    copy = await _ready_item(services, "The same thing, saved again.")
    async with services.sessions() as db:
        await db.execute(sa.update(Item).where(Item.id == copy).values(duplicate_of_id=older))
        await db.commit()
    await _goal(services, "Database internals")
    services, fake = _model(services, (3, "Never asked."))

    await _score(services, copy)

    assert _questions(fake) == []


async def test_an_item_that_started_again_keeps_no_score_of_its_old_card(
    services: Services,
) -> None:
    item_id = await _ready_item(services)
    await _goal(services, "Database internals")
    services, fake = _model(services, (3, "Of the old card."))
    inner = services.llm

    class Reprocessed:
        """The model, while the owner sends the item back to the start of the pipeline."""

        model = FakeLLM.model

        async def generate(self, **request: Any) -> Any:
            async with services.sessions() as db:
                await db.execute(
                    sa.update(Item)
                    .where(Item.id == item_id)
                    .values(enrich_state=EnrichState.PENDING)
                )
                await db.commit()
            return await inner.generate(**request)

    services = dataclasses.replace(services, llm=Reprocessed())  # pyright: ignore[reportArgumentType]

    await _score(services, item_id)

    assert len(_questions(fake)) == 1
    assert await _scores(services, item_id) == {}
