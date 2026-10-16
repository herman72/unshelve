import { GoalActions } from "@/components/goal-actions";
import type { Goal } from "@/lib/goal-fields";

/** The dates of a goal in words, or nothing when it has none. The API sends ISO dates. */
function period(goal: Goal): string | null {
  if (goal.starts_on && goal.ends_on) return `${goal.starts_on} to ${goal.ends_on}`;
  if (goal.starts_on) return `From ${goal.starts_on}`;
  if (goal.ends_on) return `Until ${goal.ends_on}`;
  return null;
}

function GoalCard({ goal }: { goal: Goal }) {
  const dates = period(goal);
  return (
    <article className="rounded-lg border border-border bg-surface p-4">
      <h2 className="flex flex-wrap items-center gap-x-2 gap-y-1 text-base leading-snug font-semibold">
        {/* A goal is written by the user, in English or Persian. */}
        <span dir="auto" className="wrap-anywhere">
          {goal.title}
        </span>
        {goal.is_focus && (
          <span className="rounded-full border border-border px-2 py-0.5 text-xs font-normal">
            Focus
          </span>
        )}
        {goal.archived_at !== null && (
          <span className="rounded-full border border-border px-2 py-0.5 text-xs font-normal text-muted">
            Archived
          </span>
        )}
      </h2>

      {goal.description && (
        <p dir="auto" className="mt-2 wrap-anywhere">
          {goal.description}
        </p>
      )}

      {goal.keywords.length > 0 && (
        <ul role="list" aria-label="Keywords" className="mt-3 flex flex-wrap gap-2">
          {goal.keywords.map((keyword) => (
            <li
              key={keyword}
              dir="auto"
              className="rounded-md border border-border px-2 py-0.5 text-xs text-muted"
            >
              {keyword}
            </li>
          ))}
        </ul>
      )}

      <p className="mt-3 text-sm text-muted">
        {dates && <>{dates} · </>}
        {/* The version goes up when the title, the description or the keywords change. */}
        Version {goal.version}
      </p>
      <div className="mt-3">
        <GoalActions goal={goal} />
      </div>
    </article>
  );
}

function Cards({ goals, label }: { goals: readonly Goal[]; label: string }) {
  return (
    <ul role="list" aria-label={label} className="flex flex-col gap-3">
      {goals.map((goal) => (
        <li key={goal.id}>
          <GoalCard goal={goal} />
        </li>
      ))}
    </ul>
  );
}

/** The active goals, and under them the archived goals. The API sends them in this order. */
export function GoalList({ goals }: { goals: readonly Goal[] }) {
  const active = goals.filter((goal) => goal.archived_at === null);
  const archived = goals.filter((goal) => goal.archived_at !== null);
  return (
    <>
      {active.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-muted">
          You have no goals yet. A goal tells Unshelve what to look for in what you save.
        </p>
      ) : (
        <Cards goals={active} label="Active goals" />
      )}
      {archived.length > 0 && (
        <section className="mt-8">
          <h2 className="text-sm font-semibold tracking-wide text-muted uppercase">
            Archived goals
          </h2>
          <div className="mt-3">
            <Cards goals={archived} label="Archived goals" />
          </div>
        </section>
      )}
    </>
  );
}
