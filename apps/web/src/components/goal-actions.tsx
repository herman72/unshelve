"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { GoalForm } from "@/components/goal-form";
import { browserApi } from "@/lib/browser-api";
import { type Goal, goalMessage } from "@/lib/goal-fields";

const BUTTON =
  "rounded-md border border-muted px-3 py-1.5 text-sm font-medium hover:bg-surface disabled:opacity-60";

/**
 * The buttons of one goal: make it the focus, change it, archive it or restore it.
 *
 * A refused request (409) can mean that the goal changed in another tab, so the page
 * fetches the goals again after it too.
 */
export function GoalActions({ goal }: { goal: Goal }) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [refreshing, startRefresh] = useTransition();

  async function send(request: "focus" | "archive" | "restore") {
    setSending(true);
    setMessage(null);
    let status: number;
    let problem;
    try {
      const path = { params: { path: { goal_id: goal.id } } };
      const { response, error } =
        request === "focus"
          ? await browserApi.POST("/api/v1/goals/{goal_id}/focus", path)
          : await browserApi.PATCH("/api/v1/goals/{goal_id}", {
              ...path,
              body: { archived: request === "archive" },
            });
      status = response.status;
      problem = error;
    } catch {
      setSending(false);
      setMessage("Cannot reach the API. Check that it runs, then try again.");
      return;
    }
    setSending(false);
    setMessage(goalMessage(status, problem));
    if (status === 200 || status === 409) startRefresh(() => router.refresh());
  }

  if (editing) return <GoalForm goal={goal} onDone={() => setEditing(false)} />;

  const busy = sending || refreshing;
  const archived = goal.archived_at !== null;
  return (
    <div>
      <div role="group" aria-label={`Change ${goal.title}`} className="flex flex-wrap gap-2">
        {!archived && !goal.is_focus && (
          <button type="button" onClick={() => send("focus")} disabled={busy} className={BUTTON}>
            Make it the focus
          </button>
        )}
        {!archived && (
          <button type="button" onClick={() => setEditing(true)} disabled={busy} className={BUTTON}>
            Edit
          </button>
        )}
        <button
          type="button"
          onClick={() => send(archived ? "restore" : "archive")}
          disabled={busy}
          className={BUTTON}
        >
          {archived ? "Restore" : "Archive"}
        </button>
      </div>
      <p role="status" className="mt-2 text-sm text-muted">
        {message}
      </p>
    </div>
  );
}
