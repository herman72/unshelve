"""Initial schema: the whole of version 1.

Besides the tables, this migration creates what the ORM does not model:

* extensions ``vector`` and ``pg_trgm``
* ``u_norm()``, the text normaliser used by generated columns, indexes and queries
* the append-only trigger on ``events``
* the ``unshelve_publisher`` role and the ``pub`` schema with its one view

The downgrade leaves the extensions and the role in place: both are shared by every
database in the cluster.

Revision ID: 0001
Revises:
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Letter forms and digits that Arabic and Persian keyboards produce for the same word:
# Arabic yeh, alef maksura, kaf, teh marbuta, heh with yeh; Arabic-Indic and Persian digits;
# and the zero-width non-joiner, which becomes a space.
_FROM = "يىكةۀ٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹‌"
_TO = "ییکهه" + "0123456789" + "0123456789" + " "

# IMMUTABLE, so it can be used in generated columns and expression indexes. The regular
# expression removes diacritics, the tatweel and the zero-width joiner.
U_NORM = f"""
CREATE FUNCTION u_norm(t text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN lower(
    regexp_replace(
        translate(t, '{_FROM}', '{_TO}'),
        '[\\u064B-\\u065F\\u0670\\u0640\\u200D]', '', 'g'
    )
)
"""

FORBID_CHANGE = """
CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END
$$
"""

# Roles are cluster-wide, so another database may have created it already.
PUBLISHER_ROLE = """
DO $$
BEGIN
    CREATE ROLE unshelve_publisher NOLOGIN;
EXCEPTION
    WHEN duplicate_object OR unique_violation THEN NULL;
END
$$
"""

# The public record of an item. Every column is listed on purpose: a new column on items
# is private until someone adds it here. Private notes, goals and non-public tags are not
# reachable from this view.
PUB_ITEMS_VIEW = """
CREATE VIEW pub.items AS
SELECT
    i.owner_id,
    i.public_slug   AS id,
    i.kind,
    i.title,
    i.canonical_url AS url,
    i.author,
    i.site,
    i.lang,
    i.summary,
    i.key_points,
    i.est_minutes,
    i.public_note   AS note,
    i.published_at,
    COALESCE(
        (
            SELECT jsonb_agg(t.slug ORDER BY t.slug)
            FROM item_tags it
            JOIN tags t ON t.id = it.tag_id
            WHERE it.item_id = i.id AND t.is_public
        ),
        '[]'::jsonb
    ) AS tags
FROM items i
WHERE i.visibility = 'public' AND i.duplicate_of_id IS NULL
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(U_NORM)
    # Message ids for captures that did not come from Telegram (POST /items).
    op.execute("CREATE SEQUENCE capture_api_seq")

    op.create_table(
        "embedding_sets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("distance", sa.Text(), server_default="cosine", nullable=False),
        sa.Column("doc_prefix", sa.Text(), server_default="", nullable=False),
        sa.Column("query_prefix", sa.Text(), server_default="", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "distance IN ('cosine', 'l2', 'ip')", name=op.f("ck_embedding_sets_distance_valid")
        ),
        sa.CheckConstraint("dim > 0", name=op.f("ck_embedding_sets_dim_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding_sets")),
        sa.UniqueConstraint("provider", "model", "dim", name=op.f("uq_embedding_sets_provider")),
    )
    op.create_index(
        "uq_embedding_sets_active",
        "embedding_sets",
        ["is_active"],
        unique=True,
        postgresql_where="is_active",
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), server_default="UTC", nullable=False),
        sa.Column("queue_limit", sa.Integer(), server_default="5", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("queue_limit > 0", name=op.f("ck_users_queue_limit_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("telegram_user_id", name=op.f("uq_users_telegram_user_id")),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_assets_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assets")),
        sa.UniqueConstraint("owner_id", "sha256", name="uq_assets_content"),
    )
    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_auth_tokens_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_tokens_token_hash")),
    )
    op.create_table(
        "channels",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_channels_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channels")),
        sa.UniqueConstraint("chat_id", name=op.f("uq_channels_chat_id")),
    )
    op.create_table(
        "digests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("period_key", sa.Text(), nullable=False),
        sa.Column(
            "item_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_digests_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_digests")),
        sa.UniqueConstraint("owner_id", "period_key", name="uq_digests_period"),
    )
    op.create_table(
        "goals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "keywords",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_focus", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_goals_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goals")),
        sa.UniqueConstraint("owner_id", "slug", name="uq_goals_slug"),
    )
    op.create_index(
        "uq_goals_focus", "goals", ["owner_id"], unique=True, postgresql_where="is_focus"
    )
    op.create_table(
        "items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("status", sa.Text(), server_default="inbox", nullable=False),
        sa.Column("enrich_state", sa.Text(), server_default="pending", nullable=False),
        sa.Column("partial", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("enrich_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("visibility", sa.Text(), server_default="private", nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("site", sa.Text(), nullable=True),
        sa.Column("lang", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "key_points",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("est_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "extra_links",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("public_note", sa.Text(), nullable=True),
        sa.Column("public_slug", sa.Text(), nullable=True),
        sa.Column("capture_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("first_captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duplicate_of_id", sa.BigInteger(), nullable=True),
        sa.Column("last_surfaced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "fts",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english'::regconfig, u_norm(coalesce(title, ''))), 'A') || setweight(to_tsvector('english'::regconfig, u_norm(coalesce(summary, ''))), 'B') || setweight(to_tsvector('english'::regconfig, u_norm(coalesce(author, '') || ' ' || coalesce(site, ''))), 'C')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.CheckConstraint(
            "enrich_state IN ('pending', 'fetched', 'enriched', 'ready', 'failed')",
            name=op.f("ck_items_enrich_state_valid"),
        ),
        sa.CheckConstraint(
            "kind IN ('article', 'video', 'post', 'repo', 'paper', 'image', 'note', 'book', 'pdf', 'unknown')",
            name=op.f("ck_items_kind_valid"),
        ),
        sa.CheckConstraint(
            "status <> 'done' OR read_at IS NOT NULL", name=op.f("ck_items_done_needs_read_at")
        ),
        sa.CheckConstraint(
            "status IN ('inbox', 'queued', 'reading', 'done', 'archived')",
            name=op.f("ck_items_status_valid"),
        ),
        sa.CheckConstraint(
            "visibility <> 'public' OR (published_at IS NOT NULL AND public_slug IS NOT NULL)",
            name=op.f("ck_items_public_needs_publication"),
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'public')", name=op.f("ck_items_visibility_valid")
        ),
        sa.CheckConstraint("capture_count >= 1", name=op.f("ck_items_capture_count_positive")),
        sa.CheckConstraint(
            "duplicate_of_id IS NULL OR duplicate_of_id <> id",
            name=op.f("ck_items_not_own_duplicate"),
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_id"],
            ["items.id"],
            name=op.f("fk_items_duplicate_of_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_items_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_items")),
        sa.UniqueConstraint("public_slug", name=op.f("uq_items_public_slug")),
    )
    op.create_index(
        "ix_items_duplicate_of",
        "items",
        ["duplicate_of_id"],
        unique=False,
        postgresql_where="duplicate_of_id IS NOT NULL",
    )
    op.create_index("ix_items_fts", "items", ["fts"], unique=False, postgresql_using="gin")
    op.create_index(
        "ix_items_owner_status_captured",
        "items",
        ["owner_id", "status", "last_captured_at"],
        unique=False,
    )
    op.create_index(
        "ix_items_public",
        "items",
        ["owner_id", "published_at"],
        unique=False,
        postgresql_where="visibility = 'public'",
    )
    op.execute("CREATE INDEX ix_items_title_trgm ON items USING gin (u_norm(title) gin_trgm_ops)")
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), server_default="pending", nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(state = 'running') = (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name=op.f("ck_jobs_running_has_lease"),
        ),
        sa.CheckConstraint(
            "kind IN ('ingest_capture', 'ingest_album', 'fetch_item', 'enrich_item', 'embed_item', 'score_item', 'send_digest', 'stale_sweep', 'publish_feed')",
            name=op.f("ck_jobs_kind_valid"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'done', 'dead')", name=op.f("ck_jobs_state_valid")
        ),
        sa.CheckConstraint(
            "(locked_by IS NULL) = (locked_until IS NULL)", name=op.f("ck_jobs_lease_is_whole")
        ),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_jobs_max_attempts_positive")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_jobs_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_claim",
        "jobs",
        ["priority", "run_after", "id"],
        unique=False,
        postgresql_where="state = 'pending'",
    )
    op.create_index(
        "ix_jobs_lease",
        "jobs",
        ["locked_until"],
        unique=False,
        postgresql_where="state = 'running'",
    )
    op.create_index(
        "uq_jobs_pending_dedupe",
        "jobs",
        ["kind", "dedupe_key"],
        unique=True,
        postgresql_where="state = 'pending' AND dedupe_key IS NOT NULL",
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=True),
        sa.Column("cache_key", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_llm_calls_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
        sa.UniqueConstraint("cache_key", name=op.f("uq_llm_calls_cache_key")),
    )
    op.create_index("ix_llm_calls_task_created", "llm_calls", ["task", "created_at"], unique=False)
    op.create_table(
        "sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("session_hash", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_sessions_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("session_hash", name=op.f("uq_sessions_session_hash")),
    )
    op.create_table(
        "tags",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_public", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source", sa.Text(), server_default="llm", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('llm', 'user')", name=op.f("ck_tags_source_valid")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_tags_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tags")),
        sa.UniqueConstraint("owner_id", "slug", name="uq_tags_slug"),
    )
    op.create_table(
        "captures",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("edit_seq", sa.Integer(), server_default="0", nullable=False),
        sa.Column("media_group_id", sa.Text(), nullable=True),
        sa.Column("forward_origin", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "urls",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "media",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("item_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('bot', 'export', 'api')", name=op.f("ck_captures_source_valid")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_captures_item_id_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_captures_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_captures")),
        sa.UniqueConstraint(
            "owner_id", "chat_id", "message_id", "edit_seq", name="uq_captures_message"
        ),
    )
    op.create_index(
        "ix_captures_album",
        "captures",
        ["chat_id", "media_group_id"],
        unique=False,
        postgresql_where="media_group_id IS NOT NULL",
    )
    op.create_index("ix_captures_item_id", "captures", ["item_id"], unique=False)
    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("embedding_set_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ord", sa.Integer(), server_default="0", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('card', 'body')", name=op.f("ck_chunks_kind_valid")),
        sa.CheckConstraint(
            "vector_dims(embedding) = dim", name=op.f("ck_chunks_dim_matches_vector")
        ),
        sa.ForeignKeyConstraint(
            ["embedding_set_id"],
            ["embedding_sets.id"],
            name=op.f("fk_chunks_embedding_set_id_embedding_sets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_chunks_item_id_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_chunks_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint(
            "item_id", "embedding_set_id", "kind", "ord", name="uq_chunks_position"
        ),
    )
    op.create_index(
        "ix_chunks_owner_set_kind", "chunks", ["owner_id", "embedding_set_id", "kind"], unique=False
    )
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor IN ('user', 'worker', 'system')", name=op.f("ck_events_actor_valid")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_events_item_id_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_events_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index("ix_events_item", "events", ["item_id", "id"], unique=False)
    op.create_table(
        "item_contents",
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("extractor", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Integer(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "fts",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english'::regconfig, u_norm(left(text, 100000)))", persisted=True
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_item_contents_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_item_contents_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("item_id", name=op.f("pk_item_contents")),
    )
    op.create_index(
        "ix_item_contents_fts", "item_contents", ["fts"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "item_goals",
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("goal_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("label_source", sa.Text(), nullable=True),
        sa.Column("labelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scorer_version", sa.Text(), nullable=False),
        sa.Column("goal_version", sa.Integer(), nullable=False),
        sa.Column(
            "scored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "label IN ('confirmed', 'rejected')", name=op.f("ck_item_goals_label_valid")
        ),
        sa.CheckConstraint(
            "label_source IN ('dm', 'web', 'digest', 'import')",
            name=op.f("ck_item_goals_label_source_valid"),
        ),
        sa.CheckConstraint(
            "(label IS NULL) = (label_source IS NULL)", name=op.f("ck_item_goals_label_has_source")
        ),
        sa.CheckConstraint("score BETWEEN 0 AND 3", name=op.f("ck_item_goals_score_range")),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name=op.f("fk_item_goals_goal_id_goals"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_item_goals_item_id_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_item_goals_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("item_id", "goal_id", name=op.f("pk_item_goals")),
    )
    op.create_index("ix_item_goals_goal_score", "item_goals", ["goal_id", "score"], unique=False)
    op.create_table(
        "item_keys",
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_item_keys_item_id_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_item_keys_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("owner_id", "key", name=op.f("pk_item_keys")),
    )
    op.create_index("ix_item_keys_item_id", "item_keys", ["item_id"], unique=False)
    op.create_table(
        "item_tags",
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), server_default="llm", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('llm', 'user')", name=op.f("ck_item_tags_source_valid")),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_item_tags_item_id_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_item_tags_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tags.id"], name=op.f("fk_item_tags_tag_id_tags"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("item_id", "tag_id", name=op.f("pk_item_tags")),
    )
    op.create_index("ix_item_tags_tag_id", "item_tags", ["tag_id"], unique=False)

    op.execute(FORBID_CHANGE)
    op.execute(
        "CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events "
        "FOR EACH ROW EXECUTE FUNCTION forbid_change()"
    )
    op.execute(
        "CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_change()"
    )

    op.execute(PUBLISHER_ROLE)
    op.execute("CREATE SCHEMA pub")
    op.execute(PUB_ITEMS_VIEW)
    op.execute("GRANT USAGE ON SCHEMA pub TO unshelve_publisher")
    op.execute("GRANT SELECT ON pub.items TO unshelve_publisher")


def downgrade() -> None:
    op.execute("DROP SCHEMA pub CASCADE")

    op.drop_index("ix_item_tags_tag_id", table_name="item_tags")
    op.drop_table("item_tags")
    op.drop_index("ix_item_keys_item_id", table_name="item_keys")
    op.drop_table("item_keys")
    op.drop_index("ix_item_goals_goal_score", table_name="item_goals")
    op.drop_table("item_goals")
    op.drop_index("ix_item_contents_fts", table_name="item_contents", postgresql_using="gin")
    op.drop_table("item_contents")
    op.drop_index("ix_events_item", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_chunks_owner_set_kind", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("ix_captures_item_id", table_name="captures")
    op.drop_index(
        "ix_captures_album", table_name="captures", postgresql_where="media_group_id IS NOT NULL"
    )
    op.drop_table("captures")
    op.drop_table("tags")
    op.drop_table("sessions")
    op.drop_index("ix_llm_calls_task_created", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_index(
        "uq_jobs_pending_dedupe",
        table_name="jobs",
        postgresql_where="state = 'pending' AND dedupe_key IS NOT NULL",
    )
    op.drop_index("ix_jobs_lease", table_name="jobs", postgresql_where="state = 'running'")
    op.drop_index("ix_jobs_claim", table_name="jobs", postgresql_where="state = 'pending'")
    op.drop_table("jobs")
    op.drop_index("ix_items_title_trgm", table_name="items", postgresql_using="gin")
    op.drop_index("ix_items_public", table_name="items", postgresql_where="visibility = 'public'")
    op.drop_index("ix_items_owner_status_captured", table_name="items")
    op.drop_index("ix_items_fts", table_name="items", postgresql_using="gin")
    op.drop_index(
        "ix_items_duplicate_of", table_name="items", postgresql_where="duplicate_of_id IS NOT NULL"
    )
    op.drop_table("items")
    op.drop_index("uq_goals_focus", table_name="goals", postgresql_where="is_focus")
    op.drop_table("goals")
    op.drop_table("digests")
    op.drop_table("channels")
    op.drop_table("auth_tokens")
    op.drop_table("assets")
    op.drop_table("users")
    op.drop_index(
        "uq_embedding_sets_active", table_name="embedding_sets", postgresql_where="is_active"
    )
    op.drop_table("embedding_sets")

    op.execute("DROP FUNCTION forbid_change()")
    op.execute("DROP SEQUENCE capture_api_seq")
    op.execute("DROP FUNCTION u_norm(text)")
