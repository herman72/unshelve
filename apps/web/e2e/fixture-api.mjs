// A pretend Unshelve API for the end-to-end tests: the answers of the real API, with made-up
// items. It keeps the items in memory and answers each request as the owner, without a
// session. The tests read what was sent to it (GET /__calls), start each test from the same
// items (POST /__reset), and change an item between two steps (POST /__items/{id}).
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

const port = Number(process.argv[2] ?? 3901);

const CAPTURED = "2026-10-15T08:30:00Z";

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

let items = freshItems();
/** @type {number[]} */
let reprocessed = [];

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
    return send(response, 200, {});
  }
  if (request.method === "GET" && path === "/__calls") return send(response, 200, { reprocessed });
  let match = /^\/__items\/(\d+)$/.exec(path);
  if (request.method === "POST" && match) {
    // For a test: the worker changes the item while the page is open.
    const id = Number(match[1]);
    const item = items.find((candidate) => candidate.id === id);
    if (!item) return problem(response, 404, "Not Found", "no such item");
    Object.assign(item, await jsonBody(request));
    return send(response, 200, item);
  }

  if (request.method === "GET" && path === "/api/v1/items") {
    const statuses = url.searchParams.getAll("status");
    const found = items.filter((item) => statuses.length === 0 || statuses.includes(item.status));
    return send(response, 200, { items: found.map(summaryOf), next_cursor: null });
  }

  match = /^\/api\/v1\/items\/(\d+)$/.exec(path);
  if (request.method === "GET" && match) {
    const id = Number(match[1]);
    const item = items.find((candidate) => candidate.id === id);
    return item ? send(response, 200, item) : problem(response, 404, "Not Found", "no such item");
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
