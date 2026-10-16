import type { components } from "@unshelve/api-client";
import Link from "next/link";
import { Badge, displayTitle } from "@/components/item-card";
import { type EventPage, ItemHistory } from "@/components/item-history";
import { ReprocessButton } from "@/components/reprocess-button";
import { StatusButtons } from "@/components/status-buttons";
import type { ApiState } from "@/lib/api-state";
import { enrichLabel, inPipeline } from "@/lib/enrich-state";
import { webAddress } from "@/lib/links";
import { statusSentence } from "@/lib/reading-status";

export type ItemDetail = components["schemas"]["ItemDetail"];

// The server of the web app runs in UTC, so the time says so. The date and the hour are enough
// to find the message in Telegram.
const SAVED_AT = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "long",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
  timeZoneName: "short",
});

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-6">
      <h2 className="text-sm font-semibold tracking-wide text-muted uppercase">{title}</h2>
      <div className="mt-2">{children}</div>
    </section>
  );
}

/** One item in full. Each piece of saved content has dir="auto": see item-card.tsx. */
export function ItemView({ item, history }: { item: ItemDetail; history: ApiState<EventPage> }) {
  const source = webAddress(item.url);

  return (
    <article>
      <Link href="/" className="text-sm text-accent hover:underline">
        Back to the inbox
      </Link>
      <h1 dir="auto" className="mt-3 text-2xl font-semibold tracking-tight wrap-anywhere">
        {displayTitle(item)}
      </h1>
      <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted">
        {item.site && (
          <span dir="auto" className="wrap-anywhere">
            {item.site}
          </span>
        )}
        {item.author && (
          <span>
            by{" "}
            <span dir="auto" className="wrap-anywhere">
              {item.author}
            </span>
          </span>
        )}
        <Badge label="Kind">{item.kind}</Badge>
        <Badge label="Status">{item.status}</Badge>
        <Badge label="Pipeline">{enrichLabel(item.enrich_state)}</Badge>
        {item.est_minutes !== null && <span>{item.est_minutes} min read</span>}
      </p>
      {source && (
        <p className="mt-2 text-sm">
          <a href={source} rel="noreferrer" className="text-accent hover:underline">
            Open the source
          </a>
        </p>
      )}

      <Section title="Reading">
        {item.duplicate_of_id !== null ? (
          // The API allows no move for a merged copy: its moves belong to the older item.
          <p className="text-sm">
            This is the same thing as{" "}
            <Link href={`/items/${item.duplicate_of_id}`} className="text-accent hover:underline">
              item {item.duplicate_of_id}
            </Link>
            . Move that item.
          </p>
        ) : (
          <>
            <p className="mb-2 text-sm">
              {item.status === "done" && item.read_at ? (
                <>
                  Read on{" "}
                  <time dateTime={item.read_at}>{SAVED_AT.format(new Date(item.read_at))}</time>.
                </>
              ) : (
                statusSentence(item.status)
              )}
            </p>
            <StatusButtons
              itemId={item.id}
              status={item.status}
              allowed={item.allowed_transitions}
            />
          </>
        )}
      </Section>

      {item.partial && (
        <p className="mt-4 text-sm text-warning">
          <span className="font-medium">Partial.</span> Built from the saved text, because the
          source was not read.
        </p>
      )}

      {item.summary && (
        <Section title="Summary">
          <p dir="auto" className="wrap-anywhere">
            {item.summary}
          </p>
        </Section>
      )}

      {item.key_points.length > 0 && (
        <Section title="Key points">
          {/* The marker inside the item: a Persian point gets it on its right, in the column. */}
          <ul className="list-inside list-disc space-y-1">
            {item.key_points.map((point, index) => (
              <li key={index} dir="auto" className="wrap-anywhere">
                {point}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {item.tags.length > 0 && (
        <Section title="Tags">
          <ul role="list" aria-label="Tags" className="flex flex-wrap gap-2">
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
        </Section>
      )}

      {item.extra_links.length > 0 && (
        <Section title="Links in it">
          <ul className="space-y-1 text-sm">
            {item.extra_links.map((link) => {
              // A link of saved content is a link only when it is a web address.
              const href = webAddress(link);
              return (
                <li key={link} className="wrap-anywhere">
                  {href ? (
                    <a href={href} rel="noreferrer" className="text-accent hover:underline">
                      {link}
                    </a>
                  ) : (
                    <span className="text-muted">{link}</span>
                  )}
                </li>
              );
            })}
          </ul>
        </Section>
      )}

      {item.note && (
        <Section title="Your note">
          <p dir="auto" className="wrap-anywhere">
            {item.note}
          </p>
        </Section>
      )}

      <Section title="Saved">
        <p className="text-sm">
          <time dateTime={item.first_captured_at}>
            {SAVED_AT.format(new Date(item.first_captured_at))}
          </time>
          {item.capture_count > 1 && <span>, and {item.capture_count - 1} times after</span>}
        </p>
      </Section>

      <Section title="History">
        <ItemHistory history={history} />
      </Section>

      <Section title="Processing">
        {inPipeline(item.enrich_state) ? (
          <p role="status" className="text-sm">
            The worker is at work on this item. Reload the page to see its new state.
          </p>
        ) : (
          <>
            <p className="text-sm text-muted">
              Read the source again, and make the card again. The model is asked again only
              when the text changed.
            </p>
            <ReprocessButton itemId={item.id} />
          </>
        )}
      </Section>
    </article>
  );
}
