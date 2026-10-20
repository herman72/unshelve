import type { components } from "@unshelve/api-client";
import Form from "next/form";
import { KIND_CHOICES, STATUS_CHOICES, type Search, searchHref } from "@/lib/search";

type Goal = components["schemas"]["GoalOut"];

const FIELD = "rounded-md border border-muted bg-background px-2 py-1.5";

/**
 * The goals to filter by: the active ones, then the archived ones. A goal of the address that
 * is not in the list stays a choice too, so the form never hides a filter that the API used.
 */
function GoalSelect({ goals, goalId }: { goals: readonly Goal[]; goalId: number | null }) {
  const active = goals.filter((goal) => goal.archived_at === null);
  const archived = goals.filter((goal) => goal.archived_at !== null);
  const unknown = goalId !== null && !goals.some((goal) => goal.id === goalId);
  if (goals.length === 0 && !unknown) return null;

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor="search-goal" className="text-muted">
        Goal
      </label>
      <select
        id="search-goal"
        name="goal_id"
        defaultValue={goalId === null ? "" : String(goalId)}
        className={FIELD}
      >
        <option value="">Any goal</option>
        {active.map((goal) => (
          <option key={goal.id} value={goal.id}>
            {goal.title}
          </option>
        ))}
        {archived.length > 0 && (
          <optgroup label="Archived goals">
            {archived.map((goal) => (
              <option key={goal.id} value={goal.id}>
                {goal.title}
              </option>
            ))}
          </optgroup>
        )}
        {unknown && <option value={goalId}>Goal {goalId}, not found</option>}
      </select>
    </div>
  );
}

/**
 * The search box and its filters. Form submits with GET and navigates in the browser, so the
 * search is the address of the page: Back, a bookmark and a reload all show the same search.
 * It still works without JavaScript.
 *
 * Without a search (the fallback while the page reads the address), the form is empty.
 */
export function SearchForm({
  search,
  goals = [],
}: {
  search?: Search;
  goals?: readonly Goal[];
}) {
  return (
    <Form action="/search" role="search" className="mb-6">
      {/* The fields start again when the search changes, also after Back: the browser keeps
          their values, and React sets a default value only when a field first appears. The key
          is on this box and not on the Form, because next/form does not support a key on a
          Form whose action is an address. */}
      <div key={search ? searchHref(search) : ""}>
        <div className="flex gap-2">
          <label htmlFor="q" className="sr-only">
            Search your saved items
          </label>
          <input
            id="q"
            name="q"
            type="search"
            defaultValue={search?.q}
            placeholder="Search your saved items"
            required
            maxLength={500}
            // The query can be English or Persian. 500 is the longest query the API accepts.
            dir="auto"
            className="min-w-0 flex-1 rounded-md border border-muted bg-background px-3 py-2 placeholder:text-muted"
          />
          <button
            type="submit"
            className="rounded-md bg-foreground px-4 py-2 font-medium text-background hover:opacity-90"
          >
            Search
          </button>
        </div>

        <div className="mt-3 flex flex-wrap items-end gap-x-6 gap-y-3 text-sm">
          {/* No box ticked: every status. Each ticked box adds one: status=inbox&status=done. */}
          <fieldset>
            <legend className="text-muted">Status</legend>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
              {STATUS_CHOICES.map(({ value, label }) => (
                <label key={value} className="inline-flex items-center gap-1.5">
                  <input
                    type="checkbox"
                    name="status"
                    value={value}
                    defaultChecked={search?.status.includes(value)}
                  />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>

          {/* A label around a select would add the chosen option to its name: "Kind Any kind". */}
          <div className="flex flex-col gap-1">
            <label htmlFor="search-kind" className="text-muted">
              Kind
            </label>
            <select
              id="search-kind"
              name="kind"
              defaultValue={search?.kind ?? ""}
              className={FIELD}
            >
              <option value="">Any kind</option>
              {KIND_CHOICES.map(({ value, label }) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>

          <GoalSelect goals={goals} goalId={search?.goalId ?? null} />
        </div>
      </div>
    </Form>
  );
}
