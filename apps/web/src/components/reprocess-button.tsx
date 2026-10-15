"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { browserApi } from "@/lib/browser-api";

/** What the API answered, in words for a person. */
function messageFor(status: number): string {
  switch (status) {
    case 202:
      return "Sent back to the start. The worker reads it again; the page shows its state.";
    case 409:
      return "The worker is at work on it already. The page shows its state now.";
    case 401:
      return "You are signed out. Send /login to your Unshelve bot to get a sign-in link.";
    case 404:
      return "This item does not exist any more.";
    default:
      return `The API answered with status ${status}. Try again in a moment.`;
  }
}

/**
 * Send a finished or failed item back to the start of its pipeline.
 *
 * The POST goes through this app's /api route to the API. Then the server components of the
 * page fetch the item again, and the page shows its new state.
 */
export function ReprocessButton({ itemId }: { itemId: number }) {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [refreshing, startRefresh] = useTransition();

  async function reprocess() {
    setSending(true);
    setMessage(null);
    let status: number;
    try {
      const { response } = await browserApi.POST("/api/v1/items/{item_id}/reprocess", {
        params: { path: { item_id: itemId } },
      });
      status = response.status;
    } catch {
      setSending(false);
      setMessage("Cannot reach the API. Check that it runs, then try again.");
      return;
    }
    setSending(false);
    setMessage(messageFor(status));
    // Both answers change what the page must show: a new state, or the state that won.
    if (status === 202 || status === 409) startRefresh(() => router.refresh());
  }

  const busy = sending || refreshing;
  return (
    <div className="mt-6">
      <button
        type="button"
        onClick={reprocess}
        disabled={busy}
        className="rounded-md border border-muted px-3 py-1.5 text-sm font-medium hover:bg-surface disabled:opacity-60"
      >
        {busy ? "Sending…" : "Process again"}
      </button>
      <p role="status" className="mt-2 text-sm text-muted">
        {message}
      </p>
    </div>
  );
}
