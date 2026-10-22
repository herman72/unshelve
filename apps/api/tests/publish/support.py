"""A made-up world for the tests of the public feed: public items, and a marker in each private
place that the feed must never show (docs/learn/42)."""

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.contracts.enums import Actor, CaptureSource, ItemKind, Visibility
from unshelve.db.models import (
    Capture,
    Event,
    Goal,
    Item,
    ItemContent,
    ItemGoal,
    ItemTag,
    Tag,
)

NOW = datetime(2026, 10, 22, 7, 0, tzinfo=UTC)
OWNER_TELEGRAM_ID = 4242
OTHER_TELEGRAM_ID = 4343

# A marker for each private place, by the name of the place. The feed must hold none of them.
# Each marker is unique, so a failure names the place that leaked.
PRIVATE = {
    "the private note of a public item": "LEAK-private-note",
    "a link in the extra links of a public item": "https://leak.example/extra-link",
    "the title of a private item": "LEAK-private-item",
    "the public note of a private item": "LEAK-public-note-of-a-private-item",
    "an item that was public and is private now": "LEAK-unpublished",
    "a public copy of a public item": "LEAK-public-copy",
    "the slug of a private tag of a public item": "leak-private-tag",
    "the name of that private tag": "LEAK-private-tag-name",
    "a public item of another owner": "LEAK-other-owner",
    "the title of a goal": "LEAK-goal-title",
    "the reason of a score": "LEAK-score-reason",
    "the saved text of a public item": "LEAK-saved-text",
    "a stored message": "LEAK-stored-message",
    "the payload of an event": "LEAK-event-payload",
}

# The two public items of the owner, the newest first: the order of the feed.
PERSIAN_SLUG = "queues-fa"
LEASES_SLUG = "leases-for-job-queues"
LEASES_NOTE = "Worth a read before the design review."
LEASES_RECORD: dict[str, object] = {
    "id": LEASES_SLUG,
    "kind": "article",
    "title": "Leases for Job Queues in Postgres",
    "url": "https://example.com/leases",
    "author": "A. Made-up",
    "site": "example.com",
    "lang": "en",
    "summary": "A job queue on one Postgres table, made safe with leases.",
    "key_points": ["A lease ends when its worker stops"],
    "est_minutes": 14,
    "note": LEASES_NOTE,
    "published_at": "2026-10-21T07:00:00Z",
    "tags": ["databases"],
}


@dataclass(frozen=True, slots=True)
class World:
    owner_id: int
    other_owner_id: int
    leases_id: int
    private_id: int


async def seed_world(sessions: async_sessionmaker[AsyncSession]) -> World:
    """Commit the made-up world. Each private marker of ``PRIVATE`` is in its place."""
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
        other_owner_id = await auth.ensure_owner(db, OTHER_TELEGRAM_ID)

        def item(owner: int = owner_id, /, **fields: object) -> Item:
            return Item(owner_id=owner, first_captured_at=NOW, last_captured_at=NOW, **fields)

        def public(slug: str, published_at: datetime, **fields: object) -> dict[str, object]:
            return {
                "visibility": Visibility.PUBLIC,
                "public_slug": slug,
                "published_at": published_at,
            } | fields

        leases = item(
            **public(LEASES_SLUG, NOW - timedelta(days=1)),
            kind=ItemKind.ARTICLE,
            title=LEASES_RECORD["title"],
            canonical_url=LEASES_RECORD["url"],
            author=LEASES_RECORD["author"],
            site=LEASES_RECORD["site"],
            lang="en",
            summary=LEASES_RECORD["summary"],
            key_points=LEASES_RECORD["key_points"],
            est_minutes=14,
            public_note=LEASES_RECORD["note"],
            note=PRIVATE["the private note of a public item"],
            extra_links=[PRIVATE["a link in the extra links of a public item"]],
        )
        persian = item(
            **public(PERSIAN_SLUG, NOW),
            kind=ItemKind.ARTICLE,
            title="صف کارها در پستگرس",
            lang="fa",
            summary="یک صف کار روی یک جدول Postgres.",
        )
        private = item(
            title=PRIVATE["the title of a private item"],
            public_note=PRIVATE["the public note of a private item"],
        )
        # Public before, private now: it keeps its slug and its time of publication.
        unpublished = item(
            title=PRIVATE["an item that was public and is private now"],
            public_slug="was-public",
            published_at=NOW - timedelta(days=3),
        )
        others = item(
            other_owner_id,
            **public("other-owner-item", NOW),
            title=PRIVATE["a public item of another owner"],
        )
        db.add_all([leases, persian, private, unpublished, others])
        await db.flush()
        # A copy of a public item, marked public too: the view leaves copies out.
        db.add(
            item(
                **public("copy-of-leases", NOW),
                title=PRIVATE["a public copy of a public item"],
                duplicate_of_id=leases.id,
            )
        )

        open_tag = Tag(owner_id=owner_id, slug="databases", name="Databases", is_public=True)
        hidden_tag = Tag(
            owner_id=owner_id,
            slug=PRIVATE["the slug of a private tag of a public item"],
            name=PRIVATE["the name of that private tag"],
        )
        goal = Goal(owner_id=owner_id, slug="goal", title=PRIVATE["the title of a goal"])
        db.add_all([open_tag, hidden_tag, goal])
        await db.flush()
        db.add_all(
            [
                ItemTag(item_id=leases.id, tag_id=open_tag.id, owner_id=owner_id),
                ItemTag(item_id=leases.id, tag_id=hidden_tag.id, owner_id=owner_id),
                ItemGoal(
                    item_id=leases.id,
                    goal_id=goal.id,
                    owner_id=owner_id,
                    score=3,
                    reason=PRIVATE["the reason of a score"],
                    scorer_version="1",
                    goal_version=1,
                ),
                ItemContent(
                    item_id=leases.id,
                    owner_id=owner_id,
                    text=PRIVATE["the saved text of a public item"],
                    content_hash="made-up",
                    extractor="article",
                    extractor_version=1,
                ),
                Capture(
                    owner_id=owner_id,
                    source=CaptureSource.BOT,
                    chat_id=-1001234567890,
                    message_id=1,
                    posted_at=NOW,
                    text=PRIVATE["a stored message"],
                ),
                Event(
                    owner_id=owner_id,
                    item_id=leases.id,
                    actor=Actor.USER,
                    type="note",
                    payload={"text": PRIVATE["the payload of an event"]},
                ),
            ]
        )
        await db.commit()
        return World(owner_id, other_owner_id, leases.id, private.id)


# --- Git repositories --------------------------------------------------------------------

# The other writers of a repository, such as the person who writes the site. Their git config
# stays out too, so that a test does the same on each computer.
_OTHERS = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "LC_ALL": "C",
    "GIT_AUTHOR_NAME": "Someone Else",
    "GIT_AUTHOR_EMAIL": "else@example.com",
    "GIT_COMMITTER_NAME": "Someone Else",
    "GIT_COMMITTER_EMAIL": "else@example.com",
}


def run_git(*args: str, cwd: Path) -> str:
    done = subprocess.run(  # noqa: S603 - git with the arguments of this test
        ["git", *args],  # noqa: S607 - the git of this computer, as the target uses
        cwd=cwd,
        env=_OTHERS,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def bare_remote(root: Path) -> Path:
    """An empty bare repository, as on a server. Its HEAD is main, so a clone checks out main."""
    root.mkdir(parents=True, exist_ok=True)
    remote = root / "site.git"
    run_git("init", "--quiet", "--bare", "--initial-branch=main", str(remote), cwd=root)
    return remote
