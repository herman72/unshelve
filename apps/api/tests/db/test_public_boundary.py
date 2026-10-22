"""The publisher role sees the public view and nothing else (docs/adr/0004)."""

import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import Visibility
from unshelve.db.models import Goal, Item, ItemGoal, ItemTag, Tag, User

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
SECRET = "SENTINEL-MUST-NOT-LEAK"  # noqa: S105 - a marker for the test, not a credential

# If this list changes, something new became public. That must be a decision, not an accident.
PUBLIC_COLUMNS = [
    "owner_id",
    "id",
    "kind",
    "title",
    "url",
    "author",
    "site",
    "lang",
    "summary",
    "key_points",
    "est_minutes",
    "note",
    "published_at",
    "tags",
]


async def _seed(db: AsyncSession) -> None:
    owner = User(telegram_user_id=1)
    db.add(owner)
    await db.flush()

    def item(**fields: object) -> Item:
        return Item(owner_id=owner.id, first_captured_at=NOW, last_captured_at=NOW, **fields)

    public = item(
        title="A public article",
        summary="Worth sharing.",
        note=f"{SECRET} private note",
        public_note="My public comment.",
        visibility=Visibility.PUBLIC,
        public_slug="pub-1",
        published_at=NOW,
    )
    private = item(title=f"{SECRET} private item", summary=f"{SECRET} summary")
    open_tag = Tag(owner_id=owner.id, slug="databases", name="Databases", is_public=True)
    hidden_tag = Tag(owner_id=owner.id, slug=f"{SECRET}-job-hunt".lower(), name="Job hunt")
    goal = Goal(owner_id=owner.id, slug="goal", title=f"{SECRET} goal title")
    db.add_all([public, private, open_tag, hidden_tag, goal])
    await db.flush()
    db.add_all(
        [
            ItemTag(item_id=public.id, tag_id=open_tag.id, owner_id=owner.id),
            ItemTag(item_id=public.id, tag_id=hidden_tag.id, owner_id=owner.id),
            ItemGoal(
                item_id=public.id,
                goal_id=goal.id,
                owner_id=owner.id,
                score=3,
                reason=f"{SECRET} reason",
                scorer_version="1",
                goal_version=1,
            ),
        ]
    )
    await db.flush()


async def test_the_public_view_has_exactly_the_agreed_columns(db: AsyncSession) -> None:
    columns = await db.scalars(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'pub' AND table_name = 'items' ORDER BY ordinal_position"
        )
    )
    assert list(columns) == PUBLIC_COLUMNS


async def test_the_publisher_sees_public_items_only_and_no_private_text(db: AsyncSession) -> None:
    await _seed(db)
    await db.execute(sa.text("SET LOCAL ROLE unshelve_publisher"))

    rows = (await db.execute(sa.text("SELECT * FROM pub.items"))).mappings().all()

    assert [row["id"] for row in rows] == ["pub-1"]
    assert rows[0]["note"] == "My public comment."
    assert rows[0]["tags"] == ["databases"]
    feed = json.dumps([dict(row) for row in rows], default=str)
    assert SECRET.lower() not in feed.lower()


@pytest.mark.parametrize("table", ["items", "goals", "item_goals", "tags", "captures", "users"])
async def test_the_publisher_cannot_read_base_tables(db: AsyncSession, table: str) -> None:
    await db.execute(sa.text("SET LOCAL ROLE unshelve_publisher"))

    with pytest.raises(ProgrammingError, match="permission denied"):
        await db.execute(sa.text(f"SELECT * FROM {table} LIMIT 1"))  # noqa: S608 - fixed names


async def test_the_publisher_has_no_right_on_any_base_table(db: AsyncSession) -> None:
    # Each table, also one that a later migration adds: none may give the role a right.
    rights = await db.execute(
        sa.text(
            "SELECT table_name, has_table_privilege('unshelve_publisher', "
            "format('public.%I', table_name), 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE') "
            "AS granted FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    )
    tables = {row.table_name: row.granted for row in rights}

    assert len(tables) > 20  # the query found the tables, so an empty list cannot pass
    assert [name for name, granted in tables.items() if granted] == []


async def test_the_pub_schema_holds_the_public_view_and_nothing_else(db: AsyncSession) -> None:
    objects = await db.scalars(
        sa.text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'pub' ORDER BY c.relname"
        )
    )

    assert list(objects) == ["items"]


async def test_the_publisher_can_only_read_the_public_view(db: AsyncSession) -> None:
    def right(name: str) -> sa.TextClause:
        return sa.text(f"SELECT has_table_privilege('unshelve_publisher', 'pub.items', '{name}')")

    assert await db.scalar(right("SELECT")) is True
    for name in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert await db.scalar(right(name)) is False, name
