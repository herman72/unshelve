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


class VisibilityRequest(Request):
    visibility: Visibility


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


class SearchQuery(Request):
    q: Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)] = (
        Field(description='The words to find, as in a web search: "a phrase", this or that, -word.')
    )
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


class OverviewRequest(Request):
    q: Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]
    status: list[ItemStatus] | None = None
    limit: int = Field(default=12, ge=1, le=30, description="How many items to read.")


class Citation(Response):
    item_id: int
    quote: str = Field(description="The passage that supports the statement.")


class OverviewSegment(Response):
    text: str
    citations: list[Citation]


class Overview(Response):
    """An answer built only from saved items. Every statement points at its sources."""

    segments: list[OverviewSegment]
    sources: list[ItemSummary]


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


class Stats(Response):
    items_by_status: dict[ItemStatus, int]
    items_by_enrich_state: dict[EnrichState, int]
    jobs_by_state: dict[JobState, int]
    read_last_7_days: int
    median_unread_age_days: float | None
    llm_cost_usd_last_30_days: float


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
