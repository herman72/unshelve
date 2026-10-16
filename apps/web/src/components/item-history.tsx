import type { components } from "@unshelve/api-client";
import type { ApiState } from "@/lib/api-state";

export type EventPage = components["schemas"]["EventPage"];
type ItemEvent = components["schemas"]["EventOut"];

// As the time of saving on the page: the server of the web app runs in UTC, so the time says so.
const AT = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "long",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
  timeZoneName: "short",
});

const VIA: Record<string, string> = { api: "through the API", telegram: "in Telegram" };

function text(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** One event in words. The payload is data of the API, so each field is checked. */
function describe(event: ItemEvent): string {
  const reason = text(event.payload.reason);
  const because = reason ? `: ${reason}` : "";
  switch (event.type) {
    case "status": {
      const via = VIA[text(event.payload.via) ?? ""];
      return `Moved from ${event.from_status} to ${event.to_status}${via ? `, ${via}` : ""}`;
    }
    case "reprocess":
      return "Sent back to the start of the pipeline";
    case "fetch_failed":
      return `The source could not be read${because}`;
    case "enrich_failed":
      return `The card could not be made${because}`;
    case "embed_failed":
      return `The vectors could not be made${because}`;
    default:
      return event.type; // a type that this page does not know yet
  }
}

/** What happened to the item, newest first: the moves of its owner and the work of the worker. */
export function ItemHistory({ history }: { history: ApiState<EventPage> }) {
  // The history is extra: without it, the rest of the page still works.
  if (history.kind !== "data") {
    return <p className="text-sm text-muted">The history cannot be shown now.</p>;
  }
  const { items, next_cursor } = history.data;
  if (items.length === 0) {
    return <p className="text-sm text-muted">Nothing in the history yet.</p>;
  }
  return (
    <>
      <ol aria-label="History" className="space-y-1 text-sm">
        {items.map((event) => (
          <li key={event.id} className="wrap-anywhere">
            <time dateTime={event.created_at} className="text-muted">
              {AT.format(new Date(event.created_at))}
            </time>{" "}
            {describe(event)}
          </li>
        ))}
      </ol>
      {next_cursor && <p className="mt-2 text-sm text-muted">Older events are not shown.</p>}
    </>
  );
}
