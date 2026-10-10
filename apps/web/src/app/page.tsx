import { Suspense } from "react";
import { ApiStatus, StatusLine } from "@/components/api-status";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-6 px-6 py-24">
      <h1 className="text-3xl font-semibold tracking-tight">Unshelve</h1>
      <p className="text-zinc-600 dark:text-zinc-400">
        A goal-aware reading inbox. The inbox itself arrives with the next milestones.
      </p>
      {/* The status is read at request time, so it streams in after the static shell. */}
      <Suspense fallback={<StatusLine ok={null}>Checking the API…</StatusLine>}>
        <ApiStatus />
      </Suspense>
    </main>
  );
}
