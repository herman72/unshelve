"""Score items again: after you add a goal, or give a goal a new meaning.

The worker scores each new item when it is ready. An item that has scores keeps them when a
goal changes, because each new score can cost a question to the model. This module plans that
work and its price; the command ``unshelve goals rescore`` shows the plan, and starts the jobs
after your yes.

An item needs a score when an active goal has no score for it, or only a score of an older
version of the goal or of an older prompt. A score with a label never counts: it stays.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.contracts.dto import LLMUsage
from unshelve.contracts.enums import EnrichState, JobKind
from unshelve.contracts.jobs import ScoreItemPayload
from unshelve.contracts.protocols import Embedder
from unshelve.db.models import Goal, Item, ItemContent, ItemGoal, ItemTag, LLMCall, Tag
from unshelve.providers.ledger import cache_key, cost_usd
from unshelve.queue import store as queue
from unshelve.relevance.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    TASK,
    item_text,
    score_parts,
    verdicts_schema,
)
from unshelve.relevance.vectors import best_similarities, ensure_goal_vectors

# As for the cards (pipeline.batches): one token for each 2.5 characters of the request. A
# score with its reason is about 45 tokens of the answer.
_CHARACTERS_PER_TOKEN = 2.5
_OUTPUT_TOKENS_PER_GOAL = 45


@dataclass(frozen=True, slots=True)
class Plan:
    goals: int
    items: list[int]  # each item that gets a score job
    far: int  # the prefilter scores them: no question
    stored: int  # the ledger has the answer of their question: no cost
    questions: int  # the questions to pay
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None  # None for a model without a known price


async def active_goals(session: AsyncSession, owner_id: int) -> list[Goal]:
    found = await session.scalars(
        sa.select(Goal)
        .where(Goal.owner_id == owner_id, Goal.archived_at.is_(None))
        .order_by(Goal.id)
    )
    return list(found)


async def _items_to_score(
    session: AsyncSession, owner_id: int, goals: Sequence[Goal], *, every_item: bool
) -> list[Item]:
    ready = list(
        await session.scalars(
            sa.select(Item)
            .where(
                Item.owner_id == owner_id,
                Item.enrich_state == EnrichState.READY,
                Item.duplicate_of_id.is_(None),
            )
            .order_by(Item.id)
        )
    )
    if every_item:
        return ready
    rows = await session.execute(
        sa.select(
            ItemGoal.item_id,
            ItemGoal.goal_id,
            ItemGoal.goal_version,
            ItemGoal.scorer_version,
            ItemGoal.label,
        ).where(ItemGoal.owner_id == owner_id)
    )
    versions = {goal.id: goal.version for goal in goals}
    current = {
        (row.item_id, row.goal_id)
        for row in rows
        if row.label is not None
        or (
            row.goal_version == versions.get(row.goal_id)
            and row.scorer_version.startswith(f"{PROMPT_VERSION}:")
        )
    }
    return [item for item in ready if any((item.id, goal.id) not in current for goal in goals)]


async def _cards(session: AsyncSession, items: Sequence[Item]) -> dict[int, str]:
    """The card of each item, as the score step shows it to the model."""
    ids = [item.id for item in items]
    tags: dict[int, list[str]] = {}
    for row in await session.execute(
        sa.select(ItemTag.item_id, Tag.name)
        .join(Tag, Tag.id == ItemTag.tag_id)
        .where(ItemTag.item_id.in_(ids))
        .order_by(Tag.name)
    ):
        tags.setdefault(row.item_id, []).append(row.name)
    texts = {
        row.item_id: row.text
        for row in await session.execute(
            sa.select(ItemContent.item_id, ItemContent.text).where(ItemContent.item_id.in_(ids))
        )
    }
    return {
        item.id: item_text(item, tags.get(item.id, []), texts.get(item.id, "")) for item in items
    }


async def plan(
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    owner_id: int,
    *,
    model: str,
    prefilter: float,
    every_item: bool = False,
) -> Plan:
    """What a new scoring of the owner's items would do, and what it would cost.

    It gives each goal its vector first, which asks the embedder for new goals.
    """
    async with sessions() as session:
        goals = await active_goals(session, owner_id)
        await session.commit()
    if not goals:
        return Plan(0, [], 0, 0, 0, 0, 0, Decimal(0))
    set_id = await ensure_goal_vectors(sessions, embedder, goals) if prefilter > -1 else None
    async with sessions() as session:
        items = await _items_to_score(session, owner_id, goals, every_item=every_item)
        closest = (
            await best_similarities(session, set_id, [item.id for item in items], goals)
            if set_id is not None
            else {}
        )
        asked = [
            item for item in items if closest.get(item.id) is None or closest[item.id] >= prefilter
        ]
        cards = await _cards(session, asked)
        schema = verdicts_schema(len(goals))
        parts = {item.id: score_parts(goals, cards[item.id]) for item in asked}
        keys = {
            item_id: cache_key(model, TASK, PROMPT_VERSION, SYSTEM_PROMPT, question, schema)
            for item_id, question in parts.items()
        }
        found = set(
            await session.scalars(
                sa.select(LLMCall.cache_key).where(LLMCall.cache_key.in_(list(keys.values())))
            )
        )
        await session.commit()
    to_pay = [item_id for item_id, key in keys.items() if key not in found]
    request = len(SYSTEM_PROMPT) + len(json.dumps(schema.model_json_schema()))
    input_tokens = sum(
        round((request + len(parts[item_id][0].text)) / _CHARACTERS_PER_TOKEN) for item_id in to_pay
    )
    output_tokens = len(to_pay) * len(goals) * _OUTPUT_TOKENS_PER_GOAL
    return Plan(
        goals=len(goals),
        items=[item.id for item in items],
        far=len(items) - len(asked),
        stored=len(asked) - len(to_pay),
        questions=len(to_pay),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd(model, LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens)),
    )


async def start(
    sessions: async_sessionmaker[AsyncSession], owner_id: int, item_ids: Sequence[int]
) -> int:
    """Add a score job for each item. Returns the number of new jobs: a waiting job counts once."""
    started = 0
    async with sessions() as session:
        for item_id in item_ids:
            job = await queue.enqueue(
                session,
                JobKind.SCORE_ITEM,
                ScoreItemPayload(item_id=item_id),
                owner_id=owner_id,
                dedupe_key=f"item:{item_id}",
            )
            started += job is not None
        await session.commit()
    return started
