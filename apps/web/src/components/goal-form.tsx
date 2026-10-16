"use client";

import { useRouter } from "next/navigation";
import { useId, useState, useTransition } from "react";
import { browserApi } from "@/lib/browser-api";
import { type Goal, goalBody, goalMessage } from "@/lib/goal-fields";

const FIELD = "mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm";
const BUTTON =
  "rounded-md border border-muted px-3 py-1.5 text-sm font-medium hover:bg-surface disabled:opacity-60";

/**
 * The form of a goal. Without `goal` it adds a goal; with `goal` it changes that goal.
 *
 * The request goes through this app's /api route to the API. After a save, the server
 * components of the page fetch the goals again. A refused save keeps what you typed, and
 * the message says why.
 */
export function GoalForm({ goal, onDone }: { goal?: Goal; onDone?: () => void }) {
  const router = useRouter();
  const id = useId();
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [refreshing, startRefresh] = useTransition();

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const body = goalBody(new FormData(form));
    setSaving(true);
    setMessage(null);
    let status: number;
    let problem;
    try {
      const { response, error } = goal
        ? await browserApi.PATCH("/api/v1/goals/{goal_id}", {
            params: { path: { goal_id: goal.id } },
            body,
          })
        : await browserApi.POST("/api/v1/goals", { body });
      status = response.status;
      problem = error;
    } catch {
      setSaving(false);
      setMessage("Cannot reach the API. Check that it runs, then try again.");
      return;
    }
    setSaving(false);
    setMessage(goalMessage(status, problem));
    if (status === 200 || status === 201) {
      if (!goal) form.reset();
      onDone?.();
      startRefresh(() => router.refresh());
    }
  }

  const busy = saving || refreshing;
  return (
    <form
      onSubmit={save}
      aria-label={goal ? `Change the goal ${goal.title}` : "Add a goal"}
      className="space-y-3"
    >
      <label className="block text-sm font-medium" htmlFor={`${id}-title`}>
        Title
        <input
          id={`${id}-title`}
          name="title"
          required
          maxLength={200}
          dir="auto"
          defaultValue={goal?.title}
          className={FIELD}
        />
      </label>
      <label className="block text-sm font-medium" htmlFor={`${id}-description`}>
        What it means for you
        <textarea
          id={`${id}-description`}
          name="description"
          rows={3}
          dir="auto"
          defaultValue={goal?.description}
          className={FIELD}
        />
      </label>
      <label className="block text-sm font-medium" htmlFor={`${id}-keywords`}>
        Keywords, with a comma between them
        <input
          id={`${id}-keywords`}
          name="keywords"
          dir="auto"
          defaultValue={goal?.keywords.join(", ")}
          className={FIELD}
        />
      </label>
      <div className="flex flex-wrap gap-4">
        <label className="block text-sm font-medium" htmlFor={`${id}-starts`}>
          Starts on
          <input
            id={`${id}-starts`}
            name="starts_on"
            type="date"
            defaultValue={goal?.starts_on ?? ""}
            className={FIELD}
          />
        </label>
        <label className="block text-sm font-medium" htmlFor={`${id}-ends`}>
          Ends on
          <input
            id={`${id}-ends`}
            name="ends_on"
            type="date"
            defaultValue={goal?.ends_on ?? ""}
            className={FIELD}
          />
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={busy} className={BUTTON}>
          {busy ? "Saving…" : goal ? "Save the goal" : "Add the goal"}
        </button>
        {onDone && goal && (
          <button type="button" onClick={onDone} disabled={busy} className={BUTTON}>
            Cancel
          </button>
        )}
      </div>
      <p role="status" className="text-sm text-muted">
        {message}
      </p>
    </form>
  );
}
