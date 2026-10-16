import type { components } from "@unshelve/api-client";

export type Goal = components["schemas"]["GoalOut"];
type GoalCreate = components["schemas"]["GoalCreate"];
type Problem = components["schemas"]["Problem"];

/**
 * The body of a request for a goal, from the fields of its form.
 *
 * The keywords are one field: a comma or a new line ends a keyword. An empty date field is no
 * date (null), so that an edit can remove a date.
 */
export function goalBody(form: FormData): GoalCreate {
  const text = (name: string) => String(form.get(name) ?? "").trim();
  const date = (name: string) => text(name) || null;
  return {
    title: text("title"),
    description: text("description"),
    keywords: text("keywords")
      .split(/[,\n]/)
      .map((keyword) => keyword.trim())
      .filter((keyword) => keyword !== ""),
    starts_on: date("starts_on"),
    ends_on: date("ends_on"),
  };
}

const FIELD_NAMES: Record<string, string> = {
  title: "the title",
  description: "the description",
  keywords: "a keyword",
  starts_on: "the start date",
  ends_on: "the end date",
};

/** What the API answered, in words for a person. Null when the page shows the answer itself. */
export function goalMessage(status: number, problem: Problem | undefined): string | null {
  if (status === 200 || status === 201) return null;
  if (status === 409) return `Not saved: ${problem?.detail ?? "the goal clashes with another"}.`;
  if (status === 422) {
    // A field that the schema refused names its place: "/body/keywords/2".
    const field = problem?.errors?.[0]?.pointer?.split("/")[2];
    if (field) return `Check ${FIELD_NAMES[field] ?? field}: ${problem?.errors?.[0]?.detail}.`;
    return `Not saved: ${problem?.detail ?? "a field is not valid"}.`;
  }
  if (status === 401) {
    return "You are signed out. Send /login to your Unshelve bot to get a sign-in link.";
  }
  if (status === 404) return "This goal does not exist any more.";
  return `The API answered with status ${status}. Try again in a moment.`;
}
