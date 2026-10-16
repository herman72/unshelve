"use client";

import type { components } from "@unshelve/api-client";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { browserApi } from "@/lib/browser-api";

type GoalLabel = components["schemas"]["GoalLabel"];

const BUTTON =
  "rounded-md border border-muted px-2 py-1 text-xs font-medium hover:bg-surface disabled:opacity-60";

/** What the API answered, in words for a person. Null when the page shows the answer itself. */
function messageFor(status: number, detail: string | null | undefined): string | null {
  switch (status) {
    case 200:
      return null;
    case 409:
      // The score changed after the page showed it: a verdict is about the score that you saw.
      return `Not saved: ${detail ?? "the score changed"}.`;
    case 404:
      return "This score does not exist any more.";
    case 401:
      return "You are signed out. Send /login to your Unshelve bot to get a sign-in link.";
    default:
      return `The API answered with status ${status}. Try again in a moment.`;
  }
}

/**
 * Your verdict on one score: "Right" or "Wrong", and "Undo" after a verdict.
 *
 * The request sends the score that the page shows. When the score changed since, the API
 * answers 409, and the page fetches the item again: it shows the new score.
 */
export function LabelButtons({
  itemId,
  goalId,
  score,
  label,
}: {
  itemId: number;
  goalId: number;
  score: number;
  label: GoalLabel | null;
}) {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [refreshing, startRefresh] = useTransition();

  async function send(verdict: GoalLabel | null) {
    setSending(true);
    setMessage(null);
    let status: number;
    let detail: string | null | undefined;
    try {
      const { response, error } = await browserApi.PUT(
        "/api/v1/items/{item_id}/goals/{goal_id}/label",
        {
          params: { path: { item_id: itemId, goal_id: goalId } },
          body: { label: verdict, score },
        },
      );
      status = response.status;
      detail = error?.detail;
    } catch {
      setSending(false);
      setMessage("Cannot reach the API. Check that it runs, then try again.");
      return;
    }
    setSending(false);
    setMessage(messageFor(status, detail));
    if (status === 200 || status === 409) startRefresh(() => router.refresh());
  }

  const busy = sending || refreshing;
  return (
    <div className="mt-1">
      {label === null ? (
        <div role="group" aria-label="Your verdict" className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted">Is this score right?</span>
          <button
            type="button"
            onClick={() => send("confirmed")}
            disabled={busy}
            className={BUTTON}
          >
            Right
          </button>
          <button
            type="button"
            onClick={() => send("rejected")}
            disabled={busy}
            className={BUTTON}
          >
            Wrong
          </button>
        </div>
      ) : (
        <div role="group" aria-label="Your verdict" className="flex flex-wrap items-center gap-2">
          <span className="text-xs">
            You said: {label === "confirmed" ? "right" : "wrong"}.
          </span>
          <button type="button" onClick={() => send(null)} disabled={busy} className={BUTTON}>
            Undo
          </button>
        </div>
      )}
      <p role="status" className="text-xs text-muted">
        {message}
      </p>
    </div>
  );
}
