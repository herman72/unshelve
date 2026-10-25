import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { type FeedRecord, parseFeed } from "../src/feed.ts";
import { createFeedSearch } from "../src/search.ts";

// The feed that the server wrote for the fixture items (apps/api/tests/publish/test_site_search.py).
const feed = parseFeed(
  JSON.parse(readFileSync(new URL("../fixtures/feed.json", import.meta.url), "utf8")),
);
const index = createFeedSearch(feed);

// The ids of the fixture items, in the order of the feed.
const MEETUP = "istanbul-postgres-meetup-queues-at-scale";
const QUEUES = "صف-کارها-در-پستگرس";
const TIPS = "۱۰-نکته-درباره-ی-ایندکس-ها";
const BOOK = "پستگرس-برای-همه";
const LEASES = "leases-for-job-queues-in-postgres";
const NOTE = "item";

/** The ids of the records that `query` finds, in alphabetical order. */
function found(query: string): string[] {
  return index.search(query).map((record) => record.id).sort();
}

function ids(...names: string[]): string[] {
  return names.sort();
}

test("a query with no words gives each record, in the order of the feed", () => {
  for (const query of ["", "   ", "!!!"]) {
    const records = index.search(query).map((record) => record.id);
    assert.deepEqual(records, [MEETUP, QUEUES, TIPS, BOOK, LEASES, NOTE], JSON.stringify(query));
  }
});

test("a word typed with the Arabic kaf and yeh finds the Persian word", () => {
  assert.deepEqual(found("صف كارها"), ids(QUEUES));
  assert.deepEqual(found("يك"), ids(QUEUES));
});

test("Persian, Arabic-Indic and ASCII digits find each other", () => {
  for (const query of ["10", "۱۰", "١٠"]) {
    assert.deepEqual(found(query), ids(TIPS), query);
  }
  assert.deepEqual(found("3"), ids(BOOK));
});

test("a query with a space finds two words joined by a zero-width non-joiner", () => {
  assert.deepEqual(found("ایندکس ها"), ids(TIPS));
});

test("a query with a zero-width non-joiner finds the two words", () => {
  assert.deepEqual(found("درباره‌ی"), ids(TIPS, BOOK));
});

test("a kasra or a tatweel in the text does not stop a match", () => {
  assert.deepEqual(found("کتاب"), ids(BOOK));
  assert.deepEqual(found("پایگاه"), ids(BOOK));
});

test("the dotted capital I is found by a plain i", () => {
  assert.deepEqual(found("istanbul"), ids(MEETUP));
  assert.deepEqual(found("İSTANBUL"), ids(MEETUP));
});

test("capitals do not matter", () => {
  assert.deepEqual(found("LEASES"), ids(LEASES));
});

test("each word of the query must be found", () => {
  assert.deepEqual(found("postgres meetup"), ids(MEETUP));
  assert.deepEqual(found("job queue"), ids(MEETUP, LEASES));
});

test("a word is found by its start", () => {
  assert.deepEqual(found("queu"), ids(MEETUP, LEASES, NOTE));
});

test("the key points, the public note and the tags are searched", () => {
  assert.deepEqual(found("skip locked"), ids(MEETUP, NOTE));
  assert.deepEqual(found("design review"), ids(LEASES));
  assert.deepEqual(found("talks"), ids(MEETUP));
});

test("a word in the title counts more than the same word in the summary", () => {
  // The title of the second record is longer than the summary of the first. A word in a long
  // field counts less, so only the weight of the title can put the second record first.
  const record = (id: string, title: string, summary: string): FeedRecord => ({
    ...feed.items[5],
    id,
    title,
    summary,
  });
  const two = createFeedSearch({
    version: 1,
    items: [record("in-summary", "a b", "postgres"), record("in-title", "postgres a b c", "a b")],
  });

  assert.deepEqual(
    two.search("postgres").map((found) => found.id),
    ["in-title", "in-summary"],
  );
});

test("a word that no record has finds nothing", () => {
  assert.deepEqual(found("kubernetes"), []);
});
