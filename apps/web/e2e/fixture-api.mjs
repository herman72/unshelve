// A pretend Unshelve API for the end-to-end tests: the answers of the real API, with made-up
// items. It keeps the items in memory and answers each request as the owner, without a
// session. The tests read what was sent to it (GET /__calls), start each test from the same
// items (POST /__reset), and change an item between two steps (POST /__items/{id}).
//
// The moves of the reading status follow the rules of the real API: the table of moves of
// contracts/states.py, a queue of 5 slots, and no move for a merged copy.
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
 * The made-up items, as GET /api/v1/items/{id} gives them. Each one is a case for the pages.
 *
 * @returns {ItemDetail[]}
 */
function freshItems() {
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

let items = freshItems();
/** @type {number[]} */
let reprocessed = [];
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
    reprocessed = [];
    moves = [];
    events = [];
    lastMove = new Map();
    return send(response, 200, {});
  }
  if (request.method === "GET" && path === "/__calls") {
    return send(response, 200, { reprocessed, moves });
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

  return problem(response, 501, "Not Implemented", "not in the pretend API");
});

server.listen(port, "127.0.0.1");
