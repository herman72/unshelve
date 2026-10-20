import Link from "next/link";
import { Fragment } from "react";
import { Badge, displayTitle } from "@/components/item-card";
import { SNIPPET_FROM, type SearchHit, type Snippet, foundBy } from "@/lib/search";

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

/** One result of a search: the item, why it was found, and whether you read it. */
export function SearchHitCard({ hit }: { hit: SearchHit }) {
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

      {found && <p className="mt-3 text-xs text-muted">Found by {found}.</p>}
    </article>
  );
}
