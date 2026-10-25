"""The data of packages/site-search, checked against the server (docs/learn/46).

The package searches the public feed in a browser, with the text normalisation of the server.
Three of its files come from here, and a test fails when one differs from the server:

    schema/feed-v1.schema.json   the JSON Schema of the feed (publish/feed_schema.py)
    fixtures/feed.json           a feed that the real code wrote, for the tests of the package
    src/u-norm-table.ts          what u_norm() of the database does to each character

To write the three files again (the database must run):

    make site-search
"""

# This file reads Persian text on purpose.
# ruff: noqa: RUF001

import json
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import NamedTuple

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.publish.support import NOW, seed_world
from unshelve.api.schemas import PublicItem
from unshelve.auth import store as auth
from unshelve.contracts.enums import ItemKind, Visibility
from unshelve.db.models import Item, ItemTag, Tag
from unshelve.publish.feed import FEED_VERSION, FIELDS, build_feed
from unshelve.publish.feed_schema import PublicFeed, PublicRecord, schema_text
from unshelve.publish.visibility import slug_for

Sessions = async_sessionmaker[AsyncSession]

PACKAGE = Path(__file__).resolve().parents[4] / "packages" / "site-search"
SCHEMA_FILE = PACKAGE / "schema" / "feed-v1.schema.json"
FIXTURE_FILE = PACKAGE / "fixtures" / "feed.json"
TABLE_FILE = PACKAGE / "src" / "u-norm-table.ts"
CASES_FILE = PACKAGE / "test" / "u-norm-cases.json"

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


# --- The fixture feed --------------------------------------------------------------------

SITE_OWNER_TELEGRAM_ID = 4545


class SiteItem(NamedTuple):
    tags: tuple[str, ...]
    columns: dict[str, object]


# The public items of the fixture feed, the newest first. Their words test the normalisation:
# the dotted capital I, Persian text with Persian digits, a zero-width non-joiner, a kasra and
# a tatweel. The note has no title, no link and no summary.
SITE_ITEMS = (
    SiteItem(
        ("databases", "talks"),
        {
            "kind": ItemKind.VIDEO,
            "title": "İstanbul Postgres Meetup: Queues at Scale",
            "canonical_url": "https://video.example.com/istanbul-meetup",
            "author": "A. Speaker",
            "site": "video.example.com",
            "lang": "en",
            "summary": "A talk on job queues in Postgres, from a meetup in İstanbul.",
            "key_points": ["SKIP LOCKED lets many workers share one table"],
            "est_minutes": 38,
        },
    ),
    SiteItem(
        ("databases",),
        {
            "kind": ItemKind.ARTICLE,
            "title": "صف کارها در پستگرس",
            "canonical_url": "https://fa.example.com/queues",
            "site": "fa.example.com",
            "lang": "fa",
            "summary": "یک صف کار روی یک جدول Postgres.",
        },
    ),
    SiteItem(
        ("databases",),
        {
            "kind": ItemKind.ARTICLE,
            "title": "۱۰ نکته درباره‌ی ایندکس‌ها",
            "canonical_url": "https://fa.example.com/indexes",
            "site": "fa.example.com",
            "lang": "fa",
            "summary": "ده نکته برای ایندکس‌های Postgres، با مثال.",
            "est_minutes": 9,
        },
    ),
    SiteItem(
        ("books", "databases"),
        {
            "kind": ItemKind.BOOK,
            "title": "پستگرس برای همه",
            "lang": "fa",
            "summary": "کِتابی کوتاه درباره‌ی پـایگاه داده، در ۳ فصل.",
        },
    ),
    SiteItem(
        ("databases",),
        {
            "kind": ItemKind.ARTICLE,
            "title": "Leases for Job Queues in Postgres",
            "canonical_url": "https://example.com/leases",
            "author": "A. Made-up",
            "site": "example.com",
            "lang": "en",
            "summary": "A job queue on one Postgres table, made safe with leases.",
            "key_points": ["A lease ends when its worker stops"],
            "est_minutes": 14,
            "public_note": "Worth a read before the design review.",
        },
    ),
    SiteItem(
        (),
        {
            "kind": ItemKind.NOTE,
            "lang": "en",
            "public_note": "SKIP LOCKED needs an index on the columns of the queue.",
        },
    ),
)


async def _seed_site(sessions: Sessions) -> int:
    """Commit the public items of the fixture feed, each a day older than the one before."""
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, SITE_OWNER_TELEGRAM_ID)
        names = sorted({name for item in SITE_ITEMS for name in item.tags})
        tags = {
            name: Tag(owner_id=owner_id, slug=name, name=name, is_public=True) for name in names
        }
        db.add_all(tags.values())
        for age, (item_tags, columns) in enumerate(SITE_ITEMS):
            title = columns.get("title")
            item = Item(
                owner_id=owner_id,
                first_captured_at=NOW,
                last_captured_at=NOW,
                visibility=Visibility.PUBLIC,
                public_slug=slug_for(title if isinstance(title, str) else None),
                published_at=NOW - timedelta(days=age),
                **columns,
            )
            db.add(item)
            await db.flush()
            db.add_all(
                ItemTag(item_id=item.id, tag_id=tags[name].id, owner_id=owner_id)
                for name in item_tags
            )
        await db.commit()
        return owner_id


async def test_the_fixture_feed_is_what_the_server_writes(committed_db: Sessions) -> None:
    owner_id = await _seed_site(committed_db)

    feed = await build_feed(committed_db, owner_id)

    if WRITE:
        FIXTURE_FILE.write_bytes(feed.data)
        return
    assert feed.items == len(SITE_ITEMS)
    assert FIXTURE_FILE.read_bytes() == feed.data, f"{FIXTURE_FILE.name} {STALE}"


# --- The normalisation -------------------------------------------------------------------

# Postgres keeps text without NUL, and a surrogate is half of a character, not a character.
LAST_CODE_POINT = 0x10FFFF
SURROGATES = range(0xD800, 0xE000)

CHANGED_CHARACTERS = sa.text(
    """
    SELECT cp, u_norm(chr(cp))
    FROM generate_series(1, :last) AS cp
    WHERE (cp < :first_surrogate OR cp > :last_surrogate)
      AND u_norm(chr(cp)) IS DISTINCT FROM chr(cp)
    ORDER BY cp
    """
).bindparams(
    last=LAST_CODE_POINT, first_surrogate=SURROGATES.start, last_surrogate=SURROGATES.stop - 1
)

type Run = tuple[int, int, int, int | None]  # first, last, step, shift (None: removed)


def _runs(changed: dict[int, str]) -> list[Run]:
    """The changes as runs: first, first + step, ..., last, each moved by the same shift.

    A run of capitals often alternates with its small letters (Ā ā Ă ă ...), so a step can be 2.
    """
    runs: list[Run] = []
    for cp, result in sorted(changed.items()):
        shift = ord(result) - cp if result else None
        if runs:
            first, last, step, run_shift = runs[-1]
            gap = cp - last
            # A run of one takes the step of its second member; a longer run keeps its step.
            if run_shift == shift and (gap == step or (first == last and gap == 2)):
                runs[-1] = (first, cp, gap, shift)
                continue
        runs.append((cp, cp, 1, shift))
    return runs


def _table_text(changed: dict[int, str]) -> str:
    tested = LAST_CODE_POINT - len(SURROGATES)
    runs = _runs(changed)
    lines = [
        "// Written from u_norm() of the database by `make site-search`: do not edit.",
        "// apps/api/tests/publish/test_site_search.py checks it against the database.",
        "//",
        f"// u_norm() changes {len(changed):,} of the {tested:,} characters that Postgres",
        f"// can store, one character at a time. These {len(runs)} runs list them. A run",
        "// [first, last, step, shift] holds first, first + step, ..., last. Each of them",
        "// becomes the character at its code point plus shift; a shift of null removes it.",
        # One line of TypeScript, in two parts here.
        "export type Run = readonly "
        "[first: number, last: number, step: number, shift: number | null];",
        "",
        "export const U_NORM_RUNS: readonly Run[] = [",
    ]
    for first, last, step, shift in runs:
        lines.append(f"  [{first:#x}, {last:#x}, {step}, {'null' if shift is None else shift}],")
    lines.append("];")
    return "\n".join(lines) + "\n"


_RUN_LINE = re.compile(r"  \[0x([0-9a-f]+), 0x([0-9a-f]+), ([12]), (-?[0-9]+|null)\],")


def _read_table(text: str) -> dict[int, str]:
    """The characters that the committed table changes, with their results."""
    body = text.split("Run[] = [\n", 1)[1].split("\n];", 1)[0]
    changed: dict[int, str] = {}
    for line in body.split("\n"):
        match = _RUN_LINE.fullmatch(line)
        assert match, f"a line of {TABLE_FILE.name} that is not a run: {line!r}"
        first, last, step, shift = match.groups()
        for cp in range(int(first, 16), int(last, 16) + 1, int(step)):
            changed[cp] = "" if shift == "null" else chr(cp + int(shift))
    return changed


async def test_u_norm_changes_exactly_the_characters_of_the_table(db: AsyncSession) -> None:
    # With the libc provider, lower() changes one character at a time, so a table of single
    # characters is the whole function. ICU would lower a word as a whole (a final sigma).
    provider = await db.scalar(
        sa.text("SELECT datlocprovider FROM pg_database WHERE datname = current_database()")
    )
    assert provider == "c", "the database must use the libc provider (docs/learn/46)"

    changed = {int(cp): str(result) for cp, result in await db.execute(CHANGED_CHARACTERS)}

    if WRITE:
        TABLE_FILE.write_text(_table_text(changed), encoding="utf-8")
        return
    committed = _read_table(TABLE_FILE.read_text(encoding="utf-8"))
    differences = sorted(
        cp for cp in changed.keys() | committed.keys() if changed.get(cp) != committed.get(cp)
    )
    assert not differences, (
        f"u_norm() gives another result for {len(differences)} characters, such as "
        f"{', '.join(f'U+{cp:04X}' for cp in differences[:10])}: {TABLE_FILE.name} {STALE}. "
        "The indexes of a database that changed its answer are stale too (docs/learn/46)."
    )


def test_a_run_of_the_table_covers_its_characters_and_no_other() -> None:
    # A run with step 1, a gap of 2 after it, a run with step 2, and two removed characters.
    changed = {0x41: "a", 0x42: "b", 0x44: "d", 0x100: "ā", 0x102: "ă", 0x104: "ą"}
    changed |= {0x64B: "", 0x64C: ""}

    assert _runs(changed) == [
        (0x41, 0x42, 1, 32),
        (0x44, 0x44, 1, 32),
        (0x100, 0x104, 2, 1),
        (0x64B, 0x64C, 1, None),
    ]
    assert _read_table(_table_text(changed)) == changed


CASES: list[dict[str, str]] = json.loads(CASES_FILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[case["why"] for case in CASES])
async def test_u_norm_gives_the_shared_results(db: AsyncSession, case: dict[str, str]) -> None:
    # The tests of the package check the same file with uNorm().
    result = await db.scalar(sa.text("SELECT u_norm(:text)").bindparams(text=case["input"]))

    assert result == case["expected"]
