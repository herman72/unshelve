"""Data that moves between modules. All models are immutable."""

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from unshelve.contracts.enums import CaptureSource, Distance, ItemKind

MediaKind = Literal["photo", "video", "document", "audio", "voice", "animation", "sticker"]


class Dto(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Capture -----------------------------------------------------------------------------


class MediaRef(Dto):
    """A file attached to a saved message. The bytes are fetched later, into the BlobStore."""

    kind: MediaKind
    file_id: str | None = None  # Bot API handle, valid for this bot only
    file_unique_id: str | None = None  # stable across bots; used for deduplication
    local_path: str | None = None  # path inside a Telegram Desktop export
    mime: str | None = None
    size_bytes: int | None = None
    file_name: str | None = None
    # The SHA-256 of the bytes: the key of their copy in the BlobStore, when the copy exists.
    # The importer sets it for a file of an export (ingest.export_files).
    sha256: str | None = None


class ForwardOrigin(Dto):
    """Where a forwarded message came from (Bot API ``MessageOrigin``).

    An export gives less than the bot: the name of the source, sometimes a date, and no ids.
    Its type is then "unknown".
    """

    type: Literal["user", "hidden_user", "chat", "channel", "unknown"]
    date: AwareDatetime | None = None
    chat_id: int | None = None
    message_id: int | None = None
    name: str | None = None


class CaptureEnvelope(Dto):
    """One saved message, in the same shape whether it came from the bot or from an export.

    ``(chat_id, message_id, edit_seq)`` identifies it. Two producers that see the same
    message must build the same key, or the message is stored twice.
    """

    source: CaptureSource
    chat_id: int
    message_id: int
    edit_seq: int = 0
    posted_at: AwareDatetime
    text: str = ""
    urls: list[str] = Field(default_factory=list[str])
    media: list[MediaRef] = Field(default_factory=list[MediaRef])
    media_group_id: str | None = None
    forward_origin: ForwardOrigin | None = None
    raw: dict[str, Any] = Field(default_factory=dict[str, Any])


# --- Fetch and extract -------------------------------------------------------------------


class FetchResult(Dto):
    url: str
    final_url: str  # after redirects
    status: int
    content_type: str | None = None
    body: bytes


class SourceRef(Dto):
    """What an extractor is asked to read: a URL, or the saved message itself."""

    url: str | None = None
    text: str = ""
    media: list[MediaRef] = Field(default_factory=list[MediaRef])
    forward_origin: ForwardOrigin | None = None


class Extracted(Dto):
    """The result of reading a source. ``text`` is plain text from an untrusted origin."""

    kind: ItemKind
    text: str
    title: str | None = None
    author: str | None = None
    site: str | None = None
    lang: str | None = None
    final_url: str | None = None
    published_at: AwareDatetime | None = None
    extra_links: list[str] = Field(default_factory=list[str])
    # The minutes that the item takes, when the source knows them: the length of a video.
    # None: the pipeline counts the minutes to read the text.
    minutes: int | None = Field(default=None, ge=0)


# --- Model providers ---------------------------------------------------------------------


class TextPart(Dto):
    type: Literal["text"] = "text"
    text: str


class ImagePart(Dto):
    type: Literal["image"] = "image"
    data: bytes
    mime: str


class LLMUsage(Dto):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


class LLMResult[T: BaseModel](Dto):
    value: T
    model: str
    usage: LLMUsage = LLMUsage()
    cached: bool = False  # served from llm_calls without a provider call


# --- An answer with citations --------------------------------------------------------------


class SourceText(Dto):
    """A text that a cited answer may quote. ``title`` and ``context`` help the model to read
    it, but only ``text`` can be quoted."""

    title: str
    context: str | None = None
    text: str = Field(min_length=1)


class Quote(Dto):
    """A part of one source that supports a statement: the characters ``start`` (from 0) up to
    ``end`` (not included) of the source with the index ``source`` (from 0)."""

    source: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str


class CitedSegment(Dto):
    """One piece of the answer, in order, and the quotes that support it. A piece without a
    quote is text of the model alone."""

    text: str
    quotes: list[Quote] = []


class CitedAnswer(Dto):
    segments: list[CitedSegment]


class BatchQuestion(Dto):
    """One question of a batch. ``custom_id`` names it in the answers: its cache key."""

    custom_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    system: str
    parts: list[TextPart | ImagePart]
    max_output_tokens: int


class BatchAnswer[T: BaseModel](Dto):
    """The answer to one question of a batch, or why there is none."""

    custom_id: str
    result: LLMResult[T] | None = None
    # errored, canceled, expired, or a refusal or an answer that does not fit the schema
    error: str | None = None


class EmbeddingSpec(Dto):
    """Identifies an embedding space. Vectors from different specs are never compared."""

    provider: str
    model: str
    dim: int = Field(gt=0)
    distance: Distance = Distance.COSINE
    doc_prefix: str = ""
    query_prefix: str = ""
