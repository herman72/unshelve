import MiniSearch from "minisearch";
import type { Feed, FeedRecord } from "./feed.ts";
import { uNorm } from "./normalize.ts";

/** The fields that a search looks in. */
const SEARCHED_FIELDS = ["title", "summary", "key_points", "note", "tags", "author", "site"];

// The splitter of MiniSearch: at each space and each punctuation mark, of any script.
const split = MiniSearch.getDefault("tokenize") as (text: string) => string[];

/**
 * The words of a text, as the server makes them: first the whole text is normalised, then it
 * is split. So a zero-width non-joiner, which u_norm() makes a space, separates two words.
 */
function words(text: string): string[] {
  return split(uNorm(text)).filter((word) => word !== "");
}

export interface FeedSearch {
  /**
   * The records that have each word of the query, or a word that starts with it; the best
   * first. A query with no words gives all the records, in the order of the feed.
   */
  search(query: string): readonly FeedRecord[];
}

/** An index of the records of `feed`, made in the browser: it needs no server. */
export function createFeedSearch(feed: Feed): FeedSearch {
  const index = new MiniSearch<FeedRecord>({
    fields: SEARCHED_FIELDS,
    // A list, such as the tags, is searched as one text.
    extractField: (record, field) => {
      const value: unknown = record[field as keyof FeedRecord];
      return Array.isArray(value) ? value.join(" ") : value;
    },
    tokenize: words,
    // The words are normalised already.
    processTerm: (word) => word,
    searchOptions: { combineWith: "AND", prefix: true, boost: { title: 2 } },
  });
  index.addAll(feed.items);
  const byId = new Map(feed.items.map((record) => [record.id, record]));

  return {
    search(query) {
      if (words(query).length === 0) return feed.items;
      return index.search(query).flatMap((result) => byId.get(result.id) ?? []);
    },
  };
}
