"""Version 1 of the public feed, as a JSON Schema for the sites that read it (docs/learn/46).

A site can rely on each field here and on its type. The version number tells a site whether it
can read a feed:

* A change that an old reader can ignore keeps version 1: a new field, a new kind of item. So a
  reader must ignore a field that it does not know, and the schema allows one.
* A change that breaks an old reader needs version 2: a field removed or renamed, a new type.

``packages/site-search/schema/feed-v1.schema.json`` holds ``json_schema()``. A test fails when
the two differ; ``make site-search`` writes the file again.
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from unshelve.contracts.enums import ItemKind

# The one form of a time in the feed (feed.time_text). [0-9], not \d: in some regular
# expression engines \d also matches the digits of other scripts.
TIME_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"


class PublicRecord(BaseModel):
    """One public item. Each field is always there; a field without a value is null."""

    model_config = ConfigDict(strict=True)

    id: str = Field(
        min_length=1,
        description=(
            "The public name of the item: lowercase words of any script, joined by hyphens. "
            "It never changes, so a link to it stays valid."
        ),
    )
    kind: str = Field(
        description="What the item is. A new kind can appear in version 1.",
        examples=[kind.value for kind in ItemKind],
    )
    title: str | None
    url: str | None = Field(description="The link to the original. Null for a note.")
    author: str | None
    site: str | None = Field(description="The host of the original, such as example.com.")
    lang: str | None = Field(description="The language of the item, as a code: en, fa.")
    summary: str | None
    key_points: list[str]
    est_minutes: int | None = Field(description="The minutes that the item takes to read.")
    note: str | None = Field(description="The public note of the owner.")
    published_at: str = Field(
        pattern=TIME_PATTERN,
        description=(
            "The time of the first publication, in UTC to the second. It never changes, also "
            "when the item is published again."
        ),
    )
    tags: list[str] = Field(description="The public tags, as slugs, in alphabetical order.")


class PublicFeed(BaseModel):
    """The public items of one owner, the newest first."""

    model_config = ConfigDict(strict=True, title="The public feed of Unshelve, version 1")

    version: Literal[1]
    items: list[PublicRecord]


def json_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **PublicFeed.model_json_schema(),
    }


def schema_text() -> str:
    """The schema as the file holds it: one field on each line, readable Persian, a newline."""
    return json.dumps(json_schema(), ensure_ascii=False, indent=2) + "\n"
