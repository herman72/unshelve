"""The score step: how relevant an item is to each active goal of its owner (docs/adr/0014).

It runs after the embed step, when the item is ready. Two stages:

1. The prefilter. The card vector of the item is compared with the vector of each goal. When
   the card is far from each goal (each similarity below ``UNSHELVE_SCORE_PREFILTER``), each
   goal gets 0, and the model is not asked.
2. Else one question to the model scores each active goal: 0 to 3, and a reason.

Each score keeps the version of its goal and the scorer that gave it. A score with a label
stays as it is: the label is the owner's verdict on that score (M3-3). The step is safe to run
two times: the ledger answers the same question again for free.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import Actor, EnrichState, JobKind
from unshelve.contracts.errors import ProviderError
from unshelve.contracts.jobs import JobContext, Permanent, Retry, ScoreItemPayload
from unshelve.db.models import Event, Goal, Item, ItemContent, ItemGoal, ItemTag, Tag
from unshelve.pipeline.items import model_wait
from unshelve.queue.registry import registry
from unshelve.relevance.prompt import (
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    TASK,
    item_text,
    score_parts,
    verdicts_of,
    verdicts_schema,
)
from unshelve.relevance.vectors import best_similarities, ensure_goal_vectors

log = structlog.get_logger()

# The scorer of a score of the prefilter. A score of the model names its prompt and its model.
PREFILTER = f"{PROMPT_VERSION}:prefilter"


@dataclass(frozen=True, slots=True)
class _Score:
    goal: Goal
    score: int
    reason: str
    scorer: str


async def _active_goals(session: AsyncSession, owner_id: int) -> list[Goal]:
    goals = await session.scalars(
        sa.select(Goal)
        .where(Goal.owner_id == owner_id, Goal.archived_at.is_(None))
        .order_by(Goal.id)
    )
    return list(goals)


async def _labelled(session: AsyncSession, item_id: int) -> set[int]:
    found = await session.scalars(
        sa.select(ItemGoal.goal_id).where(ItemGoal.item_id == item_id, ItemGoal.label.is_not(None))
    )
    return set(found)


async def _card(session: AsyncSession, item: Item) -> str:
    """The card that the model reads: the card of the item with its tags, or its text."""
    tags = await session.scalars(
        sa.select(Tag.name)
        .join(ItemTag, ItemTag.tag_id == Tag.id)
        .where(ItemTag.item_id == item.id)
        .order_by(Tag.name)
    )
    text = await session.scalar(sa.select(ItemContent.text).where(ItemContent.item_id == item.id))
    return item_text(item, list(tags), text or "")


async def _closest(ctx: JobContext, item: Item, goals: Sequence[Goal]) -> float | None:
    """The similarity of the card of the item to its closest goal. None when it is not known:
    no card vector, or no goal vectors. Then the model must read the item."""
    services = ctx.services
    try:
        set_id = await ensure_goal_vectors(services.sessions, services.embedder, goals)
    except ProviderError as error:
        if error.retryable and ctx.job.attempts < ctx.job.max_attempts:
            raise Retry(
                f"embed goals: {error}", after=model_wait(error, ctx.job.attempts)
            ) from error
        log.info("score.no_prefilter", item_id=item.id, reason=str(error)[:300])
        return None
    async with services.sessions() as session:
        found = await best_similarities(session, set_id, [item.id], goals)
    return found.get(item.id)


async def _write(session: AsyncSession, item: Item, scores: Sequence[_Score]) -> None:
    insert = postgresql.insert(ItemGoal).values(
        [
            {
                "item_id": item.id,
                "goal_id": score.goal.id,
                "owner_id": item.owner_id,
                "score": score.score,
                "reason": score.reason,
                "scorer_version": score.scorer,
                "goal_version": score.goal.version,
            }
            for score in scores
        ]
    )
    columns = ("score", "reason", "scorer_version", "goal_version")
    await session.execute(
        insert.on_conflict_do_update(
            constraint="pk_item_goals",
            set_={
                **{column: insert.excluded[column] for column in columns},
                "scored_at": sa.func.now(),
            },
            # A label is the owner's verdict on the score that was shown: that score stays.
            where=ItemGoal.label.is_(None),
        )
    )


@registry.handler(JobKind.SCORE_ITEM)
async def score_item(ctx: JobContext, payload: ScoreItemPayload) -> None:
    """Score the item against each active goal of its owner: the prefilter, then the model."""
    services = ctx.services
    async with services.sessions() as session:
        item = await session.get(Item, payload.item_id)
        if item is None:
            raise Permanent(f"item {payload.item_id} does not exist")
        # A merged copy has no score of its own: the lists show its older item.
        if item.enrich_state is not EnrichState.READY or item.duplicate_of_id is not None:
            return
        goals = await _active_goals(session, item.owner_id)
        labelled = await _labelled(session, item.id)
        card = await _card(session, item)
        await session.commit()
    if not [goal for goal in goals if goal.id not in labelled]:
        return  # no goal, or each score has a label

    threshold = services.settings.score_prefilter
    closest = await _closest(ctx, item, goals) if threshold > -1 else None
    if closest is not None and closest < threshold:
        reason = f"The card is far from each goal: the closest is at {closest:.2f}."
        scores = [_Score(goal, 0, reason, PREFILTER) for goal in goals]
    else:
        try:
            result = await services.llm.generate(
                task=TASK,
                prompt_version=PROMPT_VERSION,
                system=SYSTEM_PROMPT,
                parts=score_parts(goals, card),
                schema=verdicts_schema(len(goals)),
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
        except ProviderError as error:
            if error.retryable and ctx.job.attempts < ctx.job.max_attempts:
                raise Retry(f"model: {error}", after=model_wait(error, ctx.job.attempts)) from error
            await _failed(ctx, item, error)
            return
        scorer = f"{PROMPT_VERSION}:{result.model}"
        verdicts = verdicts_of(result.value, len(goals))
        scores = [
            _Score(goal, verdict.score, " ".join(verdict.reason.split())[:500], scorer)
            for goal, verdict in zip(goals, verdicts, strict=True)
        ]

    async with services.sessions() as session:
        # The item can have started again in the meantime: then its new card gets new scores.
        ready = await session.scalar(
            sa.select(Item.id)
            .where(Item.id == item.id, Item.enrich_state == EnrichState.READY)
            .with_for_update(read=True)
        )
        if ready is not None:
            # A score with a label is skipped by the write itself: also a label that came
            # while the model answered.
            await _write(session, item, scores)
        await session.commit()
    log.info(
        "score.done",
        item_id=item.id,
        goals=len(goals),
        prefilter=closest is not None and closest < threshold,
        closest=None if closest is None else round(closest, 3),
    )


async def _failed(ctx: JobContext, item: Item, error: ProviderError) -> None:
    """The model cannot score the item. The item keeps no new scores, and an event says why:
    `unshelve goals rescore` can try again later."""
    async with ctx.services.sessions() as session:
        session.add(
            Event(
                owner_id=item.owner_id,
                item_id=item.id,
                actor=Actor.WORKER,
                type="score_failed",
                payload={"reason": str(error)[:300], "attempt": ctx.job.attempts},
            )
        )
        await session.commit()
    log.info("score.failed", item_id=item.id, reason=str(error)[:300])
