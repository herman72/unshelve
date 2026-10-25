import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { FEED_VERSION, RECORD_FIELDS, parseFeed } from "../src/feed.ts";

function read(path: string): unknown {
  return JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
}

// The feed that the server wrote for the fixture items (apps/api/tests/publish/test_site_search.py).
const FIXTURE = read("../fixtures/feed.json") as { items: Record<string, unknown>[] };

interface Schema {
  properties: { version: { const: number } };
  $defs: {
    PublicRecord: {
      required: string[];
      properties: Record<string, { anyOf?: { type: string }[] }>;
    };
  };
}
const SCHEMA = read("../schema/feed-v1.schema.json") as Schema;

/** A feed with one record: the first record of the fixture, with `changes`. */
function feedWith(changes: Record<string, unknown>): unknown {
  return { version: 1, items: [{ ...FIXTURE.items[0], ...changes }] };
}

function feedWithout(field: string): unknown {
  const record = { ...FIXTURE.items[0] };
  delete record[field];
  return { version: 1, items: [record] };
}

test("parseFeed reads the feed that the server wrote, each field as it is", () => {
  const feed = parseFeed(FIXTURE);

  assert.equal(feed.items.length, 6);
  assert.deepEqual(feed.items, FIXTURE.items);
});

test("parseFeed refuses a feed of another version, and says why", () => {
  assert.throws(() => parseFeed({ ...FIXTURE, version: 2 }), {
    name: "FeedError",
    message: "The feed has version 2; this reader knows 1 only.",
  });
});

const BROKEN: [string, unknown, string][] = [
  ["that is not an object", [], "The feed is not a JSON object."],
  ["without a list of items", { version: 1 }, 'The feed has no list "items".'],
  ["with an item that is not an object", { version: 1, items: ["x"] }, "Item 0 is not a JSON object."],
  ["with a record without tags", feedWithout("tags"), 'Item 0 has no "tags".'],
  ["with an empty id", feedWith({ id: "" }), 'Item 0: "id" must be a text that is not empty.'],
  ["with a title that is a number", feedWith({ title: 7 }), 'Item 0: "title" must be text or null.'],
  [
    "with the minutes as text",
    feedWith({ est_minutes: "38" }),
    'Item 0: "est_minutes" must be a whole number or null.',
  ],
  [
    "with minutes that are not whole",
    feedWith({ est_minutes: 38.5 }),
    'Item 0: "est_minutes" must be a whole number or null.',
  ],
  [
    "with a time that has an offset",
    feedWith({ published_at: "2026-10-22T09:00:00+02:00" }),
    'Item 0: "published_at" must be a time such as 2026-10-22T07:00:00Z.',
  ],
  ["with a tag that is a number", feedWith({ tags: [1] }), 'Item 0: "tags" must be a list of texts.'],
  [
    "with an id that is there twice",
    { version: 1, items: [FIXTURE.items[0], FIXTURE.items[0]] },
    'Item 1: the id "istanbul-postgres-meetup-queues-at-scale" is there twice.',
  ],
];
for (const [why, feed, message] of BROKEN) {
  test(`parseFeed refuses a feed ${why}`, () => {
    assert.throws(() => parseFeed(feed), { name: "FeedError", message });
  });
}

test("parseFeed leaves out a field that version 1 does not have", () => {
  const feed = parseFeed(feedWith({ reading_list: "next week" }));

  assert.equal("reading_list" in feed.items[0], false);
});

test("the reader has the fields of the schema in their order, null where the schema allows it", () => {
  const record = SCHEMA.$defs.PublicRecord;

  assert.equal(SCHEMA.properties.version.const, FEED_VERSION);
  assert.deepEqual(Object.keys(RECORD_FIELDS), record.required);
  for (const [name, check] of Object.entries(RECORD_FIELDS)) {
    const nullable = (record.properties[name].anyOf ?? []).some(({ type }) => type === "null");
    assert.equal(check.test(null), nullable, `"${name}" can be null in only one of the two`);
  }
});
