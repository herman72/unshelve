import type { components } from "@unshelve/api-client";

export type ItemStatus = components["schemas"]["ItemStatus"];

/**
 * The words of the button that moves an item from `current` to `target`.
 *
 * The API gives the moves (`allowed_transitions`), and the page shows a button for each one.
 * One target can have more names, because the same move means different things for a person:
 * from "reading" to "queued" is "Stop reading", and from "done" to "queued" is "Read it again".
 * No button says "Back to the inbox": that is the link at the top of the page, to the list.
 */
export function moveLabel(current: ItemStatus, target: ItemStatus): string {
  switch (target) {
    case "inbox":
      return current === "queued" ? "Remove from the queue" : "Move to the inbox";
    case "queued":
      if (current === "reading") return "Stop reading";
      if (current === "done") return "Read it again";
      return "Add to the queue";
    case "reading":
      return "Start reading";
    case "done":
      return "Mark as read";
    case "archived":
      return "Archive";
  }
}

/** Where the item is now, in a sentence. */
export function statusSentence(status: ItemStatus): string {
  switch (status) {
    case "inbox":
      return "In the inbox.";
    case "queued":
      return "In the reading queue.";
    case "reading":
      return "You are reading it.";
    case "done":
      return "Read.";
    case "archived":
      return "Archived.";
  }
}
