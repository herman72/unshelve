"use client";

import type { components } from "@unshelve/api-client";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { browserApi } from "@/lib/browser-api";
import { webAddress } from "@/lib/links";

type PublicItem = components["schemas"]["PublicItem"];

// The server of the web app runs in UTC, as the time of the feed does.
const PUBLISHED_AT = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "long",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
  timeZoneName: "short",
});

const SIGNED_OUT = "You are signed out. Send /login to your Unshelve bot to get a sign-in link.";
const NO_API = "Cannot reach the API. Check that it runs, then try again.";

/** The answer of the API in words for a person, for the statuses that every request can get. */
function messageFor(status: number): string {
  switch (status) {
    case 401:
      return SIGNED_OUT;
    case 404:
      return "This item does not exist any more.";
    default:
      return `The API answered with status ${status}. Try again in a moment.`;
  }
}

type Props = {
  itemId: number;
  visibility: "private" | "public";
  publicSlug: string | null;
  publishedAt: string | null;
  copyOf: number | null;
};

/**
 * Publish an item, or make it private again (docs/learn/45).
 *
 * A publication shows the public record first, exactly as the feed will hold it. "Publish"
 * sends that record back, and the API publishes it only if it is still the same. When it
 * changed in between, the panel shows the record of now, and the owner looks again.
 */
export function PublishPanel({ itemId, visibility, publicSlug, publishedAt, copyOf }: Props) {
  const router = useRouter();
  const [preview, setPreview] = useState<PublicItem | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [refreshing, startRefresh] = useTransition();
  const path = { item_id: itemId };

  /** The record of now from the API, or a message. True when the panel shows a record. */
  async function loadPreview(): Promise<boolean> {
    const { data, error, response } = await browserApi.GET(
      "/api/v1/items/{item_id}/public-preview",
      { params: { path } },
    );
    if (data) {
      setPreview(data);
      return true;
    }
    setPreview(null);
    setMessage(response.status === 409 && error?.detail ? error.detail : messageFor(response.status));
    return false;
  }

  async function run(action: () => Promise<void>) {
    setSending(true);
    setMessage(null);
    try {
      await action();
    } catch {
      setMessage(NO_API);
    } finally {
      setSending(false);
    }
  }

  const askForPreview = () => run(async () => void (await loadPreview()));

  const publish = (record: PublicItem) =>
    run(async () => {
      const { response } = await browserApi.PUT("/api/v1/items/{item_id}/visibility", {
        params: { path },
        body: { visibility: "public", record },
      });
      if (response.status === 200) {
        setPreview(null);
        setMessage("Published. The next publish job puts it into your feed.");
        startRefresh(() => router.refresh());
      } else if (response.status === 409) {
        // The record changed, or the preview was too old: show the record of now.
        if (await loadPreview()) {
          setMessage(
            "The public record changed after your preview. This is the record now: look at " +
              "it, then publish again.",
          );
        }
      } else {
        setMessage(messageFor(response.status));
      }
    });

  const makePrivate = () =>
    run(async () => {
      const { response } = await browserApi.PUT("/api/v1/items/{item_id}/visibility", {
        params: { path },
        body: { visibility: "private" },
      });
      if (response.status === 200) {
        setMessage("Private again. The next publish job takes it out of your feed.");
        startRefresh(() => router.refresh());
      } else {
        setMessage(messageFor(response.status));
      }
    });

  const busy = sending || refreshing;
  const button =
    "rounded-md border border-muted px-3 py-1.5 text-sm font-medium hover:bg-surface disabled:opacity-60";

  if (copyOf !== null && visibility === "private") {
    // The public view leaves copies out, so the API refuses to publish one. The section
    // "Reading" above links to the item that it is a copy of.
    return <p className="text-sm">A copy is not published. Publish item {copyOf} instead.</p>;
  }

  return (
    <div>
      {visibility === "public" && publicSlug && publishedAt ? (
        <>
          <p className="text-sm">
            Public as <code dir="auto">{publicSlug}</code> since{" "}
            <time dateTime={publishedAt}>{PUBLISHED_AT.format(new Date(publishedAt))}</time>.
          </p>
          <button type="button" onClick={makePrivate} disabled={busy} className={`${button} mt-2`}>
            {busy ? "Sending…" : "Make private"}
          </button>
        </>
      ) : preview ? (
        <PublicRecord record={preview}>
          <div className="mt-3 flex gap-2">
            <button type="button" onClick={() => publish(preview)} disabled={busy} className={button}>
              {busy ? "Sending…" : "Publish"}
            </button>
            <button
              type="button"
              onClick={() => setPreview(null)}
              disabled={busy}
              className={button}
            >
              Cancel
            </button>
          </div>
        </PublicRecord>
      ) : (
        <>
          <p className="text-sm text-muted">
            Only you see this item. Before it is public, you see the record that your site gets.
          </p>
          <button type="button" onClick={askForPreview} disabled={busy} className={`${button} mt-2`}>
            {busy ? "Sending…" : "Preview the public record"}
          </button>
        </>
      )}
      <p role="status" className="mt-2 text-sm text-muted">
        {message}
      </p>
    </div>
  );
}

/** The public record as the feed holds it: each field that a visitor of your site can see. */
function PublicRecord({ record, children }: { record: PublicItem; children: React.ReactNode }) {
  const source = webAddress(record.url);
  return (
    <section aria-label="The public record" className="rounded-md border border-border p-3">
      <p className="text-sm text-muted">Your site gets exactly this, and nothing else:</p>
      <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-muted">Name</dt>
        <dd dir="auto" className="wrap-anywhere">
          {record.id}
        </dd>
        <dt className="text-muted">Title</dt>
        <dd dir="auto" className="wrap-anywhere">
          {record.title ?? "—"}
        </dd>
        <dt className="text-muted">Link</dt>
        <dd className="wrap-anywhere">
          {source ? (
            <a href={source} rel="noreferrer" className="text-accent hover:underline">
              {record.url}
            </a>
          ) : (
            (record.url ?? "—")
          )}
        </dd>
        <dt className="text-muted">Summary</dt>
        <dd dir="auto" className="wrap-anywhere">
          {record.summary ?? "—"}
        </dd>
        <dt className="text-muted">Key points</dt>
        <dd>
          {record.key_points.length > 0 ? (
            <ul className="list-inside list-disc">
              {record.key_points.map((point, index) => (
                <li key={index} dir="auto" className="wrap-anywhere">
                  {point}
                </li>
              ))}
            </ul>
          ) : (
            "—"
          )}
        </dd>
        <dt className="text-muted">Your public note</dt>
        <dd dir="auto" className="wrap-anywhere">
          {record.note ?? "—"}
        </dd>
        <dt className="text-muted">Tags</dt>
        <dd dir="auto" className="wrap-anywhere">
          {record.tags.length > 0 ? record.tags.join(", ") : "—"}
        </dd>
        <dt className="text-muted">Published</dt>
        <dd>
          {record.published_at ? (
            <time dateTime={record.published_at}>
              {PUBLISHED_AT.format(new Date(record.published_at))}
            </time>
          ) : (
            "—"
          )}
        </dd>
      </dl>
      <details className="mt-2 text-sm">
        <summary className="cursor-pointer text-muted">The record as JSON</summary>
        <pre dir="ltr" className="mt-1 overflow-x-auto text-xs">
          {JSON.stringify(record, null, 2)}
        </pre>
      </details>
      {children}
    </section>
  );
}
