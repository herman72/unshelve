import { type VectorSearch, vectorNote } from "@/lib/search";

/**
 * A line when search found items by their words only. The results are still right, but an item
 * that says the same thing in other words can be missing, so the page says so.
 */
export function VectorNote({ state }: { state: VectorSearch }) {
  const note = vectorNote(state);
  if (note === null) return null;
  return (
    <p role="note" className="mb-4 text-sm text-warning">
      {note}
    </p>
  );
}
