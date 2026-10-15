import type { components } from "@unshelve/api-client";

export type EnrichState = components["schemas"]["EnrichState"];

/**
 * Whether the worker is at work on the item. Then it cannot be sent back to the start: the API
 * answers 409, because a second start would race with the step that runs.
 */
export function inPipeline(state: EnrichState): boolean {
  return state === "pending" || state === "fetched" || state === "enriched";
}

/** The state of the pipeline in one word, for a person. */
export function enrichLabel(state: EnrichState): string {
  if (state === "ready") return "Ready";
  if (state === "failed") return "Failed";
  return "Processing";
}
