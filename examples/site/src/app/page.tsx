import { parseFeed } from "@unshelve/site-search";
import { FeedSearch } from "@unshelve/site-search/react";
// A real site imports the feed.json that the git target of Unshelve commits into its repository.
import data from "@unshelve/site-search/fixtures/feed.json";

// This runs when the site is built: a feed of a version that the site does not know stops the
// build, so a visitor never sees a page made from a feed that it cannot read.
const feed = parseFeed(data);

export default function OddsAndEnds() {
  return (
    <main>
      <h1>Odds and ends</h1>
      <p>Things that I read, watched and kept. The search runs in your browser.</p>
      <FeedSearch feed={feed} />
    </main>
  );
}
