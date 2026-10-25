// A pretend Unshelve API for the end-to-end tests: the answers of the real API, with made-up
// items. It keeps the items in memory and answers each request as the owner, without a
// session. The tests read what was sent to it (GET /__calls), start each test from the same
// items (POST /__reset), and change an item between two steps (POST /__items/{id}).
//
// The moves of the reading status follow the rules of the real API: the table of moves of
// contracts/states.py, a queue of 5 slots, and no move for a merged copy. The goals too: a slug
// from the title, a clash is a 409, at most 10 active goals, a new version for a new meaning,
// and one focus. The preview of a digest has three made-up picks, and it keeps them in order
// while they fit the reading time; the choice itself is tested in the API. Search finds words
// in the titles, the summaries and a few saved texts, and fuses three lists as the real API
// does; the overview cites the items that search found. The ranking itself is tested in the API.
//
//     node e2e/fixture-api.mjs 3901
//
// Plain JavaScript, so that it runs without a build step on each Node version of the project.
// The comments with types let `pnpm typecheck` check the made-up items against the types that
// are generated from the API (packages/api-client): a field that the API renames fails there.

// @ts-check

import { createServer } from "node:http";

/** @typedef {import("@unshelve/api-client").components["schemas"]["ItemDetail"]} ItemDetail */
/** @typedef {import("@unshelve/api-client").components["schemas"]["ItemSummary"]} ItemSummary */
/** @typedef {import("@unshelve/api-client").components["schemas"]["ItemStatus"]} ItemStatus */
/** @typedef {import("@unshelve/api-client").components["schemas"]["EventOut"]} EventOut */
/** @typedef {import("@unshelve/api-client").components["schemas"]["GoalOut"]} GoalOut */
/** @typedef {import("@unshelve/api-client").components["schemas"]["GoalScore"]} GoalScore */
/** @typedef {import("@unshelve/api-client").components["schemas"]["DigestPick"]} DigestPick */
/** @typedef {import("@unshelve/api-client").components["schemas"]["PublicItem"]} PublicItem */
/** @typedef {import("@unshelve/api-client").components["schemas"]["ItemKind"]} ItemKind */
/** @typedef {import("@unshelve/api-client").components["schemas"]["SearchHit"]} SearchHit */
/** @typedef {import("@unshelve/api-client").components["schemas"]["SearchPage"]} SearchPage */
/** @typedef {import("@unshelve/api-client").components["schemas"]["Overview"]} Overview */
/** @typedef {import("@unshelve/api-client").components["schemas"]["SnippetPart"]} SnippetPart */

const port = Number(process.argv[2] ?? 3901);

const CAPTURED = "2026-10-15T08:30:00Z";

/** The order of the statuses in the API, and the moves of each one (contracts/states.py). */
/** @type {ItemStatus[]} */
const STATUSES = ["inbox", "queued", "reading", "done", "archived"];
/** @type {Record<ItemStatus, ItemStatus[]>} */
const MOVES = {
  inbox: ["queued", "reading", "done", "archived"],
  queued: ["reading", "done", "inbox", "archived"],
  reading: ["done", "queued", "archived"],
  done: ["queued", "archived"],
  archived: ["inbox"],
};
const QUEUE_LIMIT = 5;

/**
 * The scores of item 1 against the made-up goals (freshGoals), the highest first. New objects
 * for each reset: a verdict of one test must not reach the next one.
 *
 * @returns {GoalScore[]}
 */
function scoresOfItemOne() {
  return [
    {
      goal_id: 1,
      goal_slug: "database-internals",
      goal_title: "Database internals",
      score: 3,
      reason: "It shows how one Postgres table holds a job queue with leases.",
      label: null,
    },
    {
      goal_id: 2,
      goal_slug: "learn-finnish",
      goal_title: "Learn Finnish",
      score: 0,
      reason: "It has nothing to do with Finnish.",
      label: null,
    },
  ];
}

/**
 * The made-up items, as GET /api/v1/items/{id} gives them. Each one is a case for the pages.
 *
 * @returns {ItemDetail[]}
 */
function freshItems() {
  const scores = scoresOfItemOne();
  /** @type {Omit<ItemDetail, "id" | "kind" | "enrich_state" | "partial" | "title" | "url" | "site" | "summary">} */
  const base = {
    status: "inbox",
    visibility: "private",
    author: null,
    lang: "en",
    est_minutes: null,
    tags: [],
    top_goal: null,
    capture_count: 1,
    first_captured_at: CAPTURED,
    last_captured_at: CAPTURED,
    read_at: null,
    published_at: null,
    key_points: [],
    extra_links: [],
    note: null,
    public_note: null,
    public_slug: null,
    duplicate_of_id: null,
    goals: [],
    allowed_transitions: ["queued", "reading", "done", "archived"],
  };
  return [
    {
      ...base,
      // Ready, with each field that the detail page shows.
      id: 1,
      kind: "pdf",
      enrich_state: "ready",
      partial: false,
      title: "Leases for Job Queues in Postgres",
      url: "https://example.com/papers/leases.pdf",
      site: "example.com",
      author: "A. Made-up and B. Example",
      summary: "A job queue on one Postgres table, made safe with leases.",
      est_minutes: 14,
      tags: [{ slug: "queues", name: "queues", is_public: false }],
      key_points: ["A lease ends when its worker stops", "A heartbeat moves the lease forward"],
      goals: scores,
      top_goal: scores[0],
      // The API gives only web links. The page must not trust that: the second one is no link.
      extra_links: ["https://example.org/queue-data", "javascript:alert(1)"],
      note: "Read before the design review.",
    },
    {
      ...base,
      // Persian text: each piece of it must be right to left.
      id: 2,
      kind: "article",
      enrich_state: "ready",
      partial: false,
      title: "صف کارها در پستگرس",
      url: "https://blog.example.com/queues-fa",
      site: "blog.example.com",
      lang: "fa",
      summary: "یک صف کار روی یک جدول Postgres.",
      key_points: ["هر کار یک اجاره دارد"],
    },
    {
      ...base,
      // In the pipeline: no reprocess, a state that says so.
      id: 3,
      kind: "unknown",
      enrich_state: "fetched",
      partial: false,
      title: null,
      url: "https://example.com/talks/leases",
      site: "example.com",
      summary: null,
    },
    {
      ...base,
      // Failed and partial, without a title.
      id: 4,
      kind: "unknown",
      enrich_state: "failed",
      partial: true,
      title: null,
      url: "https://example.org/a-page-that-is-gone",
      site: "example.org",
      summary: null,
    },
    {
      ...base,
      // A saved address that is no web address: it is never a link.
      id: 5,
      kind: "note",
      enrich_state: "ready",
      partial: false,
      title: "A note with a strange address",
      url: "javascript:alert(1)",
      site: null,
      summary: "The address of this item is not a web address.",
    },
    {
      ...base,
      // A merged copy of item 1: not in a list, and it does not move.
      id: 6,
      kind: "pdf",
      enrich_state: "ready",
      partial: false,
      title: "Leases for Job Queues in Postgres (saved again)",
      url: "https://example.com/papers/leases.pdf?from=feed",
      site: "example.com",
      summary: null,
      duplicate_of_id: 1,
    },
    {
      ...base,
      // Done, so not in the inbox. Its id is 1000: "/items/1e3" must not find it. As in the
      // database of the real API, a done item has the time it was read (done_needs_read_at).
      id: 1000,
      status: "done",
      read_at: "2026-10-18T19:00:00Z",
      kind: "note",
      enrich_state: "ready",
      partial: false,
      title: "Item one thousand",
      url: null,
      site: null,
      summary: null,
    },
  ];
}

const DETAIL_ONLY = [
  "key_points",
  "extra_links",
  "note",
  "public_note",
  "public_slug",
  "duplicate_of_id",
  "goals",
  "allowed_transitions",
];

/**
 * An item as the list gives it: without the fields of the detail.
 *
 * @param {ItemDetail} item
 * @returns {ItemSummary}
 */
function summaryOf(item) {
  const fields = Object.entries(item).filter(([key]) => !DETAIL_ONLY.includes(key));
  return /** @type {ItemSummary} */ (Object.fromEntries(fields));
}

/**
 * The item as GET /api/v1/items/{id} gives it: the moves come from its status now.
 *
 * @param {ItemDetail} item
 * @returns {ItemDetail}
 */
function detailOf(item) {
  const allowed = item.duplicate_of_id === null ? MOVES[item.status] : [];
  return { ...item, allowed_transitions: STATUSES.filter((status) => allowed.includes(status)) };
}

/** @param {ItemDetail} item */
function inQueue(item) {
  return item.duplicate_of_id === null && (item.status === "queued" || item.status === "reading");
}

/**
 * The made-up goals, as GET /api/v1/goals gives them: a focus goal, a goal, an archived goal.
 *
 * @returns {GoalOut[]}
 */
function freshGoals() {
  /** @type {Omit<GoalOut, "id" | "slug" | "title">} */
  const base = {
    description: "",
    keywords: [],
    starts_on: null,
    ends_on: null,
    version: 1,
    is_focus: false,
    archived_at: null,
  };
  return [
    {
      ...base,
      id: 1,
      slug: "database-internals",
      title: "Database internals",
      description: "How Postgres stores, locks and finds rows.",
      keywords: ["postgres", "mvcc"],
      starts_on: "2026-11-01",
      ends_on: "2027-01-31",
      is_focus: true,
    },
    { ...base, id: 2, slug: "learn-finnish", title: "Learn Finnish", keywords: ["suomi"] },
    {
      ...base,
      id: 3,
      slug: "run-a-10k",
      title: "Run a 10k",
      archived_at: "2026-10-01T07:00:00Z",
    },
  ];
}

const MAX_ACTIVE_GOALS = 10;
const GOALS_FULL = "you have 10 active goals, the most; archive one first";

/** @param {GoalOut} goal */
const isArchived = (goal) => goal.archived_at !== null;

/**
 * The slug of a new goal, as the real API makes it: the Latin letters and digits of the title.
 *
 * @param {string} title
 */
function slugFor(title) {
  const plain = title.normalize("NFKD").replace(/[^\x00-\x7F]/g, "").toLowerCase();
  const slug = plain.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  if (slug) return slug;
  let hash = 0;
  for (const char of title.trim().toLowerCase()) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `goal-${hash.toString(16).padStart(8, "0").slice(0, 8)}`;
}

/** @param {GoalOut} goal */
const meaningOf = (goal) => JSON.stringify([goal.title, goal.description, goal.keywords]);

let items = freshItems();
let goals = freshGoals();
/** @type {number[]} */
let reprocessed = [];
/** @type {{ item: number, goal: number, label: string | null }[]} each verdict, in order */
let verdicts = [];
/** @type {{ id: number, to: string }[]} */
let moves = [];
/** @type {{ itemId: number, event: EventOut }[]} the moves, oldest first */
let events = [];
/** @type {Map<number, number>} the id of each item, and the id of its newest move */
let lastMove = new Map();
/** @type {number[]} the reading time of each preview of a digest, in order */
let digestMinutes = [];
/** For a test: the digest has nothing to bring back. */
let emptyDigest = false;
/** @type {Record<string, string | string[] | null>[]} the query of each search, in order */
let searches = [];
/** @type {Record<string, unknown>[]} the body of each request for an overview, in order */
let overviews = [];
/** For a test: what the embedder did, as the API reports it. */
/** @type {SearchPage["vector_search"]} */
let vectorSearch = "used";
/** For a test: fewer hits on a page than the 20 of the API, so that a second page exists. */
let searchPageSize = 20;
/** For a test: the detail of a 503, as when the model has no key or the budget is used. */
/** @type {string | null} */
let overviewFailure = null;
/** @type {{ id: number, visibility: string, record: unknown }[]} each change of visibility, in order */
let visibilities = [];

/**
 * The public name of an item, as the real API makes it: lowercase words of letters and digits
 * of any script, joined by hyphens, at most 60 characters; "item" for a title without them.
 *
 * @param {string | null} title
 */
function publicNameOf(title) {
  const text = (title ?? "").normalize("NFKC").toLowerCase().replace(/\p{Mn}/gu, "");
  const words = text.split(/[^\p{L}\p{N}\p{Mc}]+/u).filter(Boolean);
  if (words.length === 0) return "item";
  let name = words[0].slice(0, 60);
  for (const word of words.slice(1)) {
    if (name.length + 1 + word.length > 60) break;
    name = `${name}-${word}`;
  }
  return name;
}

/** Now, as a time of the feed: UTC, to the second. */
const nowInTheFeed = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

/**
 * The record that the feed would hold for the item, as GET .../public-preview gives it. Only
 * the public tags, and the public note.
 *
 * @param {ItemDetail} item
 * @param {string} name
 * @param {string} publishedAt
 * @returns {PublicItem}
 */
function publicRecordOf(item, name, publishedAt) {
  return {
    id: name,
    kind: item.kind,
    title: item.title,
    url: item.url,
    author: item.author,
    site: item.site,
    lang: item.lang,
    summary: item.summary,
    key_points: item.key_points,
    est_minutes: item.est_minutes,
    note: item.public_note,
    published_at: publishedAt,
    tags: item.tags
      .filter((tag) => tag.is_public)
      .map((tag) => tag.slug)
      .sort(),
  };
}

/**
 * The made-up picks of a digest: the item for the focus goal, a recent item, a random pick.
 *
 * @returns {DigestPick[]}
 */
function digestPicks() {
  /** @type {[number, DigestPick["reason"], string | null][]} */
  const chosen = [
    [1, "focus_goal", "Database internals"],
    [2, "recent", null],
    [5, "random", null],
  ];
  return chosen.flatMap(([id, reason, goal_title]) => {
    const item = items.find((candidate) => candidate.id === id);
    return item ? [{ item: summaryOf(item), reason, goal_title }] : [];
  });
}

/**
 * The saved text of some items, as the worker keeps it (item_contents). Search finds the words
 * of a query in it, as in the title and the summary. The text of item 5 has markup in it: the
 * pages must show it as characters.
 *
 * @type {Record<number, string>}
 */
const TEXTS = {
  1:
    "Each worker claims a job with SKIP LOCKED and holds a lease on it. A reaper finds the " +
    "jobs whose lease ended and gives them to the next worker.",
  2: "هر کار یک اجاره دارد. وقتی اجاره تمام شود، کارگر دیگری آن را برمی‌دارد.",
  5: "Saved from a page with markup: <img src=x onerror=alert(1)> and <b>bold</b> stay text.",
};

/**
 * Words that find an item by its meaning only: the pretend vector search. A real embedder finds
 * a text about locks for "contention" without the word; these lists stand in for it.
 *
 * @type {Record<number, string[]>}
 */
const MEANINGS = {
  1: ["contention", "concurrency"],
  2: ["contention"],
};

/** The words of a query, as the pretend search compares them. @param {string} q */
function wordsOf(q) {
  return q.toLowerCase().split(/\s+/).filter(Boolean);
}

/**
 * The text in parts: each word that starts with a word of the query is a hit, as the snippets
 * of the real API mark "leases" for "lease". Other text stays together in one part.
 *
 * @param {string} text
 * @param {string[]} words
 * @returns {SnippetPart[]}
 */
function partsOf(text, words) {
  /** @type {SnippetPart[]} */
  const parts = [];
  // split with a group keeps the separators: the words are at the odd places.
  text.split(/([\p{L}\p{M}\p{N}]+)/u).forEach((piece, index) => {
    if (!piece) return;
    const hit = index % 2 === 1 && words.some((word) => piece.toLowerCase().startsWith(word));
    const last = parts.at(-1);
    if (last && !last.hit && !hit) last.text += piece;
    else parts.push({ text: piece, hit });
  });
  return parts;
}

/** @param {string | null | undefined} text @param {string[]} words */
function hasWord(text, words) {
  return partsOf(text ?? "", words).some((part) => part.hit);
}

/** The first sentence of a text: the passage that the pretend API shows. @param {string} text */
function firstSentence(text) {
  return /^[^.!?]*[.!?]/u.exec(text)?.[0] ?? text;
}

/**
 * The pretend hybrid search: three lists, each in the order of the ids, fused with reciprocal
 * rank fusion as in the real API (k = 60). Words: a word of the query in the title, the summary
 * or the saved text. Spelling: in the title. Meaning: in MEANINGS or in the summary, unless the
 * embedder is switched off for a test.
 *
 * @param {string[]} words
 * @param {(item: ItemDetail) => boolean} keep the filters
 * @returns {SearchHit[]}
 */
function searchHits(words, keep) {
  const found = items.filter((item) => item.duplicate_of_id === null && keep(item));
  const lists = {
    fts: found.filter((item) =>
      [item.title, item.summary, TEXTS[item.id]].some((text) => hasWord(text, words)),
    ),
    trigram: found.filter((item) => hasWord(item.title, words)),
    vector:
      vectorSearch === "used"
        ? found.filter(
            (item) =>
              (MEANINGS[item.id] ?? []).some((meaning) => words.includes(meaning)) ||
              hasWord(item.summary, words),
          )
        : [],
  };
  /** @param {ItemDetail[]} list @param {ItemDetail} item */
  const placeIn = (list, item) => (list.includes(item) ? list.indexOf(item) + 1 : null);
  const hits = found.flatMap((item) => {
    const ranks = {
      fts: placeIn(lists.fts, item),
      trigram: placeIn(lists.trigram, item),
      vector: placeIn(lists.vector, item),
    };
    const places = [ranks.fts, ranks.trigram, ranks.vector].filter((place) => place !== null);
    if (places.length === 0) return [];
    const score = places.reduce((sum, place) => sum + 1 / (60 + place), 0);
    return [{ item: summaryOf(item), ranks, score, snippet: snippetOf(item, words, ranks) }];
  });
  // The best score first; a tie goes to the smaller id, as in the real API.
  return hits.sort((one, other) => other.score - one.score || one.item.id - other.item.id);
}

/**
 * The snippet of a hit, chosen as the real API chooses it: the words in the text, else the
 * words in the summary, else the passage closest in meaning for an item that only the vector
 * list found. Null for a hit of the title: the title already shows why.
 *
 * @param {ItemDetail} item
 * @param {string[]} words
 * @param {SearchHit["ranks"]} ranks
 * @returns {SearchHit["snippet"]}
 */
function snippetOf(item, words, ranks) {
  const text = TEXTS[item.id];
  if (text && hasWord(text, words)) return { source: "text", parts: partsOf(text, words) };
  if (item.summary && hasWord(item.summary, words)) {
    return { source: "summary", parts: partsOf(item.summary, words) };
  }
  if (text && ranks.fts === null && ranks.vector !== null) {
    return { source: "passage", parts: [{ text: firstSentence(text), hit: false }] };
  }
  return null;
}

/**
 * The answer of the pretend model: one sentence from each item that it read, in the order of
 * search, each one citing its item. An item without a text or a summary is read but not cited.
 *
 * @param {SearchHit[]} hits
 * @returns {Overview["segments"]}
 */
function overviewSegments(hits) {
  const cited = hits.flatMap(({ item }) => {
    const passage = TEXTS[item.id] ?? item.summary;
    if (!passage) return [];
    const quote = firstSentence(passage);
    const citation = { item_id: item.id, chunk_id: item.id * 10 + 1, quote };
    return [{ text: `${quote} `, citations: [citation] }];
  });
  if (cited.length === 0) return [];
  return [{ text: "From your saved items:\n", citations: [] }, ...cited];
}

/**
 * @param {import("node:http").ServerResponse} response
 * @param {number} status
 * @param {unknown} body
 * @param {string} [type]
 */
function send(response, status, body, type = "application/json") {
  response.writeHead(status, { "content-type": type });
  response.end(JSON.stringify(body));
}

/**
 * An error in the form of the real API (RFC 9457).
 *
 * @param {import("node:http").ServerResponse} response
 * @param {number} status
 * @param {string} title
 * @param {string} detail
 */
function problem(response, status, title, detail) {
  send(response, status, { type: "about:blank", title, status, detail }, "application/problem+json");
}

/**
 * The body of a request, as JSON.
 *
 * @param {import("node:http").IncomingMessage} request
 * @returns {Promise<Record<string, unknown>>}
 */
async function jsonBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  const path = url.pathname;

  if (request.method === "GET" && path === "/api/v1/readyz") {
    return send(response, 200, { status: "ok", version: "fixture" });
  }
  if (request.method === "POST" && path === "/__reset") {
    items = freshItems();
    goals = freshGoals();
    reprocessed = [];
    verdicts = [];
    moves = [];
    events = [];
    lastMove = new Map();
    digestMinutes = [];
    emptyDigest = false;
    searches = [];
    overviews = [];
    vectorSearch = "used";
    searchPageSize = 20;
    overviewFailure = null;
    visibilities = [];
    return send(response, 200, {});
  }
  if (request.method === "GET" && path === "/__calls") {
    const calls = { reprocessed, moves, verdicts, digestMinutes, searches, overviews, visibilities };
    return send(response, 200, calls);
  }
  if (request.method === "POST" && path === "/__search") {
    // For a test: the embedder fails, or a page holds fewer hits.
    const body = await jsonBody(request);
    vectorSearch = /** @type {SearchPage["vector_search"]} */ (body.vector_search ?? vectorSearch);
    searchPageSize = Number(body.page_size ?? searchPageSize);
    return send(response, 200, {});
  }
  if (request.method === "POST" && path === "/__overview") {
    // For a test: the model cannot answer, and the API says why.
    overviewFailure = /** @type {string | null} */ ((await jsonBody(request)).failure ?? null);
    return send(response, 200, {});
  }
  if (request.method === "POST" && path === "/__digest") {
    emptyDigest = Boolean((await jsonBody(request)).empty);
    return send(response, 200, {});
  }
  let match = /^\/__items\/(\d+)$/.exec(path);
  if (request.method === "POST" && match) {
    // For a test: the worker changes the item while the page is open.
    const id = Number(match[1]);
    const item = items.find((candidate) => candidate.id === id);
    if (!item) return problem(response, 404, "Not Found", "no such item");
    Object.assign(item, await jsonBody(request));
    return send(response, 200, detailOf(item));
  }
  match = /^\/__events\/(\d+)$/.exec(path);
  if (request.method === "POST" && match) {
    // For a test: the worker or the bot wrote an event of the item.
    const body = await jsonBody(request);
    const event = {
      id: events.length + 1,
      actor: /** @type {EventOut["actor"]} */ (body.actor ?? "system"),
      type: String(body.type),
      from_status: null,
      to_status: null,
      payload: /** @type {Record<string, unknown>} */ (body.payload ?? {}),
      created_at: "2026-10-25T06:00:00Z",
    };
    events.push({ itemId: Number(match[1]), event });
    return send(response, 200, event);
  }

  if (request.method === "GET" && path === "/api/v1/items") {
    // As in the real API, a merged copy is not in a list.
    const statuses = url.searchParams.getAll("status");
    const found = items.filter(
      (item) =>
        item.duplicate_of_id === null && (statuses.length === 0 || statuses.includes(item.status)),
    );
    return send(response, 200, { items: found.map(summaryOf), next_cursor: null });
  }

  if (request.method === "GET" && path === "/api/v1/queue") {
    // The items being read first, then the items that wait longest.
    const queued = items.filter(inQueue);
    const order = (/** @type {ItemDetail} */ item) =>
      (item.status === "reading" ? 0 : 1e9) + (lastMove.get(item.id) ?? 0);
    queued.sort((one, other) => order(one) - order(other));
    return send(response, 200, {
      limit: QUEUE_LIMIT,
      used: queued.length,
      items: queued.map(summaryOf),
    });
  }

  if (request.method === "POST" && path === "/api/v1/digests/preview") {
    const minutes = (await jsonBody(request)).minutes ?? 60;
    if (typeof minutes !== "number" || !Number.isInteger(minutes) || minutes < 5 || minutes > 600) {
      return problem(response, 422, "Unprocessable Content", "the body is not valid");
    }
    digestMinutes.push(minutes);
    // As in the real API, an item without an estimate counts as 5 minutes.
    let left = minutes;
    const picks = (emptyDigest ? [] : digestPicks()).filter((pick) => {
      const length = pick.item.est_minutes ?? 5;
      if (length > left) return false;
      left -= length;
      return true;
    });
    return send(response, 200, {
      picks,
      total_minutes: minutes - left,
      goal_title: "Database internals",
    });
  }

  if (request.method === "GET" && path === "/api/v1/search") {
    // As in the real API: the query is required, and a status or a kind must exist.
    const q = (url.searchParams.get("q") ?? "").trim();
    const statuses = url.searchParams.getAll("status");
    const kinds = url.searchParams.getAll("kind");
    const goal = url.searchParams.get("goal_id");
    const cursor = url.searchParams.get("cursor");
    searches.push({ q, status: statuses, kind: kinds, goal_id: goal, cursor });
    const known = statuses.every((status) => STATUSES.includes(/** @type {ItemStatus} */ (status)));
    if (!q || q.length > 500 || !known) {
      return problem(response, 422, "Unprocessable Content", "the query is not valid");
    }
    const start = cursor === null ? 0 : Number(/^p(\d+)$/.exec(cursor)?.[1] ?? NaN);
    if (Number.isNaN(start)) {
      return problem(response, 422, "Unprocessable Content", "the cursor is not valid");
    }
    const hits = searchHits(wordsOf(q), (item) => {
      // A goal counts with a score of 2 or 3, unless you rejected the score.
      const helps = item.goals.some(
        (score) => String(score.goal_id) === goal && score.score >= 2 && score.label !== "rejected",
      );
      return (
        (statuses.length === 0 || statuses.includes(item.status)) &&
        (kinds.length === 0 || kinds.includes(item.kind)) &&
        (goal === null || helps)
      );
    });
    const end = start + searchPageSize;
    return send(response, 200, {
      items: hits.slice(start, end),
      next_cursor: end < hits.length ? `p${end}` : null,
      vector_search: vectorSearch,
    });
  }

  if (request.method === "POST" && path === "/api/v1/search/overview") {
    const body = await jsonBody(request);
    overviews.push(body);
    const q = typeof body.q === "string" ? body.q.trim() : "";
    const statuses = /** @type {string[] | null} */ (body.status ?? null);
    const limit = body.limit ?? 12;
    const known = (statuses ?? []).every((status) =>
      STATUSES.includes(/** @type {ItemStatus} */ (status)),
    );
    if (!q || q.length > 500 || !known || typeof limit !== "number" || limit < 1 || limit > 30) {
      return problem(response, 422, "Unprocessable Content", "the body is not valid");
    }
    if (overviewFailure) return problem(response, 503, "Service Unavailable", overviewFailure);
    const hits = searchHits(
      wordsOf(q),
      (item) => statuses === null || statuses.includes(item.status),
    ).slice(0, limit);
    const segments = overviewSegments(hits);
    return send(response, 200, {
      segments,
      // The items that the model read: none, when it could read none.
      sources: segments.length === 0 ? [] : hits.map((hit) => hit.item),
      vector_search: vectorSearch,
    });
  }

  match = /^\/api\/v1\/items\/(\d+)$/.exec(path);
  if (request.method === "GET" && match) {
    const id = Number(match[1]);
    const item = items.find((candidate) => candidate.id === id);
    return item
      ? send(response, 200, detailOf(item))
      : problem(response, 404, "Not Found", "no such item");
  }

  match = /^\/api\/v1\/items\/(\d+)\/transition$/.exec(path);
  if (request.method === "POST" && match) {
    const id = Number(match[1]);
    const item = items.find((candidate) => candidate.id === id);
    if (!item) return problem(response, 404, "Not Found", "no such item");
    const target = /** @type {ItemStatus} */ ((await jsonBody(request)).to);
    if (!STATUSES.includes(target)) {
      return problem(response, 422, "Unprocessable Content", "the body is not valid");
    }
    moves.push({ id, to: target });
    if (item.duplicate_of_id !== null) {
      const detail = `the item is the same thing as item ${item.duplicate_of_id}: change that one`;
      return problem(response, 409, "Conflict", detail);
    }
    if (item.status === target) return send(response, 200, detailOf(item));
    if (!MOVES[item.status].includes(target)) {
      const detail = `cannot move from '${item.status}' to '${target}'`;
      return problem(response, 409, "Conflict", detail);
    }
    const used = items.filter(inQueue).length;
    if ((target === "queued" || target === "reading") && !inQueue(item) && used >= QUEUE_LIMIT) {
      const detail =
        `the reading queue is full: all ${QUEUE_LIMIT} slots are used; ` +
        "move an item out of it first";
      return problem(response, 409, "Conflict", detail);
    }
    const now = new Date().toISOString();
    const event = {
      id: events.length + 1,
      actor: /** @type {const} */ ("user"),
      type: "status",
      from_status: item.status,
      to_status: target,
      payload: { via: "api" },
      created_at: now,
    };
    events.push({ itemId: id, event });
    lastMove.set(id, event.id);
    item.status = target;
    if (target === "done") item.read_at = now;
    return send(response, 200, detailOf(item));
  }

  match = /^\/api\/v1\/items\/(\d+)\/goals\/(\d+)\/label$/.exec(path);
  if (request.method === "PUT" && match) {
    const item = items.find((candidate) => candidate.id === Number(match?.[1]));
    const score = item?.goals.find((candidate) => candidate.goal_id === Number(match?.[2]));
    if (!score) {
      return problem(response, 404, "Not Found", "the item has no score for this goal");
    }
    const body = await jsonBody(request);
    if (!["confirmed", "rejected", null].includes(/** @type {any} */ (body.label))) {
      return problem(response, 422, "Unprocessable Content", "the body is not valid");
    }
    if (typeof body.score === "number" && body.score !== score.score) {
      const detail =
        `the score changed since you saw it: it is ${score.score} now; ` + "look at it again";
      return problem(response, 409, "Conflict", detail);
    }
    score.label = /** @type {GoalScore["label"]} */ (body.label);
    verdicts.push({ item: Number(match[1]), goal: Number(match[2]), label: score.label });
    return send(response, 200, score);
  }

  match = /^\/api\/v1\/items\/(\d+)\/events$/.exec(path);
  if (request.method === "GET" && match) {
    const id = Number(match[1]);
    if (!items.some((candidate) => candidate.id === id)) {
      return problem(response, 404, "Not Found", "no such item");
    }
    // Newest first, as in the real API.
    const found = events.filter((entry) => entry.itemId === id).map((entry) => entry.event);
    return send(response, 200, { items: found.reverse(), next_cursor: null });
  }

  match = /^\/api\/v1\/items\/(\d+)\/public-preview$/.exec(path);
  if (request.method === "GET" && match) {
    const id = Number(match[1]);
    const item = items.find((candidate) => candidate.id === id);
    if (!item) return problem(response, 404, "Not Found", "no such item");
    if (item.duplicate_of_id !== null) {
      const detail = `the item is a copy of item ${item.duplicate_of_id}: publish that one`;
      return problem(response, 409, "Conflict", detail);
    }
    const name = item.public_slug ?? publicNameOf(item.title);
    return send(response, 200, publicRecordOf(item, name, item.published_at ?? nowInTheFeed()));
  }

  match = /^\/api\/v1\/items\/(\d+)\/visibility$/.exec(path);
  if (request.method === "PUT" && match) {
    const id = Number(match[1]);
    const item = items.find((candidate) => candidate.id === id);
    if (!item) return problem(response, 404, "Not Found", "no such item");
    const body = await jsonBody(request);
    visibilities.push({ id, visibility: String(body.visibility), record: body.record ?? null });
    if (body.visibility === "private") {
      item.visibility = "private";
      return send(response, 200, { visibility: "private", public: null });
    }
    const seen = /** @type {PublicItem | undefined} */ (body.record ?? undefined);
    if (!seen) {
      const detail = "publishing needs the record of the preview: ask for the preview first";
      return problem(response, 422, "Unprocessable Content", detail);
    }
    // As the real API: the record of now must be the record that the owner saw.
    const name = item.public_slug ?? seen.id;
    const record = publicRecordOf(item, name, item.published_at ?? seen.published_at ?? "");
    if (JSON.stringify(record) !== JSON.stringify(seen)) {
      const detail = "the public record changed after the preview: look at the new preview";
      return problem(response, 409, "Conflict", detail);
    }
    item.visibility = "public";
    item.public_slug = record.id;
    item.published_at = record.published_at;
    return send(response, 200, { visibility: "public", public: record });
  }

  match = /^\/api\/v1\/items\/(\d+)\/reprocess$/.exec(path);
  if (request.method === "POST" && match) {
    const id = Number(match[1]);
    const item = items.find((candidate) => candidate.id === id);
    if (!item) return problem(response, 404, "Not Found", "no such item");
    if (item.enrich_state !== "ready" && item.enrich_state !== "failed") {
      return problem(response, 409, "Conflict", `the item is still in the pipeline (${item.enrich_state})`);
    }
    item.enrich_state = "pending";
    reprocessed.push(item.id);
    return send(response, 202, item);
  }

  if (request.method === "GET" && path === "/api/v1/goals") {
    const archived = url.searchParams.get("include_archived") === "true";
    const found = goals.filter((goal) => archived || goal.archived_at === null);
    // The active goals first, each group in the order of creation.
    found.sort(
      (one, other) => Number(isArchived(one)) - Number(isArchived(other)) || one.id - other.id,
    );
    return send(response, 200, { items: found });
  }

  if (request.method === "POST" && path === "/api/v1/goals") {
    const body = await jsonBody(request);
    const title = String(body.title ?? "").trim();
    if (!title) {
      const errors = [
        { pointer: "/body/title", detail: "String should have at least 1 character" },
      ];
      const body = { type: "about:blank", title: "Unprocessable Content", status: 422, errors };
      return send(response, 422, body, "application/problem+json");
    }
    const slug = slugFor(title);
    const clash = goals.find((goal) => goal.slug === slug);
    if (clash) {
      const where = clash.archived_at ? "an archived goal" : "a goal";
      const detail = `${where} with this title exists already: '${clash.title}'`;
      return problem(response, 409, "Conflict", detail);
    }
    if (goals.filter((goal) => !isArchived(goal)).length >= MAX_ACTIVE_GOALS) {
      return problem(response, 409, "Conflict", GOALS_FULL);
    }
    const starts = /** @type {string | null} */ (body.starts_on ?? null);
    const ends = /** @type {string | null} */ (body.ends_on ?? null);
    if (starts && ends && ends < starts) {
      return problem(response, 422, "Unprocessable Content", "the goal ends before it starts");
    }
    /** @type {GoalOut} */
    const goal = {
      id: Math.max(0, ...goals.map((candidate) => candidate.id)) + 1,
      slug,
      title,
      description: String(body.description ?? "").trim(),
      keywords: [...new Set(/** @type {string[]} */ (body.keywords ?? []))],
      starts_on: starts,
      ends_on: ends,
      version: 1,
      is_focus: false,
      archived_at: null,
    };
    goals.push(goal);
    return send(response, 201, goal);
  }

  match = /^\/api\/v1\/goals\/(\d+)$/.exec(path);
  if (request.method === "PATCH" && match) {
    const id = Number(match[1]);
    const goal = goals.find((candidate) => candidate.id === id);
    if (!goal) return problem(response, 404, "Not Found", "no such goal");
    const body = await jsonBody(request);
    for (const name of ["title", "description", "keywords", "archived"]) {
      if (name in body && body[name] === null) {
        const detail = `${name} cannot be null; leave it out to keep it`;
        return problem(response, 422, "Unprocessable Content", detail);
      }
    }
    const before = meaningOf(goal);
    const changed = { ...goal };
    if (typeof body.title === "string") changed.title = body.title.trim();
    if (typeof body.description === "string") changed.description = body.description.trim();
    if (Array.isArray(body.keywords)) {
      changed.keywords = [...new Set(/** @type {string[]} */ (body.keywords))];
    }
    if ("starts_on" in body) changed.starts_on = /** @type {string | null} */ (body.starts_on);
    if ("ends_on" in body) changed.ends_on = /** @type {string | null} */ (body.ends_on);
    if (changed.starts_on && changed.ends_on && changed.ends_on < changed.starts_on) {
      return problem(response, 422, "Unprocessable Content", "the goal ends before it starts");
    }
    if (meaningOf(changed) !== before) changed.version += 1;
    if (body.archived === true && changed.archived_at === null) {
      changed.archived_at = new Date().toISOString();
      changed.is_focus = false;
    } else if (body.archived === false && changed.archived_at !== null) {
      if (goals.filter((candidate) => !isArchived(candidate)).length >= MAX_ACTIVE_GOALS) {
        return problem(response, 409, "Conflict", GOALS_FULL);
      }
      changed.archived_at = null;
    }
    Object.assign(goal, changed);
    return send(response, 200, goal);
  }

  match = /^\/api\/v1\/goals\/(\d+)\/focus$/.exec(path);
  if (request.method === "POST" && match) {
    const id = Number(match[1]);
    const goal = goals.find((candidate) => candidate.id === id);
    if (!goal) return problem(response, 404, "Not Found", "no such goal");
    if (goal.archived_at !== null) {
      const detail = "an archived goal cannot be the focus; restore it first";
      return problem(response, 409, "Conflict", detail);
    }
    for (const other of goals) other.is_focus = other.id === id;
    return send(response, 200, goal);
  }

  match = /^\/__goals\/(\d+)$/.exec(path);
  if (request.method === "POST" && match) {
    // For a test: the goal changes in another tab while the page is open.
    const id = Number(match[1]);
    const goal = goals.find((candidate) => candidate.id === id);
    if (!goal) return problem(response, 404, "Not Found", "no such goal");
    Object.assign(goal, await jsonBody(request));
    return send(response, 200, goal);
  }

  return problem(response, 501, "Not Implemented", "not in the pretend API");
});

server.listen(port, "127.0.0.1");
