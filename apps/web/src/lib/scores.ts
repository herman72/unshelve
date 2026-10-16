import type { components } from "@unshelve/api-client";

export type GoalScore = components["schemas"]["GoalScore"];

// The scale of the model that scores an item against a goal (apps/api relevance/prompt.py).
const WORDS = ["No link", "Touches the goal", "Helps the goal", "Directly useful"] as const;

/** A score from 0 to 3 in words, for a person: "Helps the goal (2 of 3)". */
export function scoreWords(score: number): string {
  return `${WORDS[score] ?? "Unknown"} (${score} of 3)`;
}
