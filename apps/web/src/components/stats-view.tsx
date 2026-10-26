import { useId } from "react";
import {
  asOf,
  budgetUsed,
  count,
  days,
  dollars,
  duration,
  ENRICH_WORDS,
  isIdle,
  isLongWait,
  type JobKindStats,
  KIND_WORDS,
  type LlmCosts,
  LONG_WAIT_SECONDS,
  type Stats,
  STATUS_WORDS,
  warningsOf,
} from "@/lib/stats";

function Section({
  title,
  about,
  children,
}: {
  title: string;
  about: string;
  children: React.ReactNode;
}) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="mt-8">
      <h2 id={id} className="text-lg font-semibold tracking-tight">
        {title}
      </h2>
      <p className="mt-1 text-sm text-muted">{about}</p>
      <div className="mt-4">{children}</div>
    </section>
  );
}

/**
 * One number with its name. A warning is words, in the colour of a warning: the colour alone
 * would not reach a person who cannot see it.
 */
function Figure({
  label,
  value,
  note,
  warning,
}: {
  label: string;
  value: string;
  note?: string;
  warning?: string;
}) {
  return (
    <div className="rounded-lg bg-surface px-4 py-3">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className={`mt-1 text-2xl font-semibold tabular-nums ${warning ? "text-warning" : ""}`}>
        {value}
      </dd>
      {warning ? (
        <dd className="mt-1 text-sm font-medium text-warning">{warning}</dd>
      ) : (
        note && <dd className="mt-1 text-sm text-muted">{note}</dd>
      )}
    </div>
  );
}

/**
 * A table that can be wider than a phone. It scrolls inside its own box, not the page, and the
 * box takes the keyboard focus: a person without a mouse or touch can then scroll it with the
 * arrow keys. Chrome 132 and newer focus such a box without `tabIndex`; other browsers do not
 * promise it. The caption names the box, as in "Data Tables" of Inclusive Components.
 *
 * The caption is a paragraph above the box, not a <caption>: a <caption> is as wide as its
 * table, so on a phone its end would scroll out of sight with the table.
 */
function WideTable({
  caption,
  head,
  children,
}: {
  caption: string;
  head: string[];
  children: React.ReactNode;
}) {
  const id = useId();
  return (
    <>
      <p id={id} className="pb-2 text-sm text-muted">
        {caption}
      </p>
      <div role="group" aria-labelledby={id} tabIndex={0} className="overflow-x-auto">
        <table aria-labelledby={id} className="w-full text-sm">
          <thead>
            <tr className="border-b border-border">
              {head.map((name, index) => (
                <th
                  key={name}
                  scope="col"
                  className={`px-2 py-2 align-bottom font-medium first:pl-0 ${index === 0 ? "text-left" : "text-right"}`}
                >
                  {name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>{children}</tbody>
        </table>
      </div>
    </>
  );
}

function Cell({ children, warn = false }: { children: React.ReactNode; warn?: boolean }) {
  return (
    <td
      className={`px-2 py-2 text-right whitespace-nowrap tabular-nums ${warn ? "font-semibold text-warning" : ""}`}
    >
      {children}
    </td>
  );
}

function KindRow({ kind }: { kind: JobKindStats }) {
  const warnings = warningsOf(kind);
  const wait = kind.oldest_ready_seconds;
  return (
    <tr className={`border-b border-border align-top ${isIdle(kind) ? "text-muted" : ""}`}>
      <th scope="row" className="min-w-44 py-2 pr-2 text-left font-normal">
        <span className="font-mono">{kind.kind}</span>
        <span className="block text-xs text-muted">{KIND_WORDS[kind.kind]}</span>
        {warnings.length > 0 && (
          <span className="block text-xs font-medium text-warning">
            Needs a look: {warnings.join("; ")}
          </span>
        )}
      </th>
      <Cell>{count(kind.ready)}</Cell>
      <Cell>{count(kind.scheduled)}</Cell>
      <Cell>{count(kind.running)}</Cell>
      <Cell warn={kind.dead > 0}>{count(kind.dead)}</Cell>
      <Cell>{count(kind.retrying)}</Cell>
      <Cell>{count(kind.done_last_24_hours)}</Cell>
      <Cell warn={kind.dead_last_24_hours > 0}>{count(kind.dead_last_24_hours)}</Cell>
      <Cell warn={isLongWait(wait)}>{wait === null ? "none" : duration(wait)}</Cell>
    </tr>
  );
}

function QueuePart({ queue }: { queue: Stats["queue"] }) {
  const flagged = queue.by_kind.filter((kind) => warningsOf(kind).length > 0);
  const wait = queue.oldest_ready_seconds;
  return (
    <Section title="Job queue" about="Every job of this Unshelve, whoever started it.">
      <dl className="grid gap-3 sm:grid-cols-3">
        <Figure label="Jobs to do" value={count(queue.depth)} note="Ready, scheduled and running" />
        <Figure
          label="Dead jobs"
          value={count(queue.dead)}
          note="Failed for good"
          warning={queue.dead > 0 ? "No worker tries them again" : undefined}
        />
        <Figure
          label="Oldest wait"
          value={wait === null ? "None" : duration(wait)}
          note={wait === null ? "No job is ready" : "Of a ready job"}
          warning={
            isLongWait(wait)
              ? `Over ${duration(LONG_WAIT_SECONDS)}: is a worker running?`
              : undefined
          }
        />
      </dl>
      {flagged.length > 0 ? (
        <p className="mt-4 font-medium text-warning">
          {flagged.length === 1 ? "1 kind needs" : `${flagged.length} kinds need`} a look:{" "}
          {flagged.map((kind) => kind.kind).join(", ")}.
        </p>
      ) : (
        <p className="mt-4 text-muted">
          No dead jobs, and no job waits longer than {duration(LONG_WAIT_SECONDS)}.
        </p>
      )}
      <div className="mt-4">
        <WideTable
          caption="The jobs of each kind. A retrying job failed an attempt: it is also in ready or scheduled."
          head={[
            "Kind",
            "Ready",
            "Scheduled",
            "Running",
            "Dead",
            "Retrying",
            "Done in 24 h",
            "Dead in 24 h",
            "Oldest wait",
          ]}
        >
          {queue.by_kind.map((kind) => (
            <KindRow key={kind.kind} kind={kind} />
          ))}
        </WideTable>
      </div>
    </Section>
  );
}

function CostsPart({ llm }: { llm: LlmCosts }) {
  const used = budgetUsed(llm);
  // A budget of $0 is used before the first call, so its bar is full.
  const share =
    llm.daily_budget_usd > 0 ? Math.min(llm.spent_today_usd / llm.daily_budget_usd, 1) : 1;
  return (
    <Section title="Model costs" about="Every paid answer of the model, from the ledger.">
      <p>
        <span className="text-2xl font-semibold tabular-nums">
          {dollars(llm.spent_today_usd)}
        </span>{" "}
        of the daily budget of {dollars(llm.daily_budget_usd)} spent today.
      </p>
      <div aria-hidden className="mt-2 h-2 overflow-hidden rounded-full bg-surface">
        <div
          className={`h-full rounded-full ${used ? "bg-warning" : "bg-accent"}`}
          style={{ width: `${share * 100}%` }}
        />
      </div>
      {used && (
        <div className="mt-4 rounded-lg border border-warning px-4 py-3">
          <p className="font-medium text-warning">The daily budget is used.</p>
          <p className="mt-1 text-sm">
            New model calls wait for midnight in <code>UNSHELVE_TIMEZONE</code>, when the budget
            starts again. Until then, the cards and the scores of new items wait, and a search
            gets no overview.
          </p>
        </div>
      )}
      <p className="mt-3 text-sm text-muted">
        Today starts at midnight in <code>UNSHELVE_TIMEZONE</code>. It leaves out the answers of a
        batch, as the budget does: you accepted the price of a batch before it ran.
      </p>
      <dl className="mt-4 grid gap-3 sm:grid-cols-3">
        <Figure
          label="Last 30 days"
          value={dollars(llm.spent_last_30_days_usd)}
          note="With the answers of batches"
        />
      </dl>
      <div className="mt-4">
        {llm.by_task.length === 0 ? (
          <p className="text-muted">No paid model calls in the last 30 days.</p>
        ) : (
          <WideTable
            caption="The last 30 days for each task, the most expensive first."
            head={["Task", "Calls", "Input tokens", "Output tokens", "Cost"]}
          >
            {llm.by_task.map((task) => (
              <tr key={task.task} className="border-b border-border">
                <th scope="row" className="py-2 pr-2 text-left font-mono font-normal">
                  {task.task}
                </th>
                <Cell>{count(task.calls)}</Cell>
                <Cell>{count(task.input_tokens)}</Cell>
                <Cell>{count(task.output_tokens)}</Cell>
                <Cell>{dollars(task.cost_usd)}</Cell>
              </tr>
            ))}
          </WideTable>
        )}
      </div>
    </Section>
  );
}

function CountList({ title, rows }: { title: string; rows: [string, number][] }) {
  return (
    <div className="rounded-lg border border-border px-4 py-3">
      <h3 className="text-sm font-medium">{title}</h3>
      <dl className="mt-2 grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-sm">
        {rows.map(([word, value]) => (
          <div key={word} className="contents">
            <dt className={value === 0 ? "text-muted" : ""}>{word}</dt>
            <dd className={`text-right tabular-nums ${value === 0 ? "text-muted" : ""}`}>
              {count(value)}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function ItemsPart({ items }: { items: Stats["items"] }) {
  const median = items.median_unread_age_days;
  return (
    <Section title="Items" about="Your items. A merged copy of another item is left out.">
      <dl className="grid gap-3 sm:grid-cols-2">
        <Figure label="Read in the last 7 days" value={count(items.read_last_7_days)} />
        <Figure
          label="Median age of the unread items"
          value={median === null ? "None" : days(median)}
          note={
            median === null
              ? "No item waits to be read"
              : "Since the last save, without the archived items"
          }
        />
      </dl>
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <CountList
          title="By reading status"
          rows={STATUS_WORDS.map(([status, word]) => [word, items.by_status[status] ?? 0])}
        />
        <CountList
          title="By processing state"
          rows={ENRICH_WORDS.map(([state, word]) => [word, items.by_enrich_state[state] ?? 0])}
        />
      </div>
    </Section>
  );
}

/** The answer of GET /api/v1/stats: the queue, the costs and the items, at one moment. */
export function StatsView({ stats }: { stats: Stats }) {
  return (
    <>
      <p className="text-sm text-muted">
        The numbers of <time dateTime={stats.as_of}>{asOf(stats.as_of)}</time>, by the clock of
        the database. Reload the page for newer numbers.
      </p>
      <QueuePart queue={stats.queue} />
      <CostsPart llm={stats.llm} />
      <ItemsPart items={stats.items} />
    </>
  );
}
