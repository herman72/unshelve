import type { components } from "@unshelve/api-client";

export type DigestPick = components["schemas"]["DigestPick"];
export type DigestPreview = components["schemas"]["DigestPreview"];

/** The reading times that the page offers, in minutes. The API takes 5 to 600. */
export const BUDGETS = [15, 30, 60, 120] as const;
export const DEFAULT_BUDGET = 60;

/** The reading time of a query value: one of BUDGETS, or else the default. */
export function budgetFrom(value: string | string[] | undefined): number {
  const minutes = Number(Array.isArray(value) ? value[0] : value);
  return BUDGETS.some((budget) => budget === minutes) ? minutes : DEFAULT_BUDGET;
}

/**
 * Why a pick is in the digest, in words (resurface/selection.py). The type has a key for each
 * reason of the API, so a new reason fails the type check until it has its words here.
 */
export const REASONS: Record<DigestPick["reason"], string> = {
  focus_goal: "For your focus goal",
  other_goal: "For your goal",
  saved_again: "You saved it more than one time",
  recent: "You saved it in the last 7 days",
  random: "A random pick from your inbox",
};
