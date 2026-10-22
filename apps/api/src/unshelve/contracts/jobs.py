"""The job contract: payloads, the context a handler receives, and how a handler reports failure.

Rules for handlers (see docs/adr/0002):

* Delivery is at-least-once. A handler can run twice for the same job, so it is idempotent.
* A payload holds ids only. The handler reads current state from the database.
* A handler that calls the network does so outside a database transaction.
* A handler enqueues the next step in the same transaction that writes its own result.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from aiogram import Bot
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.config import Settings
from unshelve.contracts.enums import JobKind
from unshelve.contracts.protocols import (
    BatchProvider,
    BlobStore,
    Embedder,
    FeedTarget,
    Fetcher,
    FileSource,
    LLMProvider,
)

# --- Payloads ----------------------------------------------------------------------------


class JobPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IngestCapturePayload(JobPayload):
    capture_id: int


class IngestAlbumPayload(JobPayload):
    owner_id: int
    chat_id: int
    media_group_id: str


class ItemPayload(JobPayload):
    item_id: int


class FetchItemPayload(ItemPayload):
    pass


class EnrichItemPayload(ItemPayload):
    pass


class EmbedItemPayload(ItemPayload):
    pass


class ScoreItemPayload(ItemPayload):
    pass


class SendDigestPayload(JobPayload):
    owner_id: int
    period_key: str  # for example "2026-W41"; one digest per owner and period


class StaleSweepPayload(JobPayload):
    owner_id: int


class PublishFeedPayload(JobPayload):
    owner_id: int


class CollectBatchPayload(JobPayload):
    batch_id: int  # llm_batches.id


PAYLOAD_TYPES: Mapping[JobKind, type[JobPayload]] = MappingProxyType(
    {
        JobKind.INGEST_CAPTURE: IngestCapturePayload,
        JobKind.INGEST_ALBUM: IngestAlbumPayload,
        JobKind.FETCH_ITEM: FetchItemPayload,
        JobKind.ENRICH_ITEM: EnrichItemPayload,
        JobKind.EMBED_ITEM: EmbedItemPayload,
        JobKind.SCORE_ITEM: ScoreItemPayload,
        JobKind.SEND_DIGEST: SendDigestPayload,
        JobKind.STALE_SWEEP: StaleSweepPayload,
        JobKind.PUBLISH_FEED: PublishFeedPayload,
        JobKind.COLLECT_BATCH: CollectBatchPayload,
    }
)

# --- Failure signals ---------------------------------------------------------------------


class Retry(Exception):  # noqa: N818 - a control-flow signal, named for how it reads at the raise site
    """Run this job again after ``after``. Counts as an attempt."""

    def __init__(self, reason: str, *, after: timedelta) -> None:
        super().__init__(reason)
        self.reason = reason
        self.after = after


class Permanent(Exception):  # noqa: N818
    """This job can never succeed. It goes to ``dead`` without further attempts."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- What a handler receives -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A job this worker holds a lease on. The lease ends at ``locked_until``."""

    id: int
    kind: JobKind
    payload: dict[str, Any]
    attempts: int  # including this one
    max_attempts: int
    locked_by: str
    locked_until: datetime


@dataclass(frozen=True, slots=True)
class Services:
    """Everything a handler may use. Built once per process."""

    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    fetcher: Fetcher
    llm: LLMProvider
    embedder: Embedder
    blobs: BlobStore
    # The connection to Telegram, for the jobs that send a message. None without a bot token.
    bot: Bot | None = None
    # The files of saved messages, from the Bot API. None without a bot token.
    files: FileSource | None = None
    # Questions to the model in one batch, at a lower price (UNSHELVE_CARDS=batch).
    batches: BatchProvider | None = None
    # Where the public feed goes (UNSHELVE_PUBLISH_TARGET). None: nothing is published.
    feed: FeedTarget | None = None


@dataclass(frozen=True, slots=True)
class JobContext:
    job: ClaimedJob
    services: Services
    heartbeat: Callable[[], Awaitable[None]]  # extend the lease during long work


type JobHandler[P: JobPayload] = Callable[[JobContext, P], Awaitable[None]]
