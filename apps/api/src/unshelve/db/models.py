"""ORM models.

The schema itself is created by the migrations. ``alembic check`` (run by the tests) fails
when the models and the migrations differ. Things the ORM does not model live only in the
migration: extensions, ``u_norm()``, the ``pub`` schema and its view, roles, and triggers.

Every user-owned table has ``owner_id`` (docs/adr/0005).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column

from unshelve.contracts.enums import (
    Actor,
    CaptureSource,
    ChunkKind,
    Distance,
    EnrichState,
    GoalLabel,
    ItemKind,
    ItemStatus,
    JobKind,
    JobState,
    LabelSource,
    TagSource,
    Visibility,
)
from unshelve.db.base import Base
from unshelve.db.types import StrEnumType, enum_check

TIMESTAMP = sa.DateTime(timezone=True)
EMPTY_LIST = sa.text("'[]'::jsonb")
EMPTY_OBJECT = sa.text("'{}'::jsonb")

# Card fields, weighted. The body text has its own vector on item_contents.
ITEM_FTS = (
    "setweight(to_tsvector('english'::regconfig, u_norm(coalesce(title, ''))), 'A') || "
    "setweight(to_tsvector('english'::regconfig, u_norm(coalesce(summary, ''))), 'B') || "
    "setweight(to_tsvector('english'::regconfig, "
    "u_norm(coalesce(author, '') || ' ' || coalesce(site, ''))), 'C')"
)
# A tsvector is limited to 1 MB, so only the start of a long text is indexed.
CONTENT_FTS = "to_tsvector('english'::regconfig, u_norm(left(text, 100000)))"


def _pk() -> MappedColumn[int]:
    return mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)


def _owner(*, nullable: bool = False) -> MappedColumn[Any]:
    return mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=nullable
    )


def _item_fk(*, primary_key: bool = False, nullable: bool = False) -> MappedColumn[Any]:
    return mapped_column(
        sa.BigInteger,
        sa.ForeignKey("items.id", ondelete="RESTRICT"),
        primary_key=primary_key,
        nullable=nullable,
    )


def _created_at() -> MappedColumn[datetime]:
    return mapped_column(TIMESTAMP, server_default=sa.func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (sa.CheckConstraint("queue_limit > 0", name="queue_limit_positive"),)

    id: Mapped[int] = _pk()
    telegram_user_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True)
    display_name: Mapped[str | None] = mapped_column(sa.Text)
    timezone: Mapped[str] = mapped_column(sa.Text, server_default="UTC")
    queue_limit: Mapped[int] = mapped_column(sa.Integer, server_default="5")
    created_at: Mapped[datetime] = _created_at()


class Channel(Base):
    """A chat that the bot captures from."""

    __tablename__ = "channels"

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()
    chat_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True)
    title: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()


class EmbeddingSet(Base):
    """One embedding space. Instance-wide; exactly one is active (docs/adr/0007)."""

    __tablename__ = "embedding_sets"
    __table_args__ = (
        sa.UniqueConstraint("provider", "model", "dim"),
        sa.CheckConstraint("dim > 0", name="dim_positive"),
        enum_check("distance", Distance),
        sa.Index(
            "uq_embedding_sets_active", "is_active", unique=True, postgresql_where="is_active"
        ),
    )

    id: Mapped[int] = _pk()
    provider: Mapped[str] = mapped_column(sa.Text)
    model: Mapped[str] = mapped_column(sa.Text)
    dim: Mapped[int] = mapped_column(sa.Integer)
    distance: Mapped[Distance] = mapped_column(StrEnumType(Distance), server_default="cosine")
    doc_prefix: Mapped[str] = mapped_column(sa.Text, server_default="")
    query_prefix: Mapped[str] = mapped_column(sa.Text, server_default="")
    is_active: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    created_at: Mapped[datetime] = _created_at()


class Item(Base):
    """One thing worth reading. Never deleted."""

    __tablename__ = "items"
    __table_args__ = (
        enum_check("kind", ItemKind),
        enum_check("status", ItemStatus),
        enum_check("enrich_state", EnrichState),
        enum_check("visibility", Visibility),
        sa.CheckConstraint(
            "visibility <> 'public' OR (published_at IS NOT NULL AND public_slug IS NOT NULL)",
            name="public_needs_publication",
        ),
        sa.CheckConstraint("status <> 'done' OR read_at IS NOT NULL", name="done_needs_read_at"),
        sa.CheckConstraint("capture_count >= 1", name="capture_count_positive"),
        sa.CheckConstraint(
            "duplicate_of_id IS NULL OR duplicate_of_id <> id", name="not_own_duplicate"
        ),
        sa.Index("ix_items_owner_status_captured", "owner_id", "status", "last_captured_at"),
        sa.Index("ix_items_fts", "fts", postgresql_using="gin"),
        # Trigram match on titles: the recall net for Persian, which has no stemmer.
        sa.Index(
            "ix_items_title_trgm",
            sa.literal_column("u_norm(title)", sa.Text).label("title_norm"),
            postgresql_using="gin",
            postgresql_ops={"title_norm": "gin_trgm_ops"},
        ),
        sa.Index(
            "ix_items_public",
            "owner_id",
            "published_at",
            postgresql_where="visibility = 'public'",
        ),
        sa.Index(
            "ix_items_duplicate_of",
            "duplicate_of_id",
            postgresql_where="duplicate_of_id IS NOT NULL",
        ),
    )

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()

    kind: Mapped[ItemKind] = mapped_column(StrEnumType(ItemKind), server_default="unknown")
    status: Mapped[ItemStatus] = mapped_column(StrEnumType(ItemStatus), server_default="inbox")
    enrich_state: Mapped[EnrichState] = mapped_column(
        StrEnumType(EnrichState), server_default="pending"
    )
    # True when enrichment ran on the saved text because the source could not be fetched.
    partial: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    enrich_version: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    visibility: Mapped[Visibility] = mapped_column(
        StrEnumType(Visibility), server_default="private"
    )

    canonical_url: Mapped[str | None] = mapped_column(sa.Text)
    title: Mapped[str | None] = mapped_column(sa.Text)
    author: Mapped[str | None] = mapped_column(sa.Text)
    site: Mapped[str | None] = mapped_column(sa.Text)
    lang: Mapped[str | None] = mapped_column(sa.Text)
    summary: Mapped[str | None] = mapped_column(sa.Text)
    key_points: Mapped[list[str]] = mapped_column(JSONB, server_default=EMPTY_LIST)
    est_minutes: Mapped[int | None] = mapped_column(sa.Integer)
    extra_links: Mapped[list[str]] = mapped_column(JSONB, server_default=EMPTY_LIST)

    note: Mapped[str | None] = mapped_column(sa.Text)  # private; never published
    public_note: Mapped[str | None] = mapped_column(sa.Text)  # the only user text in the feed
    public_slug: Mapped[str | None] = mapped_column(sa.Text, unique=True)

    # A repeated save raises capture_count instead of creating a second item.
    capture_count: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    first_captured_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    last_captured_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    duplicate_of_id: Mapped[int | None] = _item_fk(nullable=True)

    last_surfaced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    read_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=sa.func.now(), onupdate=sa.func.now()
    )

    fts: Mapped[Any] = mapped_column(TSVECTOR, sa.Computed(ITEM_FTS, persisted=True))


class Capture(Base):
    """One saved message, as received. The raw payload is kept so it can be re-read later."""

    __tablename__ = "captures"
    __table_args__ = (
        sa.UniqueConstraint(
            "owner_id", "chat_id", "message_id", "edit_seq", name="uq_captures_message"
        ),
        enum_check("source", CaptureSource),
        sa.Index("ix_captures_item_id", "item_id"),
        sa.Index(
            "ix_captures_album",
            "chat_id",
            "media_group_id",
            postgresql_where="media_group_id IS NOT NULL",
        ),
    )

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()
    source: Mapped[CaptureSource] = mapped_column(StrEnumType(CaptureSource))
    chat_id: Mapped[int] = mapped_column(sa.BigInteger)
    message_id: Mapped[int] = mapped_column(sa.BigInteger)
    edit_seq: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    media_group_id: Mapped[str | None] = mapped_column(sa.Text)
    forward_origin: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    posted_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    text: Mapped[str] = mapped_column(sa.Text, server_default="")
    urls: Mapped[list[str]] = mapped_column(JSONB, server_default=EMPTY_LIST)
    media: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=EMPTY_LIST)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    item_id: Mapped[int | None] = _item_fk(nullable=True)
    created_at: Mapped[datetime] = _created_at()


class ItemKey(Base):
    """An alias that identifies an item: a URL, a file, an album (docs/adr/0003)."""

    __tablename__ = "item_keys"
    __table_args__ = (sa.Index("ix_item_keys_item_id", "item_id"),)

    owner_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    item_id: Mapped[int] = _item_fk()
    created_at: Mapped[datetime] = _created_at()


class ItemContent(Base):
    """The extracted text of an item. Untrusted: it came from the internet."""

    __tablename__ = "item_contents"
    __table_args__ = (sa.Index("ix_item_contents_fts", "fts", postgresql_using="gin"),)

    item_id: Mapped[int] = _item_fk(primary_key=True)
    owner_id: Mapped[int] = _owner()
    text: Mapped[str] = mapped_column(sa.Text)
    content_hash: Mapped[str] = mapped_column(sa.Text)
    extractor: Mapped[str] = mapped_column(sa.Text)
    extractor_version: Mapped[int] = mapped_column(sa.Integer)
    final_url: Mapped[str | None] = mapped_column(sa.Text)
    fetched_at: Mapped[datetime] = _created_at()
    fts: Mapped[Any] = mapped_column(TSVECTOR, sa.Computed(CONTENT_FTS, persisted=True))


class Asset(Base):
    """A stored media file. The bytes are in the BlobStore under ``storage_key``."""

    __tablename__ = "assets"
    __table_args__ = (sa.UniqueConstraint("owner_id", "sha256", name="uq_assets_content"),)

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()
    sha256: Mapped[str] = mapped_column(sa.Text)
    mime: Mapped[str] = mapped_column(sa.Text)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger)
    storage_key: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()


class Chunk(Base):
    """A piece of an item with its vector. ``card`` is the item's summary card."""

    __tablename__ = "chunks"
    __table_args__ = (
        sa.UniqueConstraint(
            "item_id", "embedding_set_id", "kind", "ord", name="uq_chunks_position"
        ),
        sa.CheckConstraint("vector_dims(embedding) = dim", name="dim_matches_vector"),
        enum_check("kind", ChunkKind),
        sa.Index("ix_chunks_owner_set_kind", "owner_id", "embedding_set_id", "kind"),
    )

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()
    item_id: Mapped[int] = _item_fk()
    embedding_set_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("embedding_sets.id", ondelete="RESTRICT")
    )
    kind: Mapped[ChunkKind] = mapped_column(StrEnumType(ChunkKind))
    ord: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    text: Mapped[str] = mapped_column(sa.Text)
    char_start: Mapped[int | None] = mapped_column(sa.Integer)
    char_end: Mapped[int | None] = mapped_column(sa.Integer)
    dim: Mapped[int] = mapped_column(sa.Integer)
    # No fixed dimension: a model change is a backfill, not a migration.
    embedding: Mapped[Any] = mapped_column(Vector())
    created_at: Mapped[datetime] = _created_at()


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        sa.UniqueConstraint("owner_id", "slug", name="uq_tags_slug"),
        enum_check("source", TagSource),
    )

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()
    slug: Mapped[str] = mapped_column(sa.Text)
    name: Mapped[str] = mapped_column(sa.Text)
    # Only public tags can appear in the feed. A tag can describe the user's plans.
    is_public: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    source: Mapped[TagSource] = mapped_column(StrEnumType(TagSource), server_default="llm")
    created_at: Mapped[datetime] = _created_at()


class ItemTag(Base):
    __tablename__ = "item_tags"
    __table_args__ = (
        enum_check("source", TagSource),
        sa.Index("ix_item_tags_tag_id", "tag_id"),
    )

    item_id: Mapped[int] = _item_fk(primary_key=True)
    tag_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("tags.id", ondelete="RESTRICT"), primary_key=True
    )
    owner_id: Mapped[int] = _owner()
    source: Mapped[TagSource] = mapped_column(StrEnumType(TagSource), server_default="llm")
    created_at: Mapped[datetime] = _created_at()


class Goal(Base):
    """Something the user is working towards. Private: goal text never reaches the feed."""

    __tablename__ = "goals"
    __table_args__ = (
        sa.UniqueConstraint("owner_id", "slug", name="uq_goals_slug"),
        # At most one focus goal per owner.
        sa.Index("uq_goals_focus", "owner_id", unique=True, postgresql_where="is_focus"),
    )

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()
    slug: Mapped[str] = mapped_column(sa.Text)
    title: Mapped[str] = mapped_column(sa.Text)
    description: Mapped[str] = mapped_column(sa.Text, server_default="")
    keywords: Mapped[list[str]] = mapped_column(JSONB, server_default=EMPTY_LIST)
    starts_on: Mapped[date | None] = mapped_column(sa.Date)
    ends_on: Mapped[date | None] = mapped_column(sa.Date)
    version: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    is_focus: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = _created_at()


class ItemGoal(Base):
    """How relevant an item is to a goal, and what the user said about that score."""

    __tablename__ = "item_goals"
    __table_args__ = (
        sa.CheckConstraint("score BETWEEN 0 AND 3", name="score_range"),
        sa.CheckConstraint("(label IS NULL) = (label_source IS NULL)", name="label_has_source"),
        enum_check("label", GoalLabel),
        enum_check("label_source", LabelSource),
        sa.Index("ix_item_goals_goal_score", "goal_id", "score"),
    )

    item_id: Mapped[int] = _item_fk(primary_key=True)
    goal_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("goals.id", ondelete="RESTRICT"), primary_key=True
    )
    owner_id: Mapped[int] = _owner()
    score: Mapped[int] = mapped_column(sa.SmallInteger)
    reason: Mapped[str] = mapped_column(sa.Text, server_default="")
    # NULL means "not reviewed", which is different from "rejected".
    label: Mapped[GoalLabel | None] = mapped_column(StrEnumType(GoalLabel))
    label_source: Mapped[LabelSource | None] = mapped_column(StrEnumType(LabelSource))
    labelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    scorer_version: Mapped[str] = mapped_column(sa.Text)
    goal_version: Mapped[int] = mapped_column(sa.Integer)
    scored_at: Mapped[datetime] = _created_at()


class Job(Base):
    """A unit of work. See docs/adr/0002 for the lease model."""

    __tablename__ = "jobs"
    __table_args__ = (
        enum_check("kind", JobKind),
        enum_check("state", JobState),
        sa.CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        # A running job always has a lease; a job in any other state never has one.
        sa.CheckConstraint(
            "(state = 'running') = (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name="running_has_lease",
        ),
        sa.CheckConstraint("(locked_by IS NULL) = (locked_until IS NULL)", name="lease_is_whole"),
        # Enqueueing the same work twice while it waits is coalesced into one job.
        sa.Index(
            "uq_jobs_pending_dedupe",
            "kind",
            "dedupe_key",
            unique=True,
            postgresql_where="state = 'pending' AND dedupe_key IS NOT NULL",
        ),
        # The claim query: lowest priority value first, then oldest.
        sa.Index(
            "ix_jobs_claim", "priority", "run_after", "id", postgresql_where="state = 'pending'"
        ),
        sa.Index("ix_jobs_lease", "locked_until", postgresql_where="state = 'running'"),
    )

    id: Mapped[int] = _pk()
    owner_id: Mapped[int | None] = _owner(nullable=True)
    kind: Mapped[JobKind] = mapped_column(StrEnumType(JobKind))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    state: Mapped[JobState] = mapped_column(StrEnumType(JobState), server_default="pending")
    priority: Mapped[int] = mapped_column(sa.SmallInteger, server_default="0")  # lower runs first
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(sa.Integer, server_default="5")
    run_after: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=sa.func.now())
    locked_by: Mapped[str | None] = mapped_column(sa.Text)
    locked_until: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    dedupe_key: Mapped[str | None] = mapped_column(sa.Text)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=sa.func.now(), onupdate=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class Event(Base):
    """What happened to an item. Append-only: a trigger rejects UPDATE, DELETE and TRUNCATE."""

    __tablename__ = "events"
    __table_args__ = (
        enum_check("actor", Actor),
        sa.Index("ix_events_item", "item_id", "id"),
    )

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()
    item_id: Mapped[int] = _item_fk()
    actor: Mapped[Actor] = mapped_column(StrEnumType(Actor))
    type: Mapped[str] = mapped_column(sa.Text)
    from_status: Mapped[str | None] = mapped_column(sa.Text)
    to_status: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    created_at: Mapped[datetime] = _created_at()


class AuthToken(Base):
    """A one-time login token. Only its hash is stored."""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()
    token_hash: Mapped[str] = mapped_column(sa.Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = _created_at()


class WebSession(Base):
    """A browser session. A row, not a signed cookie, so it can be revoked."""

    __tablename__ = "sessions"

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()
    session_hash: Mapped[str] = mapped_column(sa.Text, unique=True)
    user_agent: Mapped[str | None] = mapped_column(sa.Text)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    last_seen_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = _created_at()


class LLMCall(Base):
    """Every model call: the retry cache, the cost ledger, and the replay source for evals."""

    __tablename__ = "llm_calls"
    __table_args__ = (sa.Index("ix_llm_calls_task_created", "task", "created_at"),)

    id: Mapped[int] = _pk()
    owner_id: Mapped[int | None] = _owner(nullable=True)
    cache_key: Mapped[str] = mapped_column(sa.Text, unique=True)
    task: Mapped[str] = mapped_column(sa.Text)
    model: Mapped[str] = mapped_column(sa.Text)
    prompt_version: Mapped[str] = mapped_column(sa.Text)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    input_tokens: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    created_at: Mapped[datetime] = _created_at()


class Digest(Base):
    """One sent digest. The unique key makes sending idempotent."""

    __tablename__ = "digests"
    __table_args__ = (sa.UniqueConstraint("owner_id", "period_key", name="uq_digests_period"),)

    id: Mapped[int] = _pk()
    owner_id: Mapped[int] = _owner()
    period_key: Mapped[str] = mapped_column(sa.Text)
    item_ids: Mapped[list[int]] = mapped_column(JSONB, server_default=EMPTY_LIST)
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = _created_at()
