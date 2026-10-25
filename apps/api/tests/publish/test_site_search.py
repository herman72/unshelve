"""The data of packages/site-search, checked against the server (docs/learn/46).

The package searches the public feed in a browser. Its JSON Schema comes from here, and a test
fails when the file differs from the server:

    schema/feed-v1.schema.json   the JSON Schema of the feed (publish/feed_schema.py)

To write it again:

    make site-search
"""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.publish.support import seed_world
from unshelve.api.schemas import PublicItem
from unshelve.publish.feed import FEED_VERSION, FIELDS, build_feed
from unshelve.publish.feed_schema import PublicFeed, PublicRecord, schema_text

Sessions = async_sessionmaker[AsyncSession]

PACKAGE = Path(__file__).resolve().parents[4] / "packages" / "site-search"
SCHEMA_FILE = PACKAGE / "schema" / "feed-v1.schema.json"

# make site-search sets it: each test writes its file, and checks nothing.
WRITE = os.environ.get("UNSHELVE_SITE_SEARCH") == "write"
STALE = "is not what the server gives now: when the change is right, run `make site-search`"


# --- The schema --------------------------------------------------------------------------


def test_the_schema_file_is_the_schema_of_the_feed() -> None:
    if WRITE:
        SCHEMA_FILE.write_text(schema_text(), encoding="utf-8")
        return
    assert SCHEMA_FILE.read_text(encoding="utf-8") == schema_text(), f"{SCHEMA_FILE.name} {STALE}"


def test_the_schema_has_the_fields_and_the_version_of_the_feed() -> None:
    # The API shows the same record in the preview of an item (PublicItem).
    assert list(PublicRecord.model_fields) == list(FIELDS) == list(PublicItem.model_fields)
    assert PublicFeed.model_validate({"version": FEED_VERSION, "items": []}).items == []


async def test_the_feed_of_the_server_is_valid_under_the_schema(committed_db: Sessions) -> None:
    world = await seed_world(committed_db)

    feed = PublicFeed.model_validate_json((await build_feed(committed_db, world.owner_id)).data)

    assert [record.id for record in feed.items] == ["queues-fa", "leases-for-job-queues"]
    # The Persian item has no link, no author and no key points: null and [] are valid.
    assert feed.items[0].url is None
    assert feed.items[0].key_points == []


def _feed(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": "leases",
        "kind": "article",
        "title": "Leases",
        "url": "https://example.com/leases",
        "author": None,
        "site": "example.com",
        "lang": "en",
        "summary": None,
        "key_points": [],
        "est_minutes": 14,
        "note": None,
        "published_at": "2026-10-21T07:00:00Z",
        "tags": ["databases"],
    }
    return {"version": 1, "items": [record | changes]}


@pytest.mark.parametrize(
    ("feed", "field"),
    [
        ({"version": 2, "items": []}, "version"),
        ({"version": 1}, "items"),
        (_feed(id=""), "id"),
        (_feed(est_minutes="14"), "est_minutes"),
        (_feed(published_at="2026-10-21T09:00:00+02:00"), "published_at"),
        (_feed(published_at="۲۰۲۶-10-21T07:00:00Z"), "published_at"),
        (_feed(tags=[1]), "tags"),
        (_feed(title=123), "title"),
    ],
    ids=[
        "version 2",
        "no items",
        "an empty id",
        "minutes as text",
        "a time with an offset",
        "a time with Persian digits",
        "a tag that is a number",
        "a title that is a number",
    ],
)
def test_the_schema_refuses_a_feed_that_breaks_version_1(feed: object, field: str) -> None:
    with pytest.raises(ValidationError) as refused:
        PublicFeed.model_validate_json(json.dumps(feed))

    assert all(field in error["loc"] for error in refused.value.errors())


def test_a_reader_can_ignore_a_new_field() -> None:
    feed = _feed(reading_list="next week")

    assert PublicFeed.model_validate_json(json.dumps(feed)).items[0].id == "leases"
    # The schema says so too: it does not forbid a field that it does not list.
    assert "additionalProperties" not in schema_text()
