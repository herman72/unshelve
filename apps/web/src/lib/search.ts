import type { components } from "@unshelve/api-client";
import type { ItemSummary } from "@/components/item-card";
import type { ItemStatus } from "@/lib/reading-status";

export type SearchPage = components["schemas"]["SearchPage"];
export type SearchHit = components["schemas"]["SearchHit"];
export type Snippet = components["schemas"]["Snippet"];
export type Overview = components["schemas"]["Overview"];
export type ItemKind = components["schemas"]["ItemKind"];
export type VectorSearch = SearchPage["vector_search"];

// Each status and each kind of the API, with its name on the page. `satisfies Record<…>` makes
// the compiler refuse a list that misses one: a new kind in the API fails the type check here.
const STATUS_NAMES = {
  inbox: "Inbox",
  queued: "Queued",
  reading: "Reading",
  done: "Done",
  archived: "Archived",
} satisfies Record<ItemStatus, string>;

const KIND_NAMES = {
  article: "Article",
  video: "Video",
  post: "Post",
  repo: "Repository",
  paper: "Paper",
  image: "Image",
  note: "Note",
  book: "Book",
  pdf: "PDF",
  unknown: "Unknown",
} satisfies Record<ItemKind, string>;

type Choice<T> = { value: T; label: string };

function choices<T extends string>(names: Record<T, string>): Choice<T>[] {
  return (Object.entries(names) as [T, string][]).map(([value, label]) => ({ value, label }));
}

export const STATUS_CHOICES = choices<ItemStatus>(STATUS_NAMES);
export const KIND_CHOICES = choices<ItemKind>(KIND_NAMES);

/**
 * A search as the address of the page holds it. The form shows these values, and the API gets
 * them: one source for both, so the page never shows a filter that the API did not use.
 */
export type Search = {
  q: string;
  status: ItemStatus[];
  // Only the items without a read time. Not a status: an item keeps its read time when it
  // leaves "done".
  unread: boolean;
  kind: ItemKind | null;
  goalId: number | null;
  // The slugs of the tag filters, in the order of the address, each one once. An item must have
  // each of them.
  tags: string[];
  cursor: string | null;
};

type Params = Record<string, string | string[] | undefined>;

function values(value: string | string[] | undefined): string[] {
  if (value === undefined) return [];
  return Array.isArray(value) ? value : [value];
}

const isKind = (value: string): value is ItemKind => Object.hasOwn(KIND_NAMES, value);

// An id of the API: digits only, from 1. Number("1e3") would be 1000, so a pattern decides.
const ID = /^[1-9][0-9]{0,15}$/;

// A tag of the API: lowercase letters and digits, with one hyphen between two words, at most 80
// characters (Slug in apps/api/src/unshelve/api/schemas.py). The API refuses the whole search
// for any other value, so the page never sends one.
const TAG = /^(?=.{1,80}$)[a-z0-9]+(?:-[a-z0-9]+)*$/;

/** Whether the API can filter by this tag. */
export const isTag = (slug: string): boolean => TAG.test(slug);

/**
 * The search in the query string of the page. A value that the API does not know is left out,
 * and the form shows one kind and one goal, so only the first of each counts.
 */
export function parseSearch(params: Params): Search {
  const given = values(params.status);
  const goal = values(params.goal_id)[0] ?? "";
  return {
    q: (values(params.q)[0] ?? "").trim(),
    // In the order of the API, each status once: ?status=done&status=done is one filter.
    status: STATUS_CHOICES.map(({ value }) => value).filter((status) => given.includes(status)),
    // The box of the form sends unread=true. Any other value leaves the filter off.
    unread: values(params.unread)[0] === "true",
    kind: values(params.kind).find(isKind) ?? null,
    goalId: ID.test(goal) ? Number(goal) : null,
    // Each tag once: ?tag=queues&tag=queues is one filter.
    tags: [...new Set(values(params.tag).filter(isTag))],
    cursor: values(params.cursor)[0] || null,
  };
}

/**
 * The filters of a search, as the API reads them: only those that are set. The search and the
 * overview both send them, so the model reads the best items of the results that you see.
 */
function apiFilters(search: Search) {
  return {
    ...(search.status.length > 0 && { status: search.status }),
    ...(search.unread && { unread: true }),
    ...(search.kind !== null && { kind: [search.kind] }),
    ...(search.goalId !== null && { goal_id: search.goalId }),
    ...(search.tags.length > 0 && { tag: search.tags }),
  };
}

/** The query of GET /api/v1/search. */
export function apiQuery(search: Search) {
  return {
    q: search.q,
    ...apiFilters(search),
    ...(search.cursor !== null && { cursor: search.cursor }),
  };
}

/** The body of POST /api/v1/search/overview: the words and the filters of the search. */
export function overviewBody(search: Search, limit: number) {
  return { q: search.q, ...apiFilters(search), unread: search.unread, limit };
}

/**
 * The address of a search, with the fields in the order of the form. Without a cursor, it is
 * also the name of the search: the fields of the form and the results start again when it changes.
 */
export function searchHref(search: Search, cursor: string | null = null): string {
  const params = new URLSearchParams({ q: search.q });
  for (const status of search.status) params.append("status", status);
  if (search.unread) params.set("unread", "true");
  if (search.kind !== null) params.set("kind", search.kind);
  if (search.goalId !== null) params.set("goal_id", String(search.goalId));
  for (const tag of search.tags) params.append("tag", tag);
  if (cursor !== null) params.set("cursor", cursor);
  return `/search?${params}`;
}

/**
 * The same search with one more tag filter. It starts from the first page: a cursor belongs to
 * one search, and the API refuses it for another.
 */
export function withTag(search: Search, slug: string): Search {
  const tags = search.tags.includes(slug) ? search.tags : [...search.tags, slug];
  return { ...search, tags, cursor: null };
}

/** The same search without one of its tag filters, from the first page. */
export function withoutTag(search: Search, slug: string): Search {
  return { ...search, tags: search.tags.filter((tag) => tag !== slug), cursor: null };
}

/** What the vector search did, in a sentence. Null when it compared meanings as it should. */
export function vectorNote(state: VectorSearch): string | null {
  switch (state) {
    case "used":
      return null;
    case "no_vectors":
      return (
        "Found by words only: meanings were not compared, because your items have no vectors " +
        "of this embedder yet."
      );
    case "failed":
      return (
        "Found by words only this time: meanings were not compared, because the embedder did " +
        "not answer in time."
      );
  }
}

const ORDINAL = new Intl.PluralRules("en-GB", { type: "ordinal" });
const SUFFIX: Record<Intl.LDMLPluralRule, string> = {
  zero: "th",
  one: "st",
  two: "nd",
  few: "rd",
  many: "th",
  other: "th",
};
const AND = new Intl.ListFormat("en-GB", { style: "long", type: "conjunction" });

/** 1st, 2nd, 3rd, 4th, 11th, 21st: the rules of English, from Intl.PluralRules. */
export function ordinal(place: number): string {
  return `${place}${SUFFIX[ORDINAL.select(place)]}`;
}

const RETRIEVERS = [
  ["fts", "words"],
  ["trigram", "spelling"],
  ["vector", "meaning"],
] as const;

/**
 * How search found an item, from its place in the list of each retriever before the fusion:
 * "words (1st) and meaning (4th)". Null when no list has it, which the API never sends.
 */
export function foundBy(ranks: SearchHit["ranks"]): string | null {
  const found = RETRIEVERS.flatMap(([retriever, name]) => {
    const place = ranks[retriever];
    return place === null ? [] : [`${name} (${ordinal(place)})`];
  });
  return found.length > 0 ? AND.format(found) : null;
}

/** Where the passage of a snippet comes from, as a caption above it. */
export const SNIPPET_FROM: Record<Snippet["source"], string> = {
  text: "In the saved text",
  summary: "In the summary",
  passage: "The closest passage in meaning",
};

/** A source of an overview: an item that the model read, and what the answer quotes from it. */
export type Source = {
  number: number;
  itemId: number;
  item: ItemSummary | null;
  quotes: string[];
};

/**
 * The sources of an overview with their numbers, and the numbers that each segment cites.
 *
 * The numbers follow the items that the model read, the best first, so [1] is the same item
 * wherever it stands. A segment cites an item once, also with two quotes from it. An item that
 * a citation names but the list of sources lacks still gets a number: a citation is never lost.
 */
export function numberSources(overview: Overview): { sources: Source[]; cites: number[][] } {
  const byItem = new Map<number, Source>();
  const sourceOf = (itemId: number, item: ItemSummary | null): Source => {
    let source = byItem.get(itemId);
    if (source === undefined) {
      source = { number: byItem.size + 1, itemId, item, quotes: [] };
      byItem.set(itemId, source);
    }
    return source;
  };
  for (const item of overview.sources) sourceOf(item.id, item);
  const cites = overview.segments.map((segment) => {
    const numbers: number[] = [];
    for (const citation of segment.citations) {
      const source = sourceOf(citation.item_id, null);
      if (!source.quotes.includes(citation.quote)) source.quotes.push(citation.quote);
      if (!numbers.includes(source.number)) numbers.push(source.number);
    }
    return numbers;
  });
  return { sources: [...byItem.values()], cites };
}
