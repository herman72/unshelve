"use client";

import { useMemo, useState } from "react";
import type { Feed, FeedRecord } from "./feed.ts";
import { safeLink } from "./links.ts";
import { createFeedSearch } from "./search.ts";

// The same text on the server and in the browser: a fixed language and the time zone of the feed.
const DATE = new Intl.DateTimeFormat("en-GB", { dateStyle: "long", timeZone: "UTC" });

export interface FeedSearchProps {
  /** The feed, from parseFeed(). */
  feed: Feed;
  /** The name of the search field. */
  label?: string;
}

/**
 * A search field, and the records of the feed that match it. The browser makes the index and
 * searches it again at each key press: no server. Each class name starts with
 * "unshelve-search", so that the site can give the markup its own style.
 */
export function FeedSearch({ feed, label = "Search" }: FeedSearchProps) {
  const index = useMemo(() => createFeedSearch(feed), [feed]);
  const [query, setQuery] = useState("");
  const records = useMemo(() => index.search(query), [index, query]);
  const total = feed.items.length;

  return (
    <section className="unshelve-search">
      <label className="unshelve-search-field">
        <span>{label}</span>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          dir="auto"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <p className="unshelve-search-count" role="status">
        {records.length} of {total} items
      </p>
      <ol className="unshelve-search-results">
        {records.map((record) => (
          <Result key={record.id} record={record} />
        ))}
      </ol>
    </section>
  );
}

function Result({ record }: { record: FeedRecord }) {
  const link = safeLink(record.url);
  const facts = [
    record.kind,
    record.site,
    DATE.format(new Date(record.published_at)),
    record.est_minutes === null ? null : `${record.est_minutes} min`,
  ].filter((fact) => fact !== null);

  // dir="auto": a Persian record reads from right to left, an English one from left to right.
  return (
    <li className="unshelve-search-result" lang={record.lang ?? undefined} dir="auto">
      {record.title !== null && (
        <h3 className="unshelve-search-title">
          {link === null ? record.title : <a href={link}>{record.title}</a>}
        </h3>
      )}
      <p className="unshelve-search-facts">{facts.join(" · ")}</p>
      {record.summary !== null && <p className="unshelve-search-summary">{record.summary}</p>}
      {record.note !== null && <p className="unshelve-search-note">{record.note}</p>}
      {record.tags.length > 0 && (
        <ul className="unshelve-search-tags">
          {record.tags.map((tag) => (
            <li key={tag}>{tag}</li>
          ))}
        </ul>
      )}
    </li>
  );
}
