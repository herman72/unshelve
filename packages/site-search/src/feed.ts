/**
 * The public feed, version 1, as apps/api/src/unshelve/publish/feed.py writes it. The schema of
 * the feed is in schema/feed-v1.schema.json.
 */

/** The version of the feed that this reader knows. */
export const FEED_VERSION = 1;

/** One public item. A field without a value is null. */
export interface FeedRecord {
  /** The public name of the item. It never changes. */
  id: string;
  /** article, video, post, repo, paper, image, note, book, pdf or unknown; others can come. */
  kind: string;
  title: string | null;
  /** The link to the original. Null for a note. */
  url: string | null;
  author: string | null;
  site: string | null;
  lang: string | null;
  summary: string | null;
  key_points: string[];
  est_minutes: number | null;
  /** The public note of the owner. */
  note: string | null;
  /** The time of the first publication, in UTC to the second: 2026-10-22T07:00:00Z. */
  published_at: string;
  /** The public tags, as slugs. */
  tags: string[];
}

export interface Feed {
  version: typeof FEED_VERSION;
  /** The newest first. */
  items: FeedRecord[];
}

/** A feed that this reader cannot read. The message says why. */
export class FeedError extends Error {
  override name = "FeedError";
}

interface Check {
  test: (value: unknown) => boolean;
  /** What the value must be, for the message. */
  expected: string;
}

const text: Check = { test: (value) => typeof value === "string", expected: "text" };
const textOrNull: Check = {
  test: (value) => value === null || typeof value === "string",
  expected: "text or null",
};
const textList: Check = {
  test: (value) => Array.isArray(value) && value.every((entry) => typeof entry === "string"),
  expected: "a list of texts",
};
const TIME = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/;

/** The fields of a record in version 1, in their order in the feed, each with its check. */
export const RECORD_FIELDS = {
  id: {
    test: (value) => typeof value === "string" && value !== "",
    expected: "a text that is not empty",
  },
  kind: text,
  title: textOrNull,
  url: textOrNull,
  author: textOrNull,
  site: textOrNull,
  lang: textOrNull,
  summary: textOrNull,
  key_points: textList,
  est_minutes: {
    test: (value) => value === null || Number.isInteger(value),
    expected: "a whole number or null",
  },
  note: textOrNull,
  published_at: {
    test: (value) => typeof value === "string" && TIME.test(value),
    expected: "a time such as 2026-10-22T07:00:00Z",
  },
  tags: textList,
} satisfies Record<keyof FeedRecord, Check>;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The feed in `value`, the parsed JSON of a feed file. It throws a FeedError for a feed of
 * another version, and for a feed that breaks version 1. A field that version 1 does not have
 * is left out: a newer feed can add one, and this reader ignores it.
 */
export function parseFeed(value: unknown): Feed {
  if (!isObject(value)) throw new FeedError("The feed is not a JSON object.");
  if (value.version !== FEED_VERSION) {
    const version = JSON.stringify(value.version);
    throw new FeedError(`The feed has version ${version}; this reader knows ${FEED_VERSION} only.`);
  }
  if (!Array.isArray(value.items)) throw new FeedError('The feed has no list "items".');

  const ids = new Set<string>();
  const items = value.items.map((item: unknown, index) => {
    const record = parseRecord(item, index);
    if (ids.has(record.id)) {
      throw new FeedError(`Item ${index}: the id "${record.id}" is there twice.`);
    }
    ids.add(record.id);
    return record;
  });
  return { version: FEED_VERSION, items };
}

function parseRecord(value: unknown, index: number): FeedRecord {
  if (!isObject(value)) throw new FeedError(`Item ${index} is not a JSON object.`);
  const record: Record<string, unknown> = {};
  for (const [name, check] of Object.entries(RECORD_FIELDS)) {
    if (!(name in value)) throw new FeedError(`Item ${index} has no "${name}".`);
    if (!check.test(value[name])) {
      throw new FeedError(`Item ${index}: "${name}" must be ${check.expected}.`);
    }
    record[name] = value[name];
  }
  // Each field of FeedRecord passed its check just above.
  return record as unknown as FeedRecord;
}
