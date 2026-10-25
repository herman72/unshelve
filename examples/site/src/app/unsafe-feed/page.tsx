import { type FeedRecord, parseFeed } from "@unshelve/site-search";
import { FeedSearch } from "@unshelve/site-search/react";

// Not a page of a real site: the end-to-end tests read it. The server writes only web links,
// but a site must not trust that: an address that is not one must not become a link.
const RECORD: FeedRecord = {
  id: "",
  kind: "article",
  title: null,
  url: null,
  author: null,
  site: null,
  lang: "en",
  summary: null,
  key_points: [],
  est_minutes: null,
  note: null,
  published_at: "2026-10-22T07:00:00Z",
  tags: [],
};

const feed = parseFeed({
  version: 1,
  items: [
    { ...RECORD, id: "script", title: "An address with a script", url: "javascript:alert(1)" },
    { ...RECORD, id: "data", title: "An address with data", url: "data:text/html,<b>x</b>" },
    { ...RECORD, id: "relative", title: "An address on this site", url: "/admin" },
    { ...RECORD, id: "web", title: "A web address", url: "https://example.com/a" },
  ],
});

export default function UnsafeFeed() {
  return (
    <main>
      <h1>A feed with unsafe addresses</h1>
      <FeedSearch feed={feed} />
    </main>
  );
}
