import type { components } from "@unshelve/api-client";
import type { EnrichState } from "@/lib/enrich-state";
import type { ItemStatus } from "@/lib/reading-status";

export type Stats = components["schemas"]["Stats"];
export type JobKindStats = components["schemas"]["JobKindStats"];
export type LlmCosts = components["schemas"]["LLMCostStats"];
type JobKind = components["schemas"]["JobKind"];

/**
 * A wait longer than this means that the workers are far behind, or that none runs.
 *
 * One answer cannot show that a wait grows, so the page uses a limit. An idle worker looks for a
 * job each second (`poll_interval` in queue/worker.py), and a busy one takes the next job when
 * its job ends. A handler stops after 120 seconds at most (queue/registry.py), so 5 minutes is
 * more than two jobs that each use all their time.
 */
export const LONG_WAIT_SECONDS = 5 * 60;

/** What a person must look at in one kind of job: dead jobs, or a wait over the limit. */
export function warningsOf(kind: JobKindStats): string[] {
  const warnings: string[] = [];
  if (kind.dead > 0) {
    const recent = kind.dead_last_24_hours > 0 ? `, ${kind.dead_last_24_hours} in 24 h` : "";
    warnings.push(`${kind.dead} dead${recent}`);
  }
  if (isLongWait(kind.oldest_ready_seconds)) warnings.push("waits long");
  return warnings;
}

export function isLongWait(seconds: number | null): boolean {
  return seconds !== null && seconds > LONG_WAIT_SECONDS;
}

/** Whether the kind has no job at all, so that its row can step back. */
export function isIdle(kind: JobKindStats): boolean {
  return (
    kind.ready + kind.scheduled + kind.running + kind.dead === 0 &&
    kind.done_last_24_hours + kind.dead_last_24_hours === 0
  );
}

/**
 * Whether today's spend has reached the daily budget. It is the test of the ledger
 * (`_check_budget` in providers/ledger.py): `>=`, so a budget of $0 is always used.
 */
export function budgetUsed(llm: LlmCosts): boolean {
  return llm.spent_today_usd >= llm.daily_budget_usd;
}

const CENTS = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const BELOW_A_CENT = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumSignificantDigits: 2,
});

/** "$1.01", and two significant digits below one cent: one cheap call costs $0.00027. */
export function dollars(amount: number): string {
  const format = amount !== 0 && Math.abs(amount) < 0.01 ? BELOW_A_CENT : CENTS;
  return format.format(amount);
}

const COUNT = new Intl.NumberFormat("en-GB");

/** "2,500": a count with a separator for each thousand. */
export function count(value: number): string {
  return COUNT.format(value);
}

/** "42 s", "1 min 35 s", "2 h 5 min" or "3 d 4 h": the two largest units are enough. */
export function duration(seconds: number): string {
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const [m, h, d] = [Math.floor(s / 60), Math.floor(s / 3600), Math.floor(s / 86400)];
  if (s < 3600) return s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
  if (s < 86400) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

const ONE_DECIMAL = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1 });

/** "1 day", "4.5 days". */
export function days(value: number): string {
  const text = ONE_DECIMAL.format(value);
  return `${text} ${text === "1" ? "day" : "days"}`;
}

// The server of the web app runs in UTC, so the time says so. The seconds name the moment.
const AS_OF = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "long",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  timeZone: "UTC",
  timeZoneName: "short",
});

export function asOf(iso: string): string {
  return AS_OF.format(new Date(iso));
}

/** The reading statuses in the order of an item's life, with a word for a person. */
export const STATUS_WORDS: [ItemStatus, string][] = [
  ["inbox", "Inbox"],
  ["queued", "Queued"],
  ["reading", "Reading"],
  ["done", "Done"],
  ["archived", "Archived"],
];

/** The states of the pipeline in order, with the step that each one waits for. */
export const ENRICH_WORDS: [EnrichState, string][] = [
  ["pending", "Waits for the fetch"],
  ["fetched", "Waits for the card"],
  ["enriched", "Waits for the vectors"],
  ["ready", "Ready"],
  ["failed", "Failed"],
];

/** What the jobs of each kind do (the handlers in apps/api/src/unshelve/pipeline). */
export const KIND_WORDS: Record<JobKind, string> = {
  ingest_capture: "Makes an item from a message",
  ingest_album: "Makes one item from an album",
  fetch_item: "Reads the source of an item",
  enrich_item: "Asks the model for the card",
  embed_item: "Stores the vectors for search",
  score_item: "Scores an item against your goals",
  send_digest: "Sends the weekly digest",
  stale_sweep: "Not used: the digest job does it",
  publish_feed: "Writes the public feed",
  collect_batch: "Reads the answers of a batch",
};
