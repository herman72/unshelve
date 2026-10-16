"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { browserApi } from "@/lib/browser-api";
import { type ItemStatus, moveLabel } from "@/lib/reading-status";

/** What the API answered, in words for a person. Null when the page itself shows the answer. */
function messageFor(status: number, detail: string | null | undefined): string | null {
  switch (status) {
    case 200:
      return null; // the page shows the new status
    case 409:
      // The API says why: the move is not in the table, or the reading queue is full.
      return `Not moved: ${detail ?? "the item cannot move now"}.`;
    case 401:
      return "You are signed out. Send /login to your Unshelve bot to get a sign-in link.";
    case 404:
      return "This item does not exist any more.";
    default:
      return `The API answered with status ${status}. Try again in a moment.`;
  }
}

/**
 * A button for each move that the API allows for the item now.
 *
 * The POST goes through this app's /api route to the API. Then the server components of the
 * page fetch the item and its history again: the page shows the new status, its new buttons,
 * and the move in the history.
 */
export function StatusButtons({
  itemId,
  status,
  allowed,
}: {
  itemId: number;
  status: ItemStatus;
  allowed: ItemStatus[];
}) {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);
  const [moving, setMoving] = useState<ItemStatus | null>(null);
  const [refreshing, startRefresh] = useTransition();

  async function move(target: ItemStatus) {
    setMoving(target);
    setMessage(null);
    let answer: number;
    let detail: string | null | undefined;
    try {
      const { response, error } = await browserApi.POST("/api/v1/items/{item_id}/transition", {
        params: { path: { item_id: itemId } },
        body: { to: target },
      });
      answer = response.status;
      detail = error?.detail;
    } catch {
      setMoving(null);
      setMessage("Cannot reach the API. Check that it runs, then try again.");
      return;
    }
    setMoving(null);
    setMessage(messageFor(answer, detail));
    // A 409 can mean that the item moved in another tab or in Telegram: the page shows where.
    if (answer === 200 || answer === 409) startRefresh(() => router.refresh());
  }

  const busy = moving !== null || refreshing;
  return (
    <div>
      <div role="group" aria-label="Move the item" className="flex flex-wrap gap-2">
        {allowed.map((target) => (
          <button
            key={target}
            type="button"
            onClick={() => move(target)}
            disabled={busy}
            className="rounded-md border border-muted px-3 py-1.5 text-sm font-medium hover:bg-surface disabled:opacity-60"
          >
            {moving === target ? "Moving…" : moveLabel(status, target)}
          </button>
        ))}
      </div>
      <p role="status" className="mt-2 text-sm text-muted">
        {message}
      </p>
    </div>
  );
}
