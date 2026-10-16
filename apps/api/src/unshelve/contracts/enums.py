"""Closed sets of values. The database stores them as text with CHECK constraints."""

from enum import StrEnum


class ItemStatus(StrEnum):
    """Reading state. Only the user changes it."""

    INBOX = "inbox"
    QUEUED = "queued"
    READING = "reading"
    DONE = "done"
    ARCHIVED = "archived"


class EnrichState(StrEnum):
    """Processing state. Only workers change it."""

    PENDING = "pending"
    FETCHED = "fetched"
    ENRICHED = "enriched"
    READY = "ready"
    FAILED = "failed"


class ItemKind(StrEnum):
    ARTICLE = "article"
    VIDEO = "video"
    POST = "post"
    REPO = "repo"
    PAPER = "paper"
    IMAGE = "image"
    NOTE = "note"
    BOOK = "book"
    PDF = "pdf"
    UNKNOWN = "unknown"


class Visibility(StrEnum):
    PRIVATE = "private"
    PUBLIC = "public"


class CaptureSource(StrEnum):
    BOT = "bot"
    EXPORT = "export"
    API = "api"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"


class BatchState(StrEnum):
    """The life of a batch of questions to the model."""

    SENDING = "sending"  # stored, before the provider has it: its items wait
    SENT = "sent"  # the provider answers it within 24 hours
    COLLECTED = "collected"  # its answers are in the ledger
    FAILED = "failed"  # the provider refused it: its items wait for the next batch


class JobKind(StrEnum):
    INGEST_CAPTURE = "ingest_capture"
    INGEST_ALBUM = "ingest_album"
    FETCH_ITEM = "fetch_item"
    ENRICH_ITEM = "enrich_item"
    EMBED_ITEM = "embed_item"
    SCORE_ITEM = "score_item"
    SEND_DIGEST = "send_digest"
    STALE_SWEEP = "stale_sweep"
    PUBLISH_FEED = "publish_feed"
    # Reads the answers of a batch of questions to the model when the batch has ended.
    COLLECT_BATCH = "collect_batch"


class GoalLabel(StrEnum):
    """The user's verdict on a relevance score. No label means "not reviewed"."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class LabelSource(StrEnum):
    """Where a label was given. Labels from a digest are biased towards surfaced items."""

    DM = "dm"
    WEB = "web"
    DIGEST = "digest"
    IMPORT = "import"


class Actor(StrEnum):
    USER = "user"
    WORKER = "worker"
    SYSTEM = "system"


class ChunkKind(StrEnum):
    CARD = "card"
    BODY = "body"


class TagSource(StrEnum):
    LLM = "llm"
    USER = "user"


class Distance(StrEnum):
    COSINE = "cosine"
    L2 = "l2"
    IP = "ip"
