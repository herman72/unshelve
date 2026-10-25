import Link from "next/link";
import { Fragment } from "react";
import { Badge, displayTitle } from "@/components/item-card";
import {
  SNIPPET_FROM,
  type Search,
  type SearchHit,
  type Snippet,
  foundBy,
  isTag,
  searchHref,
  withTag,
} from "@/lib/search";

// A date is enough for a mark. The server of the web app runs in UTC, as on the item page.
const READ_ON = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

/**
 * Whether you read the item. The API sets read_at when the item becomes "done", and keeps it
 * when the item moves on: an item that you read and then archived is still read.
 */
function ReadMark({ readAt }: { readAt: string | null }) {
  if (readAt === null) {
    return (
      // The words carry the state; the dot only helps the eye find unread items in a list.
      <span className="font-medium text-foreground">
        <span
          aria-hidden="true"
          className="me-1.5 inline-block size-2 rounded-full bg-accent align-middle"
        />
        Unread
      </span>
    );
  }
  return (
    <span>
      <span aria-hidden="true" className="me-1">
        ✓
      </span>
      Read on <time dateTime={readAt}>{READ_ON.format(new Date(readAt))}</time>
    </span>
  );
}

/**
 * The passage that shows why search found the item. The parts are plain text from the item,
 * and React writes them as text: a part that looks like HTML stays visible as characters.
 */
function SnippetView({ snippet }: { snippet: Snippet }) {
  return (
    <figure className="mt-3">
      <figcaption className="text-xs text-muted">{SNIPPET_FROM[snippet.source]}</figcaption>
      {/* border-s and ps follow the direction of the text: the bar is on the right for Persian. */}
      <blockquote dir="auto" className="mt-1 border-s-2 border-border ps-3 wrap-anywhere">
        {snippet.parts.map((part, index) =>
          part.hit ? (
            <mark key={index} className="rounded-sm bg-highlight px-0.5 text-foreground">
              {part.text}
            </mark>
          ) : (
            <Fragment key={index}>{part.text}</Fragment>
          ),
        )}
      </blockquote>
    </figure>
  );
}

const TAG = "inline-block rounded-md border px-2 py-0.5";

/**
 * The tags of the item. Each one is a link to the same search with the tag as one more filter.
 * The hit shows the name of a tag ("job leases"), and the link carries its slug ("job-leases"),
 * which is what the API filters by. A tag that is a filter already is no link: a click would
 * give the same search. A slug that the API would refuse is no link either.
 */
function HitTags({ tags, search }: { tags: SearchHit["item"]["tags"]; search: Search }) {
  if (tags.length === 0) return null;

  return (
    <ul role="list" aria-label="Tags" className="mt-3 flex flex-wrap gap-2 text-xs">
      {tags.map((tag) => (
        <li key={tag.slug} dir="auto">
          {search.tags.includes(tag.slug) ? (
            <span className={`${TAG} border-foreground font-medium`}>
              {tag.name}
              <span className="sr-only"> (a filter of this search)</span>
            </span>
          ) : isTag(tag.slug) ? (
            <Link
              href={searchHref(withTag(search, tag.slug))}
              aria-label={`Filter by the tag ${tag.name}`}
              className={`${TAG} border-border text-muted hover:border-muted hover:text-foreground`}
            >
              {tag.name}
            </Link>
          ) : (
            <span className={`${TAG} border-border text-muted`}>{tag.name}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

/** One result of a search: the item, why it was found, and whether you read it. */
export function SearchHitCard({ hit, search }: { hit: SearchHit; search: Search }) {
  const { item, snippet } = hit;
  const found = foundBy(hit.ranks);

  return (
    <article className="rounded-lg border border-border bg-surface p-4">
      <h3 dir="auto" className="text-base leading-snug font-semibold wrap-anywhere">
        <Link href={`/items/${item.id}`} className="hover:underline">
          {displayTitle(item)}
        </Link>
      </h3>

      <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted">
        <ReadMark readAt={item.read_at} />
        {item.site && (
          <span dir="auto" className="wrap-anywhere">
            {item.site}
          </span>
        )}
        <Badge label="Kind">{item.kind}</Badge>
        <Badge label="Status">{item.status}</Badge>
        {item.est_minutes !== null && <span>{item.est_minutes} min read</span>}
      </p>

      {/* No snippet: the title or the summary already shows why the item was found. */}
      {snippet ? (
        <SnippetView snippet={snippet} />
      ) : (
        item.summary && (
          <p dir="auto" className="mt-3 wrap-anywhere">
            {item.summary}
          </p>
        )
      )}

      <HitTags tags={item.tags} search={search} />

      {found && <p className="mt-3 text-xs text-muted">Found by {found}.</p>}
    </article>
  );
}
