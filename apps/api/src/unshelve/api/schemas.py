"""Request and response bodies of the HTTP API.

This file is the contract with the web app: ``openapi.json`` and the TypeScript types are
generated from it. Like ``unshelve.contracts``, it is changed by the maintainer only.

Conventions:

* Lists that can grow are pages: ``{"items": [...], "next_cursor": "..." | null}``.
* Errors are RFC 9457 problem details (``Problem``), sent as ``application/problem+json``.
* Request bodies reject unknown fields, so a typo in a field name is an error, not a no-op.
* Every date-time carries a time zone.
"""

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from unshelve.contracts.enums import (
    Actor,
    EnrichState,
    GoalLabel,
    ItemKind,
    ItemStatus,
    JobKind,
    JobState,
    Visibility,
)

HttpUrlStr = Annotated[str, StringConstraints(pattern=r"^https?://\S+$", max_length=2048)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
LongText = Annotated[str, StringConstraints(max_length=10_000)]
Score = Annotated[int, Field(ge=0, le=3)]


class Response(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Errors and paging -------------------------------------------------------------------


class FieldProblem(Response):
    pointer: str = Field(description="JSON pointer to the invalid value, e.g. /body/title")
    detail: str


class Problem(Response):
    """RFC 9457 problem details. The media type on the wire is application/problem+json."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    errors: list[FieldProblem] | None = Field(
        default=None, description="Present on validation errors (422)."
    )


class Page[T](Response):
    items: list[T]
    next_cursor: str | None = Field(
        default=None, description="Pass as ?cursor= to get the next page. Null on the last page."
    )


# --- Auth --------------------------------------------------------------------------------


class MagicConsumeRequest(Request):
    token: Annotated[str, StringConstraints(min_length=16, max_length=256)]


class Me(Response):
    id: int
    telegram_user_id: int
    display_name: str | None
    timezone: str
    queue_limit: int


# --- Items -------------------------------------------------------------------------------


class TagRef(Response):
    slug: str
    name: str
    is_public: bool


class GoalScore(Response):
    """How relevant an item is to one goal."""

    goal_id: int
    goal_slug: str
    goal_title: str
    score: Score
    reason: str
    label: GoalLabel | None = Field(description="The user's verdict. Null means not reviewed.")


class ItemSummary(Response):
    """An item as shown in lists."""

    id: int
    kind: ItemKind
    status: ItemStatus
    enrich_state: EnrichState
    partial: bool = Field(description="Enriched from the saved text; the source was not fetched.")
    visibility: Visibility
    title: str | None
    url: str | None
    site: str | None
    author: str | None
    lang: str | None
    summary: str | None
    est_minutes: int | None
    tags: list[TagRef]
    top_goal: GoalScore | None = Field(
        description="The active goal with the highest score, if a score is above 0."
    )
    capture_count: int = Field(description="How many times this was saved.")
    first_captured_at: AwareDatetime
    last_captured_at: AwareDatetime
    read_at: AwareDatetime | None
    published_at: AwareDatetime | None


class ItemDetail(ItemSummary):
    key_points: list[str]
    extra_links: list[str]
    note: str | None = Field(description="Private. Never published.")
    public_note: str | None = Field(description="Shown with the item when it is public.")
    public_slug: str | None
    duplicate_of_id: int | None
    goals: list[GoalScore]
    allowed_transitions: list[ItemStatus] = Field(
        description="The statuses this item can move to now."
    )


class ItemPage(Page[ItemSummary]):
    pass


class ItemListQuery(Request):
    status: list[ItemStatus] | None = None
    kind: list[ItemKind] | None = None
    tag: list[Slug] | None = None
    goal_id: int | None = None
    min_score: Score | None = Field(default=None, description="Needs goal_id.")
    visibility: Visibility | None = None
    sort: Literal["captured_desc", "captured_asc", "relevance"] = "captured_desc"
    limit: int = Field(default=30, ge=1, le=100)
    cursor: str | None = None


class ItemCreate(Request):
    """Save something by hand: a link, a note, or both."""

    url: HttpUrlStr | None = None
    text: LongText | None = None

    @model_validator(mode="after")
    def _has_content(self) -> "ItemCreate":
        if not self.url and not (self.text and self.text.strip()):
            msg = "give a url, a text, or both"
            raise ValueError(msg)
        return self


class ItemUpdate(Request):
    """Fields that are left out stay as they are. ``null`` clears a field."""

    title: ShortText | None = None
    note: LongText | None = None
    public_note: LongText | None = None
    tags: list[Slug] | None = Field(default=None, description="Replaces the item's tags.")


class TransitionRequest(Request):
    to: ItemStatus


class PublicItem(Response):
    """Exactly what the public feed contains for an item. Mirrors the pub.items view."""

    id: str
    kind: ItemKind
    title: str | None
    url: str | None
    author: str | None
    site: str | None
    lang: str | None
    summary: str | None
    key_points: list[str]
    est_minutes: int | None
    note: str | None
    published_at: AwareDatetime | None
    tags: list[str]


class VisibilityRequest(Request):
    visibility: Visibility
    record: PublicItem | None = Field(
        default=None,
        description=(
            "To publish: the record of the preview, as it was shown. The item becomes public "
            "with exactly this record, or the answer is a 409 and nothing changes."
        ),
    )

    @model_validator(mode="after")
    def _record_to_publish(self) -> "VisibilityRequest":
        if self.visibility == Visibility.PUBLIC and self.record is None:
            msg = "publishing needs the record of the preview: ask for the preview first"
            raise ValueError(msg)
        return self


class VisibilityResult(Response):
    visibility: Visibility
    public: PublicItem | None = Field(description="The published record. Null when private.")


class EventOut(Response):
    id: int
    actor: Actor
    type: str
    from_status: str | None
    to_status: str | None
    payload: dict[str, Any]
    created_at: AwareDatetime


class EventPage(Page[EventOut]):
    pass


class SimilarItem(Response):
    item: ItemSummary
    similarity: float = Field(ge=-1, le=1)


class SimilarItems(Response):
    items: list[SimilarItem]


# --- Goals -------------------------------------------------------------------------------


class GoalOut(Response):
    id: int
    slug: str
    title: str
    description: str
    keywords: list[str]
    starts_on: date | None
    ends_on: date | None
    version: int = Field(description="Goes up when the meaning of the goal changes.")
    is_focus: bool
    archived_at: AwareDatetime | None


class GoalList(Response):
    items: list[GoalOut]


class GoalCreate(Request):
    title: ShortText
    description: LongText = ""
    keywords: list[ShortText] = Field(default_factory=list[str], max_length=30)
    starts_on: date | None = None
    ends_on: date | None = None


class GoalUpdate(Request):
    """Fields that are left out stay as they are."""

    title: ShortText | None = None
    description: LongText | None = None
    keywords: list[ShortText] | None = Field(default=None, max_length=30)
    starts_on: date | None = None
    ends_on: date | None = None
    archived: bool | None = None


class LabelRequest(Request):
    label: GoalLabel | None = Field(description="Null removes the label.")
    score: Score | None = Field(
        default=None,
        description="The score that you saw. When the score changed since, the answer is a 409: "
        "a verdict is about the score that was shown.",
    )


# --- Search ------------------------------------------------------------------------------


SearchWords = Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]


class SearchFields(Request):
    """The words and the filters of a search. The search and the overview both take them, so
    the overview reads the best items of the list that the search gives for the same fields."""

    q: SearchWords
    status: list[ItemStatus] | None = Field(default=None, description="One of these statuses.")
    kind: list[ItemKind] | None = Field(default=None, description="One of these kinds.")
    tag: list[Slug] | None = Field(
        default=None, description="Each of these tags: two tags find the items that have both."
    )
    goal_id: int | None = Field(
        default=None,
        description="Only the items that help this goal: a score of 2 or 3 after your verdict "
        "(a rejected score turns). An unknown goal finds nothing.",
    )
    unread: bool = Field(
        default=False,
        description="true: only the items that you have not read, those without read_at. An "
        "item keeps read_at when it leaves done, so this is not a status filter. false: read "
        "and unread items.",
    )


class SearchQuery(SearchFields):
    q: SearchWords = Field(
        description='The words to find, as in a web search: "a phrase", this or that, -word.'
    )
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = None


class RetrieverRanks(Response):
    """The position of the item in each retriever's result, before fusion. Null: not found."""

    fts: int | None
    trigram: int | None
    vector: int | None


class SnippetPart(Response):
    text: str = Field(description="Plain text from the item. Show it as text, never as HTML.")
    hit: bool = Field(description="A word of the query, or another form of it: show it marked.")


class Snippet(Response):
    """A short passage that shows why search found the item."""

    source: Literal["text", "summary", "passage"] = Field(
        description="text: the words of the query in the saved text. summary: the words in the "
        "summary. passage: the piece of the text closest in meaning; it has no hits."
    )
    parts: list[SnippetPart]


class SearchHit(Response):
    item: ItemSummary
    score: float = Field(description="Reciprocal rank fusion: the sum of 1 / (60 + position).")
    snippet: Snippet | None = Field(
        description="Null when the title or the summary already shows why: a hit of the title, "
        "or an item whose summary is closest in meaning."
    )
    ranks: RetrieverRanks


class SearchPage(Page[SearchHit]):
    vector_search: Literal["used", "no_vectors", "failed"] = Field(
        description="used: the search compared meanings too. no_vectors: the active set of "
        "vectors was not made by this embedder, or no set exists yet, so meanings were not "
        "compared. failed: the embedder did not answer in time; the words still did."
    )


class OverviewRequest(SearchFields):
    """The question and the filters of a search: the model reads the best items of that search."""

    q: SearchWords = Field(description="The question, in your words.")
    limit: int = Field(default=12, ge=1, le=30, description="How many items to read.")


class Citation(Response):
    item_id: int
    chunk_id: int = Field(description="The retrieved piece of the item that the quote is from.")
    quote: str = Field(description="The passage that supports the statement: plain text.")


class OverviewSegment(Response):
    text: str = Field(
        description="Plain text of the model, without links or images. Show it as text, never "
        "as Markdown or HTML."
    )
    citations: list[Citation] = Field(description="Empty for text of the model alone.")


class Overview(Response):
    """An answer built only from saved items. Every statement points at its sources."""

    segments: list[OverviewSegment] = Field(
        description="The answer in order. Empty when no saved item could be read."
    )
    sources: list[ItemSummary] = Field(description="The items that the model read, the best first.")
    vector_search: Literal["used", "no_vectors", "failed"] = Field(
        description="As in the search: whether the items were also found by meaning."
    )


# --- Resurface ---------------------------------------------------------------------------


class Queue(Response):
    limit: int
    used: int
    items: list[ItemSummary]


class DigestPick(Response):
    item: ItemSummary
    reason: Literal["focus_goal", "other_goal", "saved_again", "recent", "random"]
    goal_title: str | None = Field(default=None, description="The goal of a pick for a goal.")


class DigestOut(Response):
    id: int
    period_key: str
    sent_at: AwareDatetime | None
    picks: list[DigestPick]
    total_minutes: int


class DigestPage(Page[DigestOut]):
    pass


class DigestPreviewRequest(Request):
    minutes: int = Field(default=60, ge=5, le=600, description="The reading time budget.")
    goal_id: int | None = Field(default=None, description="Defaults to the focus goal.")


class DigestPreview(Response):
    picks: list[DigestPick]
    total_minutes: int
    goal_title: str | None = Field(
        description="The goal of the focus picks: the goal of the request, or the focus goal."
    )


# --- Operations --------------------------------------------------------------------------


class TagOut(TagRef):
    item_count: int


class TagList(Response):
    items: list[TagOut]


class ItemStats(Response):
    """Your items, as the list of items has them: an item that duplicates another is left out."""

    by_status: dict[ItemStatus, int] = Field(description="Each status, also with 0 items.")
    by_enrich_state: dict[EnrichState, int] = Field(description="Each state, also with 0 items.")
    read_last_7_days: int = Field(description="Items whose read_at is in the last 7 days.")
    median_unread_age_days: float | None = Field(
        description="The median age of the items that you have not read and not archived (no "
        "read_at, status not archived), in days since the last save. Null without such items."
    )


class JobKindStats(Response):
    """The jobs of one kind, by where they are in the queue."""

    kind: JobKind
    ready: int = Field(description="Pending, and a worker can take it now.")
    scheduled: int = Field(
        description="Pending, but no worker takes it before a later time: a retry after a "
        "failed attempt, or a job that starts later."
    )
    running: int = Field(description="A worker has it now.")
    dead: int = Field(
        description="Failed for good: its attempts are used up, or the error is permanent. No "
        "worker takes it again."
    )
    retrying: int = Field(
        description="Pending after a failed attempt. These jobs are also in ready or scheduled."
    )
    done_last_24_hours: int
    dead_last_24_hours: int
    oldest_ready_seconds: float | None = Field(
        description="How long the oldest ready job has waited for a worker. Null when no job "
        "is ready."
    )


class QueueStats(Response):
    """The job queue of the whole instance: every job, whoever started it."""

    depth: int = Field(
        description="The jobs that the workers still have to do: ready, scheduled and running, "
        "of every kind. A dead job is not in it: no worker takes it again."
    )
    dead: int = Field(description="The dead jobs of every kind.")
    oldest_ready_seconds: float | None = Field(
        description="The longest wait of a ready job, of every kind. A number that grows "
        "means that no worker takes jobs."
    )
    by_kind: list[JobKindStats] = Field(description="Each kind of job, in a fixed order.")


class TaskCost(Response):
    task: str = Field(
        description="What the calls were for, as the ledger names it: for example "
        "enrich_item, score_item, read_image or overview."
    )
    calls: int = Field(
        description="Paid answers. A stored answer of the same question is free and not a call."
    )
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LLMCostStats(Response):
    """What the model calls of the whole instance cost, from the ledger (``llm_calls``)."""

    spent_today_usd: float = Field(
        description="Since midnight in UNSHELVE_TIMEZONE, as the daily budget counts it: the "
        "answers of a batch are left out."
    )
    daily_budget_usd: float = Field(
        description="UNSHELVE_LLM_DAILY_BUDGET_USD. When today's spend reaches it, a new call "
        "waits for the next day."
    )
    spent_last_30_days_usd: float = Field(
        description="Since midnight 30 days before today, with the answers of batches."
    )
    by_task: list[TaskCost] = Field(
        description="The same 30 days for each task, the most expensive task first."
    )


class Stats(Response):
    """The numbers of one moment: your items, the job queue with its failures, and what the
    model calls cost for each task."""

    as_of: AwareDatetime = Field(description="The moment of the numbers: the database clock.")
    items: ItemStats
    queue: QueueStats
    llm: LLMCostStats


class JobOut(Response):
    id: int
    kind: JobKind
    state: JobState
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    run_after: AwareDatetime
    locked_by: str | None
    locked_until: AwareDatetime | None
    last_error: str | None
    created_at: AwareDatetime
    finished_at: AwareDatetime | None


class JobPage(Page[JobOut]):
    pass


class JobListQuery(Request):
    state: list[JobState] | None = None
    kind: list[JobKind] | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
