import type { components } from "@unshelve/api-client";
import Link from "next/link";
import { enrichLabel } from "@/lib/enrich-state";
import { webAddress } from "@/lib/links";

export type ItemSummary = components["schemas"]["ItemSummary"];

// Saved content mixes English and Persian. dir="auto" lets each piece of it pick its own
// direction from its first strong letter, so a Persian title is right-to-left and an English
// one is left-to-right, on the same page. Put it on every element that shows saved content.

/** The title, or the next best thing to show in its place. */
export function displayTitle(item: ItemSummary): string {
  return item.title || item.url || "Untitled";
}

export function Badge({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <span className="rounded-full border border-border px-2 py-0.5 text-xs capitalize">
      <span className="sr-only">{label}: </span>
      {children}
    </span>
  );
}

export function ItemCard({ item }: { item: ItemSummary }) {
  const source = webAddress(item.url);
  const goal = item.top_goal;

  return (
    <article className="rounded-lg border border-border bg-surface p-4">
      {/* Each item has a page of its own; not each item has a web address. */}
      <h2 dir="auto" className="text-base leading-snug font-semibold wrap-anywhere">
        <Link href={`/items/${item.id}`} className="hover:underline">
          {displayTitle(item)}
        </Link>
      </h2>

      <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted">
        {item.site &&
          (source ? (
            <a dir="auto" href={source} rel="noreferrer" className="wrap-anywhere hover:underline">
              {item.site}
            </a>
          ) : (
            <span dir="auto" className="wrap-anywhere">
              {item.site}
            </span>
          ))}
        <Badge label="Kind">{item.kind}</Badge>
        <Badge label="Status">{item.status}</Badge>
        {/* "Ready" is the normal case, so only the other states take room. */}
        {item.enrich_state !== "ready" && (
          <Badge label="Pipeline">{enrichLabel(item.enrich_state)}</Badge>
        )}
        {item.est_minutes !== null && <span>{item.est_minutes} min read</span>}
        {/* A thing that was saved again is one item. The number is a sign of interest. */}
        {item.capture_count > 1 && <span>Saved {item.capture_count} times</span>}
      </p>

      {item.summary && (
        <p dir="auto" className="mt-3 wrap-anywhere">
          {item.summary}
        </p>
      )}

      {item.tags.length > 0 && (
        <ul role="list" aria-label="Tags" className="mt-3 flex flex-wrap gap-2">
          {item.tags.map((tag) => (
            <li
              key={tag.slug}
              dir="auto"
              className="rounded-md border border-border px-2 py-0.5 text-xs text-muted"
            >
              {tag.name}
            </li>
          ))}
        </ul>
      )}

      {goal && (
        <p className="mt-3 text-sm">
          <span className="text-muted">Top goal: </span>
          <span dir="auto" className="font-medium wrap-anywhere">
            {goal.goal_title}
          </span>
          <span className="text-muted"> ({goal.score} of 3)</span>
        </p>
      )}

      {item.partial && (
        <p className="mt-3 text-sm text-warning">
          <span className="font-medium">Partial.</span> Built from the saved text, because the
          source page was not fetched.
        </p>
      )}
    </article>
  );
}
