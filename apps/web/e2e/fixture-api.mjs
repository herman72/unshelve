// A pretend Unshelve API for the end-to-end tests: the answers of the real API, with made-up
// items. It keeps the items in memory and answers each request as the owner, without a
// session. The tests read what was sent to it (GET /__calls), start each test from the same
// items (POST /__reset), and change an item between two steps (POST /__items/{id}).
//
// The moves of the reading status follow the rules of the real API: the table of moves of
// contracts/states.py, a queue of 5 slots, and no move for a merged copy. The goals too: a slug
// from the title, a clash is a 409, at most 10 active goals, a new version for a new meaning,
// and one focus.
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
      // Done, so not in the inbox. Its id is 1000: "/items/1e3" must not find it.
      id: 1000,
      status: "done",
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
    return send(response, 200, {});
  }
  if (request.method === "GET" && path === "/__calls") {
    return send(response, 200, { reprocessed, moves, verdicts });
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
